r"""The Teacup demo set: synthetic wells, synthetic documents, synthetic 2D.

Three datasets, loaded on camera and taken back out between takes:

  wells     300 wells + children from synth_data\*.csv   (Bulk Tabular Loader)
  docs      1,055 files from synth_docs\                 (the file pipeline)
  seismic   17 lines from synth_seismic\                 (the file pipeline)

EACH IS SCOPED BY WHAT IT IS, NOT BY WHEN IT ARRIVED. row_created_by is
'PROMOTE' for everything the file-catalog path writes, so a stamp cannot tell
these rows from any other. What can:

  wells     the 300 uwis listed in synth_data\dv_well.csv, read at run time
  docs      the INVENTORY_IDs of files under synth_docs\
  seismic   the seismic set built from synth_seismic\

The wells go through tools/delete_wells.py, which derives the child order from
sys.foreign_keys rather than a written-down list -- 24 tables reference
dv_well, and dv_prod_volume has no uwi of its own.

--reset prints the count per table before it removes anything.
"""

import argparse
import csv
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO_ROOT, "tools")
SERVER = r"localhost\SQLEXPRESS"

SYNTH = r"C:\Bulk\Synthetic\synthetic_data"
DATA_DIR = os.path.join(SYNTH, "synth_data")
DOCS_DIR = os.path.join(SYNTH, "synth_docs")
WELL_CSV = os.path.join(DATA_DIR, "dv_well.csv")

SEIS_DIR = os.path.join(SYNTH, "synth_seismic")
SEIS_SET_LIKE = "%SYNTHETIC%"

DOC_EXTS = ".las,.pdf,.docx"


def like_prefix(path):
    """A LIKE pattern for everything under `path`.

    '_' is a wildcard in T-SQL LIKE and both synth_docs and synthetic_data
    contain one, so an unbracketed prefix reaches further than the folder.
    """
    return path.replace("[", "[[]").replace("_", "[_]").rstrip("\\") + "\\%"


def demo_uwis():
    """The 300 uwis, read from the CSV so the scope follows the source."""
    if not os.path.exists(WELL_CSV):
        return []
    with open(WELL_CSV, encoding="utf-8-sig") as fh:
        return [r["uwi"].strip() for r in csv.DictReader(fh) if r.get("uwi")]


# ── counting ──────────────────────────────────────────────────────────────

def _uwi_clause(n):
    return ("RTRIM(uwi) IN (SELECT LTRIM(RTRIM(value)) "
            "FROM STRING_SPLIT(CAST(:pat AS nvarchar(max)), ','))")


def well_counts(c, uwis):
    """{table: n} for the demo wells and every child that carries a uwi."""
    from sqlalchemy import text
    if not uwis:
        return {}
    pat = ",".join(uwis)
    out = {}
    kids = [r[0] for r in c.execute(text("""
        SELECT DISTINCT OBJECT_NAME(fk.parent_object_id)
          FROM sys.foreign_keys fk
         WHERE fk.referenced_object_id = OBJECT_ID('dataview.dv_well')"""))]
    for t in sorted(set(kids) | {"dv_well"}):
        has = c.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='dataview' AND TABLE_NAME=:t "
            "  AND LOWER(COLUMN_NAME)='uwi'"), {"t": t}).scalar()
        if not has:
            continue
        n = c.execute(text("SELECT COUNT(*) FROM dataview.[%s] WHERE %s"
                           % (t, _uwi_clause(0))), {"pat": pat}).scalar()
        if n:
            out[t] = n
    # dv_prod_volume reaches a well only through dv_prod_entity.
    n = c.execute(text(
        "SELECT COUNT(*) FROM dataview.dv_prod_volume v WHERE EXISTS ("
        "  SELECT 1 FROM dataview.dv_prod_entity e"
        "   WHERE e.prod_entity_id = v.prod_entity_id AND %s)"
        % _uwi_clause(0)), {"pat": pat}).scalar()
    if n:
        out["dv_prod_volume"] = n
    return out


def catalog_counts(c, folder, label=""):
    """{table: n} for every file_catalog row whose file is under `folder`.

    EVERY TABLE KEYED BY INVENTORY_ID, not just the catalog itself. A header
    row whose catalog row has been deleted is an orphan nothing will ever
    clean up -- which is exactly what the seismic reset used to leave behind.
    """
    from sqlalchemy import text
    pat = like_prefix(folder)
    out = {}
    n = c.execute(text("SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG "
                       "WHERE FILE_PATH LIKE :p"), {"p": pat}).scalar()
    if n:
        out["GLOBAL_FILE_CATALOG" + label] = n
    for t in _inventory_tables(c):
        if t == "GLOBAL_FILE_CATALOG":
            continue
        try:
            n = c.execute(text(
                "SELECT COUNT(*) FROM file_catalog.[%s] WHERE INVENTORY_ID IN "
                "(SELECT INVENTORY_ID FROM file_catalog.GLOBAL_FILE_CATALOG "
                " WHERE FILE_PATH LIKE :p)" % t), {"p": pat}).scalar()
        except Exception:
            n = 0
        if n:
            out[t + label] = n
    return out


def purge_catalog(c, folder, label=""):
    """Delete every file_catalog row for files under `folder`.

    GLOBAL_FILE_CATALOG goes LAST -- the other deletes read it to find their
    own rows, so removing it first would leave everything else behind.
    """
    from sqlalchemy import text
    pat = like_prefix(folder)
    done = {}
    for t in _inventory_tables(c):
        if t == "GLOBAL_FILE_CATALOG":
            continue
        try:
            r = c.execute(text(
                "DELETE FROM file_catalog.[%s] WHERE INVENTORY_ID IN "
                "(SELECT INVENTORY_ID FROM file_catalog.GLOBAL_FILE_CATALOG"
                "  WHERE FILE_PATH LIKE :p)" % t), {"p": pat})
            if r.rowcount and r.rowcount > 0:
                done[t + label] = r.rowcount
        except Exception as exc:
            done[t + label] = "failed: %s" % str(exc)[:120]
    r = c.execute(text("DELETE FROM file_catalog.GLOBAL_FILE_CATALOG "
                       "WHERE FILE_PATH LIKE :p"), {"p": pat})
    if r.rowcount and r.rowcount > 0:
        done["GLOBAL_FILE_CATALOG" + label] = r.rowcount
    return done


def doc_counts(c):
    """{table: n} for every catalog row whose file is under synth_docs."""
    return catalog_counts(c, DOCS_DIR)


def _inventory_tables(c):
    """file_catalog tables keyed by INVENTORY_ID, from the live schema.

    Derived, not written down -- this project has already paid for four lists
    that had to agree and did not.
    """
    from sqlalchemy import text
    return [r[0] for r in c.execute(text("""
        SELECT c.TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS c
          JOIN INFORMATION_SCHEMA.TABLES t
            ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME
         WHERE c.TABLE_SCHEMA='file_catalog'
           AND UPPER(c.COLUMN_NAME)='INVENTORY_ID'
           AND t.TABLE_TYPE='BASE TABLE'
         ORDER BY 1"""))]


def seis_counts(c):
    """{table: n} for the synthetic 2D set."""
    from sqlalchemy import text
    out = {}
    try:
        n = c.execute(text(
            "SELECT COUNT(*) FROM dataview.dv_seis_line l WHERE EXISTS ("
            " SELECT 1 FROM dataview.dv_seis_set s"
            "  WHERE s.seis_set_id=l.seis_set_id AND s.seis_set_name LIKE :p)"),
            {"p": SEIS_SET_LIKE}).scalar()
        if n:
            out["dv_seis_line"] = n
        n = c.execute(text("SELECT COUNT(*) FROM dataview.dv_seis_set "
                           "WHERE seis_set_name LIKE :p"),
                      {"p": SEIS_SET_LIKE}).scalar()
        if n:
            out["dv_seis_set"] = n
        out.update(catalog_counts(c, SEIS_DIR, " (2d)"))
    except Exception:
        pass
    return out


def counts(engine):
    """{dataset: {table: n}} for all three."""
    uwis = demo_uwis()
    with engine.connect() as c:
        return {"wells": well_counts(c, uwis),
                "docs": doc_counts(c),
                "seismic": seis_counts(c)}


def total(engine):
    return sum(sum(v.values()) for v in counts(engine).values())


# ── loading ───────────────────────────────────────────────────────────────

def _pipeline(root, exts, timeout=5400):
    """One ordinary scan-extract-capture-promote run, scoped to a folder."""
    if not os.path.isdir(root):
        return False, "no such folder: %s" % root
    cmd = [sys.executable, "-u", "-m", "dataview.import_data.pipeline_run",
           "--root", root, "--exts", exts,
           "--server", SERVER, "--database", "DataView_Demo",
           "--workers", "4", "--promote", "--promote-apply"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=REPO_ROOT)
        tail = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
        return (p.returncode == 0,
                "  ".join(tail[-2:]) or (p.stderr or "")[-300:])
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def load_docs(apply=False):
    if not apply:
        return True, "would scan %s" % DOCS_DIR
    return _pipeline(DOCS_DIR, DOC_EXTS)


def load_seismic(apply=False):
    if not apply:
        return True, "would scan %s" % SEIS_DIR
    return _pipeline(SEIS_DIR, ".sgy,.segy")


# ── resetting ─────────────────────────────────────────────────────────────

def reset_wells(engine, apply=False):
    """Hand the 300 uwis to delete_wells, which owns the FK order."""
    uwis = demo_uwis()
    if not uwis:
        return {"wells": "no %s" % WELL_CSV}
    if not apply:
        with engine.connect() as c:
            return well_counts(c, uwis)
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(uwis))
        p = subprocess.run(
            [sys.executable, "-u", os.path.join(TOOLS, "delete_wells.py"),
             "--uwi-file", path, "--apply"],
            capture_output=True, text=True, timeout=1800, cwd=REPO_ROOT)
        out = {}
        for line in (p.stdout or "").splitlines():
            if "deleted" in line and "from" in line:
                parts = line.split()
                try:
                    out[parts[-1]] = int(parts[-3].replace(",", ""))
                except Exception:
                    pass
        if p.returncode != 0 and not out:
            out["wells"] = "failed: %s" % ((p.stderr or p.stdout)[-200:])
        return out
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def reset_docs(engine, apply=False):
    """Remove the catalog rows for every file under synth_docs."""
    from sqlalchemy import text
    if not apply:
        with engine.connect() as c:
            return doc_counts(c)
    with engine.begin() as c:
        return purge_catalog(c, DOCS_DIR)


def reset_seismic(engine, apply=False):
    """Remove the synthetic 2D set, its lines, and its catalog rows.

    Children first: dv_seis_line and dv_seis_file_catalog both FK to
    dv_seis_set, so the parent cannot go until they have.
    """
    from sqlalchemy import text
    if not apply:
        with engine.connect() as c:
            return seis_counts(c)
    done = {}
    with engine.begin() as c:
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
                if r.rowcount and r.rowcount > 0:
                    done[key] = r.rowcount
            except Exception as exc:
                done[key] = "failed: %s" % str(exc)[:120]
        try:
            done.update(purge_catalog(c, SEIS_DIR, " (2d)"))
        except Exception as exc:
            done["catalog (2d)"] = "failed: %s" % str(exc)[:120]
    return done


def reset_all(engine, apply=False):
    """Wells first -- the documents' catalog rows are what the wells cite."""
    return {"wells": reset_wells(engine, apply=apply),
            "docs": reset_docs(engine, apply=apply),
            "seismic": reset_seismic(engine, apply=apply)}


# ── CLI ───────────────────────────────────────────────────────────────────

def _show(engine):
    rows = counts(engine)
    grand = 0
    for ds in ("wells", "docs", "seismic"):
        d = rows.get(ds) or {}
        n = sum(v for v in d.values() if isinstance(v, int))
        grand += n
        print("   %-9s %6d row(s)%s" % (ds, n, "" if d else "   (nothing)"))
        for t, v in sorted(d.items()):
            print("        %-34s %8s" % (t, "{:,}".format(v)
                                         if isinstance(v, int) else v))
    print("   %-9s %6d row(s)" % ("TOTAL", grand))
    return grand


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--load", action="store_true",
                    help="scan the documents and the synthetic 2D")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--only", choices=("wells", "docs", "seismic"),
                    help="act on one dataset only")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    print("\nTeacup demo set")
    grand = _show(engine)

    if a.reset:
        if not a.apply:
            print("\n--reset without --apply: would remove the rows above.")
            return 0
        print("\nremoving...")
        pick = (lambda k: a.only in (None, k))
        if pick("wells"):
            for k, v in reset_wells(engine, apply=True).items():
                print("   wells    %-30s %s" % (k, v))
        if pick("docs"):
            for k, v in reset_docs(engine, apply=True).items():
                print("   docs     %-30s %s" % (k, v))
        if pick("seismic"):
            for k, v in reset_seismic(engine, apply=True).items():
                print("   seismic  %-30s %s" % (k, v))
        print("\nafter:")
        _show(engine)
        return 0

    if a.load:
        pick = (lambda k: a.only in (None, k))
        if pick("docs"):
            ok, msg = load_docs(apply=a.apply)
            print("\ndocs     %s %s" % ("ok" if ok else "FAILED", msg))
        if pick("seismic"):
            ok, msg = load_seismic(apply=a.apply)
            print("seismic  %s %s" % ("ok" if ok else "FAILED", msg))
        if pick("wells"):
            print("wells    load the %s CSVs with the Bulk Tabular Loader "
                  "-- that load IS the demo, so this tool does not do it "
                  "behind your back." % os.path.basename(DATA_DIR))
        if a.apply:
            print("\nafter:")
            _show(engine)
        else:
            print("\nCOUNTS ONLY -- re-run with --apply.")
    elif not a.reset:
        print("\nNothing asked for. --load or --reset (add --apply to act).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
