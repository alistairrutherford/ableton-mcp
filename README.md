# ableton-mcp

An MCP server that lets Claude work in Ableton Live 12. It is built on **LiveBridge**, vendored
from NoteCap's copy (`../audio/notecap/LiveBridge`, 2026-10-24) and renamed **ClaudeLiveBridge**
on port **9873**, so it runs alongside Chordism's bridge (9871) and NoteCap's (9872) in one Live
session. See [PLAN.md](PLAN.md) for where this is going.

```
Claude ──stdio MCP──▶ ableton_mcp ──TCP 127.0.0.1:9873, JSON lines──▶ ClaudeLiveBridge Remote Script (inside Live)
                      livebridge/                                       polled from Live's update tick → LOM
```

| Path | What it is |
|---|---|
| `RemoteScript/ClaudeLiveBridge/` | The Live control surface. `server.py` is the socket (no Live imports), `commands.py` the LOM commands. |
| `src/livebridge/` | Python client (`client.py`), set-file editing (`als.py`), Live's `Log.txt` (`livelog.py`), note timing (`timing.py`), and a CLI (`python -m livebridge`). |
| `src/ableton_mcp/` | The MCP server (stdio). Registered in `.mcp.json` as `ableton`. |
| `scripts/install_remote_script.sh` | Symlinks the Remote Script into `~/Music/Ableton/User Library/Remote Scripts/`. |
| `scripts/restart_live.sh` | Quits Live and reopens it with a set, then waits for the bridge. |
| `tests/test_offline.py` | Protocol and commands against a fake LOM, timing math, `.als` editing. No Live needed. |

## Setup

Needs [uv](https://docs.astral.sh/uv/). Live 12 runs Remote Scripts on its own Python 3.11.

```sh
uv sync                              # .venv with mcp and pytest
scripts/install_remote_script.sh     # symlink (use --copy if Live ignores the link)
```

Then restart Live and, in **Settings → Link, Tempo & MIDI → Control Surface**, pick
**ClaudeLiveBridge** in an empty row. Leave its Input and Output on *None*. Live shows
*"ClaudeLiveBridge listening on port 9873"* in the status bar, and the script logs to
`~/Library/Preferences/Ableton/Live 12.x.x/Log.txt` (search for `ClaudeLiveBridge:`).

Check it:

```sh
uv run livebridge ping
uv run livebridge get_state
```

`LIVEBRIDGE_HOST` and `LIVEBRIDGE_PORT` override the client's address.

**Plug-in parameters.** The bridge reaches a plug-in only through the parameters on Live's
device panel, which Live fills by itself only for plug-ins with at most 64 parameters. For
bigger plug-ins, add `-_PluginAutoPopulateThreshold=128` to
`~/Library/Preferences/Ableton/Live 12.x.x/Options.txt`.

## Tests

```sh
uv run pytest            # offline: no Live needed
```

Live tests will go in `tests/` too. They take the `live` fixture from `conftest.py`, which skips
them when the bridge doesn't answer.

## MCP server

`.mcp.json` registers it for Claude Code in this folder:

```json
{"mcpServers": {"ableton": {"command": "uv", "args": ["run", "--directory", ".", "python", "-m", "ableton_mcp"]}}}
```

Its tools are the commands below, plus `live_status` (start here: Live's version, tempo and
track count, or what to fix if the bridge isn't answering), `load_device` (which waits for the
device), `get_audio_settings` (sample rate, buffer sizes and latencies from Live's log), and
`bridge_command` (any command by name). It works with the `mcp` SDK 1.x (`FastMCP`) and 2.x
(`MCPServer`). Claude Code asks to approve the server the first time it starts in this folder.

## Manual steps (not in the Live Object Model)

- **Creating, opening and saving sets.** The LOM has none of these. PLAN.md Phase 3 covers how
  projects will be made from a template `.als`.
- **A device's Sidechain chooser.** The LOM doesn't reach it, but `python -m livebridge.als`
  routes it in a copy of a saved set.
- **Audio buffer size and sample rate** (Settings → Audio). `get_audio_settings` reads the
  current values from Live's log.
- **Plug-in windows.** The LOM can't open a plug-in editor or click in it.

## Socket protocol

TCP on `127.0.0.1:9873`, localhost only. One JSON object per line, UTF-8, `\n` terminated, in
each direction. Several clients may connect. Requests on one connection are answered in order.

```json
{"id": 7, "cmd": "set_tempo", "args": {"bpm": 120}}
{"id": 7, "ok": true, "result": {"tempo": 120.0}}
{"id": 8, "ok": false, "error": "CommandError: no track named 'Foo'; have ['1-MIDI', '2-MIDI']", "traceback": "..."}
```

`id` is any JSON value and is echoed back. `args` is optional. Errors never close the
connection; a line that isn't JSON gets an error reply with `id: null`.

**Threading.** The listener and client sockets are non-blocking and are serviced only in
`update_display`, Live's main-thread tick (about every 100 ms). Each tick accepts, reads, runs
up to 64 commands and writes. So every LOM call runs on the main thread, and one call takes up
to ~100 ms. Nothing blocks in Live. Commands that Live finishes later (loading a device) return
straight away, and the client polls (`load_device_and_wait`).

**Addressing.** `track`: 0-based index, a name, or `"master"`. `device` and `parameter`: 0-based
index or a name. Names match exactly (ignoring case) first, then by a unique substring.

### Commands

| Command | Args | Result |
|---|---|---|
| `ping` | | `{pong, live_version}` |
| `list_commands` | | command names |
| `get_state` | `include_devices=true` | tempo, signature, is_playing, current_song_time, record_mode, session_record, is_counting_in, arrangement_overdub, loop, metronome, midi_recording_quantization, `tracks[]` (index, name, is_midi, arm, monitoring, mute, solo, input/output routing, devices), master meter, selected_track |
| `set_tempo` | `bpm` | `{tempo}` |
| `start_transport` | `from_beat?`, `resume=false` | `{is_playing, current_song_time}` |
| `stop_transport` | | same |
| `set_song_properties` | any of `current_song_time, loop, loop_start, loop_length, metronome, arrangement_overdub, record_mode, session_record, signature_numerator, signature_denominator, back_to_arranger, midi_recording_quantization` (`"none"` or a `RecordingQuantization` name) | the new values |
| `create_midi_track` | `index=-1`, `name?` | the track |
| `create_audio_track` | `index=-1`, `name?` | the track |
| `duplicate_track` | `track` | the copy (Live saves each plug-in's state and loads it into the copy) |
| `delete_track` | `track` | `{deleted, track_count}` |
| `get_track` | `track` | the track |
| `set_track` | `track`, `name? arm? monitoring? ("in"/"auto"/"off") mute? solo?` | the track |
| `arm_track` | `track`, `arm=true` | `{track, arm}` |
| `browser_search` | `query`, `category="plugins"`, `format?`, `limit=50` | `[{name, path, uri, format, exact, is_device}]` |
| `load_device` | `name`, `track`, `category="plugins"`, `format?` | `{track, device_count_before, loaded, other_matches}`. The device appears a moment later. |
| `load_plugin` | `name`, `track`, `format?` | same as `load_device` with category `plugins` |
| `list_devices` | `track` | `[{index, name, class_name, class_display_name, type, is_active, parameter_count}]` |
| `list_parameters` | `track`, `device` | `{device, parameters: [{index, name, value, min, max, is_quantized, is_enabled, display, value_items?}]}` |
| `get_parameter` | `track`, `device`, `parameter` | the parameter |
| `set_parameter` | `track`, `device`, `parameter`, `value` (number, `true`/`false` → max/min, or a value item) | the parameter |
| `get_routing` | `track`, `direction="input"` | `{current: {type, channel}, available_types: [{display_name, category, track_index}], available_channels: [{display_name}]}` |
| `set_routing` | `track`, `direction`, `type?` or `source_track?`, `channel?` | as `get_routing` |
| `get_input_routing` / `set_input_routing` | as above, input only | |
| `start_recording` | `mode="arrangement"`, `from_beat?`; or `mode="session"` with `track?`, `scene?`, `length_beats?` | the record state |
| `stop_recording` | `mode`, `stop_transport=true` | the record and transport state |
| `clear_arrangement_clips` | `track` | `{deleted}` |
| `clear_session_clips` | `track` | `{deleted}` |
| `get_clip_notes` | `track`, `source="auto"` (`arrangement`/`session`), `clip_index?`, `scene?` | clip info (start_time, markers, loop, length) and `notes[]` (pitch, start_time, duration, velocity, mute, probability, and `song_time` for arrangement clips) |
| `get_meters` | `tracks?` (default all) | `[{track, name, left, right, level}]`, master last |
| `get_presets` | `track`, `device` | `{device, presets[], selected}`: Live's preset list for a plug-in (for a JUCE VST3, only "Default") |
| `set_preset` | `track`, `device`, `preset` (index or name) | as `get_presets`, after selecting it |
| `load_clip_file` | `name`, `category="user_library"` | loads a MIDI file from the browser as a double-click would: Live imports it as a session clip on a new MIDI track; returns `track_count_before` and the browser item |
| `get_cpu` | | `{average, peak}`: Live's CPU meter (`Application.average_process_usage` / `peak_process_usage`, in percent: about 0.6 for an empty set in 12.4.6), `null` where Live lacks them |
| `delete_device` | `track`, `device` | `{track, deleted, devices}` |

**Plug-in lookup.** `load_device` walks `application().browser.<category>` (for plugins, the
Plug-Ins tree). It loads onto the track by selecting it and calling `browser.load_item`.
An exact name wins over a substring match. If a plug-in is listed in more than one format, the
order is VST3, then AU, then VST, unless `format` says otherwise. The format comes from the
browser folder above the item ("VST3", "Audio Units", "VST"), or else from its URI.

**MIDI From another track's plug-in.** Set the routing type with `source_track` (matched by the
routing type's `attached_object`, so track names don't matter). Then set `channel` to the plug-in's
name, which Live lists next to "Pre FX" and "Post FX".

**"Most recent clip".** In the arrangement, the MIDI clip that starts latest. In Session View,
the slot last recorded through this bridge, or else the recording or playing slot, or else the
last filled one. Note times are in clip beats. `song_time = clip.start_time + start_time - start_marker`.

