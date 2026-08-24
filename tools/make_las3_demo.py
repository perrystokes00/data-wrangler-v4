r"""Write rich multi-section LAS 3.0 files into the synthetic corpus.

WHY THESE EXIST. The corpus had one LAS 3.0 file and it carried a single
~Log section -- so the thing 3.0 exists FOR, and the thing that makes lasio
fail on a 3.0 file, was never produced by the generator. split_las3 and the
section viewer were both written against the spec's published samples and one
hand-made fixture, and las3_capture's own docstring says Core, Tops and Test
"have mirrors waiting and are the obvious next additions, but each needs its
own column mapping read off the real files rather than assumed". These files
are that input.

Each file carries:

    ~Log            18 curves (the original 9 plus a derived suite)
    ~Core[1]        cored intervals, with quoted descriptions
    ~Tops           the state's stratigraphic column down to TD
    ~Inclinometry   a minimum-curvature build-and-hold survey
    ~Test           drill stem / formation tests

NEW FILES, NEVER EDITS. INVENTORY_ID is a SHA1 of the FILE_PATH, so rewriting
a catalogued file in place keeps its id and silently changes what that id
describes; writing beside it makes a new id for new content, which is what the
catalogue is built to handle. The existing files are source data and are not
touched.

The well headers come from dv_well, so a file names a well that already
exists. A LAS whose UWI matches nothing would be HELD on promote -- correct
behaviour, and useless as a demo of the loaded path.

    python tools/make_las3_demo.py                 # dry run, says what it would write
    python tools/make_las3_demo.py --apply
    python tools/make_las3_demo.py --apply --count 6
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dataview.migration.synth_docs import las_file          # noqa: E402
from dataview.file_catalog.las_reader import split_las3     # noqa: E402

DEFAULT_DIR = (r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
               r"\data_wrangler\training\synth50\synth_docs\las_files")
EXPECT = ("Log", "Core[1]", "Tops", "Inclinometry", "Test")


def _wells(engine, count):
    import sqlalchemy as sa
    q = sa.text("""
        SELECT TOP (:n) uwi, well_name, operator_name, county, province_state,
               final_td, kb_elevation, spud_date
          FROM dataview.dv_well
         WHERE final_td IS NOT NULL AND well_name IS NOT NULL
         ORDER BY uwi""")
    with engine.connect() as c:
        return [{"uwi": str(r[0]).strip(), "well_name": r[1],
                 "operator_name": r[2], "county": r[3],
                 "province_state": r[4],
                 "final_td": float(r[5] or 9000),
                 "kb_elevation": r[6],
                 "ground_elevation": (float(r[6]) - 12) if r[6] else "",
                 "spud_date": str(r[7] or "")[:10]}
                for r in c.execute(q, {"n": count})]


def _next_path(folder, uwi):
    """<uwi>_<n>.las at the first free index -- never an existing name."""
    n = 1
    while True:
        p = os.path.join(folder, f"{uwi}_{n}.las")
        if not os.path.exists(p):
            return p
        n += 1


def _verify(path):
    """Read the file back through the REAL parser. (ok, note).

    VERIFY BY CONTENT. A LAS 3.0 file that writes without error and parses to
    zero data sets is exactly the failure this corpus is meant to expose --
    it happened once already, when a bare ~Ascii found no definition section
    and every generated file produced a perfect header and no curves.
    """
    try:
        l3 = split_las3(path)
    except Exception as e:
        return False, f"unreadable: {e}"
    missing = [s for s in EXPECT if s not in l3.sets]
    if missing:
        return False, "missing sets: " + ", ".join(missing)
    empty = [s for s in EXPECT if not l3.sets[s].rows]
    if empty:
        return False, "sets with no rows: " + ", ".join(empty)
    return True, " · ".join(f"{s} {len(l3.sets[s].rows):,}r×"
                            f"{len(l3.sets[s].columns)}c" for s in EXPECT)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Write rich multi-section LAS 3.0 demo files. "
                    "Dry run unless --apply.")
    ap.add_argument("--dir", default=DEFAULT_DIR,
                    help="folder to write into (default: the synth50 las_files)")
    ap.add_argument("--count", type=int, default=4,
                    help="how many wells to write a file for")
    ap.add_argument("--seed", type=int, default=1102)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it, nothing is created.")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.dir):
        print(f"REFUSED: not a directory -- {a.dir}")
        return 2

    from dataview.core.dw_utils import make_engine
    wells = _wells(make_engine(a.database), a.count)
    if not wells:
        print("No wells with a total depth in dv_well; nothing to describe.")
        return 1

    print(f"{len(wells)} well(s) from dv_well -> {a.dir}\n")
    written = []
    for i, w in enumerate(wells):
        path = _next_path(a.dir, w["uwi"])
        if not a.apply:
            print(f"   would write  {os.path.basename(path):26s} "
                  f"{str(w['well_name'])[:24]:26s} TD {w['final_td']:,.0f}")
            continue
        rows = las_file(path, w, random.Random(a.seed + i),
                        version="3.0", rich=True)
        ok, note = _verify(path)
        print(f"   {'wrote' if ok else 'FAILED'}  "
              f"{os.path.basename(path):26s} {rows:,} log rows")
        print(f"          {note}")
        if not ok:
            # A file that does not read back is worse than no file: it would
            # sit in the corpus teaching the wrong lesson.
            os.remove(path)
            print("          removed -- it did not read back")
            return 1
        written.append(path)

    if not a.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0
    print(f"\n{len(written)} file(s) written and verified by re-reading them.")
    print("Scan the folder to catalogue them: the ~Inclinometry sets map "
          "straight into cat_well_dir_srvy_hdr/sta via las3_capture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
