"""
dataview/migration/synth_docs.py
===============================
Synthetic well documents for File Catalog testing, modelled on real reports.

WHY THE DETAIL MATTERS
----------------------
An earlier version of this produced a header block and one table per document.
That exercises "can we find a UWI in a PDF" and nothing else. Real well
documents are dense — a casing and cementing record carries a string programme,
a cement job summary AND a CBL evaluation; an end-of-well report carries a
stratigraphic column, an NPT ledger with cost categories, and a completion
summary. Extractors that cope with a thin document routinely fail on a real one,
because the real failure modes are multi-table pages, repeated column headers,
numbers that look like depths but aren't, and units baked into values.

So these follow the shape of actual reports: several tables per document,
realistic magnitudes, and the incidental noise — confidentiality footers,
service company names, interpreter credentials — that surrounds the data you
actually want.

GROUND TRUTH
------------
Every document is generated from a known well, and MANIFEST.csv records the UWI
each file should resolve to plus the case it represents. That makes catalog
testing a score — matched correctly / matched wrongly / not matched — rather
than an impression. A confidently wrong UWI is worse than none: it attaches a
document to another operator's well and nothing complains.

THE HARD CASES ARE DELIBERATE
-----------------------------
A share of the output is difficult on purpose, each labelled in the manifest:
UWI only in the filename; only in the text; dashed API rather than 14-digit; a
UWI belonging to no well; none at all; two wells in one document; and an
image-only scan with no text layer. Worth noting the real reports these were
modelled on had EMPTY "UWI / API:" fields — a document naming an operator, a
field and a county but never its own well is common, so it belongs in the set.

USAGE (from the repo root)
--------------------------
    py -m dataview.migration.synth_docs --wells-csv C:\\synth\\dv_well.csv ^
        --out C:\\synth_docs --per-well 3
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import random
from datetime import date, timedelta

HARD_CASE_SHARE = 0.22

# Stratigraphy by region, so a Texas well doesn't come back with Kansas tops.
STRAT_PERMIAN = [
    ("Rustler", 1240, ""), ("Salado", 1420, "Salt section"),
    ("Castile", 3310, ""), ("Bell Canyon", 3730, ""),
    ("Cherry Canyon", 4070, ""), ("Brushy Canyon", 4550, ""),
    ("Bone Spring 1st", 5070, "Shows"), ("Bone Spring 2nd", 5410, "Shows"),
    ("Bone Spring 3rd", 5980, ""), ("Wolfcamp A", 6840, "TARGET"),
    ("Wolfcamp B", 7320, ""), ("Wolfcamp C", 7940, ""),
    ("Dean", 8460, ""), ("Spraberry Upper", 8890, ""),
]
STRAT_MIDCON = [
    ("Stone Corral", 1450, "Anhydrite"), ("Hutchinson Salt", 1600, "Salt"),
    ("Chase", 2400, ""), ("Council Grove", 2700, ""), ("Admire", 2900, ""),
    ("Lansing", 3200, ""), ("Kansas City", 3350, ""), ("Marmaton", 3600, ""),
    ("Cherokee", 3750, "TARGET"), ("Mississippian", 3950, "Shows"),
    ("Arbuckle", 4300, ""), ("Precambrian", 4600, "Basement"),
]
FIELDS_BY_STATE = {
    "42": ["Spraberry Trend", "Delaware Basin", "Midland Basin", "Wolfcamp Shale"],
    "35": ["SCOOP/STACK", "Anadarko Basin", "Cana-Woodford"],
    "30": ["Delaware Basin", "Northwest Shelf"],
    "15": ["Hugoton", "El Dorado", "Chase-Silica", "Trapp"],
}
_ABBR = {"42": "TX", "35": "OK", "30": "NM", "15": "KS", "05": "CO", "49": "WY"}
SERVICE_COS = ["Halliburton", "Schlumberger", "Baker Hughes", "Weatherford"]
RIGS = ["Patterson UTI #219", "Helmerich & Payne #451", "Nabors X-12",
        "Precision Drilling #308", "Cactus Rig #145"]
INTERPRETERS = ["J. Rodriguez, M.Sc. Petrophysics", "A. Whitfield, P.Geo.",
                "M. Okonkwo, Senior Petrophysicist", "L. Trevino, M.Sc."]

CASING_PROGRAMME = [
    ("Conductor",    '20"',      94,   "K-55",  0.016, "Float shoe"),
    ("Surface",      '13-3/8"',  54.5, "K-55",  0.17,  "Float shoe + collar"),
    ("Intermediate", '9-5/8"',   47,   "L-80",  0.55,  "Float shoe + collar"),
    ("Production",   '5-1/2"',   20,   "P-110", 1.00,  "Float shoe + collar"),
]
NPT_EVENTS = [
    ("Stuck pipe — worked free", "Formation-related"),
    ("BHA twist-off — fishing op", "Mechanical"),
    ("Lost circulation — cement squeeze", "Formation-related"),
    ("MWD failure — replacement", "Equipment"),
    ("Waiting on weather", "Weather"),
    ("Mud motor failure", "Equipment"),
]
CURVES = ["GR", "CALI", "SP", "RILD", "RT", "RHOB", "NPHI", "DPHI", "PEF",
          "DT", "DTSM"]


def _dashed(uwi):
    return f"{uwi[:2]}-{uwi[2:5]}-{uwi[5:10]}" if len(uwi) >= 10 else uwi


def _long_uwi(uwi):
    """42-317-12345-00-00 — the form the Word examples used."""
    return f"{_dashed(uwi)}-00-00"


def _st(w):
    return str(w.get("province_state", ""))[:2]


def _strat(w):
    return STRAT_PERMIAN if _st(w) in ("42", "30") else STRAT_MIDCON


def _field(w, rng):
    return rng.choice(FIELDS_BY_STATE.get(_st(w), ["Unnamed Field"]))


def _abbr(w):
    return _ABBR.get(_st(w), "US")


# --------------------------------------------------------------------------- #
# PDF scaffolding
# --------------------------------------------------------------------------- #
def _pdf(path, title, subtitle, ident_rows, blocks, footer):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=15, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9,
                         textColor=colors.HexColor("#555555"), spaceAfter=10)
    hd = ParagraphStyle("hd", parent=ss["Heading2"], fontSize=10.5, spaceBefore=9)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=8.5, leading=11)
    foot = ParagraphStyle("foot", parent=ss["Normal"], fontSize=7,
                          textColor=colors.HexColor("#777777"), spaceBefore=13)

    story = [Paragraph(title, h1), Paragraph(subtitle, sub)]
    if ident_rows:
        pairs, row = [], []
        for k, v in ident_rows:
            row += [k, str(v)]
            if len(row) == 4:
                pairs.append(row)
                row = []
        if row:
            pairs.append(row + [""] * (4 - len(row)))
        t = Table(pairs, colWidths=[1.25*inch, 2.1*inch, 1.25*inch, 2.1*inch])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ]))
        story += [t, Spacer(1, 6)]

    for heading, content in blocks:
        if heading:
            story.append(Paragraph(heading, hd))
        if isinstance(content, tuple):
            head, rows, widths = content
            t = Table([head] + rows, repeatRows=1,
                      colWidths=[x*inch for x in widths] if widths else None)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e6e6")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.2),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999999")),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(t)
        else:
            story.append(Paragraph(content, body))
        story.append(Spacer(1, 4))
    story.append(Paragraph(footer, foot))
    SimpleDocTemplate(path, pagesize=letter, title=title, leftMargin=44,
                      rightMargin=44, topMargin=44, bottomMargin=36).build(story)


def _ident(w, shown, rng, extra=()):
    return [("Operator", w.get("operator_name", "")),
            ("Well Name", w.get("well_name", "")),
            ("UWI / API", shown),
            ("Field", _field(w, rng)),
            ("State", _abbr(w)), ("County", w.get("county", ""))] + list(extra)


# --------------------------------------------------------------------------- #
# PDF document types
# --------------------------------------------------------------------------- #
def scout_ticket(path, w, rng, shown):
    td = float(w.get("final_td") or 12000)
    tvd = round(td * rng.uniform(0.45, 0.62))
    casing = [[n, od, wt, gr, f"{round(td*fr):,}", f"{rng.randint(100,900):,}"]
              for n, od, wt, gr, fr, _ in CASING_PROGRAMME]
    n_st = rng.randint(14, 42)
    stages, top = [], tvd + rng.uniform(50, 300)
    for i in range(1, n_st + 1):
        if i in (1, 2, 5, 10, 15, 20, n_st):
            stages.append([i, f"{top:,.0f}", f"{top+350:,.0f}", "350",
                           f"{rng.randint(3000,3600):,}",
                           f"{rng.choice([450000,500000,550000]):,}",
                           f"{rng.randint(8100,8500):,}", rng.randint(58, 64)])
        top += (td - tvd) / n_st
    ip, q, d0 = [], rng.uniform(900, 2100), date(2024, 6, 20)
    for days in (5, 35, 65, 95):
        gas = q * rng.uniform(1.4, 1.9)
        ip.append([(d0 + timedelta(days=days)).isoformat(), days, f"{q:,.0f}",
                   f"{gas:,.0f}", f"{q*rng.uniform(0.15,0.55):,.0f}",
                   f"{gas/q*1000:,.0f}", f"{rng.choice([32,40,48,56])}/64",
                   f"{rng.randint(3200,4300):,}"])
        q *= rng.uniform(0.78, 0.88)
    _pdf(path, "WELL SCOUT TICKET", "IHS Markit · Basin Intelligence",
         _ident(w, shown, rng, [
             ("Well Type", f"{w.get('well_type','Oil')} — Horizontal"),
             ("Status", w.get("well_status", "Producing")),
             ("Spud Date", w.get("spud_date", "")),
             ("Completion Date", w.get("completion_date", "")),
             ("Rig", rng.choice(RIGS)),
             ("KB Elevation", f"{w.get('kb_elevation','')} ft"),
             ("Total Depth", f"{td:,.0f} ft MD"), ("TVD", f"{tvd:,.0f} ft"),
             ("Lateral Length", f"{td-tvd:,.0f} ft"),
             ("Azimuth", f"{rng.randint(0,359)}°")]),
         [("Completion Information",
           (["Casing String", "Size (in)", "Weight (ppf)", "Grade",
             "Set Depth (ft MD)", "Cement (sacks)"], casing,
            [1.1, 0.8, 1.0, 0.7, 1.3, 1.2])),
          ("Perforation &amp; Stimulation",
           (["Stage #", "Top (ft MD)", "Base (ft MD)", "Interval (ft)",
             "Fluid (bbl)", "Proppant (lbs)", "Max Press (psi)", "Rate (bpm)"],
            stages, [0.6, 0.9, 0.9, 0.8, 0.85, 1.05, 1.0, 0.7])),
          ("Initial Production (IP)",
           (["Test Date", "Days On", "Oil (bbl/d)", "Gas (Mcf/d)",
             "Water (bbl/d)", "GOR (scf/bbl)", "Choke (in)", "WHP (psi)"], ip,
            [0.95, 0.7, 0.9, 0.9, 0.95, 1.0, 0.75, 0.8]))],
         f"CONFIDENTIAL — {w.get('operator_name','')} — "
         f"{w.get('well_name','')} — Scout Ticket 2024")


def end_of_well(path, w, rng, shown):
    td = float(w.get("final_td") or 12000)
    tvd = round(td * rng.uniform(0.45, 0.62))
    try:
        spud = date.fromisoformat(str(w.get("spud_date"))[:10])
    except Exception:
        spud = date(2024, 1, 8)
    days = rng.randint(95, 165)
    tops, prev = [], 0.0
    for name, nominal, note in _strat(w):
        if nominal > tvd * 0.98:
            break
        d = nominal * rng.uniform(0.9, 1.1)
        if d <= prev:
            continue
        prev = d
        tops.append([name, f"{d*td/max(tvd,1):,.0f}", f"{d:,.0f}",
                     f"{rng.randint(120,900):,}", note])
    npt, th, tc = [], 0, 0
    for day, (ev, cat) in zip(sorted(rng.sample(range(8, days), 4)),
                              rng.sample(NPT_EVENTS, 4)):
        h = rng.choice([12, 18, 24, 36, 48])
        c = round(h * rng.uniform(6, 9))
        th += h
        tc += c
        npt.append([day, ev, f"{rng.uniform(0.2,0.9)*td:,.0f}", h, c, cat])
    npt.append(["", "TOTAL NPT", "", th, tc, ""])
    n_st = rng.randint(24, 48)
    _pdf(path, "END OF WELL REPORT", "Final Well Summary",
         _ident(w, shown, rng, [
             ("Spud Date", spud.isoformat()),
             ("Rig Release", (spud + timedelta(days=days)).isoformat()),
             ("Total Depth", f"{td:,.0f} ft MD"), ("TVD", f"{tvd:,.0f} ft"),
             ("Lateral", f"{td-tvd:,.0f} ft"),
             ("Azimuth", f"{rng.randint(0,359)}°"), ("Elapsed Days", days),
             ("AFE Days", days - rng.randint(-12, 14)),
             ("Actual Cost", f"${rng.uniform(6,16):.1f} MM"),
             ("AFE Cost", f"${rng.uniform(6,15):.1f} MM")]),
         [("Stratigraphic Summary",
           (["Formation", "Top (ft MD)", "Top (ft TVD)", "Thickness (ft)",
             "Note"], tops, [1.5, 1.0, 1.0, 1.1, 1.6])),
          ("Drilling Events &amp; NPT",
           (["Day", "Event", "MD (ft)", "Duration (hrs)", "Cost ($K)",
             "Category"], npt, [0.5, 2.4, 0.9, 1.0, 0.85, 1.3])),
          ("Completion Summary",
           f"Stages completed: {n_st} of {n_st} planned. Total fluid: "
           f"{rng.randint(80,160)*1000:,} bbl. Total proppant: "
           f"{rng.randint(12,24)*1000000:,} lbs. Average stage spacing: "
           f"{rng.randint(240,340)} ft. Cluster spacing: "
           f"{rng.choice([25,30,35])} ft. Clusters per stage: "
           f"{rng.choice([4,5,6])}.")],
         f"CONFIDENTIAL — {w.get('operator_name','')} — "
         f"{w.get('well_name','')} — End of Well Report 2024")


def casing_cement(path, w, rng, shown):
    td = float(w.get("final_td") or 12000)
    tvd = round(td * rng.uniform(0.45, 0.62))
    prog = [[n, od, wt, gr, f"{round(td*fr):,}", f"{min(round(td*fr), tvd):,}",
             rng.randint(3, 220), fe] for n, od, wt, gr, fr, fe in CASING_PROGRAMME]
    toc = round(td * rng.uniform(0.3, 0.5))
    job = [["Cement Type", "Class H + Silica Flour", "Lead Slurry",
            f"{rng.uniform(12.8,14.2):.1f} ppg"],
           ["Tail Slurry", f"{rng.uniform(15.8,16.8):.1f} ppg",
            "Top of Cement (TOC)", f"{toc:,} ft MD"],
           ["Sacks Lead", f"{rng.randint(900,1600):,}", "Sacks Tail",
            f"{rng.randint(400,900):,}"],
           ["Mix Water (bbl)", rng.randint(300, 520), "Displacement (bbl)",
            rng.randint(600, 980)],
           ["BHCT (°F)", rng.randint(180, 240), "BHST (°F)",
            rng.randint(200, 260)],
           ["WOC Time (hrs)", 24, "Thickening Time",
            f"{rng.uniform(4.5,8.0):.1f} hrs"],
           ["24-hr Compressive Strength", f"{rng.randint(2400,3800):,} psi",
            "48-hr Strength", f"{rng.randint(4000,5600):,} psi"]]
    cbl, d = [], round(td * 0.17)
    while d < td and len(cbl) < 6:
        nxt = min(td, d + rng.uniform(1200, 3400))
        amp = rng.randint(6, 26)
        bond = ("Excellent (&gt;80%)" if amp < 10 else "Good (70–80%)"
                if amp < 16 else "Moderate (50–70%)")
        cbl.append([f"{d:,.0f}–{nxt:,.0f}", amp, bond,
                    "Possible microannulus" if amp > 18
                    else ("Lateral section" if d > tvd else "")])
        d = nxt
    _pdf(path, "CASING &amp; CEMENTING RECORD",
         f"{rng.choice(SERVICE_COS)} Cementing Services", _ident(w, shown, rng),
         [("Casing Programme",
           (["String", "OD (in)", "Weight (ppf)", "Grade", "Shoe MD (ft)",
             "Shoe TVD (ft)", "Centralizers", "Float Equipment"], prog,
            [1.0, 0.7, 0.9, 0.6, 0.95, 0.95, 0.95, 1.3])),
          ("Cement Job Summary — Production String",
           (["Parameter", "Value", "Parameter", "Value"], job,
            [1.7, 1.5, 1.7, 1.5])),
          ("Cement Evaluation (CBL/VDL)",
           (["Interval (ft MD)", "CBL Amplitude (mV)", "Cement Bond", "Note"],
            cbl, [1.6, 1.4, 1.6, 1.8]))],
         f"CONFIDENTIAL — {w.get('operator_name','')} — "
         f"{w.get('well_name','')} — Casing &amp; Cementing Record 2024")


def well_test(path, w, rng, shown):
    td = float(w.get("final_td") or 12000)
    tvd = round(td * rng.uniform(0.45, 0.62))
    zone = _strat(w)[-4][0]
    pres = rng.randint(3800, 5200)
    flow, q = [], rng.uniform(700, 1200)
    for i, (kind, hrs) in enumerate([("Flow", 8), ("Shut-in", 4), ("Flow", 8),
                                     ("Shut-in", 4), ("Flow", 8),
                                     ("Buildup", 24)], start=1):
        if kind == "Flow":
            gas = q * rng.uniform(1.4, 1.8)
            flow.append([i, kind, hrs, f"{rng.choice([24,32,40,48])}/64",
                         f"{q:,.0f}", f"{gas:,.0f}",
                         f"{q*rng.uniform(0.08,0.14):,.0f}",
                         f"{rng.randint(3100,3900):,}",
                         f"{rng.randint(3700,4300):,}"])
            q *= rng.uniform(1.25, 1.55)
        else:
            flow.append([i, kind, hrs, "—", "—", "—", "—", "—",
                         f"{pres-rng.randint(0,120):,}"])
    api_g, gor = rng.uniform(38, 48), rng.randint(900, 2200)
    res = [["Static Reservoir Pressure", f"{pres:,}", "psi",
            "Horner plot extrapolation"],
           ["Formation Permeability (k)", f"{rng.uniform(0.05,2.4):.2f}", "mD",
            "Buildup analysis"],
           ["Skin Factor (S)", f"{rng.uniform(-5,2):.1f}", "—",
            "Buildup analysis (stimulated)"],
           ["Productivity Index (PI)", f"{rng.uniform(0.1,0.9):.2f}",
            "bbl/d/psi", "Flow period 3"],
           ["Drainage Radius", f"{rng.randint(1200,3600):,}", "ft",
            "Transient analysis"],
           ["Reservoir Temperature", rng.randint(160, 240), "°F",
            "BHT measured"],
           ["Fluid Gravity", f"{api_g:.1f}", "°API", "Surface sample"],
           ["GOR", f"{gor:,}", "scf/bbl", "Flow period 3 average"]]
    samples = [["Separator oil", "Separator", 85, 120, f"{api_g:.1f}",
                f"{gor:,}", "0.2"],
               ["Separator gas", "Separator", 85, 120, "—", "—", "—"],
               ["Recombined", "Lab", rng.randint(190, 235), f"{pres:,}",
                f"{api_g-rng.uniform(0.5,2):.1f}",
                f"{gor+rng.randint(50,200):,}", "—"]]
    _pdf(path, "WELL TEST REPORT", "Production Test — Multi-Rate Flow Test",
         _ident(w, shown, rng, [
             ("Test Date", w.get("completion_date", "")),
             ("Test Type", "Multi-Rate Flow Test"), ("Zone", zone),
             ("Perforations", f"{tvd:,.0f}–{td:,.0f} ft MD"),
             ("Tubing", '2-7/8" EUE'), ("Separator", "Portable 3-phase")]),
         [("Flow Test Summary",
           (["Period", "Type", "Hrs", "Choke (in)", "Oil (bbl/d)",
             "Gas (Mcf/d)", "Water (bbl/d)", "FWHP (psi)", "FBHP (psi)"], flow,
            [0.55, 0.75, 0.5, 0.8, 0.9, 0.95, 1.0, 0.9, 0.9])),
          ("Reservoir Analysis",
           (["Parameter", "Value", "Units", "Method"], res,
            [1.9, 1.0, 0.9, 2.4])),
          ("Fluid Samples",
           (["Sample Type", "Collection Point", "Temp (°F)", "Press (psi)",
             "API Gravity", "GOR (scf/bbl)", "BS&amp;W (%)"], samples,
            [1.2, 1.3, 0.85, 0.95, 1.0, 1.15, 0.85]))],
         f"CONFIDENTIAL — {w.get('operator_name','')} — "
         f"{w.get('well_name','')} — Well Test Report 2024")


def petrophysics(path, w, rng, shown):
    td = float(w.get("final_td") or 12000)
    tvd = round(td * rng.uniform(0.45, 0.62))
    top = max(500.0, tvd - rng.uniform(600, 1400))
    zones, detail = [], []
    for name, _n, _x in _strat(w)[-5:]:
        gross = rng.uniform(220, 460)
        ng = rng.uniform(0.24, 0.78)
        zones.append([name, f"{top:,.0f}", f"{top+gross:,.0f}", f"{gross:,.0f}",
                      f"{gross*ng:,.0f}", f"{ng:.2f}",
                      f"{rng.uniform(4.0,9.5):.1f}", f"{rng.uniform(20,70):.0f}",
                      f"{rng.uniform(20,58):.0f}",
                      "High" if ng > 0.65 else "Moderate" if ng > 0.4 else "Low"])
        top += gross
    d = float(zones[0][1].replace(",", ""))
    for _ in range(8):
        detail.append([f"{d:,.0f}", f"{rng.uniform(48,92):.0f}",
                       f"{rng.uniform(2.38,2.58):.2f}",
                       f"{rng.uniform(9,16):.1f}", f"{rng.uniform(8,32):.1f}",
                       f"{rng.uniform(5,11):.1f}", f"{rng.uniform(16,42):.0f}",
                       f"{rng.uniform(20,42):.0f}",
                       rng.choice(["Silt/Lime", "Calcareous shale", "Shale",
                                   "Lime-rich silt", "Lime silt"])])
        d += 50
    cutoffs = [["Vcl (clay volume)", "&lt; 50%", "Core-log calibration"],
               ["PHIE (effective porosity)", "&gt; 4%", "Core plug analysis"],
               ["SW (water saturation)", "&lt; 65%",
                f"Archie, m={rng.uniform(1.7,2.0):.1f} n=2.0 "
                f"Rw={rng.uniform(0.02,0.08):.2f}"],
               ["RT (resistivity)", "&gt; 8 ohm·m", "Pickett plot"]]
    _pdf(path, "PETROPHYSICAL INTERPRETATION REPORT",
         f"{rng.choice(SERVICE_COS)} Petrotechnical Services",
         _ident(w, shown, rng, [
             ("Log Suite", " · ".join(rng.sample(CURVES, 6))),
             ("Interval", f"{zones[0][1]}–{zones[-1][2]} ft MD"),
             ("Interpreter", rng.choice(INTERPRETERS)),
             ("Date", w.get("completion_date", ""))]),
         [("Zone Summary",
           (["Zone", "Top (ft)", "Base (ft)", "Gross (ft)", "Net Pay (ft)",
             "N/G", "PHIE (%)", "SW (%)", "Vcl (%)", "HC Pore Vol"], zones,
            [1.35, 0.72, 0.72, 0.68, 0.78, 0.5, 0.68, 0.6, 0.6, 0.85])),
          ("Detailed Interval Analysis",
           (["Depth (ft)", "GR (API)", "RHOB (g/cc)", "NPHI (%)",
             "RT (ohm·m)", "PHIE (%)", "SW (%)", "Vcl (%)", "Lithology"],
            detail, [0.8, 0.75, 0.9, 0.75, 0.85, 0.75, 0.65, 0.65, 1.35])),
          ("Cutoff Criteria Applied",
           (["Parameter", "Cutoff Value", "Basis"], cutoffs, [1.9, 1.2, 3.1]))],
         f"CONFIDENTIAL — {w.get('operator_name','')} — "
         f"{w.get('well_name','')} — Petrophysical Report 2024")


def dir_survey(path, w, rng, shown):
    td = float(w.get("final_td") or 12000)
    rows, md, incl, azi, tvd, ns, ew = [], 0.0, 0.0, rng.uniform(0, 360), 0.0, 0.0, 0.0
    while md < td and len(rows) < 34:
        step = rng.uniform(90, 160)
        md += step
        incl = min(90.0, incl + rng.uniform(0, 4.2))
        azi = (azi + rng.uniform(-3, 3)) % 360
        tvd += step * math.cos(math.radians(incl))
        ns += step * math.sin(math.radians(incl)) * math.cos(math.radians(azi))
        ew += step * math.sin(math.radians(incl)) * math.sin(math.radians(azi))
        rows.append([f"{md:,.0f}", f"{incl:.2f}", f"{azi:.2f}", f"{tvd:,.0f}",
                     f"{ns:,.0f}", f"{ew:,.0f}", f"{math.hypot(ns, ew):,.0f}",
                     f"{rng.uniform(0,3.5):.2f}"])
    _pdf(path, "DIRECTIONAL SURVEY REPORT",
         f"{rng.choice(SERVICE_COS)} Drilling Services",
         _ident(w, shown, rng, [
             ("Survey Type", rng.choice(["GYRO", "MWD", "MAGNETIC"])),
             ("Calculation", "Minimum curvature"), ("Depth Reference", "KB"),
             ("Survey Date", w.get("spud_date", ""))]),
         [("Survey Stations",
           (["MD (ft)", "Incl (°)", "Azim (°)", "TVD (ft)", "N/S (ft)",
             "E/W (ft)", "Closure (ft)", "DLS (°/100ft)"], rows,
            [0.85, 0.8, 0.85, 0.85, 0.85, 0.85, 0.95, 1.1]))],
         f"CONFIDENTIAL — {w.get('operator_name','')} — "
         f"{w.get('well_name','')} — Directional Survey 2024")


def image_only_pdf(path, w, rng):
    """A scan with no text layer — pdftotext returns nothing. The catalog should
    record 'no text extracted', not 'no well found'. Different failures needing
    different handling, and easy to conflate."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1275, 1650), "white")
    d = ImageDraw.Draw(img)
    d.text((90, 80), "WELL SCOUT TICKET   (scanned)", fill="black")
    d.text((90, 130), f"API {_dashed(w['uwi'])}", fill="black")
    d.text((90, 175), w.get("well_name", ""), fill="black")
    d.text((90, 220), w.get("operator_name", ""), fill="black")
    for i in range(22):
        d.line((90, 280 + i*40, 1180, 280 + i*40), fill=(205, 205, 205))
    c = canvas.Canvas(path, pagesize=letter)
    c.drawImage(ImageReader(img), 0, 0, width=612, height=792)
    c.save()


# --------------------------------------------------------------------------- #
# LAS files
# --------------------------------------------------------------------------- #
# Bed types and the log response each produces. Values are centres; the curve
# generator walks toward them rather than sampling them independently.
BEDS = {
    #            GR    RHOB  NPHI   RT    DT    PEF   SP
    "shale":    (105,  2.52, 0.34,   4,  105,  3.2, -12),
    "sand":     ( 42,  2.31, 0.22,  28,   82,  2.0, -68),
    "limestone":( 22,  2.68, 0.06, 180,   50,  5.1,  -6),
    "dolomite": ( 28,  2.80, 0.03, 240,   45,  3.1,  -8),
    "anhydrite":( 14,  2.95, 0.01, 900,   50,  5.0,   0),
    "coal":     ( 38,  1.45, 0.48,  90,  130,  0.2, -30),
}
BED_SEQ = ["shale", "sand", "shale", "limestone", "shale", "dolomite",
           "sand", "shale", "limestone", "shale"]


def _walk(prev, target, step_ft, rng, sd, tightness=0.06):
    """One sample of a depth-correlated curve.

    A log is not white noise: adjacent samples half a foot apart are strongly
    correlated, because the tool has a vertical resolution of a foot or more and
    the rock itself changes gradually within a bed. Sampling each depth
    independently — which the example LAS did — produces something that passes a
    format check and fails any statistic computed over it, and looks obviously
    wrong to anyone who reads logs.

    So each sample pulls toward the bed's characteristic value and adds a small
    excursion, giving autocorrelation within beds and a step at each boundary.
    """
    pull = (target - prev) * tightness * max(1.0, step_ft / 0.5)
    return prev + pull + rng.gauss(0, sd)


def las_file(path, w, rng, shown="full", wrap=False, version="2.0",
             units="FT", null_val=-999.25):
    """A LAS 2.0 file with lithology-driven, depth-correlated curves."""
    td = float(w.get("final_td") or 9000)
    start = round(td * rng.uniform(0.12, 0.25), 1)
    stop = round(td * rng.uniform(0.90, 0.99), 1)
    step = 0.5 if units == "FT" else 0.1524          # 0.5 ft, or its metric twin
    if units == "M":
        start, stop = round(start * 0.3048, 2), round(stop * 0.3048, 2)

    uwi_txt = {"full": w["uwi"], "dashed": _long_uwi(w["uwi"]), "none": ""}[shown]
    curves = [("DEPT", units, "DEPTH"), ("GR", "GAPI", "Gamma Ray"),
              ("CALI", "IN", "Caliper"), ("SP", "MV", "Spontaneous Potential"),
              ("RHOB", "G/CC", "Bulk Density"), ("NPHI", "V/V", "Neutron Porosity"),
              ("RT", "OHMM", "True Resistivity"), ("DT", "US/F", "Sonic Delta-T"),
              ("PEF", "B/E", "Photoelectric Factor")]

    # Bed boundaries down the logged interval — thick enough to be readable.
    n = int((stop - start) / step) + 1
    bounds, d = [], start
    seq = BED_SEQ * 6
    i = 0
    while d < stop:
        thick = rng.uniform(40, 260) * (1 if units == "FT" else 0.3048)
        bounds.append((d, min(d + thick, stop), seq[i % len(seq)]))
        d += thick
        i += 1

    hdr = [
        "~VERSION INFORMATION",
        f" VERS.                 {version}   : CWLS Log ASCII Standard - VERSION {version}",
        f" WRAP.                 {'YES' if wrap else 'NO'}    : "
        f"{'MULTIPLE LINES PER DEPTH STEP' if wrap else 'ONE LINE PER DEPTH STEP'}",
        "",
        "~WELL INFORMATION",
        f" UWI .                 {uwi_txt:<30}: UNIQUE WELL IDENTIFIER",
        f" WELL.                 {w.get('well_name',''):<30}: WELL NAME",
        f" COMP.                 {w.get('operator_name',''):<30}: COMPANY",
        f" FLD .                 {_field(w, rng):<30}: FIELD",
        f" SRVC.                 {rng.choice(SERVICE_COS).upper():<30}: SERVICE COMPANY",
        f" DATE.                 {w.get('spud_date',''):<30}: LOG DATE",
        f" STRT.{units:<17}{start:<30}: START DEPTH",
        f" STOP.{units:<17}{stop:<30}: STOP DEPTH",
        f" STEP.{units:<17}{step:<30}: STEP",
        f" NULL.                 {null_val:<30}: NULL VALUE",
        f" CNTY.                 {w.get('county',''):<30}: COUNTY",
        f" STAT.                 {_abbr(w):<30}: STATE",
        " CTRY.                 US                            : COUNTRY",
        f" API .                 {_dashed(w['uwi']) if shown != 'none' else '':<30}: API NUMBER",
        f" LOG_ID.               LOG_{w['uwi']}_1{'':<10}: LOG ID",
        "",
        "~PARAMETER INFORMATION",
        f" RUN .                 {rng.randint(1,3):<30}: RUN NUMBER",
        f" EKB .{units:<17}{w.get('kb_elevation',''):<30}: KELLY BUSHING ELEVATION",
        f" EGL .{units:<17}{w.get('ground_elevation',''):<30}: GROUND LEVEL ELEVATION",
        f" TDD .{units:<17}{td:<30}: DRILLER TOTAL DEPTH",
        f" BHT .DEGF             {rng.randint(140,240):<30}: BOTTOM HOLE TEMPERATURE",
        f" MUD .                 {rng.choice(['WBM','OBM','SBM']):<30}: MUD TYPE",
        f" MDEN.G/CC             {rng.uniform(1.05,1.35):<30.2f}: MUD DENSITY",
        f" MATR.                 {rng.choice(['SAND','LIME','DOLO']):<30}: MATRIX FOR NEUTRON",
        "",
        "~CURVE INFORMATION",
    ]
    for m, u, d_ in curves:
        hdr.append(f" {m:<5}.{u:<16}: {d_}")
    hdr += ["", "~A  " + "  ".join(m for m, _u, _d in curves)]

    # Curve state, seeded at the first bed's characteristics
    b0 = BEDS[bounds[0][2]]
    cur = {"GR": b0[0], "RHOB": b0[1], "NPHI": b0[2], "RT": b0[3],
           "DT": b0[4], "PEF": b0[5], "SP": b0[6], "CALI": 8.5}
    sd = {"GR": 4.0, "RHOB": 0.012, "NPHI": 0.006, "RT": 0.9, "DT": 1.2,
          "PEF": 0.06, "SP": 1.5, "CALI": 0.05}

    lines, d, bi = [], start, 0
    hole = 8.5
    while d <= stop + 1e-6:
        while bi + 1 < len(bounds) and d > bounds[bi][1]:
            bi += 1
        t = BEDS[bounds[bi][2]]
        tgt = {"GR": t[0], "RHOB": t[1], "NPHI": t[2], "RT": t[3],
               "DT": t[4], "PEF": t[5], "SP": t[6],
               # washout in shale, on gauge elsewhere — a caliper that never
               # moves is a giveaway that the file is synthetic
               "CALI": hole + (1.4 if bounds[bi][2] == "shale" else 0.05)}
        for k in cur:
            cur[k] = _walk(cur[k], tgt[k], step, rng, sd[k])
        cur["NPHI"] = max(0.005, cur["NPHI"])
        cur["RHOB"] = max(1.4, cur["RHOB"])
        cur["RT"] = max(0.2, cur["RT"])
        vals = [d, cur["GR"], cur["CALI"], cur["SP"], cur["RHOB"],
                cur["NPHI"], cur["RT"], cur["DT"], cur["PEF"]]
        # A real log has gaps — tool off-bottom, bad hole, a curve not recorded
        if rng.random() < 0.004:
            k = rng.randint(1, len(vals) - 1)
            vals[k] = null_val
        if wrap:
            lines.append(f"{vals[0]:>10.4f}")
            lines.append("   " + "".join(f"{v:>12.4f}" for v in vals[1:]))
        else:
            lines.append("".join(f"{v:>12.4f}" for v in vals))
        d += step

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(hdr) + "\n" + "\n".join(lines) + "\n")
    return len(lines)


# --------------------------------------------------------------------------- #
# Office documents
# --------------------------------------------------------------------------- #
def completion_docx(path, w, rng, shown, extra_uwi=None):
    from docx import Document
    doc = Document()
    doc.add_heading("WELL COMPLETION REPORT", 0)
    doc.add_paragraph("CONFIDENTIAL — For Operator Use Only")
    td = float(w.get("final_td") or 12000)
    tvd = round(td * rng.uniform(0.45, 0.62))
    zone = _strat(w)[-4][0]

    doc.add_heading("1. Well Identification", level=1)
    t = doc.add_table(rows=1, cols=2)
    t.style = "Light Grid Accent 1"
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Field", "Value"
    for k, v in (("Well Name", w.get("well_name", "")),
                 ("UWI", _long_uwi(w["uwi"]) if shown != "none" else ""),
                 ("API Number", _dashed(w["uwi"]) if shown != "none" else ""),
                 ("Operator", w.get("operator_name", "")),
                 ("Field", _field(w, rng)), ("County", w.get("county", "")),
                 ("State", _abbr(w)), ("Spud Date", w.get("spud_date", "")),
                 ("Completion Date", w.get("completion_date", "")),
                 ("Total Depth (ft MD)", f"{td:,.0f}"), ("TVD (ft)", f"{tvd:,}"),
                 ("KB Elevation (ft)", w.get("kb_elevation", ""))):
        r = t.add_row().cells
        r[0].text, r[1].text = k, str(v)

    doc.add_heading("2. Completion Summary", level=1)
    doc.add_paragraph(
        f"The {w.get('well_name','')} was completed as a horizontal producer in "
        f"the {zone} formation. The lateral was landed at {tvd:,} ft TVD and "
        f"drilled to {td:,.0f} ft MD. Production casing was set at total depth "
        f"and cemented. The well was stimulated in {rng.randint(20,44)} stages "
        f"using slickwater and hybrid fluid systems.")
    if extra_uwi:
        doc.add_paragraph(
            f"Offset well {_dashed(extra_uwi)} was used for correlation of the "
            f"{zone} landing zone and stage placement.")

    doc.add_heading("3. Perforation Intervals", level=1)
    t = doc.add_table(rows=1, cols=5)
    t.style = "Light Grid Accent 1"
    for i, h in enumerate(("Stage", "Top Depth (ft MD)", "Base Depth (ft MD)",
                           "Length (ft)", "Formation")):
        t.rows[0].cells[i].text = h
    top = tvd + rng.uniform(50, 250)
    for st in range(1, rng.randint(6, 12)):
        r = t.add_row().cells
        r[0].text, r[1].text, r[2].text = str(st), f"{top:,.0f}", f"{top+50:,.0f}"
        r[3].text, r[4].text = "50", zone
        top += rng.uniform(230, 280)

    doc.add_heading("4. Stimulation Parameters", level=1)
    t = doc.add_table(rows=1, cols=2)
    t.style = "Light Grid Accent 1"
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Parameter", "Value"
    for k, v in (("Total Fluid (bbl)", f"{rng.randint(70,160)*1000:,}"),
                 ("Total Proppant (lbs)", f"{rng.randint(10,24)*1000000:,}"),
                 ("Max Treating Pressure (psi)", f"{rng.randint(7800,8900):,}"),
                 ("Average Rate (bpm)", rng.randint(55, 70)),
                 ("Fluid System", "Slickwater / hybrid"),
                 ("Proppant Type", "100 mesh + 40/70 white sand")):
        r = t.add_row().cells
        r[0].text, r[1].text = k, str(v)

    doc.add_heading("5. Initial Production", level=1)
    t = doc.add_table(rows=1, cols=5)
    t.style = "Light Grid Accent 1"
    for i, h in enumerate(("Date", "Oil (bbl/d)", "Gas (Mcf/d)",
                           "Water (bbl/d)", "Choke")):
        t.rows[0].cells[i].text = h
    q = rng.uniform(800, 2000)
    try:
        d0 = date.fromisoformat(str(w.get("completion_date"))[:10])
    except Exception:
        d0 = date(2024, 6, 1)
    for m in range(4):
        r = t.add_row().cells
        r[0].text = (d0 + timedelta(days=30*m)).isoformat()
        r[1].text, r[2].text = f"{q:,.0f}", f"{q*rng.uniform(1.4,1.9):,.0f}"
        r[3].text = f"{q*rng.uniform(0.2,0.6):,.0f}"
        r[4].text = f"{rng.choice([32,40,48])}/64"
        q *= rng.uniform(0.78, 0.9)
    doc.save(path)


def tops_docx(path, wells, rng):
    """A multi-well report. Formation tops studies cover a project, not a single
    well — so this is the many-UWIs-in-one-document case, which is realistic
    rather than contrived."""
    from docx import Document
    doc = Document()
    doc.add_heading("FORMATION TOPS REPORT", 0)
    doc.add_paragraph(f"{_field(wells[0], rng)} — Sub-Basin Study")
    doc.add_paragraph("Prepared by: Data Wrangler Solutions  |  Date: "
                      f"{date(2024, rng.randint(1,12), 15).strftime('%B %Y')}")
    doc.add_heading("1. Geological Overview", level=1)
    doc.add_paragraph(
        f"The following formation tops were picked from petrophysical log "
        f"analysis of {len(wells)} horizontal wells. Picks were correlated "
        f"against regional type logs and calibrated to available core.")
    doc.add_heading("2. Formation Tops by Well", level=1)
    t = doc.add_table(rows=1, cols=6)
    t.style = "Light Grid Accent 1"
    for i, h in enumerate(("UWI", "Formation", "Top MD (ft)", "Base MD (ft)",
                           "Net Pay (ft)", "Fluid")):
        t.rows[0].cells[i].text = h
    for w in wells:
        td = float(w.get("final_td") or 12000)
        for name, nominal, _n in _strat(w)[-4:]:
            if nominal > td:
                continue
            top = nominal * rng.uniform(0.92, 1.08)
            r = t.add_row().cells
            r[0].text, r[1].text = _dashed(w["uwi"]), name
            r[2].text = f"{top:,.0f}"
            r[3].text = f"{top+rng.uniform(120,320):,.0f}"
            r[4].text, r[5].text = str(rng.randint(18, 62)), \
                rng.choice(["OIL", "OIL/GAS", "GAS"])
    doc.add_heading("3. Fluid Contacts", level=1)
    t = doc.add_table(rows=1, cols=5)
    t.style = "Light Grid Accent 1"
    for i, h in enumerate(("UWI", "Formation", "Contact Type", "Depth MD (ft)",
                           "Method")):
        t.rows[0].cells[i].text = h
    for w in wells[:4]:
        r = t.add_row().cells
        r[0].text, r[1].text = _dashed(w["uwi"]), _strat(w)[-4][0]
        r[2].text = rng.choice(["OWC", "GWC", "GOC"])
        r[3].text = f"{float(w.get('final_td') or 9000)*rng.uniform(0.5,0.7):,.0f}"
        r[4].text = rng.choice(["Log analysis", "Pressure data", "Test data"])
    doc.save(path)


def production_xlsx(path, w, rng):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Monthly Production"
    ws.append(["UWI", "WELL_NAME", "OPERATOR", "DATE", "OIL_BBL", "GAS_MCF",
               "WATER_BBL", "BOE", "TUBING_PRESS_PSI", "STATUS"])
    try:
        d0 = date.fromisoformat(str(w.get("completion_date"))[:10])
    except Exception:
        d0 = date(2022, 1, 1)
    q = rng.uniform(200, 900)
    n = rng.randint(18, 48)
    for m in range(n):
        d = d0 + timedelta(days=30*m)
        oil = q * math.exp(-0.03*m) * rng.uniform(0.85, 1.15)
        gas = oil * rng.uniform(1.2, 2.6)
        ws.append([_long_uwi(w["uwi"]), w.get("well_name", ""),
                   w.get("operator_name", ""), d.isoformat(), round(oil),
                   round(gas), round(oil*rng.uniform(0.4, 2.4)),
                   round(oil + gas/6), rng.randint(900, 2600),
                   "PRODUCING" if m < n-3 else "SHUT-IN"])
    ws2 = wb.create_sheet("Well Header")
    for k, v in (("UWI", _long_uwi(w["uwi"])), ("API", _dashed(w["uwi"])),
                 ("Well Name", w.get("well_name", "")),
                 ("Operator", w.get("operator_name", "")),
                 ("County", w.get("county", "")), ("State", _abbr(w)),
                 ("Total Depth", w.get("final_td", "")),
                 ("Spud Date", w.get("spud_date", ""))):
        ws2.append([k, str(v)])
    wb.save(path)


def core_xlsx(path, w, rng):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Core Analysis"
    ws.append(["CORE ANALYSIS REPORT"])
    ws.append(["UWI", _long_uwi(w["uwi"])])
    ws.append(["API Number", _dashed(w["uwi"])])
    ws.append(["Well Name", w.get("well_name", "")])
    ws.append(["Operator", w.get("operator_name", "")])
    ws.append(["Laboratory", rng.choice(["Core Lab", "Weatherford Labs",
                                         "Premier Oilfield Group"])])
    ws.append([])
    ws.append(["Sample", "Depth (ft)", "Porosity (%)", "Perm kair (md)",
               "Perm Klink (md)", "Grain Density (g/cc)", "So (%)", "Sw (%)",
               "Lithology", "Description"])
    base = float(w.get("final_td") or 9000) * rng.uniform(0.55, 0.75)
    for i in range(1, rng.randint(20, 60)):
        ka = rng.uniform(0.005, 480)
        ws.append([f"S{i:03d}", round(base + i*0.5, 1),
                   round(rng.uniform(2.5, 22), 2), round(ka, 3),
                   round(ka*rng.uniform(0.6, 0.95), 3),
                   round(rng.uniform(2.62, 2.74), 3),
                   round(rng.uniform(0, 45), 1), round(rng.uniform(15, 70), 1),
                   rng.choice(["Limestone", "Dolomite", "Sandstone", "Shale"]),
                   rng.choice(["vuggy", "fractured", "tight", "porous",
                               "argillaceous", "bioturbated"])])
    wb.save(path)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def load_wells(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if (r.get("uwi") or "").strip()]


def generate(wells, out_dir, per_well, seed=42, log=print):
    rng = random.Random(seed)
    pdf_dir = os.path.join(out_dir, "sample_pdfs")
    off_dir = os.path.join(out_dir, "sample_office")
    las_dir = os.path.join(out_dir, "las_files")
    for d in (pdf_dir, off_dir, las_dir):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):          # a stale document is as misleading here
            try:                          # as a stale CSV is on the data side
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    have = {}
    for mod, name in (("reportlab", "pdf"), ("openpyxl", "xlsx"),
                      ("docx", "docx")):
        try:
            __import__(mod)
            have[name] = True
        except ImportError:
            have[name] = False
            log(f"  ⚠ {mod} not installed — skipping {name} documents")

    manifest = []
    PDF_KINDS = [("SCOUT", scout_ticket), ("EOW", end_of_well),
                 ("CASING_CEMENT", casing_cement), ("WELL_TEST", well_test),
                 ("PETROPHYSICS", petrophysics), ("DIRSRVY", dir_survey)]

    for w in wells:
        uwi = w["uwi"]
        for _ in range(per_well):
            case, shown, fname_uwi, expect, ww = "clean", "full", uwi, uwi, w
            if rng.random() < HARD_CASE_SHARE:
                case = rng.choice(["filename_only", "text_only", "dashed_api",
                                   "unknown_uwi", "no_uwi", "image_only"])
                if case == "filename_only":
                    shown = "none"
                elif case == "text_only":
                    fname_uwi = f"DOC{rng.randint(10000, 99999)}"
                elif case == "dashed_api":
                    shown = "dashed"
                elif case == "unknown_uwi":
                    ghost = f"{uwi[:5]}{rng.randint(90000, 99999)}0000"
                    ww, fname_uwi, expect = dict(w, uwi=ghost), ghost, ""
                elif case == "no_uwi":
                    shown, expect = "none", ""
                    fname_uwi = f"REPORT{rng.randint(1000, 9999)}"
            shown_txt = {"full": ww["uwi"], "dashed": _dashed(ww["uwi"]),
                         "none": ""}[shown]
            try:
                if case == "image_only" and have["pdf"]:
                    p = os.path.join(pdf_dir, f"SCOUT_SCAN_{fname_uwi}.pdf")
                    image_only_pdf(p, ww, rng)
                    expect = ""
                else:
                    pick = rng.random()
                    if pick < 0.62 and have["pdf"]:
                        tag, fn = rng.choice(PDF_KINDS)
                        p = os.path.join(pdf_dir, f"{tag}_{fname_uwi}.pdf")
                        fn(p, ww, rng, shown_txt)
                    elif pick < 0.78 and have["xlsx"]:
                        p = os.path.join(off_dir, f"PRODUCTION_{fname_uwi}.xlsx")
                        production_xlsx(p, ww, rng)
                    elif pick < 0.88 and have["xlsx"]:
                        p = os.path.join(off_dir, f"CORE_ANALYSIS_{fname_uwi}.xlsx")
                        core_xlsx(p, ww, rng)
                    elif have["docx"]:
                        p = os.path.join(off_dir, f"COMPLETION_{fname_uwi}.docx")
                        other = (rng.choice(wells)["uwi"]
                                 if rng.random() < 0.25 else None)
                        completion_docx(p, ww, rng, shown, other)
                        if other:
                            case = f"two_uwis (also {other})"
                    else:
                        continue
            except Exception as e:
                log(f"  !! {case} for {uwi}: {type(e).__name__}: {e}")
                continue
            manifest.append({"file": os.path.relpath(p, out_dir),
                             "expected_uwi": expect,
                             "well_name": ww.get("well_name", ""), "case": case})

    # LAS — one or two per well, plus format variants. A parser that only ever
    # meets unwrapped LAS 2.0 with a populated UWI is not a tested parser.
    for w in wells:
        for run in range(1, rng.randint(2, 3)):
            case, shown = "clean", "full"
            wrap, ver, units, nullv = False, "2.0", "FT", -999.25
            r = rng.random()
            if r < 0.10:
                case, shown = "no_uwi", "none"
            elif r < 0.18:
                case, shown = "dashed_api", "dashed"
            elif r < 0.26:
                case, wrap = "wrapped", True
            elif r < 0.33:
                case, ver = "las_1.2", "1.2"
            elif r < 0.40:
                case, units = "metric", "M"
            elif r < 0.46:
                case, nullv = "null_9999", -9999.00
            p = os.path.join(las_dir, f"{w['uwi']}_{run}.las")
            try:
                las_file(p, w, rng, shown, wrap, ver, units, nullv)
            except Exception as e:
                log(f"  !! LAS for {w['uwi']}: {type(e).__name__}: {e}")
                continue
            manifest.append({"file": os.path.relpath(p, out_dir),
                             "expected_uwi": "" if shown == "none" else w["uwi"],
                             "well_name": w.get("well_name", ""),
                             "case": f"las_{case}"})

    if have["docx"] and len(wells) >= 4:
        p = os.path.join(off_dir, "Formation_Tops_Study.docx")
        grp = rng.sample(wells, min(8, len(wells)))
        try:
            tops_docx(p, grp, rng)
            manifest.append({"file": os.path.relpath(p, out_dir),
                             "expected_uwi": "|".join(g["uwi"] for g in grp),
                             "well_name": "(multiple)", "case": "multi_well"})
        except Exception as e:
            log(f"  !! tops study: {type(e).__name__}: {e}")

    mpath = os.path.join(out_dir, "MANIFEST.csv")
    with open(mpath, "w", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=["file", "expected_uwi",
                                            "well_name", "case"])
        wtr.writeheader()
        wtr.writerows(manifest)

    from collections import Counter
    for case, n in sorted(Counter(m["case"].split(" ")[0]
                                  for m in manifest).items()):
        log(f"   {case:16} {n:>5}")
    log(f"-- {len(manifest)} document(s); ground truth in {mpath}")
    return len(manifest)


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate synthetic well documents for File Catalog testing")
    ap.add_argument("--wells-csv", required=True)
    ap.add_argument("--out", default="synth_docs")
    ap.add_argument("--per-well", type=int, default=2)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    wells = load_wells(a.wells_csv)
    if a.limit:
        wells = wells[:a.limit]
    print(f"-- {len(wells)} well(s) x {a.per_well} -> {a.out}")
    generate(wells, a.out, a.per_well, a.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
