"""Note-timing analysis: offset of each recorded note from the nearest grid line."""

import statistics


def beats_to_ms(beats, tempo):
    return beats * 60000.0 / tempo


def note_offsets(notes, tempo, grid=1.0, time_key="song_time", sample_rate=None):
    """Per-note offset from the nearest multiple of `grid` beats.

    Positive = late, negative = early. With `sample_rate`, also `offset_samples`.
    Returns dicts sorted by time.
    """
    rows = []
    for n in notes:
        t = n.get(time_key, n["start_time"])
        nearest = round(t / grid) * grid
        off = t - nearest
        rows.append({
            "pitch": n["pitch"],
            "time": t,
            "beat": nearest,
            "offset_beats": off,
            "offset_ms": beats_to_ms(off, tempo),
            "duration": n.get("duration"),
            "velocity": n.get("velocity"),
        })
        if sample_rate:
            rows[-1]["offset_samples"] = rows[-1]["offset_ms"] * sample_rate / 1000.0
    rows.sort(key=lambda r: (r["time"], r["pitch"]))
    return rows


def summarize(rows, buffer_size=None):
    """Offset statistics. With samples in the rows, also in samples; with `buffer_size`, the
    mean offset as whole buffers plus a remainder (a delay of one buffer shows as 1 + n)."""
    if not rows:
        return {"count": 0}
    ms = [r["offset_ms"] for r in rows]
    beats = sorted({r["beat"] for r in rows})
    summary = {
        "count": len(rows),
        "beats_hit": len(beats),
        "first_beat": beats[0],
        "last_beat": beats[-1],
        "mean_ms": statistics.fmean(ms),
        "median_ms": statistics.median(ms),
        "min_ms": min(ms),
        "max_ms": max(ms),
        "spread_ms": max(ms) - min(ms),
        "stdev_ms": statistics.pstdev(ms),
    }
    if all("offset_samples" in r for r in rows):
        smp = [r["offset_samples"] for r in rows]
        summary.update({
            "mean_samples": statistics.fmean(smp),
            "min_samples": min(smp),
            "max_samples": max(smp),
            "spread_samples": max(smp) - min(smp),
        })
        if buffer_size:
            whole = int(summary["mean_samples"] // buffer_size)
            summary.update({
                "buffer_size": buffer_size,
                "mean_buffers": summary["mean_samples"] / buffer_size,
                "mean_whole_buffers": whole,
                "mean_remainder_samples": summary["mean_samples"] - whole * buffer_size,
            })
    return summary


def missing_beats(rows, first, last, grid=1.0):
    have = {round(r["beat"] / grid) for r in rows}
    n0, n1 = round(first / grid), round(last / grid)
    return [k * grid for k in range(n0, n1 + 1) if k not in have]


def format_table(title, rows, summary, tempo, audio=None):
    """Text report. `audio` is livelog.audio_settings() (sample rate, buffer sizes), if known."""
    audio = audio or {}
    samples = bool(rows) and all("offset_samples" in r for r in rows)
    setting = ""
    if audio.get("sample_rate"):
        setting = ", %d Hz, buffer %s" % (audio["sample_rate"], audio.get("output_buffer", "?"))
    lines = ["", "== %s (%.1f BPM%s) ==" % (title, tempo, setting),
             "%8s %6s %10s %14s %10s" % ("beat", "pitch", "time", "offset_beats", "offset_ms")
             + (" %14s" % "offset_samples" if samples else "")]
    for r in rows:
        lines.append("%8.2f %6d %10.5f %+14.6f %+10.3f" % (
            r["beat"], r["pitch"], r["time"], r["offset_beats"], r["offset_ms"])
            + (" %+14.1f" % r["offset_samples"] if samples else ""))
    if summary.get("count"):
        lines.append("notes %(count)d on %(beats_hit)d beats; offset ms: mean %(mean_ms)+.3f "
                     "median %(median_ms)+.3f min %(min_ms)+.3f max %(max_ms)+.3f "
                     "spread %(spread_ms).3f stdev %(stdev_ms).3f" % summary)
        if "mean_samples" in summary:
            lines.append("offset samples: mean %(mean_samples)+.1f min %(min_samples)+.1f "
                         "max %(max_samples)+.1f spread %(spread_samples).1f" % summary)
        if "buffer_size" in summary:
            lines.append("mean = %(mean_buffers).3f x buffer (%(buffer_size)d) = %(mean_whole_buffers)d "
                         "buffer(s) + %(mean_remainder_samples).1f samples" % summary)
    else:
        lines.append("no notes recorded")
    if not audio.get("sample_rate"):
        lines.append("sample rate and buffer size unknown (not found in Live's Log.txt)")
    return "\n".join(lines)
