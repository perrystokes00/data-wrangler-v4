r"""Synthetic well documents for the TEAPOT DOME wells.

Teapot has 1,373 wells and no paperwork of its own. The document corpora it
can reach -- synth_docs\, Teapot_Field_Model\wells\ -- belong to no Teapot
well in particular, so loading them fills the catalog and almost nothing else.
These documents belong to specific wells: the header of every page is that
well's row from dv_well, so an extracted document AGREES with the well already
in the database rather than contradicting it, and the detail it carries hangs
off that well's uwi.

WHICH WELLS. `uwi LIKE '49025%'` -- Natrona County, Wyoming, which is all
1,373 of them (1,344 carry the name + location + TD a document needs). Read
from dv_well rather than from a workbook because the loaded rows ARE the
Teapot set here: the 120-well Teapot_Field_Model\tabular\DV_WELL.xlsx is a
different, unloaded population (uwi 4902590xxxxx), and documents written
against it would name wells that do not exist.

WRITTEN AGAINST THE EXTRACTOR, NOT AGAINST TASTE. pdf_document_loader finds a
section two ways and BOTH have to be satisfied:

  * a table is matched by SIGNATURE WORDS in its header row -- rows_of("md",
    "azi") for a survey grid, rows_of("test date","result") for a DST -- and
    then each column by _find_col(head, ...), which is a startswith. So
    "Test Type" does NOT match the candidate "type" and the column is dropped;
    the header must read "Type".
  * four sections are gated on the DOCUMENT TYPE, which _detect_type() reads
    out of the page text: pressure points need "rft"/"mdt"/"formation tester",
    flow periods need "well test"/"flow test", petrophysical zones need
    "petrophysical". A perfect table in a document whose title does not say the
    magic word extracts NOTHING.

That second rule is why this writes eight document types rather than the six
asked for: DST detail and flow periods cannot come from the same page (the
period branch runs only for a well test, and a well test emits ONE dst row and
ignores a detail table), and pressure points need their own title. The type
detector is also ORDERED -- "survey report" is tested before "rft", so the
pressure document must not call itself a survey.

WHAT IS REAL AND WHAT IS NOT. Header values are the well's own dv_well row --
uwi, name, operator, field, county, state, spud, completion, TD, KB, latitude,
longitude -- printed only where the column is populated, never invented. A
blank label is honest; a filled-in wrong one plots and gets quoted. Everything
measured -- tops, casing, stations, zones, stages, tests -- is generated,
deterministically from the uwi, because those are the tables a document is the
ONLY source for and so are what the provenance scorecard has to show.

THE SECTION IS REAL TEAPOT DOME, AT REAL DEPTHS. Shannon at ~400 ft through
Tensleep at ~5,400 to Madison at ~6,300, clipped at the well's own TD -- and
Teapot TDs run 180 to 6,864 ft with a MEDIAN of 1,080, because the shallow
Shannon producers outnumber the deep Tensleep wells. Placing tops as a
fraction of TD would put Madison at 171 ft in a 180-ft hole; absolute depths
with a TD cut give a shallow well its two formations and a deep one all
sixteen. The casing plan is cut the same way.

    python tools/gen_teapot_docs.py                    # what it would write
    python tools/gen_teapot_docs.py --apply
    python tools/gen_teapot_docs.py --wells 100 --apply
    python tools/gen_teapot_docs.py --remove --apply

Then scan the output folder from the File Catalog, or:

    python -m dataview.import_data.pipeline_run --root <out> --exts .pdf \
        --server localhost\SQLEXPRESS --database DataView_Demo \
        --promote --promote-apply
"""
import argparse
import os
import random
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ...\data_wrangler\data_wrangler_v4\tools -> ...\data_wrangler\training
TRAINING = os.path.join(os.path.dirname(REPO_ROOT), "training")
OUT_DIR = os.path.join(TRAINING, "Teapot_Dome", "well_reports")

# Natrona County, Wyoming -- every Teapot well carries it, and nothing else in
# the database does. NOT C:\Bulk: that is staging workspace, never input.
UWI_LIKE = "49025%"

# type -> (title, what the loader takes from it). The title is not decoration:
# _detect_type() reads it, and four of these sections exist only because of it.
DOC_TYPES = {
    "well_report": ("END OF WELL REPORT",
                    "formation tops + casing strings"),
    "scout":       ("WELL SCOUT TICKET",
                    "tops + casing + DST records + survey stations"),
    "deviation":   ("DIRECTIONAL SURVEY REPORT",
                    "survey header + stations"),
    "core":        ("PETROPHYSICAL AND CORE ANALYSIS REPORT",
                    "petrophysical interp + zones   [needs 'petrophysical']"),
    "completion":  ("WELL COMPLETION REPORT",
                    "stimulation stages + casing"),
    "dst":         ("DRILL STEM TEST REPORT",
                    "one DST record per test"),
    "welltest":    ("WELL TEST REPORT",
                    "one DST + flow periods   [needs 'well test']"),
    "pressure":    ("FORMATION PRESSURE TEST - RFT / MDT",
                    "pressure points   [needs 'rft'; must NOT say 'survey report']"),
}
TYPES = tuple(DOC_TYPES)

# A dry hole was still drilled, logged, surveyed and tested -- a DST is how it
# was found to be dry -- but it was never completed and never flow tested, so
# it gets six documents rather than eight. Wrong is worse than missing.
#
# WOGCC codes, not words: dv_well.well_type is 'O'/'I'/'S'/'W'/'DH' here and
# well_status is 'PR'/'PA'/'SI'/'DR'/'TA'. A test written against the word
# 'DRY' -- which is what the Teacup CSV says -- matches nothing in this data
# and would silently give every dry hole a frac job.
_COMPLETED_ONLY = ("completion", "welltest")
_DRY_TYPE = {"DH", "D", "DRY", "DRY HOLE"}
_DRY_STATUS = {"DR", "DRY", "D&A"}


def is_dry(w):
    return ((w.get("well_type") or "").strip().upper() in _DRY_TYPE
            or (w.get("status") or "").strip().upper() in _DRY_STATUS)


def types_for(w, wanted):
    if is_dry(w):
        return [t for t in wanted if t not in _COMPLETED_ONLY]
    return list(wanted)


# Teapot Dome (NPR-3), Natrona County WY -- the real section, at real depths,
# shallowest first. Each entry is a depth RANGE the top is drawn from, so
# neighbouring wells disagree by a plausible amount instead of every well in
# the field reporting Shannon at exactly the same foot.
#              name,             top lo,  top hi, thickness, pay?
COLUMN = [
    ("Shannon",          300,   620,  110, True),
    ("Sussex",           780,  1150,   90, True),
    ("Steele",          1200,  1500,  180, False),
    ("Niobrara",        1450,  1850,  260, False),
    ("Carlile",         1900,  2250,  140, False),
    ("Frontier",        2350,  2800,  210, True),
    ("Mowry",           2950,  3150,  120, False),
    ("Muddy",           3150,  3480,   70, True),
    ("Thermopolis",     3400,  3600,   90, False),
    ("Dakota",          3600,  3950,  120, True),
    ("Lakota",          4050,  4380,  110, True),
    ("Morrison",        4400,  4720,  150, False),
    ("Sundance",        4800,  5120,  180, True),
    ("Crow Mountain",   5200,  5420,  110, False),
    ("Alcova",          5420,  5560,   60, False),
    ("Tensleep",        5400,  5900,  240, True),
    ("Amsden",          5950,  6220,  180, False),
    ("Madison",         6250,  6600,  300, True),
]
# name, OD in decimal inches (a printed fraction like 13-3/8 survives the
# loader's _num() as "13-38"), weight lb/ft, grade, shoe as a fraction of TD.
# Cut to what the hole is deep enough for: a 300-ft Shannon well was drilled
# with surface pipe and a production string, not four strings.
CASING_PLAN = [
    ("Conductor",    "20.000",  "94",   "K-55", 0.05, 1500),
    ("Surface",      "13.375",  "54.5", "K-55", 0.20,  350),
    ("Intermediate", "9.625",   "40",   "J-55", 0.62, 3000),
    ("Production",   "5.500",   "17",   "N-80", 0.97,    0),
]


def _rng(uwi):
    """Deterministic per well: the same uwi always yields the same document."""
    return random.Random("teapot-doc-" + str(uwi))


def _f(v, nd=0):
    try:
        return format(float(v), ",.%df" % nd)
    except (TypeError, ValueError):
        return ""


def _depth(v):
    """A depth from dv_well, printed WITHOUT losing it.

    _f() rounds to whole feet, which turned a TD of 1032.5 into "1,032" -- a
    document that contradicts the row it was written from, by half a foot, in
    the one field the loader reads back into dv_well. Print what is there and
    drop only the zeros SQL Server's decimal(n,4) pads on: 1032.5000 ->
    "1,032.5", 5038.3200 -> "5,038.32", 460.0000 -> "460".
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    s = format(f, ",.4f")
    return s.rstrip("0").rstrip(".")


def _coord(v):
    """A latitude or longitude, without decimal(n,10)'s padding zeros.

    "43.2841107000" is not wrong -- _num() reads it correctly -- but nothing
    printed by a person looks like that, and a document that reads as machine
    output invites the reader to distrust the rest of the page.
    """
    s = str(v or "").strip()
    return s.rstrip("0").rstrip(".") if "." in s else s


# ── the wells ──────────────────────────────────────────────────────────────

_FIELDS = ("uwi", "well_name", "operator", "field", "county", "state",
           "status", "well_type", "spud", "comp", "td", "kb", "lat", "lon")

_WELL_SQL = """
    SELECT RTRIM(w.uwi), w.well_name, w.operator_name, w.field_name,
           w.county, w.province_state, w.well_status, w.well_type,
           CONVERT(varchar(10), w.spud_date, 120),
           CONVERT(varchar(10), w.completion_date, 120),
           w.final_td, w.kb_elevation,
           w.surface_latitude, w.surface_longitude
      FROM dataview.dv_well w
     WHERE w.uwi LIKE :like
       AND w.well_name       IS NOT NULL
       AND w.surface_latitude IS NOT NULL
       AND w.final_td        IS NOT NULL
     ORDER BY w.uwi"""


def teapot_wells(engine, limit, like=UWI_LIKE):
    """The Teapot wells a document can actually be written for.

    Name, location and TD are the minimum: a document with no TD has no
    section to report and a document with no location cannot be checked
    against the map. 1,344 of the 1,373 qualify.

    Sampled with a stride over the uwi order rather than TOP n, so the sample
    spans the field's whole depth range -- the shallow Shannon wells and the
    deep Tensleep wells sort into different blocks, and TOP n would document
    one kind of well and call it Teapot.
    """
    from sqlalchemy import text
    with engine.connect() as c:
        rows = [tuple(r) for r in c.execute(text(_WELL_SQL), {"like": like})]
    if not rows:
        raise SystemExit(
            "no wells match uwi LIKE '%s' with a name, a location and a TD.\n"
            "Is the Teapot set loaded?" % like)
    if limit and limit < len(rows):
        step = len(rows) / float(limit)
        rows = [rows[int(i * step)] for i in range(limit)]
    out = []
    for r in rows:
        # Values travel as SQL Server hands them back; _depth() does the
        # printing. A "%g" here would silently round anything past six
        # significant digits, which is the same class of bug as _f().
        out.append(dict(zip(_FIELDS,
                            ["" if v is None else str(v).strip() for v in r])))
    return out


# ── generated content ──────────────────────────────────────────────────────

def _td(w):
    try:
        return max(120.0, float(w["td"]))
    except (TypeError, ValueError):
        return 5000.0


def _section(w):
    """The formations this well actually penetrated, top-down.

    One place decides it, because tops, zones and pressure points must agree:
    a core report naming a zone the well report says was never reached is the
    kind of quiet contradiction that survives every check and gets noticed on
    camera.
    """
    r, td = _rng(w["uwi"]), _td(w)
    out = []
    for name, lo, hi, thick, pay in COLUMN:
        if lo > td - 20:                    # the hole never reached this unit
            continue
        # Clamp the draw to what the hole actually reached. Drawing from the
        # full range and then discarding anything below TD is what left a
        # 460 ft Shannon producer reporting NO formations at all -- the well
        # plainly penetrated Shannon; it is where its TD is.
        top = r.uniform(lo, max(lo, min(hi, td - 20)))
        out.append((name, top, min(td, top + thick), pay))
    return out


def tops_for(w):
    """Formation tops. A DRY HOLE REPORTS NO PAY.

    The section is the same either way -- a dry hole penetrated the same rock
    as its neighbour -- but a well the database calls dry cannot also report
    net oil pay in eight formations. That contradiction is on the page, in
    front of the audience, and it is the reader who notices it.
    """
    r, dry = _rng(w["uwi"]), is_dry(w)
    out = []
    for name, top, base, pay in _section(w):
        show = pay and not dry
        out.append([name, _f(top), _f(base),
                    ("%.1f" % r.uniform(4, max(6, (base - top) * 0.5)))
                    if show else "\u2014",
                    ("OIL" if show else "\u2014")])
    return out


def casing_for(w):
    """Only the strings a hole this deep would carry.

    Teapot TDs run from 180 ft; a shallow Shannon well got surface pipe and a
    production string, and printing a conductor plus an intermediate on it
    would be four confident wrong rows in dv_well_casing.
    """
    td = _td(w)
    return [[nm, od, wt, gr, _f(td * frac)]
            for nm, od, wt, gr, frac, min_td in CASING_PLAN if td >= min_td]


def survey_for(w, n=16):
    """MD / Incl / Azi / TVD. Signature: "md" + "azi"."""
    r, td = _rng(w["uwi"]), _td(w)
    az, md, tvd, inc = r.uniform(0, 359), 0.0, 0.0, 0.0
    step, rows = td / n, []
    for _ in range(n):
        md += step
        # Teapot wells are vertical to near-vertical; drift a degree, not a
        # landing. The loader drops a station with incl > 120 or azi > 360.
        inc = max(0.0, min(6.0, inc + r.uniform(-0.6, 0.9)))
        tvd += step * (1 - (inc / 180.0))
        az = (az + r.uniform(-8, 8)) % 360
        rows.append([_f(md), "%.2f" % inc, "%.2f" % az, _f(tvd),
                     "%.2f" % r.uniform(0, 1.4)])
    return rows


def zones_for(w):
    """Petrophysical zones. Signature: "zone" + "top"; needs dt == petro.

    The pay members of the same section the well report prints, so the two
    documents agree.
    """
    r, dry = _rng(w["uwi"]), is_dry(w)
    out = []
    for name, top, base, pay in _section(w):
        if not pay:
            continue
        gross = base - top
        # A dry hole was logged and analysed like any other; what it lacks is
        # net. Keep the porosity and Sw, take the net-to-gross away.
        ng = r.uniform(0.0, 0.10) if dry else r.uniform(0.25, 0.85)
        out.append([name, _f(top), _f(base), "%.1f" % gross,
                    "%.1f" % (gross * ng), "%.2f" % ng,
                    "%.3f" % r.uniform(0.06, 0.22),
                    "%.3f" % r.uniform(0.18, 0.62)])
    return out


def stages_for(w):
    """Frac stages, across the deepest pay the well reached.

    Signature: "stage" + "top".
    """
    r, td = _rng(w["uwi"]), _td(w)
    pays = [z for z in _section(w) if z[3]]
    if not pays:
        return []
    top, base = pays[-1][1], min(td, pays[-1][2])
    n = r.randint(2, 6)
    span = max(30.0, base - top)
    return [[str(i + 1), _f(top + span * i / n), _f(top + span * (i + 1) / n),
             "SLICKWATER", _f(r.randint(1200, 9000)),
             _f(r.randint(40000, 190000)), _f(r.randint(3800, 7600)),
             "%.1f" % r.uniform(12, 42)] for i in range(n)]


def pressures_for(w):
    """RFT/MDT points, one per pay member of the section this well reached.

    Signature: "depth" + "pressure"; needs dt == pressure.
    """
    r = _rng(w["uwi"])
    grad = r.uniform(0.38, 0.46)
    return [[_f(top + (base - top) * 0.4), name,
             "%.1f" % ((top + (base - top) * 0.4) * grad),
             r.choice(["OIL", "OIL", "WATER", "GAS"]),
             "%.1f" % r.uniform(0.4, 240.0)]
            for name, top, base, pay in _section(w) if pay]


def dst_for(w):
    """DST records, one per pay member the well reached.

    Signature: "test date" + "result". Two constraints, both because a DST is
    a fact and not a decoration:

    * A DST tests a FORMATION, so the intervals come from the same _section()
      the well report prints. Depths drawn as a fraction of TD instead put a
      test at 130 ft in a 180-ft cellar hole that never reached the Shannon.
    * A well with neither a completion nor a spud date gets no DST table --
      17 of the 1,344 -- rather than a test dated from thin air. The loader
      only accepts a row matching \\d{4}-\\d\\d-\\d\\d, so an invented date is
      not a blank that reads as missing; it is a row in dv_well_dst.
    """
    from datetime import datetime, timedelta
    r = _rng(w["uwi"])
    base = w["comp"] or w["spud"]
    pays = [z for z in _section(w) if z[3]]
    if not base or not pays:
        return []
    try:
        d0 = datetime.strptime(base[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return []
    # A dry hole's tests are how it was found to be dry.
    results = (["WATER", "TIGHT", "SHOWS", "NO RECOVERY"] if is_dry(w)
               else ["OIL", "OIL AND GAS", "GAS", "WATER", "TIGHT"])
    out = []
    for name, top, bot, _p in pays[-3:]:
        d = (d0 - timedelta(days=r.randint(3, 90))).strftime("%Y-%m-%d")
        res = r.choice(results)
        wet = res in ("WATER", "TIGHT", "SHOWS", "NO RECOVERY")
        out.append([d, "DST", _f(top), _f(bot), res,
                    _f(0 if wet else r.randint(20, 900)),
                    _f(0 if wet else r.randint(0, 2600)),
                    "—" if wet else "%.1f" % r.uniform(28, 42)])
    return out


def periods_for(w):
    """Flow periods. Signature: "period"; needs dt == welltest."""
    r = _rng(w["uwi"])
    ch = "%d/64" % r.randint(12, 48)
    return [["1", "INITIAL FLOW", "%d" % r.randint(5, 20), ch,
             _f(r.randint(60, 400)), _f(r.randint(200, 900)),
             _f(r.randint(0, 300))],
            ["2", "INITIAL SHUT-IN", "%d" % r.randint(30, 90), "CLOSED",
             "0", "0", "0"],
            ["3", "FINAL FLOW", "%d" % r.randint(30, 120), ch,
             _f(r.randint(90, 600)), _f(r.randint(300, 1200)),
             _f(r.randint(0, 500))],
            ["4", "FINAL SHUT-IN", "%d" % r.randint(45, 180), "CLOSED",
             "0", "0", "0"]]


# ── PDF ────────────────────────────────────────────────────────────────────

def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    s = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=s["Title"], fontSize=15,
                                alignment=0, spaceAfter=1,
                                textColor=colors.HexColor("#16324f")),
        "sub": ParagraphStyle("s", parent=s["Normal"], fontSize=8.5,
                              textColor=colors.HexColor("#6b7280")),
        "sec": ParagraphStyle("h", parent=s["Heading3"], fontSize=10,
                              textColor=colors.HexColor("#1d4ed8"),
                              spaceBefore=12, spaceAfter=4),
    }


_AVAIL = 7.4 * 72          # letter width less the two 0.55" margins
_PAD = 9                   # 4 left + 4 right + a hair


def _grid(data, header=True):
    """A table whose columns are sized FROM THEIR CONTENT.

    Hand-picked widths are how this file first broke: "Max Treating Pressure
    (psi)" was wider than its 1.3" column, the glyphs overran the cell, and
    pdfplumber read the collision as two cells -- 'Max Treating Pressure (p'
    and 'siR)ate (bpm)'. The table still looked plausible; the Rate column
    simply stopped extracting, because _find_col() matches on startswith.

    So measure instead of guess: take each column's widest string, and if the
    total will not fit the page, step the font down rather than let anything
    clip. A header cell is never wrapped or truncated, which is the property
    the extractor depends on -- a signature word split across two lines would
    fail rows_of() the same silent way.
    """
    from reportlab.lib import colors
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Table, TableStyle
    ncol = max(len(r) for r in data)
    fs, widths = 7.4, None
    for fs in (7.4, 7.0, 6.6, 6.2, 5.8, 5.4, 5.0):
        widths = []
        for c in range(ncol):
            w = 0.0
            for i, row in enumerate(data):
                fn = "Helvetica-Bold" if (header and i == 0) else "Helvetica"
                cell = str(row[c]) if c < len(row) and row[c] is not None else ""
                w = max(w, stringWidth(cell, fn, fs))
            widths.append(w + _PAD)
        if sum(widths) <= _AVAIL:
            break
    t = Table(data, colWidths=widths, hAlign="LEFT")
    st = [("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b8bcc4")),
          ("FONTSIZE", (0, 0), (-1, -1), fs),
          ("TOPPADDING", (0, 0), (-1, -1), 2.5),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
          ("LEFTPADDING", (0, 0), (-1, -1), 4)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eefa")),
               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(st))
    return t


def _header_block(w):
    """Colon-suffixed labels, two pairs per row -- what _header() walks.

    It reads a cell ending in ':' and takes the cell to its right, matching the
    label with endswith against _HDR. Blank values are still printed as blank
    cells so the grid stays rectangular; _header ignores an empty value.
    """
    ft = lambda v: (_depth(v) + " ft") if str(v or "").strip() else ""
    rows = [["API:", w["uwi"], "Well Name:", w["well_name"]],
            ["Operator:", w["operator"], "Field:", w["field"]],
            ["County:", w["county"], "State:", w["state"]],
            ["Spud Date:", w["spud"], "Completion Date:", w["comp"]],
            ["Total Depth MD:", ft(w["td"]), "KB Elevation:", ft(w["kb"])],
            ["Surface Latitude:", _coord(w["lat"]),
             "Surface Longitude:", _coord(w["lon"])],
            ["Status:", w["status"], "Well Type:", w["well_type"]]]
    return _grid(rows, header=False)


def build_doc(path, w, dtype):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch as I
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    S = _styles()
    title = DOC_TYPES[dtype][0]
    doc = SimpleDocTemplate(path, pagesize=letter, topMargin=0.55 * I,
                            leftMargin=0.55 * I, rightMargin=0.55 * I,
                            bottomMargin=0.5 * I,
                            title=title, author=w["operator"] or "SYNTH")
    story = [Paragraph(title, S["title"]),
             Paragraph(" &middot; ".join(x for x in
                       (w["operator"], w["field"], "API " + w["uwi"]) if x),
                       S["sub"]),
             Spacer(1, 7),
             Paragraph("Well Header", S["sec"]), _header_block(w)]

    def sec(label, head, body):
        if not body:
            return
        story.append(Paragraph(label, S["sec"]))
        story.append(_grid([head] + body))

    def tops():
        sec("Formation Tops",
            ["Formation", "Top MD (ft)", "Base MD (ft)", "Net Pay (ft)", "Fluid"],
            tops_for(w))

    def casing():
        sec("Casing and Cementing Record",
            ["Casing String", "OD (in)", "Weight (lb/ft)", "Grade",
             "Shoe Depth (ft)"],
            casing_for(w))

    def dst():
        sec("Drill Stem Tests",
            ["Test Date", "Type", "Top MD (ft)", "Base MD (ft)", "Result",
             "Max Oil (bbl/d)", "Max Gas (Mcf/d)", "API Gravity"],
            dst_for(w))

    def stations():
        sec("Survey Stations",
            ["MD (ft)", "Incl (deg)", "Azi (deg)", "TVD (ft)",
             "DLS (deg/100ft)"],
            survey_for(w))

    if dtype in ("well_report", "scout"):
        tops()
        casing()
    if dtype == "scout":
        dst()
        stations()
    if dtype == "deviation":
        stations()
    if dtype == "core":
        sec("Zone Summary",
            ["Zone", "Top MD (ft)", "Base MD (ft)", "Gross (ft)",
             "Net Pay (ft)", "N/G", "Avg PHIE (v/v)", "Avg Sw (v/v)"],
            zones_for(w))
    if dtype == "completion":
        sec("Stimulation Stages",
            ["Stage", "Top MD (ft)", "Base MD (ft)", "Treatment",
             "Fluid Volume (bbl)", "Proppant (lb)",
             "Max Treating Pressure (psi)", "Rate (bpm)"],
            stages_for(w))
        casing()
    if dtype == "dst":
        dst()
    if dtype == "welltest":
        story.append(Paragraph(
            "Flow test conducted on the interval below. Rates are averages "
            "over each period.", S["sub"]))
        sec("Flow Periods",
            ["Period", "Type", "Duration (min)", "Choke", "Avg Oil (bbl/d)",
             "Avg Gas (Mcf/d)", "Avg Water (bbl/d)"],
            periods_for(w))
    if dtype == "pressure":
        sec("Pressure Points",
            ["Depth (ft MD)", "Formation", "Pressure (psi)", "Fluid",
             "Mobility (mD/cp)"],
            pressures_for(w))

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "Synthetic document generated for the Teapot Dome demo. Header values "
        "are the well's own dv_well record; measured detail is generated.",
        S["sub"]))
    doc.build(story)


# ── CLI ────────────────────────────────────────────────────────────────────

def _safe(s):
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_"
                   for ch in str(s or ""))[:40]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--wells", type=int, default=40,
                    help="how many Teapot wells to document "
                         "(default 40 of the 1,344 documentable)")
    ap.add_argument("--uwi-like", default=UWI_LIKE,
                    help="which wells are Teapot (default %s)" % UWI_LIKE)
    ap.add_argument("--types", default=",".join(TYPES),
                    help="comma list: " + ",".join(TYPES))
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()

    if a.remove:
        if not os.path.isdir(a.out):
            print("nothing at %s" % a.out)
            return 0
        n = len([f for f in os.listdir(a.out) if f.lower().endswith(".pdf")])
        if a.apply:
            shutil.rmtree(a.out)
        print("%s %d file(s) in %s" %
              ("removed" if a.apply else "would remove", n, a.out))
        print("Deleting the FILES does not remove their catalog or dv_ rows --\n"
              "those key on INVENTORY_ID; see tools/reconcile_orphans.py.")
        return 0

    types = [t.strip() for t in a.types.split(",") if t.strip()]
    bad = [t for t in types if t not in TYPES]
    if bad:
        print("unknown type(s): %s\nknown: %s" % (", ".join(bad), ", ".join(TYPES)))
        return 2

    from dataview.core.dw_utils import make_engine
    ws = teapot_wells(make_engine(a.database), a.wells, a.uwi_like)
    plan = [(w, types_for(w, types)) for w in ws]
    n_files = sum(len(t) for _w, t in plan)
    n_dry = sum(1 for w, _t in plan if is_dry(w))
    print("\n%d Teapot well(s) -> %d file(s)   (%d dry hole(s) get no %s)"
          % (len(ws), n_files, n_dry, "/".join(_COMPLETED_ONLY)))
    print("   -> %s\n" % a.out)
    for t in types:
        print("   %-12s %-38s %s" % (t, DOC_TYPES[t][0], DOC_TYPES[t][1]))
    if not a.apply:
        print("\nCOUNTS ONLY -- re-run with --apply.")
        return 0

    os.makedirs(a.out, exist_ok=True)
    made, failed = 0, 0
    for w, wtypes in plan:
        for t in wtypes:
            fn = "%s_%s_%s.pdf" % (t, _safe(w["uwi"]), _safe(w["well_name"]))
            try:
                build_doc(os.path.join(a.out, fn), w, t)
                made += 1
            except Exception as exc:
                # Name the file. A generator that reports "12 failed" without
                # saying which cannot be debugged.
                failed += 1
                print("   FAILED %s: %s" % (fn, str(exc)[:140]))
    print("\nwrote %d file(s)%s to %s"
          % (made, (", %d FAILED" % failed) if failed else "", a.out))
    print("Scan that folder from the File Catalog, or:")
    print("   python -m dataview.import_data.pipeline_run --root \"%s\" \\\n"
          "       --exts .pdf --server localhost\\SQLEXPRESS "
          "--database %s --promote --promote-apply" % (a.out, a.database))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
