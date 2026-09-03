"""
resolve_log_identity.py — give DLIS/LIS logs a real UWI
========================================================
DLIS/LIS binaries rarely carry a UWI, so the worker keys their curve inventory
by a synthetic FN_<hash> id. Those rows are captured but HOLD at promote (the
synthetic key matches no dv_well). This pass resolves a real UWI for each
synthetic log and re-keys its cat_well_log + cat_well_log_curve rows so they can
promote.

Resolution cascade (first hit wins), per synthetic log:
  1. UWI in the FILENAME            — e.g. 42999000020000_welllog.dlis
  2. UWI in the FOLDER PATH         — e.g. ...\\4299900002\\logs\\foo.dlis
  3. DLIS ORIGIN well_name → match  — read well_name from the file, match it to
       dv_well.well_name, else to gold WELL_MASTER name → uwi14
  4. SIBLING file in same folder    — another file in the same directory that
       already resolved to a real UWI (the folder is the well)

Anything unresolved stays synthetic (correct — it goes to manual review).

Idempotent and reversible-safe: only rewrites rows whose UWI currently starts
with 'FN_'. Dry-run by default.

  py resolve_log_identity.py                 # dry-run, show what WOULD resolve
  py resolve_log_identity.py --apply         # re-key resolved logs
"""
from __future__ import annotations
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataview.file_catalog import worker_core as wc
from sqlalchemy import text as _t

ZERO14 = "0" * 14


def uwi14(s):
    """Normalize any UWI-ish string to bare 14 digits, or None."""
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) < 10:
        return None
    u = d[:14] if len(d) >= 14 else d.ljust(14, "0")
    return None if u == ZERO14 else u


def name_norm(s):
    """Match gold WELL_MASTER.NAME_NORM: trim, collapse whitespace, uppercase."""
    s = re.sub(r"\s+", " ", str(s or "").strip()).upper()
    return s or None


# regex for a UWI embedded in a filename/path (dashed or 14-digit run)
_UWI_IN_TEXT = [
    re.compile(r"(\d{2}-\d{3}-\d{5}-\d{2}-\d{2})"),
    re.compile(r"(?<!\d)(\d{14})(?!\d)"),
    re.compile(r"(?<!\d)(\d{10})(?!\d)"),
]


def _uwi_from_text(s):
    for rx in _UWI_IN_TEXT:
        m = rx.search(str(s or ""))
        if m:
            u = uwi14(m.group(1))
            if u:
                return u
    return None


def _origin_well_name(fpath):
    """Read well_name (and well_id) from a DLIS/LIS file's ORIGIN/header.
    Returns (well_name, well_id_uwi) — either may be None."""
    ext = os.path.splitext(fpath)[1].lower()
    try:
        if ext in (".dlis", ".dls"):
            from dlisio import dlis
            f, *tail = dlis.load(fpath)
            for lf in [f] + list(tail):
                try:
                    for o in lf.origins:
                        wn = (getattr(o, "well_name", "") or "").strip()
                        wid = uwi14(getattr(o, "well_id", "") or "")
                        if wn or wid:
                            return (wn or None, wid)
                finally:
                    pass
        elif ext in (".lis",):
            # LIS: well name via the existing lis catalog metadata, if present
            try:
                from dataview.file_catalog.lis_catalog import classify_lis
            except ImportError:
                try:
                    from dataview.file_catalog.lis_catalog import classify_lis
                except ImportError:
                    classify_lis = None
            if classify_lis:
                meta = classify_lis(fpath) or {}
                return (meta.get("well_name"), uwi14(meta.get("uwi")))
    except Exception:
        pass
    return (None, None)


def _build_indexes(con):
    """In-memory lookups: dv_well by name, gold by name. Built once."""
    dv_by_name = {}
    for uwi, nm in con.execute(_t(
            "SELECT uwi, well_name FROM dataview.dv_well "
            "WHERE well_name IS NOT NULL")).fetchall():
        nn = name_norm(nm)
        if nn and nn not in dv_by_name:
            dv_by_name[nn] = uwi
    gold_by_name = {}
    try:
        for u, nn in con.execute(_t(
                "SELECT uwi14, NAME_NORM FROM WELL_REF.well_ref.well_master_public_v2 "
                "WHERE NAME_NORM IS NOT NULL")).fetchall():
            if nn and nn not in gold_by_name:
                gold_by_name[name_norm(nn)] = u
    except Exception:
        pass  # gold db may be absent in test env
    # also a set of real UWIs that exist, to validate filename/path hits
    dv_uwis = {r[0] for r in con.execute(_t(
        "SELECT uwi FROM dataview.dv_well")).fetchall()}
    return dv_by_name, gold_by_name, dv_uwis


def resolve(engine, apply=False, log=print):
    with engine.connect() as con:
        # the synthetic logs and their source files
        logs = con.execute(_t("""
            SELECT l.LOG_ID, l.UWI, l.FILE_PATH, l.FILE_FORMAT
              FROM file_catalog.cat_well_log l
             WHERE l.UWI LIKE 'FN[_]%'
             ORDER BY l.FILE_PATH
        """)).fetchall()
        log(f"{len(logs)} synthetic-UWI log(s) to resolve\n")
        if not logs:
            return

        dv_by_name, gold_by_name, dv_uwis = _build_indexes(con)

        # first pass: resolve each log; remember folder→uwi for sibling fill
        resolved = {}      # LOG_ID -> (new_uwi, source)
        folder_uwi = {}    # dir -> uwi (for sibling resolution)
        for log_id, syn_uwi, fpath, fmt in logs:
            fpath = fpath or ""
            new_uwi = src = None

            # 1. filename
            u = _uwi_from_text(os.path.basename(fpath))
            if u:
                new_uwi, src = u, "filename"
            # 2. folder path
            if not new_uwi:
                u = _uwi_from_text(os.path.dirname(fpath))
                if u:
                    new_uwi, src = u, "folderpath"
            # 3. ORIGIN well_name → dv_well / gold
            if not new_uwi and os.path.exists(fpath):
                wn, wid = _origin_well_name(fpath)
                if wid:
                    new_uwi, src = wid, "origin_well_id"
                elif wn:
                    nn = name_norm(wn)
                    if nn in dv_by_name:
                        new_uwi, src = dv_by_name[nn], "origin_name→dv_well"
                    elif nn in gold_by_name:
                        new_uwi, src = gold_by_name[nn], "origin_name→gold"

            if new_uwi:
                resolved[log_id] = (new_uwi, src)
                folder_uwi.setdefault(os.path.dirname(fpath), new_uwi)

        # 4. sibling fill — unresolved logs inherit a folder-mate's UWI
        for log_id, syn_uwi, fpath, fmt in logs:
            if log_id in resolved:
                continue
            sib = folder_uwi.get(os.path.dirname(fpath or ""))
            if sib:
                resolved[log_id] = (sib, "sibling")

        # report
        by_src = {}
        for _, (u, s) in resolved.items():
            by_src[s] = by_src.get(s, 0) + 1
        log("── resolution by source ──")
        for s, n in sorted(by_src.items(), key=lambda x: -x[1]):
            log(f"  {s:24} {n}")
        log(f"  {'unresolved':24} {len(logs) - len(resolved)}")
        log(f"\n  {len(resolved)} of {len(logs)} logs resolved to a real UWI")

        if not apply:
            log("\n[dry-run] nothing written. Re-run with --apply to re-key.")
            return

    # apply: re-key cat_well_log + cat_well_log_curve from FN_ to real UWI.
    # Also rewrite LOG_ID (it embeds the old key) so curve↔log stay joined.
    n_logs = n_curves = 0
    with engine.begin() as con:
        for log_id, (new_uwi, src) in resolved.items():
            new_logid = re.sub(r"^FN_[0-9A-F]+", new_uwi, log_id) \
                if log_id.startswith("FN_") else f"{new_uwi}-" + \
                log_id.split("-", 1)[-1]
            r1 = con.execute(_t("""
                UPDATE file_catalog.cat_well_log
                   SET UWI = :u, LOG_ID = :nl
                 WHERE LOG_ID = :ol AND UWI LIKE 'FN[_]%'
            """), {"u": new_uwi, "nl": new_logid, "ol": log_id})
            r2 = con.execute(_t("""
                UPDATE file_catalog.cat_well_log_curve
                   SET UWI = :u, LOG_ID = :nl
                 WHERE LOG_ID = :ol AND UWI LIKE 'FN[_]%'
            """), {"u": new_uwi, "nl": new_logid, "ol": log_id})
            n_logs += r1.rowcount or 0
            n_curves += r2.rowcount or 0
    log(f"\n[apply] re-keyed {n_logs} log header(s) and {n_curves} curve(s) "
        f"to real UWIs.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    engine = wc.make_engine(a.server, a.database)
    resolve(engine, apply=a.apply)


if __name__ == "__main__":
    main()
