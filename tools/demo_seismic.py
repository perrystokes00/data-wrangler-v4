"""A seismic-only demo set: load it on camera, take it back out between takes.

WHY THIS IS SCOPED BY PATH AND NOT BY STAMP. Every seismic row in this database
carries row_created_by = 'PROMOTE' -- the generic file-catalog stamp shared with
everything that path has ever written. Deleting by it would take the real Teapot
3D survey and the real NPR-3 2D lines along with the demo, which is precisely
the class of mistake this tool exists to avoid.

Path is the honest handle here. dv_seis_set.file_path, dv_seis_line.file_path
and dv_seis_file_catalog.full_path all record where the file came from, so a set
built from C:\\Bulk\\demo_seismic is separable from one built anywhere else no
matter what the SEG-Y called itself. The three existing sets live under
training\\ and Seismic\\, match nothing here, and are never named.

The catalog rows under the folder go too, so a re-load actually re-does the work
rather than reporting every file already catalogued and drawing nothing new.

--reset prints the count per table BEFORE it removes anything, so the claim can
be read rather than trusted.
"""

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_DIR = r"C:\Bulk\demo_seismic"
SERVER = r"localhost\SQLEXPRESS"

# The staged subset, and where each file came from. Everything here already
# existed on disk -- nothing is generated -- so the folder is reproducible from
# this list alone if it is ever lost.
#
# PROCESSED ONLY. Each line also ships a _RAW_ twin carrying the same survey
# name; loading both would put two sets on one survey and draw the line twice.
#
# TestDataAll's gulf_shelf_2d and delta_deep_3d were tried and dropped. They
# extract cleanly -- survey name, trace count -- but every trace-header
# coordinate is NULL, so promote holds them rather than inventing a position.
# That is the right behaviour and a useless demo: a survey with no location
# plots nowhere. These ten are real Geoscience Australia lines with real
# coordinates and EPSG 7854, which is also why they sit visibly far from the
# Wyoming data and can never be mistaken for it.
SOURCES = [
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\83-QXL_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\80-QBR_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\83-NHJ_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\84-SXY_80-QBM_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\84-TPA_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\83-NHQ_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\79-QAD_81-QJR_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\79-QAG_POSTM_PROCESSED_Stack_3S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\79-QAM_81-QJP_POSTM_PROCESSED_Stack_4S.segy",
    r"C:\Bulk\Seismic\2D_SEGY\CENTRAL_AUSTRALIA\POSTM_STACK\Cebtral_Australia\POSTM_PROCESSED_STACK"
    r"\65-LH_POSTM_PROCESSED_Stack_5S.segy",
]

# Which table is reached by which path column. Order is DELETE order: children
# before parents, because dv_seis_line and dv_seis_file_catalog both FK to
# dv_seis_set. Deleting the parent first aborts the batch on a 547 and leaves
# the demo half-removed, which on camera is worse than not removing it at all.
SCOPED = [
    ("dataview.dv_seis_file_catalog", "full_path"),
    ("dataview.dv_seis_line", "file_path"),
    ("dataview.dv_seis_set", "file_path"),
    ("file_catalog.GLOBAL_FILE_CATALOG", "FILE_PATH"),
]


def like_prefix(path):
    """A LIKE pattern matching everything under `path`.

    '_' IS A WILDCARD IN T-SQL LIKE, and this folder is called demo_seismic.
    Unbracketed, 'C:\\Bulk\\demo_seismic%' also matches demoXseismic and
    demo-seismic -- folders that do not exist today but whose accidental
    creation would silently widen a DELETE. Bracketing costs nothing and the
    project has already been bitten once by LIKE 'cat_%' matching
    catalog_setting.
    """
    return path.replace("[", "[[]").replace("_", "[_]").rstrip("\\") + "\\%"


def counts(engine):
    """{table: n} for every row this demo owns, scoped by path."""
    from sqlalchemy import text
    pat = like_prefix(DEMO_DIR)
    out = {}
    with engine.connect() as c:
        for table, col in SCOPED:
            try:
                n = c.execute(text(
                    "SELECT COUNT(*) FROM %s WHERE [%s] LIKE :p"
                    % (table, col)), {"p": pat}).scalar()
            except Exception:
                n = None
            if n:
                out[table.split(".")[-1]] = n
    return out


def total(engine):
    return sum(counts(engine).values())


def make_folder(apply=False):
    """Stage the subset. Reports what is missing rather than skipping quietly."""
    made, missing = [], []
    for src in SOURCES:
        if not os.path.exists(src):
            missing.append(src)
            continue
        dst = os.path.join(DEMO_DIR, os.path.basename(src))
        if apply:
            os.makedirs(DEMO_DIR, exist_ok=True)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
        made.append((os.path.basename(src),
                     os.path.getsize(src) / 1048576.0))
    return made, missing


def load(apply=False):
    """Scan the demo folder through the real pipeline.

    This is the ordinary scan-extract-capture-promote run scoped to one folder
    -- the same path the demo shows on camera, not a shortcut around it.
    """
    if not os.path.isdir(DEMO_DIR):
        return False, "no such folder: %s (run --stage first)" % DEMO_DIR
    files = [f for f in os.listdir(DEMO_DIR)
             if f.lower().endswith((".sgy", ".segy"))]
    if not files:
        return False, "no SEG-Y in %s" % DEMO_DIR
    if not apply:
        return True, "would scan %s (%d file(s))" % (DEMO_DIR, len(files))
    cmd = [sys.executable, "-u", "-m", "dataview.import_data.pipeline_run",
           "--root", DEMO_DIR, "--exts", ".sgy,.segy",
           "--server", SERVER, "--database", "DataView_Demo",
           "--workers", "2", "--promote", "--promote-apply"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=1800, cwd=REPO_ROOT)
        tail = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
        return (p.returncode == 0,
                "  ".join(tail[-3:]) or (p.stderr or "")[-300:])
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def reset(engine, apply=False):
    """Remove every row whose file came from the demo folder.

    Children first (see SCOPED). Nothing here sweeps a table: each DELETE
    carries the folder predicate, so a row this demo did not load is a row this
    demo will not delete.
    """
    from sqlalchemy import text
    if not apply:
        return counts(engine)
    pat = like_prefix(DEMO_DIR)
    done = {}
    with engine.begin() as c:
        for table, col in SCOPED:
            key = table.split(".")[-1]
            try:
                r = c.execute(text("DELETE FROM %s WHERE [%s] LIKE :p"
                                   % (table, col)), {"p": pat})
                done[key] = r.rowcount if r.rowcount and r.rowcount > 0 else 0
            except Exception as exc:
                done[key] = "failed: %s" % exc
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--stage", action="store_true",
                    help="copy the subset into %s" % DEMO_DIR)
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    if a.stage:
        made, missing = make_folder(apply=a.apply)
        for name, mb in made:
            print("   %6.1f MB  %s" % (mb, name))
        for m in missing:
            print("   MISSING   %s" % m)
        print("\n%s %s -> %d file(s)"
              % ("staged" if a.apply else "would stage", DEMO_DIR, len(made)))
        if not a.load and not a.reset:
            return 0

    print("\ndemo seismic set — %s" % DEMO_DIR)
    before = counts(engine)
    if before:
        for t, n in sorted(before.items()):
            print("   %-34s %5d" % (t, n))
        print("   %-34s %5d" % ("TOTAL", total(engine)))
    else:
        print("   nothing loaded")

    if a.reset:
        if not a.apply:
            print("\n--reset without --apply: would remove the rows above.")
            return 0
        print("\nremoving…")
        for k, v in reset(engine, apply=True).items():
            print("   %-34s %s removed" % (k, v))
        print("\nremaining: %d row(s)" % total(engine))
        return 0

    if a.load:
        ok, msg = load(apply=a.apply)
        print("\n%s %s" % ("loaded" if ok else "FAILED", msg))
        if a.apply:
            print("\nnow: %d row(s)" % total(engine))
            for t, n in sorted(counts(engine).items()):
                print("   %-34s %5d" % (t, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
