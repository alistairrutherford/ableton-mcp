"""Phase 1 in Live: add MIDI (with an instrument), audio and return tracks, colour them, swap the
instrument, and roll back a track whose instrument doesn't exist. Run it in a scratch set: it
removes the tracks it adds (named "MCP ...") before and after.

    uv run pytest tests/test_tracks_live.py
"""

import re

import pytest

from ableton_mcp import server

pytestmark = pytest.mark.live

NAMES = ("MCP Lead", "MCP Audio", "MCP Verb", "MCP Broken")


def _bare(name):
    """A track name without the letter Live puts in front of a return track's ("C-MCP Verb")."""
    return re.sub(r"^[A-Z]-", "", name)


def _remove_ours(live):
    state = live.call("get_state", include_devices=False)
    for t in reversed(state["tracks"] + state["return_tracks"]):
        if _bare(t["name"]) in NAMES:
            live.call("delete_track", track=t["index"])


@pytest.fixture
def mcp(live, monkeypatch):
    monkeypatch.setattr(server, "_client", live)
    _remove_ours(live)
    yield server
    _remove_ours(live)


def test_add_tracks_colour_and_swap_instrument(live, mcp, report):
    lead = mcp.add_track(kind="midi", name="MCP Lead", color=5, instrument="Drift")
    assert lead["kind"] == "midi" and lead["color_index"] == 5
    assert lead["instrument"]["name"] == "Drift" and lead["instrument"]["replaced"] is None
    assert [d["name"] for d in lead["devices"]] == ["Drift"]

    audio = mcp.add_track(kind="audio", name="MCP Audio", color="#ff0000")
    assert audio["kind"] == "audio"

    verb = mcp.add_track(kind="return", name="MCP Verb", color=20)
    assert verb["kind"] == "return" and verb["index"].startswith("return:") and verb["color_index"] == 20
    assert re.match(r"^[A-Z]-MCP Verb$", verb["name"])
    assert live.call("get_track", track="MCP Verb")["index"] == verb["index"]   # found without the letter

    # The Phase 0 bug: an instrument preset replaces Drift, and the device count stays at 1.
    swapped = live.load_device_and_wait("Attack Pad", "MCP Lead", "sounds", timeout=20)
    assert swapped["name"] == "Attack Pad" and swapped["replaced"]["name"] == "Drift"
    assert [d["name"] for d in live.call("list_devices", track="MCP Lead")] == ["Attack Pad"]

    count = len(live.call("get_state", include_devices=False)["tracks"])
    with pytest.raises(server.ToolError, match="removed again"):
        mcp.add_track(name="MCP Broken", instrument="No Such Instrument 123")
    assert len(live.call("get_state", include_devices=False)["tracks"]) == count

    listed = mcp.list_tracks()
    ours = {_bare(t["name"]): t for t in listed["tracks"] + listed["return_tracks"] if _bare(t["name"]) in NAMES}
    assert set(ours) == {"MCP Lead", "MCP Audio", "MCP Verb"}
    report("Phase 1 live: #ff0000 became %s (index %s); device ids %s; Attack Pad replaced %s"
           % (audio["color"], audio["color_index"],
              "present" if swapped.get("id") is not None else "absent (matched by name and class)",
              swapped["replaced"]["class_display_name"]))
