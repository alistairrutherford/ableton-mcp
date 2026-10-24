"""Phase 3 in Live: create a project (92 BPM, 6/8, a Keys track with Drift and a Vox audio track),
open it, have the user save it with Cmd-S, and check what Live wrote. It needs the user: to answer
Live's "Save changes?" prompt if the set open before has unsaved changes, and to press Cmd-S.

    uv run pytest tests/test_projects_live.py -s

Projects go in $MCP_TEST_PROJECTS (default: pytest's tmp dir).
"""

import os
import re
import time

import pytest

from ableton_mcp import server
from livebridge import als

pytestmark = pytest.mark.live

WAIT_FOR_USER = float(os.environ.get("MCP_WAIT_FOR_USER", "900"))


def _say(msg):
    print("\n>>> " + msg, flush=True)


def _wait(what, done, timeout=WAIT_FOR_USER):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = done()
        except server.ToolError:   # the bridge restarts while a set loads
            value = None
        if value:
            return value
        time.sleep(0.5)
    pytest.fail("gave up after %.0f s waiting for %s" % (timeout, what))


def _master(text, tag):
    block = re.search(r"<%s>(.*?)</%s>" % (tag, tag), text, re.S).group(1)
    target = re.search(r'<AutomationTarget Id="(\d+)">', block).group(1)
    ev = re.search(r'<PointeeId Value="%s" />\s*</EnvelopeTarget>\s*<Automation>\s*<Events>\s*<\w+ Id="\d+" '
                   r'Time="-63072000" Value="([^"]*)"' % target, text)
    return re.search(r'<Manual Value="([^"]*)"', block).group(1), ev and ev.group(1)


def test_create_open_and_save_a_project(live, monkeypatch, tmp_path, report):
    monkeypatch.setattr(server, "_client", live)
    location = os.environ.get("MCP_TEST_PROJECTS") or str(tmp_path)
    name = "MCP Test %s" % time.strftime("%H%M%S")
    tracks = [{"kind": "midi", "name": "Keys", "instrument": "Drift", "color": 5},
              {"kind": "audio", "name": "Vox"}]

    made = server.create_project(name, tempo=92, signature=[6, 8], tracks=tracks, location=location)
    set_path = made["set_path"]
    assert os.path.isdir(os.path.join(made["project_dir"], "Ableton Project Info"))
    if not made["opened"]:
        _say("Live is asking: %s  Answer it in Live (Save or Don't Save)." % made["live_is_asking"])
        _wait("the new set to open", lambda: server._same_file(server.get_project_info()["set_path"], set_path))
        out = server.open_project(set_path)
        built = out["built"]
        asked = made["live_is_asking"]
    else:
        built, asked = made, None
    assert [t["name"] for t in built["tracks"]] == ["Keys", "Vox"] and not built["errors"]

    info = server.get_project_info()
    assert info["name"] == name and info["project_dir"] == os.path.realpath(made["project_dir"])
    assert info["tempo"] == 92.0 and info["signature"] == [6, 8] and info["live_is_asking"] is None
    keys, vox = info["tracks"]
    assert keys["devices"] == ["Drift"] and keys["color_index"] == 5 and vox["kind"] == "audio"

    mtime = os.path.getmtime(set_path)
    _say("Press Cmd-S in Live to save %r." % name)
    _wait("Cmd-S", lambda: os.path.getmtime(set_path) != mtime or server.get_project_info()["live_is_asking"],
          timeout=WAIT_FOR_USER)
    time.sleep(2)
    assert server.get_project_info()["live_is_asking"] is None, "Live showed a dialog instead of saving"
    text = als.read(set_path)
    header = als.header(text)
    assert _master(text, "Tempo") == ("92", "92") and _master(text, "TimeSignature") == ("302", "302")
    assert 'UserName Value="Keys"' in text and 'UserName Value="Vox"' in text
    assert "Vocal" not in [t[2] for t in als.tracks(text)]           # the base's track is gone
    listed = server.list_projects(location)
    assert any(s["is_open"] for p in listed["projects"] for s in p["sets"])
    report("Phase 3 live: %s from %s (%s); Live asked first: %r; saved in place by %s (%s); project now: %s"
           % (name, made["template"], made["schema"], asked, header["Creator"], header["MinorVersion"],
              sorted(os.listdir(made["project_dir"]))))
