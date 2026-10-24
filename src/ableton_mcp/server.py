"""MCP server (stdio) exposing the ClaudeLiveBridge commands as tools.

    uv run python -m ableton_mcp

Each tool forwards to the Remote Script over localhost TCP. Tracks are 0-based indices or
names ("master" for the master track); devices and parameters are indices or names.
"""

import os
from typing import Any, Optional, Union

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

from livebridge import livelog
from livebridge.client import BRIDGE_NAME, BridgeError, LiveClient

Key = Union[int, str]

REMOTE_SCRIPTS = os.path.join(os.environ.get("LIVE_USER_LIBRARY", os.path.expanduser("~/Music/Ableton/User Library")),
                              "Remote Scripts")

mcp = MCPServer("ableton")
_client = LiveClient(timeout=15.0)


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


@mcp.tool()
def create_midi_track(index: int = -1, name: Optional[str] = None) -> dict:
    """Create a MIDI track at index (-1 = at the end), optionally named."""
    return _call("create_midi_track", index=index, name=name)


@mcp.tool()
def create_audio_track(index: int = -1, name: Optional[str] = None) -> dict:
    """Create an audio track at index (-1 = at the end), optionally named."""
    return _call("create_audio_track", index=index, name=name)


@mcp.tool()
def duplicate_track(track: Key) -> dict:
    """Duplicate a track with its devices (plug-in state is saved and loaded into the copy)."""
    return _call("duplicate_track", track=track)


@mcp.tool()
def delete_track(track: Key) -> dict:
    """Delete a track."""
    return _call("delete_track", track=track)


@mcp.tool()
def set_track(track: Key, name: Optional[str] = None, arm: Optional[bool] = None,
              monitoring: Optional[str] = None, mute: Optional[bool] = None,
              solo: Optional[bool] = None) -> dict:
    """Rename a track or set arm, monitoring ("in", "auto", "off"), mute and solo."""
    return _call("set_track", track=track, name=name, arm=arm, monitoring=monitoring, mute=mute, solo=solo)


@mcp.tool()
def arm_track(track: Key, arm: bool = True) -> dict:
    """Arm or disarm a track for recording."""
    return _call("arm_track", track=track, arm=arm)


@mcp.tool()
def browser_search(query: str, category: str = "plugins", format: Optional[str] = None,
                   limit: int = 50) -> list:
    """Search a browser tree (plugins, instruments, audio_effects, midi_effects, drums, sounds,
    max_for_live, user_library, packs) for loadable items whose name contains `query`.
    For plugins, `format` filters by vst3, au or vst."""
    return _call("browser_search", query=query, category=category, format=format, limit=limit)


@mcp.tool()
def load_device(name: str, track: Key, category: str = "plugins", format: Optional[str] = None,
                wait: bool = True) -> dict:
    """Load a browser item by name onto a track (exact name preferred; for plugins VST3 is preferred
    over AU over VST unless `format` is given). With wait, returns the new device once it appears."""
    if wait:
        try:
            return _client.load_device_and_wait(name, track, category, format)
        except (BridgeError, TimeoutError) as e:
            raise ToolError(str(e))
        except OSError as e:
            _client.close()
            raise ToolError("bridge not reachable: %s" % e)
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
