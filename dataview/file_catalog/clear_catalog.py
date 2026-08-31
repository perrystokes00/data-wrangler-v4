r"""
clear_catalog.py — reset the document pipeline to empty.  DRY-RUN by default.

Clears (FK constraints disabled, rows deleted, identities reseeded, constraints
re-enabled — all in one transaction):

  * every table in file_catalog   (GLOBAL_FILE_CATALOG, FILE_*_HEADER, cat_*)
  * every table in las_catalog     (binary curve / seismic detail)
  * from the catalog-derived dv_* tables (the promote allowlist: dv_well +
    details, dv_log_curve, dv_prod_*), ONLY the rows whose INVENTORY_ID
    resolves to a DOCUMENT in the file catalog — a pdf/docx/html/rtf that a
    recogniser had to read. Rows from a csv/xlsx/las, and rows with no
    INVENTORY_ID at all, are left intact, as is every reference / spatial /
    lookup table (dv_country, dv_province_state, dv_county, dv_r_*,
    api_state_code, …).

    WHY NOT "INVENTORY_ID IS NOT NULL", WHICH THIS USED TO DO: that test
    meant "came from the catalog" until 3 August 2026, when the bulk
    tabular loader began stamping ids on its own rows and registering its
    CSVs in GLOBAL_FILE_CATALOG. After that change the old predicate
    matched EVERYTHING — on the database this was found on it would have
    deleted 6,737 bulk-loaded production rows and 411 completions while
    reporting them as "catalog rows". A delete must not inherit a
    definition that a later feature quietly invalidated.

--scope documents+las WIDENS THAT ID SET to the log family (.las/.lis/.dlis),
    so las_catalog, the LAS entries in GLOBAL_FILE_CATALOG and the dv_* rows
    those files produced clear TOGETHER. It stays symmetric — the property
    'documents' was built for — and CSV/XLSX rows are still left alone. Use it
    when a log reload needs a clean slate; the default leaves logs standing.

Optionally deletes the on-disk vault tree (<vault-root>\curated).

  python clear_catalog.py                                  # dry-run: list + counts
  python clear_catalog.py --apply                          # clear all DB tables
  python clear_catalog.py --apply --scope documents+las    # documents AND log files
  python clear_catalog.py --apply --vault C:\Bulk\Vault    # also delete vault\curated
  python clear_catalog.py --apply --no-dv                  # leave dv_* alone
  python clear_catalog.py --apply --keep PIPELINE_RUN      # preserve named tables

Never touches WELL_REF, the reference seeders, any reference/spatial table,
or the PROTECTED tables below — dv_column_map above all, which holds every
column mapping ever approved and is the one thing here a reload cannot
reproduce.
"""
import argparse
import os
import shutil
import sys

import pyodbc

CAT_SCHEMA = "file_catalog"
LAS_SCHEMA = "las_catalog"
DV_SCHEMA  = "dataview"

# NEVER CLEARED, whatever the allowlist says. These hold LEARNED STATE —
# decisions a person made that no reload reproduces:
#
#   dv_column_map     the synonym store and fingerprint recall. Every column
#                     mapping ever approved, and the reason a remembered
#                     folder loads without asking a single question. Months
#                     of accumulated decisions, and NOT synthetic even in a
#                     synthetic database.
#   dv_target_attribute   schema metadata the fit pre-flight reads.
#   dv_global_file_catalog  the load ledger: provenance, not data.
#
# They are already absent from the allowlist below, so today this changes
# nothing. It is here because "protected by omission" is not protection:
# DV_TABLES is IMPORTED from build_catalog_mirror.MIRROR_TABLES, and the day
# somebody adds a table there this guarantee would vanish silently. The same
# weakness in purge_source.py was fixed the same way, for the same reason.
# ONE SOURCE OF TRUTH -- shared with demo_reset, which used to carry its own
# copy and disagreed about dv_global_file_catalog. See reset_protection.
from dataview.core.reset_protection import PROTECTED

# Catalog-derived dv_* tables = the promote allowlist, IMPORTED so this tool
# and promote can never disagree about what the catalog owns.
#
# THE PATH IS THE REPO ROOT, not this directory. `dataview.file_catalog.…`
# resolves from the root; inserting the file_catalog folder resolves nothing.
# So the import failed when this ran as a SCRIPT and succeeded when the page
# imported it as a module — and the two then cleared DIFFERENT SETS OF TABLES,
# silently, for weeks.
#
# AND THERE IS NO FALLBACK. The hardcoded list that used to sit here had
# drifted out of step: it omitted dv_well_casing and dv_well_perforation and
# added dv_log_curve, so a clear running on it left casing and perforation
# rows behind while reporting the catalog-derived set as cleared. A
# DESTRUCTIVE TOOL MUST NOT GUESS AT ITS OWN SCOPE — if the import fails that
# is a deployment fault, and it should say so before anything is deleted.
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from dataview.file_catalog.build_catalog_mirror import MIRROR_TABLES as DV_TABLES

# A row is CATALOG-DERIVED when the file behind it is a DOCUMENT — something
# a recogniser had to interpret. A csv/xlsx/las was parsed, and its rows
# belong to whoever loaded them, not to this tool.
DOC_EXTS = ('.pdf', '.docx', '.doc', '.html', '.htm', '.rtf', '.pptx',
            '.msg', '.odt')

# The well-log family: the files the LAS / LIS / DLIS loaders read and whose
# curve detail lands in las_catalog. NOT documents — a loader parsed them, it
# didn't interpret them — so they are a SEPARATE opt-in, never part of the
# default scope.
#
# All three are here rather than '.las' alone because they populate the SAME
# tables through the same log path (las_catalog.*, dv_well_log,
# dv_well_log_curve). Clearing only '.las' would empty part of a table and
# leave LIS/DLIS curves behind — a half-cleared log catalog that looks cleared.
# If a LAS-only scope is ever wanted it belongs as its own named scope, not as
# a quiet narrowing of this one.
LOG_EXTS = ('.las', '.lis', '.dlis')

# scope name -> the extensions whose INVENTORY_IDs are in delete scope.
# 'all' is absent: it doesn't scope by id at all, it wipes the catalog tables.
_SCOPE_EXTS = {
    "documents":     DOC_EXTS,
    "documents+las": DOC_EXTS + LOG_EXTS,
}
SCOPES = ("documents", "documents+las", "all")


def scope_label(scope):
    """One line naming exactly what a scope deletes. Used by the CLI and both
    Streamlit panels so the three can't describe the same scope differently."""
    return {
        "documents":     "Documents only — pdf / docx / html rows",
        "documents+las": "Documents + logs — also las / lis / dlis rows",
        "all":           "Everything — wipe the catalog wholesale",
    }[scope]


def row_label(scope):
    """What the per-table 'document' tag means for THIS run's scope.

    'all' reads as 'documents' here and that is not a slip: under 'all' the
    catalog tables are wiped wholesale, but the dv_* block still id-scopes and
    the ids it uses are the DOCUMENT ids. Those rows are document-derived, so
    that is what the row must say.
    """
    return ("document + log-derived rows only" if scope == "documents+las"
            else "document-derived rows only")


# The document ids for this run. A MODULE-LEVEL LIST, not a #temp table.
#
# The first version materialised #doc_ids and every query referenced it.
# That works from the CLI, where one pyodbc cursor runs the whole job — and
# FAILS from the File Catalog page with "Invalid object name '#doc_ids'",
# because a local temp table lives in the SESSION that created it and the
# page runs its statements on different pooled connections. Carrying the
# ids in Python is connection-model agnostic: CLI, SQLAlchemy pool, or a
# Streamlit page that reconnects between calls.
#
# Size is not a concern — this is one row per DOCUMENT in the catalog, and
# the IN lists are chunked below.
_DOC_IDS = []

# The scope the ids in _DOC_IDS were captured under. gather() checks it, because
# capture and gather each take `scope` separately and a caller that updates one
# and not the other gets a delete that says "documents+las" and behaves like
# "documents" — silently, and only visibly as rows that survived a clear.
_DOC_SCOPE = "documents"


def capture_doc_ids(cur, log=print, scope="documents"):
    """Read the in-scope inventory ids into memory. Returns the list.

    scope 'documents' captures pdf/docx/html/… only; 'documents+las' adds the
    log family (.las/.lis/.dlis) so the LAS side of the catalog and the dv_*
    rows it produced clear together. 'all' captures the same ids as
    'documents' — under 'all' the catalog tables are wiped wholesale and the
    ids only govern the dv_* deletes, which stay document-scoped.

    MUST run before anything clears GLOBAL_FILE_CATALOG. The dv_* deletes
    need to know which ids came from documents, and this tool empties the
    catalog in the SAME transaction — so by the time those deletes run the
    evidence is gone. Capturing first is the only ordering that works.
    """
    global _DOC_IDS, _DOC_SCOPE
    exts = _SCOPE_EXTS.get(scope, DOC_EXTS)
    marks = ", ".join("?" for _ in exts)
    cur.execute(
        "SELECT DISTINCT INVENTORY_ID FROM file_catalog.GLOBAL_FILE_CATALOG "
        "WHERE INVENTORY_ID IS NOT NULL AND LOWER(RIGHT(FILE_NAME, "
        "CHARINDEX('.', REVERSE(FILE_NAME) + '.'))) IN (" + marks + ")",
        *exts)
    _DOC_IDS = [r[0] for r in cur.fetchall() if r[0]]
    _DOC_SCOPE = scope
    _las = scope == "documents+las"
    kind = "document + log" if _las else "document"
    spared = "csv/xlsx" if _las else "csv/xlsx/las"
    log(f"-- {kind} files in the catalog: {len(_DOC_IDS):,} "
        f"(rows from these are in scope; {spared} rows are not)")
    return _DOC_IDS


def _chunks(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _count_doc_rows(cur, schema, table, ids):
    """COUNT(*) for rows whose inventory_id is one of ids, chunked."""
    if not ids:
        return 0
    total = 0
    for chunk in _chunks(ids):
        marks = ", ".join("?" for _ in chunk)
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}] "
                        f"WHERE INVENTORY_ID IN ({marks})", *chunk)
            total += int(cur.fetchone()[0] or 0)
        except Exception:
            return -1
    return total


def _delete_doc_rows(cur, schema, table, ids):
    """DELETE those rows, chunked. Returns rows removed."""
    n = 0
    for chunk in _chunks(ids):
        marks = ", ".join("?" for _ in chunk)
        cur.execute(f"DELETE FROM [{schema}].[{table}] "
                    f"WHERE INVENTORY_ID IN ({marks})", *chunk)
        n += max(cur.rowcount, 0)
    return n


# SET options required for DML on dv_well (it carries a spatial geography index).
_SET_OPTS = ("SET QUOTED_IDENTIFIER ON; SET ANSI_NULLS ON; SET ANSI_PADDING ON; "
             "SET ANSI_WARNINGS ON; SET ARITHABORT ON; "
             "SET CONCAT_NULL_YIELDS_NULL ON; SET NUMERIC_ROUNDABORT OFF;")


def connect(server, database):
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
        f"DATABASE={database};Trusted_Connection=yes;",
        autocommit=False)


def _schema_tables(cur, schema):
    cur.execute(
        "SELECT t.name FROM sys.tables t "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE s.name = ? ORDER BY t.name", schema)
    return [r[0] for r in cur.fetchall()]


def _has_col(cur, schema, table, col):
    cur.execute(
        "SELECT 1 FROM sys.columns "
        "WHERE object_id = OBJECT_ID(?) AND name = ?",
        f"{schema}.{table}", col)
    return cur.fetchone() is not None


def _count(cur, schema, table, where=""):
    try:
        cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}] {where}")
        return cur.fetchone()[0]
    except Exception:
        return -1


def gather(cur, do_dv, keep, doc_ids=None, scope="documents"):
    """Everything we would clear, as [(schema, table, rowcount, scope)].

    scope is one of:
      'all'        delete every row (working tables with no INVENTORY_ID)
      'document'   delete only rows whose INVENTORY_ID is one of the ids
                   captured for this run — pdf/docx/… under scope
                   'documents', plus .las/.lis/.dlis under 'documents+las'.
                   Rows from any other data file, and rows with no id, are
                   left untouched. (The tag is named for the default scope
                   and is a STABLE contract — both Streamlit panels key a
                   label map off these four strings. Call row_label(scope)
                   for wording that matches the run.)
      'skip'       a dv_* table with no INVENTORY_ID column — can't tell catalog
                   rows apart, so it is left alone
      'protected'  learned state, never cleared

    THE `scope` ARGUMENT — WHY IT EXISTS
    ------------------------------------
    'documents' (default) is SYMMETRIC: the catalog side is scoped exactly the
    way the dv_* side already was. That matters because the two used to
    disagree — dv_* deletions spared a CSV-derived row on purpose while
    file_catalog was wiped WHOLESALE, so the row survived and the
    GLOBAL_FILE_CATALOG entry naming its source did not. The result was 300
    dv_well rows citing a source nothing could resolve: orphaned provenance,
    manufactured by the clear itself, every time it ran.

    IF YOU KEEP THE ROWS, KEEP THEIR PROVENANCE.

    'documents+las' widens BOTH sides the same way: the log family joins the
    id set, so las_catalog, the LAS entries in GLOBAL_FILE_CATALOG and the
    dv_* rows those files produced go together. Symmetry is the whole point —
    clearing las_catalog while its GLOBAL_FILE_CATALOG entries survive would
    manufacture the same orphaned provenance 'documents' exists to avoid.

    'all' restores the old wholesale behaviour for anyone who wants a genuine
    empty catalog. It is honest about the consequence rather than hiding it:
    main() warns that non-document dv_* rows will be left orphaned, because
    they will be.
    """
    keepset = {k.upper() for k in keep}
    # Callers that did not capture first still work: fall back to whatever
    # capture_doc_ids last read, and capture now if it never ran.
    if doc_ids is None:
        doc_ids = (_DOC_IDS if _DOC_SCOPE == scope else []) \
            or capture_doc_ids(cur, lambda *_a: None, scope=scope)
    elif _DOC_SCOPE != scope:
        # The ids were captured for a DIFFERENT scope than the one being
        # gathered. Under 'documents+las' that means the LAS ids are missing
        # and the run quietly degrades to a document-only delete while every
        # caption says otherwise. A destructive tool must not proceed on a
        # scope it can't vouch for.
        raise ValueError(
            f"scope mismatch: ids were captured for '{_DOC_SCOPE}' but "
            f"gather() was asked for '{scope}'. Pass the same scope to "
            f"capture_doc_ids() and gather().")
    out = []
    for sch in (CAT_SCHEMA, LAS_SCHEMA):
        for t in _schema_tables(cur, sch):
            if t.upper() in keepset:
                continue
            # PROTECTED BY NAME IN EVERY SCHEMA, not just dataview.
            # These tables live in dataview today, so this branch is
            # theoretical — but a protection that depends on WHERE a
            # table happens to sit is the same "protected by omission"
            # weakness this set exists to remove.
            if t.lower() in PROTECTED:
                out.append((sch, t, -1, "protected"))
                continue
            # SYMMETRIC WITH THE dv_* SIDE. A catalog table that carries
            # INVENTORY_ID can be scoped to the captured ids, so a CSV's (and,
            # under 'documents', a LAS file's) entry survives alongside the
            # dv_* rows this tool deliberately keeps. A table with no
            # INVENTORY_ID is a working table and is cleared wholesale as
            # before.
            #
            # `scope != "all"` rather than `== "documents"`: every new scope
            # is an id-scoped one, so the wholesale branch must be reached by
            # NAMING 'all', never by failing to match a list of scope names
            # that a later scope forgets to join.
            if scope != "all" and _has_col(cur, sch, t, "INVENTORY_ID"):
                n = _count_doc_rows(cur, sch, t, doc_ids)
                out.append((sch, t, n, "document"))
            else:
                out.append((sch, t, _count(cur, sch, t), "all"))
    if do_dv:
        existing = set(_schema_tables(cur, DV_SCHEMA))
        for t in DV_TABLES:
            if t not in existing or t.upper() in keepset:
                continue
            if t.lower() in PROTECTED:
                out.append((DV_SCHEMA, t, -1, "protected"))
                continue
            if _has_col(cur, DV_SCHEMA, t, "INVENTORY_ID"):
                n = _count_doc_rows(cur, DV_SCHEMA, t, doc_ids)
                out.append((DV_SCHEMA, t, n, "document"))
            else:
                out.append((DV_SCHEMA, t, -1, "skip"))
    return out


def clear(cur, tables, log, doc_ids=None, scope=None):
    # `scope` only chooses the wording of the log lines — what actually gets
    # deleted is fixed by the per-row scope tags gather() produced. Defaults to
    # the scope the ids were captured under, so an old 4-arg caller still
    # reports the truth.
    if doc_ids is None:
        doc_ids = _DOC_IDS
    kept = ("kept csv/xlsx-derived" if (scope or _DOC_SCOPE) == "documents+las"
            else "kept bulk-loaded")
    # disable FK constraints on every table we'll touch so delete order is free
    targets = [r for r in tables if r[3] not in ("skip", "protected")]
    for sch, tbl, _, _ in targets:
        cur.execute(f"ALTER TABLE [{sch}].[{tbl}] NOCHECK CONSTRAINT ALL")
    # delete per scope
    for sch, tbl, n, scope in tables:
        if scope == "protected":
            log(f"  PROTECT {sch}.{tbl}  (learned state — never cleared)")
            continue
        if scope == "skip":
            log(f"  SKIP    {sch}.{tbl}  (no INVENTORY_ID — can't scope to "
                f"catalog rows; left intact)")
            continue
        if scope == "document":
            got = _delete_doc_rows(cur, sch, tbl, doc_ids)
            log(f"  cleared {sch}.{tbl}  ({got:,} in-scope rows; {kept})")
        else:  # 'all'
            cur.execute(f"DELETE FROM [{sch}].[{tbl}]")
            cur.execute(
                f"IF EXISTS (SELECT 1 FROM sys.identity_columns "
                f"WHERE object_id = OBJECT_ID('{sch}.{tbl}')) "
                f"DBCC CHECKIDENT('{sch}.{tbl}', RESEED, 0) WITH NO_INFOMSGS")
            log(f"  cleared {sch}.{tbl}  ({n:,} rows)")
    # re-enable constraints (plain CHECK — don't re-validate surviving rows)
    for sch, tbl, _, _ in targets:
        cur.execute(f"ALTER TABLE [{sch}].[{tbl}] CHECK CONSTRAINT ALL")


def clear_vault(vault_root, apply, log):
    curated = os.path.join(vault_root, "curated")
    if not os.path.isdir(curated):
        log(f"  vault: nothing at {curated}")
        return
    nfiles = sum(len(f) for _, _, f in os.walk(curated))
    if apply:
        shutil.rmtree(curated)
        log(f"  vault: deleted {curated}  ({nfiles:,} files)")
    else:
        log(f"  vault: would delete {curated}  ({nfiles:,} files)")


def main():
    ap = argparse.ArgumentParser(
        description="Clear the document-pipeline tables (+ optional vault).")
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry-run, counts only)")
    ap.add_argument("--no-dv", action="store_true",
                    help="leave the dv_* catalog tables alone")
    ap.add_argument("--keep", nargs="*", default=[],
                    help="table name(s) to preserve, e.g. --keep PIPELINE_RUN")
    ap.add_argument("--vault", default=None,
                    help=r"vault root; deletes <root>\curated")
    ap.add_argument("--scope", choices=SCOPES, default="documents",
                    help="documents (default): clear only document-derived "
                         "catalog rows, so a CSV/LAS entry survives with the "
                         "dv_* rows it produced. documents+las: also clear the "
                         "log family (.las/.lis/.dlis) — las_catalog, their "
                         "catalog entries and the dv_* rows they produced go "
                         "together; CSV/XLSX still survive. all: wipe the "
                         "catalog wholesale — leaves non-document dv_* rows "
                         "orphaned.")
    a = ap.parse_args()

    con = connect(a.server, a.database)
    cur = con.cursor()
    cur.execute(_SET_OPTS)

    # BEFORE anything else: the dv_* scope depends on the catalog, and
    # this tool empties the catalog. Same scope to both calls — gather()
    # rejects a mismatch rather than quietly deleting the narrower set.
    ids = capture_doc_ids(cur, print, scope=a.scope)
    tables = gather(cur, do_dv=not a.no_dv, keep=a.keep, doc_ids=ids,
                    scope=a.scope)
    total = sum(n for _, _, n, sc in tables
                if n > 0 and sc not in ("skip", "protected"))

    print(f"-- target : {a.server} / {a.database}")
    print(f"-- mode   : {'APPLY (delete)' if a.apply else 'DRY-RUN'}"
          f"{'  (dv_* kept)' if a.no_dv else ''}")
    print(f"-- scope  : {a.scope}  ({scope_label(a.scope)})")
    if a.scope == "documents+las":
        print("-- LAS/LIS/DLIS rows are IN SCOPE: las_catalog, their "
              "GLOBAL_FILE_CATALOG entries and the dv_* rows they produced "
              "(dv_well_log, dv_well_log_curve, …) are deleted together. "
              "CSV/XLSX-derived rows are untouched.")
    if a.scope == "all" and not a.no_dv:
        print("-- WARNING: --scope all wipes the catalog wholesale while the "
              "dv_* deletes stay document-scoped, so every non-document dv_* "
              "row (CSV/LAS-derived) will be left citing a source that no "
              "longer exists. That is orphaned provenance, and selftest's "
              "invariants tier will report it.")
    print(f"\n{'table':48} {'rows':>10}  scope")
    print("-" * 72)
    for sch, tbl, n, scope in tables:
        rows = ("   (skip)" if scope == "skip"
                else "  (keep)" if scope == "protected" else f"{n:>10,}")
        tag = {"all": "all rows",
               "document": row_label(a.scope),
               "protected": "PROTECTED — learned state, never cleared",
               "skip": "no INVENTORY_ID — left intact"}[scope]
        print(f"{sch + '.' + tbl:48} {rows}  {tag}")
    print("-" * 72)
    print(f"{'TOTAL rows to delete':48} {total:>10,}")
    if a.vault:
        clear_vault(a.vault, False, print)   # always show the vault plan

    if not a.apply:
        print("\n-- dry-run; nothing deleted. Re-run with --apply to clear.")
        con.close()
        return 0

    try:
        print()
        clear(cur, tables, print, doc_ids=ids, scope=a.scope)
        con.commit()
    except Exception as e:
        con.rollback()
        print(f"\n-- ERROR (rolled back, nothing deleted): {e}", file=sys.stderr)
        con.close()
        return 1
    con.close()

    if a.vault:
        clear_vault(a.vault, True, print)

    print("\n-- done. Cleared file_catalog + las_catalog"
          + ("" if a.no_dv else " + dv_* catalog tables")
          + (" + vault\\curated" if a.vault else "") + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
