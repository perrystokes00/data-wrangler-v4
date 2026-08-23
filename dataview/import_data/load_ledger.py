"""
load_ledger.py — what did this file put in my database?
========================================================

Two writes per verified tabular load, and they answer different questions:

    file_catalog.GLOBAL_FILE_CATALOG   WHAT the file is  (identity)
    dataview.dv_global_file_catalog    WHAT it DID       (the load result)

Identity goes through file_gate — the same code path the extracted formats
already use — so a CSV is registered exactly like a LAS, with the same
canonical id. The result has nowhere to live in the File Catalog (it has no
rows_staged/promoted/held), which is what the loader's own table is for.

THE ID
------
`file_gate.inventory_id(path)` = SHA1(UPPER(full_path), UTF-16-LE), 40
chars, which is also HASHBYTES('SHA1', UPPER(path)) in T-SQL. One id
convention across the platform, computable server-side, and a re-scan of
the same path is idempotent.

Not a content hash — deliberately. A content key cannot be derived in SQL,
costs a full read of every file just to name it, and DANGLES when someone
corrects and re-saves a source file. With a path key that row still
resolves and reports a changed file_hash_full and modified date, which is
the more useful answer: this row came from that file, and that file has
since changed. Content identity is still recorded — file_gate stores
SHA-256 quick and full hashes and sets duplicate_group to the full one —
it just is not the key.

WRITTEN ON VERIFICATION, NOT ON START
-------------------------------------
A load that fails half way is not a fact about the database. Both writes
happen after the promote counts are known, and all three counts are kept:
"1,000 staged" and "1,000 in the database" are different claims and the
difference is the interesting part.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime

TABLE = "dataview.dv_global_file_catalog"

# Added if absent, never dropped or retyped — the cat_review_layer rule.
EXTRA_COLUMNS = [
    ("rows_staged", "int"),
    ("rows_promoted", "int"),
    ("rows_held", "int"),
    ("column_fingerprint", "nvarchar(64)"),
]


# ═════════════════════════════════════════════════════════════════════════ #
# hashing
# ═════════════════════════════════════════════════════════════════════════ #
def file_identity(path):
    """(inventory_id, quick_hash, full_hash) — all from file_gate.

    Deliberately NOT computed here. Two modules hashing the same file with
    different algorithms produce two answers to one question, and the one
    that ends up in the catalog wins by accident of call order.
    """
    from dataview.import_data.file_gate import (inventory_id, quick_hash,
                                                full_hash)
    ap = os.path.abspath(path)
    return inventory_id(ap), quick_hash(ap), full_hash(ap)


def register_file(engine, path, root=None, log=None):
    """Put the file in file_catalog.GLOBAL_FILE_CATALOG — the SAME way the
    extracted formats get there.

    classify() decides and upsert() writes; both already exist and both
    already know to touch only the identity columns and leave the
    pipeline's own state alone. A tabular load has no business having its
    own registration code.
    """
    try:
        from dataview.import_data import file_gate as _gate
        ap = os.path.abspath(path)
        dec = _gate.classify(engine, [ap], root=root, force=True)
        n, note = _gate.upsert(engine, dec, root=root)
        if log:
            log(f"  catalog: registered {os.path.basename(ap)}"
                + (f" ({note})" if note else ""))
        return dec.get(ap, {}).get("inventory_id")
    except Exception as e:
        if log:
            log(f"  catalog registration skipped: {type(e).__name__}: {e}")
        return None


def _doc_type(ext):
    e = (ext or "").lower().lstrip(".")
    if e in ("csv", "txt", "dat", "tsv", "prn"):
        return "TABULAR", e.upper()
    if e in ("xlsx", "xls", "xlsm"):
        return "TABULAR", "EXCEL"
    if e in ("las", "lis", "dlis"):
        return "LOG", e.upper()
    if e in ("segy", "sgy"):
        return "SEISMIC", "SEGY"
    return "OTHER", (e.upper() or "NONE")


# ═════════════════════════════════════════════════════════════════════════ #
# schema
# ═════════════════════════════════════════════════════════════════════════ #
def ensure_columns(engine, log=None):
    """Add the load-result columns if absent. Add-only — never drops,
    never retypes, never narrows (the cat_review_layer rule).

    Nothing widens inventory_id any more: the canonical id is 40 chars and
    every table already holds at least that.
    """
    from sqlalchemy import text
    schema, table = TABLE.split(".")
    with engine.begin() as cx:
        have = {r[0].lower() for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t"),
            {"s": schema, "t": table})}
        for col, typ in EXTRA_COLUMNS:
            if col.lower() not in have:
                cx.execute(text(f"ALTER TABLE {TABLE} ADD {col} {typ} NULL"))
                if log:
                    log(f"  + {TABLE}.{col} {typ}")


# ═════════════════════════════════════════════════════════════════════════ #
# recording
# ═════════════════════════════════════════════════════════════════════════ #
_MERGE = f"""
MERGE {TABLE} AS t
USING (SELECT :inventory_id AS inventory_id) AS s
   ON t.inventory_id = s.inventory_id
WHEN MATCHED THEN UPDATE SET
       full_path          = :full_path,
       file_name          = :file_name,
       file_ext           = :file_ext,
       file_size_kb       = :file_size_kb,
       file_hash          = :file_hash,
       file_hash_full     = :file_hash_full,
       duplicate_group    = :duplicate_group,
       modified_date      = :modified_date,
       scan_date          = :scan_date,
       doc_type_group     = :doc_type_group,
       doc_type           = :doc_type,
       catalog_status     = :catalog_status,
       catalog_table      = :catalog_table,
       ppdm_loaded_ind    = :ppdm_loaded_ind,
       root_path          = :root_path,
       rows_staged        = :rows_staged,
       rows_promoted      = :rows_promoted,
       rows_held          = :rows_held,
       column_fingerprint = :column_fingerprint,
       source             = :source,
       row_changed_by     = :who,
       row_changed_date   = :now
WHEN NOT MATCHED THEN INSERT
      (inventory_id, full_path, file_name, file_ext, file_size_kb,
       file_hash, file_hash_full, duplicate_group, modified_date, scan_date,
       doc_type_group, doc_type, catalog_status, catalog_table,
       ppdm_loaded_ind, root_path, rows_staged, rows_promoted, rows_held,
       column_fingerprint, source, row_created_by, row_created_date)
VALUES
      (:inventory_id, :full_path, :file_name, :file_ext, :file_size_kb,
       :file_hash, :file_hash_full, :duplicate_group, :modified_date, :scan_date,
       :doc_type_group, :doc_type, :catalog_status, :catalog_table,
       :ppdm_loaded_ind, :root_path, :rows_staged, :rows_promoted, :rows_held,
       :column_fingerprint, :source, :who, :now);
"""


def build_row(path, target=None, staged=None, promoted=None, held=None,
              fingerprint=None, user=None, source="LOADER", root=None):
    """Everything the ledger stores about one load. Separated from the
    write so it can be inspected and unit-tested without a database."""
    iid, quick, full = file_identity(path)
    st = os.stat(path)
    ext = os.path.splitext(path)[1]
    grp, dtype = _doc_type(ext)
    now = datetime.now()
    return {
        "inventory_id": iid,          # canonical: SHA1(UPPER(path))
        "full_path": str(path)[:1000],
        "file_name": os.path.basename(path)[:500],
        "file_ext": ext[:20] or None,
        "file_size_kb": round(st.st_size / 1024.0, 2),
        "file_hash": quick,           # SHA-256, same as the catalog's
        "file_hash_full": full,
        "duplicate_group": full,      # content identity, recorded not keyed
        "modified_date": datetime.fromtimestamp(st.st_mtime),
        "scan_date": now,
        "doc_type_group": grp,
        "doc_type": dtype,
        # A load that promoted nothing is STAGED, not LOADED. The status
        # must not claim more than the counts support.
        "catalog_status": ("LOADED" if (promoted or 0) > 0
                           else "STAGED" if (staged or 0) > 0
                           else "SCANNED"),
        "catalog_table": (target or "")[:80] or None,
        "ppdm_loaded_ind": "Y" if (promoted or 0) > 0 else "N",
        "root_path": (root or os.path.dirname(str(path)))[:500],
        "rows_staged": staged,
        "rows_promoted": promoted,
        "rows_held": held,
        "column_fingerprint": (fingerprint or "")[:64] or None,
        "source": source[:40],
        "who": (user or os.environ.get("USERNAME") or "loader")[:40],
        "now": now,
    }


def record_load(engine, path, target=None, staged=None, promoted=None,
                held=None, fingerprint=None, user=None, source="LOADER",
                root=None, register=True, log=None):
    """Register the file and record what the load did.

    Returns {"inventory_id", "registered", "ledger", "problems"} — a REPORT,
    not just an id, because the two halves fail differently and one of them
    matters far more than the other.

    Best effort in both halves: bookkeeping must never fail a load that
    already succeeded. But "must not fail the load" was read as "need not be
    mentioned", and those are not the same thing:

      * the LEDGER half is genuinely cosmetic — dv_global_file_catalog
        records staged/promoted/held counts, and losing them costs a report.
      * REGISTRATION is not. The promote has ALREADY stamped every inserted
        row with this file's INVENTORY_ID by the time we get here, so if the
        GLOBAL_FILE_CATALOG entry does not appear, those rows cite a source
        nothing can resolve — and the load still reports success.

    That is how 50 dv_well rows came to name D7E2B1D3… on 19 Aug with no
    catalog entry anywhere. The failure WAS logged, to a Streamlit progress
    pane that died with the session, and nothing downstream looked at the
    return value — so by the time the invariant caught it there was nothing
    left to say what had gone wrong. reconcile_orphans can no longer identify
    the file at all.

    So the outcome is returned rather than dropped. The caller decides what
    to do with it; what it may not do is not know.
    """
    from sqlalchemy import text
    out = {"inventory_id": None, "registered": None,
           "ledger": False, "problems": []}
    if register:
        out["registered"] = register_file(engine, path, root=root, log=log)
        if not out["registered"]:
            # LOUD, and named as provenance rather than as bookkeeping. The
            # rows are already stamped; this is the half that leaves them
            # orphaned.
            out["problems"].append(
                "PROVENANCE NOT REGISTERED: " + os.path.basename(str(path))
                + " is not in file_catalog.GLOBAL_FILE_CATALOG, so rows "
                  "promoted from it cite a source nothing can resolve")
            if log:
                log("  ** " + out["problems"][-1])
    try:
        ensure_columns(engine, log=log)
        row = build_row(path, target, staged, promoted, held, fingerprint,
                        user, source, root)
        with engine.begin() as cx:
            cx.execute(text(_MERGE), row)
        if log:
            log(f"  ledger: {row['file_name']} -> {target} "
                f"({promoted or 0:,} promoted) as {row['inventory_id'][:8]}…")
        out["inventory_id"] = row["inventory_id"]
        out["ledger"] = True
    except Exception as e:                       # never break a good load
        if log:
            log(f"  ledger skipped: {type(e).__name__}: {e}")
        out["problems"].append(f"ledger not written: {type(e).__name__}: {e}")
    return out


# ═════════════════════════════════════════════════════════════════════════ #
# reading it back
# ═════════════════════════════════════════════════════════════════════════ #
def history(engine, path=None, target=None, limit=50):
    """What has been loaded — by file, by target, or everything recent."""
    from sqlalchemy import text
    q = (f"SELECT TOP {int(limit)} scan_date, file_name, catalog_table, "
         f"rows_staged, rows_promoted, rows_held, catalog_status, "
         f"inventory_id, full_path FROM {TABLE} WITH (NOLOCK) WHERE 1 = 1")
    p = {}
    if path:
        q += " AND full_path = :p"
        p["p"] = str(path)
    if target:
        q += " AND catalog_table = :t"
        p["t"] = target
    q += " ORDER BY scan_date DESC"
    with engine.connect() as cx:
        return cx.execute(text(q), p).fetchall()


def duplicates(engine):
    """Files loaded more than once under different paths — same bytes."""
    from sqlalchemy import text
    with engine.connect() as cx:
        return cx.execute(text(
            f"SELECT duplicate_group, n = COUNT(*), "
            f"paths = STRING_AGG(CAST(full_path AS varchar(max)), ' | ') "
            f"FROM {TABLE} WITH (NOLOCK) "
            f"WHERE duplicate_group IS NOT NULL "
            f"GROUP BY duplicate_group HAVING COUNT(*) > 1")).fetchall()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Inspect the load ledger.")
    ap.add_argument("--server", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
    ap.add_argument("--target", help="filter to one target table")
    ap.add_argument("--limit", type=int, default=50)
    a = ap.parse_args()
    from sqlalchemy import create_engine
    url = (f"mssql+pyodbc://@{a.server}/{a.database}"
           f"?driver={a.driver.replace(' ', '+')}&trusted_connection=yes")
    eng = create_engine(url)
    rows = history(eng, target=a.target, limit=a.limit)
    print(f"{'when':20} {'file':34} {'target':22} {'staged':>8} "
          f"{'promoted':>9} {'held':>6}")
    for r in rows:
        print(f"{str(r[0])[:19]:20} {str(r[1])[:34]:34} {str(r[2] or ''):22} "
              f"{r[3] or 0:8,} {r[4] or 0:9,} {r[5] or 0:6,}")
    dups = duplicates(eng)
    if dups:
        print(f"\n{len(dups)} file(s) loaded from more than one path:")
        for g, n, paths in dups[:10]:
            print(f"  {n}x  {paths[:150]}")
