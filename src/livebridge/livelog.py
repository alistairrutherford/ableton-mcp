"""Live's audio settings, read from its Log.txt (the LOM has no sample rate or buffer size).

Live writes an "Audio In Out:" block to
~/Library/Preferences/Ableton/Live <version>/Log.txt at startup and on every change in
Settings > Audio. The last block is the current setting.
"""

import glob
import os
import re

PREFS = os.path.expanduser("~/Library/Preferences/Ableton")

_LINE = re.compile(r"^(\S+): info: Audio In Out: ([^:]+): (.*?)\s*$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

_KEYS = {
    "Sample Rate": "sample_rate",
    "Input Buffer Size": "input_buffer",
    "Output Buffer Size": "output_buffer",
    "Input Latency": "input_latency_ms",
    "Output Latency": "output_latency_ms",
    "Overall Latency": "overall_latency_ms",
}


def log_path(live_version=None):
    """Log.txt for a Live version ("12.4.6"), else the most recently written one."""
    if live_version:
        p = os.path.join(PREFS, "Live %s" % live_version, "Log.txt")
        if os.path.exists(p):
            return p
    logs = glob.glob(os.path.join(PREFS, "Live *", "Log.txt"))
    return max(logs, key=os.path.getmtime) if logs else None


def parse_audio_settings(text):
    """The last value of each audio setting in a Log.txt's text, or {} if none.

    Integers for sample rate and buffer sizes, floats for latencies in ms; `logged_at` is the
    timestamp of the last Sample Rate line.
    """
    out = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        stamp, key, value = m.groups()
        name = _KEYS.get(key)
        if name is None:
            continue
        num = _NUMBER.search(value)
        if num is None:
            continue
        out[name] = float(num.group()) if name.endswith("_ms") else int(float(num.group()))
        if name == "sample_rate":
            out["logged_at"] = stamp
    return out


def audio_settings(live_version=None, path=None):
    path = path or log_path(live_version)
    if not path:
        return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        settings = parse_audio_settings(f.read())
    if settings:
        settings["log"] = path
    return settings
