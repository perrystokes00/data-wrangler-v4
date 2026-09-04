r"""
collect_final_documents.py  —  Data Wrangler v3
================================================================================
Find every catalogued document whose FILE PATH or FILE NAME contains the word
"final" and copy it into the vault under:

    <vault>\Final_Documents\Well\
    <vault>\Final_Documents\Seismic\
    <vault>\Final_Documents\Other\

Classification (Well / Seismic / Other) comes from the catalog header join, not
from opening the file. Nothing is written back to the database — this only
reads file_catalog and copies files, then writes a CSV report next to the copies.

Matching
--------
SQL pre-filters with  FILE_PATH LIKE '%final%'  (case-insensitive collation),
then a word-boundary check confirms it's the *word* "final" — so "Final_Report",
"WELL-FINAL.pdf" and "...\Final\..." match, but "semifinal" and "finalize" do
not. Use --substring to keep the loose LIKE behaviour instead.

Usage
-----
    py collect_final_documents.py                 # copy into C:\Bulk\Vault\Final_Documents
    py collect_final_documents.py --dry-run       # preview, copy nothing
    py collect_final_documents.py --vault D:\Vault
    py collect_final_documents.py --all-ext       # any file type, not just docs
    py collect_final_documents.py --word draft     # match a different word

Requires:  pip install pyodbc
"""
import argparse
import csv
import os
import re
import shutil
import sys
from datetime import datetime

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_SERVER = r"PERRY\SQLEXPRESS"
DEFAULT_DB     = "DataView_Demo"
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"
try:
    from dataview.core.config import DW_VAULT as DEFAULT_VAULT
except Exception:            # a tool must still run if config cannot import
    DEFAULT_VAULT = r"C:\Bulk\Vault"
DEFAULT_WORD   = "final"

# Document-ish extensions copied by default (override with --types / --all-ext).
DOC_EXTS = {
    "pdf", "doc", "docx", "rtf", "txt",
    "xls", "xlsx", "xlsm", "csv", "tsv",
    "ppt", "pptx",
}


# ── SQL Server ──────────────────────────────────────────────────────────────
def sql_conn(a):
    try:
        import pyodbc
    except ImportError:
        sys.exit("pip install pyodbc")
    return pyodbc.connect(
        f"DRIVER={{{a.odbc_driver}}};SERVER={a.server};DATABASE={a.database};"
        "Trusted_Connection=yes;", autocommit=True)


def fetch_rows(conn, word):
    """All non-deleted catalog rows whose FILE_PATH contains `word`, with the
    header fields needed to classify Well / Seismic / Other."""
    sql = """
        SELECT g.INVENTORY_ID, g.FILE_PATH, g.FILE_EXT, g.FILE_TYPE_GROUP,
               g.MATCHED_UWI, wh.UWI, wh.WELL_NAME, wh.REPORT_TYPE,
               sh.SURVEY_NAME
        FROM file_catalog.GLOBAL_FILE_CATALOG g
        LEFT JOIN file_catalog.FILE_WELL_HEADER wh ON wh.INVENTORY_ID = g.INVENTORY_ID
        LEFT JOIN file_catalog.FILE_SEIS_HEADER sh ON sh.INVENTORY_ID = g.INVENTORY_ID
        WHERE (g.FLAG_DELETE IS NULL OR g.FLAG_DELETE = 0)
          AND g.FILE_PATH LIKE ?
        ORDER BY g.FILE_PATH
    """
    cur = conn.cursor()
    cur.execute(sql, f"%{word}%")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── Classification ────────────────────────────────────────────────────────────
def classify(row):
    """Well / Seismic / Other from the catalog (no file access)."""
    grp = (row.get("FILE_TYPE_GROUP") or "").lower()
    rpt = (row.get("REPORT_TYPE") or "").lower()
    if (row.get("SURVEY_NAME") or "").strip() or "seis" in grp or "seis" in rpt:
        return "Seismic"
    if (row.get("UWI") or row.get("WELL_NAME") or row.get("MATCHED_UWI")
            or "well" in grp or "log" in rpt or "well" in rpt):
        return "Well"
    return "Other"


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Copy catalogued documents with 'final' in their path/name "
                    "into the vault, split by Well/Seismic/Other.")
    p.add_argument("--server", default=DEFAULT_SERVER)
    p.add_argument("--database", default=DEFAULT_DB)
    p.add_argument("--odbc-driver", default=DEFAULT_DRIVER)
    p.add_argument("--vault", default=DEFAULT_VAULT,
                   help=r"Vault root; copies land in <vault>\Final_Documents")
    p.add_argument("--dest", default=None,
                   help="Override the Final_Documents folder entirely.")
    p.add_argument("--word", default=DEFAULT_WORD,
                   help="Word to look for in the path/name (default: final)")
    p.add_argument("--types", default=None,
                   help="Comma list of extensions to include "
                        "(default: common document types)")
    p.add_argument("--all-ext", action="store_true",
                   help="Copy any matching file regardless of extension.")
    p.add_argument("--substring", action="store_true",
                   help="Match 'final' anywhere (loose), not just as a word.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would copy without copying.")
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args()
    conn = sql_conn(a)
    collect(conn, a)


def collect(conn, a, log=print):
    """Core 'final documents' collector — callable from CLI or app UI.
    Reads the catalog via `conn` and copies matching files to the vault.
    `log` receives progress lines. Returns a counts dict."""
    say = log
    word     = a.word.strip().lower()
    dest_root = a.dest or os.path.join(a.vault, "Final_Documents")
    exts = (None if a.all_ext else
            {e.strip().lower().lstrip(".") for e in a.types.split(",")} if a.types
            else DOC_EXTS)

    # Word-boundary matcher: any non-alphanumeric (incl. _ - . space \ /) bounds
    # the word, so "Final_Report" matches but "semifinal"/"finalize" do not.
    word_re = re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
                         re.IGNORECASE)

    say(f"[CONNECT] {getattr(a, 'server', '?')} / {getattr(a, 'database', '?')}")
    rows = fetch_rows(conn, word)
    say(f"[QUERY ] {len(rows):,} catalog row(s) with '{word}' in the path")

    report = []
    counts = {"Well": 0, "Seismic": 0, "Other": 0}
    copied = exists = missing = filtered = nonword = errors = 0

    for row in rows:
        path = (row.get("FILE_PATH") or "").strip()
        if not path:
            continue
        name = os.path.basename(path)
        ext  = os.path.splitext(name)[1].lower().lstrip(".")

        # Word-boundary confirmation (skip if --substring).
        if not a.substring and not word_re.search(path):
            nonword += 1
            continue
        # Extension gate.
        if exts is not None and ext not in exts:
            filtered += 1
            continue

        cat = classify(row)
        dest_dir  = os.path.join(dest_root, cat)
        dest_file = os.path.join(dest_dir, name)

        status, reason = "", ""
        if not os.path.isfile(path):
            status, reason = "missing", "source not on disk"
            missing += 1
        elif a.dry_run:
            status = "would-copy"
            counts[cat] += 1
        else:
            try:
                os.makedirs(dest_dir, exist_ok=True)
                if (os.path.exists(dest_file)
                        and os.path.getsize(dest_file) == os.path.getsize(path)):
                    status, reason = "exists", "same name+size already in vault"
                    exists += 1
                else:
                    # Name clash with a different file: keep both.
                    if os.path.exists(dest_file):
                        stem, e = os.path.splitext(name)
                        n = 2
                        while os.path.exists(dest_file):
                            dest_file = os.path.join(dest_dir, f"{stem} ({n}){e}")
                            n += 1
                    shutil.copy2(path, dest_file)
                    status = "copied"
                    copied += 1
                    counts[cat] += 1
            except Exception as e:
                status, reason = "error", str(e)[:200]
                errors += 1

        report.append({
            "inventory_id": row.get("INVENTORY_ID"),
            "category": cat, "file_name": name, "ext": ext,
            "source_path": path,
            "dest_path": "" if status in ("missing", "error") else dest_file,
            "status": status, "reason": reason,
        })

        if a.limit and len(report) >= a.limit:
            break

    # ── Report ────────────────────────────────────────────────────────────────
    if not a.dry_run and report:
        os.makedirs(dest_root, exist_ok=True)
    rpt_path = os.path.join(
        dest_root if os.path.isdir(dest_root) else ".",
        f"_final_collect_report_{datetime.now():%Y%m%d_%H%M%S}.csv")
    try:
        with open(rpt_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "inventory_id", "category", "file_name", "ext",
                "source_path", "dest_path", "status", "reason"])
            w.writeheader()
            w.writerows(report)
        rpt_note = rpt_path
    except Exception as e:
        rpt_note = f"(report not written: {e})"

    verb = "Would copy" if a.dry_run else "Copied"
    say("\n──────── summary ────────")
    say(f"{verb:>12}: {sum(counts.values()):,}  "
        f"(Well {counts['Well']:,} · Seismic {counts['Seismic']:,} · "
        f"Other {counts['Other']:,})")
    if not a.dry_run:
        say(f"{'already in':>12}: {exists:,}")
    say(f"{'not the word':>12}: {nonword:,}")
    say(f"{'wrong type':>12}: {filtered:,}")
    say(f"{'missing src':>12}: {missing:,}")
    say(f"{'errors':>12}: {errors:,}")
    say(f"\nDestination: {dest_root}")
    say(f"Report:      {rpt_note}")
    if a.dry_run:
        say("\n(dry run — no files copied)")
    return {"copied": sum(counts.values()), **counts,
            "already_in": exists, "missing": missing, "report": rpt_note}


if __name__ == "__main__":
    main()
