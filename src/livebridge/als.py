"""Edit a saved Live set (.als: gzipped XML) where the Live Object Model can't reach.

The LOM has no access to a device's Sidechain chooser, but Live stores the choice in the set:

    <PluginDevice> ... <SideChain> <OnOff><Manual Value="true"/>...</OnOff>
        <RoutedInput><Routable><Target Value="AudioIn/Track.18/PostFxOut"/> ...

`set_sidechain` writes that into a copy of a set, so a test can open the copy with the sidechain
already routed. Verified in Live 12.4.6 with NoteCap's VST3 (the sidechain test passes on it).

`set_tempo` and `set_time_signature` set the song's tempo and signature in a set before Live opens
it, for new projects (docs/spikes.md §2). The master's value is in two places: the parameter's
`Manual` value, and the first event of its automation envelope, which wins when it exists.

    python -m livebridge.als "path/to/set.als" --track "Vocal Comp" --source "Kick" --device "MyCompressor"
"""

import argparse
import gzip
import re
import sys

TRACK_TAGS = ("AudioTrack", "MidiTrack")


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)


def tracks(text):
    """[(tag, id, name, start, end)] for every audio and MIDI track, end = index of its closing tag."""
    out = []
    for m in re.finditer(r'<(%s) Id="(\d+)"' % "|".join(TRACK_TAGS), text):
        tag, tid, start = m.group(1), m.group(2), m.start()
        end = text.index("</%s>" % tag, start)
        name = re.search(r'<EffectiveName Value="([^"]*)"', text[start:end])
        out.append((tag, tid, name.group(1) if name else "", start, end))
    return out


def header(text):
    """The <Ableton> element's attributes: MajorVersion, MinorVersion (the schema, e.g.
    "12.0_12402"), SchemaChangeCount, Creator ("Ableton Live 12.4.7") and Revision."""
    m = re.search(r"<Ableton\b([^>]*)>", text)
    if not m:
        raise ValueError("not a Live set: no <Ableton> element")
    return dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))


def schema_key(minor_version):
    """A sortable key for a MinorVersion such as "12.0_12402": (12, 0, 12402)."""
    return tuple(int(n) for n in re.findall(r"\d+", minor_version or "")) or (0,)


def _set_master_param(text, tag, value, event):
    """Set the song's <Tempo> or <TimeSignature>: its Manual value, and the initial event
    (Time="-63072000") of the automation envelope that points at its AutomationTarget."""
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), text, re.S)
    if not m:
        raise ValueError("no <%s> in the set" % tag)
    block = m.group(1)
    target = re.search(r'<AutomationTarget Id="(\d+)">', block)
    new_block, n = re.subn(r'(<Manual Value=")[^"]*(" />)', r"\g<1>%s\2" % value, block, count=1)
    if n != 1:
        raise ValueError("no Manual value in <%s>" % tag)
    text = text[:m.start(1)] + new_block + text[m.end(1):]
    if target:
        text = re.sub(r'(<PointeeId Value="%s" />\s*</EnvelopeTarget>\s*<Automation>\s*<Events>\s*'
                      r'<%s Id="\d+" Time="-63072000" Value=")[^"]*(")' % (target.group(1), event),
                      r"\g<1>%s\2" % value, text, count=1)
    return text


def set_tempo(text, bpm):
    bpm = float(bpm)
    if not 20.0 <= bpm <= 999.0:
        raise ValueError("tempo must be 20..999 BPM")
    return _set_master_param(text, "Tempo", ("%f" % bpm).rstrip("0").rstrip("."), "FloatEvent")


SIGNATURE_DENOMINATORS = (1, 2, 4, 8, 16)


def time_signature_value(numerator, denominator):
    """Live's encoding of a time signature: (numerator - 1) + 99 * log2(denominator), so 4/4 is
    201 and 3/4 is 200 (checked by opening patched sets in Live 12.4.7)."""
    numerator, denominator = int(numerator), int(denominator)
    if not 1 <= numerator <= 99:
        raise ValueError("time signature numerator must be 1..99")
    if denominator not in SIGNATURE_DENOMINATORS:
        raise ValueError("time signature denominator must be one of %s" % (SIGNATURE_DENOMINATORS,))
    return (numerator - 1) + 99 * SIGNATURE_DENOMINATORS.index(denominator)


def set_time_signature(text, numerator, denominator):
    return _set_master_param(text, "TimeSignature", time_signature_value(numerator, denominator), "EnumEvent")


def _track(text, name):
    found = [t for t in tracks(text) if t[2] == name]
    if not found:
        raise ValueError("no track named %r; have %s" % (name, [t[2] for t in tracks(text)]))
    if len(found) > 1:
        raise ValueError("more than one track named %r" % name)
    return found[0]


def set_sidechain(text, track, source, device):
    """Return `text` with `device` on `track` taking its sidechain from `source`'s Post FX output,
    and its Sidechain switched on."""
    _, _, _, start, end = _track(text, track)
    _, source_id, _, _, _ = _track(text, source)
    span = text[start:end]

    for m in re.finditer(r"<PluginDevice Id=", span):
        d0 = m.start()
        d1 = span.index("</PluginDevice>", d0)
        dev = span[d0:d1]
        if 'Value="%s"' % device not in dev or "<SideChain>" not in dev:
            continue
        s0 = dev.index("<SideChain>")
        s1 = dev.index("</SideChain>", s0)
        sc = dev[s0:s1]
        new = re.sub(r'(<OnOff>\s*<LomId Value="\d+" />\s*<Manual Value=")[a-z]+(")', r"\1true\2", sc, count=1)
        new = re.sub(r'(<Routable>\s*<Target Value=")[^"]*(")', r"\1AudioIn/Track.%s/PostFxOut\2" % source_id, new,
                     count=1)
        new = re.sub(r'(<UpperDisplayString Value=")[^"]*(")', r"\g<1>%s\2" % source, new, count=1)
        new = re.sub(r'(<LowerDisplayString Value=")[^"]*(")', r"\1Post FX\2", new, count=1)
        if "AudioIn/Track.%s/PostFxOut" % source_id not in new:
            raise ValueError("could not find the sidechain routing in %r on %r" % (device, track))
        dev = dev[:s0] + new + dev[s1:]
        span = span[:d0] + dev + span[d1:]
        return text[:start] + span + text[end:]
    raise ValueError("no plug-in %r with a sidechain on track %r" % (device, track))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="livebridge.als", description="Route a plug-in's sidechain in a copy of a set.")
    ap.add_argument("set", help="the saved .als")
    ap.add_argument("--track", required=True, help="track holding the plug-in")
    ap.add_argument("--source", required=True, help="track to feed its sidechain (Post FX)")
    ap.add_argument("--device", required=True, help="plug-in name")
    ap.add_argument("-o", "--out", help="output .als (default: '<set> sidechain.als' next to it)")
    opts = ap.parse_args(argv)
    out = opts.out or re.sub(r"\.als$", "", opts.set) + " sidechain.als"
    if out == opts.set:
        ap.error("refusing to overwrite the input set; pick another --out")
    write(out, set_sidechain(read(opts.set), opts.track, opts.source, opts.device))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
