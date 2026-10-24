# ableton-mcp

A Claude Code plugin that lets Claude work in Ableton Live 12: create and open projects, add and
shape tracks, and search Live's browser for instruments, presets, samples and effects to load. It
is an MCP server plus an `ableton-live` skill that teaches Claude the workflow.

It is built on **LiveBridge**, vendored
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
| `src/ableton_mcp/` | The MCP server (stdio): `server.py` (tools), `index.py` (browser search), `projects.py` (projects on disk), `remote_script.py` (installing the Remote Script), and a CLI (`python -m ableton_mcp ...`). |
| `.claude-plugin/plugin.json` | The plugin: its MCP server (`ableton`) and its one option (the projects folder). |
| `.claude-plugin/marketplace.json` | Makes this folder a one-plugin marketplace, so it can be installed. |
| `skills/ableton-live/SKILL.md` | The skill: how Claude should use the tools, and what is left to the user. |
| `scripts/install_remote_script.sh` | Symlinks the Remote Script into `~/Music/Ableton/User Library/Remote Scripts/` (`python -m ableton_mcp install-remote-script` does the same, copying by default). |
| `scripts/restart_live.sh` | Quits Live and reopens it with a set, then waits for the bridge. |
| `tests/test_offline.py` | Protocol and commands against a fake LOM, timing math, `.als` editing. No Live needed. |

## Setup

You need macOS, Live 12, [uv](https://docs.astral.sh/uv/) on your `PATH`, and Claude Code. Live runs
Remote Scripts on its own Python 3.11; the server runs on Python 3.10 or later, which uv provides.

**1. Install the plugin.** This folder is a marketplace with one plugin in it:

```sh
claude plugin marketplace add /path/to/ableton-mcp
claude plugin install ableton-mcp@ableton-mcp
```

Or, in Claude Code, `/plugin marketplace add /path/to/ableton-mcp`, then `/plugin install
ableton-mcp@ableton-mcp`. Claude Code asks for the plugin's one option, the **projects folder**.
Leave it empty for `~/Music/Ableton/Projects`. The server's virtualenv goes in the plugin's data
folder (`~/.claude/plugins/data/...`), so it survives plugin updates. To try the plugin without
installing it, start Claude Code with `claude --plugin-dir /path/to/ableton-mcp`.

**2. Install the Remote Script.** Ask Claude ("set up Ableton"): `live_status` notices that the
Remote Script is missing, and `install_remote_script` copies it into
`~/Music/Ableton/User Library/Remote Scripts/ClaudeLiveBridge`. Or run it yourself:

```sh
uv run --directory /path/to/ableton-mcp python -m ableton_mcp install-remote-script          # a copy
uv run --directory /path/to/ableton-mcp python -m ableton_mcp install-remote-script --link   # a symlink, for a git checkout
```

A copy has to be reinstalled when the plugin updates. `live_status` says when it no longer
matches.

**3. Select it in Live, once.** Restart Live, since it loads Remote Scripts only at startup. In
**Settings → Link, Tempo & MIDI → Control Surface**, pick **ClaudeLiveBridge** in an empty row and
leave its Input and Output on *None*. Live remembers the choice. Its status bar shows
*"ClaudeLiveBridge listening on port 9873"*, and the script logs to
`~/Library/Preferences/Ableton/Live 12.x.x/Log.txt` (search for `ClaudeLiveBridge:`).

Check it:

```sh
uv run python -m ableton_mcp status    # the Remote Script, Live, and the projects folder
uv run livebridge ping
```

`LIVEBRIDGE_HOST` and `LIVEBRIDGE_PORT` override the client's address. `ABLETON_MCP_PROJECTS`,
`ABLETON_MCP_CACHE`, `LIVE_APP` and `LIVE_USER_LIBRARY` override where things are.

**Claude Desktop.** Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json`.
Desktop apps don't get your shell's `PATH`, so give uv's full path (`which uv`):

```json
{"mcpServers": {"ableton": {"command": "/Users/you/.local/bin/uv",
  "args": ["run", "--quiet", "--directory", "/path/to/ableton-mcp", "python", "-m", "ableton_mcp"]}}}
```

The `ableton-live` skill comes with the Claude Code plugin. Where the server runs without it,
Claude has only the tools' own descriptions to go on.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `live_status`: "cannot connect", Remote Script installed | Live isn't running, or ClaudeLiveBridge isn't selected as a control surface. Live loads Remote Scripts only at startup. |
| ClaudeLiveBridge isn't in Live's Control Surface list | Live hasn't restarted since the install, or the folder is missing from `User Library/Remote Scripts/`. If a symlink is ignored, reinstall it as a copy. |
| Status bar: "ClaudeLiveBridge: port 9873 busy" | Something else holds the port. Usually a second Live instance; check with `lsof -nP -iTCP:9873`. |
| `live_status` says the installed Remote Script differs | Run `install_remote_script` again, then restart Live. |
| The server doesn't start in Claude Desktop | Use uv's full path (see above). In Claude Code, `claude mcp list` shows the server's state. |
| `search` takes about 10 s the first time | It is indexing Live's browser; afterwards it uses the cache in `~/Library/Caches/ableton-mcp/`. |
| New packs or samples don't show up in `search` | `refresh_index`. |
| A sample loads nowhere | It goes to a MIDI track (Simpler) or an audio track's clip slot; the load brings Session view to the front first. |
| Live asks "Save changes…?" when a project opens | That is Live protecting the open set: answer it in Live. Then `open_project` again finishes a `create_project`. |

**Plug-in parameters.** The bridge reaches a plug-in only through the parameters on Live's
device panel, which Live fills by itself only for plug-ins with at most 64 parameters. For
bigger plug-ins, add `-_PluginAutoPopulateThreshold=128` to
`~/Library/Preferences/Ableton/Live 12.x.x/Options.txt`.

## Tests

```sh
uv run pytest tests/test_offline.py     # no Live needed
uv run pytest tests/test_tracks_live.py # Phase 1 in Live: tracks, colours, instruments
uv run pytest tests/test_search_live.py # Phase 2 in Live: index, search, load samples and devices
uv run pytest tests/test_projects_live.py -s   # Phase 3 in Live: create, open, Cmd-S (needs you)
```

Live tests take the `live` fixture from `conftest.py`, which skips them when the bridge doesn't
answer. Run them in a scratch set: they add tracks named `MCP ...` and remove them again.

## MCP server

The plugin runs it as `uv run --quiet --directory ${CLAUDE_PLUGIN_ROOT} python -m ableton_mcp`. To
work on it in this folder, start Claude Code with `claude --plugin-dir .`.

Its tools are most of the commands below, plus:

- `live_status`: start here. It gives Live's version, tempo and track count, or what to fix if
  the bridge isn't answering.
- `list_tracks`: tracks, return tracks and the master, with kind, colour and device names.
- `add_track`: a MIDI, audio or return track, with a name, position, colour and (MIDI) an
  instrument. The instrument is looked up in instruments, sounds, drums, then plugins. If it
  can't be loaded, the track is removed again.
- `update_track`: name, colour, mute, solo, arm, monitoring.
- `search`: Live's browser, ranked, with a `kind` filter (instrument, sound, drum, sample, clip,
  audio_effect, midi_effect, effect, plugin, preset) and an optional `pack`. Results carry an `id`;
  samples carry `hints` from the filename (bpm, key, loop). See "Browser index" below.
- `load_item`: a search result onto a track. Instruments and presets go on MIDI tracks; a sample
  goes into a Simpler on a MIDI track, or into the first empty clip slot on an audio track; a
  loop's tempo change is undone unless `keep_tempo=false`.
- `preview_item` / `stop_preview`: audition a search result through Live's browser preview.
- `refresh_index`: re-index after installing a pack or adding files.
- `load_device`: load by name instead of by id; waits for the device and says what it replaced.
- `create_project`: makes `<location>/<name> Project/<name>.als` (default location
  `~/Music/Ableton/Projects`, or `$ABLETON_MCP_PROJECTS`) with tempo and signature, opens it, and
  builds its `tracks` (add_track arguments). See "Projects" below.
- `open_project`: by name, project folder or `.als` path.
- `list_projects`: projects newest first, which set is open, and the templates.
- `get_project_info`: the open set's file, project folder, tempo, signature, key, tracks, and any
  dialog Live is showing.
- `get_audio_settings`: sample rate, buffer sizes and latencies from Live's log.
- `install_remote_script`: copy (or `link=true`: symlink) the Remote Script into Live's User Library.
- `bridge_command`: any command by name. This includes the ones `add_track` and `update_track`
  stand in for (`create_*_track`, `set_track`, `arm_track`) and the Phase 0 probes.

`delete_track` is annotated as destructive and `list_tracks` as read-only. It works with the `mcp` SDK 1.x (`FastMCP`) and 2.x
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

**Addressing.** `track`: 0-based index, a name, `"master"`, or `"return:N"` (0-based: `"return:0"` is
the A return). Names are looked up among the regular tracks first, then the returns. Live reads a
return's name back with its letter (`"MCP Verb"` → `"C-MCP Verb"`), and the name without the
letter matches exactly. `device` and `parameter`: 0-based index or a name. Names match exactly
(ignoring case) first, then by a unique substring.

**Colour.** `color` is a palette index 0..69 (`color_index`) or `"#rrggbb"`, which Live snaps
to the nearest palette colour (`#ff0000` → `#ff3636`, index 14). Tracks report both.

### Commands

| Command | Args | Result |
|---|---|---|
| `ping` | | `{pong, live_version}` |
| `list_commands` | | command names |
| `get_state` | `include_devices=true` | tempo, signature, is_playing, current_song_time, record_mode, session_record, is_counting_in, arrangement_overdub, loop, metronome, midi_recording_quantization, `tracks[]` and `return_tracks[]` (index, name, kind, color, color_index, is_midi, arm, monitoring, mute, solo, input/output routing, devices with `id`), master meter, selected_track |
| `set_tempo` | `bpm` | `{tempo}` |
| `start_transport` | `from_beat?`, `resume=false` | `{is_playing, current_song_time}` |
| `stop_transport` | | same |
| `set_song_properties` | any of `current_song_time, loop, loop_start, loop_length, metronome, arrangement_overdub, record_mode, session_record, signature_numerator, signature_denominator, back_to_arranger, midi_recording_quantization` (`"none"` or a `RecordingQuantization` name) | the new values |
| `create_midi_track` | `index=-1`, `name?` | the track |
| `create_audio_track` | `index=-1`, `name?` | the track |
| `create_return_track` | `name?` | the track (always after the existing returns) |
| `duplicate_track` | `track` | the copy (Live saves each plug-in's state and loads it into the copy) |
| `delete_track` | `track` (also a return track) | `{deleted, name, track_count, return_track_count}` |
| `get_track` | `track` | the track |
| `set_track` | `track`, `name? color? arm? monitoring? ("in"/"auto"/"off") mute? solo?` | the track |
| `arm_track` | `track`, `arm=true` | `{track, arm}` |
| `browser_search` | `query`, `category="plugins"`, `format?`, `limit=50` | `[{name, path, uri, format, exact, is_device}]`. Walks the tree in one tick: prefer the index (`search`). |
| `index_start` | `roots?` (default all but `current_project`), `budget_ms?` (per tick, default 30) | the index status. The walk then runs a little per tick. |
| `index_status` | | `{state (idle/running/done), entries, open_folders, ticks, walk_ms, slowest_tick_ms, wall_s, budget_ms}` |
| `index_page` | `offset=0`, `limit=2000` | `{total, offset, entries: [{id, name, root, path, is_device, source}]}` |
| `index_fingerprint` | | `{live_version, packs, user_library, user_folders}`: what a cached index depends on |
| `load_item` | `id`, `track`, `root?`, `path?`, `scene?` | `{track, id, name, devices_before, scene, slot_had_clip, showed_session_view, tempo_before}`. `root` and `path` find the item again when the index in Live doesn't hold it (after a restart). With `scene`, Session view is brought to the front first: in Arrangement view Live ignores the load. |
| `view_state` | | which of Session, Arranger, Browser, Detail, Detail/Clip, Detail/DeviceChain are showing |
| `preview_item` | `id`, `root?`, `path?` | `{previewing, name}` |
| `stop_preview` | | `{stopped}` |
| `clip_slots` | `track` | `[{scene, has_clip, clip: {name, is_audio_clip, file_path, length}}]` |
| `load_device` | `name`, `track`, `category="plugins"`, `format?`, `scene?` (the clip slot to highlight first, where a sample goes on an audio track) | `{track, device_count_before, devices_before, loaded, other_matches}`. The device appears a moment later. An instrument replaces the track's instrument, so `load_device_and_wait` waits for the list to differ from `devices_before`. |
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

**Browser index.** `indexer.py` walks every browser root a few ms per Live tick (an iterator per
open folder, so even the flat Samples root never lists all its children in one tick). It keeps the
`id → BrowserItem` map at module level, because Live re-creates the control surface on every set
load. An item's id is its URI, or `<root>:<path>` where it has none. The MCP server reads the
entries once and caches them in `~/Library/Caches/ableton-mcp/browser-index-<live version>.json`
(override with `ABLETON_MCP_CACHE`), rebuilding when `index_fingerprint` changes. Live lists one
file in several places, and the server merges them:

- A preset under Instruments and under a Sounds category has the same `FileId` in both URIs.
- The Packs tree repeats almost everything under URIs with no `FileId`. A Packs entry joins the
  item from the same pack (its `source`) with the same parent folder and filename, or the same
  filename where that's unique.

In Live 12.4.7 Standard that turns 24,150 entries into 9,551 items, of which 550 exist only in
Packs (mostly legacy devices). An item is every kind it is listed as (`kind`, plus `also`). All
its folders count for ranking. Its kind comes from its root and file type. Browser items have no tags, so ranking uses, in order: the exact name, a prefix, all
words in the name, then words in its folders or pack. A few synonyms are built in ("verb",
"bd", "hh", "keys", ...), and close misspellings are tried when nothing matches.

**Projects.** The LOM can't create, open or save a set, so `projects.py` makes the files and
`open -a` opens them. What Live 12.4.7 needs (docs/spikes.md §1–2):

- **The marker first.** `<Name> Project/Ableton Project Info/` must exist before Live opens the
  set, or Cmd-S asks where to save it.
- **A current-schema template.** It must be on the running Live's schema (`MinorVersion`, the
  newest among the Core Library's templates), or Live asks to save it as a new file.
- **Patch both values.** Tempo and signature are set in the parameter's `Manual` value and in
  its automation envelope's first event. The signature is stored as (numerator − 1) + 99 ×
  log₂(denominator): 4/4 = 201, 3/4 = 200, 6/8 = 302.

The template is one you name (a User Library or Core Library template, or a path), else your
`User Library/Templates/Default*.als`, else the Core Library's "Vocal Recording" as a base. When
the base is used, its track is replaced by `tracks` (default: two MIDI, two audio) after the set
opens. Live re-creates the bridge on every set load, so opening polls `Song.file_path` and
reconnects. If the open set has unsaved changes, Live asks about them. `create_project` and
`open_project` then return Live's question (`live_is_asking`) instead of waiting, and the user
answers it in Live. `open_project` on the new set afterwards builds the tracks the prompt
interrupted. A new set is not saved for you: Cmd-S in Live saves it in place, with no dialog.

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

