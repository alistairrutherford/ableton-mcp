---
name: ableton-live
description: Use when the user wants to work in Ableton Live 12, through the ableton MCP tools. It covers making or opening a Live project or set, adding, naming, colouring or removing tracks, putting an instrument or effect on a track, and finding instruments, presets, Sounds, drum kits, samples, loops or clips in Live's browser and loading them. It also covers checking that Live is connected.
---

# Working in Ableton Live

The `ableton` MCP tools drive a running Ableton Live 12 through the ClaudeLiveBridge Remote
Script. Everything happens in the user's real Live session, so it is audible and visible to
them. Live's own dialogs are theirs to answer.

## Start with `live_status`

Call `live_status` before anything else in a conversation, and again whenever a tool says it
can't reach the bridge.

- `ok: true`: go ahead. If it also has a `fix` (the installed Remote Script is out of date), pass
  that on to the user.
- `ok: false`: tell the user what its `fix` says, in plain words. The usual cases:
  - The Remote Script isn't installed. Offer to run `install_remote_script`, and run it only once
    they agree, since it writes into their Ableton User Library. Then they restart Live.
  - Live isn't running, or the script isn't selected. The user restarts Live, and once, picks
    **ClaudeLiveBridge** in *Settings → Link, Tempo & MIDI → Control Surface* with Input and Output
    set to *None*. You can't do either for them.

## Projects

- **New project:** `create_project(name, tempo, signature, tracks)`.
  - It makes `<name> Project/<name>.als` in the projects folder and opens it in Live. It never
    overwrites an existing folder.
  - `tracks` takes `add_track` arguments: `{"kind": "midi", "name": "Keys", "instrument": "Drift",
    "color": 5}`. Without `tracks`, the project starts like a new Live set, with 2 MIDI and 2 audio
    tracks.
  - Pass `template` only if the user names one. `list_projects` shows the templates.
  - `signature` is `[numerator, denominator]`, e.g. `[6, 8]`.
- **Open:** `open_project(name)` opens a project by name, folder or `.als` path.
  `list_projects` shows what exists, newest first.
- **What's open:** `get_project_info` returns the file, project folder, tempo, signature, key,
  tracks, and any dialog Live is showing.
- **If Live asks to save the current set** (`live_is_asking` in the result): tell the user Live's
  exact question and let them answer it in Live. Never suggest that discarding is fine. After a
  `create_project`, once they've answered, call `open_project` with the new set's path. It
  finishes building the tracks the prompt interrupted.
- **Saving:** Live's API can't save. After creating or changing a set, ask the user to press
  **Cmd-S** in Live. A project made by `create_project` saves in place, with no dialog.

## Tracks

- Call `list_tracks` before changing tracks, so you address the right ones.
  - Regular tracks are addressed by 0-based index or by name.
  - Return tracks are `"return:0"` (A), `"return:1"` (B), and so on, or their name without Live's
    letter prefix: "Verb" for "C-Verb".
- `add_track(kind="midi"|"audio"|"return", name, index, color, instrument)`:
  - `color` is a palette index 0–69 or `"#rrggbb"`; Live snaps it to its nearest palette colour.
  - `instrument` goes on MIDI tracks only. It takes a `search` result's `id` (best), or a name.
  - If the instrument can't be loaded, the new track is removed again.
- `update_track` renames, colours, mutes, solos, arms, or sets monitoring.
- `delete_track` removes a track with its clips and devices. Say which track you're about to
  delete and get the user's OK first. Live's Undo (Cmd-Z) brings it back.

## Finding and loading sounds

- **Search first:** `search(query, kind, pack)`, then load what it found by `id` with `load_item`.
  Don't guess browser names.
  - `kind`: `instrument` (devices, their presets and Sounds), `sound`, `drum`, `sample`, `clip`,
    `audio_effect`, `midi_effect`, `effect`, `plugin`, `preset`, or `any`.
- **No tags:** Live's browser has no tags. A query matches names, the folders an item sits in
  (Sounds categories such as "Ambient & Evolving"), and the pack. For "a warm pad", try "warm
  pad", then "pad" with `kind="sound"`, and pick from what comes back. Say what you chose and offer
  alternatives, rather than presenting one result as the answer.
- **Results with the same name:** these come from different packs (`pack`). Say which one you
  used.
- **Sample hints:** samples have `hints` from their filename (`bpm`, `key`, `loop`). Use them to
  pick loops that suit the set's tempo and key (`get_project_info`).
- **Audition:** `preview_item(id)` plays an item in Live; `stop_preview` stops it.
- **Where `load_item` puts things:**
  - Instruments, presets and Sounds go on MIDI tracks, and an instrument replaces the track's
    instrument.
  - Effects are added to any track's device chain.
  - A sample becomes a Simpler on a MIDI track, or a clip in the first empty slot on an audio
    track. That brings Session view to the front.
  - If a loop changes the song tempo, it is put back unless `keep_tempo=false`.
- **Live 12 Standard has no Max for Live**, and fewer devices than Suite. Never assume a device
  exists: search for it.
- **Re-indexing:** the first `search` indexes the browser (about 10 s). After the user installs a
  pack or adds samples, call `refresh_index`.

## What you can't do here

These aren't in Live's API, so say so instead of trying:
- save a set, or answer Live's dialogs;
- open a plug-in's window, or route a device's sidechain;
- change Live's audio settings.

There are also tools for transport, recording, routing and device parameters (`start_transport`,
`set_parameter`, `set_routing`, …). `bridge_command` sends any raw bridge command; use it only for
commands without a tool of their own (`list_commands` lists them).
