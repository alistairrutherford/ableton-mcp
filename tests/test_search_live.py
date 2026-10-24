"""Phase 2 in Live: index the real browser, search it, and load what it finds. Run it in a scratch
set: it adds tracks named "MCP ..." and removes them before and after. It rebuilds the index cache
in ~/Library/Caches/ableton-mcp.

    uv run pytest tests/test_search_live.py
"""

import time

import pytest

from ableton_mcp import server

pytestmark = pytest.mark.live

NAMES = ("MCP Loops", "MCP Sampler")


def _remove_ours(live):
    state = live.call("get_state", include_devices=False)
    for t in reversed(state["tracks"]):
        if t["name"] in NAMES:
            live.call("delete_track", track=t["index"])


@pytest.fixture
def mcp(live, monkeypatch):
    monkeypatch.setattr(server, "_client", live)
    monkeypatch.setattr(server, "_index", None)
    _remove_ours(live)
    yield server
    _remove_ours(live)


def test_index_search_and_load(live, mcp, report):
    t0 = time.monotonic()
    stats = mcp.refresh_index()
    built = mcp._index.build_status
    report("index: %d entries (%d before merging Packs), %s; walk %.0f ms over %d ticks, slowest tick "
           "%.1f ms, %.1f s wall" % (stats["entries"], stats["raw_entries"], stats["kinds"], built["walk_ms"],
                                     built["ticks"], built["slowest_tick_ms"], time.monotonic() - t0))
    assert stats["entries"] > 5000 and stats["raw_entries"] > 2 * stats["entries"]   # Packs merged in
    assert built["slowest_tick_ms"] < 80   # never one long freeze (budget 30 ms per tick)

    drift = mcp.search("drift", kind="instrument")["results"]
    assert drift[0]["name"] == "Drift" and drift[0]["kind"] == "instrument"
    kicks = mcp.search("kick", kind="sample")["results"]
    assert kicks and all(r["kind"] == "sample" for r in kicks)
    pads = mcp.search("pad", kind="sound")["results"]
    assert pads and all("sound" in [r["kind"]] + r.get("also", []) for r in pads)
    reverb = mcp.search("reverb", kind="audio_effect")["results"]
    assert reverb[0]["name"] == "Reverb"
    report("top results: kick -> %s; pad -> %s; reverb -> %s" % (
        [(r["name"], r.get("hints")) for r in kicks[:3]], [r["name"] for r in pads[:3]],
        [r["name"] for r in reverb[:3]]))

    loop = next((r for r in mcp.search("loop", kind="sample", limit=200)["results"]
                 if r.get("hints", {}).get("bpm")), kicks[0])
    tempo = live.call("get_state", include_devices=False)["tempo"]

    views = live.call("view_state")
    mcp.add_track(kind="audio", name="MCP Loops")
    clip = mcp.load_item(loop["id"], "MCP Loops")
    assert live.call("view_state")["Session"] is True
    assert clip["clip"]["name"] == loop["name"] and clip["clip"]["is_audio_clip"]
    assert live.call("get_state", include_devices=False)["tempo"] == tempo   # put back if it changed

    mcp.add_track(kind="midi", name="MCP Sampler")
    sampler = mcp.load_item(loop["id"], "MCP Sampler")
    assert sampler["device"]["class_name"] == "OriginalSimpler"

    fx = mcp.load_item(reverb[0]["id"], "MCP Loops")
    assert fx["device"]["name"] == "Reverb" and fx["device"]["replaced"] is None

    mcp.preview_item(kicks[0]["id"])
    time.sleep(0.5)
    mcp.stop_preview()
    report("views before: %s; loaded %r (%s): clip in slot %s, showed Session view: %s, tempo change %s; "
           "Simpler on MIDI; Reverb on the audio track" % (
               {k: v for k, v in views.items() if v}, loop["name"], loop.get("hints"), clip["scene"],
               clip.get("showed_session_view", False), clip.get("tempo_changed")))
    sounds = mcp.search("ambient", kind="sound")["results"]
    report("sounds for 'ambient': %s" % [(r["name"], r["kind"], r.get("also")) for r in sounds[:4]])
    assert sounds

    # a new MCP process reads the cache
    server._index = None
    t1 = time.monotonic()
    assert mcp.search("drift")["index"]["from_cache"] is True
    report("index from the disk cache in %.2f s" % (time.monotonic() - t1))
