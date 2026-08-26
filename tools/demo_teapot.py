"""The Teapot demo set: what to load on camera, and how to take it back out.

    python tools/demo_teapot.py                     # what is loaded right now
    python tools/demo_teapot.py --make-folder       # carve out the file subset
    python tools/demo_teapot.py --load --apply      # load it
    python tools/demo_teapot.py --reset --apply     # take it back out

WHAT THIS DELETES, AND WHY THAT LIST IS SHORT
---------------------------------------------
A reset button is the most dangerous thing in this codebase and its history
says so: data_wrangler_v3's demo_reset protected none of dv_column_map,
dv_column_synonym or dv_target_attribute while pointing at this same database,
and full=True was its DEFAULT -- one click in a retired app destroyed ~2,604
rows of learned mappings belonging to the app that replaced it.

So this one does not sweep tables, does not take a `full` flag, and owns no
DELETE of its own. It calls each specialised loader's --remove, which deletes
only rows carrying that loader's own row_created_by stamp:

    CORE_PHOTO_LOADER    dv_well_core, dv_well_core_sample, dv_well_core_photo
    MUDLOG_LOADER        dv_well_mud_log, dv_well_shows
    WELL_DETAIL_LOADER   dv_well_legal, dv_well_alias, dv_well_pressure,
                         dv_strat_interval

Those --remove paths are the same ones round-tripped in testing, so the reset
is the undo half of a pair that is exercised every time the pair is used --
not a second implementation that can drift from what the loaders actually
wrote.

SEISMIC IS THE ONE EXCEPTION, AND IT IS SCOPED BY SET. Every seismic row
carries row_created_by = 'PROMOTE' -- the generic file-catalog stamp shared
with everything that path has ever written -- so the stamp is useless as a
scope here. The synthetic 2D set is separable another way: one seis_set_id,
one folder, seventeen lines this project generated. Reset removes that set,
its lines, and ITS catalog rows (the 17 under synth_seismic, so a re-scan
really re-scans instead of reporting everything already done). The real 3D
survey and the real lineA-E match neither and are never named.

NOTHING ELSE IS TOUCHED. Not dv_well, not the reference tables, not the rest of
the file catalog, not the learned column mappings. A row this demo did not
create is a row this demo will not delete, and --reset prints the count per
table before it removes anything so the claim is checkable rather than
trusted.

THE FILE SUBSET (--make-folder) is a copy, never a move: the training tree is
source data and stays untouched. It carries enough to show each path without
being a wait on camera -- a mud log, two LAS runs, the core workbooks and a
dozen photographs.
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEMO_DIR = r"C:\Bulk\demo_teapot"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEIS_SERVER = r"localhost\SQLEXPRESS"

TRAINING = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "training", "Teapot_Dome", "DataSets")

# (loader module, stamp, tables it owns)
LOADERS = [
    ("load_core_data", "CORE_PHOTO_LOADER",
     ("dv_well_core", "dv_well_core_sample", "dv_well_core_photo")),
    ("load_mudlog", "MUDLOG_LOADER",
     ("dv_well_mud_log", "dv_well_shows")),
    ("load_well_detail", "WELL_DETAIL_LOADER",
     ("dv_well_legal", "dv_well_alias", "dv_well_pressure",
      "dv_strat_interval")),
]

# ── seismic ───────────────────────────────────────────────────────────────
# SCOPED BY SET, NOT BY STAMP. Every seismic row in this database carries
# row_created_by = 'PROMOTE' -- the generic file-catalog promote stamp shared
# with everything else that path has ever written. Deleting by it would take
# the real Teapot 3D and the real NPR-3 2D lines with the demo, which is
# exactly the class of mistake this tool exists to avoid.
#
# The synthetic 2D set is separable because it IS a set: one seis_set_id, one
# folder, seventeen lines this project generated and can regenerate. The two
# real sets -- the 3D survey and lineA-E -- share neither, and are never
# named here.
SEIS_SET_LIKE = "%SYNTHETIC%"
# The underscore is a LIKE wildcard; bracket it so the pattern matches
# the folder rather than anything merely shaped like it.
SEIS_FOLDER_LIKE = "%synth[_]seismic%"
# Moved in beside the rest of the synthetic demo data, so all three
# datasets the demo loads share one root.
SEIS_DIR = r"C:\Bulk\Synthetic\synthetic_data\synth_seismic"

# What the file subset carries. Kept small deliberately: a demo that waits is
# a demo that gets edited out.
SUBSET = [
    ("Core/CD Files/mudlog", ("48X28.LOG",)),
    ("Core/CD Files", ("CORE ACCOUNTING.xls",)),
    ("Core/CD Files/CORE P&P ANALYSES",
     ("RMOTC DOE 48x28 Well Core Data W-85011 7-27-04.xls",)),
    ("Core/CD Files/SLAB PHOTOS", "*.jpg:12"),
]


def counts(engine):
    """{table: {stamp: n}} for everything the demo owns."""
    from sqlalchemy import text
    out = {}
    with engine.connect() as c:
        for _mod, stamp, tables in LOADERS:
            for t in tables:
                try:
                    n = c.execute(text(
                        "SELECT COUNT(*) FROM dataview.[%s] "
                        "WHERE row_created_by = :b" % t), {"b": stamp}).scalar()
                except Exception:
                    n = None
                if n:
                    out.setdefault(t, {})[stamp] = n
        for t, n in seismic_counts(c).items():
            if n:
                out.setdefault(t, {})["synthetic 2D set"] = n
    return out


def seismic_counts(c):
    """Rows belonging to the synthetic 2D set only."""
    from sqlalchemy import text
    try:
        sets = c.execute(text(
            "SELECT COUNT(*) FROM dataview.dv_seis_set "
            "WHERE seis_set_name LIKE :p"), {"p": SEIS_SET_LIKE}).scalar()
        lines = c.execute(text(
            "SELECT COUNT(*) FROM dataview.dv_seis_line l "
            "WHERE EXISTS (SELECT 1 FROM dataview.dv_seis_set s "
            "              WHERE s.seis_set_id = l.seis_set_id "
            "                AND s.seis_set_name LIKE :p)"),
            {"p": SEIS_SET_LIKE}).scalar()
        cat = c.execute(text(
            "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG "
            "WHERE FILE_PATH LIKE :f"), {"f": SEIS_FOLDER_LIKE}).scalar()
    except Exception:
        return {}
    return {"dv_seis_set": sets, "dv_seis_line": lines,
            "GLOBAL_FILE_CATALOG (synth_seismic)": cat}


def reset_seismic(engine, apply=False):
    """Remove the synthetic 2D set, its lines, and its catalog rows.

    CHILDREN FIRST. dv_seis_line and dv_seis_file_catalog both FK to
    dv_seis_set, so the set cannot go until they have. Deleting the parent
    first would abort the batch on a 547 and leave the demo half-removed --
    which on camera is worse than not removing it at all.

    The catalog rows go too, so a re-scan actually re-does the work rather
    than reporting everything already done and drawing nothing new.
    """
    from sqlalchemy import text
    done = {}
    with engine.begin() as c:
        if not apply:
            return seismic_counts(c)
        for sql, key in (
            ("DELETE FROM dataview.dv_seis_file_catalog WHERE seis_set_id IN "
             "(SELECT seis_set_id FROM dataview.dv_seis_set "
             " WHERE seis_set_name LIKE :p)", "dv_seis_file_catalog"),
            ("DELETE FROM dataview.dv_seis_line WHERE seis_set_id IN "
             "(SELECT seis_set_id FROM dataview.dv_seis_set "
             " WHERE seis_set_name LIKE :p)", "dv_seis_line"),
            ("DELETE FROM dataview.dv_seis_set WHERE seis_set_name LIKE :p",
             "dv_seis_set"),
        ):
            try:
                r = c.execute(text(sql), {"p": SEIS_SET_LIKE})
                done[key] = r.rowcount if r.rowcount and r.rowcount > 0 else 0
            except Exception as exc:
                done[key] = "failed: %s" % exc
        try:
            r = c.execute(text(
                "DELETE FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE FILE_PATH LIKE :f"), {"f": SEIS_FOLDER_LIKE})
            done["GLOBAL_FILE_CATALOG (synth_seismic)"] = (
                r.rowcount if r.rowcount and r.rowcount > 0 else 0)
        except Exception as exc:
            done["GLOBAL_FILE_CATALOG (synth_seismic)"] = "failed: %s" % exc
    return done


def total(engine):
    return sum(sum(v.values()) for v in counts(engine).values())


def _run(mod_name, argv):
    """Call a loader's own main(), capturing what it says."""
    import contextlib
    import io as _io
    import importlib
    mod = importlib.import_module(mod_name)
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = mod.main(argv)
        return (rc in (0, None)), buf.getvalue()
    except SystemExit as e:
        msg = str(e) if str(e) not in ("0", "None") else ""
        return (e.code in (0, None)), buf.getvalue() + ("\n" + msg if msg else "")
    except Exception as e:
        return False, buf.getvalue() + "\n%s: %s" % (type(e).__name__, e)


def load(apply=False):
    lines = []
    for mod, _stamp, _t in LOADERS:
        ok, out = _run(mod, ["--apply"] if apply else [])
        lines.append((mod, ok, out))
    return lines


def load_seismic(apply=False):
    """Re-scan the synthetic 2D folder through the real pipeline.

    THE UNDO HALF HAS TO EXIST BEFORE THE DELETE DOES. Reset removes the
    seismic set and its catalog rows; without a way back the demo could be
    taken apart and not reassembled, which is a worse failure than never
    having had a reset. This is the ordinary scan-extract-capture-promote
    run scoped to one folder -- the same path the demo shows on camera,
    not a shortcut around it."""
    import subprocess
    if not os.path.isdir(SEIS_DIR):
        return False, "no such folder: %s" % SEIS_DIR
    if not apply:
        return True, "would scan %s (%d file(s))" % (
            SEIS_DIR, len(os.listdir(SEIS_DIR)))
    cmd = [sys.executable, "-u", "-m",
           "dataview.import_data.pipeline_run",
           "--root", SEIS_DIR, "--exts", ".sgy,.segy",
           "--server", SEIS_SERVER, "--database", "DataView_Demo",
           "--workers", "2", "--promote", "--promote-apply"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=900, cwd=REPO_ROOT)
        tail = [x for x in (p.stdout or "").strip().splitlines()
                if x.strip()]
        return (p.returncode == 0,
                "  ".join(tail[-3:]) or (p.stderr or "")[-300:])
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def reset(apply=False):
    """Remove ONLY rows stamped by the three loaders, via their own --remove."""
    lines = []
    for mod, _stamp, _t in LOADERS:
        argv = ["--remove"] + (["--apply"] if apply else [])
        ok, out = _run(mod, argv)
        lines.append((mod, ok, out))
    return lines


def make_folder(dest=DEMO_DIR):
    """Copy the subset. A COPY: the training tree is source data."""
    import glob
    made, missing = [], []
    for rel, what in SUBSET:
        src_dir = os.path.join(TRAINING, *rel.split("/"))
        dst_dir = os.path.join(dest, rel.replace("/", os.sep))
        if not os.path.isdir(src_dir):
            missing.append(rel)
            continue
        os.makedirs(dst_dir, exist_ok=True)
        if isinstance(what, tuple):
            names = what
        else:
            pat, _c, lim = what.partition(":")
            names = [os.path.basename(p) for p in
                     sorted(glob.glob(os.path.join(src_dir, pat)))[:int(lim)]]
        for n in names:
            s = os.path.join(src_dir, n)
            if not os.path.exists(s):
                missing.append(os.path.join(rel, n))
                continue
            d = os.path.join(dst_dir, n)
            if not os.path.exists(d):
                shutil.copy2(s, d)
            made.append(d)
    return made, missing


def main(argv=None):
    from dataview.core.dw_utils import make_engine
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--make-folder", action="store_true")
    ap.add_argument("--dest", default=DEMO_DIR)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-seismic", action="store_true",
                    help="leave the synthetic 2D set alone")
    a = ap.parse_args(argv)
    engine = make_engine(a.database)

    if a.make_folder:
        made, missing = make_folder(a.dest)
        print("%s" % a.dest)
        print("   %d file(s) copied" % len(made))
        for m in missing:
            print("   MISSING: %s" % m)
        return 0

    before = counts(engine)
    if not (a.load or a.reset):
        print("Teapot demo set — currently loaded:")
        if not before:
            print("   nothing")
        for t, st in sorted(before.items()):
            for stamp, n in st.items():
                print("   %-24s %-20s %5d" % (t, stamp, n))
        print("   %-24s %-20s %5d" % ("TOTAL", "", total(engine)))
        return 0

    if a.reset:
        print("Reset will remove ONLY rows stamped by the demo loaders:")
        if not before:
            print("   nothing is loaded.")
            return 0
        for t, st in sorted(before.items()):
            for stamp, n in st.items():
                print("   %-24s %-20s %5d" % (t, stamp, n))
        print("   %-24s %-20s %5d" % ("TOTAL", "", total(engine)))
        if not a.apply:
            print("\nAdd --apply to remove them. Nothing else is touched.")
            return 0
        for mod, ok, out in reset(apply=True):
            tail = [l for l in out.strip().splitlines() if l.strip()][-1:]
            print("   %-20s %s" % (mod, tail[0] if tail else ("ok" if ok else "FAILED")))
        if not a.no_seismic:
            for k, v in reset_seismic(engine, apply=True).items():
                print("   %-20s %s removed from %s"
                      % ("seismic", v, k))
        print("\nremaining: %d row(s)" % total(engine))
        return 0

    for mod, ok, out in load(apply=a.apply):
        tail = [l for l in out.strip().splitlines() if l.strip()][-1:]
        print("   %-20s %s" % (mod, tail[0] if tail else ("ok" if ok else "FAILED")))
    if not a.no_seismic:
        sok, smsg = load_seismic(apply=a.apply)
        print("   %-20s %s"
              % ("seismic", smsg if smsg else ("ok" if sok else "FAILED")))
    if a.apply:
        print("\nloaded: %d row(s)" % total(engine))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
