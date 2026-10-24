"""Tests that need no Live: the socket protocol, the commands against a fake LOM, and the timing math."""

import contextlib
import json
import os
import socket
import threading
import time

import pytest

import commands as bridge_commands
import indexer as bridge_indexer
import server as bridge_server
from livebridge import livelog, timing
from livebridge.client import BridgeError, LiveClient


# -- fake Live Object Model --------------------------------------------------

class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Param(Obj):
    def str_for_value(self, v):
        return "%g" % v


class Note(Obj):
    pass


class Clip(Obj):
    def __init__(self, notes, **kw):
        defaults = dict(name="take", is_midi_clip=True, start_marker=0.0, end_marker=16.0,
                        loop_start=0.0, loop_end=16.0, looping=False, length=16.0,
                        start_time=0.0, end_time=16.0, is_recording=False)
        defaults.update(kw)
        super().__init__(**defaults)
        self._notes = notes

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return [n for n in self._notes if from_time <= n.start_time < from_time + time_span]


class ClipSlot(Obj):
    def __init__(self, clip=None):
        super().__init__(clip=clip, is_recording=False, is_playing=False)

    @property
    def has_clip(self):
        return self.clip is not None


class Track(Obj):
    def __init__(self, name, devices=(), **kw):
        kw.setdefault("has_midi_input", True)
        kw.setdefault("can_be_armed", True)
        super().__init__(name=name, devices=list(devices), color=0x1AFF2F, color_index=0, is_foldable=False,
                         arm=False, current_monitoring_state=1, mute=False, solo=False,
                         playing_slot_index=-1, arrangement_clips=[], clip_slots=[ClipSlot() for _ in range(4)],
                         output_meter_left=0.0, output_meter_right=0.0, output_meter_level=0.0,
                         available_input_routing_types=[], available_input_routing_channels=[],
                         input_routing_type=None, input_routing_channel=None,
                         output_routing_type=None, output_routing_channel=None, **kw)

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)


class Song(Obj):
    def __init__(self, tracks):
        super().__init__(tracks=tracks, return_tracks=[], master_track=Track("Master"), tempo=120.0,
                         file_path="", name="", can_undo=False, root_note=0, scale_name="Major", scale_mode=False,
                         signature_numerator=4, signature_denominator=4, is_playing=False,
                         current_song_time=0.0, record_mode=False, session_record=False,
                         is_counting_in=False, arrangement_overdub=False, loop=False,
                         metronome=False, midi_recording_quantization="rec_q_no_q",
                         view=Obj(selected_track=None, highlighted_clip_slot=None))

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False

    def create_midi_track(self, index):
        self.tracks.insert(len(self.tracks) if index < 0 else index, Track("MIDI"))

    def duplicate_track(self, index):
        src = self.tracks[index]
        self.tracks.insert(index + 1, Track(src.name, list(src.devices), has_midi_input=src.has_midi_input))

    def create_audio_track(self, index):
        self.tracks.insert(len(self.tracks) if index < 0 else index, Track("Audio", has_midi_input=False))

    def create_return_track(self):
        self.return_tracks.append(Track("Return", has_midi_input=False, can_be_armed=False))

    def delete_track(self, index):
        del self.tracks[index]

    def delete_return_track(self, index):
        del self.return_tracks[index]


class AppView(Obj):
    """Application.View: starts in Arrangement view, like the 8-Track template set."""
    def __init__(self):
        super().__init__(visible="Arranger", shown=[])

    def is_view_visible(self, name):
        return name == self.visible

    def show_view(self, name):
        self.visible = name
        self.shown.append(name)


class BrowserItem(Obj):
    def __init__(self, name, children=(), loadable=False, uri="", kind=None, source="Core Library",
                 is_device=None):
        super().__init__(name=name, children=list(children), is_loadable=loadable,
                         is_device=loadable if is_device is None else is_device, uri=uri, kind=kind,
                         source=source)

    @property
    def iter_children(self):
        return iter(self.children)


_ptrs = iter(range(1000, 10 ** 6))


def device(name, kind="instrument", **kw):
    """A fake device; `_live_ptr` is what the bridge reports as its id."""
    return Obj(name=name, class_name=name, class_display_name=name, type=1 if kind == "instrument" else 2,
               is_active=True, parameters=[], _live_ptr=next(_ptrs), **kw)


def make_world():
    test_chords = Param(name="Test Chords", value=0.0, min=0.0, max=1.0, is_quantized=True,
                         value_items=["Off", "On"], is_enabled=True)
    gate = Param(name="Gate", value=-50.0, min=-80.0, max=-20.0, is_quantized=False,
                        is_enabled=True)
    plugin = Obj(name="TestSynth", class_name="PluginDevice", class_display_name="VST3",
                 type=1, is_active=True, parameters=[test_chords, gate])
    src = Track("Synth Src", [plugin])
    rec = Track("Synth Rec")
    src_type = Obj(display_name="1-Synth Src", category="track", attached_object=src)
    port_type = Obj(display_name="IAC Driver Bus 1", category="external", attached_object=None)
    rec.available_input_routing_types = [Obj(display_name="All Ins", attached_object=None),
                                         src_type, port_type]
    rec.available_input_routing_channels = [Obj(display_name="Pre FX"), Obj(display_name="Post FX"),
                                            Obj(display_name="TestSynth")]
    song = Song([src, rec])
    loaded = []
    browser = Obj(
        plugins=BrowserItem("Plug-Ins", [
            BrowserItem("AUv2", [BrowserItem("Test Vendor", [
                BrowserItem("TestSynth", loadable=True, uri="query:Plugins#AU:x")])]),
            BrowserItem("VST3", [BrowserItem("Test Vendor", [
                BrowserItem("TestSynth", loadable=True, uri="query:Plugins#VST3:x"),
                BrowserItem("TestSynth Helper", loadable=True)])]),
        ]),
        instruments=BrowserItem("Instruments", [BrowserItem("Drift", loadable=True, kind="instrument")]),
        sounds=BrowserItem("Sounds", [BrowserItem("Pads", [
            BrowserItem("Attack Pad.adv", loadable=True, kind="instrument")])]),
        drums=BrowserItem("Drums"),
        audio_effects=BrowserItem("Audio Effects", [BrowserItem("Reverb", loadable=True, kind="effect")]),
        midi_effects=BrowserItem("MIDI Effects"),
        max_for_live=BrowserItem("Max for Live"),
        clips=BrowserItem("Clips"),
        samples=BrowserItem("Samples", [
            BrowserItem("Acoustic Kick Loop 86bpm.aif", loadable=True, is_device=False, kind="sample",
                        uri="query:Samples#FileId_6889", source="Chop and Swing"),
            BrowserItem("Snare One Shot.wav", loadable=True, is_device=False, kind="sample",
                        uri="query:Samples#FileId_7001")]),
        packs=BrowserItem("Packs", [BrowserItem("Chop and Swing", [BrowserItem("Samples", [
            BrowserItem("Acoustic Kick Loop 86bpm.aif", loadable=True, is_device=False, kind="sample",
                        uri="query:LivePacks#www.ableton.com/249:Samples:Acoustic%20Kick%20Loop%2086bpm.aif",
                        source="Chop and Swing")])])]),   # as Live 12.4.7 has them: no FileId
        user_library=BrowserItem("User Library", [BrowserItem("Presets")]),
        user_folders=[BrowserItem("My Samples", [
            BrowserItem("Field Rec.wav", loadable=True, is_device=False, kind="sample", uri="")])],
        load_item=loaded.append,
        preview_item=lambda item: world.previews.append(item.name),
        stop_preview=lambda: world.previews.append(None),
    )
    app = Obj(browser=browser, view=AppView(), get_major_version=lambda: 12, get_minor_version=lambda: 4,
              get_bugfix_version=lambda: 6, open_dialog_count=0, current_dialog_message="")
    cmds = bridge_commands.Commands(lambda: song, lambda: app)
    world = Obj(song=song, app=app, cmds=cmds, src=src, rec=rec, plugin=plugin, loaded=loaded,
                test_chords=test_chords, port_type=port_type, pending=[], previews=[])

    def apply_loads():
        """Like Live: a load lands a few ticks later on the selected track, and an instrument
        replaces the track's instrument instead of adding one."""
        while loaded:
            world.pending.append([3, loaded.pop(0), song.view.selected_track, song.view.highlighted_clip_slot])
        for job in list(world.pending):
            job[0] -= 1
            if job[0] > 0:
                continue
            world.pending.remove(job)
            _, item, track, slot = job
            kind = item.kind or "instrument"
            if kind == "sample":
                if "bpm" in item.name:
                    song.tempo = 86.0               # as Live did with this loop in an empty set
                if not track.has_midi_input:    # an audio track: a clip in the highlighted slot,
                    if not app.view.is_view_visible("Session"):   # only in Session view
                        continue
                    slot.clip = Obj(name=item.name.rsplit(".", 1)[0], is_audio_clip=True,
                                    file_path="/samples/" + item.name, length=4.0)
                    continue
                item_name, kind = "Simpler", "instrument"
            else:
                item_name = item.name.rsplit(".", 1)[0]
            dev = device(item_name, kind)
            old = [i for i, d in enumerate(track.devices) if d.type == 1]
            if kind == "instrument" and old:
                track.devices[old[0]] = dev
            else:
                track.devices.append(dev)

    world.apply_loads = apply_loads
    return world


@pytest.fixture(autouse=True)
def fresh_index(tmp_path, monkeypatch):
    """The Remote Script's INDEX is module-level (it outlives the control surface), and so are the
    MCP server's index and its cache: give every test its own."""
    from ableton_mcp import index as mcp_index, server as mcp_server
    bridge_indexer.INDEX.reset()
    monkeypatch.setattr(mcp_index, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(mcp_server, "_index", None)
    yield
    bridge_indexer.INDEX.reset()


# -- protocol ----------------------------------------------------------------

@pytest.fixture
def served():
    """A BridgeServer on a free port, polled every 10 ms like Live's tick, over the fake LOM."""
    world = make_world()
    srv = bridge_server.BridgeServer(world.cmds.dispatch, port=0)
    stop = threading.Event()

    def tick():
        while not stop.is_set():
            srv.poll()
            world.cmds.tick()
            world.apply_loads()
            time.sleep(0.01)

    th = threading.Thread(target=tick, daemon=True)
    th.start()
    client = LiveClient(port=srv.address[1], timeout=5.0)
    yield client, world
    client.close()
    stop.set()
    th.join()
    srv.close()


def test_round_trip_and_errors(served):
    client, world = served
    assert client.call("ping")["live_version"] == "12.4.6"
    assert "get_clip_notes" in client.call("list_commands")
    state = client.call("get_state")
    assert [t["name"] for t in state["tracks"]] == ["Synth Src", "Synth Rec"]
    with pytest.raises(BridgeError, match="unknown command"):
        client.call("no_such_command")
    with pytest.raises(BridgeError, match="no track named"):
        client.call("get_track", track="nope")
    assert client.call("set_tempo", bpm=97)["tempo"] == 97  # connection survives errors


def test_many_requests_in_one_write(served):
    client, _ = served
    client.connect()
    batch = b"".join(b'{"id": %d, "cmd": "ping"}\n' % i for i in range(100))
    client._sock.sendall(batch)
    got = [client._readline() for _ in range(100)]
    assert len(got) == 100


def test_bad_json_gets_error_reply(served):
    client, _ = served
    client.connect()
    client._sock.sendall(b"not json\n")
    assert b'"ok": false' in client._readline()


# -- commands against the fake LOM -------------------------------------------

def test_set_parameter_by_name_and_item():
    w = make_world()
    w.cmds.dispatch("set_parameter", {"track": "Synth Src", "device": "testsynth", "parameter": "test chords",
                                      "value": True})
    assert w.test_chords.value == 1.0
    w.cmds.dispatch("set_parameter", {"track": 0, "device": 0, "parameter": "Test Chords", "value": "Off"})
    assert w.test_chords.value == 0.0
    with pytest.raises(bridge_commands.CommandError):
        w.cmds.dispatch("set_parameter", {"track": 0, "device": 0, "parameter": "Gate", "value": 99})


def test_input_routing_from_source_track_then_channel():
    w = make_world()
    w.cmds.dispatch("set_input_routing", {"track": 1, "source_track": 0})
    assert w.rec.input_routing_type.display_name == "1-Synth Src"
    w.cmds.dispatch("set_input_routing", {"track": 1, "channel": "TestSynth"})
    assert w.rec.input_routing_channel.display_name == "TestSynth"
    w.cmds.dispatch("set_input_routing", {"track": 1, "type": "IAC Driver Bus"})
    assert w.rec.input_routing_type is w.port_type
    r = w.cmds.dispatch("get_input_routing", {"track": 1})
    assert r["available_types"][1]["track_index"] == 0


def test_load_plugin_prefers_exact_name_and_vst3():
    w = make_world()
    res = w.cmds.dispatch("load_plugin", {"name": "TestSynth", "track": 1})
    assert res["loaded"]["format"] == "vst3"
    assert res["loaded"]["path"] == ["VST3", "Test Vendor", "TestSynth"]
    assert w.song.view.selected_track is w.rec and len(w.loaded) == 1
    res = w.cmds.dispatch("load_plugin", {"name": "TestSynth", "track": 1, "format": "au"})
    assert res["loaded"]["format"] == "au"
    hits = w.cmds.dispatch("browser_search", {"query": "testsynth"})
    assert len(hits) == 3


def test_create_track_and_arm():
    w = make_world()
    t = w.cmds.dispatch("create_midi_track", {"name": "New"})
    assert t["index"] == 2 and w.song.tracks[2].name == "New"
    w.cmds.dispatch("arm_track", {"track": "New"})
    assert w.song.tracks[2].arm is True
    a = w.cmds.dispatch("create_audio_track", {"name": "Guitar", "index": 0})
    assert a["index"] == 0 and a["is_midi"] is False and w.song.tracks[0].name == "Guitar"


def test_recording_and_clip_notes():
    w = make_world()
    w.cmds.dispatch("start_recording", {"mode": "arrangement", "from_beat": 0})
    assert w.song.record_mode and w.song.is_playing
    w.cmds.dispatch("stop_recording", {"mode": "arrangement"})
    assert not w.song.record_mode and not w.song.is_playing
    old = Clip([Note(pitch=60, start_time=0.0, duration=0.5, velocity=100, mute=False)], start_time=0.0)
    new = Clip([Note(pitch=64, start_time=1.002, duration=0.5, velocity=100, mute=False),
                Note(pitch=60, start_time=1.002, duration=0.5, velocity=100, mute=False)],
               start_time=32.0, start_marker=0.0)
    w.rec.arrangement_clips = [old, new]
    res = w.cmds.dispatch("get_clip_notes", {"track": 1})
    assert res["start_time"] == 32.0
    assert [n["pitch"] for n in res["notes"]] == [60, 64]
    assert res["notes"][0]["song_time"] == pytest.approx(33.002)
    assert w.cmds.dispatch("clear_arrangement_clips", {"track": 1})["deleted"] == 2


def test_duplicate_track():
    w = make_world()
    t = w.cmds.dispatch("duplicate_track", {"track": "Synth Src"})
    assert t["index"] == 1 and len(w.song.tracks) == 3
    assert w.song.tracks[1].devices[0] is w.plugin
    with pytest.raises(bridge_commands.CommandError):
        w.cmds.dispatch("duplicate_track", {"track": "master"})


# -- timing math -------------------------------------------------------------

def test_note_offsets_early_and_late():
    notes = [{"pitch": 60, "start_time": 0.0}, {"pitch": 60, "start_time": 1.01},
             {"pitch": 60, "start_time": 1.99}]
    rows = timing.note_offsets(notes, tempo=120.0)
    assert [r["beat"] for r in rows] == [0.0, 1.0, 2.0]
    assert rows[1]["offset_ms"] == pytest.approx(5.0)    # 0.01 beat at 120 BPM
    assert rows[2]["offset_ms"] == pytest.approx(-5.0)
    s = timing.summarize(rows)
    assert s["beats_hit"] == 3 and s["spread_ms"] == pytest.approx(10.0)
    assert timing.missing_beats(rows, 0.0, 3.0) == [3.0]
    assert "offset_ms" in timing.format_table("t", rows, s, 120.0)


LOG = """\
2026-10-19T17:12:07.655523: info: Audio In Out: Sample Rate: 44100
2026-10-19T17:12:07.655547: info: Audio In Out: Output Buffer Size: 512 Samples
2026-10-19T17:12:07.655632: info: Audio In Out: Output Latency: 14.8 ms
2026-10-19T17:12:10.000000: info: Something else: Sample Rate: 1
2026-10-19T17:12:24.562881: info: Audio In Out: Sample Rate: 44100
2026-10-19T17:12:24.562889: info: Audio In Out: Input Buffer Size: 128 Samples
2026-10-19T17:12:24.562903: info: Audio In Out: Output Buffer Size: 128 Samples
2026-10-19T17:12:24.563061: info: Audio In Out: Output Latency: 6.10 ms
2026-10-19T17:12:24.563187: info: Audio In Out: Output Latency (with buffering): 6.10 ms
2026-10-19T17:12:24.563337: info: Audio In Out: Overall Latency: 9.00 ms
"""


def test_audio_settings_take_the_last_block():
    a = livelog.parse_audio_settings(LOG)
    assert a["sample_rate"] == 44100 and a["output_buffer"] == 128 and a["input_buffer"] == 128
    assert a["output_latency_ms"] == pytest.approx(6.10)
    assert a["overall_latency_ms"] == pytest.approx(9.0)
    assert a["logged_at"] == "2026-10-19T17:12:24.562881"
    assert livelog.parse_audio_settings("no audio here") == {}


def test_offsets_in_samples_and_buffers():
    # +4.444 ms at 44.1 kHz with a 128-sample buffer, as the port measured.
    notes = [{"pitch": 60, "start_time": b + 0.008889} for b in range(4)]
    rows = timing.note_offsets(notes, tempo=120.0, sample_rate=44100)
    assert rows[0]["offset_samples"] == pytest.approx(196.0, abs=0.1)
    s = timing.summarize(rows, buffer_size=128)
    assert s["mean_whole_buffers"] == 1
    assert s["mean_remainder_samples"] == pytest.approx(68.0, abs=0.1)
    text = timing.format_table("t", rows, s, 120.0, {"sample_rate": 44100, "output_buffer": 128})
    assert "44100 Hz, buffer 128" in text and "1 buffer(s) + 68.0 samples" in text


# -- editing a saved set (livebridge.als) ----------------------------------------

SET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Ableton><LiveSet><Tracks>
<MidiTrack Id="18"><Name><EffectiveName Value="Synth" /></Name></MidiTrack>
<AudioTrack Id="19"><Name><EffectiveName Value="Vocal Comp" /></Name><DeviceChain>
<PluginDevice Id="0"><PluginDesc><Vst3PluginInfo Id="0"><Name Value="TestSynth" /></Vst3PluginInfo></PluginDesc>
<SideChain>
  <OnOff>
    <LomId Value="0" />
    <Manual Value="false" />
  </OnOff>
  <RoutedInput><Routable>
    <Target Value="AudioIn/None" />
    <UpperDisplayString Value="No Output" />
    <LowerDisplayString Value="" />
  </Routable></RoutedInput>
</SideChain>
</PluginDevice></DeviceChain></AudioTrack>
</Tracks></LiveSet></Ableton>
"""


def test_als_set_sidechain(tmp_path):
    from livebridge import als
    assert [t[2] for t in als.tracks(SET_XML)] == ["Synth", "Vocal Comp"]
    out = als.set_sidechain(SET_XML, "Vocal Comp", "Synth", "TestSynth")
    assert 'Target Value="AudioIn/Track.18/PostFxOut"' in out
    assert 'UpperDisplayString Value="Synth"' in out and 'LowerDisplayString Value="Post FX"' in out
    assert '<Manual Value="true" />' in out
    with pytest.raises(ValueError, match="no track named"):
        als.set_sidechain(SET_XML, "Nope", "Synth", "TestSynth")
    with pytest.raises(ValueError, match="no plug-in"):
        als.set_sidechain(SET_XML, "Vocal Comp", "Synth", device="Other")

    src = tmp_path / "t.als"
    als.write(str(src), SET_XML)
    assert als.main([str(src), "--track", "Vocal Comp", "--source", "Synth", "--device", "TestSynth"]) == 0
    assert "PostFxOut" in als.read(str(tmp_path / "t sidechain.als"))
    with pytest.raises(SystemExit):
        als.main([str(src), "--track", "Vocal Comp", "--source", "Synth", "--device", "TestSynth", "-o", str(src)])


# -- ClaudeLiveBridge naming and the MCP server ----------------------------------

def test_client_and_remote_script_agree_on_port_and_name():
    import os

    from livebridge import client
    assert client.DEFAULT_PORT == bridge_server.DEFAULT_PORT == 9873
    # The Remote Script's package __init__ imports Live's _Framework, so read NAME from its source.
    path = os.path.join(os.path.dirname(bridge_server.__file__), "__init__.py")
    with open(path) as f:
        assert 'NAME = "%s"' % client.BRIDGE_NAME in f.read()
    assert os.path.basename(os.path.dirname(path)) == client.BRIDGE_NAME  # Live lists the folder name


def test_live_status_up(served, monkeypatch):
    from ableton_mcp import server
    client, _ = served
    monkeypatch.setattr(server, "_client", client)
    status = server.live_status()
    assert status["ok"] is True and status["live_version"] == "12.4.6"
    assert status["track_count"] == 2 and status["tempo"] == 120.0


def test_live_status_down_says_what_to_fix(tmp_path, monkeypatch):
    import socket

    from ableton_mcp import server
    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(server, "_client", LiveClient(port=port, timeout=1.0))
    monkeypatch.setenv("LIVE_USER_LIBRARY", str(tmp_path))
    status = server.live_status()
    assert status["ok"] is False and status["remote_script_installed"] is False
    assert "install_remote_script" in status["fix"]
    server.install_remote_script()
    status = server.live_status()
    assert status["remote_script_installed"] is True and "Control Surface" in status["fix"]


def test_install_remote_script_copies_links_and_spots_stale_copies(tmp_path, monkeypatch):
    from ableton_mcp import remote_script
    monkeypatch.setenv("LIVE_USER_LIBRARY", str(tmp_path))
    assert remote_script.status()["installed"] is False
    out = remote_script.install()
    dest = tmp_path / "Remote Scripts" / "ClaudeLiveBridge"
    assert out["kind"] == "copy" and out["up_to_date"] is True and out["next"].startswith("Restart Live")
    assert sorted(p.name for p in dest.iterdir()) == ["__init__.py", "commands.py", "indexer.py", "server.py"]
    (dest / "commands.py").write_text("# an older version\n")
    assert remote_script.status()["up_to_date"] is False
    out = remote_script.install(link=True)
    assert out["kind"] == "link" and out["up_to_date"] is True and dest.is_symlink()
    assert remote_script.install()["kind"] == "copy" and not dest.is_symlink()   # back to a copy
    (tmp_path / "Remote Scripts" / "ClaudeLiveBridge").rename(tmp_path / "moved")
    (dest).mkdir()
    (dest / "notes.txt").write_text("someone else's folder")
    with pytest.raises(RuntimeError, match="leaving it alone"):
        remote_script.install()


def test_cli_status_and_install(tmp_path, monkeypatch, capsys):
    import json as _json
    from ableton_mcp.__main__ import main
    monkeypatch.setenv("LIVE_USER_LIBRARY", str(tmp_path))
    assert main(["install-remote-script"]) == 0
    assert _json.loads(capsys.readouterr().out)["kind"] == "copy"


# -- Phase 1: return tracks, colour, loading, add_track ----------------------------

def test_return_tracks():
    w = make_world()
    t = w.cmds.dispatch("create_return_track", {"name": "Verb"})
    assert t["index"] == "return:0" and t["kind"] == "return" and w.song.return_tracks[0].name == "Verb"
    w.cmds.dispatch("create_return_track", {})
    assert w.cmds.dispatch("get_track", {"track": "Verb"})["index"] == "return:0"   # by name
    assert w.cmds.dispatch("get_track", {"track": "return:1"})["name"] == "Return"
    state = w.cmds.dispatch("get_state", {})
    assert [r["name"] for r in state["return_tracks"]] == ["Verb", "Return"]
    assert [t["kind"] for t in state["tracks"]] == ["midi", "midi"]
    with pytest.raises(bridge_commands.CommandError, match="return"):
        w.cmds.dispatch("duplicate_track", {"track": "return:0"})
    with pytest.raises(bridge_commands.CommandError, match="out of range"):
        w.cmds.dispatch("get_track", {"track": "return:5"})
    with pytest.raises(bridge_commands.CommandError, match="returns"):
        w.cmds.dispatch("get_track", {"track": "nope"})   # the error lists both kinds
    res = w.cmds.dispatch("delete_track", {"track": "Verb"})
    assert res == {"deleted": "return:0", "name": "Verb", "track_count": 2, "return_track_count": 1}
    with pytest.raises(bridge_commands.CommandError, match="master"):
        w.cmds.dispatch("delete_track", {"track": "master"})


def test_return_tracks_match_without_their_letter():
    w = make_world()
    for name in ("C-Verb Long", "D-Verb"):   # as Live reads them back
        w.cmds.dispatch("create_return_track", {"name": name})
    assert w.cmds.dispatch("get_track", {"track": "Verb"})["name"] == "D-Verb"   # not ambiguous
    assert w.cmds.dispatch("get_track", {"track": "D-Verb"})["index"] == "return:1"
    assert w.cmds.dispatch("get_track", {"track": "long"})["name"] == "C-Verb Long"


def test_regular_tracks_win_name_lookups_over_returns():
    w = make_world()
    w.cmds.dispatch("create_return_track", {"name": "Synth Src"})
    assert w.cmds.dispatch("get_track", {"track": "Synth Src"})["index"] == 0


def test_track_colour():
    w = make_world()
    t = w.cmds.dispatch("set_track", {"track": 0, "color": 12})
    assert w.src.color_index == 12 and t["color_index"] == 12
    t = w.cmds.dispatch("set_track", {"track": 0, "color": "#FF8000"})
    assert w.src.color == 0xFF8000 and t["color"] == "#ff8000"
    for bad in ("red", 70, -1, True):
        with pytest.raises(bridge_commands.CommandError, match="color"):
            w.cmds.dispatch("set_track", {"track": 0, "color": bad})


def test_load_device_reports_devices_before_and_ids():
    w = make_world()
    w.rec.devices = [device("Simpler")]
    res = w.cmds.dispatch("load_device", {"name": "Drift", "track": 1, "category": "instruments"})
    assert [d["name"] for d in res["devices_before"]] == ["Simpler"]
    assert res["devices_before"][0]["id"] == w.rec.devices[0]._live_ptr
    assert w.cmds.dispatch("list_devices", {"track": 0})[0]["id"] is None   # no _live_ptr: no id


def test_load_device_and_wait_sees_a_replaced_instrument(served):
    """The Phase 0 bug: an instrument replaces the track's instrument, so the count stays at 1."""
    client, w = served
    w.rec.devices = [device("Simpler")]
    info = client.load_device_and_wait("Drift", 1, "instruments", timeout=5)
    assert info["name"] == "Drift" and info["replaced"]["name"] == "Simpler"
    assert [d.name for d in w.rec.devices] == ["Drift"]
    # an effect is added after it, and replaces nothing
    info = client.load_device_and_wait("Reverb", 1, "audio_effects", timeout=5)
    assert info["name"] == "Reverb" and info["index"] == 1 and info["replaced"] is None
    # an identical instrument replacing another is still seen, by id
    info = client.load_device_and_wait("Drift", 1, "instruments", timeout=5)
    assert info["name"] == "Drift" and info["replaced"]["name"] == "Drift"


def test_add_track_with_instrument_colour_and_rollback(served, monkeypatch):
    from ableton_mcp import server
    client, w = served
    monkeypatch.setattr(server, "_client", client)
    t = server.add_track(kind="midi", name="Lead", color=5, instrument="Attack Pad")
    assert t["name"] == "Lead" and t["kind"] == "midi" and t["color_index"] == 5
    assert t["instrument"]["name"] == "Attack Pad"   # found in sounds, after missing in instruments
    assert [d["name"] for d in t["devices"]] == ["Attack Pad"]

    count = len(w.song.tracks)
    with pytest.raises(server.ToolError, match="no instrument named 'Nope'.*removed again"):
        server.add_track(name="Broken", instrument="Nope")
    assert len(w.song.tracks) == count                       # rolled back
    with pytest.raises(server.ToolError, match="color.*"):
        server.add_track(name="Badly coloured", color="red")
    assert len(w.song.tracks) == count
    with pytest.raises(server.ToolError, match="MIDI tracks"):
        server.add_track(kind="audio", instrument="Drift")
    with pytest.raises(server.ToolError, match="kind"):
        server.add_track(kind="group")

    r = server.add_track(kind="return", name="Verb", color="#0000ff")
    assert r["index"] == "return:0" and r["color"] == "#0000ff"
    with pytest.raises(server.ToolError, match="after the existing"):
        server.add_track(kind="return", index=0)
    a = server.add_track(kind="audio", name="Guitar", index=0)
    assert a["index"] == 0 and a["kind"] == "audio"


def test_list_and_update_tracks(served, monkeypatch):
    from ableton_mcp import server
    client, w = served
    monkeypatch.setattr(server, "_client", client)
    server.add_track(kind="return", name="Verb")
    server.update_track("Synth Src", name="Bass", color=20, mute=True)
    listed = server.list_tracks()
    assert listed["tracks"][0] == {"index": 0, "name": "Bass", "kind": "midi", "color": "#1aff2f",
                                   "color_index": 20, "mute": True, "solo": False, "arm": False,
                                   "devices": ["TestSynth"]}
    assert [r["name"] for r in listed["return_tracks"]] == ["Verb"]
    assert listed["master"]["kind"] == "master"
    deleted = server.delete_track("return:0")
    assert deleted["name"] == "Verb" and deleted["return_track_count"] == 0


# -- Phase 2: the browser index, search and load ------------------------------------

def _build(w, budget_ms=0):
    w.cmds.dispatch("index_start", {})
    bridge_indexer.INDEX.budget_ms = budget_ms   # 0: one node per tick
    ticks = 0
    while bridge_indexer.INDEX.state == "running":
        w.cmds.tick()
        ticks += 1
    return ticks


def test_indexer_walks_a_little_per_tick():
    w = make_world()
    status = w.cmds.dispatch("index_start", {"budget_ms": 15})
    assert status["state"] == "running" and status["budget_ms"] == 15
    ticks = _build(w)
    status = w.cmds.dispatch("index_status", {})
    assert status["state"] == "done" and ticks > 10          # one node per tick: many ticks
    page = w.cmds.dispatch("index_page", {"offset": 0, "limit": 100})
    by_name = {e["name"]: e for e in page["entries"]}
    assert by_name["Drift"]["root"] == "instruments" and by_name["Drift"]["is_device"] is True
    assert by_name["Attack Pad.adv"]["path"] == ["Pads", "Attack Pad.adv"]
    assert by_name["Field Rec.wav"]["root"] == "user_folders"
    assert by_name["Field Rec.wav"]["id"] == "user_folders:My Samples/Field Rec.wav"   # no URI
    assert [e["root"] for e in page["entries"]].count("packs") == 1
    assert not any(e["name"] in ("Pads", "Presets") for e in page["entries"])   # folders aren't entries
    assert w.cmds.dispatch("index_page", {"offset": 2, "limit": 1})["entries"] == page["entries"][2:3]
    with pytest.raises(bridge_commands.CommandError, match="unknown browser roots"):
        w.cmds.dispatch("index_start", {"roots": ["nope"]})


def test_index_outlives_the_control_surface():
    """Live re-creates the control surface (and Commands) on every set load; INDEX is module-level."""
    w = make_world()
    _build(w)
    fresh = bridge_commands.Commands(lambda: w.song, lambda: w.app)
    assert fresh.cmd_index_status()["state"] == "done"
    fresh.cmd_load_item("query:Samples#FileId_7001", 1)
    assert w.loaded[-1].name == "Snare One Shot.wav"


def test_load_item_finds_an_item_by_path_without_the_index():
    w = make_world()   # nothing indexed: an index read from the disk cache after Live restarted
    res = w.cmds.dispatch("load_item", {"id": "query:Samples#FileId_6889", "track": 1, "root": "samples",
                                        "path": ["Acoustic Kick Loop 86bpm.aif"], "scene": 2})
    assert res["name"] == "Acoustic Kick Loop 86bpm.aif" and res["tempo_before"] == 120.0
    assert w.song.view.highlighted_clip_slot is w.rec.clip_slots[2]
    assert res["showed_session_view"] is True and w.app.view.shown == ["Session"]
    assert w.cmds.dispatch("view_state", {})["Session"] is True
    w.cmds.dispatch("load_item", {"id": "user_folders:My Samples/Field Rec.wav", "track": 1,
                                  "root": "user_folders", "path": ["My Samples", "Field Rec.wav"]})
    assert w.loaded[-1].name == "Field Rec.wav"
    with pytest.raises(bridge_commands.CommandError, match="no longer"):
        w.cmds.dispatch("load_item", {"id": "query:Samples#FileId_1", "track": 1, "root": "samples",
                                      "path": ["Acoustic Kick Loop 86bpm.aif"]})   # another item there now
    with pytest.raises(bridge_commands.CommandError, match="give its root and path"):
        w.cmds.dispatch("load_item", {"id": "query:Samples#FileId_1", "track": 1})
    w.cmds.dispatch("preview_item", {"id": "query:Samples#FileId_7001", "root": "samples",
                                     "path": ["Snare One Shot.wav"]})
    w.cmds.dispatch("stop_preview", {})
    assert w.previews == ["Snare One Shot.wav", None]


def _index_of(w):
    from ableton_mcp.index import Index
    _build(w)
    return Index(w.cmds.dispatch("index_page", {"limit": 10000})["entries"], {"live_version": "test"})


def test_index_merges_packs_and_works_out_kinds():
    w = make_world()
    idx = _index_of(w)
    kick = idx.get("query:Samples#FileId_6889")
    assert kick["root"] == "samples" and kick["kind"] == "sample" and kick["pack"] == "Chop and Swing"
    packs_id = "query:LivePacks#www.ableton.com/249:Samples:Acoustic%20Kick%20Loop%2086bpm.aif"
    assert idx.get(packs_id) is kick                                     # the Packs copy is an alias
    assert idx.raw_count == len(idx.entries) + 1
    assert kick["hints"] == {"bpm": 86, "loop": True}
    assert idx.get("query:Samples#FileId_7001")["hints"] == {"loop": False}
    kinds = {e["stem"]: e["kind"] for e in idx.entries}
    assert kinds["Drift"] == "instrument" and kinds["Attack Pad"] == "sound" and kinds["Reverb"] == "audio_effect"
    assert kinds["TestSynth"] == "plugin" and kinds["Field Rec"] == "sample"


def test_packs_copies_merge_by_pack_folder_and_name():
    from ableton_mcp.index import Index
    sample = lambda id, name, root, path, source: {"id": id, "name": name, "root": root, "path": path,
                                                   "is_device": False, "source": source}
    raw = [
        sample("query:Drums#Drum%20Hits:Kick:FileId_1", "Kick LD.aif", "drums", ["Drum Hits", "Kick", "Kick LD.aif"], "Core Library"),
        sample("query:Drums#Drum%20Hits:Kick:FileId_2", "Kick LD.aif", "drums", ["Drum Hits", "Kick", "Kick LD.aif"], "Drum Essentials"),
        sample("query:Samples#FileId_3", "Snap.wav", "samples", ["Snap.wav"], "Drum Essentials"),
        sample("query:Samples#FileId_4", "Snap.wav", "samples", ["Snap.wav"], "Drum Essentials"),
        sample("query:LivePacks#core:Kick%20LD.aif", "Kick LD.aif", "packs", ["Core Library", "Samples", "Kick", "Kick LD.aif"], "Core Library"),
        sample("query:LivePacks#de:Kick%20LD.aif", "Kick LD.aif", "packs", ["Drum Essentials", "Samples", "Kick", "Kick LD.aif"], "Drum Essentials"),
        sample("query:LivePacks#de:Snap.wav", "Snap.wav", "packs", ["Drum Essentials", "Samples", "Snap.wav"], "Drum Essentials"),
        sample("query:LivePacks#de:Only.wav", "Only.wav", "packs", ["Drum Essentials", "Samples", "Only.wav"], "Drum Essentials"),
    ]
    idx = Index(raw)
    assert idx.get("query:LivePacks#core:Kick%20LD.aif")["id"] == "query:Drums#Drum%20Hits:Kick:FileId_1"
    assert idx.get("query:LivePacks#de:Kick%20LD.aif")["id"] == "query:Drums#Drum%20Hits:Kick:FileId_2"
    kicks = idx.search("kick ld")
    assert [(r["name"], r["pack"]) for r in kicks] == [("Kick LD", "Core Library"), ("Kick LD", "Drum Essentials")]
    assert idx.get("query:LivePacks#de:Snap.wav")["root"] == "packs"     # two files it could be: kept apart
    assert idx.get("query:LivePacks#de:Only.wav")["pack"] == "Drum Essentials"   # only in Packs
    assert len(idx.entries) == 6 and "Samples" in idx.get("query:Drums#Drum%20Hits:Kick:FileId_2")["context"]


def test_whole_words_rank_above_word_starts():
    from ableton_mcp.index import Index
    raw = [{"id": n, "name": n + ".adg", "root": "sounds", "path": ["Pads", n + ".adg"], "is_device": False,
            "source": "Core Library"} for n in ("Paddles Out", "Sea Pad", "PPP Pad")]
    assert [r["name"] for r in Index(raw).search("pad")] == ["PPP Pad", "Sea Pad", "Paddles Out"]


def test_an_item_listed_under_two_roots_is_both_kinds():
    """Live lists a preset under Instruments > Drift and under Sounds > a category: keep both."""
    from ableton_mcp.index import Index
    raw = [{"id": "query:Instruments#FileId_5", "name": "Glass Pad.adv", "root": "instruments",
            "path": ["Drift", "Glass Pad.adv"], "is_device": False, "source": "Core Library"},
           {"id": "query:Sounds#FileId_5", "name": "Glass Pad.adv", "root": "sounds",
            "path": ["Ambient & Evolving", "Glass Pad.adv"], "is_device": False, "source": "Core Library"}]
    idx = Index(raw)
    assert len(idx.entries) == 1 and idx.entries[0]["kinds"] == ["instrument", "sound"]
    assert [r["name"] for r in idx.search("pad", kind="sound")] == ["Glass Pad"]
    hit = idx.search("ambient")[0]                      # a folder from the Sounds listing
    assert hit["name"] == "Glass Pad" and hit["kind"] == "instrument" and hit["also"] == ["sound"]
    assert idx.stats()["kinds"] == {"instrument": 1, "sound": 1}


def test_search_ranking_filters_and_typos():
    from ableton_mcp.index import Index
    raw = [
        {"id": "d", "name": "Drift", "root": "instruments", "path": ["Drift"], "is_device": True, "source": "Core Library"},
        {"id": "dp", "name": "Drift Pad.adv", "root": "instruments", "path": ["Drift", "Drift Pad.adv"], "is_device": False, "source": "Core Library"},
        {"id": "wp", "name": "Warm Analog Pad.adg", "root": "sounds", "path": ["Ambient & Evolving", "Warm Analog Pad.adg"], "is_device": False, "source": "Core Library"},
        {"id": "ev", "name": "Glass Swell.adg", "root": "sounds", "path": ["Ambient & Evolving", "Glass Swell.adg"], "is_device": False, "source": "Core Library"},
        {"id": "query:Samples#FileId_1", "name": "Kick 808 Long.wav", "root": "samples", "path": ["Kick 808 Long.wav"], "is_device": False, "source": "Drum Essentials"},
        {"id": "query:Samples#FileId_2", "name": "Big Kick.aif", "root": "drums", "path": ["Kicks", "Big Kick.aif"], "is_device": False, "source": "Core Library"},
        {"id": "rv", "name": "Reverb", "root": "audio_effects", "path": ["Reverb"], "is_device": True, "source": "Core Library"},
        {"id": "rvp", "name": "Big Room Reverb.adv", "root": "audio_effects", "path": ["Reverb", "Big Room Reverb.adv"], "is_device": False, "source": "Core Library"},
    ]
    idx = Index(raw)
    names = lambda res: [r["name"] for r in res]
    assert names(idx.search("drift"))[:2] == ["Drift", "Drift Pad"]           # the device first
    assert names(idx.search("warm pad", kind="instrument")) == ["Warm Analog Pad"]
    assert "Glass Swell" in names(idx.search("evolving"))                      # its folder matches
    assert names(idx.search("kick", kind="sample")) == ["Big Kick", "Kick 808 Long"]   # tie: shorter first
    assert names(idx.search("kick", kind="instrument")) == []
    assert names(idx.search("kick", pack="drum ess")) == ["Kick 808 Long"]
    assert names(idx.search("verb"))[0] == "Reverb"                            # synonym
    assert names(idx.search("reverb", kind="audio_effect"))[:2] == ["Reverb", "Big Room Reverb"]
    assert names(idx.search("drfit")) == ["Drift"]                             # typo
    assert idx.search("kick", kind="sample", limit=1)[0]["id"] == "query:Samples#FileId_2"
    with pytest.raises(ValueError, match="kind"):
        idx.search("x", kind="nope")


def test_index_cache_round_trip_and_fingerprint(tmp_path):
    w = make_world()
    idx = _index_of(w)
    path = str(tmp_path / "idx.json")
    idx.save(path)
    from ableton_mcp.index import Index
    again = Index.load(path, {"live_version": "test"})
    assert [e["id"] for e in again.entries] == [e["id"] for e in idx.entries]
    assert again.get("query:LivePacks#www.ableton.com/249:Samples:Acoustic%20Kick%20Loop%2086bpm.aif")["root"] == "samples"
    assert Index.load(path, {"live_version": "other"}) is None
    assert Index.load(str(tmp_path / "missing.json"), {"live_version": "test"}) is None


def test_mcp_search_and_load_item(served, monkeypatch):
    from ableton_mcp import server
    client, w = served
    monkeypatch.setattr(server, "_client", client)
    res = server.search("kick", kind="sample")
    assert res["results"][0]["name"] == "Acoustic Kick Loop 86bpm" and res["index"]["from_cache"] is False
    kick = res["results"][0]["id"]

    # a sample on an audio track: a clip in the first empty slot, and the tempo put back
    audio = server.add_track(kind="audio", name="Loops")
    w.song.tracks[audio["index"]].clip_slots[0].clip = Obj(name="old", is_audio_clip=True, file_path=None, length=1.0)
    out = server.load_item(kick, "Loops")
    assert out["scene"] == 1 and out["clip"]["name"] == "Acoustic Kick Loop 86bpm"
    assert out["showed_session_view"] is True     # it was in Arrangement view, where the load does nothing
    assert out["tempo_changed"] == {"from": 120.0, "to": 86.0, "restored": True} and w.song.tempo == 120.0
    with pytest.raises(server.ToolError, match="already has a clip"):
        server.load_item(kick, "Loops", scene=0)

    # the same sample on a MIDI track: a Simpler; keep_tempo=False leaves Live's tempo
    out = server.load_item(kick, "Synth Rec", keep_tempo=False)
    assert out["device"]["name"] == "Simpler" and w.song.tempo == 86.0

    # an instrument needs a MIDI track; add_track takes a search result id
    drift = server.search("drift", kind="instrument")["results"][0]["id"]
    with pytest.raises(server.ToolError, match="needs a MIDI track"):
        server.load_item(drift, "Loops")
    lead = server.add_track(name="Lead", instrument=drift)
    assert lead["instrument"]["name"] == "Drift"
    with pytest.raises(server.ToolError, match="no browser item"):
        server.load_item("query:Nope", 0)

    server.preview_item(kick)
    server.stop_preview()
    assert w.previews == ["Acoustic Kick Loop 86bpm.aif", None]

    # a second MCP process reads the disk cache instead of indexing again
    monkeypatch.setattr(server, "_index", None)
    bridge_indexer.INDEX.reset()
    assert server.search("drift")["index"]["from_cache"] is True
    assert bridge_indexer.INDEX.state == "idle"
    assert server.load_item(kick, "Loops")["scene"] == 2       # found again by its path
    assert server.refresh_index()["entries"] == len(server._index.entries)


# -- Phase 3: projects ----------------------------------------------------------------

def _set_xml(schema="12.0_12402", tracks=("Vocal",), envelope=True):
    """A minimal set with the parts create_project touches, laid out as Live 12.4 writes them."""
    env = """<AutomationEnvelopes><Envelopes>
<AutomationEnvelope Id="0"><EnvelopeTarget>
<PointeeId Value="10" />
</EnvelopeTarget><Automation><Events>
<EnumEvent Id="0" Time="-63072000" Value="201" />
</Events></Automation></AutomationEnvelope>
<AutomationEnvelope Id="1"><EnvelopeTarget>
<PointeeId Value="8" />
</EnvelopeTarget><Automation><Events>
<FloatEvent Id="0" Time="-63072000" Value="120" />
</Events></Automation></AutomationEnvelope>
</Envelopes></AutomationEnvelopes>""" if envelope else ""
    tracks_xml = "".join('<AudioTrack Id="%d"><Name><EffectiveName Value="%s" /><UserName Value="" /></Name></AudioTrack>'
                         % (i, n) for i, n in enumerate(tracks))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Ableton MajorVersion="5" MinorVersion="%s" '
            'SchemaChangeCount="1" Creator="Ableton Live 12.4d1" Revision="x">\n<LiveSet><Tracks>%s</Tracks>'
            '<MainTrack>%s<DeviceChain><Mixer>\n<Tempo>\n<LomId Value="0" />\n<Manual Value="120" />\n'
            '<AutomationTarget Id="8">\n<LockEnvelope Value="0" />\n</AutomationTarget>\n</Tempo>\n'
            '<TimeSignature>\n<LomId Value="0" />\n<Manual Value="201" />\n<AutomationTarget Id="10">\n'
            '<LockEnvelope Value="0" />\n</AutomationTarget>\n</TimeSignature>\n</Mixer></DeviceChain>'
            '</MainTrack></LiveSet></Ableton>\n') % (schema, tracks_xml, env)


def _master_values(text, tag):
    import re
    block = re.search(r"<%s>(.*?)</%s>" % (tag, tag), text, re.S).group(1)
    target = re.search(r'<AutomationTarget Id="(\d+)">', block).group(1)
    ev = re.search(r'<PointeeId Value="%s" />\s*</EnvelopeTarget>\s*<Automation>\s*<Events>\s*<\w+ Id="\d+" '
                   r'Time="-63072000" Value="([^"]*)"' % target, text)
    return re.search(r'<Manual Value="([^"]*)"', block).group(1), ev and ev.group(1)


def test_als_tempo_signature_and_header():
    from livebridge import als
    assert [als.time_signature_value(*sig) for sig in ((4, 4), (3, 4), (6, 8), (7, 8), (2, 2), (5, 16))] == \
        [201, 200, 302, 303, 100, 400]
    for bad in ((0, 4), (4, 3), (100, 4)):
        with pytest.raises(ValueError):
            als.time_signature_value(*bad)
    x = _set_xml()
    y = als.set_time_signature(als.set_tempo(x, 92.5), 3, 4)
    assert _master_values(y, "Tempo") == ("92.5", "92.5") and _master_values(y, "TimeSignature") == ("200", "200")
    assert _master_values(als.set_tempo(x, 90), "Tempo") == ("90", "90")
    z = als.set_tempo(_set_xml(envelope=False), 140)        # no envelope: the Manual value is enough
    assert _master_values(z, "Tempo") == ("140", None)
    with pytest.raises(ValueError, match="tempo"):
        als.set_tempo(x, 5)
    h = als.header(x)
    assert h["MinorVersion"] == "12.0_12402" and h["Creator"] == "Ableton Live 12.4d1"
    assert als.schema_key("12.0_12117") < als.schema_key("12.0_12402")
    with pytest.raises(ValueError, match="not a Live set"):
        als.header("<xml/>")


@pytest.fixture
def live_files(tmp_path, monkeypatch):
    """A fake Live app with Core Library templates, an empty User Library and a projects root."""
    from livebridge import als
    from ableton_mcp import projects
    app = tmp_path / "Ableton Live 12 Test.app"
    core = app / "Contents" / "App-Resources" / "Core Library" / "Templates"
    core.mkdir(parents=True)
    als.write(str(core / "Vocal Recording.als"), _set_xml(tracks=("Vocal",)))
    als.write(str(core / "8-Track Template.als"), _set_xml(tracks=["%d-Audio" % i for i in range(1, 9)]))
    user = tmp_path / "User Library"
    (user / "Templates").mkdir(parents=True)
    monkeypatch.setenv("LIVE_APP", str(app))
    monkeypatch.setattr(projects, "USER_LIBRARY", str(user))
    monkeypatch.setattr(projects, "PROJECTS_ROOT", str(tmp_path / "Projects"))
    return Obj(app=app, core=core, user=user, root=tmp_path / "Projects")


def test_create_project_on_disk(live_files):
    import os
    from livebridge import als
    from ableton_mcp import projects
    made = projects.create("Night Drive", tempo=92, signature=[6, 8])
    folder = live_files.root / "Night Drive Project"
    assert made["set_path"] == str(folder / "Night Drive.als") and made["template"] == "Vocal Recording"
    assert made["template_is_base"] is True and "warning" not in made
    assert (folder / "Ableton Project Info").is_dir()
    text = als.read(made["set_path"])
    assert _master_values(text, "Tempo") == ("92", "92") and _master_values(text, "TimeSignature") == ("302", "302")
    with pytest.raises(projects.ProjectError, match="already exists"):
        projects.create("Night Drive")
    assert projects.create("Night Drive Project 2 Project")["name"] == "Night Drive Project 2"   # suffix dropped
    for bad in ("", "  ", "a/b", "a:b", ".hidden"):
        with pytest.raises(projects.ProjectError):
            projects.create(bad)
    with pytest.raises(projects.ProjectError, match="signature"):
        projects.create("Bad Sig", signature=[4])
    with pytest.raises(ValueError, match="denominator"):
        projects.create("Bad Sig 2", signature=[4, 3])
    assert not os.path.exists(live_files.root / "Bad Sig 2 Project") or \
        not os.path.exists(live_files.root / "Bad Sig 2 Project" / "Bad Sig 2.als")


def test_templates_by_name_user_default_and_old_schema(live_files, tmp_path):
    from livebridge import als
    from ableton_mcp import projects
    made = projects.create("Eight", template="8-track")
    assert made["template"] == "8-Track Template" and made["template_is_base"] is False
    with pytest.raises(projects.ProjectError, match="no template 'Nope'"):
        projects.create("X", template="Nope")
    old = tmp_path / "Old.als"
    als.write(str(old), _set_xml(schema="12.0_12117"))
    made = projects.create("From Old", template=str(old))
    assert "older than this Live's 12.0_12402" in made["warning"]
    als.write(str(live_files.user / "Templates" / "Default Set.als"), _set_xml(tracks=("Mine",)))
    made = projects.create("Mine Too")
    assert made["template"] == "Default Set" and made["template_is_base"] is False   # the user's default
    listed = {t["name"]: t for t in projects.list_templates()}
    assert listed["Default Set"]["where"] == "user" and listed["Vocal Recording"]["saves_in_place"] is True


def test_list_and_resolve_projects(live_files):
    import os
    from ableton_mcp import projects
    assert projects.list_projects()["exists"] is False
    a = projects.create("Alpha")
    b = projects.create("Beta")
    os.utime(a["set_path"], (1e9, 1e9))                       # Alpha is older
    (live_files.root / "Not A Project").mkdir()                # no marker, no " Project", no set
    listed = projects.list_projects(open_set=b["set_path"])
    assert [p["name"] for p in listed["projects"]] == ["Beta", "Alpha"]
    assert listed["projects"][0]["sets"][0]["is_open"] is True and listed["projects"][0]["has_marker"]
    assert projects.resolve_set("Alpha") == a["set_path"]
    assert projects.resolve_set(a["project_dir"]) == a["set_path"]
    assert projects.resolve_set(b["set_path"]) == b["set_path"]
    with pytest.raises(projects.ProjectError, match="no project 'Gamma'.*Alpha"):
        projects.resolve_set("Gamma")
    assert projects.project_of(a["set_path"]) == os.path.realpath(a["project_dir"])
    assert projects.project_of("/tmp/loose.als") is None


@pytest.fixture
def live_opens(served, live_files, monkeypatch):
    """The MCP server against the fake Live, with `open -a` loading the set the way Live does (or
    putting up Live's save prompt, with world.dirty)."""
    from ableton_mcp import server
    client, w = served
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_live_running", lambda: True)
    w.dirty = False
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["open", "-a"]:
            if w.dirty:
                w.app.open_dialog_count = 1
                w.app.current_dialog_message = 'Save changes to "Untitled" before closing?'
            else:
                w.song.file_path = cmd[3]
                w.song.name = os.path.splitext(os.path.basename(cmd[3]))[0]
        return Obj(returncode=0)

    import os
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return Obj(server=server, w=w, calls=calls, files=live_files)


def test_create_project_opens_it_and_builds_tracks(live_opens):
    server, w = live_opens.server, live_opens.w
    made = server.create_project("Night Drive", tempo=92)
    assert made["opened"] is True and made["set_path"].endswith("Night Drive Project/Night Drive.als")
    assert live_opens.calls[-1][:3] == ["open", "-a", str(live_opens.files.app)]
    # the base's tracks (here the fake's two) are replaced by Live's new-set layout
    assert [t["kind"] for t in made["tracks"]] == ["midi", "midi", "audio", "audio"]
    assert made["template_tracks_removed"] == 2 and made["unsaved_changes"] is True and "Cmd-S" in made["save"]

    made = server.create_project("Keys", tracks=[{"kind": "midi", "name": "Keys", "instrument": "Drift"},
                                                 {"kind": "audio", "name": "Vox", "color": 7},
                                                 {"kind": "midi", "name": "Broken", "instrument": "Nope"}])
    assert [t["name"] for t in made["tracks"]] == ["Keys", "Vox"]
    assert made["tracks"][0]["devices"] == ["Drift"] and made["tracks"][1]["color_index"] == 7
    assert made["errors"][0]["track"]["name"] == "Broken" and "no instrument" in made["errors"][0]["error"]

    with pytest.raises(server.ToolError, match="unknown track fields"):
        server.create_project("Typo", tracks=[{"knd": "midi"}])
    with pytest.raises(server.ToolError, match="open_in_live"):
        server.create_project("Closed", tracks=[{"kind": "midi"}], open_in_live=False)
    closed = server.create_project("Closed", open_in_live=False)
    assert "opened" not in closed and os.path.exists(closed["set_path"])

    info = server.get_project_info()
    assert info["name"] == "Keys" and info["saved_to_disk"] is True and info["project_dir"].endswith("Keys Project")
    assert info["tempo"] == 120.0 and info["signature"] == [4, 4] and info["live_is_asking"] is None
    listed = server.list_projects()
    assert {p["name"] for p in listed["projects"]} == {"Night Drive", "Keys", "Closed"}
    assert any(s["is_open"] for p in listed["projects"] for s in p["sets"] if p["name"] == "Keys")
    assert {t["name"] for t in listed["templates"]} == {"Vocal Recording", "8-Track Template"}


def test_open_project_reports_lives_save_prompt(live_opens):
    server, w = live_opens.server, live_opens.w
    server.create_project("Alpha", open_in_live=False)
    w.dirty = True
    out = server.open_project("Alpha")
    assert out["opened"] is False and out["live_is_asking"] == 'Save changes to "Untitled" before closing?'
    assert "user answers" in out["next"]
    with pytest.raises(server.ToolError, match="answered in Live first"):
        server.open_project("Alpha")                      # the dialog is still up
    with pytest.raises(server.ToolError, match="answered in Live first"):
        server.create_project("Beta")                     # checked before anything is written
    assert not (live_opens.files.root / "Beta Project").exists()

    # the user answers it in Live; then it opens
    w.dirty = False
    w.app.open_dialog_count, w.app.current_dialog_message = 0, ""
    out = server.open_project("Alpha")
    assert out["opened"] is True and out["project"]["name"] == "Alpha" and "built" not in out
    assert server.open_project("Alpha")["already_open"] is True


def test_create_project_stopped_by_lives_prompt_is_finished_by_open_project(live_opens):
    server, w = live_opens.server, live_opens.w
    w.dirty = True
    made = server.create_project("Gamma", tracks=[{"kind": "midi", "name": "Lead"}])
    assert made["opened"] is False and "open_project(" in made["next"]
    # the user answers Live's prompt, and Live goes on to open the new set
    w.dirty = False
    w.app.open_dialog_count, w.app.current_dialog_message = 0, ""
    w.song.file_path = made["set_path"]
    out = server.open_project(made["set_path"])
    assert out["already_open"] is True and [t["name"] for t in out["built"]["tracks"]] == ["Lead"]
    assert out["built"]["template_tracks_removed"] == 2 and "Cmd-S" in out["save"]
    assert "built" not in server.open_project(made["set_path"])   # only once


# -- Phase 5: reconnects, errors, tick budgets, logging ----------------------------

def _bridge(port=0, world=None):
    """A BridgeServer over the fake LOM, polled every 10 ms; returns (server, stop)."""
    world = world or make_world()
    srv = bridge_server.BridgeServer(world.cmds.dispatch, port=port)
    stop = threading.Event()

    def tick():
        while not stop.is_set():
            srv.poll()
            time.sleep(0.01)
    th = threading.Thread(target=tick, daemon=True)
    th.start()

    def shut():
        stop.set()
        th.join()
        srv.close()
    return srv, shut


@contextlib.contextmanager
def socket_free_port():
    """A port nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    yield port


def test_client_reconnects_after_the_bridge_restarts():
    srv, shut = _bridge()
    port = srv.address[1]
    client = LiveClient(port=port, timeout=2.0, reconnect_wait=3.0)
    assert client.call("ping")["pong"]
    shut()                                               # Live re-creates the bridge (a set load)
    time.sleep(0.1)
    restarted = []

    def come_back():
        time.sleep(0.6)                                  # refused for a moment, like Live
        restarted.append(_bridge(port))
    threading.Thread(target=come_back, daemon=True).start()
    assert client.call("create_midi_track", name="After")["name"] == "After"   # not a read: still fine,
    assert client.reconnects == 1                        # because the stale socket is noticed first
    restarted[0][1]()
    client.close()


def test_a_first_connection_fails_at_once():
    with socket_free_port() as port:
        client = LiveClient(port=port, timeout=1.0, reconnect_wait=3.0)
        t0 = time.monotonic()
        with pytest.raises(ConnectionRefusedError):
            client.call("ping")
        assert time.monotonic() - t0 < 0.5


def _flaky_bridge(answer_after):
    """A raw server that closes the first `answer_after` connections right after reading a request
    (as when Live re-creates the bridge mid-call), then answers every request "done". Returns (port,
    the commands it read, a stop event)."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    seen, stop = [], threading.Event()

    def handle(conn, drop):
        with conn, conn.makefile("rb") as lines:
            for line in lines:
                req = json.loads(line)
                seen.append(req["cmd"])
                if drop:
                    return                                # close without answering
                conn.sendall(json.dumps({"id": req["id"], "ok": True, "result": "done"}).encode() + b"\n")

    def serve():
        drops = answer_after
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            handle(conn, drops > 0)
            drops -= 1
        listener.close()
    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1], seen, stop


def test_only_reads_are_sent_again_when_the_connection_drops_mid_call():
    port, seen, stop = _flaky_bridge(answer_after=1)
    client = LiveClient(port=port, timeout=2.0, reconnect_wait=1.0)
    assert client.call("get_state") == "done" and seen == ["get_state", "get_state"]
    client.close()
    stop.set()

    port, seen, stop = _flaky_bridge(answer_after=1)
    client = LiveClient(port=port, timeout=2.0, reconnect_wait=1.0)
    with pytest.raises(OSError):
        client.call("create_midi_track")                 # it may have run: not repeated
    assert seen == ["create_midi_track"]
    assert client.call("ping") == "done"                 # and the next call reconnects
    client.close()
    stop.set()


def test_a_timeout_is_not_retried():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)                                   # accepts, never answers
    client = LiveClient(port=listener.getsockname()[1], timeout=0.3)
    t0 = time.monotonic()
    with pytest.raises(socket.timeout):
        client.call("ping")
    assert time.monotonic() - t0 < 0.9                   # one timeout, not two
    listener.close()


def test_unreachable_messages_say_what_is_wrong(monkeypatch):
    from ableton_mcp import server
    with socket_free_port() as port:
        monkeypatch.setattr(server, "_client", LiveClient(port=port, timeout=0.5))
        monkeypatch.setattr(server, "_live_running", lambda: False)
        with pytest.raises(server.ToolError, match="Live isn't running"):
            server.get_state()
        monkeypatch.setattr(server, "_live_running", lambda: True)
        with pytest.raises(server.ToolError, match="Live is running, but ClaudeLiveBridge isn't answering"):
            server.list_tracks()
    assert "didn't answer within" in server._unreachable(socket.timeout("timed out"))


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_indexer_keeps_to_its_budget_per_tick():
    """Each node costs 1 ms on a fake clock; a 10 ms budget must give ticks of about 10 ms."""
    clock = _Clock()

    class SlowItem(BrowserItem):
        @property
        def iter_children(self):
            for child in self.children:
                clock.now += 0.001                       # reading a child from Live: 1 ms
                yield child
    leaves = [SlowItem("s%03d.wav" % i, loadable=True, uri="query:Samples#FileId_%d" % i) for i in range(500)]
    browser = Obj(**{root: SlowItem(root) for root in bridge_indexer.ROOTS if root != "user_folders"})
    browser.samples = SlowItem("Samples", leaves)        # one flat root, like Live's
    browser.user_folders = []
    idx = bridge_indexer.Indexer(clock=clock)
    idx.start(browser)
    ticks = []
    while idx.state == "running":
        t0 = clock.now
        idx.step(10)
        ticks.append((clock.now - t0) * 1000)
    assert len(idx.entries) == 500
    assert max(ticks) <= 11.0 + 1e-9                    # the budget, plus the one node that crosses it
    assert len(ticks) >= 45                              # 500 ms of work really was spread out


def test_the_socket_serves_at_most_64_commands_per_tick():
    w = make_world()
    srv = bridge_server.BridgeServer(w.cmds.dispatch, port=0)
    sock = socket.create_connection(srv.address)
    sock.sendall(b"".join(b'{"id": %d, "cmd": "ping"}\n' % i for i in range(150)))
    time.sleep(0.1)
    counts = []
    received = b""
    sock.settimeout(0.2)
    for _ in range(4):
        srv.poll()
        try:
            while True:
                chunk = sock.recv(1 << 20)
                if not chunk:
                    break
                received += chunk
                if len(chunk) < (1 << 20):
                    break
        except socket.timeout:
            pass
        counts.append(received.count(b"\n"))
    assert counts[:3] == [64, 128, 150]
    sock.close()
    srv.close()


def test_tool_calls_and_failures_go_to_the_log(tmp_path, monkeypatch):
    import logging
    from ableton_mcp import server
    handler = server._setup_logging(str(tmp_path / "logs" / "server.log"))
    try:
        with socket_free_port() as port:
            monkeypatch.setattr(server, "_client", LiveClient(port=port, timeout=0.5))
            monkeypatch.setattr(server, "_live_running", lambda: False)
            with pytest.raises(server.ToolError):
                server.set_tempo(bpm=97)
        server.list_projects(location=str(tmp_path / "nowhere"))
        handler.flush()
        text = (tmp_path / "logs" / "server.log").read_text()
        assert 'set_tempo {"bpm": 97} failed' in text and "Live isn't running" in text
        assert "list_projects" in text and "ok in" in text
        assert server._setup_logging(str(tmp_path / "off")) is None
    finally:
        for name in ("ableton_mcp", "livebridge"):
            logging.getLogger(name).removeHandler(handler)
        handler.close()


def test_index_build_waits_for_a_walk_already_running(served):
    from ableton_mcp.index import Index
    client, w = served
    client.call("index_start")                           # another MCP server started it
    bridge_indexer.INDEX.budget_ms = 0.01
    calls = []
    real = client.call

    def spy(cmd, **args):
        calls.append(cmd)
        return real(cmd, **args)
    client.call = spy
    idx = Index.build(client, {"live_version": "test"})
    assert "index_start" not in calls and len(idx.entries) > 5
