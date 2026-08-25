r"""Build the WHOLE Teapot Field model into one self-contained folder tree.

WHY A SEPARATE TREE. The synthetic data was scattered through the REAL NPR-3
dataset -- SEG-Y under Teapot_Dome\DataSets\Seismic, documents under
Teapot_Dome\DataSets\Synthetic_Field -- so anyone pointing a loader at the
Teapot folder got both, and the only thing keeping them apart was a UWI block
and a date range. Real production runs 1922-2005 on 49025063xxx; the model runs
2014-2024 on 49025900xxx. That is a distinction you have to KNOW to see, which
makes it the wrong kind of separation.

One root, nothing of the original in it, and a README that says so.

    python tools/make_teapot_model.py                 # plan only
    python tools/make_teapot_model.py --apply
    python tools/make_teapot_model.py --apply --skip-seismic   # 324 MB skipped

The database side (horizons, geography) is loaded by its own tools, but this
also EXPORTS both to CSV so the folder is a complete dataset rather than half
of one -- a tree that cannot rebuild what it describes is a backup with a hole
in it.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# WHICH EXTENSIONS THE FILE CATALOG WILL NOT CRAWL. Imported rather than
# retyped: if the app ever changes what it treats as tabular, this folder
# split has to change with it or the corpus quietly misfiles again.
try:
    from dataview.file_catalog.promotion_lineage import TABULAR_EXTS
except Exception:
    TABULAR_EXTS = {".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".xlsb"}

DEFAULT_ROOT = (r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
                r"\data_wrangler\training\Teapot_Field_Model")

README = """Teapot Field Model - synthetic dataset
=====================================

EVERYTHING HERE IS GENERATED. It is not NPR-3 field data and must not be
mixed with it. The real Teapot Dome data lives under training\\Teapot_Dome
and is untouched by the tools that build this tree.

Telling them apart:

    this model      UWI 49025900xxxxxx    production 2014-2024
    real NPR-3      UWI 490250xxxxxxxx    production 1922-2005

What is here
------------

  wells/                  {n_docs} documents describing {n_wells} wells
    sample_pdfs/          scout tickets, end-of-well, casing and cement,
                          well tests, petrophysics, directional surveys,
                          monthly production reports
    sample_office/        completion reports (.docx)
    las_files/            wireline logs in LAS 1.2, 2.0 and 3.0
    teapot_field_wells.csv    the well list the documents describe
    MANIFEST.csv          ground truth: which UWI each document belongs to,
                          including the deliberately unreadable cases

  seismic/
    2d_segy/              {n_segy} 2D lines, SEG-Y rev 1, IBM floats
    horizons/             four interpreted horizons, as grids and contours

  tabular/                production and core analysis workbooks (.xlsx).
                          THESE DO NOT GO THROUGH THE FILE CATALOG -- .xlsx
                          is in TABULAR_EXTS and a scan skips it silently.
                          Point the Data Assistant here instead.

  geography/              field outline, leases, reserve boundary, pipelines

How it holds together
---------------------

One structural model underlies all of it. The dome that generated the
seismic also generated the horizons, the formation tops in every document,
and the production profile of every well - so a horizon pick lands on its
reflector, a top in a scout ticket matches the top in that well's LAS, and
the wells nearest the crest are the ones that produce. Nothing here was
drawn independently and made to agree afterwards.

Loading it
----------

  1. File Catalog: scan  wells/  then run the pipeline and Promote.
     That folder holds only what the catalog can read - PDF, LAS, DOCX.
  1b. Data Assistant: point it at  tabular/  for the workbooks. Kept in
     its own folder because the File Catalog skips .xlsx WITHOUT SAYING
     SO, and the Data Assistant cannot use the .docx beside them.
  2. File Catalog: scan  seismic/2d_segy/  for the seismic lines.
  3. Horizons and geography are loaded straight into dataview.* by
     tools/make_teapot_horizons.py and tools/make_teapot_geography.py.
     The CSVs here are an export of the same content.

Rebuilding
----------

  python tools/make_teapot_model.py --apply

Deterministic: the same seeds produce the same files, byte for byte.
"""


def _export_horizons(root, log):
    """Horizon grids and contours to CSV, so the tree can rebuild them."""
    import csv
    from dataview.migration.synth_horizons import teapot_horizons
    d = os.path.join(root, "seismic", "horizons")
    os.makedirs(d, exist_ok=True)
    hz = teapot_horizons()
    meta_p = os.path.join(d, "horizons.csv")
    with open(meta_p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["horizon_id", "horizon_name", "horizon_type",
                    "strat_unit_name", "seq_no", "pick_domain", "pick_uom",
                    "min_value", "max_value", "display_colour"])
        for meta, _g, _s in hz:
            w.writerow([meta[k] for k in
                        ("horizon_id", "horizon_name", "horizon_type",
                         "strat_unit_name", "seq_no", "pick_domain",
                         "pick_uom", "min_value", "max_value",
                         "display_colour")])
    n_nodes = n_seg = 0
    for meta, (lats, lons, vals), segs in hz:
        gp = os.path.join(d, f"{meta['horizon_id']}_grid.csv")
        with open(gp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["horizon_id", "row_no", "col_no", "latitude",
                        "longitude", "twt_ms"])
            for i in range(vals.shape[0]):
                for j in range(vals.shape[1]):
                    w.writerow([meta["horizon_id"], i, j,
                                f"{lats[i]:.7f}", f"{lons[j]:.7f}",
                                f"{vals[i, j]:.4f}"])
                    n_nodes += 1
        cp = os.path.join(d, f"{meta['horizon_id']}_contours.csv")
        with open(cp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["horizon_id", "contour_id", "contour_value", "wkt"])
            for k, (value, pts) in enumerate(segs):
                wkt = "LINESTRING(" + ", ".join(
                    f"{lo:.7f} {la:.7f}" for la, lo in pts) + ")"
                w.writerow([meta["horizon_id"],
                            f"{meta['horizon_id']}_C{k:04d}",
                            f"{value:.4f}", wkt])
                n_seg += 1
    log(f"   horizons  {len(hz)} horizon(s), {n_nodes:,} grid node(s), "
        f"{n_seg} contour(s)")
    return len(hz)


def _export_geography(root, log):
    """Field, leases, boundary and pipelines to CSV with WKT geometry."""
    import csv
    from dataview.migration.synth_field import Surfaces, plan_field
    from dataview.migration.synth_geography import (
        field_outline, gathering_system, lease_parcels, reserve_boundary,
        wkt_line, wkt_polygon)
    d = os.path.join(root, "geography")
    os.makedirs(d, exist_ok=True)
    S = Surfaces()
    wells = plan_field(surfaces=S)
    outline, level = field_outline(S)
    bnd = reserve_boundary()
    leases = lease_parcels(bnd)
    pipes = gathering_system(wells)

    with open(os.path.join(d, "field.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["field_id", "field_name", "field_type", "county",
                    "province_state", "basin_name", "remark", "wkt"])
        w.writerow(["TEAPOT_DOME", "Teapot Dome (NPR-3)", "OIL", "NATRONA",
                    "WY", "Powder River Basin",
                    f"{level:,.0f} ms closing contour of the Tensleep horizon",
                    wkt_polygon(outline)])
    with open(os.path.join(d, "boundary.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["boundary_id", "boundary_name", "boundary_type",
                    "province_state", "wkt"])
        w.writerow(["NPR3_RESERVE", "Naval Petroleum Reserve No. 3",
                    "FEDERAL RESERVE", "WY", wkt_polygon(bnd)])
    with open(os.path.join(d, "leases.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["land_tract_id", "tract_name", "lease_number",
                    "operator_name", "legal_description", "wkt"])
        for i, (nm, legal, ring, owner, _col) in enumerate(leases, start=1):
            w.writerow([f"NPR3_LSE_{i:03d}", nm, f"WYW-{160000 + i}",
                        owner, legal, wkt_polygon(ring)])
    with open(os.path.join(d, "pipelines.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pipeline_id", "pipeline_name", "commodity", "wkt"])
        for i, (nm, pts) in enumerate(pipes, start=1):
            w.writerow([f"NPR3_PL_{i:03d}", nm,
                        "OIL" if nm.startswith("Flowline") else "CRUDE",
                        wkt_line(pts)])
    log(f"   geography 1 field, {len(leases)} lease(s), 1 boundary, "
        f"{len(pipes)} pipeline(s)")
    return len(leases) + len(pipes) + 2


def _write_tabular(wells, tab_dir, share, seed):
    """One workbook per mirror, each holding `share` of the wells.

    Sheet names and column headers are the ones synth_tables produces, which
    are in turn the ones scout_pdf_reader.SECTIONS declares -- so the workbook
    and the scout ticket describe a well identically, and the Data Assistant's
    column mapping sees the same names either way.
    """
    import random
    from openpyxl import Workbook
    from dataview.migration.synth_tables import well_header_row, well_tables

    rng = random.Random(seed + 77)
    # Independent draw per mirror. A single shuffle reused across sheets would
    # put the same wells in every workbook and the same wells in every
    # document -- two populations that never meet.
    def _take(name):
        import zlib
        r = random.Random(zlib.crc32(f"{seed}:{name}".encode("utf-8")))
        return {w["uwi"] for w in wells if r.random() < share}

    total = 0

    # Well header: its own workbook, because it is the parent every other
    # mirror hangs off and is the one most likely to be loaded on its own.
    keep = _take("well_header")
    wb = Workbook(); ws = wb.active; ws.title = "Well Header"
    rows = [well_header_row(w, rng) for w in wells if w["uwi"] in keep]
    if rows:
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append(list(r.values()))
        total += len(rows)
    wb.save(os.path.join(tab_dir, "WELL_HEADER.xlsx"))

    # Every other section, one workbook each, UWI carried on every row so the
    # loader can resolve the parent without a join the operator has to invent.
    by_section = {}
    for w in wells:
        for name, (cols, rws, _wd) in well_tables(w):
            by_section.setdefault(name, (cols, []))[1].extend(
                [w["uwi"]] + list(r) for r in rws)
    for name, (cols, rws) in by_section.items():
        keep = _take(name)
        sel = [r for r in rws if r[0] in keep]
        if not sel:
            continue
        wb = Workbook(); ws = wb.active; ws.title = name[:31]
        ws.append(["UWI"] + list(cols))
        for r in sel:
            ws.append(r)
        fn_ = name.upper().replace(" ", "_").replace("/", "_") + ".xlsx"
        wb.save(os.path.join(tab_dir, fn_))
        total += len(sel)
    return total


def _tree_size(p):
    n = b = 0
    for root, _d, fs in os.walk(p):
        for f in fs:
            try:
                b += os.path.getsize(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return n, b


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the whole Teapot Field model into one folder tree.")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--wells", type=int, default=120)
    ap.add_argument("--per-well", type=int, default=2)
    ap.add_argument("--lines", type=int, default=250)
    ap.add_argument("--variants", type=int, default=4)
    ap.add_argument("--seed", type=int, default=90210,
                    help="seed for the field and the tabular draw")
    ap.add_argument("--tabular-share", type=float, default=0.75,
                    help="share of each mirror that arrives as a "
                         "workbook rather than a document")
    ap.add_argument("--skip-seismic", action="store_true",
                    help="skip the 1,000 SEG-Y (324 MB) -- everything else")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    wells_dir = os.path.join(a.root, "wells")
    segy_dir = os.path.join(a.root, "seismic", "2d_segy")

    print("Teapot Field Model")
    print(f"   root      {a.root}")
    print(f"   wells     {a.wells} well(s), {a.per_well} document(s) each")
    print(f"   seismic   {'SKIPPED' if a.skip_seismic else str(a.lines * a.variants) + ' SEG-Y line file(s)'}")
    print(f"   plus      horizons and geography, exported to CSV")
    if not a.apply:
        print("\nPLAN ONLY -- nothing written. Re-run with --apply.")
        return 0

    t0 = time.perf_counter()
    os.makedirs(wells_dir, exist_ok=True)

    print("\n-- wells ------------------------------------------------")
    from dataview.migration import synth_docs
    from dataview.migration.synth_field import (
        Surfaces, plan_field, write_csv)
    S = Surfaces()
    n_expl = max(1, round(a.wells * 0.025))
    n_delin = max(1, round(a.wells * 0.067))
    wells = plan_field(n_expl=n_expl, n_delin=n_delin,
                       n_dev=max(1, a.wells - n_expl - n_delin), surfaces=S)
    write_csv(wells, os.path.join(wells_dir, "teapot_field_wells.csv"))
    n_docs = synth_docs.generate(wells, wells_dir, a.per_well,
                                 log=lambda m: None)
    print(f"   {n_docs:,} document(s) for {len(wells)} well(s)")

    # TWO LOADERS, TWO FOLDERS. .xlsx is in TABULAR_EXTS, so the File
    # Catalog does not crawl it -- spreadsheets belong to the Bulk
    # Tabular Loader. Left beside the .docx, the workbooks are SILENTLY
    # skipped by a scan of wells/ and the Data Assistant is shown .docx
    # it cannot use. Neither tool complains; the rows just never arrive.
    # A folder each makes the boundary visible instead of implicit.
    tab_dir = os.path.join(a.root, "tabular")
    os.makedirs(tab_dir, exist_ok=True)
    moved = 0
    off = os.path.join(wells_dir, "sample_office")
    if os.path.isdir(off):
        import shutil
        for f in sorted(os.listdir(off)):
            if os.path.splitext(f)[1].lower() in TABULAR_EXTS:
                shutil.move(os.path.join(off, f),
                            os.path.join(tab_dir, f))
                moved += 1
    print(f"   {moved:,} workbook(s) -> tabular/ (Data Assistant, not "
          f"the File Catalog)")

    # -- the tabular share -------------------------------------------
    # SPREADSHEET FIRST, DOCUMENT SECOND. Perry's split: the well header and
    # every other mirror arrive 75% from a workbook and 25% from a log or a
    # report. That is how a real data-management job looks -- the same entity
    # turning up from more than one place -- and it is the only thing that
    # exercises BOTH loaders, since .xlsx is in TABULAR_EXTS and the File
    # Catalog will not crawl it.
    #
    # The mechanism is promote's own rule: it is insert-only with NOT EXISTS,
    # so THE FIRST SOURCE IN WINS. Load the workbooks first and 75% of each
    # mirror is theirs; the documents then fill the remaining 25% and add
    # nothing to the rows already there.
    #
    # The 75% is drawn INDEPENDENTLY PER MIRROR, so a well can have its header
    # off a spreadsheet and its tops off a scout ticket. Splitting whole wells
    # instead would produce two clean populations and never test a merge.
    _stale = [f for f in os.listdir(tab_dir)
              if f.lower().endswith(".xlsx")
              and f.upper().startswith(("PRODUCTION_", "CORE_ANALYSIS_"))
              and f.upper() not in ("PRODUCTION_SUMMARY.XLSX",)]
    for f in _stale:
        os.remove(os.path.join(tab_dir, f))
    if _stale:
        print(f"   {len(_stale):,} per-well workbook(s) removed -- their "
              f"content is in the consolidated mirror workbooks")
    n_tab = _write_tabular(wells, tab_dir, a.tabular_share, a.seed)
    print(f"   {n_tab:,} workbook row(s) -> tabular/ "
          f"({a.tabular_share:.0%} of each mirror)")


    if not a.skip_seismic:
        print("\n-- seismic ----------------------------------------------")
        os.makedirs(segy_dir, exist_ok=True)
        from tools.make_teapot_2d import main as segy_main
        segy_main(["--apply", "--dir", segy_dir,
                   "--count", str(a.lines * a.variants),
                   "--variants", str(a.variants)])

    print("\n-- horizons and geography -------------------------------")
    _export_horizons(a.root, print)
    _export_geography(a.root, print)

    with open(os.path.join(a.root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(README.format(
            n_docs=n_docs, n_wells=len(wells),
            n_segy=("0 (skipped)" if a.skip_seismic
                    else f"{a.lines * a.variants:,}")))

    n, b = _tree_size(a.root)
    print(f"\n{n:,} file(s), {b / 1048576:,.0f} MB in {time.perf_counter() - t0:,.0f}s")
    print(f"   {a.root}")
    print("\nNothing of the original NPR-3 data is in this tree. See README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
