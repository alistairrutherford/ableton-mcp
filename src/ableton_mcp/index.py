"""Live's browser as a searchable index: fetched from the Remote Script, cached on disk, ranked here.

The Remote Script walks the browser a few ms per tick and hands over a flat list of loadable
items (index_start / index_status / index_page). Everything else happens in this process, so it
can't stall Live: de-duplicating the Packs tree against the others, working out what kind of
thing each item is, and ranking. Live's browser items carry no tags (docs/spikes.md §4), so
ranking uses the name, the folders above it, the pack, and words in sample filenames.
"""

import difflib
import json
import os
import re
import time

CACHE_DIR = os.path.expanduser(os.environ.get("ABLETON_MCP_CACHE", "~/Library/Caches/ableton-mcp"))
CACHE_VERSION = 1

AUDIO_EXTS = {"wav", "aif", "aiff", "flac", "mp3", "ogg", "m4a"}
CLIP_EXTS = {"alc"}

# What each browser root holds, for items that aren't samples or clip files.
ROOT_KINDS = {
    "instruments": "instrument", "sounds": "sound", "drums": "drum", "audio_effects": "audio_effect",
    "midi_effects": "midi_effect", "plugins": "plugin", "max_for_live": "max_for_live",
    "clips": "clip", "samples": "sample", "packs": "preset", "user_library": "preset",
    "user_folders": "preset",
}
# search(kind=...) -> the entry kinds it covers.
KINDS = {
    "any": None,
    "instrument": {"instrument", "sound"},
    "sound": {"sound"},
    "drum": {"drum"},
    "sample": {"sample"},
    "clip": {"clip"},
    "audio_effect": {"audio_effect"},
    "midi_effect": {"midi_effect"},
    "effect": {"audio_effect", "midi_effect"},
    "plugin": {"plugin"},
    "max_for_live": {"max_for_live"},
    "preset": {"preset", "instrument", "sound", "drum", "audio_effect", "midi_effect"},
}
# Kinds that need a MIDI track.
NEEDS_MIDI = {"instrument", "sound", "drum", "midi_effect"}

# A few words people use for things Live names otherwise.
SYNONYMS = {
    "verb": ["reverb"], "bd": ["kick"], "kik": ["kick"], "sd": ["snare"], "hh": ["hihat", "hat"],
    "hat": ["hihat"], "hats": ["hihat", "hat"], "keys": ["piano", "keys", "ep"], "ep": ["electric"],
    "vox": ["vocal", "voice"], "808": ["808"], "perc": ["percussion", "perc"], "fx": ["fx", "effect"],
}

_FILE_ID = re.compile(r"FileId_(\d+)")
_BPM = re.compile(r"(\d{2,3})\s?bpm", re.I)
_KEY = re.compile(r"(?<![A-Za-z0-9])([A-G])(#|b)?(maj|min|m)?(?![A-Za-z0-9])")


def _tokens(text):
    """Lower-case words of a name, splitting camelCase, digits and punctuation."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [t for t in re.split(r"[^a-z0-9#]+", text.lower()) if t]


def split_ext(name):
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        if ext.lower() in AUDIO_EXTS | CLIP_EXTS | {"adg", "adv", "amxd", "agr"}:
            return stem, ext.lower()
    return name, ""


def entry_kind(e):
    _, ext = split_ext(e["name"])
    if ext in AUDIO_EXTS:
        return "sample"
    if ext in CLIP_EXTS:
        return "clip"
    return ROOT_KINDS.get(e["root"], "preset")


def hints(stem):
    """What a sample's filename says: BPM, key, loop or one-shot."""
    out = {}
    m = _BPM.search(stem)
    if m:
        out["bpm"] = int(m.group(1))
    for m in _KEY.finditer(stem):
        note, accidental, quality = m.groups()
        if accidental or quality:  # a lone capital letter is usually just a word ("A Bass")
            out["key"] = note + (accidental or "") + ("m" if quality in ("m", "min") else "")
            break
    low = stem.lower()
    if "loop" in low:
        out["loop"] = True
    elif re.search(r"one[\s-]?shot|\bshot\b|\bhit\b", low):
        out["loop"] = False
    return out


class Index:
    def __init__(self, raw_entries, fingerprint=None, built_at=None):
        self.fingerprint = fingerprint
        self.built_at = built_at or time.time()
        self.raw = list(raw_entries)
        self.raw_count = len(self.raw)
        self.entries = self._merge(self.raw)
        self.by_id = {e["id"]: e for e in self.entries}
        for e in self.entries:
            for alias in e.get("aliases", ()):
                self.by_id.setdefault(alias, e)

    # -- building ---------------------------------------------------------------

    @staticmethod
    def _merge(raw):
        """One entry per item.

        Live lists one file in several places: a preset under Instruments > Drift and under Sounds
        > a category (the same FileId in both URIs), and everything again under Packs > <pack> >
        ... (a URI with no FileId). Entries with the same FileId are merged first. Then each Packs
        entry joins the entry from the same pack (its `source`) with the same parent folder and
        filename, or else the same filename where that is unique. The merged entry is every kind it
        is listed as, and all its folders count for ranking.
        """
        groups, order, in_packs = {}, [], []
        for e in raw:
            if e["root"] == "packs":
                in_packs.append(e)
                continue
            m = _FILE_ID.search(e["id"])
            key = ("file", m.group(1)) if m else ("id", e["id"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(e)

        parent = lambda e: e["path"][-2] if len(e["path"]) > 1 else None
        by_parent, by_name = {}, {}
        for key in order:
            first = groups[key][0]
            by_parent.setdefault((first.get("source"), parent(first), first["name"]), []).append(key)
            by_name.setdefault((first.get("source"), first["name"]), []).append(key)
        for e in in_packs:
            pack = e["path"][0] if e["path"] else None
            hits = by_parent.get((pack, parent(e), e["name"])) or by_name.get((pack, e["name"])) or []
            if len(hits) == 1:
                groups[hits[0]].append(e)
            else:  # only in Packs, or more than one file it could be
                key = ("id", e["id"])
                groups[key] = [e]
                order.append(key)

        out = []
        for key in order:
            group = groups[key]
            main = next((e for e in group if e["root"] != "packs"), group[0])
            entry = dict(main)
            stem, ext = split_ext(entry["name"])
            entry["stem"], entry["ext"] = stem, ext
            entry["kind"] = entry_kind(entry)
            listed = [e for e in group if e["root"] != "packs"] or group
            entry["kinds"] = sorted({entry_kind(e) for e in listed})
            entry["context"] = sorted({folder for e in group for folder in e["path"][:-1]})
            pack = next((e["path"][0] for e in group if e["root"] == "packs" and e["path"]), None)
            entry["pack"] = pack or entry.get("source")
            entry["library"] = entry.get("source")
            entry["aliases"] = [e["id"] for e in group if e["id"] != entry["id"]]
            if entry["kind"] == "sample":
                entry["hints"] = hints(stem)
            out.append(entry)
        return out

    @classmethod
    def build(cls, client, fingerprint, roots=None, budget_ms=None, timeout=180.0, progress=None):
        """Have the Remote Script walk the browser, wait for it, and read the entries in pages."""
        status = client.call("index_start", roots=roots, budget_ms=budget_ms)
        deadline = time.monotonic() + timeout
        while status["state"] != "done":
            if time.monotonic() > deadline:
                raise TimeoutError("browser index still building after %.0f s (%d entries so far)" % (
                    timeout, status["entries"]))
            time.sleep(0.5)
            status = client.call("index_status")
            if progress:
                progress(status)
        entries, offset = [], 0
        while True:
            page = client.call("index_page", offset=offset, limit=2000)
            entries += page["entries"]
            offset += len(page["entries"])
            if not page["entries"] or offset >= page["total"]:
                break
        index = cls(entries, fingerprint)
        index.build_status = status
        return index

    # -- cache ------------------------------------------------------------------

    @staticmethod
    def cache_path(live_version, cache_dir=None):
        return os.path.join(cache_dir or CACHE_DIR, "browser-index-%s.json" % live_version)

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:   # the raw entries: load() merges them again
            json.dump({"version": CACHE_VERSION, "fingerprint": self.fingerprint, "built_at": self.built_at,
                       "raw": self.raw}, f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, fingerprint):
        """The cached index, or None if there is none or it was built for other browser contents."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if data.get("version") != CACHE_VERSION or data.get("fingerprint") != fingerprint:
            return None
        return cls(data["raw"], data["fingerprint"], data.get("built_at"))

    # -- search -----------------------------------------------------------------

    def get(self, id):
        return self.by_id.get(id)

    def search(self, query, kind="any", limit=20, pack=None):
        if kind not in KINDS:
            raise ValueError("kind must be one of %s" % sorted(KINDS))
        kinds = KINDS[kind]
        q = query.strip().lower()
        q_tokens = _tokens(query)
        expanded = [[t] + SYNONYMS.get(t, []) for t in q_tokens]
        want_pack = pack.lower() if pack else None
        scored = []
        for e in self.entries:
            if kinds is not None and not kinds.intersection(e["kinds"]):
                continue
            if want_pack and want_pack not in (e.get("pack") or "").lower():
                continue
            score = self._score(e, q, q_tokens, expanded)
            if score > 0:
                scored.append((score, e))
        if not scored and q_tokens:  # nothing matched: allow typos
            scored = self._fuzzy(q, kinds, want_pack)
        scored.sort(key=lambda se: (-se[0], len(se[1]["stem"]), se[1]["stem"].lower()))
        return [self.public(e, score) for score, e in scored[:limit]]

    @staticmethod
    def _score(e, q, q_tokens, expanded):
        stem = e["stem"].lower()
        if not q:
            return 1.0
        if stem == q:
            score = 100.0
        elif stem.startswith(q) and not stem[len(q)].isalnum():
            score = 85.0          # the leading words: "kick" -> "Kick 808 Long"
        else:
            name_tokens = _tokens(e["stem"])
            context = set(_tokens(" ".join(e["context"]) + " " + (e.get("pack") or "")))
            in_name = in_context = 0.0
            for alts in expanded:
                if any(nt == a for a in alts for nt in name_tokens):
                    in_name += 1.0    # a whole word: "pad" -> "Sea Pad"
                elif any(nt.startswith(a) for a in alts for nt in name_tokens):
                    in_name += 0.6    # the start of a word: "pad" -> "Paddles Out"
                elif any(ct.startswith(a) for a in alts for ct in context):
                    in_context += 1.0
                else:
                    return 0.0
            score = 40.0 + 40.0 * in_name / len(expanded) + 5.0 * in_context / len(expanded)
            if q in stem:
                score += 5.0
        if e.get("is_device"):
            score += 4.0          # "Drift" the device before presets named after it
        if e["root"] in ("user_library", "user_folders"):
            score += 2.0          # the user's own things
        return score

    def _fuzzy(self, q, kinds, want_pack):
        out = []
        for e in self.entries:
            if kinds is not None and not kinds.intersection(e["kinds"]):
                continue
            if want_pack and want_pack not in (e.get("pack") or "").lower():
                continue
            ratio = difflib.SequenceMatcher(None, q, e["stem"].lower()).ratio()
            if ratio >= 0.75:
                out.append((20.0 * ratio, e))
        return out

    @staticmethod
    def public(e, score=None):
        out = {"id": e["id"], "name": e["stem"], "kind": e["kind"], "path": e["path"],
               "pack": e.get("pack"), "library": e.get("library")}
        if len(e["kinds"]) > 1:
            out["also"] = [k for k in e["kinds"] if k != e["kind"]]
        if e.get("ext"):
            out["file_type"] = e["ext"]
        if e.get("hints"):
            out["hints"] = e["hints"]
        if score is not None:
            out["score"] = round(score, 1)
        return out

    def stats(self):
        kinds = {}
        for e in self.entries:
            for k in e["kinds"]:
                kinds[k] = kinds.get(k, 0) + 1
        return {"entries": len(self.entries), "raw_entries": self.raw_count, "kinds": kinds,
                "built_at": self.built_at, "fingerprint": self.fingerprint}
