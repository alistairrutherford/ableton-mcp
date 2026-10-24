# Ableton MCP: project plan

A Claude plugin (an MCP server, plus a skill) that lets Claude work in Ableton Live 12: create
projects, add tracks, and find and load instruments and samples. It is built on **LiveBridge**,
the component already used by Chordism and NoteCap.

Status (2026-10-24): Phase 0 is done. LiveBridge is vendored as ClaudeLiveBridge on port 9873, the uv
project is set up, 16 offline tests pass, and all five spikes are answered (`docs/spikes.md`).

## 1. Goals

| # | Goal | "Done" means |
|---|---|---|
| G1 | **Create projects** | "Make a new project called *Night Drive* at 92 BPM in 4/4" gives a `Night Drive Project/Night Drive.als` on disk, opened in Live, with that tempo and signature. Later saves go to the same file with no dialog. |
| G2 | **Add tracks** | Claude can add MIDI, audio and return tracks with a name, position and colour, and optionally put an instrument on them in the same step. It can list, rename, duplicate and delete tracks too. |
| G3 | **Search instruments and samples** | "Find a warm analog pad" or "find kick samples" returns ranked results from Live's browser (instruments, sounds, drums, plug-ins, samples, packs, User Library), each with a stable id that Claude can load onto a track. |

Out of scope for v1: writing MIDI or arrangement content, mixing and automation, and editing
plug-in GUIs. LiveBridge already reaches some of these, so they make sense as later phases (see §8).

## 2. What LiveBridge gives us

There are two copies:

- `../audio/notecap/LiveBridge`, the newer one and a superset (it adds `create_audio_track`,
  `duplicate_track` and `als.py`).
- `../audio/chordism/LiveBridge`.

We start from the **NoteCap** copy.

```
Claude ──stdio MCP──▶ livebridge/mcp_server.py ──TCP 127.0.0.1:9872, JSON lines──▶ NoteCapBridge Remote Script
                       livebridge/client.py                                          (inside Live, polled from
                                                                                     update_display ≈ every 100 ms)
```

Parts we can reuse as they are:

- **The Remote Script** (`RemoteScript/NoteCapBridge/`):
  - `server.py` is a non-blocking socket server with no Live imports, and it already has offline tests.
  - `commands.py` holds about 35 LOM commands: tracks (create MIDI/audio, duplicate, delete,
    set, arm), devices and parameters, routing, recording, clip notes, meters, presets and CPU.
  - Tracks, devices and parameters can be addressed by index or by name, with exact-then-substring
    matching.
  - It re-reads the song through a callable on every call, so it keeps working after a different
    set is opened.
- **Browser search and load** (`browser_search`, `load_device`): a depth-first walk of
  `application().browser.<category>`, with plug-in format detection (VST3 > AU > VST), then
  `browser.load_item` onto the selected track.
- **`client.py`**: a persistent connection with `wait_until` and `load_device_and_wait`.
  Async loads are handled by polling.
- **`als.py`**: reads and writes a gzipped `.als` as XML and patches what the LOM can't reach.
  This is the basis for creating projects.
- **`livelog.py`** (reads Live's `Log.txt`), `scripts/restart_live.sh` (opens a set and waits for
  the bridge), and `install_remote_script.sh`.
- **`mcp_server.py`**: works with both the 1.x and 2.x `mcp` SDK, and turns `ToolError` into a
  helpful "is Live running?" message.
- **Tests**: `tests/test_offline.py` (a fake LOM, run in CI) and `tests/rig.py` (live-test set-up).

Gaps for our goals:

| Need | Gap |
|---|---|
| G1 projects | The LOM has **no way to create, open or save a set** (the README checked `_MxDCore/LomTypes`). This has to be done from outside Live. |
| G2 tracks | Return tracks, track colour and "track + instrument in one call" are missing. Grouping tracks isn't in the LOM. |
| G3 search | The `samples`, `clips`, `user_folders` and `current_project` browser roots aren't in `BROWSER_CATEGORIES`. Every search re-walks up to 50 000 nodes **in a single Live tick**, which will freeze Live's UI on large trees such as samples and packs. Matching is a plain substring with no ranking. Results come back by name only, so loading something re-searches by name, which is ambiguous. |
| Coexistence | Port 9872 and the name `NoteCapBridge` both belong to NoteCap. The well-known open-source `ableton-mcp` uses `AbletonMCP` on port 9877, so we avoid that name and port as well. |

The installed version is **Live 12 Standard**, with logs up to 12.4.7. Standard has no Max for
Live and less bundled content than Suite. Search must report what is installed and never assume
Suite devices exist.

## 3. Architecture

```
┌────────────── Claude (Code / Desktop) ──────────────┐
│  skill: ableton-live (workflow + conventions)        │
└──────────────┬──────────────────────────────────────┘
               │ stdio MCP
┌──────────────▼──────────────────────────────────────┐
│ ableton_mcp server (Python 3.11+, uv)                │
│  tools/projects.py  — template copy, .als patch,     │
│                       open in Live, save (optional)  │
│  tools/tracks.py    — thin wrappers + composites     │
│  tools/browser.py   — search over a cached index     │
│  index.py           — fetch, cache on disk, rank     │
│  livebridge/        — client, als, livelog (vendored)│
└──────────────┬───────────────────────┬──────────────┘
   TCP 127.0.0.1:9873, JSON lines      │ filesystem + `open -a` (+ optional osascript)
┌──────────────▼──────────────┐   ┌────▼──────────────────────┐
│ ClaudeLiveBridge Remote      │   │ <Projects root>/          │
│ Script (inside Live)         │   │   <Name> Project/<Name>.als│
│  server.py  (unchanged)      │   └───────────────────────────┘
│  commands.py (+ new commands)│
│  indexer.py (time-sliced     │
│   browser walk, uri→item map)│
└──────────────────────────────┘
```

Key decisions:

1. **Vendor LiveBridge and rename it** to `ClaudeLiveBridge` on port **9873**, so it can run
   alongside Chordism's bridge (9871) and NoteCap's (9872) in one Live session. The `livebridge`
   Python package keeps its name, so diffs against the other copies stay readable.
2. **Keep logic out of Live's Python.** The Remote Script only walks the browser and resolves ids.
   Ranking, caching and project files live in the MCP server, which is easier to test and can't
   stall Live.
3. **Index the browser across ticks.** A new `indexer.py` walks the browser as a generator with a
   time budget of about 8 ms per `update_display` tick. It keeps a `uri → BrowserItem` map so items
   can be loaded by id. The MCP server fetches the flat index once (paged), caches it under
   `~/Library/Caches/ableton-mcp/`, keyed by Live version and the browser's top-level contents, and
   searches it locally.
4. **Create projects from a template file.** We create `<root>/<Name> Project/Ableton Project
   Info/` first, copy a template `.als` that is already on the running Live's schema into
   `<root>/<Name> Project/<Name>.als`, patch tempo, signature and initial tracks with `als.py`, and
   open it with `open -a "<Live app>" <path>`. Then a later Cmd-S saves in place with no dialog
   (checked in the Phase 0 spikes; see `docs/spikes.md` for why each of these is needed).
5. **Saving is opt-in UI automation.** The LOM can't save, so `save_project` sends Cmd-S to Live
   through `osascript` / System Events. It needs macOS Accessibility permission for the host app.
   It's off by default; when it's off, the tool tells the user to press Cmd-S.
6. **Fewer, higher-level tools.** Raw LiveBridge has about 35 commands. Claude gets around 15
   task-shaped tools plus a `bridge_command` escape hatch. Destructive tools (`delete_track`, and
   `open_project` over a dirty set) are annotated `destructiveHint` and say what they will discard.

## 4. MCP tool surface (v1)

| Area | Tool | Notes |
|---|---|---|
| Status | `live_status` | Ping, Live version, bridge port, index state, current tempo and track count. If something is wrong it says what to fix, such as installing the Remote Script or selecting the control surface. |
| Projects | `create_project(name, tempo?, signature?, template?, location?, tracks?, open=true)` | `tracks` takes the same specs as `add_track`, pre-built in the `.als` so the set opens ready. |
| | `open_project(path)` | Warns that Live will show its own save prompt if the current set has unsaved changes, then waits for the bridge to see the new set. |
| | `list_projects(location?)` | Scans the projects root for `* Project/*.als`, with modified time. |
| | `save_project()` | Opt-in (decision 5). |
| | `get_project_info` | Tempo, signature, tracks, scenes and the set's path if known. |
| Tracks | `add_track(kind=midi\|audio\|return, name?, index?, color?, instrument?)` | `instrument` is a search result id or a name. It loads and waits for the device. |
| | `list_tracks` | Returns tracks only, unlike `get_state`, which also returns transport fields. |
| | `update_track(track, name?, color?, mute?, solo?, arm?)` | |
| | `duplicate_track(track)`, `delete_track(track)` | |
| Search | `search(query, kind=any\|instrument\|sound\|drum\|sample\|audio_effect\|midi_effect\|plugin\|clip, limit=20)` | Returns ranked `{id, name, kind, path, format, pack}`. |
| | `load_item(id, track, slot?)` | Devices and presets go onto the track. A sample goes into a Simpler on a MIDI track, or into a clip slot on an audio track. |
| | `preview_item(id)` / `stop_preview` | Uses Live's browser preview to audition a sample. |
| | `refresh_index` | |
| Escape hatch | `bridge_command(cmd, args)` | Gives access to every raw LiveBridge command (routing, recording, parameters, …). |

## 5. Phases

Sizes: S ≈ ½ day, M ≈ 1–2 days, L ≈ 3–5 days.

### Phase 0: Bootstrap and spikes (M)

- Set up the repo (`pyproject.toml`, `src/ableton_mcp/`, uv) and `git init`.
- Vendor NoteCap's LiveBridge into this repo, then rename it to `ClaudeLiveBridge` on port 9873.
  Make the port and name a single constant in each of the script, the client and the installer.
- Drop the NoteCap-specific pieces: the NoteCap tests and `measure_note_timing`. `timing.py` stays
  for later use.
- Port `test_offline.py` and make it pass with no Live running.
- **Spikes**, on Live 12.4.7 Standard, written up in `docs/spikes.md`:
  1. Open a set while Live is running with `open -a`. Find out what happens to a clean set and to
     a dirty one, and how we detect that the new set is loaded. Candidates are a template
     fingerprint, a `Song.get_data` token, or the document-load line in `Log.txt`.
  2. Find out whether Live accepts a hand-built `<Name> Project/<Name>.als`. Does it need an
     `Ableton Project Info/` folder? What happens to Cmd-S on it?
  3. Measure browser size: node counts per root (especially `samples` and `packs`), and the cost
     of one full walk.
  4. Check whether the LOM exposes Live 12 browser tags or labels (for "warm pad" style queries).
  5. Check `browser.load_item` on a sample, with a MIDI track selected and with an audio track or
     clip slot highlighted. Also check `browser.preview_item`.

Exit: Claude Code shows the server, `live_status` answers from Live, and the spikes are answered.

### Phase 1: Tracks (S–M) → G2

- Remote Script: add `create_return_track`, track `color` / `color_index` (on `get_track` and
  `set_track`), and return tracks in `get_state`.
- MCP: `add_track` (including the `instrument` composite, which uses `load_device_and_wait`),
  `list_tracks`, `update_track`, `duplicate_track` and `delete_track`.
- Tests: offline (fake LOM) and one live test in a scratch set. The live test adds a MIDI track
  with Drift, an audio track and a return track, checks their names and colours, then cleans up.

### Phase 2: Search and load (L) → G3

- Remote Script `indexer.py`: a time-sliced walk over every browser root, including `samples`,
  `clips`, `user_folders` and `current_project`. It records `{uri, name, path, root, is_device,
  is_folder, is_loadable}` and the `uri → item` map. It adds commands `index_start`,
  `index_status` and `index_page(offset, limit)`, plus `load_uri(uri, track, slot?)`,
  `preview_uri` and `stop_preview`.
- The existing `browser_search` and `load_device` keep working, backed by the index when it's ready.
- MCP `index.py`:
  - fetches the index in pages and caches it to disk;
  - rebuilds when the Live version or the browser's top level changes;
  - ranks results: exact, then prefix, then token match, then fuzzy (`rapidfuzz`), with a boost by
    kind;
  - maps natural kinds (`instrument`, `sample`, …) onto browser roots;
  - infers the pack from the path.
- Sample extras: duration and sample rate from the file on disk where the URI resolves to one,
  plus filename hints (BPM, key, "one shot" / "loop").
- Tests:
  - offline: ranking tests on a recorded index fixture, and indexer tests that slice a fake tree
    across ticks;
  - live: search for "Drift" returns it first; searching "kick" under samples gives results that
    load onto an audio track and onto a MIDI track (as Simpler); a full index build doesn't drop
    Live's UI tick (watch `get_cpu` and the tick gaps).

### Phase 3: Projects (M–L) → G1

- Template, from the spikes: prefer the user's `User Library/Templates/*.als` (saved by their Live,
  so always on the current schema), else the newest-schema set in the Core Library `Templates/`.
  Warn when the template's `MinorVersion` is older than the running Live's, because Live won't save
  over it in place.
- Extend `als.py` with `set_tempo`, `set_signature` and `add_tracks` (cloning track XML from a
  template track and fixing `Id`s), so `create_project` can pre-build tracks.
- MCP `projects.py`: `create_project`, `open_project`, `list_projects`, `get_project_info`, and
  `save_project` behind a config flag. The projects root is configurable and defaults to
  `~/Music/Ableton/Projects`.
- Make opening safe. Refuse to overwrite an existing project folder. Before opening over a set
  that may have unsaved changes, tell the user that Live will prompt. Wait up to N seconds for the
  bridge to see the new set, then report "waiting on Live's save dialog" rather than failing.
- Tests: offline `.als` round trips on the template (patch, re-read, check the XML). Live:
  create → open → check tempo and tracks through the bridge → save (if enabled) → reopen → check
  again.

### Phase 4: Packaging as a Claude plugin (S–M)

- Claude Code plugin layout:
  - `.claude-plugin/plugin.json`;
  - `.mcp.json` running `uv run --directory ${CLAUDE_PLUGIN_ROOT} python -m ableton_mcp`;
  - `skills/ableton-live/SKILL.md`, which covers: check `live_status` first; search before
    loading; use ids, not names; never delete or open over a set without saying so; and how to
    handle Live's dialogs.
- Install helper: `ableton-mcp install-remote-script`, the existing symlink/copy script, plus a
  one-time manual step to select the control surface in Live's settings. The plugin can't make
  that choice itself because Live keeps it in its binary preferences.
- Claude Desktop: document the `claude_desktop_config.json` entry, and consider an `.mcpb` bundle.
- README: setup, the manual steps, troubleshooting (port busy, the script not listed, more than
  64 plug-in parameters needing `-_PluginAutoPopulateThreshold`).

### Phase 5: Hardening (S–M)

- Timeouts and reconnects across a Live restart. Clear errors for "Live not running", "bridge not
  selected" and "index building (n %)".
- A tick-budget test in CI against the fake LOM.
- Logging to `~/Library/Logs/ableton-mcp.log`.

## 6. Testing strategy

- **Offline (CI, no Live):**
  - the socket protocol and commands against the fake LOM (inherited);
  - indexer slicing;
  - search ranking on a fixture;
  - `.als` patch round trips;
  - MCP tool schemas.
- **Live:** `tests/live/`, skipped when the bridge doesn't answer. Tests run in a scratch set, reuse
  their tracks, and name them with an `MCP ` prefix. They follow LiveBridge's pattern of saving a
  JSON report in `reports/` (gitignored).
- **Manual acceptance script:** a short checklist of prompts to try in Claude for each of G1–G3.

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| The LOM can't create, open or save sets | G1 needs work outside Live | Template plus `open -a` (spike 1/2); opt-in Cmd-S; a clear manual fallback |
| A hand-made `.als` is rejected or upgraded oddly by Live | Broken projects | Use a template saved by the same Live version; patch minimally; round-trip tests |
| A browser walk freezes Live | Bad UX, audio dropouts | Time-sliced indexer and a cached index; never walk on demand |
| Browser tags aren't in the LOM | "Warm pad" queries match names only | Synonyms and kind boosts in ranking; filename hints; revisit if Live adds tags |
| Live's own dialogs (save changes, missing files) block automation | Tools hang | Poll with timeouts and report "waiting on Live"; never click dialogs |
| Three diverging LiveBridge copies | Fixes made in one copy don't reach the others | Make this repo's bridge the generic upstream later (§8) |
| Standard edition content | Suite-only devices missing | Search reports what's installed; the skill says not to assume devices |

## 8. Later (post-v1)

- MIDI: write notes into clips (`create_clip`, `set_notes`), and scenes.
- Mixing: volume, pan, sends, device parameters by name (already in the bridge), and simple
  automation.
- Arrangement: place audio clips from samples (`Track.create_audio_clip` in Live 12), and locators.
- Extract a generic LiveBridge package that Chordism and NoteCap depend on, with a plugin-specific
  command module per project.

## 9. Open questions

1. Where do projects live? The default proposed here is `~/Music/Ableton/Projects`.
2. Is opt-in UI automation for **save** acceptable? It needs Accessibility permission. The
   alternative is that Claude always asks you to press Cmd-S.
3. Is the target **Claude Code** (plugin) only, or Claude Desktop too?
4. Should this repo become the canonical LiveBridge now, or stay a fork until v1 ships?
