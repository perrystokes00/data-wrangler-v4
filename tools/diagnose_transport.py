"""
diagnose_transport.py — Where is the pyodbc wall?

Measures Wells-mode query performance across 5 transport methods × 2 tables ×
2 column shapes (popup-ready wide vs minimal narrow). Outputs a timing table
plus per-row analysis so we can answer:

    Q1: How fast can SQL Server actually deliver 55K / 477K wells?
    Q2: Where does pyodbc stop scaling?
    Q3: How wide a column projection matters?
    Q4: What's the live-query budget for the page?

Methods tested:
    1. pyodbc + plain SELECT          — fetchall()
    2. pyodbc + FOR JSON PATH         — current page pattern
    3. pyodbc + pd.read_sql chunked   — chunksize=5000
    4. BCP OUT + Python CSV parse     — the bypass pattern
    5. BCP OUT + FOR JSON to file     — server emits JSON, BCP streams to disk

Tables tested:
    dataview_gom.well  (~55K rows, 17 columns)
    dataview.dv_well   (~477K rows, 22 columns)

Run:
    python diagnose_transport.py
    python diagnose_transport.py --table gom        # GoM only
    python diagnose_transport.py --skip-slow        # skip methods known to hang
    python diagnose_transport.py --csv-out timings.csv

Output: a Markdown table in stdout AND a transport_diagnostic.md file with
detailed analysis and recommendations.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

import pandas as pd
from sqlalchemy import create_engine, text


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
CONN_STR = (
    "mssql+pyodbc://@localhost\\SQLEXPRESS/DataView_Demo"
    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
)

BCP_SERVER = r"localhost\SQLEXPRESS"
# DERIVED FROM CONN_STR, NEVER TYPED A SECOND TIME. This said "DataView"
# while CONN_STR two lines up said DataView_Demo, so THIS TOOL'S OUTPUT WAS
# NOT A COMPARISON: the pyodbc methods ran against one database and the bcp
# methods against another -- and since the login cannot open DataView, every
# bcp row it ever reported was a failure timing dressed as a measurement.
# It also PRINTS this name at startup ("Connecting to ...") and then connects
# with CONN_STR, so it announced the wrong database and carried on.
#
# A benchmark that silently changes one variable it is not measuring is worse
# than no benchmark. One source, parsed, so they cannot drift again.
BCP_DATABASE = CONN_STR.rsplit("/", 1)[-1].split("?", 1)[0]

WORK_DIR = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "dw_transport_diag"

# Methods that hung yesterday at 477K rows. With --skip-slow, we skip these
# for the big table. Per-table because GoM at 55K may not trigger the hang.
METHODS_SKIP_AT_SCALE = {"pyodbc_select", "pyodbc_for_json"}
SCALE_THRESHOLD = 200_000   # rows above which we'd skip "slow" methods


# -----------------------------------------------------------------------------
# Column shapes — wide (popup-ready) vs narrow (minimal)
# -----------------------------------------------------------------------------
# Popup-ready: matches what _qry_wells and _qry_gom_wells select.
# Narrow:     only the columns the H3 cell drill actually needs.
#
# This isolates "is the wide projection itself the slow part, or pyodbc?"
# -----------------------------------------------------------------------------

GOM_WIDE_SELECT = """
    SELECT CONVERT(VARCHAR(36), w.well_id) AS uwi,
           w.well_name,
           ISNULL(w.type_code,   'Unknown') AS well_type,
           ISNULL(w.status_code, 'Unknown') AS well_status,
           w.surface_latitude  AS lat,
           w.surface_longitude AS lon,
           CAST('' AS NVARCHAR(40))  AS county,
           w.region                 AS province_state,
           w.api_well_number         AS api_num,
           CONVERT(VARCHAR(10), w.spud_date,        120) AS spud_date,
           CONVERT(VARCHAR(10), w.total_depth_date, 120) AS completion_date,
           w.bh_total_md_ft          AS final_td,
           w.rkb_ft                  AS depth_datum,
           CAST(NULL AS INT)         AS operator_ba_id,
           CAST(NULL AS INT)         AS field_id,
           ISNULL(w.company_name, 'Unknown')      AS operator_name,
           ISNULL(w.bottom_area_code, 'Unknown')  AS field_name
    FROM dataview_gom.well w
    WHERE w.surface_latitude  IS NOT NULL
      AND w.surface_longitude IS NOT NULL
"""

GOM_NARROW_SELECT = """
    SELECT CONVERT(VARCHAR(36), w.well_id) AS uwi,
           w.well_name,
           w.surface_latitude  AS lat,
           w.surface_longitude AS lon
    FROM dataview_gom.well w
    WHERE w.surface_latitude  IS NOT NULL
      AND w.surface_longitude IS NOT NULL
"""

MAIN_WIDE_SELECT = """
    SELECT w.uwi, w.well_name, w.well_type, w.well_status,
           w.surface_latitude  AS lat,
           w.surface_longitude AS lon,
           w.county, w.province_state, w.country, w.api_num,
           w.source,
           CONVERT(VARCHAR(10), w.spud_date,       120) AS spud_date,
           CONVERT(VARCHAR(10), w.completion_date, 120) AS completion_date,
           w.final_td, w.depth_datum,
           w.operator_ba_id, w.field_id,
           ISNULL(ba.ba_name,   'Unknown') AS operator_name,
           ISNULL(f.field_name, 'Unknown') AS field_name,
           ISNULL(f.basin_name, 'Unknown') AS basin_name,
           w.area,
           w.protraction_area
    FROM dataview.dv_well w
    LEFT JOIN dataview.dv_business_associate ba ON ba.ba_id = w.operator_ba_id
    LEFT JOIN dataview.dv_field f ON f.field_id = w.field_id
    WHERE w.surface_latitude  IS NOT NULL
      AND w.surface_longitude IS NOT NULL
"""

MAIN_NARROW_SELECT = """
    SELECT w.uwi, w.well_name,
           w.surface_latitude  AS lat,
           w.surface_longitude AS lon
    FROM dataview.dv_well w
    WHERE w.surface_latitude  IS NOT NULL
      AND w.surface_longitude IS NOT NULL
"""


@dataclass
class TableShape:
    label: str          # human label, used in output
    table: str          # full schema.table
    wide: str           # SELECT SQL, no ORDER BY no FOR JSON
    narrow: str         # SELECT SQL, no ORDER BY no FOR JSON
    expected_rows: int  # approximate


TABLES = {
    "gom": TableShape(
        label="dataview_gom.well",
        table="dataview_gom.well",
        wide=GOM_WIDE_SELECT,
        narrow=GOM_NARROW_SELECT,
        expected_rows=55_000,
    ),
    "main": TableShape(
        label="dataview.dv_well",
        table="dataview.dv_well",
        wide=MAIN_WIDE_SELECT,
        narrow=MAIN_NARROW_SELECT,
        expected_rows=477_000,
    ),
}


# -----------------------------------------------------------------------------
# Result record
# -----------------------------------------------------------------------------
@dataclass
class TimingResult:
    table: str
    shape: str           # "wide" | "narrow"
    method: str
    rows: int
    server_ms: float     # SQL Server elapsed (from STATISTICS TIME if available)
    transport_ms: float  # wall clock for the transport step
    parse_ms: float      # wall clock for Python parsing into list[dict]
    total_ms: float
    notes: str = ""
    failed: bool = False
    error: str = ""

    def fmt_ms(self, ms: float) -> str:
        if self.failed:
            return "  FAIL"
        if ms < 1:
            return "   -"
        if ms < 1000:
            return f"{ms:5.0f}ms"
        return f"{ms/1000:5.1f}s "


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def get_server_elapsed(engine, sql: str) -> float:
    """
    Run the query in a side connection with STATISTICS TIME to capture
    server-side elapsed (CPU + wait, not transport). Returns ms.

    The query is run with TOP 0 (returns no rows but does the work to plan it),
    then with the full SELECT — we measure both and report the full one.

    Falls back to 0 if we can't extract the timing.
    """
    # SQL Server only emits STATISTICS TIME messages via raw cursor, not via
    # SQLAlchemy result rows. We use a raw pyodbc connection for this.
    try:
        with engine.raw_connection() as conn:
            cur = conn.cursor()
            cur.execute("SET STATISTICS TIME ON")
            # Drain the SET command's info-messages
            while cur.nextset():
                pass
            t0 = time.time()
            cur.execute(sql)
            while cur.fetchmany(10000):
                pass
            elapsed_ms = (time.time() - t0) * 1000.0

            # Try to extract the STATISTICS TIME messages from messages list.
            # pyodbc surfaces info messages via cur.messages on some drivers;
            # if not available, fall back to wall time we just measured.
            msgs = getattr(cur, "messages", None) or []
            for m in msgs:
                # Messages look like:
                # "[Microsoft][ODBC Driver 17][SQL Server] SQL Server Execution Times: CPU time = X ms, elapsed time = Y ms."
                if "elapsed time" in str(m):
                    try:
                        s = str(m)
                        elapsed_part = s.split("elapsed time = ")[1].split(" ms")[0]
                        return float(elapsed_part)
                    except (IndexError, ValueError):
                        pass
            # Fall back to wall time (overstates: includes pyodbc transport)
            return elapsed_ms
    except Exception:
        return 0.0


# -----------------------------------------------------------------------------
# Method 1 — pyodbc plain SELECT, fetchall
# -----------------------------------------------------------------------------
def method_pyodbc_select(engine, sql: str, expected_rows: int) -> TimingResult:
    notes = []
    server_ms = get_server_elapsed(engine, sql)

    try:
        t_trans = time.time()
        with engine.connect().execution_options(timeout=120) as con:
            rows = con.execute(text(sql)).fetchall()
        transport_ms = (time.time() - t_trans) * 1000.0

        t_parse = time.time()
        # Build list of dicts (matches what _qry_gom_wells returns)
        data = [dict(r._mapping) for r in rows]
        parse_ms = (time.time() - t_parse) * 1000.0

        return TimingResult(
            table="", shape="", method="pyodbc_select",
            rows=len(data),
            server_ms=server_ms,
            transport_ms=transport_ms,
            parse_ms=parse_ms,
            total_ms=transport_ms + parse_ms,
            notes="; ".join(notes),
        )
    except Exception as exc:
        return TimingResult(
            table="", shape="", method="pyodbc_select",
            rows=0, server_ms=server_ms,
            transport_ms=0, parse_ms=0, total_ms=0,
            failed=True, error=str(exc)[:120],
        )


# -----------------------------------------------------------------------------
# Method 2 — pyodbc + FOR JSON PATH (current pattern)
# -----------------------------------------------------------------------------
def method_pyodbc_for_json(engine, sql: str, expected_rows: int) -> TimingResult:
    json_sql = sql.rstrip().rstrip(";") + " FOR JSON PATH"
    server_ms = get_server_elapsed(engine, json_sql)

    try:
        t_trans = time.time()
        with engine.connect().execution_options(timeout=120) as con:
            rows = con.execute(text(json_sql)).fetchall()
        transport_ms = (time.time() - t_trans) * 1000.0

        t_parse = time.time()
        if rows:
            json_str = "".join(r[0] for r in rows if r[0])
            data = json.loads(json_str) if json_str else []
        else:
            data = []
        parse_ms = (time.time() - t_parse) * 1000.0

        return TimingResult(
            table="", shape="", method="pyodbc_for_json",
            rows=len(data),
            server_ms=server_ms,
            transport_ms=transport_ms,
            parse_ms=parse_ms,
            total_ms=transport_ms + parse_ms,
        )
    except Exception as exc:
        return TimingResult(
            table="", shape="", method="pyodbc_for_json",
            rows=0, server_ms=server_ms,
            transport_ms=0, parse_ms=0, total_ms=0,
            failed=True, error=str(exc)[:120],
        )


# -----------------------------------------------------------------------------
# Method 3 — pyodbc + pd.read_sql chunked
# -----------------------------------------------------------------------------
def method_pyodbc_chunked(engine, sql: str, expected_rows: int) -> TimingResult:
    server_ms = get_server_elapsed(engine, sql)

    try:
        t_trans = time.time()
        chunks = []
        for chunk in pd.read_sql(text(sql), engine, chunksize=5000):
            chunks.append(chunk)
        df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
        transport_ms = (time.time() - t_trans) * 1000.0

        t_parse = time.time()
        data = df.to_dict("records") if not df.empty else []
        parse_ms = (time.time() - t_parse) * 1000.0

        return TimingResult(
            table="", shape="", method="pyodbc_chunked",
            rows=len(data),
            server_ms=server_ms,
            transport_ms=transport_ms,
            parse_ms=parse_ms,
            total_ms=transport_ms + parse_ms,
            notes=f"{len(chunks)} chunks",
        )
    except Exception as exc:
        return TimingResult(
            table="", shape="", method="pyodbc_chunked",
            rows=0, server_ms=server_ms,
            transport_ms=0, parse_ms=0, total_ms=0,
            failed=True, error=str(exc)[:120],
        )


# -----------------------------------------------------------------------------
# Method 4 — BCP OUT to CSV, Python parses to list[dict]
# -----------------------------------------------------------------------------
def method_bcp_csv(engine, sql: str, expected_rows: int) -> TimingResult:
    server_ms = get_server_elapsed(engine, sql)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_path = WORK_DIR / "method4.csv"

    # BCP queryout doesn't accept newlines in query; collapse to one line
    one_line_sql = " ".join(sql.split())

    try:
        t_trans = time.time()
        cmd = [
            "bcp", one_line_sql, "queryout", str(out_path),
            "-c", "-t|", "-C", "65001",
            "-T", f"-S{BCP_SERVER}", f"-d{BCP_DATABASE}", "-q",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        transport_ms = (time.time() - t_trans) * 1000.0

        if result.returncode != 0:
            return TimingResult(
                table="", shape="", method="bcp_csv",
                rows=0, server_ms=server_ms,
                transport_ms=transport_ms, parse_ms=0, total_ms=transport_ms,
                failed=True,
                error=(result.stderr or result.stdout)[:120],
            )

        t_parse = time.time()
        data = []
        with out_path.open("r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="|")
            for row in reader:
                # We don't preserve column names with BCP -c; for timing
                # accuracy we just count and stuff into a positional dict
                data.append({"_cols": row})
        parse_ms = (time.time() - t_parse) * 1000.0

        return TimingResult(
            table="", shape="", method="bcp_csv",
            rows=len(data),
            server_ms=server_ms,
            transport_ms=transport_ms,
            parse_ms=parse_ms,
            total_ms=transport_ms + parse_ms,
            notes=f"file={out_path.stat().st_size//1024} KB",
        )
    except Exception as exc:
        return TimingResult(
            table="", shape="", method="bcp_csv",
            rows=0, server_ms=server_ms,
            transport_ms=0, parse_ms=0, total_ms=0,
            failed=True, error=str(exc)[:120],
        )
    finally:
        try: out_path.unlink()
        except FileNotFoundError: pass


# -----------------------------------------------------------------------------
# Method 5 — BCP queryout with FOR JSON, Python parses the JSON file
# -----------------------------------------------------------------------------
def method_bcp_json(engine, sql: str, expected_rows: int) -> TimingResult:
    server_ms = get_server_elapsed(engine, sql)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_path = WORK_DIR / "method5.json"

    json_sql = sql.rstrip().rstrip(";") + " FOR JSON PATH"
    one_line_sql = " ".join(json_sql.split())

    try:
        t_trans = time.time()
        cmd = [
            "bcp", one_line_sql, "queryout", str(out_path),
            "-c", "-t|", "-C", "65001",
            "-T", f"-S{BCP_SERVER}", f"-d{BCP_DATABASE}", "-q",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        transport_ms = (time.time() - t_trans) * 1000.0

        if result.returncode != 0:
            return TimingResult(
                table="", shape="", method="bcp_json",
                rows=0, server_ms=server_ms,
                transport_ms=transport_ms, parse_ms=0, total_ms=transport_ms,
                failed=True,
                error=(result.stderr or result.stdout)[:120],
            )

        t_parse = time.time()
        # BCP -c writes one row per line; FOR JSON returns a single JSON
        # array split into many varchar rows. Concat (joining without
        # separator) reconstructs the JSON.
        with out_path.open("r", encoding="utf-8") as f:
            json_str = f.read().replace("\n", "")
        # The pipe delimiter we passed via -t is field-only; FOR JSON has
        # one field so it doesn't appear in the output. Strip if it does.
        json_str = json_str.replace("|", "")
        data = json.loads(json_str) if json_str.strip() else []
        parse_ms = (time.time() - t_parse) * 1000.0

        return TimingResult(
            table="", shape="", method="bcp_json",
            rows=len(data),
            server_ms=server_ms,
            transport_ms=transport_ms,
            parse_ms=parse_ms,
            total_ms=transport_ms + parse_ms,
            notes=f"file={out_path.stat().st_size//1024} KB",
        )
    except Exception as exc:
        return TimingResult(
            table="", shape="", method="bcp_json",
            rows=0, server_ms=server_ms,
            transport_ms=0, parse_ms=0, total_ms=0,
            failed=True, error=str(exc)[:120],
        )
    finally:
        try: out_path.unlink()
        except FileNotFoundError: pass


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------
METHODS: dict[str, Callable] = {
    "pyodbc_select":   method_pyodbc_select,
    "pyodbc_for_json": method_pyodbc_for_json,
    "pyodbc_chunked":  method_pyodbc_chunked,
    "bcp_csv":         method_bcp_csv,
    "bcp_json":        method_bcp_json,
}


def run_all(args) -> list[TimingResult]:
    print(f"Connecting to {BCP_SERVER}/{BCP_DATABASE} ...")
    engine = create_engine(CONN_STR)

    # Pre-flight: confirm BCP is available since methods 4 & 5 need it
    if not shutil.which("bcp"):
        print("WARNING: bcp.exe not found on PATH. Methods 4/5 will fail.")

    target_tables = (list(TABLES.values())
                     if args.table == "all"
                     else [TABLES[args.table]])

    target_shapes = ["wide", "narrow"] if args.shape == "both" else [args.shape]
    target_methods = list(METHODS.keys()) if not args.methods else args.methods

    results: list[TimingResult] = []

    for spec in target_tables:
        for shape in target_shapes:
            sql = spec.wide if shape == "wide" else spec.narrow
            cols = "wide" if shape == "wide" else "narrow"

            for method_name in target_methods:
                # Skip-slow guard: avoid hanging on large tables with known-slow methods
                if (args.skip_slow
                        and spec.expected_rows >= SCALE_THRESHOLD
                        and method_name in METHODS_SKIP_AT_SCALE):
                    print(f"  SKIPPING {method_name} on {spec.label} {cols} "
                          f"(--skip-slow, table > {SCALE_THRESHOLD:,} rows)")
                    continue

                fn = METHODS[method_name]
                print(f"  RUNNING  {method_name:18s} on {spec.label:25s} {cols} ... ",
                      end="", flush=True)
                t0 = time.time()
                result = fn(engine, sql, spec.expected_rows)
                result.table = spec.label
                result.shape = cols
                # If method didn't fill total_ms, use wall time
                if not result.total_ms and not result.failed:
                    result.total_ms = (time.time() - t0) * 1000.0
                results.append(result)

                if result.failed:
                    print(f"FAIL ({result.error})")
                else:
                    print(f"{result.rows:>7,} rows in {result.total_ms/1000:6.2f}s")

    return results


def format_table(results: list[TimingResult]) -> str:
    """Markdown table of timings."""
    lines = []
    lines.append("| Table | Shape | Method | Rows | Server | Transport | Parse | Total | Notes |")
    lines.append("|-------|-------|--------|------|--------|-----------|-------|-------|-------|")
    for r in results:
        if r.failed:
            lines.append(
                f"| {r.table} | {r.shape} | {r.method} | - | - | - | - | "
                f"**FAIL** | {r.error} |"
            )
            continue
        lines.append(
            f"| {r.table} | {r.shape} | {r.method} | {r.rows:,} | "
            f"{r.server_ms/1000:.2f}s | {r.transport_ms/1000:.2f}s | "
            f"{r.parse_ms/1000:.2f}s | {r.total_ms/1000:.2f}s | {r.notes} |"
        )
    return "\n".join(lines)


def write_report(results: list[TimingResult], out_md: Path) -> None:
    """Write a Markdown report with the table + analysis."""
    table = format_table(results)
    by_method: dict[str, list[TimingResult]] = {}
    for r in results:
        by_method.setdefault(r.method, []).append(r)

    # Pick the winner for each (table, shape) combo
    winners: dict[tuple[str, str], TimingResult] = {}
    for r in results:
        if r.failed:
            continue
        key = (r.table, r.shape)
        cur = winners.get(key)
        if cur is None or r.total_ms < cur.total_ms:
            winners[key] = r

    lines = [
        "# Transport Diagnostic Report",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Timing Table",
        "",
        table,
        "",
        "## Winners",
        "",
        "Fastest method per (table, shape):",
        "",
    ]
    for (tbl, sh), r in sorted(winners.items()):
        lines.append(f"- **{tbl} ({sh})**: `{r.method}` — {r.total_ms/1000:.2f}s for {r.rows:,} rows")

    lines += [
        "",
        "## Bottleneck analysis",
        "",
        "For each method, the ratio `transport_ms / server_ms` reveals where time goes:",
        "",
        "- ratio ≈ 1: server-bound (query is doing the work, transport is free)",
        "- ratio > 10: transport-bound (pyodbc/BCP is the bottleneck)",
        "- ratio > 100: severe pyodbc choke (the yesterday-H3 backfill pattern)",
        "",
    ]
    for r in results:
        if r.failed or r.server_ms < 1:
            continue
        ratio = r.transport_ms / r.server_ms if r.server_ms else 0
        verdict = (
            "server-bound" if ratio < 3
            else "balanced" if ratio < 10
            else "transport-bound" if ratio < 100
            else "SEVERE transport choke"
        )
        lines.append(
            f"- {r.table} {r.shape} {r.method}: "
            f"ratio {ratio:.1f}x — {verdict}"
        )

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Transport diagnostic")
    parser.add_argument("--table", choices=list(TABLES.keys()) + ["all"],
                        default="all")
    parser.add_argument("--shape", choices=["wide", "narrow", "both"],
                        default="both")
    parser.add_argument("--methods", nargs="*",
                        choices=list(METHODS.keys()),
                        help="Subset of methods (default: all 5)")
    parser.add_argument("--skip-slow", action="store_true",
                        help="Skip methods known to hang on large tables "
                             f"(>{SCALE_THRESHOLD:,} rows)")
    parser.add_argument("--csv-out", default=None,
                        help="Write per-row CSV of timings (for graphing)")
    parser.add_argument("--md-out", default="transport_diagnostic.md",
                        help="Markdown report path (default: transport_diagnostic.md)")
    args = parser.parse_args()

    print("=" * 72)
    print("TRANSPORT DIAGNOSTIC")
    print("=" * 72)

    t0 = time.time()
    results = run_all(args)
    total_min = (time.time() - t0) / 60.0

    print()
    print("=" * 72)
    print(f"COMPLETE — {total_min:.1f} min total")
    print("=" * 72)
    print()
    print(format_table(results))
    print()

    if args.csv_out:
        csv_path = Path(args.csv_out)
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys())
                               if results else [])
            w.writeheader()
            for r in results:
                w.writerow(asdict(r))
        print(f"CSV: {csv_path.resolve()}")

    md_path = Path(args.md_out)
    write_report(results, md_path)
    print(f"Report: {md_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
