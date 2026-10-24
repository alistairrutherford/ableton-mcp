# Phase 0 spikes

Run on 2026-10-24 against Live 12.4.7 Standard (macOS, arm64) through ClaudeLiveBridge on port 9873.
The probe commands used here (`lom_dir`, `browser_stats`, `preview_item`, `stop_preview`,
`song_data`, `clip_slots`, and `scene` on `load_device`) are in the "Phase 0 spikes" section of
`RemoteScript/ClaudeLiveBridge/commands.py`.

## 1. Opening a set while Live is running

- `open -a "Ableton Live 12 Standard" <path>.als` asks the running Live to open the set.
- **Over a set with unsaved changes**, Live shows its own prompt straight away. The bridge sees it:
  `Application.open_dialog_count` = 1, `current_dialog_message` = `Save changes to "Untitled"
  before closing?`, and `current_dialog_button_count` = 3. The old set stays loaded until someone
  answers. `Application.press_current_dialog_button(i)` exists, but we haven't tried it, and the
  button order is unknown.
- **Telling which set is open:** `Song.file_path` exists. It is `""` for an unsaved set and the
  path of the `.als` otherwise. `Song.name` exists too. No fingerprint or `Log.txt` parsing is needed.
- `Song.get_data` / `set_data` exist (not needed for detection now).

So `open_project` can open the set, poll `file_path`, and, if a dialog is up, report its message
("Live is asking: Save changes to … — answer it in Live") instead of hanging.

- **Over a clean set**, there is no dialog and the new set is loaded in about 1 s.
- **The bridge restarts on every set load.** The open connection gets `ConnectionResetError`, then
  `ConnectionRefusedError` for about a second, and then the bridge answers again. Live tears down
  and re-creates the control surface, so the client must reconnect and retry. Anything the Remote
  Script keeps on its instance (`_last_session_slot`, `browser_stats` walks, and the Phase 2
  `uri → item` map) is lost. Keep it at module level, or rebuild it after a load.
- The dirty-case dialog was answered in Live by hand. We didn't learn which button index does what.

## 2. A hand-built project folder

- Live ships its default set as
  `/Applications/Ableton Live 12 Standard.app/Contents/App-Resources/Builtin/Templates/DefaultLiveSet.als`.
  It is 13 KB gzipped and 182 KB of XML, created by "Ableton Live 12.1d1", and contains 1-MIDI,
  2-MIDI, 3-Audio, 4-Audio, A-Reverb and B-Delay at 120 BPM in 4/4. This is our template, so we
  don't need to save one by hand.
- The song tempo is `<Tempo><LomId Value="0" /><Manual Value="120" />`.
- The song time signature is `<TimeSignature><LomId Value="0" /><Manual Value="201" />`. The value
  looks like it is encoded as (numerator − 1) + 99 × log₂(denominator), so 4/4 = 201 and
  3/4 = 200. Scenes also carry their own `RemoteableTimeSignature` (Numerator/Denominator).
- **The `Manual` values alone are ignored.** The first test (`Spike Test.als`) patched only
  `Manual` and `EffectiveName`. Live opened it happily but showed 120 BPM, 4/4 and "1-MIDI". The
  master track holds automation envelopes whose initial event wins:
  - `PointeeId 8` (the tempo's `AutomationTarget Id="8"`) holds `<FloatEvent Time="-63072000" Value="120" />`;
  - `PointeeId 10` (the time signature's `AutomationTarget Id="10"`) holds `<EnumEvent Time="-63072000" Value="201" />`.
  
  Track names come from `<UserName>`. Live derives `EffectiveName` from it.
- **Patching `Manual` + the initial envelope event + `UserName` works.** `Spike Two.als` opened at
  93 BPM, in 3/4, with track 1 named "Spike Lead". That confirms the signature encoding
  (3/4 = 200). The pointee ids must be read from the template, not hard-coded.
- **No `Ableton Project Info/` folder is needed to open** a set from a `<Name> Project/` folder.
- **It is needed to save.** Cmd-S on `Spike Two.als` (no marker) showed a 2-button dialog: "The
  Live Set you are about to save is located outside of a Project folder. Live must save the Set
  into a Project folder. Please choose a Project folder to save the Set into." The folder's
  ` Project` name doesn't count.
- In Live 12, the marker is an **empty `Ableton Project Info/` directory**. Compare Chordism's
  Live-made `SimpleProject Project/`, which holds just that directory, the `.als`, and a 0-byte
  `Icon\r` (the Finder folder icon, via `com.apple.FinderInfo`). `create_project` must make the
  directory.

- **Adding the marker after the set is open is not enough.** We created `Ableton Project
  Info/`, cancelled, and pressed Cmd-S again: the same dialog came back. Live decides at open time
  whether a set is in a project, so the marker must exist before `open -a`.

- **Live won't save over a set from an older schema.** Saving `Spike Two` (built from
  `DefaultLiveSet.als`) also raised a 2-button dialog: "The Live Set you are trying to save was
  created with a version of Live that is either older or has fewer features. To avoid
  incompatibilities, please save your Set as a new file."
- Compare the headers:
  - `DefaultLiveSet.als`: `MinorVersion="12.0_12117"`, Creator 12.1d1;
  - Core Library `Templates/8-Track Template.als`: `12.0_12402`, Creator 12.4d1;
  - a set saved by Live 12.4.6: `12.0_12402`.
  
  So **the template must be on the running Live's schema**. Built-in sets from older builds open
  fine but can't be saved in place. Don't just rewrite the header: the content would still be in
  the old schema.
- The tempo's automation target id differs per template (8 in `DefaultLiveSet`, 845 in
  `8-Track Template`). Find it from `<Tempo>…<AutomationTarget Id=…>`; never hard-code it.
- (`Spike Three`'s test never ran: the open was cancelled at Live's "Save changes?" prompt.)

- **Dirty-set dialogs chain together and are easy to get wrong.** When opening Spike Four over a
  dirty Spike Two:
  1. Live asked "Save changes to "Spike Two" before closing?";
  2. answering *Save* raised the version dialog above (Spike Two can't be saved in place);
  3. cancelling that also cancelled the open.
  
  It happened twice. Design rule for `open_project` / `create_project`: if the current set has
  unsaved changes (an open dialog right after `open -a`, or better, a check before it), stop.
  Tell the user, and let them save or discard in Live first. Don't fire `open -a` into a dialog
  chain.

- **The working recipe:** a template already on the running Live's schema, plus an `Ableton Project
  Info/` directory created before opening. `Spike Four` (from `8-Track Template.als`,
  `12.0_12402`, tempo patched to 93) opened. After `set_tempo 96`, **Cmd-S saved in place with no
  dialog**. Live then:
  - rewrote the header to `Creator="Ableton Live 12.4.7"`, `SchemaChangeCount="5"`;
  - kept the marker;
  - added `Backup/Spike Four [2026-10-24 171834].als` (the file as it was before the save).

## Consequences for the plan

1. **Templates.** We can't use the built-in `DefaultLiveSet.als` (old schema). In order of preference:
   - a template you save from Live (File → Save Live Set As Template, in `User Library/Templates/`),
     which is always on the current schema;
   - the newest-schema set in the Core Library `Templates/`.
   
   `create_project` should compare the template's `MinorVersion` with the schema the running Live
   writes, and warn when they differ.
2. **Project folder.** Create `<Name> Project/Ableton Project Info/` (an empty directory)
   **before** `open -a`.
3. **Patching.** Set `Manual` *and* the initial automation event, looking the target id up through
   `<Tempo>` / `<TimeSignature>` → `AutomationTarget Id`. Name tracks with `UserName`.
4. **Opening.** Refuse, or ask first, when the current set has unsaved changes. Detect the switch
   with `Song.file_path`, and reconnect after the bridge restarts on each set load. Report any
   `current_dialog_message` instead of hanging.
5. **Search (Phase 2).**
   - Use a time-sliced index of about 25k nodes, roughly 5 s to build.
   - Rank on category folders, `source`, URI and filename words, since there are no tags.
   - Keep the `uri → item` map at module level, because the control surface is re-created on
     every set load.
6. **Loading (Phase 1).** Fix `load_device_and_wait`: an instrument replaces the existing one, so
   wait for the device list to change, not to grow. A sample goes to Simpler on a MIDI track, or
   to the highlighted clip slot on an audio track. Watch for tempo changes caused by loops.

## 3. Browser size and walk cost

The walk is time-sliced at 50 ms per call (one call per Live tick), the way the Phase 2 indexer
will work:

| root | nodes | loadable | folders | depth | walk ms | wall s | main types |
|---|---|---|---|---|---|---|---|
| instruments | 1777 | 1704 | 72 | 3 | 153 | 0.4 | .adg 1343, .adv 342, 19 devices |
| sounds | 1417 | 1399 | 17 | 2 | 73 | 0.2 | .adg 1059, .adv 340 |
| drums | 4297 | 4278 | 18 | 3 | 251 | 0.6 | .aif 2217, .wav 1777, .adg 281 |
| audio_effects | 818 | 799 | 18 | 3 | 67 | 0.2 | .adv 601, .adg 159, 39 devices |
| midi_effects | 154 | 150 | 3 | 3 | 16 | 0.1 | .adv 116, 15 devices |
| plugins | 36 | 30 | 5 | 3 | 7 | 0.1 | |
| max_for_live | 4 | 0 | 3 | 1 | 25 | 0.1 | (Standard: none) |
| clips | 810 | 809 | 0 | 1 | 56 | 0.2 | .alc 809 |
| samples | 5537 | 5536 | 0 | 1 | 284 | 0.6 | .aif 3316, .wav 2220 (flat) |
| packs | 9851 | 9394 | 449 | 6 | 854 | 1.8 | overlaps the roots above |
| user_library | 84 | 0 | 84 | 3 | 8 | 0.1 | |
| current_project | 1 | 0 | 1 | 0 | 0 | 0.1 | |
| user_folders | 53 | 51 | 2 | 2 | 9 | 0.1 | .wav 50 |

- Everything together is about 25k nodes and about 1.8 s of main-thread time. Time-sliced, it
  takes about 5 s of wall time. CPU stayed at about 0.6 % throughout.
- The old one-shot `browser_search` on `packs` blocked one tick for about 0.9 s. The time-sliced
  indexer is needed, but the index is small enough to fetch whole and cache.

## 4. Tags

- `BrowserItem` has only `children`, `iter_children`, `is_device`, `is_folder`, `is_loadable`,
  `is_selected`, `name`, `source` (e.g. "Core Library"), and `uri` (e.g.
  `query:Sounds#Ambient%20&%20Evolving:FileId_34942`). **There are no tags.**
- `Browser` also has `colors` (the user's colour labels, as a `BrowserItemVector`),
  `legacy_libraries`, `filter_type`, `hotswap_target`, `preview_item` and `stop_preview`.
- What we can rank on instead:
  - the category folders (Sounds: "Ambient & Evolving" etc.; Instruments and Drums by device);
  - the category inside the URI;
  - `source`, which gives the pack;
  - colour-label membership;
  - filename words such as "86bpm", "Kick" and "Loop".

## 5. Loading samples, and preview

- **Sample → MIDI track:** a new **Simpler** (`OriginalSimpler`) with the sample.
- **Sample → audio track:** with `song.view.highlighted_clip_slot` set first, an **audio clip in
  that slot**. Its `file_path` is the real file, e.g. `…/Factory Packs/Chop and Swing/Samples/…/Acoustic
  Kick Loop 86bpm.aif`. Loading the 86 bpm loop also changed the song tempo from 120 to 86.
  Phase 2 should check whether that is the "auto-adjust tempo" behaviour and say so, or restore
  the tempo.
- **`browser.preview_item` / `stop_preview`:** both run without errors. (Not checked by ear.)
- **Bug found:** loading an instrument preset (Attack Pad → Tension) onto a MIDI track that already
  has an instrument **replaces** it, so the device count stays the same.
  `LiveClient.load_device_and_wait` waits for the count to grow and times out. Phase 1's
  `add_track(instrument=…)` and `load_item` must instead wait for the device list to *change*.
- Live 12 Standard loaded and ran a Tension-based preset (Attack Pad, class `StringStudio`).
