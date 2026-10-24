"""Tests that need no Live: the socket protocol, the commands against a fake LOM, and the timing math."""

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
              get_bugfix_version=lambda: 6)
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
