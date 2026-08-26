"""Load a MUD.LOG 4.4b file into dv_well_mud_log and dv_well_shows.

    python tools/load_mudlog.py                    # dry run, prints what it found
    python tools/load_mudlog.py --apply
    python tools/load_mudlog.py --remove --apply   # undo

THIS IS THE ONLY WRITER of dv_well_mud_log and dv_well_shows. Both used to be
written by load_well_detail.py alongside four other tables; they moved here
because a mud log is not a well attribute, it is its own acquisition with its
own vendor binary format, and it needed a parser rather than a row builder.

Why a mud log is not a wireline log
-----------------------------------
It is on DRILLER'S depth with lagged returns; a wireline log is on logger's
depth. They disagree by feet, routinely. Folding the two into dv_well_log
would put two incompatible depth systems in one column with nothing marking
which is which -- and they would plot together and look fine, which is the
failure this codebase cares about most. dv_well_shows also FKs to
(uwi, mud_log_id): merge the tables and all 1,189 wireline logs become legal
parents for a gas show.

The file format, which cost an afternoon to read
------------------------------------------------
Two sections, two different layouts. Neither is documented; both were derived
from a hex dump and are self-validating here so a wrong guess cannot pass.

HEADER -- tag/length/value, starting at offset 0x32:

    <uint16 tag><uint16 len><len bytes of text>

    0x13f4  "51.04.65'"          ground elevation (see the typo note below)
    0x13f5  "5114.65'"           KB elevation -- THE DEPTH DATUM
    0x13f6  "545'"               top of the logged interval
    0x13f7  "5760'"              base of the logged interval

    Reading these by TAG matters. The first version of this parser pulled every
    depth-looking run out of the first 3000 bytes, sorted them, and took the
    min and max. That returns 545-5760, which is RIGHT -- and it is right by
    luck, because the two elevations happened to sort to the middle. It also
    silently discarded both of them, and the KB elevation is the one field that
    makes the whole table's depths meaningful.

    It also read three strings one character too long -- "Steel - TensleepP",
    "RMOTCR", "Mark MillikenQ". Those trailing capitals are the NEXT record's
    low tag byte bleeding into a printable run. A regex that strips a trailing
    capital gets the right name by the wrong method and will eventually strip a
    real one.

DESCRIPTIONS -- fixed record, 330 of them:

    <uint16 track><float32 depth><uint16 subtype><uint16 len><len bytes of text>

    THE TRACK TAG COMES FIRST, BEFORE THE DEPTH -- see TRACK_NAMES. 263 records
    are the geologist's sample descriptions and 67 the driller's engineering
    remarks. The depth is trustworthy: the record at 4316.40 reads
    "...set 4316.49' of 7" Csg to 4323' KB", quoting its own depth back.

    The scan below accepts a record ONLY when its declared length matches
    printable text of exactly that length, so it needs no knowledge of what
    surrounds a record and cannot drift into the numeric arrays.

What is NOT read: the curve samples. They are in the file -- five float32
arrays, and two of them are 2,606 samples spanning 545-5755 ft at exactly
2.0 ft, which is the logged interval -- but the file gives no binding from an
array to a track. The track TABLE is readable (Depth, % Lithology, Porosity,
Oil Shows, "TG, C1-C4", Eng. Data ...) and the arrays are findable, and there
is nothing connecting the two. Calling one of them ROP would be a guess that
plots, so they are left alone and rop_avg / mud_weight_avg stay NULL. See the
notes in build().

    0x0765e8  2606 @ 2.0 ft  2.00-122.00  41 distinct, integer-valued
    0x078f0e  2606 @ 2.0 ft  1.48- 90.00  55 distinct
    0x054f4e  2400           0.20- 45.00  66 distinct
    0x07c994  1192           0.18-  4.54  45 distinct
    0x07dc38   301           0.27-  9.50  42 distinct
"""

import argparse
import datetime
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MUDLOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "training", "Teapot_Dome", "DataSets", "Core", "CD Files", "mudlog",
    "48X28.LOG")

WELL_NUM = "48-X-28"
MUD_ID = "48-X-28-MUDLOG1"
BY = "MUDLOG_LOADER"
SRC = "MUDLOG"

# Header tags. Named here because a bare 0x13f5 in the code below is exactly
# the kind of magic number that gets copied to the wrong field later.
T_WELL = 0x13ec
T_LOCATION = 0x13ed
T_LATLONG = 0x13f2
T_ELEV_GL = 0x13f4
T_ELEV_KB = 0x13f5
T_TOP = 0x13f6
T_BASE = 0x13f7
T_MUD = 0x13f9
T_CASING = 0x13fa
T_LOGGER = 0x1450
T_OPERATOR = 0x1451

HEADER_START = 0x32

# THE TRACK IS NAMED IN THE FILE, and the name is the tag that PRECEDES the
# depth -- not a position after it. The first version read the uint16 that
# FOLLOWS the depth and called it a track, which sorted the records into a 0
# and a 1 that happened to separate descriptions from events. It was a
# coincidence that held for this file: that field is a subtype, and reading
# it as the track found 302 of the 330 records and could name none of them.
#
# The file declares its own tracks in a definition table at 0x054974, which is
# where these tags and names come from. "Oil Shows" is declared and carries NO
# text -- it is a graphical track -- which is why shows have to be read out of
# the geologist's descriptions rather than looked up.
TRACK_NAMES = {
    0x1131: "Curve Track",
    0x1134: "Eng. Data",
    0x1135: "Depth",
    0x1136: "Porosity Type",
    0x1137: "Porosity",
    0x1138: "Lithology",
    0x1141: "Oil Shows",
    0x1142: "Geol. Descrs.",
    0x114a: "TG, C1-C4",
    0x114b: "Eng. Data 2",
    0x114c: "Intervals",
    0x114d: "Events",
    0x114e: "% Lithology",
}
TRACK_SAMPLE = 0x1142                     # the geologist's sample descriptions
TRACK_EVENTS = (0x1134, 0x114b)           # the driller's engineering remarks


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------

def read_header(data):
    """Walk the tag/length/value header. Stops at the first record that is not
    clean text, which is where the header ends and the drawing data begins."""
    out, off, seen = {}, HEADER_START, 0
    while off + 4 <= len(data) and seen < 64:
        tag, ln = struct.unpack_from("<HH", data, off)
        if off + 4 + ln > len(data):
            break
        val = data[off + 4:off + 4 + ln]
        if ln and not all(32 <= b < 127 for b in val):
            break
        if ln:
            # A tag can repeat; the first occurrence wins, matching the
            # promote rule elsewhere in this codebase.
            out.setdefault(tag, val.decode("ascii").strip())
        off += 4 + ln
        seen += 1
    return out


def read_records(data):
    """Every <float32 depth><uint16 track><uint16 len><text> record.

    Accepted only when the DECLARED length matches printable text of exactly
    that length. That check is what lets this scan the whole file, including
    the float32 curve arrays, without inventing records out of curve values."""
    found = []
    for off in range(2, len(data) - 8):
        depth, = struct.unpack_from("<f", data, off)
        if not 0.0 < depth < 6500.0:
            continue
        subtype, ln = struct.unpack_from("<HH", data, off + 4)
        if not 0 < ln <= 400 or subtype > 64:
            continue
        if off + 8 + ln > len(data):
            continue
        text = data[off + 8:off + 8 + ln]
        if not all(32 <= b < 127 for b in text):
            continue
        text = text.decode("ascii")
        if len(text.strip()) < 6 or not re.search(r"[A-Za-z]", text):
            continue
        # the track tag sits two bytes BEFORE the depth
        track, = struct.unpack_from("<H", data, off - 2)
        if track not in TRACK_NAMES:
            continue
        found.append((off, depth, track, text.strip()))
    # Overlapping candidates: the earliest wins and the rest of its bytes are
    # consumed, so a record cannot also be read starting one byte later.
    found.sort()
    keep, end = [], -1
    for off, depth, track, text in found:
        if off >= end:
            keep.append((depth, track, text))
            end = off + 8 + len(text)
    return keep


# --------------------------------------------------------------------------
# shows
# --------------------------------------------------------------------------
# THE NEGATIVES ARE THE WHOLE JOB. 38 of the 263 sample descriptions mention
# fluorescence, stain or cut, and only about a quarter of those are a
# hydrocarbon show. This mud logger was careful and wrote down what he did NOT
# see -- "no vis ostn, no vis flor", "no cut", "prob mnrl" -- so a naive search
# for "flor" reports 38 shows in a well that has roughly 11, most of them weak.
# A confident wrong show plots, exports and gets quoted; a missing one is
# visible. So every negation is deleted from the text BEFORE anything positive
# is looked for, and what survives is the evidence.

_NEGATIONS = [
    # MINERAL FLUORESCENCE IS CARBONATE OR ANHYDRITE, NOT HYDROCARBON, and it
    # is the single largest false positive here: 13 of the 38. The qualifier
    # must swallow the WORD IT QUALIFIES. "rr spty flor prob mnrl" means the
    # fluorescence is probably mineral -- deleting only "prob mnrl" leaves
    # "flor" standing and the description is then read as a show. That was
    # wrong on four intervals in the Frontier before this pattern was widened.
    r"\bflor\s+prob\s+mnrl", r"\bflor\s+poss\s+mnrl",
    r"abndt\s+mnrl\s+flor", r"tr\s+mnrl\s+flor", r"yel\s+mnrl\s+flor",
    r"mnrl\s+flor", r"prob\s+mnrl", r"poss\s+mnrl",
    # explicit absence. "dy" is the logger's typo for "dry" and appears more
    # often than the correct spelling.
    r"no\s+vis\s+ostn", r"no\s+vis\s+flor\.?", r"no\s+vis\s+flour\.?",
    r"no\s+flor", r"no\s+flour",
    r"no\s+cut(\s+wet\s+or\s+d(ry|y))?", r"poor\s*-\s*no\s+cut",
    r"or\s+chrushed",
]
# "no cut" is a TEST THAT WAS RUN AND FAILED. Fluorescence with no cut is
# routinely mineral or contamination, so when the only surviving evidence is
# fluorescence and the logger explicitly recorded no cut, that is a record of
# absence and not a show.
_NO_CUT = re.compile(r"no\s+cut", re.I)
# Cavings are rock from higher up the hole, so a show in them is not a show at
# this depth. Excluded outright rather than downgraded.
_CAVINGS = re.compile(r"poss\s+cvgs|\bcvgs\b", re.I)

_STAIN = re.compile(r"\bostn\b|\bstn\b", re.I)
_CUT = re.compile(r"\bcut\b", re.I)
_FLOR = re.compile(r"\bflor\b|\bflour\b|\bfluor\b", re.I)
# Qualifiers the logger used for weak evidence: rare, faint, trace, spotty,
# scattered, residual, dull, poor.
_WEAK = re.compile(r"\brr\b|\bfnt\b|\btr\b|\bspty\b|\bscat\b|\bdull\b|\bpoor\b",
                   re.I)
_RESIDUAL = re.compile(r"\bresd\b", re.I)

_LITH = [(r"^\s*SS\b", "SANDSTONE"), (r"^\s*Dolo", "DOLOMITE"),
         (r"^\s*Ls\b", "LIMESTONE"), (r"^\s*Sh\b", "SHALE"),
         (r"^\s*Sltst", "SILTSTONE"), (r"^\s*Anhy", "ANHYDRITE")]


def classify_show(text):
    """Return a show dict, or None when the description is not a show.

    Returns None for: no evidence, mineral fluorescence only, explicit
    negatives only, and anything the logger attributed to cavings."""
    if _CAVINGS.search(text):
        return None
    stripped = text
    for pat in _NEGATIONS:
        stripped = re.sub(pat, " ", stripped, flags=re.I)

    stain = bool(_STAIN.search(stripped))
    cut = bool(_CUT.search(stripped))
    flor = bool(_FLOR.search(stripped))
    if not (stain or cut or flor):
        return None
    if flor and not stain and not cut and _NO_CUT.search(text):
        return None                     # tested for a cut, and there was none

    score = (2 if stain else 0) + (2 if cut else 0) + (1 if flor else 0)
    weak = bool(_WEAK.search(stripped))
    residual = bool(_RESIDUAL.search(stripped))

    if residual or score <= 2:
        rating = "POOR"
    elif score >= 4 and not weak:
        rating = "GOOD"
    else:
        rating = "FAIR"

    lith = next((v for p, v in _LITH if re.search(p, text, re.I)), None)
    return {"show_type": "OIL" if (stain or cut) else "FLUORESCENCE",
            "show_rating": rating, "lithology": lith,
            "stain": stain, "cut": cut, "flor": flor}


# --------------------------------------------------------------------------
# the database
# --------------------------------------------------------------------------

def _facts(engine):
    from sqlalchemy import text
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT uwi FROM dataview.dv_well "
            "WHERE REPLACE(REPLACE(UPPER(well_num),'-',''),' ','') = :n"),
            {"n": WELL_NUM.upper().replace("-", "")}).fetchall()
        if len(rows) != 1:
            raise SystemExit("Expected one well numbered %s, found %d."
                             % (WELL_NUM, len(rows)))
        uwi = str(rows[0][0])
        tops = [(str(r[0]), float(r[1])) for r in c.execute(text(
            "SELECT strat_unit_name, top_depth "
            "FROM dataview.dv_well_formation_top "
            "WHERE uwi = :u AND top_depth IS NOT NULL ORDER BY top_depth"),
            {"u": uwi}).fetchall()]
    return uwi, tops


def _unit_at(tops, depth):
    """The deepest pick at or above this depth, and how far above it is.

    NOT written to strat_unit_name. This is the driller's-depth / logger's-
    depth disagreement that justifies the whole table, arriving as a concrete
    problem: the best show in the well is a SANDSTONE at mud-log 5450.6, and
    the deepest pick above that depth is the Opeche SHALE -- because the
    Tensleep A Sandstone is picked at 5459.63 on the wireline, nine feet
    deeper. Comparing the two depth systems directly would file the well's
    best oil show under the wrong formation, and it would look perfectly
    reasonable in a report.

    So the pick travels in the remark, with its distance, and the geologist
    decides. Missing is visible; a wrong formation is not."""
    name, top = None, None
    for nm, t in tops:
        if t <= depth:
            name, top = nm, t
        else:
            break
    if name is None:
        return None
    return "%s (picked %.1f ft, %.1f ft above this sample)" % (
        name, top, depth - top)


def _feet(raw):
    """A header depth like "5114.65'" -> 5114.65.

    "51.04.65'" is in this file where the ground elevation belongs, and is
    5104.65 mistyped with an extra dot -- it sits 10 ft below the KB elevation
    of 5114.65, which is a normal KB height. The repair is spelled out rather
    than silently regexed away, because a value this shape is exactly what
    "wrong is worse than missing" is about: returning None here loses a real
    datum, and guessing wrong invents an elevation."""
    if not raw:
        return None
    s = raw.strip().rstrip("'").strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    m = re.fullmatch(r"(\d+)\.(\d{2})\.(\d+)", s)
    if m:                      # 51.04.65 -> 5104.65
        return float("%s%s.%s" % (m.group(1), m.group(2), m.group(3)))
    return None


def build(engine, path):
    data = open(path, "rb").read()
    hdr = read_header(data)
    recs = read_records(data)
    uwi, tops = _facts(engine)

    # THE FILE THESE ROWS CAME FROM, by the catalog's own id. This is what
    # promotion_lineage tests: a file's data reached the database when its
    # INVENTORY_ID turns up in a dv_ table. Looked up, never minted -- an id
    # the catalog does not carry is an orphan that reads as provenance until
    # someone follows it. NULL when the file is not catalogued, and main()
    # says so rather than letting it pass silently.
    from dataview.core.file_identity import catalogued_inventory_id
    inv, inv_why = catalogued_inventory_id(engine, path)

    samples = sorted([r for r in recs if r[1] == TRACK_SAMPLE])
    events = sorted([r for r in recs if r[1] in TRACK_EVENTS])

    kb = _feet(hdr.get(T_ELEV_KB))
    gl = _feet(hdr.get(T_ELEV_GL))
    top = _feet(hdr.get(T_TOP))
    base = _feet(hdr.get(T_BASE))

    rows = []
    # -- the header row ------------------------------------------------
    # rop_avg, mud_weight_avg and their uoms stay NULL. They live in the
    # float32 curve arrays this parser does not read yet, and an average is
    # exactly the kind of number that looks authoritative once it is in a
    # column. Missing is visible; wrong is not.
    remark = ["MUD.LOG 4.4b."]
    if hdr.get(T_LOGGER):
        remark.append("Mud logger: %s." % hdr[T_LOGGER])
    if hdr.get(T_OPERATOR):
        remark.append("Operator: %s." % hdr[T_OPERATOR])
    if hdr.get(T_CASING):
        # Verbatim. "Steel - Tensleep" is casing and objective on one line and
        # splitting it would be a guess about a vendor's field.
        remark.append("Casing/objective as written: %s." % hdr[T_CASING])
    if hdr.get(T_LATLONG):
        remark.append("Header %s." % hdr[T_LATLONG])
    remark.append("%d sample descriptions and %d drilling events decoded; "
                  "curve arrays not extracted." % (len(samples), len(events)))

    rows.append(("dv_well_mud_log", {
        "uwi": uwi, "mud_log_id": MUD_ID,
        "log_date": datetime.date(2004, 5, 18),
        "top_depth": top, "base_depth": base, "depth_ouom": "FT",
        "depth_datum": "KB" if kb else None,
        "kb_elevation": kb, "ground_elevation": gl,
        "elevation_ouom": "FT" if (kb or gl) else None,
        "mud_type": hdr.get(T_MUD),
        "file_path": path,
        "remark": " ".join(remark),
        "INVENTORY_ID": inv,
        "source": SRC}))

    # -- the shows -----------------------------------------------------
    shows, si = [], 0
    for i, (depth, _t, txt) in enumerate(samples):
        cls = classify_show(txt)
        if cls is None:
            continue
        si += 1
        # The base of a sample is the next sample's depth: these are interval
        # samples, not points, and inventing a fixed thickness would be a
        # number nobody measured.
        nxt = samples[i + 1][0] if i + 1 < len(samples) else base
        ev = [k for k in ("stain", "cut", "flor") if cls[k]]
        shows.append(("dv_well_shows", {
            "uwi": uwi, "mud_log_id": MUD_ID,
            "show_id": "%s-SHOW%02d" % (WELL_NUM, si),
            "show_type": cls["show_type"], "show_rating": cls["show_rating"],
            "top_depth": depth, "base_depth": nxt, "depth_ouom": "FT",
            # strat_unit_name stays NULL on purpose -- see _unit_at.
            "strat_unit_name": None,
            "lithology": cls["lithology"],
            "remark": "%s | evidence: %s | driller's depth; nearest pick "
                      "above (logger's depth): %s"
                      % (txt[:300], ", ".join(ev),
                         _unit_at(tops, depth) or "none"),
            "INVENTORY_ID": inv,
            "source": SRC}))
    rows.extend(shows)
    return rows, hdr, samples, events, shows, inv, inv_why


# --------------------------------------------------------------------------

def _write(engine, rows):
    from collections import Counter
    from sqlalchemy import text
    made = Counter()
    with engine.begin() as c:
        # The same guard load_well_detail honours: dv_r_source is owned by the
        # Reference Tables app, which supplies a curated short and long name.
        # A loader that seeds its own code is a second writer to that table.
        cited = {d["source"] for _t, d in rows if d.get("source")}
        known = {r[0] for r in c.execute(text(
            "SELECT source FROM dataview.dv_r_source"))}
        missing = sorted(cited - known)
        if missing:
            raise SystemExit(
                "These source codes are not registered in dv_r_source:\n"
                "    %s\n"
                "Add them in the Reference Tables app (it owns this table), "
                "then re-run." % ", ".join(missing))

        need = {}
        for t, cn in c.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='dataview' AND IS_NULLABLE='NO' "
                "AND COLUMN_DEFAULT IS NULL "
                "AND COLUMN_NAME IN ('active_ind','row_created_date')")):
            need.setdefault(t, set()).add(cn)
        now = datetime.datetime.now()
        for table, d in rows:
            fill = {cn: ("Y" if cn == "active_ind" else now)
                    for cn in need.get(table, ()) if cn not in d}
            if fill:
                d = dict(d, **fill)
            cols = list(d)
            p = dict(d)
            p["__by"] = BY
            c.execute(text(
                "INSERT INTO dataview.[%s] (%s, row_created_by) "
                "VALUES (%s, :__by)"
                % (table, ", ".join("[%s]" % x for x in cols),
                   ", ".join(":%s" % x for x in cols))), p)
            made[table] += 1
    return made


def _remove(engine, uwi):
    from collections import Counter
    from sqlalchemy import text
    gone = Counter()
    with engine.begin() as c:
        # Children first: dv_well_shows FKs to (uwi, mud_log_id).
        for t in ("dv_well_shows", "dv_well_mud_log"):
            r = c.execute(text(
                "DELETE FROM dataview.[%s] WHERE uwi=:u AND row_created_by=:b"
                % t), {"u": uwi, "b": BY})
            gone[t] = r.rowcount if r.rowcount and r.rowcount > 0 else 0
    return gone


def main(argv=None):
    from dataview.core.dw_utils import make_engine
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default=MUDLOG)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--show-all", action="store_true",
                    help="print every description that mentions stain, "
                         "fluorescence or cut, including the rejected ones")
    a = ap.parse_args(argv)
    engine = make_engine(a.database)

    if a.remove:
        uwi, _ = _facts(engine)
        if not a.apply:
            print("Would remove every row stamped %s. Add --apply." % BY)
            return 0
        gone = _remove(engine, uwi)
        for t in sorted(gone):
            print("   %-24s %5d removed" % (t, gone[t]))
        print("Total %d row(s)." % sum(gone.values()))
        return 0

    if not os.path.exists(a.path):
        raise SystemExit("No such file: %s" % a.path)
    rows, hdr, samples, events, shows, inv, inv_why = build(engine, a.path)

    print("MUD.LOG header, read by tag")
    print("   well          : %s" % hdr.get(T_WELL))
    print("   location      : %s" % hdr.get(T_LOCATION))
    print("   %s" % hdr.get(T_LATLONG))
    print("   elevation     : KB %s   GL %s"
          % (_feet(hdr.get(T_ELEV_KB)), _feet(hdr.get(T_ELEV_GL))))
    print("   logged        : %s - %s ft"
          % (_feet(hdr.get(T_TOP)), _feet(hdr.get(T_BASE))))
    print("   mud type      : %s" % hdr.get(T_MUD))
    print("   casing/obj    : %s" % hdr.get(T_CASING))
    print("   mud logger    : %s" % hdr.get(T_LOGGER))
    print("   operator      : %s" % hdr.get(T_OPERATOR))
    print()
    print("   %d sample descriptions, %d drilling events"
          % (len(samples), len(events)))
    if inv:
        print("   INVENTORY_ID  : %s (from the file catalog)" % inv)
    else:
        # LOUD, because rows that cite no file are invisible to lineage --
        # promotion_lineage reports them as "Nothing", which is the one state
        # that means real failure.
        print("   INVENTORY_ID  : NULL -- %s" % inv_why)
        print("                   These rows will not be traceable to the "
              "file. Scan it with --exts .log to fix.")
    print()

    if a.show_all:
        pat = re.compile(r"flor|flour|fluor|\bcut\b|ostn|\bstn\b", re.I)
        print("Every description mentioning stain / fluorescence / cut:")
        for depth, _t, txt in samples:
            if not pat.search(txt):
                continue
            cls = classify_show(txt)
            verdict = ("%s %s" % (cls["show_type"], cls["show_rating"])
                       if cls else "rejected")
            print("   %7.1f  %-16s %s" % (depth, verdict, txt[:88]))
        print()

    print("Shows accepted (%d):" % len(shows))
    for _t, d in shows:
        print("   %7.1f - %-8.1f %-12s %-4s %-10s %s"
              % (d["top_depth"], d["base_depth"], d["show_type"],
                 d["show_rating"], d["lithology"] or "",
                 d["remark"].split("logger's depth): ")[-1]))
    print()

    if not a.apply:
        print("Dry run. Add --apply to write %d row(s)." % len(rows))
        return 0
    made = _write(engine, rows)
    for t in sorted(made):
        print("   %-24s %5d" % (t, made[t]))
    print("Wrote %d row(s)." % sum(made.values()))
    print("Undo with:  python tools/load_mudlog.py --remove --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
