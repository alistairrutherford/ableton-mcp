"""MCP server (stdio) exposing the ClaudeLiveBridge commands as tools.

    uv run python -m ableton_mcp

Each tool forwards to the Remote Script over localhost TCP. Tracks are 0-based indices, names,
"master", or "return:N" for return tracks; devices and parameters are indices or names.
"""

import os
from typing import Any, Optional, Union

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from livebridge import livelog
from livebridge.client import BRIDGE_NAME, BridgeError, LiveClient

from .index import NEEDS_MIDI, Index

Key = Union[int, str]

REMOTE_SCRIPTS = os.path.join(os.environ.get("LIVE_USER_LIBRARY", os.path.expanduser("~/Music/Ableton/User Library")),
                              "Remote Scripts")

# Where add_track looks for an instrument given by name, in order.
INSTRUMENT_CATEGORIES = ("instruments", "sounds", "drums", "plugins")

mcp = MCPServer("ableton")
_client = LiveClient(timeout=15.0)
_index = None   # the browser Index, built or read from the cache on the first search


def _hints(**hints):
    """ToolAnnotations from snake_case hints (mcp 2.x field names), or their camelCase 1.x names."""
    try:
        return ToolAnnotations(**hints)
    except Exception:
        return ToolAnnotations(**{k.split("_")[0] + "".join(w.title() for w in k.split("_")[1:]): v
                                  for k, v in hints.items()})


READ_ONLY = _hints(read_only_hint=True)
DESTRUCTIVE = _hints(destructive_hint=True)


def _call(cmd, **args):
    args = {k: v for k, v in args.items() if v is not None}
    try:
        return _client.call(cmd, **args)
    except BridgeError as e:
        raise ToolError(str(e))  # ToolError text reaches the model; other exceptions are hidden
    except OSError as e:
        _client.close()
        raise ToolError(
            "Cannot reach the %s control surface on %s:%d (%s). Is Live running, with %s selected "
            "in Settings > Link, Tempo & MIDI?" % (BRIDGE_NAME, _client.host, _client.port, e, BRIDGE_NAME))


@mcp.tool()
def live_status() -> dict:
    """Start here. Whether Live and the bridge are up, with Live's version, tempo and track count;
    if they aren't, what to fix (install the Remote Script, start Live, select the control surface)."""
    status = {"bridge": BRIDGE_NAME, "host": _client.host, "port": _client.port}
    try:
        ping = _client.call("ping")
        state = _client.call("get_state", include_devices=False)
    except BridgeError as e:
        return dict(status, ok=False, error=str(e))
    except OSError as e:
        _client.close()
        installed = os.path.exists(os.path.join(REMOTE_SCRIPTS, BRIDGE_NAME))
        if not installed:
            fix = ("The Remote Script isn't installed in %s: run scripts/install_remote_script.sh, then "
                   "restart Live." % REMOTE_SCRIPTS)
        else:
            fix = ("Start Live, and pick %s in an empty Control Surface row of Settings > Link, Tempo & "
                   "MIDI (Input and Output: None). Live loads Remote Scripts only at startup." % BRIDGE_NAME)
        return dict(status, ok=False, error="cannot connect: %s" % e, remote_script_installed=installed, fix=fix)
    return dict(status, ok=True, live_version=ping["live_version"], tempo=state["tempo"],
                signature=state["signature"], track_count=len(state["tracks"]),
                is_playing=state["is_playing"])


@mcp.tool()
def ping() -> dict:
    """Check that Live and the bridge are up; returns Live's version."""
    return _call("ping")


@mcp.tool()
def get_state(include_devices: bool = True) -> dict:
    """Song and transport state, plus every track with its devices, arm, monitoring and routing."""
    return _call("get_state", include_devices=include_devices)


@mcp.tool()
def set_tempo(bpm: float) -> dict:
    """Set the song tempo in BPM."""
    return _call("set_tempo", bpm=bpm)


@mcp.tool()
def start_transport(from_beat: Optional[float] = None, resume: bool = False) -> dict:
    """Start playback, optionally from a song position in beats, or continue from where it stopped."""
    return _call("start_transport", from_beat=from_beat, resume=resume)


@mcp.tool()
def stop_transport() -> dict:
    """Stop playback."""
    return _call("stop_transport")


@mcp.tool()
def set_song_properties(properties: dict) -> dict:
    """Set song properties: current_song_time, loop, loop_start, loop_length (beats), metronome,
    arrangement_overdub, record_mode, session_record, signature_numerator, signature_denominator,
    back_to_arranger, midi_recording_quantization ("none" or a Live.Song.RecordingQuantization name)."""
    return _call("set_song_properties", **properties)


def _load_and_wait(name, track, category, format=None):
    try:
        return _client.load_device_and_wait(name, track, category, format)
    except (BridgeError, TimeoutError) as e:
        raise ToolError(str(e))
    except OSError as e:
        _client.close()
        raise ToolError("bridge not reachable: %s" % e)


def _load_instrument(instrument, track, category):
    """Load `instrument`: a search result id, or a name looked up in `category` or else in the
    first of INSTRUMENT_CATEGORIES that has it."""
    if instrument.startswith("query:") or (_index is not None and _index.get(instrument)):
        entry = _entry(instrument)
        if entry["kind"] not in ("instrument", "sound", "drum", "plugin", "sample", "preset"):
            raise ToolError("%r is a %s, not an instrument" % (entry["stem"], entry["kind"]))
        return _load_entry(entry, track, None, "device")["device"]
    if category:
        return _load_and_wait(instrument, track, category)
    misses = []
    for cat in INSTRUMENT_CATEGORIES:
        try:
            return _client.load_device_and_wait(instrument, track, cat)
        except BridgeError as e:
            if "nothing named" not in str(e):
                raise ToolError(str(e))  # ambiguous, or a real failure: stop here
            misses.append(cat)
        except TimeoutError as e:
            raise ToolError(str(e))
        except OSError as e:
            _client.close()
            raise ToolError("bridge not reachable: %s" % e)
    raise ToolError("no instrument named %r in %s; try browser_search" % (instrument, ", ".join(misses)))


def _brief(t):
    """A track as list_tracks shows it."""
    out = {k: t.get(k) for k in ("index", "name", "kind", "color", "color_index", "mute", "solo", "arm")}
    if "devices" in t:
        out["devices"] = [d["name"] for d in t["devices"]]
    return out


@mcp.tool(annotations=READ_ONLY)
def list_tracks() -> dict:
    """Every track, return track and the master, with kind (midi, audio, group, return, master),
    colour, mute, solo, arm and device names. Use the index or the name to address a track;
    return tracks are "return:0", "return:1", ..."""
    state = _call("get_state", include_devices=True)
    return {"tracks": [_brief(t) for t in state["tracks"]],
            "return_tracks": [_brief(t) for t in state["return_tracks"]],
            "master": _brief(_call("get_track", track="master")),
            "selected_track": state["selected_track"]}


@mcp.tool()
def add_track(kind: str = "midi", name: Optional[str] = None, index: int = -1,
              color: Optional[Union[int, str]] = None, instrument: Optional[str] = None,
              instrument_category: Optional[str] = None) -> dict:
    """Add a track and return it.

    kind: "midi", "audio" or "return". index: where it goes (-1 = at the end); return tracks always
    go after the existing returns. color: a palette index 0..69 or "#rrggbb" (Live picks the nearest
    palette colour). instrument (MIDI tracks only): a browser name such as "Drift" or a preset
    such as "Attack Pad", looked up in instruments, sounds, drums, then plugins, unless
    instrument_category says where. If the instrument can't be loaded, the new track is removed again.
    """
    kind = kind.strip().lower()
    if kind not in ("midi", "audio", "return"):
        raise ToolError("kind must be midi, audio or return")
    if instrument and kind != "midi":
        raise ToolError("instruments go on MIDI tracks; use kind=\"midi\" (or load_device for effects)")
    if kind == "return":
        if index != -1:
            raise ToolError("Live always adds return tracks after the existing ones; leave index at -1")
        info = _call("create_return_track", name=name)
    else:
        info = _call("create_%s_track" % kind, index=index, name=name)
    track = info["index"]
    if color is not None:
        try:
            _call("set_track", track=track, color=color)
        except ToolError:
            _call("delete_track", track=track)
            raise
    if instrument:
        try:
            device = _load_instrument(instrument, track, instrument_category)
        except ToolError as e:
            _call("delete_track", track=track)
            raise ToolError("%s (the new track was removed again)" % e)
        info = _call("get_track", track=track)
        info["instrument"] = device
        return info
    return _call("get_track", track=track)


@mcp.tool()
def update_track(track: Key, name: Optional[str] = None, color: Optional[Union[int, str]] = None,
                 mute: Optional[bool] = None, solo: Optional[bool] = None, arm: Optional[bool] = None,
                 monitoring: Optional[str] = None) -> dict:
    """Rename a track or set its colour (palette index 0..69 or "#rrggbb"), mute, solo, arm, or
    monitoring ("in", "auto", "off")."""
    return _call("set_track", track=track, name=name, color=color, mute=mute, solo=solo, arm=arm,
                 monitoring=monitoring)


@mcp.tool()
def duplicate_track(track: Key) -> dict:
    """Duplicate a track with its devices (plug-in state is saved and loaded into the copy). Not for
    return tracks or the master."""
    return _call("duplicate_track", track=track)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_track(track: Key) -> dict:
    """Delete a track or a return track, with its clips and devices. Live's Undo (Cmd-Z) brings it
    back; tell the user which track you are deleting."""
    return _call("delete_track", track=track)


# -- search and load (Phase 2) ---------------------------------------------------

def _get_index(rebuild=False):
    """The browser index: this process's copy, else the disk cache if Live's browser still matches
    it, else built by the Remote Script (about 10 s) and cached."""
    global _index
    if _index is not None and not rebuild:
        return _index
    fingerprint = _call("index_fingerprint")
    path = Index.cache_path(fingerprint["live_version"])
    cached = None if rebuild else Index.load(path, fingerprint)
    if cached is not None:
        cached.from_cache = True
        _index = cached
        return _index
    try:
        built = Index.build(_client, fingerprint)
    except (BridgeError, TimeoutError) as e:
        raise ToolError("could not index Live's browser: %s" % e)
    except OSError as e:
        _client.close()
        raise ToolError("bridge not reachable: %s" % e)
    built.from_cache = False
    built.save(path)
    _index = built
    return _index


def _entry(id):
    entry = _get_index().get(id)
    if entry is None:
        raise ToolError("no browser item with id %r; search again (or refresh_index if content changed)" % (id,))
    return entry


def _load_entry(entry, track, scene, expect):
    try:
        return _client.load_item_and_wait(entry["id"], track, entry["root"], entry["path"], scene, expect)
    except (BridgeError, TimeoutError, ValueError) as e:
        raise ToolError(str(e))
    except OSError as e:
        _client.close()
        raise ToolError("bridge not reachable: %s" % e)


@mcp.tool(annotations=READ_ONLY)
def search(query: str, kind: str = "any", limit: int = 20, pack: Optional[str] = None) -> dict:
    """Search Live's browser for instruments, presets, samples, clips and effects.

    kind: any, instrument (instruments and their presets, and Sounds), sound, drum, sample, clip,
    audio_effect, midi_effect, effect, plugin, max_for_live or preset. pack: only items from a
    pack whose name contains this. Results are ranked (exact name, then prefix, then all words in
    the name, then words in its folders or pack; typos are tried when nothing matches) and carry
    an `id` for load_item and preview_item. Samples have `hints` from their filename (bpm, key,
    loop). Live's browser has no tags: words like "warm" or "pad" match names and folders such as
    Sounds' "Ambient & Evolving". The first search indexes the browser (about 10 s), then it is
    cached on disk.
    """
    index = _get_index()
    try:
        results = index.search(query, kind=kind, limit=limit, pack=pack)
    except ValueError as e:
        raise ToolError(str(e))
    return {"results": results, "count": len(results),
            "index": {"entries": len(index.entries), "from_cache": getattr(index, "from_cache", None)}}


@mcp.tool()
def load_item(id: str, track: Key, scene: Optional[int] = None, keep_tempo: bool = True) -> dict:
    """Load a search result onto a track, as double-clicking it in Live's browser would.

    Instruments, presets and Sounds go on MIDI tracks (an instrument replaces the track's
    instrument). Effects go on any track. A sample goes into a new Simpler on a MIDI track, or into
    clip slot `scene` (default: the first empty one) on an audio track; a clip file goes into a
    slot too. Loading a loop can change the song tempo: with keep_tempo, the tempo is put back.
    """
    entry = _entry(id)
    t = _call("get_track", track=track)
    kind, track_kind = entry["kind"], t["kind"]
    if NEEDS_MIDI.intersection(entry["kinds"]) and track_kind != "midi":
        raise ToolError("%s %r needs a MIDI track; %r is a %s track" % (kind, entry["stem"], t["name"], track_kind))
    if kind in ("sample", "clip") and track_kind not in ("midi", "audio"):
        raise ToolError("a %s goes on a MIDI or audio track; %r is a %s track" % (kind, t["name"], track_kind))
    expect = "clip" if kind == "clip" or (kind == "sample" and track_kind == "audio") else "device"
    if expect == "clip":
        slots = _call("clip_slots", track=t["index"])
        if scene is None:
            empty = [sl["scene"] for sl in slots if not sl["has_clip"]]
            if not empty:
                raise ToolError("track %r has no empty clip slot; add a scene or give `scene`" % t["name"])
            scene = empty[0]
        elif not 0 <= scene < len(slots):
            raise ToolError("scene %d out of range (0..%d)" % (scene, len(slots) - 1))
        elif slots[scene]["has_clip"]:
            raise ToolError("slot %d on %r already has a clip" % (scene, t["name"]))
    res = _load_entry(entry, t["index"], scene if expect == "clip" else None, expect)
    if res["tempo_after"] != res["tempo_before"]:
        res["tempo_changed"] = {"from": res["tempo_before"], "to": res["tempo_after"], "restored": keep_tempo}
        if keep_tempo:
            _call("set_tempo", bpm=res["tempo_before"])
    res["item"] = Index.public(entry)
    return res


@mcp.tool()
def preview_item(id: str) -> dict:
    """Play a search result through Live's browser preview (samples; some presets), to audition it.
    stop_preview stops it."""
    entry = _entry(id)
    return _call("preview_item", id=entry["id"], root=entry["root"], path=entry["path"])


@mcp.tool()
def stop_preview() -> dict:
    """Stop the browser preview."""
    return _call("stop_preview")


@mcp.tool()
def refresh_index() -> dict:
    """Re-index Live's browser (about 10 s): after installing a pack or adding files to the User
    Library or a browser folder, if search doesn't find them."""
    return _get_index(rebuild=True).stats()


@mcp.tool()
def load_device(name: str, track: Key, category: str = "plugins", format: Optional[str] = None,
                wait: bool = True) -> dict:
    """Load a browser item by name onto a track (exact name preferred; for plugins VST3 is preferred
    over AU over VST unless `format` is given). An instrument replaces the track's instrument; an
    effect is added. With wait, returns the new device once it appears, and what it replaced."""
    if wait:
        return _load_and_wait(name, track, category, format)
    return _call("load_device", name=name, track=track, category=category, format=format)


@mcp.tool()
def list_devices(track: Key) -> list:
    """Devices on a track."""
    return _call("list_devices", track=track)


@mcp.tool()
def list_parameters(track: Key, device: Key) -> dict:
    """A device's parameters with value, range and (for quantized ones) value items."""
    return _call("list_parameters", track=track, device=device)


@mcp.tool()
def set_parameter(track: Key, device: Key, parameter: Key, value: Any) -> dict:
    """Set a device parameter: a number within [min, max], true/false, or a value item name."""
    return _call("set_parameter", track=track, device=device, parameter=parameter, value=value)


@mcp.tool()
def get_routing(track: Key, direction: str = "input") -> dict:
    """Current and available routing types and channels for a track ("input" or "output")."""
    return _call("get_routing", track=track, direction=direction)


@mcp.tool()
def set_routing(track: Key, direction: str = "input", type: Optional[str] = None,
                channel: Optional[str] = None, source_track: Optional[Key] = None) -> dict:
    """Set a track's routing type (by display name, or `source_track` for MIDI From another track),
    then its channel by display name (e.g. the plug-in name for its MIDI output, "Post FX")."""
    return _call("set_routing", track=track, direction=direction, type=type, channel=channel,
                 source_track=source_track)


@mcp.tool()
def start_recording(mode: str = "arrangement", from_beat: Optional[float] = None,
                    track: Optional[Key] = None, scene: Optional[int] = None,
                    length_beats: Optional[float] = None) -> dict:
    """Start arrangement recording (record mode + play, optionally from a beat) or session recording
    (into a track's clip slot, or session record on all armed tracks)."""
    return _call("start_recording", mode=mode, from_beat=from_beat, track=track, scene=scene,
                 length_beats=length_beats)


@mcp.tool()
def stop_recording(mode: str = "arrangement", stop_transport: bool = True) -> dict:
    """Stop arrangement or session recording, and by default the transport."""
    return _call("stop_recording", mode=mode, stop_transport=stop_transport)


@mcp.tool()
def clear_arrangement_clips(track: Key) -> dict:
    """Delete every arrangement clip on a track."""
    return _call("clear_arrangement_clips", track=track)


@mcp.tool()
def clear_session_clips(track: Key) -> dict:
    """Delete every clip in a track's session slots."""
    return _call("clear_session_clips", track=track)


@mcp.tool()
def get_clip_notes(track: Key, source: str = "auto", clip_index: Optional[int] = None,
                   scene: Optional[int] = None) -> dict:
    """Notes of the most recent clip on a track ("arrangement", "session" or "auto"). Arrangement
    notes include `song_time`, their position in the song in beats."""
    return _call("get_clip_notes", track=track, source=source, clip_index=clip_index, scene=scene)


@mcp.tool()
def get_meters(tracks: Optional[list] = None) -> list:
    """Output meter levels (0..1) of the given tracks (default all) and the master."""
    return _call("get_meters", tracks=tracks)


@mcp.tool()
def get_audio_settings() -> dict:
    """Live's current sample rate, buffer sizes and latencies, from the last "Audio In Out" block
    in its Log.txt (the Live Object Model doesn't expose them)."""
    try:
        version = _call("ping")["live_version"]
    except ToolError:
        version = None  # Live not reachable: read the most recent log anyway
    return livelog.audio_settings(version)


@mcp.tool()
def bridge_command(cmd: str, args: Optional[dict] = None) -> Any:
    """Send any bridge command by name (see the command table in README.md, or list_commands)."""
    return _call(cmd, **(args or {}))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
