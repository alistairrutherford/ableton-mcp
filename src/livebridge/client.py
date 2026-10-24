"""Client for the ClaudeLiveBridge Remote Script (newline-delimited JSON over TCP)."""

import itertools
import json
import os
import socket
import time

BRIDGE_NAME = "ClaudeLiveBridge"   # the Remote Script, as listed in Live's Control Surface menu
DEFAULT_HOST = os.environ.get("LIVEBRIDGE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("LIVEBRIDGE_PORT", "9873"))


class BridgeError(RuntimeError):
    """The bridge ran the command and it failed inside Live."""

    def __init__(self, cmd, error, traceback=None):
        super().__init__("%s: %s" % (cmd, error))
        self.cmd = cmd
        self.error = error
        self.remote_traceback = traceback


class LiveClient:
    """One persistent connection. Each call waits for its reply.

    Live polls the socket about every 100 ms, so a call takes up to ~100 ms.
    """

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock = None
        self._buf = b""
        self._ids = itertools.count(1)

    # -- connection --------------------------------------------------------

    def connect(self):
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._buf = b""
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def call(self, cmd, **args):
        self.connect()
        req_id = next(self._ids)
        line = json.dumps({"id": req_id, "cmd": cmd, "args": args}).encode("utf-8") + b"\n"
        try:
            self._sock.sendall(line)
            while True:
                resp = json.loads(self._readline())
                if resp.get("id") == req_id:
                    break
        except (OSError, ValueError):
            self.close()
            raise
        if not resp.get("ok"):
            raise BridgeError(cmd, resp.get("error"), resp.get("traceback"))
        return resp.get("result")

    def _readline(self):
        while b"\n" not in self._buf:
            data = self._sock.recv(65536)
            if not data:
                raise ConnectionError("bridge closed the connection")
            self._buf += data
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    # -- helpers built on the commands ---------------------------------------

    def wait_until(self, predicate, timeout, interval=0.1, what="condition"):
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                raise TimeoutError("timed out after %.1f s waiting for %s" % (timeout, what))
            time.sleep(interval)

    def load_device_and_wait(self, name, track, category="plugins", format=None, timeout=20.0):
        """Load from the browser by name and wait until the track's devices change; returns the new
        device's info, with `replaced` set to the device it took the place of, if any."""
        res = self.call("load_device", name=name, track=track, category=category, format=format)
        info = self.wait_for_new_device(track, res["devices_before"], timeout, what="%s to load" % name)
        info["browser_item"] = res["loaded"]
        return info

    def wait_for_new_device(self, track, before, timeout, what="a device"):
        """Wait until a track's devices differ from `before` (list_devices output); return the new
        device with `replaced` set to the one it took the place of, if any.

        An effect is added to the chain, but an instrument replaces the track's instrument, so the
        device count can stay the same: we wait for the list to differ, not to grow.
        """
        old = [_device_key(d) for d in before]
        devices = self.wait_until(
            lambda: (lambda d: d if [_device_key(x) for x in d] != old else None)(
                self.call("list_devices", track=track)),
            timeout, what="%s on track %s" % (what, track))
        now = [_device_key(d) for d in devices]
        new = [d for d, k in zip(devices, now) if k not in old]
        if not new:  # the same devices, reordered: take the first one that moved
            new = [d for i, (d, k) in enumerate(zip(devices, now)) if i >= len(old) or k != old[i]]
        gone = [d for d, k in zip(before, old) if k not in now]
        info = dict(new[0])
        info["replaced"] = gone[0] if gone else None
        return info

    def load_item_and_wait(self, id, track, root=None, path=None, scene=None, expect="device",
                           timeout=20.0):
        """Load a browser index entry (see the index_* commands) and wait for the result.

        expect="device": the track's devices change (see wait_for_new_device).
        expect="clip": clip slot `scene` gets a clip (a sample on an audio track, a clip file).
        Returns {"device": ...} or {"clip": ...}, plus "tempo_before" and "tempo_after", since
        loading a loop can change the song tempo.
        """
        if expect not in ("device", "clip"):
            raise ValueError("expect must be 'device' or 'clip'")
        if expect == "clip" and scene is None:
            raise ValueError("a clip needs a scene (clip slot) to go into")
        res = self.call("load_item", id=id, track=track, root=root, path=path, scene=scene)
        out = {"track": res["track"], "id": id, "name": res["name"], "tempo_before": res["tempo_before"]}
        if res.get("showed_session_view"):
            out["showed_session_view"] = True
        what = "%s to load" % res["name"]
        if expect == "device":
            out["device"] = self.wait_for_new_device(track, res["devices_before"], timeout, what)
        else:
            out["clip"] = self.wait_until(
                lambda: (lambda sl: sl["clip"] if sl["has_clip"] else None)(
                    self.call("clip_slots", track=track)[scene]),
                timeout, what="%s into slot %d of track %s" % (what, scene, track))
            out["scene"] = scene
        out["tempo_after"] = self.call("get_state", include_devices=False)["tempo"]
        return out

    def wait_song_time(self, beats, timeout):
        return self.wait_until(
            lambda: self.call("get_state", include_devices=False)["current_song_time"] >= beats,
            timeout, what="song time %.2f" % beats)

    def peak_meter(self, track, seconds, interval=0.05):
        """Highest output meter level of a track over `seconds` (sampled by the client)."""
        peak = 0.0
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            m = self.call("get_meters", tracks=[track])[0]
            peak = max(peak, m["level"] or 0.0, m["left"] or 0.0, m["right"] or 0.0)
            time.sleep(interval)
        return peak

    def record_arrangement(self, track, beats, from_beat=0.0, timeout_pad=10.0):
        """Record `beats` beats into the arrangement from `from_beat` and return the clip's notes."""
        state = self.call("get_state", include_devices=False)
        seconds = beats * 60.0 / state["tempo"]
        self.call("start_recording", mode="arrangement", from_beat=from_beat)
        try:
            # Allow for a count-in bar, and stop a little after the last beat.
            self.wait_song_time(from_beat + beats + 0.25, seconds * 2 + timeout_pad)
        finally:
            self.call("stop_recording", mode="arrangement", stop_transport=True)
        return self.call("get_clip_notes", track=track, source="arrangement")


def _device_key(d):
    """What identifies a device across two list_devices calls: Live's id where the bridge has it,
    else its name and class (so replacing a device with an identical one goes unnoticed)."""
    if d is None:
        return None
    if d.get("id") is not None:
        return ("id", d["id"])
    return ("name", d["name"], d.get("class_name"))


def connect(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=10.0):
    return LiveClient(host, port, timeout).connect()
