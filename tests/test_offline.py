"""Tests that need no Live: the socket protocol, the commands against a fake LOM, and the timing math."""

import threading
import time

import pytest

import commands as bridge_commands
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


class Track(Obj):
    def __init__(self, name, devices=(), **kw):
        kw.setdefault("has_midi_input", True)
        kw.setdefault("can_be_armed", True)
        super().__init__(name=name, devices=list(devices), color=0x1AFF2F, color_index=0, is_foldable=False,
                         arm=False, current_monitoring_state=1, mute=False, solo=False,
                         playing_slot_index=-1, arrangement_clips=[], clip_slots=[],
                         output_meter_left=0.0, output_meter_right=0.0, output_meter_level=0.0,
                         available_input_routing_types=[], available_input_routing_channels=[],
                         input_routing_type=None, input_routing_channel=None,
                         output_routing_type=None, output_routing_channel=None, **kw)

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)


class Song(Obj):
    def __init__(self, tracks):
        super().__init__(tracks=tracks, return_tracks=[], master_track=Track("Master"), tempo=120.0,
                         signature_numerator=4, signature_denominator=4, is_playing=False,
                         current_song_time=0.0, record_mode=False, session_record=False,
                         is_counting_in=False, arrangement_overdub=False, loop=False,
                         metronome=False, midi_recording_quantization="rec_q_no_q",
                         view=Obj(selected_track=None))

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


class BrowserItem(Obj):
    def __init__(self, name, children=(), loadable=False, uri="", kind=None):
        super().__init__(name=name, children=list(children), is_loadable=loadable,
                         is_device=loadable, uri=uri, kind=kind)


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
        load_item=loaded.append,
    )
    app = Obj(browser=browser, get_major_version=lambda: 12, get_minor_version=lambda: 4,
              get_bugfix_version=lambda: 6)
    cmds = bridge_commands.Commands(lambda: song, lambda: app)
    world = Obj(song=song, app=app, cmds=cmds, src=src, rec=rec, plugin=plugin, loaded=loaded,
                test_chords=test_chords, port_type=port_type, pending=[])

    def apply_loads():
        """Like Live: a load lands a few ticks later on the selected track, and an instrument
        replaces the track's instrument instead of adding one."""
        while loaded:
            world.pending.append([3, loaded.pop(0), song.view.selected_track])
        for job in list(world.pending):
            job[0] -= 1
            if job[0] > 0:
                continue
            world.pending.remove(job)
            _, item, track = job
            kind = item.kind or "instrument"
            dev = device(item.name.rsplit(".", 1)[0], kind)
            old = [i for i, d in enumerate(track.devices) if d.type == 1]
            if kind == "instrument" and old:
                track.devices[old[0]] = dev
            else:
                track.devices.append(dev)

    world.apply_loads = apply_loads
    return world


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
    monkeypatch.setattr(server, "REMOTE_SCRIPTS", str(tmp_path))
    status = server.live_status()
    assert status["ok"] is False and status["remote_script_installed"] is False
    assert "install_remote_script.sh" in status["fix"]
    (tmp_path / "ClaudeLiveBridge").mkdir()
    status = server.live_status()
    assert status["remote_script_installed"] is True and "Control Surface" in status["fix"]


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
