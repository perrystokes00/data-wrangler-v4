"""Read a WellSight "Export All Data" file (.dat) from STRIP/MUD/HORIZONTAL.LOG.

    from mudlog_export import parse
    ex = parse(r"C:\\Bulk\\mudlog_test\\export.dat")
    ex.curve("ROP").data        # [(depth, value), ...]
    ex.text("Geol. Descrs.")    # [(depth, "SS - clr wh, ..."), ...]
    ex.choice("Lithology")      # [(top, base, ["SHALE"]), ...]

WHY THIS EXISTS, AND WHAT IT REPLACES
-------------------------------------
tools/load_mudlog.py reverse-engineers the .LOG binary, because that was the
only way in until the viewer turned up. This reads what the viewer WRITES, and
it is better in every measurable way:

                            binary parse      this
    text records                     330       507
    coded lithology                  263*      703
    coded oil shows                    0        22
    curves identified                  0**       6
    curve scales                       0         4 (they change with depth)

    *  first word of each description, guessed
    ** two were FOUND and one was MISNAMED -- see below

READ THIS BEFORE TRUSTING A REVERSE-ENGINEERED CURVE. The binary parse located
a float32 array at 0x0765E8 and identified it as ROP on four grounds that all
looked independent: integer-valued, 97% inside the 0-30 scale the viewer
prints, excursions consistent with hard-rock spikes, and 2,606 samples at
exactly 2.0 ft spanning the logged interval.

Every one of those was a coincidence. The array is TG -- total gas -- and the
neighbouring array at 0x078F0E is C1. Real ROP has a maximum of 45 and a mean
of 2.45; the array called ROP has a maximum of 122 and a mean of 11.22, which
is TG to two decimal places.

The "0-30 scale" argument was the worst of them, because the scale is not 0-30.
It is 0-5 for most of the hole and changes FOUR times:

    520 ft   0-5        5300 ft   0-30
   5200 ft   0-10       5656 ft   0-10

so a curve was being tested against a range that applies to 356 ft of a 5,215 ft
log. Four weak arguments agreeing is not four independent confirmations; it is
one assumption wearing four hats. The plot was labelled a candidate and put
beside the viewer's own render precisely so this could be caught, and it was --
by eye, in about a minute, by someone who has read a lot of mud logs.

THE FORMAT is documented by the vendor in "Export All.txt", which ships beside
strip.exe. Tab-separated, one record per line, first field a reference depth
for sorting, second field the tag. Readers are told to ignore records and
fields they do not recognise, which is what makes it safe to parse loosely.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NULL_DEFAULT = -999.25


class Track(object):
    __slots__ = ("tid", "name", "unit", "step", "kind", "type", "codes",
                 "scales", "data")

    def __init__(self, tid, name, kind):
        self.tid = tid
        self.name = name
        self.kind = kind          # curve | text | choice
        self.unit = None
        self.step = None
        self.type = None          # DEPTH | RANGE
        self.codes = {}           # choice tracks: {int: label}
        self.scales = []          # curve tracks: [(from_depth, min, max)]
        self.data = []

    def __repr__(self):
        return "<%s %s %r n=%d>" % (self.kind, self.tid, self.name,
                                    len(self.data))

    def scale_at(self, depth):
        """The (min, max) in force at this depth.

        THE SCALE IS NOT A PROPERTY OF THE CURVE, it is a property of the
        curve AT A DEPTH -- the mud logger rescales the track when the hole
        changes character, and the viewer reprints the legend where it does.
        A single fixed scale is how the first version of the plot came out
        looking nothing like the original."""
        lo, hi = None, None
        for d, mn, mx in self.scales:
            if d <= depth:
                lo, hi = mn, mx
            else:
                break
        if lo is None and self.scales:
            lo, hi = self.scales[0][1], self.scales[0][2]
        return lo, hi


class Export(object):
    def __init__(self):
        self.source = {}
        self.header = {}
        self.depth_unit = "FT"
        self.depth_range = (None, None)
        self.depth_scale = None
        self.null = NULL_DEFAULT
        self.tracks = {}

    def _by_name(self, kind, name):
        for t in self.tracks.values():
            if t.kind == kind and t.name == name:
                return t
        return None

    def curve(self, name):
        return self._by_name("curve", name)

    def text(self, name):
        t = self._by_name("text", name)
        return t.data if t else []

    def choice(self, name):
        t = self._by_name("choice", name)
        return t.data if t else []

    def curves(self):
        return [t for t in self.tracks.values() if t.kind == "curve"]

    def __repr__(self):
        return "<Export %s %s-%s %s>" % (self.header.get("Well Name", "?"),
                                         self.depth_range[0],
                                         self.depth_range[1], self.depth_unit)


def _num(s, null=NULL_DEFAULT):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return None if v == null else v


def parse(path):
    """Read the export. Unknown records and extra fields are ignored, which
    the format explicitly guarantees is safe -- record types are never removed
    and fields are only appended."""
    ex = Export()
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        rows = [ln.rstrip("\n").split("\t") for ln in fh]

    # Two passes: the format promises every header record precedes the tracks,
    # and every ???_DATUM is preceded by its ???_TRACK -- but a file that
    # breaks that promise should lose its data, not raise, so the tracks are
    # collected first and the datums attached second.
    for f in rows:
        if len(f) < 2:
            continue
        tag = f[1]
        if tag == "NULL_NUMBER" and len(f) > 2:
            try:
                ex.null = float(f[2])
            except ValueError:
                pass
        elif tag == "SOURCE" and len(f) > 2:
            ex.source = dict(zip(("program", "version", "file", "date", "time"),
                                 f[2:7]))
        elif tag == "HEADER_DATUM" and len(f) > 4:
            ex.header[f[3]] = f[4]
        elif tag == "DEPTH_UNIT" and len(f) > 2:
            ex.depth_unit = f[2]
        elif tag == "DEPTH_RANGE" and len(f) > 3:
            ex.depth_range = (_num(f[2]), _num(f[3]))
        elif tag == "DEPTH_SCALE" and len(f) > 2:
            ex.depth_scale = _num(f[2])
        elif tag == "CURVE_TRACK" and len(f) > 3:
            t = Track(f[2], f[3], "curve")
            t.unit = f[4] if len(f) > 4 else None
            t.step = _num(f[5]) if len(f) > 5 else None
            ex.tracks[f[2]] = t
        elif tag == "TEXT_TRACK" and len(f) > 3:
            t = Track(f[2], f[3], "text")
            t.type = f[4] if len(f) > 4 else "DEPTH"
            ex.tracks[f[2]] = t
        elif tag in ("CHOICE_TRACK", "COMPOSITION_TRACK") and len(f) > 6:
            t = Track(f[2], f[3], "choice")
            t.type = f[4]
            for pair in f[7:]:
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    try:
                        t.codes[int(k)] = v
                    except ValueError:
                        pass
            ex.tracks[f[2]] = t

    for f in rows:
        if len(f) < 3:
            continue
        tag, tid = f[1], f[2]
        t = ex.tracks.get(tid)
        if t is None:
            continue
        if tag == "CURVE_SCALE" and len(f) > 5:
            d, mn, mx = _num(f[3]), _num(f[4]), _num(f[5])
            if d is not None:
                t.scales.append((d, mn, mx))
        elif tag == "CURVE_DATUM" and len(f) > 4:
            d = _num(f[3])
            v = _num(f[4], ex.null)
            if d is not None:
                t.data.append((d, v))          # v is None where the log is null
        elif tag == "TEXT_DATUM" and len(f) > 4:
            d = _num(f[3])
            if d is not None:
                # A LINE BREAK IS TWO CHARACTERS IN THIS FORMAT. The spec says
                # a text field "may contain the character sequence \n to denote
                # a new line" -- backslash then n, not a control character.
                # Left as they are they print literally, which is how the
                # engineering track first came out reading
                # "WOB 10/14\nRPM 100\nPP 2" straight across the plot.
                t.data.append((d, f[4].replace(chr(92) + "n", chr(10))))
        elif tag in ("CHOICE_DATUM", "COMPOSITION_DATUM") and len(f) > 4:
            if t.type == "RANGE" and len(f) > 5:
                top, base, rest = _num(f[3]), _num(f[4]), f[5:]
            else:
                top, base, rest = _num(f[3]), None, f[4:]
            labels = []
            for c in rest:
                try:
                    labels.append(t.codes.get(int(float(c)), c))
                except ValueError:
                    labels.append(c)
            if top is not None:
                t.data.append((top, base, labels))

    for t in ex.tracks.values():
        t.scales.sort()
        t.data.sort(key=lambda r: r[0])
    return ex


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Summarise a WellSight export.")
    ap.add_argument("path")
    a = ap.parse_args(argv)
    ex = parse(a.path)
    print("%s" % ex)
    print()
    for k, v in ex.header.items():
        print("   %-24s %s" % (k, v))
    print()
    for t in sorted(ex.tracks.values(), key=lambda x: (x.kind, x.name)):
        extra = ""
        if t.kind == "curve":
            vals = [v for _d, v in t.data if v is not None]
            if vals:
                extra = "  %.2f-%.2f  scales: %s" % (
                    min(vals), max(vals),
                    ", ".join("%.0f ft %g-%g" % s for s in t.scales))
        print("   %-7s %-8s %-15s n=%-5d%s"
              % (t.kind, t.tid, t.name, len(t.data), extra))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
