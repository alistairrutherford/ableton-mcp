"""Phase 5 in Live: tools keep working across a set load, which re-creates the bridge. It opens a
new project (made in pytest's tmp dir) and then the set that was open before. If that set has
unsaved changes, Live asks about them first and the test stops: save it, then run it again.

    uv run pytest tests/test_reconnect_live.py
"""

import pytest

from ableton_mcp import server

pytestmark = pytest.mark.live


def test_tools_survive_a_set_load(live, monkeypatch, tmp_path, report):
    monkeypatch.setattr(server, "_client", live)
    before = server.get_project_info()
    if not before["set_path"]:
        pytest.skip("the open set has never been saved, so the test couldn't reopen it")
    other = server.create_project("MCP Reconnect", location=str(tmp_path), open_in_live=False)

    reconnects = live.reconnects
    opened = server.open_project(other["set_path"])
    if not opened["opened"]:
        pytest.fail("Live asked %r before opening another set; save the open set and run this again"
                    % opened["live_is_asking"])
    # The bridge was re-created by the load: ordinary tools carry on, on a new connection.
    tracks = server.list_tracks()["tracks"]
    assert [t["name"] for t in tracks] == ["Vocal"]          # the Core Library base, untouched
    assert server.live_status()["ok"] is True
    assert live.reconnects > reconnects

    back = server.open_project(before["set_path"])
    assert back["opened"] is True and server.get_project_info()["set_path"] == before["set_path"]
    report("Phase 5 live: %d reconnect(s) across two set loads; back on %s"
           % (live.reconnects - reconnects, before["name"]))
