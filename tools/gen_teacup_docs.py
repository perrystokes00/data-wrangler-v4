r"""Synthetic well documents for the TEACUP demo wells.

The Teacup document corpus is 1,055 files that belong to no well in
particular, so loading it fills the catalog and almost nothing else. These
documents belong to specific wells: the header of every page is the row from
synth_data\dv_well.csv, so a document loads onto a well the demo just created
and the detail tables it carries hang off that well's uwi.

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

WHAT IS REAL AND WHAT IS NOT. Header values are copied verbatim from
dv_well.csv -- uwi, name, operator, field, county and state FIPS, spud,
completion, TD, KB, latitude, longitude -- so an extracted document AGREES with
the well beside it rather than contradicting it. Placeholder values are
dropped, not printed: the synthetic generator fills unknown columns with
`<column_name>-<random>`, and a report reading "Lease: lease_name-723" is
visibly fake. Everything measured -- tops, casing, stations, zones, stages,
tests -- is generated, deterministically from the uwi, because those tables are
where documents are the ONLY source and so are where the provenance scorecard
has something to show.

TIED TO TEACUP BY LIVING INSIDE IT. Output goes under synth_docs\, so
demo_teacup.py already counts these files, already scans them with --load, and
already purges them with --reset. Nothing there needed changing.

    python tools/gen_teacup_docs.py                    # what it would write
    python tools/gen_teacup_docs.py --apply
    python tools/gen_teacup_docs.py --wells 100 --apply
    python tools/gen_teacup_docs.py --remove --apply

Load the Teacup wells FIRST. Promote holds a child row whose parent well is
missing, so documents scanned before their wells exist stay in the mirror.
"""
import argparse
import csv
import os
import random
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SYNTH = r"C:\Bulk\Synthetic\synthetic_data"
WELL_CSV = os.path.join(SYNTH, "synth_data", "dv_well.csv")
OUT_DIR = os.path.join(SYNTH, "synth_docs", "well_reports")

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
# was found to be dry -- but it was never completed and never flow tested. 80
# of the 300 Teacup wells are DRY, so issuing every well the same eight
# documents would put a frac job and a producing flow test on a quarter of the
# corpus. Wrong is worse than missing.
_COMPLETED_ONLY = ("completion", "welltest")


def types_for(w, wanted):
    if (w.get("well_type") or "").upper() == "DRY":
        return [t for t in wanted if t not in _COMPLETED_ONLY]
    return list(wanted)

# A mid-continent / Permian column, placed as a FRACTION of the well's own TD
# rather than at fixed depths -- the Teacup wells run 1,800 to 12,000 ft and a
# fixed depth list would put half the section below TD.
#                name,          top fraction, thickness fraction, pay?
COLUMN = [
    ("Ogallala",        0.03, 0.04, False), ("Yates",        0.14, 0.03, False),
    ("Seven Rivers",    0.20, 0.04, False), ("Queen",        0.27, 0.03, True),
    ("Grayburg",        0.33, 0.04, True),  ("San Andres",   0.40, 0.06, True),
    ("Glorieta",        0.49, 0.04, False), ("Clear Fork",   0.55, 0.05, True),
    ("Wichita",         0.62, 0.03, False), ("Wolfcamp",     0.67, 0.06, True),
    ("Cisco",           0.75, 0.03, False), ("Canyon",       0.79, 0.03, True),
    ("Strawn",          0.84, 0.03, True),  ("Atoka",        0.88, 0.02, False),
    ("Morrow",          0.91, 0.03, True),  ("Mississippian", 0.95, 0.03, True),
]
# name, OD in decimal inches (a fraction like 13-3/8 survives _num as "13-38"),
# weight lb/ft, grade, shoe as a fraction of TD
CASING_PLAN = [
    ("Conductor",    "20.000",  "94",   "K-55", 0.04),
    ("Surface",      "13.375",  "54.5", "K-55", 0.18),
    ("Intermediate", "9.625",   "40",   "J-55", 0.62),
    ("Production",   "5.500",   "17",   "N-80", 0.97),
]

# The synthetic generator's tell: a value that starts with the name of its own
# column. Nothing real does. See find_placeholders.sql -- same rule, one place
# it is applied on the way OUT so a fake value never reaches a printed page.
_PLACEHOLDER = re.compile(r"^[a-z_]+-\d+$")


def _clean(col, val):
    """Return val, or '' if it is the generator's <column>-<n> placeholder."""
    v = (val or "").strip()
    if not v:
        return ""
    if v.lower().startswith(col.lower() + "-") and _PLACEHOLDER.match(v.lower()):
        return ""
    return v


def _rng(uwi):
    """Deterministic per well: the same uwi always yields the same document."""
    return random.Random("teacup-doc-" + str(uwi))


def _f(v, nd=0):
    try:
        return format(float(v), ",.%df" % nd)
    except (TypeError, ValueError):
        return ""


# ── the wells ──────────────────────────────────────────────────────────────

def teacup_wells(limit):
    """Wells from the Teacup CSV -- the same source demo_teacup.py scopes by.

    Read from the CSV, not from dv_well, so this runs whether or not the demo
    is currently loaded; that is also what makes the printed header agree with
    the row the Bulk Tabular Loader will insert.

    Sampled with a stride rather than TOP n so the documents span several
    operators and fields instead of whichever block sorts first.
    """
    if not os.path.exists(WELL_CSV):
        raise SystemExit("no %s -- the Teacup well list is the input" % WELL_CSV)
    with open(WELL_CSV, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh)
                if (r.get("uwi") or "").strip()
                and (r.get("surface_latitude") or "").strip()
                and (r.get("final_td") or "").strip()]
    if limit and limit < len(rows):
        step = len(rows) / float(limit)
        rows = [rows[int(i * step)] for i in range(limit)]
    out = []
    for r in rows:
        out.append({
            "uwi": r["uwi"].strip(),
            "well_name": _clean("well_name", r.get("well_name")),
            "operator": _clean("operator_name", r.get("operator_name")),
            "field": _clean("field_name", r.get("field_name")),
            "county": _clean("county", r.get("county")),
            "state": _clean("province_state", r.get("province_state")),
            "status": _clean("well_status", r.get("well_status")),
            "well_type": _clean("well_type", r.get("well_type")),
            "spud": (r.get("spud_date") or "")[:10],
            "comp": (r.get("completion_date") or "")[:10],
            "td": r.get("final_td") or "",
            "kb": r.get("kb_elevation") or "",
            "lat": r.get("surface_latitude") or "",
            "lon": r.get("surface_longitude") or "",
        })
    return out


# ── generated content ──────────────────────────────────────────────────────

def _td(w):
    try:
        return max(800.0, float(w["td"]))
    except (TypeError, ValueError):
        return 6000.0


def tops_for(w):
    r, td = _rng(w["uwi"]), _td(w)
    out = []
    for name, ft, th, pay in COLUMN:
        top = td * (ft + r.uniform(-0.012, 0.012))
        if top < 60 or top > td - 40:
            continue
        base = min(td, top + td * th)
        out.append([name, _f(top), _f(base),
                    ("%.1f" % r.uniform(4, td * th * 0.5)) if pay else "\u2014",
                    ("OIL" if pay else "\u2014")])
    return out


def casing_for(w):
    td = _td(w)
    return [[nm, od, wt, gr, _f(td * frac)]
            for nm, od, wt, gr, frac in CASING_PLAN]


def survey_for(w, n=16):
    """MD / Incl / Azi / TVD. Signature: "md" + "azi"."""
    r, td = _rng(w["uwi"]), _td(w)
    az, md, tvd, inc = r.uniform(0, 359), 0.0, 0.0, 0.0
    step, rows = td / n, []
    for _ in range(n):
        md += step
        # Teacup wells are vertical to near-vertical; drift a degree, not a
        # landing. The loader drops a station with incl > 120 or azi > 360.
        inc = max(0.0, min(6.0, inc + r.uniform(-0.6, 0.9)))
        tvd += step * (1 - (inc / 180.0))
        az = (az + r.uniform(-8, 8)) % 360
        rows.append([_f(md), "%.2f" % inc, "%.2f" % az, _f(tvd),
                     "%.2f" % r.uniform(0, 1.4)])
    return rows


def zones_for(w):
    """Petrophysical zones. Signature: "zone" + "top"; needs dt == petro."""
    r, td = _rng(w["uwi"]), _td(w)
    out = []
    for name, ft, th, pay in COLUMN:
        if not pay:
            continue
        top = td * ft
        if top > td - 40:
            continue
        gross = td * th
        ng = r.uniform(0.25, 0.85)
        out.append([name, _f(top), _f(top + gross), "%.1f" % gross,
                    "%.1f" % (gross * ng), "%.2f" % ng,
                    "%.3f" % r.uniform(0.06, 0.22),
                    "%.3f" % r.uniform(0.18, 0.62)])
    return out


def stages_for(w):
    """Frac stages. Signature: "stage" + "top"."""
    r, td = _rng(w["uwi"]), _td(w)
    top = td * 0.72
    span = (td * 0.96 - top)
    n = r.randint(3, 8)
    return [[str(i + 1), _f(top + span * i / n), _f(top + span * (i + 1) / n),
             "SLICKWATER", _f(r.randint(1200, 9000)),
             _f(r.randint(40000, 190000)), _f(r.randint(3800, 7600)),
             "%.1f" % r.uniform(12, 42)] for i in range(n)]


def pressures_for(w):
    """RFT/MDT points. Signature: "depth" + "pressure"; needs dt == pressure."""
    r, td = _rng(w["uwi"]), _td(w)
    pays = [c for c in COLUMN if c[3] and td * c[1] < td - 40]
    grad = r.uniform(0.38, 0.46)
    out = []
    for name, ft, th, _p in pays:
        d = td * (ft + th * 0.4)
        out.append([_f(d), name, "%.1f" % (d * grad),
                    r.choice(["OIL", "OIL", "WATER", "GAS"]),
                    "%.1f" % r.uniform(0.4, 240.0)])
    return out


def dst_for(w):
    """DST records. Signature: "test date" + "result"."""
    from datetime import datetime, timedelta
    r, td = _rng(w["uwi"]), _td(w)
    base = w["comp"] or w["spud"] or "1998-06-01"
    try:
        d0 = datetime.strptime(base, "%Y-%m-%d")
    except (ValueError, TypeError):
        d0 = datetime(1998, 6, 1)
    out = []
    for i in range(r.randint(1, 3)):
        d = (d0 - timedelta(days=r.randint(3, 90))).strftime("%Y-%m-%d")
        t = td * r.uniform(0.62, 0.94)
        out.append([d, "DST", _f(t), _f(t + r.randint(20, 90)),
                    r.choice(["OIL", "OIL AND GAS", "GAS", "WATER", "TIGHT"]),
                    _f(r.randint(0, 900)), _f(r.randint(0, 2600)),
                    "%.1f" % r.uniform(28, 42)])
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
    rows = [["API:", w["uwi"], "Well Name:", w["well_name"]],
            ["Operator:", w["operator"], "Field:", w["field"]],
            ["County:", w["county"], "State:", w["state"]],
            ["Spud Date:", w["spud"], "Completion Date:", w["comp"]],
            ["Total Depth MD:", _f(w["td"]) + " ft",
             "KB Elevation:", _f(w["kb"]) + " ft"],
            ["Surface Latitude:", w["lat"], "Surface Longitude:", w["lon"]],
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
        "Synthetic document generated for the Teacup demo set. Header values "
        "are the well's own record; measured detail is generated.", S["sub"]))
    doc.build(story)


# ── CLI ────────────────────────────────────────────────────────────────────

def _safe(s):
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_"
                   for ch in str(s or ""))[:40]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wells", type=int, default=40,
                    help="how many Teacup wells to document (default 40 of 300)")
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
        print("Catalog rows for them go with `demo_teacup.py --reset --only docs`.")
        return 0

    types = [t.strip() for t in a.types.split(",") if t.strip()]
    bad = [t for t in types if t not in TYPES]
    if bad:
        print("unknown type(s): %s\nknown: %s" % (", ".join(bad), ", ".join(TYPES)))
        return 2

    ws = teacup_wells(a.wells)
    plan = [(w, types_for(w, types)) for w in ws]
    n_files = sum(len(t) for _w, t in plan)
    n_dry = sum(1 for w, _t in plan if (w["well_type"] or "").upper() == "DRY")
    print("\n%d Teacup well(s) -> %d file(s)   (%d dry hole(s) get no %s)"
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
    print("Load them with the rest of the documents:")
    print("   python tools/demo_teacup.py --load --only docs --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
