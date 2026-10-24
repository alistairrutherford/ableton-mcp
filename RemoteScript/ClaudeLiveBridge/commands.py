"""Bridge commands against the Live Object Model.

Every handler runs on Live's main thread (called from the control surface's
update tick through BridgeServer.poll). Handlers take keyword arguments from
the request's "args" and return plain JSON data.

Tracks are addressed by 0-based index, by name, as "master", or as "return:N"
(0-based, so "return:0" is the A return). Names are looked up among the regular
tracks first, then the return tracks, whose names Live prefixes with their letter
("C-Verb"): "Verb" matches it exactly. Devices and parameters are addressed by
0-based index or by name (exact match first, ignoring case, then a unique
substring match).
"""

import re
import time

MONITORING ={"in": 0, "auto": 1, "off": 2}
MONITORING_NAMES = {v: k for k, v in MONITORING.items()}

COLOR_INDEX_COUNT = 70   # Live 12's track colour palette: color_index 0..69

# Live names a return track with its letter: "MCP Verb" reads back as "C-MCP Verb".
RETURN_LETTER = re.compile(r"^[A-Z]-")

BROWSER_CATEGORIES = ("plugins", "instruments", "audio_effects", "midi_effects",
                      "drums", "sounds", "max_for_live", "user_library", "packs",
                      "samples", "clips", "current_project")

# Folder names Live uses for plug-in formats in the browser, and URI fragments.
FORMAT_ALIASES = {
    "vst3": ("VST3",),
    "au": ("AUv2", "AUv3", "Audio Units", "AU", "AudioUnit"),
    "audiounit": ("AUv2", "AUv3", "Audio Units", "AU", "AudioUnit"),
    "vst": ("VST", "VST2"),
    "vst2": ("VST", "VST2"),
}
FORMAT_PREFERENCE = ("vst3", "au", "vst")

BROWSER_MAX_NODES = 50000
BROWSER_MAX_DEPTH = 10


class CommandError(Exception):
    pass


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _match(items, key, name_of, what):
    """Find one item by int index or by name (exact, ignoring case, then unique substring)."""
    items = list(items)
    if isinstance(key, bool):
        raise CommandError("%s must be an index or a name" % what)
    if isinstance(key, int):
        if 0 <= key < len(items):
            return items[key]
        raise CommandError("%s index %d out of range (0..%d)" % (what, key, len(items) - 1))
    if not isinstance(key, str):
        raise CommandError("%s must be an index or a name" % what)
    want = key.strip().lower()
    names = [name_of(i) for i in items]
    exact = [i for i, n in zip(items, names) if n.lower() == want]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact[0]  # duplicates: first one, as Live's own lists do
    partial = [i for i, n in zip(items, names) if want in n.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise CommandError("no %s named %r; have %s" % (what, key, names))
    raise CommandError("%s %r is ambiguous: %s" % (what, key, [name_of(i) for i in partial]))


class Commands:
    def __init__(self, song, application, log=None):
        self._song = song                # callables, so a reloaded set is always current
        self._app = application
        self._log = log or (lambda msg: None)
        self._last_session_slot = None   # (track, slot index) of the last session recording
        self._stats = {}                 # category -> resumable browser_stats walk

    # -- dispatch ----------------------------------------------------------

    def names(self):
        return sorted(n[4:] for n in dir(self) if n.startswith("cmd_"))

    def dispatch(self, cmd, args):
        fn = getattr(self, "cmd_" + str(cmd), None)
        if fn is None:
            raise CommandError("unknown command %r; commands: %s" % (cmd, self.names()))
        return fn(**args)

    # -- lookups -----------------------------------------------------------

    def _tracks(self):
        return list(self._song().tracks)

    def _returns(self):
        return list(self._song().return_tracks)

    def _track(self, key):
        if isinstance(key, str):
            k = key.strip().lower()
            if k == "master":
                return self._song().master_track
            if k.startswith("return:"):
                try:
                    i = int(k[len("return:"):])
                except ValueError:
                    raise CommandError("return tracks are addressed as return:N, got %r" % key)
                return _match(self._returns(), i, lambda t: t.name, "return track")
            try:
                return _match(self._tracks(), key, lambda t: t.name, "track")
            except CommandError as e:
                if not str(e).startswith("no track named"):
                    raise
                want = RETURN_LETTER.sub("", key.strip()).lower()
                bare = [t for t in self._returns() if RETURN_LETTER.sub("", t.name).lower() == want]
                if bare:
                    return bare[0]
                try:
                    return _match(self._returns(), key, lambda t: t.name, "return track")
                except CommandError as e2:
                    if not str(e2).startswith("no return track named"):
                        raise
                raise CommandError("no track named %r; have %s, returns %s" % (
                    key, [t.name for t in self._tracks()], [t.name for t in self._returns()]))
        return _match(self._tracks(), key, lambda t: t.name, "track")

    def _track_index(self, track):
        for i, t in enumerate(self._tracks()):
            if t == track:
                return i
        for i, t in enumerate(self._returns()):
            if t == track:
                return "return:%d" % i
        return "master" if track == self._song().master_track else None

    def _device(self, track, key):
        return _match(track.devices, key, lambda d: d.name, "device")

    def _param(self, device, key):
        return _match(device.parameters, key, lambda p: p.name, "parameter")

    # -- descriptions ------------------------------------------------------

    def _routing_info(self, track, direction):
        t = getattr(track, direction + "_routing_type", None)
        ch = getattr(track, direction + "_routing_channel", None)
        return {
            "type": _safe(lambda: t.display_name),
            "channel": _safe(lambda: ch.display_name),
        }

    @staticmethod
    def _kind(track, index):
        if index == "master":
            return "master"
        if isinstance(index, str) and index.startswith("return:"):
            return "return"
        if _safe(lambda: bool(track.is_foldable), False):
            return "group"
        return "midi" if _safe(lambda: bool(track.has_midi_input), False) else "audio"

    def _track_info(self, track, index, include_devices=True):
        info = {
            "index": index,
            "name": track.name,
            "kind": self._kind(track, index),
            "color": _safe(lambda: "#%06x" % (int(track.color) & 0xFFFFFF)),
            "color_index": _safe(lambda: int(track.color_index)),
            "is_midi": _safe(lambda: bool(track.has_midi_input)),
            "can_be_armed": _safe(lambda: bool(track.can_be_armed), False),
            "arm": _safe(lambda: bool(track.arm), False),
            "monitoring": _safe(lambda: MONITORING_NAMES.get(int(track.current_monitoring_state))),
            "mute": _safe(lambda: bool(track.mute)),
            "solo": _safe(lambda: bool(track.solo)),
            "input_routing": self._routing_info(track, "input"),
            "output_routing": self._routing_info(track, "output"),
            "playing_slot_index": _safe(lambda: track.playing_slot_index),
            "arrangement_clips": _safe(lambda: len(track.arrangement_clips)),
        }
        if include_devices:
            info["devices"] = [self._device_info(d, i) for i, d in enumerate(track.devices)]
        return info

    def _device_info(self, d, index):
        return {
            "index": index,
            "id": _safe(lambda: int(d._live_ptr)),   # stable while the device exists, where Live has it
            "name": d.name,
            "class_name": _safe(lambda: d.class_name),
            "class_display_name": _safe(lambda: d.class_display_name),
            "type": _safe(lambda: int(d.type)),
            "is_active": _safe(lambda: bool(d.is_active)),
            "parameter_count": _safe(lambda: len(d.parameters), 0),
        }

    def _param_info(self, p, index):
        info = {
            "index": index,
            "name": p.name,
            "value": p.value,
            "min": p.min,
            "max": p.max,
            "is_quantized": bool(p.is_quantized),
            "is_enabled": _safe(lambda: bool(p.is_enabled), True),
            "display": _safe(lambda: p.str_for_value(p.value)),
        }
        if p.is_quantized:
            info["value_items"] = _safe(lambda: [str(v) for v in p.value_items], [])
        return info

    # -- song and transport ------------------------------------------------

    def cmd_ping(self):
        app = self._app()
        return {
            "pong": True,
            "live_version": "%s.%s.%s" % (
                _safe(app.get_major_version), _safe(app.get_minor_version), _safe(app.get_bugfix_version)),
        }

    def cmd_list_commands(self):
        return self.names()

    def cmd_get_state(self, include_devices=True):
        s = self._song()
        return {
            "tempo": s.tempo,
            "signature": [s.signature_numerator, s.signature_denominator],
            "is_playing": bool(s.is_playing),
            "current_song_time": s.current_song_time,
            "record_mode": bool(s.record_mode),
            "session_record": bool(s.session_record),
            "is_counting_in": _safe(lambda: bool(s.is_counting_in)),
            "arrangement_overdub": _safe(lambda: bool(s.arrangement_overdub)),
            "loop": _safe(lambda: bool(s.loop)),
            "metronome": _safe(lambda: bool(s.metronome)),
            "midi_recording_quantization": _safe(lambda: str(s.midi_recording_quantization)),
            "tracks": [self._track_info(t, i, include_devices) for i, t in enumerate(self._tracks())],
            "return_tracks": [self._track_info(t, "return:%d" % i, include_devices)
                              for i, t in enumerate(self._returns())],
            "master": {
                "output_meter_level": _safe(lambda: s.master_track.output_meter_level),
            },
            "selected_track": _safe(lambda: self._track_index(s.view.selected_track)),
        }

    def cmd_set_tempo(self, bpm):
        bpm = float(bpm)
        if not 20.0 <= bpm <= 999.0:
            raise CommandError("tempo must be 20..999 BPM")
        self._song().tempo = bpm
        return {"tempo": self._song().tempo}

    def cmd_start_transport(self, from_beat=None, resume=False):
        s = self._song()
        if from_beat is not None:
            s.current_song_time = float(from_beat)
        if resume:
            s.continue_playing()
        else:
            s.start_playing()
        return {"is_playing": bool(s.is_playing), "current_song_time": s.current_song_time}

    def cmd_stop_transport(self):
        s = self._song()
        s.stop_playing()
        return {"is_playing": bool(s.is_playing), "current_song_time": s.current_song_time}

    def cmd_set_song_properties(self, **props):
        """Set any of: current_song_time, loop, loop_start, loop_length (beats), metronome,
        arrangement_overdub, record_mode, session_record, signature_numerator,
        signature_denominator, back_to_arranger, midi_recording_quantization ("none" or a
        Live.Song.RecordingQuantization name)."""
        s = self._song()
        allowed = ("current_song_time", "loop", "loop_start", "loop_length", "metronome", "arrangement_overdub",
                   "record_mode", "session_record", "signature_numerator", "signature_denominator",
                   "back_to_arranger", "midi_recording_quantization")
        for key, value in props.items():
            if key not in allowed:
                raise CommandError("cannot set %r; settable: %s" % (key, allowed))
            if key == "midi_recording_quantization":
                import Live
                RQ = Live.Song.RecordingQuantization
                name = "rec_q_no_q" if value in (None, "none", "off", 0) else str(value)
                if not hasattr(RQ, name):
                    raise CommandError("unknown recording quantization %r" % value)
                value = getattr(RQ, name)
            elif key in ("current_song_time", "loop_start", "loop_length"):
                value = float(value)
            elif key in ("signature_numerator", "signature_denominator"):
                value = int(value)
            else:
                value = bool(value)
            setattr(s, key, value)
        return {k: _safe(lambda k=k: str(getattr(s, k)) if k == "midi_recording_quantization"
                         else getattr(s, k)) for k in props}

    # -- tracks ------------------------------------------------------------

    def _create_track(self, create, index, name):
        s = self._song()
        before = len(s.tracks)
        index = int(index)
        create(index)
        new_index = before if index < 0 or index > before else index
        track = s.tracks[new_index]
        if name:
            track.name = str(name)
        return self._track_info(track, new_index)

    def cmd_create_midi_track(self, index=-1, name=None):
        return self._create_track(self._song().create_midi_track, index, name)

    def cmd_create_audio_track(self, index=-1, name=None):
        return self._create_track(self._song().create_audio_track, index, name)

    def cmd_create_return_track(self, name=None):
        """Add a return track. Live always appends it after the existing returns."""
        s = self._song()
        s.create_return_track()
        i = len(s.return_tracks) - 1
        track = s.return_tracks[i]
        if name:
            track.name = str(name)
        return self._track_info(track, "return:%d" % i)

    def cmd_duplicate_track(self, track):
        """Duplicate a track with its devices. Live saves each plug-in's state and loads it into the
        copy, so this is a state round trip without saving the set."""
        t = self._track(track)
        i = self._track_index(t)
        if not isinstance(i, int):
            raise CommandError("cannot duplicate the %s track (Live has no command for it)" % (
                "master" if i == "master" else "return"))
        self._song().duplicate_track(i)
        return self._track_info(self._song().tracks[i + 1], i + 1)

    def cmd_delete_track(self, track):
        t = self._track(track)
        i = self._track_index(t)
        s = self._song()
        if i == "master":
            raise CommandError("cannot delete the master track")
        name = t.name
        if isinstance(i, str):
            s.delete_return_track(int(i[len("return:"):]))
        else:
            s.delete_track(i)
        return {"deleted": i, "name": name, "track_count": len(s.tracks),
                "return_track_count": len(s.return_tracks)}

    @staticmethod
    def _set_color(t, color):
        """color: a palette index 0..69 (color_index), or "#rrggbb" (Live snaps it to the palette)."""
        if isinstance(color, bool):
            raise CommandError("color must be a palette index or \"#rrggbb\"")
        if isinstance(color, int):
            if not 0 <= color < COLOR_INDEX_COUNT:
                raise CommandError("color index must be 0..%d" % (COLOR_INDEX_COUNT - 1))
            t.color_index = color
            return
        text = str(color).strip().lstrip("#")
        if not re.match(r"^[0-9a-fA-F]{6}$", text):
            raise CommandError("color must be a palette index or \"#rrggbb\", got %r" % (color,))
        t.color = int(text, 16)

    def cmd_set_track(self, track, name=None, arm=None, monitoring=None, mute=None, solo=None,
                      color=None):
        t = self._track(track)
        if name is not None:
            t.name = str(name)
        if color is not None:
            self._set_color(t, color)
        if arm is not None:
            t.arm = bool(arm)
        if monitoring is not None:
            if monitoring not in MONITORING:
                raise CommandError("monitoring must be one of %s" % list(MONITORING))
            t.current_monitoring_state = MONITORING[monitoring]
        if mute is not None:
            t.mute = bool(mute)
        if solo is not None:
            t.solo = bool(solo)
        return self._track_info(t, self._track_index(t), include_devices=False)

    def cmd_arm_track(self, track, arm=True):
        t = self._track(track)
        if not t.can_be_armed:
            raise CommandError("track %r cannot be armed" % t.name)
        t.arm = bool(arm)
        return {"track": self._track_index(t), "arm": bool(t.arm)}

    def cmd_get_track(self, track):
        t = self._track(track)
        return self._track_info(t, self._track_index(t))

    # -- browser -----------------------------------------------------------

    def _browser_root(self, category):
        if category not in BROWSER_CATEGORIES:
            raise CommandError("category must be one of %s" % (BROWSER_CATEGORIES,))
        return getattr(self._app().browser, category)

    def _walk(self, root):
        """Yield (item, path) for every loadable item under root, depth first."""
        stack = [(root, [])]
        seen = 0
        while stack:
            item, path = stack.pop()
            seen += 1
            if seen > BROWSER_MAX_NODES:
                self._log("browser walk stopped at %d nodes" % BROWSER_MAX_NODES)
                return
            if path and item.is_loadable:
                yield item, path
            if len(path) < BROWSER_MAX_DEPTH:
                children = _safe(lambda: list(item.children), [])
                for child in reversed(children):
                    stack.append((child, path + [child.name]))

    @staticmethod
    def _format_of(path, uri):
        """Plug-in format from the browser folders above the item, else from its URI tokens."""
        folders = [p.lower() for p in path[:-1]]
        tokens = set(re.split(r"[^a-z0-9]+", (uri or "").lower()))
        for fmt in FORMAT_PREFERENCE:
            aliases = [a.lower() for a in FORMAT_ALIASES[fmt]]
            if any(f in aliases for f in folders):
                return fmt
        for fmt in FORMAT_PREFERENCE:
            aliases = [re.sub(r"[^a-z0-9]", "", a.lower()) for a in FORMAT_ALIASES[fmt]]
            if tokens & set(aliases):
                return fmt
        return None

    def _search(self, query, category, fmt=None):
        want = query.strip().lower()
        hits = []
        for item, path in self._walk(self._browser_root(category)):
            name = item.name
            lname = name.lower()
            # Live sometimes shows file extensions for presets; compare without them too.
            stem = lname.rsplit(".", 1)[0] if "." in lname else lname
            if want not in lname:
                continue
            uri = _safe(lambda: item.uri, "")
            hit = {
                "name": name, "path": path, "uri": uri,
                "format": self._format_of(path, uri) if category == "plugins" else None,
                "exact": lname == want or stem == want,
                "is_device": _safe(lambda: bool(item.is_device), False),
                "_item": item,
            }
            if fmt and hit["format"] != fmt:
                continue
            hits.append(hit)
        return hits

    @staticmethod
    def _public(hit):
        return {k: v for k, v in hit.items() if not k.startswith("_")}

    def cmd_browser_search(self, query, category="plugins", format=None, limit=50):
        fmt = format.lower() if format else None
        hits = self._search(query, category, fmt)
        return [self._public(h) for h in hits[: int(limit)]]

    def cmd_load_device(self, name, track, category="plugins", format=None, scene=None):
        """Load a browser item onto a track (appended after its devices). With `scene`, that clip
        slot is highlighted first, which is where Live puts a sample loaded onto an audio track."""
        fmt = format.lower() if format else None
        if fmt and fmt not in FORMAT_ALIASES:
            raise CommandError("format must be one of %s" % sorted(FORMAT_ALIASES))
        hits = self._search(name, category, fmt)
        exact = [h for h in hits if h["exact"]]
        pool = exact or hits
        if not pool:
            raise CommandError("nothing named %r in browser.%s%s" % (
                name, category, " (format %s)" % fmt if fmt else ""))
        if not exact and len({h["name"] for h in pool}) > 1:
            raise CommandError("%r is ambiguous: %s" % (name, sorted({h["name"] for h in pool})))
        if len(pool) > 1 and category == "plugins":
            rank = {f: i for i, f in enumerate(FORMAT_PREFERENCE)}
            pool.sort(key=lambda h: rank.get(h["format"], 99))
        hit = pool[0]
        t = self._track(track)
        s = self._song()
        before = len(t.devices)
        s.view.selected_track = t
        if scene is not None:
            s.view.highlighted_clip_slot = list(t.clip_slots)[int(scene)]
        devices_before = [self._device_info(d, i) for i, d in enumerate(t.devices)]
        self._app().browser.load_item(hit["_item"])
        # Live adds the device asynchronously, and an instrument replaces the track's instrument
        # instead of adding one: the client polls list_devices until it differs from devices_before.
        return {"track": self._track_index(t), "device_count_before": before,
                "devices_before": devices_before,
                "loaded": self._public(hit),
                "other_matches": [self._public(h) for h in pool[1:]]}

    def cmd_load_plugin(self, name, track, format=None):
        return self.cmd_load_device(name, track, "plugins", format)

    # -- devices and parameters --------------------------------------------

    def cmd_list_devices(self, track):
        t = self._track(track)
        return [self._device_info(d, i) for i, d in enumerate(t.devices)]

    def cmd_list_parameters(self, track, device):
        d = self._device(self._track(track), device)
        return {"device": d.name,
                "parameters": [self._param_info(p, i) for i, p in enumerate(d.parameters)]}

    def cmd_get_parameter(self, track, device, parameter):
        d = self._device(self._track(track), device)
        p = self._param(d, parameter)
        return self._param_info(p, list(d.parameters).index(p))

    def cmd_set_parameter(self, track, device, parameter, value):
        """value: a number in [min, max], true/false, or one of value_items (quantized)."""
        d = self._device(self._track(track), device)
        p = self._param(d, parameter)
        if isinstance(value, bool):
            v = p.max if value else p.min
        elif isinstance(value, str):
            items = [str(x) for x in p.value_items] if p.is_quantized else []
            lowered = [x.lower() for x in items]
            if value.lower() in lowered:
                v = p.min + lowered.index(value.lower())
            else:
                try:
                    v = float(value)
                except ValueError:
                    raise CommandError("%r is not a value of %r; items: %s" % (value, p.name, items))
        else:
            v = float(value)
        if not p.min <= v <= p.max:
            raise CommandError("%s must be in [%s, %s], got %s" % (p.name, p.min, p.max, v))
        if not _safe(lambda: p.is_enabled, True):
            raise CommandError("parameter %r is disabled (automation or macro owns it)" % p.name)
        p.value = v
        return self._param_info(p, list(d.parameters).index(p))

    # -- routing -----------------------------------------------------------

    def _routing(self, track, direction):
        t = self._track(track)
        types = list(getattr(t, "available_%s_routing_types" % direction))
        chans = list(getattr(t, "available_%s_routing_channels" % direction))
        return t, types, chans

    def cmd_get_routing(self, track, direction="input"):
        t, types, chans = self._routing(track, direction)

        def type_info(rt):
            att = _safe(lambda: rt.attached_object)
            return {"display_name": rt.display_name,
                    "category": _safe(lambda: str(rt.category)),
                    "track_index": _safe(lambda: self._track_index(att)) if att is not None else None}

        return {
            "track": self._track_index(t),
            "direction": direction,
            "current": self._routing_info(t, direction),
            "available_types": [type_info(rt) for rt in types],
            "available_channels": [{"display_name": c.display_name} for c in chans],
        }

    def cmd_set_routing(self, track, direction="input", type=None, channel=None, source_track=None):
        """Pick the routing type by display name, or by `source_track` (the track it comes from);
        then the channel by display name from the list that type makes available."""
        t, types, _ = self._routing(track, direction)
        if source_track is not None:
            src = self._track(source_track)
            matches = [rt for rt in types if _safe(lambda rt=rt: rt.attached_object) == src]
            if not matches:
                raise CommandError("track %r is not an available %s source for %r" % (
                    src.name, direction, t.name))
            setattr(t, direction + "_routing_type", matches[0])
        elif type is not None:
            setattr(t, direction + "_routing_type", _match(types, type, lambda r: r.display_name,
                                                            direction + " routing type"))
        if channel is not None:
            chans = list(getattr(t, "available_%s_routing_channels" % direction))
            setattr(t, direction + "_routing_channel", _match(chans, channel, lambda c: c.display_name,
                                                               direction + " routing channel"))
        return self.cmd_get_routing(self._track_index(t), direction)

    def cmd_get_input_routing(self, track):
        return self.cmd_get_routing(track, "input")

    def cmd_set_input_routing(self, track, type=None, channel=None, source_track=None):
        return self.cmd_set_routing(track, "input", type, channel, source_track)

    # -- recording ---------------------------------------------------------

    def cmd_start_recording(self, mode="arrangement", from_beat=None, track=None, scene=None,
                            length_beats=None):
        """arrangement: record_mode on, then play (from `from_beat` if given).
        session: with `track`, fire its slot `scene` (default: first empty) to record into it;
        without, trigger session record on all armed tracks."""
        s = self._song()
        if mode == "arrangement":
            if s.is_playing:
                s.stop_playing()
            if from_beat is not None:
                s.current_song_time = float(from_beat)
            s.record_mode = True
            s.start_playing()
            return {"mode": mode, "record_mode": bool(s.record_mode),
                    "current_song_time": s.current_song_time}
        if mode != "session":
            raise CommandError("mode must be 'arrangement' or 'session'")
        if track is None:
            if length_beats is not None and hasattr(s, "trigger_session_record"):
                s.trigger_session_record(float(length_beats))
            else:
                s.session_record = True
            if not s.is_playing:
                s.start_playing()
            return {"mode": mode, "session_record": bool(s.session_record)}
        t = self._track(track)
        slots = list(t.clip_slots)
        if scene is None:
            empty = [i for i, sl in enumerate(slots) if not sl.has_clip]
            if not empty:
                raise CommandError("track %r has no empty clip slot" % t.name)
            scene = empty[0]
        slot = slots[int(scene)]
        if slot.has_clip:
            raise CommandError("slot %d on %r already has a clip" % (scene, t.name))
        if length_beats is not None:
            try:
                slot.fire(record_length=float(length_beats))
            except TypeError:
                slot.fire()
        else:
            slot.fire()
        self._last_session_slot = (t, int(scene))
        return {"mode": mode, "track": self._track_index(t), "scene": int(scene)}

    def cmd_stop_recording(self, mode="arrangement", stop_transport=True):
        s = self._song()
        if mode == "arrangement":
            s.record_mode = False
        elif mode == "session":
            s.session_record = False
            if self._last_session_slot:
                t, i = self._last_session_slot
                slot = _safe(lambda: list(t.clip_slots)[i])
                if slot is not None and _safe(lambda: slot.is_recording, False):
                    slot.stop()
        else:
            raise CommandError("mode must be 'arrangement' or 'session'")
        if stop_transport:
            s.stop_playing()
        return {"record_mode": bool(s.record_mode), "session_record": bool(s.session_record),
                "is_playing": bool(s.is_playing), "current_song_time": s.current_song_time}

    def cmd_clear_session_clips(self, track):
        """Delete every clip in the track's session slots (Phase 7: looped takes are recorded there)."""
        t = self._track(track)
        n = 0
        for slot in t.clip_slots:
            if slot.has_clip:
                slot.delete_clip()
                n += 1
        if self._last_session_slot and self._last_session_slot[0] == t:
            self._last_session_slot = None
        return {"track": self._track_index(t), "deleted": n}

    def cmd_clear_arrangement_clips(self, track):
        t = self._track(track)
        clips = list(t.arrangement_clips)
        for c in clips:
            t.delete_clip(c)
        return {"track": self._track_index(t), "deleted": len(clips)}

    # -- clips and notes ---------------------------------------------------

    def _pick_clip(self, t, source, clip_index, scene):
        if source in ("arrangement", "auto"):
            clips = [c for c in _safe(lambda: list(t.arrangement_clips), []) if c.is_midi_clip]
            if clips:
                if clip_index is not None:
                    return clips[int(clip_index)], "arrangement"
                return max(clips, key=lambda c: c.start_time), "arrangement"
            if source == "arrangement":
                raise CommandError("track %r has no arrangement MIDI clips" % t.name)
        slots = list(t.clip_slots)
        if scene is None:
            last = self._last_session_slot
            if last and last[0] == t and slots[last[1]].has_clip:
                scene = last[1]
            else:
                filled = [i for i, sl in enumerate(slots) if sl.has_clip]
                live = [i for i in filled if _safe(lambda i=i: slots[i].is_recording or slots[i].is_playing)]
                if not (live or filled):
                    raise CommandError("track %r has no clips" % t.name)
                scene = (live or filled)[-1]
        slot = slots[int(scene)]
        if not slot.has_clip or not slot.clip.is_midi_clip:
            raise CommandError("slot %d on %r has no MIDI clip" % (scene, t.name))
        return slot.clip, "session"

    @staticmethod
    def _read_notes(clip):
        lo = min(0.0, clip.start_marker, clip.loop_start)
        hi = max(clip.end_marker, clip.loop_end, clip.length) + 1.0
        if hasattr(clip, "get_notes_extended"):
            raw = clip.get_notes_extended(0, 128, lo, hi - lo)
            return [{"pitch": n.pitch, "start_time": n.start_time, "duration": n.duration,
                     "velocity": n.velocity, "mute": bool(n.mute),
                     "probability": _safe(lambda n=n: n.probability)} for n in raw]
        raw = clip.get_notes(lo, 0, hi - lo, 128)  # Live 10: (pitch, time, duration, velocity, mute)
        return [{"pitch": p, "start_time": st, "duration": d, "velocity": v, "mute": bool(m)}
                for p, st, d, v, m in raw]

    def cmd_get_clip_notes(self, track, source="auto", clip_index=None, scene=None):
        """Notes of the most recent clip on the track: the latest-starting arrangement clip, or
        the last session slot recorded through this bridge (else the playing/last filled one).
        Arrangement notes also get `song_time` = clip.start_time + start_time - start_marker."""
        t = self._track(track)
        clip, where = self._pick_clip(t, source, clip_index, scene)
        notes = self._read_notes(clip)
        info = {
            "track": self._track_index(t), "source": where, "name": clip.name,
            "start_time": _safe(lambda: clip.start_time), "end_time": _safe(lambda: clip.end_time),
            "start_marker": clip.start_marker, "end_marker": clip.end_marker,
            "loop_start": clip.loop_start, "loop_end": clip.loop_end,
            "looping": bool(clip.looping), "length": clip.length,
            "is_recording": _safe(lambda: bool(clip.is_recording)),
        }
        if where == "arrangement":
            for n in notes:
                n["song_time"] = clip.start_time + n["start_time"] - clip.start_marker
        notes.sort(key=lambda n: (n["start_time"], n["pitch"]))
        info["notes"] = notes
        return info

    # -- plug-in presets and clip files (Phase 6) ---------------------------

    def cmd_get_presets(self, track, device):
        """A plug-in's presets (its programs, as Live lists them) and the selected one."""
        d = self._device(self._track(track), device)
        return {"device": d.name,
                "presets": _safe(lambda: [str(p) for p in d.presets], []),
                "selected": _safe(lambda: int(d.selected_preset_index), -1)}

    def cmd_set_preset(self, track, device, preset):
        """Select a plug-in preset by index or name (Live sends the program change)."""
        d = self._device(self._track(track), device)
        names = _safe(lambda: [str(p) for p in d.presets], [])
        if not names:
            raise CommandError("%r lists no presets" % d.name)
        if isinstance(preset, int) or (isinstance(preset, str) and preset.isdigit()):
            index = int(preset)
        else:
            lowered = [n.lower() for n in names]
            if str(preset).lower() not in lowered:
                raise CommandError("%r has no preset %r; presets: %s" % (d.name, preset, names))
            index = lowered.index(str(preset).lower())
        if not 0 <= index < len(names):
            raise CommandError("preset index %d out of range (%d presets)" % (index, len(names)))
        d.selected_preset_index = index
        return self.cmd_get_presets(track, device)

    def cmd_load_clip_file(self, name, category="user_library"):
        """Load a MIDI file from Live's browser (e.g. the User Library), as a double-click or a
        drop would: Live imports it as a session clip on a new MIDI track. Returns the track
        count before; the client polls get_state for the new track and get_clip_notes for its
        clip."""
        hits = self._search(name, category)
        exact = [h for h in hits if h["exact"]]
        pool = exact or hits
        if not pool:
            raise CommandError("nothing named %r in browser.%s (Live may still be indexing it)" % (name, category))
        before = len(self._tracks())
        self._app().browser.load_item(pool[0]["_item"])
        return {"track_count_before": before, "loaded": self._public(pool[0])}

    # -- Phase 8: CPU load, deleting a device -------------------------------

    def cmd_get_cpu(self):
        """Live's CPU meter: Application.average_process_usage and peak_process_usage, in percent
        (Live 12.4.6 reports about 0.6 for its empty default set), or None where Live lacks them."""
        app = self._app()
        return {"average": _safe(lambda: float(app.average_process_usage)),
                "peak": _safe(lambda: float(app.peak_process_usage))}

    def cmd_delete_device(self, track, device):
        """Delete a device from a track (as pressing Delete on it would)."""
        t = self._track(track)
        d = self._device(t, device)
        index = list(t.devices).index(d)
        t.delete_device(index)
        return {"track": self._track_index(t), "deleted": index, "devices": [x.name for x in t.devices]}

    # -- meters ------------------------------------------------------------

    def cmd_get_meters(self, tracks=None):
        s = self._song()
        keys = tracks if tracks is not None else list(range(len(s.tracks)))
        out = []
        for key in keys:
            t = self._track(key)
            out.append({
                "track": self._track_index(t), "name": t.name,
                "left": _safe(lambda: t.output_meter_left),
                "right": _safe(lambda: t.output_meter_right),
                "level": _safe(lambda: t.output_meter_level),
            })
        m = s.master_track
        out.append({"track": "master", "name": m.name,
                    "left": _safe(lambda: m.output_meter_left),
                    "right": _safe(lambda: m.output_meter_right),
                    "level": _safe(lambda: m.output_meter_level)})
        return out

    # -- Phase 0 spikes: probing the LOM (see docs/spikes.md) ----------------

    def _lom_target(self, target, track=None, category=None, path=None):
        if target == "song":
            return self._song()
        if target == "song_view":
            return self._song().view
        if target == "application":
            return self._app()
        if target == "browser":
            return self._app().browser
        if target == "track":
            return self._track(track)
        if target == "browser_item":
            item = self._browser_root(category)
            for name in path or []:
                item = _match(_safe(lambda: list(item.children), []), name, lambda c: c.name, "browser item")
            return item
        raise CommandError("target must be song, song_view, application, browser, track or browser_item")

    def cmd_lom_dir(self, target="browser", track=None, category=None, path=None):
        """Public attribute names of a LOM object, with the value of plain ones (read-only)."""
        obj = self._lom_target(target, track, category, path)
        out = {}
        for name in dir(obj):
            if name.startswith("_"):
                continue
            value = _safe(lambda: getattr(obj, name), "<error>")
            if isinstance(value, (bool, int, float, str)) or value is None:
                out[name] = value
            elif callable(value):
                out[name] = "<method>"
            else:
                out[name] = "<%s>" % type(value).__name__
        return out

    def cmd_browser_stats(self, category, budget_ms=50, reset=False):
        """Count every node under a browser root, walking at most `budget_ms` per call and resuming
        on the next call, the way a time-sliced indexer would. Call until `done`."""
        walks = self._stats
        if reset or category not in walks:
            root = (list(self._app().browser.user_folders) if category == "user_folders"
                    else [self._browser_root(category)])
            walks[category] = {"stack": [(r, 0) for r in reversed(root)], "nodes": 0, "loadable": 0,
                               "folders": 0, "devices": 0, "max_depth": 0, "ext": {}, "ms": 0.0,
                               "calls": 0, "slowest_ms": 0.0}
        w = walks[category]
        t0 = time.time()
        deadline = t0 + float(budget_ms) / 1000.0
        stack = w["stack"]
        while stack and time.time() < deadline:
            item, depth = stack.pop()
            w["nodes"] += 1
            w["max_depth"] = max(w["max_depth"], depth)
            if _safe(lambda: item.is_folder, False):
                w["folders"] += 1
            if _safe(lambda: item.is_loadable, False):
                w["loadable"] += 1
                name = item.name
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                w["ext"][ext] = w["ext"].get(ext, 0) + 1
            if _safe(lambda: item.is_device, False):
                w["devices"] += 1
            for child in reversed(_safe(lambda: list(item.children), [])):
                stack.append((child, depth + 1))
        took = (time.time() - t0) * 1000.0
        w["ms"] += took
        w["calls"] += 1
        w["slowest_ms"] = max(w["slowest_ms"], took)
        out = {k: v for k, v in w.items() if k != "stack"}
        out.update(category=category, done=not stack, pending=len(stack))
        return out

    def cmd_preview_item(self, name, category="samples"):
        hits = self._search(name, category)
        exact = [h for h in hits if h["exact"]]
        pool = exact or hits
        if not pool:
            raise CommandError("nothing named %r in browser.%s" % (name, category))
        self._app().browser.preview_item(pool[0]["_item"])
        return {"previewing": self._public(pool[0])}

    def cmd_stop_preview(self):
        self._app().browser.stop_preview()
        return {"stopped": True}

    def cmd_song_data(self, key, value=None):
        """Song.get_data / set_data: a value stored in the set (to tell which set is open)."""
        s = self._song()
        if value is not None:
            s.set_data(str(key), value)
        return {"key": key, "value": s.get_data(str(key), None)}

    def cmd_clip_slots(self, track):
        t = self._track(track)
        return [{"scene": i, "has_clip": bool(sl.has_clip),
                 "clip": _safe(lambda sl=sl: {"name": sl.clip.name, "is_audio_clip": bool(sl.clip.is_audio_clip),
                                               "file_path": _safe(lambda: sl.clip.file_path),
                                               "length": sl.clip.length}) if sl.has_clip else None}
                for i, sl in enumerate(t.clip_slots)]
