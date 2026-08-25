r"""Per-well row sets, rendered as either a document or a spreadsheet.

ONE DEFINITION, TWO RENDERINGS. A scout ticket and a workbook that disagree
about the same well are two facts, and the loader has no way to tell which is
wrong -- so the rows are built ONCE, here, and the PDF writer and the XLSX
writer both consume them. The section names and column headers are the ones
scout_pdf_reader.SECTIONS declares, because that is what the reader parses;
the spreadsheet inherits them for free rather than inventing its own.

WHY A SPLIT AT ALL. In any real data-management job the same entity arrives
from more than one place -- a header off a spreadsheet, a header off a log --
and the interesting behaviour is what happens when they meet. A corpus where
every mirror has exactly one source never exercises that, and never exercises
the second loader either: .xlsx is in TABULAR_EXTS, so the File Catalog does
not crawl it and the Bulk Tabular Loader is the only way in.
"""
import math
import random
import zlib
from datetime import date, timedelta

from dataview.migration.synth_docs import (
    _abbr, _field, _strat, CASING_PROGRAMME)


def well_header_row(w, rng=None):
    """The well header as a flat record -- what a spreadsheet column set is."""
    rng = rng or random.Random(0)
    td = float(w.get("final_td") or 12000)
    return {
        "UWI": w.get("uwi", ""),
        "WELL_NAME": w.get("well_name", ""),
        "OPERATOR": w.get("operator_name", ""),
        "FIELD": w.get("field_name") or _field(w, rng),
        "COUNTY": w.get("county", ""),
        "STATE": _abbr(w),
        "COUNTRY": "US",
        "WELL_TYPE": w.get("well_type", ""),
        "WELL_STATUS": w.get("well_status", ""),
        "WELL_PROFILE": w.get("well_profile_type", ""),
        "SPUD_DATE": w.get("spud_date", ""),
        "COMPLETION_DATE": w.get("completion_date", ""),
        "TOTAL_DEPTH": round(td, 1),
        "KB_ELEVATION": w.get("kb_elevation", ""),
        "GROUND_ELEVATION": w.get("ground_elevation", ""),
        "SURFACE_LATITUDE": w.get("surface_latitude", ""),
        "SURFACE_LONGITUDE": w.get("surface_longitude", ""),
        "BOTTOM_HOLE_LATITUDE": w.get("bottom_hole_latitude", ""),
        "BOTTOM_HOLE_LONGITUDE": w.get("bottom_hole_longitude", ""),
    }


def well_tables(w, rng=None):
    """[(section, (columns, rows, widths)), ...] for one well.

    Exactly the blocks the scout ticket renders, so a workbook built from this
    and a ticket built from this describe the same well the same way.
    """
    rng = random.Random(zlib.crc32(
        ("synth_tables:" + str(w.get("uwi", ""))).encode("utf-8")))
    td = float(w.get("final_td") or 12000)
    tvd = round(td * rng.uniform(0.82, 0.97))
    tops = _strat(w)
    zone = tops[-2][0] if len(tops) > 1 else "TARGET"
    blocks = []

    # ── Stratigraphy ────────────────────────────────────────────────────
    strat = []
    for i, (nm, dep, note) in enumerate(tops):
        base = tops[i + 1][1] if i + 1 < len(tops) else dep + rng.uniform(60, 200)
        strat.append([nm, f"{dep:,.0f}", f"{base:,.0f}", f"{base - dep:,.0f}",
                      rng.choice(["Sandstone", "Shale", "Limestone",
                                  "Dolomite", "Siltstone"])])
    blocks.append(("Stratigraphy",
                   (["Formation", "Top MD (ft)", "Base MD (ft)", "Gross (ft)",
                     "Lithology"], strat,
                    [1.9, 1.0, 1.0, 0.9, 1.2])))

    # ── Directional survey ──────────────────────────────────────────────
    srv, md, inc, azi, tv = [], 0.0, 0.0, rng.uniform(0, 359), 0.0
    ns = ew = 0.0
    while md < td:
        md = min(md + 500, td)
        inc = min(rng.uniform(0, 4) + inc, 62) if md > td * 0.45 else 0.0
        tv = min(td, tv + 500 * math.cos(math.radians(inc)))
        ns += 500 * math.sin(math.radians(inc)) * math.cos(math.radians(azi))
        ew += 500 * math.sin(math.radians(inc)) * math.sin(math.radians(azi))
        srv.append([f"{md:,.0f}", f"{inc:.2f}", f"{azi:.2f}", f"{tv:,.0f}",
                    f"{ns:,.1f}", f"{ew:,.1f}", f"{rng.uniform(0, 2.4):.2f}"])
    blocks.append(("Directional Survey",
                   (["MD (ft)", "Inc", "Azi", "TVD (ft)", "N/S (ft)",
                     "E/W (ft)", "DLS"], srv, [1.0, 0.7, 0.7, 1.0, 0.9, 0.9, 0.7])))

    # ── DST ─────────────────────────────────────────────────────────────
    dst = []
    for i in range(rng.randint(1, 3)):
        top = round(tvd * rng.uniform(0.6, 0.95))
        dst.append([w.get("spud_date", ""), rng.choice(["DST", "DST", "RFT"]),
                    f"{top:,}", f"{top + rng.randint(30, 160):,}",
                    rng.choice(["Oil and gas to surface", "Gas cut mud",
                                "Salt water", "Oil, no water"]),
                    f"{rng.uniform(60, 900):,.0f}",
                    f"{rng.uniform(100, 3000):,.0f}",
                    f"{rng.uniform(28, 44):.1f}"])
    blocks.append(("DST",
                   (["Test Date", "Type", "Top MD", "Base MD", "Result",
                     "Max Oil", "Max Gas", "API Grav"], dst,
                    [0.9, 0.55, 0.75, 0.75, 1.7, 0.7, 0.7, 0.7])))

    # ── Core runs and core samples ──────────────────────────────────────
    runs, samples = [], []
    for r in range(1, rng.randint(2, 4)):
        ctop = round(tvd * rng.uniform(0.55, 0.9))
        clen = rng.randint(18, 60)
        runs.append([str(r), "CONVENTIONAL",
                     rng.choice(["Oil stain", "Fluorescence", "None"]),
                     zone, f"{ctop:,}", f"{ctop + clen:,}", str(clen),
                     f"{rng.uniform(72, 100):.0f}", w.get("spud_date", ""),
                     str(rng.randint(0, 40))])
        for s in range(rng.randint(4, 9)):
            dpt = ctop + rng.uniform(0, clen)
            por = rng.uniform(3.5, 24.0)
            samples.append([
                f"{r}-{s + 1}", rng.choice(["PLUG", "PLUG", "SIDEWALL"]),
                f"{dpt:,.1f}",
                rng.choice(["Sandstone", "Shale", "Limestone", "Dolomite"]),
                rng.choice(["Oil stain", "Fluorescence", "None"]),
                f"{por:.1f}",
                f"{max(0.01, 10 ** (0.22 * por - 3.1) * rng.uniform(0.5, 2)):.3f}",
                f"{rng.uniform(2.2, 2.72):.2f}",
                f"{rng.uniform(0.18, 0.78):.2f}",
                f"{rng.uniform(0, 0.42):.2f}"])
    blocks.append(("Core Runs",
                   (["#", "Type", "Show", "Formation", "Top MD", "Base MD",
                     "Length", "Rec %", "Date", "Photos"], runs,
                    [0.3, 1.05, 0.85, 1.2, 0.7, 0.7, 0.6, 0.55, 0.85, 0.5])))
    blocks.append(("Core Sample",
                   (["Sample", "Type", "Depth", "Lithology", "Show", "Por %",
                     "Perm", "Bulk Den", "Sw", "So"], samples,
                    [0.65, 0.8, 0.75, 1.05, 0.9, 0.55, 0.6, 0.7, 0.5, 0.5])))

    # ── Completion and frac stages ──────────────────────────────────────
    n_st = rng.randint(14, 42)
    lat_len = max(0, td - tvd)
    fluid = round(rng.uniform(180000, 420000))
    prop = round(rng.uniform(6e6, 1.6e7))
    blocks.append(("Completion Summary",
                   (["Completion Date", "Type", "Orientation", "Formation",
                     "Lateral (ft)", "Stages", "Fluid (bbl)", "Proppant (lbs)",
                     "Prop Intensity", "Fluid System"],
                    [[w.get("completion_date", ""), "MULTISTAGE FRAC",
                      "HORIZONTAL" if lat_len > 500 else "VERTICAL", zone,
                      f"{lat_len:,.0f}", str(n_st), f"{fluid:,}", f"{prop:,}",
                      f"{(prop / lat_len) if lat_len else 0:,.0f}",
                      rng.choice(["SLICKWATER", "HYBRID", "GEL"])]],
                    [1.0, 1.15, 1.0, 1.1, 0.75, 0.5, 0.8, 0.95, 0.85, 0.95])))

    frac, top = [], tvd + rng.uniform(50, 300)
    for i in range(1, n_st + 1):
        ftop = round(top + (i - 1) * (lat_len / max(1, n_st)))
        frac.append([str(i), f"{ftop:,}", f"{ftop + round(lat_len / max(1, n_st)):,}",
                     str(rng.randint(3, 8)), f"{rng.uniform(18, 42):.0f}",
                     f"{fluid // n_st:,}", f"{prop // n_st:,}",
                     f"{rng.uniform(3200, 6400):,.0f}",
                     f"{rng.uniform(5200, 8600):,.0f}",
                     f"{rng.uniform(60, 100):.1f}"])
    blocks.append(("Frac Stages",
                   (["Stage", "Top MD", "Base MD", "Clusters", "Cluster Sp",
                     "Fluid (bbl)", "Proppant (lbs)", "ISIP", "Avg Treat",
                     "Max Rate"], frac,
                    [0.5, 0.75, 0.75, 0.7, 0.75, 0.85, 0.95, 0.7, 0.75, 0.7])))

    # -- Checkshots ----------------------------------------------------
    # THE SAME VELOCITY FUNCTION THE HORIZONS WERE DEPTH-CONVERTED WITH.
    # V(z) = V0 + k*z, so a station at true vertical depth z has one-way
    #     t1 = (1/k) * ln(1 + k*z/V0)
    # A checkshot generated from ANY OTHER velocity would disagree with
    # the tops in this same document, and a well that mis-ties its own
    # seismic is worse than a well with no checkshot at all -- someone
    # will calibrate against it.
    _V0, _K = 7200.0, 2.4
    shots, _prev_z, _prev_t = [], 0.0, 0.0
    _n_cs = rng.randint(8, 16)
    for _i in range(1, _n_cs + 1):
        _md = round(td * _i / _n_cs, 1)
        _tv = round(_md * rng.uniform(0.985, 1.0), 1)
        _t1 = (1.0 / _K) * math.log(1.0 + _K * _tv / _V0)
        _owt = _t1 * 1000.0
        _twt = 2.0 * _owt
        _avg = (_tv / _t1) if _t1 > 0 else 0.0
        _int = (((_tv - _prev_z) / (_t1 - _prev_t))
                if _t1 > _prev_t else _avg)
        shots.append([f"CS{_i:02d}", f"{_md:,.1f}", f"{_tv:,.1f}",
                      f"{_twt:,.2f}", f"{_owt:,.2f}",
                      f"{_avg:,.0f}", f"{_int:,.0f}"])
        _prev_z, _prev_t = _tv, _t1
    blocks.append(("Checkshots",
                   (["Station", "MD (ft)", "TVD (ft)", "TWT (ms)",
                     "OWT (ms)", "Avg Vel", "Int Vel"], shots,
                    [0.75, 1.0, 1.0, 1.0, 1.0, 0.85, 0.85])))

    # -- Perforations --------------------------------------------------
    perfs = []
    _pt = tvd + rng.uniform(40, 260)
    for i in range(rng.randint(3, 8)):
        ptop = round(_pt + i * rng.uniform(60, 240))
        plen = rng.randint(8, 44)
        spf = rng.choice([4, 6, 6, 12])
        perfs.append([w.get("completion_date", ""), f"{ptop:,}",
                      f"{ptop + plen:,}", str(plen * spf), str(spf),
                      rng.choice(["3-1/8 HSD", "2-7/8 HSD", "4-1/2 HMX"]),
                      str(rng.choice([60, 60, 90, 120])), zone,
                      rng.choice(["OPEN", "OPEN", "OPEN", "SQUEEZED"])])
    blocks.append(("Perforations",
                   (["Perf Date", "Top MD", "Base MD", "Shots", "SPF",
                     "Gun", "Phasing", "Formation", "Status"], perfs,
                    [1.0, 0.78, 0.78, 0.6, 0.5, 1.0, 0.68, 1.15,
                     0.92])))

    # ── Production ──────────────────────────────────────────────────────
    try:
        d0 = date.fromisoformat(str(w.get("completion_date"))[:10])
    except Exception:
        d0 = date(2019, 1, 1)
    q = float(w.get("_qi") or rng.uniform(200, 900))
    n = min(int(w.get("_months") or rng.randint(12, 36)), 60)
    dec = float(w.get("_decline") or 0.03)
    prod = []
    for m in range(n):
        # CALENDAR MONTHS, NOT 30 DAYS. A month averages 30.44 days, so a
        # 30-day step drifts ~22 days backwards over a 50-month series and
        # eventually REPEATS a month. dv_prod_volume's PK is
        # (prod_entity_id, period_date, fluid_type), so each repeat was a
        # duplicate key -- 213 rows silently skipped by an insert-only
        # promote, with no error anywhere, on 57 of 89 wells.
        _mo = d0.month - 1 + m
        d = date(d0.year + _mo // 12, _mo % 12 + 1, 1)
        oil = q * math.exp(-dec * m) * rng.uniform(0.88, 1.12)
        gas = oil * rng.uniform(1.2, 2.6)
        prod.append([d.strftime("%Y-%m"), f"{oil:,.0f}", f"{gas:,.0f}",
                     f"{oil * rng.uniform(0.4, 2.4):,.0f}", f"{oil:,.0f}"])
    blocks.append(("Production Summary",
                   (["Date", "Oil (bbl)", "Gas (Mcf)", "Water (bbl)",
                     "Avg Rate"], prod, [1.0, 1.0, 1.0, 1.0, 1.0])))
    return blocks
