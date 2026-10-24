"""A time-sliced index of Live's browser, advanced from the control surface's update tick.

Walking the whole browser costs about 1.8 s of main-thread time in Live 12.4.7 (about 25k nodes;
see docs/spikes.md), so the walk runs a few milliseconds per tick instead of in one go. It keeps
an iterator per open folder, so even a flat root such as `samples` (5.5k children) never has to
be listed in one tick.

No Live imports, so it can be tested outside Live. INDEX is module-level because Live re-creates
the control surface on every set load while the module stays imported, so an index (and its
id -> BrowserItem map) survives opening another set.
"""

import time

ROOTS = ("instruments", "sounds", "drums", "audio_effects", "midi_effects", "plugins",
         "max_for_live", "clips", "samples", "packs", "user_library", "user_folders")
MAX_DEPTH = 12


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _children(item):
    """An iterator over a browser item's children, without listing them all at once if Live can."""
    it = _safe(lambda: item.iter_children)
    if it is not None:
        try:
            return iter(it)
        except TypeError:
            pass
    return iter(_safe(lambda: list(item.children), []))


def item_id(item, root, path):
    """The id an entry is known by: Live's URI, or the root and path where an item has none."""
    return _safe(lambda: item.uri, "") or "%s:%s" % (root, "/".join(path))


class Indexer:
    def __init__(self, clock=time.time):
        self._clock = clock
        self.budget_ms = 30.0      # per tick: Live 12.4.7's ~24k nodes in about 10 s at its ~100 ms
                                   # tick (15 ms took 25 s)
        self.reset()

    def reset(self):
        self.state = "idle"        # idle, running, done
        self.roots = []
        self.entries = []
        self.items = {}            # id -> BrowserItem, for load_item
        self._stack = []           # [children iterator, root, path, depth]
        self.ms = 0.0
        self.ticks = 0
        self.slowest_ms = 0.0
        self.started = self.finished = None

    def start(self, browser, roots=None):
        roots = list(roots or ROOTS)
        bad = [r for r in roots if r not in ROOTS]
        if bad:
            raise ValueError("unknown browser roots %s; roots: %s" % (bad, ROOTS))
        self.reset()
        self.roots = roots
        self.state = "running"
        self.started = self._clock()
        for root in reversed(roots):
            if root == "user_folders":
                for folder in reversed(_safe(lambda: list(browser.user_folders), [])):
                    self._stack.append([_children(folder), root, [folder.name], 1])
            else:
                self._stack.append([_children(getattr(browser, root)), root, [], 0])

    def step(self, budget_ms=None):
        """Walk for about `budget_ms` (default `self.budget_ms`; at least one node). Returns
        whether there was work to do."""
        if self.state != "running":
            return False
        t0 = self._clock()
        deadline = t0 + (self.budget_ms if budget_ms is None else budget_ms) / 1000.0
        first = True
        while self._stack and (first or self._clock() < deadline):
            first = False
            frame = self._stack[-1]
            child = next(frame[0], None)
            if child is None:
                self._stack.pop()
                continue
            root, path, depth = frame[1], frame[2] + [child.name], frame[3] + 1
            if _safe(lambda: child.is_loadable, False):
                key = item_id(child, root, path)
                self.items[key] = child
                self.entries.append({
                    "id": key, "name": child.name, "root": root, "path": path,
                    "is_device": _safe(lambda: bool(child.is_device), False),
                    "source": _safe(lambda: child.source),
                })
            if depth < MAX_DEPTH:
                self._stack.append([_children(child), root, path, depth])
        took = (self._clock() - t0) * 1000.0
        self.ms += took
        self.ticks += 1
        self.slowest_ms = max(self.slowest_ms, took)
        if not self._stack:
            self.state = "done"
            self.finished = self._clock()
        return True

    def status(self):
        return {"state": self.state, "roots": self.roots, "entries": len(self.entries),
                "budget_ms": self.budget_ms,
                "open_folders": len(self._stack), "ticks": self.ticks, "walk_ms": round(self.ms, 1),
                "slowest_tick_ms": round(self.slowest_ms, 1),
                "wall_s": round(((self.finished or self._clock()) - self.started), 1) if self.started else None}

    def page(self, offset=0, limit=2000):
        return {"total": len(self.entries), "offset": offset,
                "entries": self.entries[offset:offset + limit]}


INDEX = Indexer()
