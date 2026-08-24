r"""Generate the Teapot Dome field as DOCUMENTS, for the real pipeline to load.

FILES FIRST, THEN scan -> extract -> capture -> promote. Writing rows straight
into dv_* would be faster and would prove nothing: the point of this corpus is
that the pipeline puts them there, so a gap in an extractor, a mirror or the
LINEAGE registry shows up as held rows instead of staying invisible.

The document generators already existed -- scout ticket, end-of-well, casing
and cement, well test, petrophysics, directional survey, completion report,
tops table, production and core workbooks, and LAS in 1.2 / 2.0 / 3.0. What did
not exist was a FIELD for them to describe. synth_field supplies that: 120
wells placed against the same dome that generated the 2D SEG-Y and the
horizons, with tops read from the horizon surfaces at each well's own position
and production scaled by structural height above the reservoir crest.

So the documents are internally consistent with the seismic AND with each
other: the tops in the scout ticket are the tops in the LAS is the top of the
completion interval, and the well that produces best is the well nearest the
crest.

    python tools/make_teapot_field.py                 # plan only, no files
    python tools/make_teapot_field.py --apply
    python tools/make_teapot_field.py --apply --wells 40 --per-well 2

NOTE: synth_docs.generate() CLEARS its own output folders before writing, so
--dir must be a folder that holds nothing else. The default is a new one.
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dataview.migration import synth_docs                      # noqa: E402
from dataview.migration.synth_field import (                   # noqa: E402
    FIELD_NAME, Surfaces, plan_field, write_csv)

DEFAULT_DIR = (r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
               r"\data_wrangler\training\Teapot_Dome\DataSets\Synthetic_Field")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate the Teapot Dome field documents. "
                    "Plan only unless --apply.")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--wells", type=int, default=120)
    ap.add_argument("--per-well", type=int, default=2,
                    help="documents per well, on top of the LAS files")
    ap.add_argument("--prod-years", type=int, default=5)
    ap.add_argument("--seed", type=int, default=90210)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    n_expl = max(1, round(a.wells * 0.025))
    n_delin = max(1, round(a.wells * 0.067))
    n_dev = max(1, a.wells - n_expl - n_delin)

    print("Building the structural model ...")
    t0 = time.perf_counter()
    S = Surfaces()
    wells = plan_field(n_expl=n_expl, n_delin=n_delin, n_dev=n_dev,
                       seed=a.seed, prod_years=a.prod_years, surfaces=S)
    print(f"   {len(wells)} well(s) planned in {time.perf_counter() - t0:.1f}s"
          f"   crest reservoir {S.crest_depth():,.0f} ft\n")

    ph = collections.Counter(w["_phase"] for w in wells)
    print(f"   {'phase':14s} {'wells':>6s} {'dry':>5s} {'producing':>10s} "
          f"{'best qi':>8s}")
    for p in ("EXPLORATION", "DELINEATION", "DEVELOPMENT"):
        grp = [w for w in wells if w["_phase"] == p]
        dry = sum(1 for w in grp if w["well_type"] == "DRY")
        qis = [w["_qi"] for w in grp if w["_qi"] > 0]
        print(f"   {p:14s} {ph[p]:6d} {dry:5d} {len(grp) - dry:10d} "
              f"{(max(qis) if qis else 0):8.0f}")
    prod = [w for w in wells if w["_months"]]
    print(f"\n   {len(prod)} well(s) with production, "
          f"{sum(w['_months'] for w in prod):,} well-months")
    print(f"   tops per well: {len(wells[0]['_tops'])}   field: {FIELD_NAME}")

    if not a.apply:
        print("\nPLAN ONLY -- no files written. Re-run with --apply.")
        return 0

    if os.path.isdir(a.dir) and any(
            f for f in os.listdir(a.dir)
            if f.lower() not in ("sample_pdfs", "sample_office",
                                 "las_files", "teapot_field_wells.csv",
                                 "manifest.csv")):
        print(f"\nREFUSED: {a.dir} holds files this tool did not write, and "
              f"synth_docs.generate() CLEARS its output folders. Point --dir "
              f"at an empty or dedicated folder.")
        return 2

    os.makedirs(a.dir, exist_ok=True)
    csv_path = os.path.join(a.dir, "teapot_field_wells.csv")
    write_csv(wells, csv_path)
    print(f"\n   well list -> {os.path.basename(csv_path)}")

    # THE DICTS GO STRAIGHT IN, not via the CSV. _tops / _qi / _months are
    # per-well structures rather than columns, so round-tripping through a file
    # would drop exactly the parts that tie the documents to the model.
    print(f"   generating documents into {a.dir} ...")
    t0 = time.perf_counter()
    manifest = synth_docs.generate(wells, a.dir, a.per_well, seed=a.seed,
                                   log=lambda m: None)
    dt = time.perf_counter() - t0

    counts, total_bytes = collections.Counter(), 0
    for sub in ("sample_pdfs", "sample_office", "las_files"):
        d = os.path.join(a.dir, sub)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            counts[os.path.splitext(f)[1].lower()] += 1
            total_bytes += os.path.getsize(os.path.join(d, f))
    n = sum(counts.values())
    print(f"\n   {n:,} document(s) in {dt:,.0f}s, "
          f"{total_bytes / 1048576:,.0f} MB")
    for ext, c in counts.most_common():
        print(f"      {ext or '(none)':8s} {c:5,}")
    print(f"   MANIFEST.csv: {manifest:,} entry(ies) of ground truth, "
          f"including the deliberately hard cases whose UWI is not "
          f"recoverable from the document at all.")
    print(f"\nScan {a.dir} in the File Catalog, then run the pipeline. "
          f"Every mirror an extractor writes should fill; anything held will "
          f"say why.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
