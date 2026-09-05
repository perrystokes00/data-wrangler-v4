"""
load_kgs.py — KGS wells loader for the federated dataview schema.

Reads the KGS-published wells CSV (ks_wells.txt, 514,713 rows, 43 native
columns), cleans the values using a proper csv.reader (handles quoting and
embedded newlines), and loads three tables:

  1. dv_well_ext_kgs       — native 43 KGS columns, preserved as-is
  2. dv_well               — PPDM common shape, source='KGS'
  3. dv_well_identifier    — KID + API + UWI crosswalk per well

Transport: BCP-bypass (CSV-to-disk + bcp in). Measured in Session 4 to be
~50-100x faster than pyodbc on 477K rows.

Prerequisites:
  - cleanup_kgs_existing.sql has run (clears old dirty KGS rows)
  - dataview.dv_well_ext_kgs table exists (create_dv_well_ext_kgs.sql)

Usage:
  python load_kgs.py
  python load_kgs.py --file C:/path/to/ks_wells.txt
  python load_kgs.py --dry-run     # parse and report, do not write
  python load_kgs.py --skip-bcp    # write staging CSVs, skip BCP step
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

csv.field_size_limit(10_000_000)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
BCP_SERVER   = r"localhost\SQLEXPRESS"
# DataView_Demo, not DataView. An older DataView is still on this instance and
# the login cannot open it, so this loader could never have run as shipped --
# every bcp call would have died with "Cannot open database". Unlike the map's
# copy of this constant, nothing here falls back, so the failure is at least
# loud; it is fixed for the same reason, which is that the name was simply
# wrong. See CLAUDE.md > Environment: the demo database is the one with data.
BCP_DATABASE = "DataView_Demo"

# Where staging CSVs are written. Cleaned up at end unless --keep-staging.
STAGING_DIR = Path(os.environ.get("LOCALAPPDATA", "/tmp")) / "Temp" / "dw_kgs_load"

# Field/row terminators for BCP CSV files.
# Using | (pipe) for fields since well names sometimes contain commas.
# After cleaning, pipes shouldn't appear in any value (the loader strips them).
FIELD_SEP = "|"
ROW_SEP   = "\n"

# Source label written to dv_well.source. Was 'KGS_GEOJSON' in the broken
# load; new label is the cleaner 'KGS'.
SOURCE_LABEL = "KGS"

# Default file path. Override with --file.
DEFAULT_KGS_FILE = "ks_wells.txt"


# -----------------------------------------------------------------------------
# KGS source column order (matches the published CSV header exactly)
# -----------------------------------------------------------------------------
KGS_COLUMNS = [
    "KID", "API_NUMBER", "API_NUM_NODASH", "LEASE", "WELL", "FIELD",
    "LATITUDE", "LONGITUDE", "LONG_LAT_SOURCE",
    "TOWNSHIP", "TWN_DIR", "RANGE", "RANGE_DIR", "SECTION", "SPOT",
    "FEET_NORTH", "FEET_EAST", "FOOT_REF",
    "ORIG_OPERATOR", "CURR_OPERATOR",
    "ELEVATION", "ELEV_REF", "SURFACE_ELEVATION_LIDAR",
    "DEPTH", "FORMATION_AT_TOTAL_DEPTH", "PRODUCE_FORM",
    "IP_OIL", "IP_GAS", "IP_WATER",
    "PERMIT", "SPUD", "COMPLETION", "PLUGGING", "MODIFIED",
    "OIL_KID", "OIL_DOR_ID", "GAS_KID", "GAS_DOR_ID", "KCC_PERMIT",
    "STATUS", "STATUS2", "COMMENTS", "LEASE_WELL_NAME",
]

# Reserved word renames for dv_well_ext_kgs (RANGE → RANGE_, SECTION → SECTION_)
RESERVED_RENAMES = {"RANGE": "RANGE_", "SECTION": "SECTION_"}

# Columns of dv_well_ext_kgs, in order (43 source + 1 audit)
EXT_TABLE_COLUMNS = ["uwi"] + [
    RESERVED_RENAMES.get(c, c) for c in KGS_COLUMNS
] + ["loaded_date"]


# -----------------------------------------------------------------------------
# Schema of the dv_well rows we'll write — only the columns we set
# -----------------------------------------------------------------------------
DV_WELL_COLUMNS = [
    "uwi",
    "well_name",
    "well_num",
    "operator_ba_id",          # NULL — denormalized
    "field_id",                # NULL — denormalized
    "well_type",
    "well_status",
    "country",
    "province_state",
    "county",
    "legal_survey_type",
    "surface_latitude",
    "surface_longitude",
    "ground_elevation",
    "kb_elevation",
    "spud_date",
    "completion_date",
    "final_td",
    "depth_datum",
    "epsg_code",
    "api_num",
    "license_num",
    "lease_name",
    "onshore_offshore_ind",
    "active_ind",              # always 'Y'
    "remark",
    "row_created_by",          # 'KGS_LOADER'
    "row_created_date",
    "row_changed_by",
    "row_changed_date",
    "source",                  # 'KGS'
    "abandonment_date",
    "bottom_hole_latitude",    # NULL — KGS doesn't have
    "bottom_hole_longitude",   # NULL — KGS doesn't have
    "current_operator_ba_id",  # NULL
    "original_operator_ba_id", # NULL
    "elevation_ouom",
    "formation_at_td",
    "long_lat_source",
    "permit_number",
    "producing_formation",
    "area",                    # NULL — onshore concept doesn't apply
    "operator_name",           # denormalized: CURR_OPERATOR cleaned
    "field_name",              # denormalized: FIELD cleaned
    "protraction_area",        # NULL — offshore concept
    "h3_r4", "h3_r5", "h3_r6", "h3_r7", "h3_coord_hash",  # NULL — backfill later
]

# Schema of dv_well_identifier rows
DV_IDENTIFIER_COLUMNS = [
    "well_id",          # UNIQUEIDENTIFIER NEWID() per well
    "identifier_type",  # 'UWI' | 'KID' | 'API'
    "identifier_value",
    "source_system",    # 'KGS'
    "loaded_date",
    "is_primary",       # 1 for UWI, 0 for others
]


# -----------------------------------------------------------------------------
# Value cleaning utilities
# -----------------------------------------------------------------------------
def clean_text(s: str | None, maxlen: int | None = None) -> str | None:
    """
    Normalize a text value:
      - None or empty/'unavailable'/'unknown' → None
      - Strip leading/trailing whitespace
      - Replace embedded \\r\\n / \\n / \\r with space
      - Collapse runs of whitespace
      - Replace pipe with space (BCP field delimiter safety)
      - Truncate to maxlen if specified

    Note: pipes are extremely rare in well data but we strip defensively.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    lower = s.lower()
    if lower in ("unavailable", "unknown", "n/a", "na", "null", "none"):
        return None

    # Strip CR/LF and collapse whitespace
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s+", " ", s)

    # BCP delimiter safety
    s = s.replace("|", " ")

    # Truncate to column width
    if maxlen and len(s) > maxlen:
        s = s[:maxlen]

    return s.strip() or None


def parse_float(s: str | None) -> float | None:
    """Parse a numeric field, return None on missing or invalid."""
    if not s or not s.strip():
        return None
    try:
        return float(s.strip())
    except (TypeError, ValueError):
        return None


def parse_int(s: str | None) -> int | None:
    """Parse an integer field, return None on missing or invalid."""
    if not s or not s.strip():
        return None
    try:
        return int(float(s.strip()))
    except (TypeError, ValueError):
        return None


# Date parsing: KGS uses 'dd-Mon-yy' (e.g., '01-Apr-69').
# Two-digit year convention: YY <= 30 → 20YY, YY >= 31 → 19YY.
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_kgs_date(s: str | None) -> str | None:
    """
    Parse KGS date strings to ISO format 'YYYY-MM-DD' for BCP.
    Returns None on missing or unparseable input. BCP coerces the string
    to datetime2 on insert.

    KGS publishes two formats over the years:
      - 4-digit year: '01-MAY-1964'  (current convention, most rows)
      - 2-digit year: '01-MAY-64'    (legacy; YY <= 30 → 20YY else 19YY)

    Examples:
      '01-MAY-1964'  → '1964-05-01'
      '01-Apr-69'    → '1969-04-01'
      '15-Mar-25'    → '2025-03-15'
      ''             → None
    """
    if not s or not s.strip():
        return None
    parts = s.strip().split("-")
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        mon = _MONTH_MAP.get(parts[1].upper()[:3])
        if mon is None:
            return None
        y = int(parts[2])
        # 4-digit year passes through, 2-digit year uses the 1931-2030 window
        if y >= 100:
            year = y
        else:
            year = 2000 + y if y <= 30 else 1900 + y
        # Sanity check
        d = datetime(year, mon, day)
        return d.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return None


# -----------------------------------------------------------------------------
# CSV writer helper (BCP-friendly format)
# -----------------------------------------------------------------------------
class BcpCsvWriter:
    """
    Writes a BCP-compatible CSV using | as field separator and \\n as row
    terminator. Values that are None become empty string (BCP -k flag tells
    BCP to read empty as NULL).

    No quoting — we cleaned the values so they don't contain pipes or
    newlines.
    """
    def __init__(self, path: Path):
        self.path = path
        self.f = path.open("w", encoding="utf-8", newline="")
        self.n = 0

    def write_row(self, values: list) -> None:
        out_fields = []
        for v in values:
            if v is None:
                out_fields.append("")
            else:
                out_fields.append(str(v))
        self.f.write(FIELD_SEP.join(out_fields))
        self.f.write(ROW_SEP)
        self.n += 1

    def close(self) -> int:
        self.f.close()
        return self.n


# -----------------------------------------------------------------------------
# Stats accumulator
# -----------------------------------------------------------------------------
@dataclass
class LoadStats:
    rows_read:           int = 0
    rows_missing_kid:    int = 0
    rows_accepted:       int = 0
    rows_with_coords:    int = 0
    rows_no_coords:      int = 0
    rows_with_api:       int = 0
    rows_no_api:         int = 0
    rows_with_operator:  int = 0
    rows_no_operator:    int = 0
    rows_with_field:     int = 0
    rows_no_field:       int = 0
    duplicate_uwis:      int = 0


# -----------------------------------------------------------------------------
# Phase 1: read raw KGS CSV, build three staging files
# -----------------------------------------------------------------------------
def build_staging_csvs(
    src: Path,
    staging_dir: Path,
    stats: LoadStats,
) -> tuple[Path, Path, Path]:
    """
    Read the KGS source CSV with csv.reader (handles quoting + embedded
    newlines natively) and write three staging CSVs ready for BCP.

    Returns (ext_csv_path, well_csv_path, identifier_csv_path).
    """
    ext_csv  = staging_dir / "kgs_ext.csv"
    well_csv = staging_dir / "kgs_well.csv"
    id_csv   = staging_dir / "kgs_identifier.csv"

    ext_w  = BcpCsvWriter(ext_csv)
    well_w = BcpCsvWriter(well_csv)
    id_w   = BcpCsvWriter(id_csv)

    # Track UWIs we've seen so duplicates are detected/skipped
    seen_uwis: set[str] = set()

    # Audit columns share one timestamp for the whole batch
    load_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with src.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)

        # Sanity check the header matches what we expect
        if header != KGS_COLUMNS:
            print(f"WARNING: header doesn't exactly match expected KGS columns.")
            print(f"  Expected: {KGS_COLUMNS[:5]} ... ({len(KGS_COLUMNS)} total)")
            print(f"  Got:      {header[:5]} ... ({len(header)} total)")
            # Continue anyway — we use positional indexing.

        # Map column name → index for safe access
        col_idx = {name: i for i, name in enumerate(KGS_COLUMNS)}

        def C(row, name):
            i = col_idx.get(name)
            if i is None or i >= len(row):
                return ""
            return row[i]

        for row in reader:
            stats.rows_read += 1
            if stats.rows_read % 50_000 == 0:
                print(f"   {stats.rows_read:,} rows read…")

            # KID is the required PK — reject row if missing
            kid = (C(row, "KID") or "").strip()
            if not kid:
                stats.rows_missing_kid += 1
                continue

            uwi = f"KGS_{kid}"
            if uwi in seen_uwis:
                stats.duplicate_uwis += 1
                continue
            seen_uwis.add(uwi)

            stats.rows_accepted += 1

            # ───── Extract cleaned values ─────
            api_number   = clean_text(C(row, "API_NUMBER"), 40)
            api_nodash   = clean_text(C(row, "API_NUM_NODASH"), 20)
            lease        = clean_text(C(row, "LEASE"))
            well         = clean_text(C(row, "WELL"))
            field_name   = clean_text(C(row, "FIELD"), 255)
            lease_well   = clean_text(C(row, "LEASE_WELL_NAME"), 255)
            curr_op      = clean_text(C(row, "CURR_OPERATOR"), 255)
            orig_op      = clean_text(C(row, "ORIG_OPERATOR"), 255)
            status       = clean_text(C(row, "STATUS"), 40)
            status2      = clean_text(C(row, "STATUS2"), 40)
            formation_td = clean_text(C(row, "FORMATION_AT_TOTAL_DEPTH"), 255)
            produce_form = clean_text(C(row, "PRODUCE_FORM"), 255)
            ll_source    = clean_text(C(row, "LONG_LAT_SOURCE"), 40)
            permit       = clean_text(C(row, "PERMIT"), 40)

            lat   = parse_float(C(row, "LATITUDE"))
            lon   = parse_float(C(row, "LONGITUDE"))
            depth = parse_float(C(row, "DEPTH"))
            elev  = parse_float(C(row, "ELEVATION"))
            elev_lidar = parse_float(C(row, "SURFACE_ELEVATION_LIDAR"))

            spud_date  = parse_kgs_date(C(row, "SPUD"))
            comp_date  = parse_kgs_date(C(row, "COMPLETION"))
            plug_date  = parse_kgs_date(C(row, "PLUGGING"))

            # Stats tracking
            if lat is not None and lon is not None:
                stats.rows_with_coords += 1
            else:
                stats.rows_no_coords += 1
            if api_number:
                stats.rows_with_api += 1
            else:
                stats.rows_no_api += 1
            if curr_op:
                stats.rows_with_operator += 1
            else:
                stats.rows_no_operator += 1
            if field_name:
                stats.rows_with_field += 1
            else:
                stats.rows_no_field += 1

            # ───── Row 1: dv_well_ext_kgs (native columns preserved) ─────
            # Order MUST match EXT_TABLE_COLUMNS exactly
            ext_row = [uwi]
            for kgs_col in KGS_COLUMNS:
                raw_val = C(row, kgs_col)
                # Minimal cleaning for the native preservation table —
                # strip newlines/pipes (BCP transport safety) but leave
                # the rest as-is.
                if raw_val is None:
                    ext_row.append(None)
                else:
                    cleaned = re.sub(r"[\r\n\t]+", " ", raw_val)
                    cleaned = cleaned.replace("|", " ")
                    cleaned = cleaned.strip()
                    ext_row.append(cleaned if cleaned else None)
            ext_row.append(load_ts)
            ext_w.write_row(ext_row)

            # ───── Row 2: dv_well (PPDM common shape) ─────
            # Order MUST match DV_WELL_COLUMNS exactly
            well_name_value = lease_well or (
                f"{lease} {well}".strip() if (lease or well) else None
            )
            if well_name_value:
                well_name_value = clean_text(well_name_value, 255)

            well_row = [
                uwi,                          # uwi
                well_name_value,              # well_name
                clean_text(well, 40),         # well_num — using KGS WELL
                None,                         # operator_ba_id
                None,                         # field_id
                None,                         # well_type — KGS has no direct mapping
                status,                       # well_status
                "USA",                        # country
                "KS",                         # province_state
                None,                         # county — KGS uses API county code, not name
                "PLSS",                       # legal_survey_type
                lat,                          # surface_latitude
                lon,                          # surface_longitude
                elev_lidar or elev,           # ground_elevation (prefer LIDAR)
                None,                         # kb_elevation — KGS doesn't have
                spud_date,                    # spud_date
                comp_date,                    # completion_date
                depth,                        # final_td
                None,                         # depth_datum
                4326,                         # epsg_code (WGS84)
                api_number,                   # api_num
                None,                         # license_num
                clean_text(lease, 255),       # lease_name
                "ONSHORE",                    # onshore_offshore_ind
                "Y",                          # active_ind
                None,                         # remark
                "KGS_LOADER",                 # row_created_by
                load_ts,                      # row_created_date
                None,                         # row_changed_by
                None,                         # row_changed_date
                SOURCE_LABEL,                 # source = 'KGS'
                plug_date,                    # abandonment_date
                None,                         # bottom_hole_latitude
                None,                         # bottom_hole_longitude
                None,                         # current_operator_ba_id
                None,                         # original_operator_ba_id
                "FT",                         # elevation_ouom
                formation_td,                 # formation_at_td
                ll_source,                    # long_lat_source
                permit,                       # permit_number
                produce_form,                 # producing_formation
                None,                         # area (onshore-doesn't-apply)
                curr_op,                      # operator_name (denormalized)
                field_name,                   # field_name (denormalized)
                None,                         # protraction_area
                # H3 columns NULL — populated by backfill_h3_bcp.py after load
                None, None, None, None, None,
            ]
            well_w.write_row(well_row)

            # ───── Row 3+: dv_well_identifier (1-3 identifier rows per well) ─────
            well_id = str(uuid.uuid4())

            # UWI (always present, marked primary)
            id_w.write_row([
                well_id, "UWI", uwi, SOURCE_LABEL, load_ts, 1
            ])

            # KID (always present, not primary)
            id_w.write_row([
                well_id, "KID", kid, SOURCE_LABEL, load_ts, 0
            ])

            # API_NUMBER (when present)
            if api_number:
                id_w.write_row([
                    well_id, "API", api_number, SOURCE_LABEL, load_ts, 0
                ])

    n_ext = ext_w.close()
    n_well = well_w.close()
    n_id = id_w.close()

    print(f"   staging files written:")
    print(f"     dv_well_ext_kgs    : {n_ext:,} rows  ({ext_csv.stat().st_size // 1024} KB)")
    print(f"     dv_well            : {n_well:,} rows ({well_csv.stat().st_size // 1024} KB)")
    print(f"     dv_well_identifier : {n_id:,} rows  ({id_csv.stat().st_size // 1024} KB)")

    return ext_csv, well_csv, id_csv


# -----------------------------------------------------------------------------
# BCP loader
# -----------------------------------------------------------------------------
def bcp_load(
    csv_path: Path,
    table: str,
    columns: list[str],
) -> int:
    """
    BCP IN a staging CSV into a destination table.

    Uses:
      -c       character mode
      -t |     field separator pipe
      -r \\n   row terminator newline
      -C 65001 UTF-8 codepage
      -T       trusted connection
      -k       keep nulls (treat empty fields as NULL, not '')
      -E       keep identity values (no IDENTITY in our targets, but defensive)
      -m 10    bail after 10 errors (default is ~10, explicit for clarity)

    Note: BCP IN with no format file requires the staging CSV to match
    the table column count and order EXACTLY. The order of our staging
    CSVs matches the column lists at the top of this file.
    """
    cmd = [
        "bcp", f"dataview.{table}", "in", str(csv_path),
        "-c",
        "-t", FIELD_SEP,
        "-r", "0x0a",  # hex-encoded LF — '\\n' literal is not reliably interpreted on Windows BCP
        "-C", "65001",
        "-T",
        f"-S{BCP_SERVER}",
        f"-d{BCP_DATABASE}",
        "-k",
        "-q",          # QUOTED_IDENTIFIER ON — required for filtered/computed indexes
        "-m", "10",
    ]
    print(f"   bcp in {table}: {csv_path.name}")
    t0 = time.time()
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    elapsed = time.time() - t0

    # Parse "N rows copied" from BCP stdout
    rows_copied = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if "rows copied" in line.lower():
            try:
                rows_copied = int(line.split()[0].replace(",", ""))
            except (ValueError, IndexError):
                pass
            break

    if result.returncode != 0:
        # Print full BCP output for diagnostics
        print(f"   BCP failed (exit={result.returncode}):")
        print("   STDOUT:")
        for line in (result.stdout or "").splitlines()[:30]:
            print(f"     {line}")
        print("   STDERR:")
        for line in (result.stderr or "").splitlines()[:30]:
            print(f"     {line}")
        raise RuntimeError(f"BCP into {table} failed")

    # Defensive: BCP can return exit code 0 while loading 0 rows
    # (e.g. row-terminator mismatch reads the whole file as one bad row,
    # then errors at -m threshold). Treat 0 rows as a failure.
    if rows_copied == 0:
        print(f"   BCP returned 0 rows copied (exit code was 0 but no data loaded):")
        print("   STDOUT:")
        for line in (result.stdout or "").splitlines()[:30]:
            print(f"     {line}")
        print("   STDERR:")
        for line in (result.stderr or "").splitlines()[:30]:
            print(f"     {line}")
        raise RuntimeError(
            f"BCP into {table} reported 0 rows copied. "
            "Common causes: row terminator mismatch, column count mismatch, "
            "or all rows hit the -m error threshold."
        )

    print(f"     {rows_copied:,} rows copied in {elapsed:.1f}s")
    return rows_copied


# -----------------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------------
def print_stats(stats: LoadStats, elapsed_total: float) -> None:
    print()
    print("=" * 70)
    print("KGS Load Summary")
    print("=" * 70)
    print(f"  Rows read from source        : {stats.rows_read:,}")
    print(f"  Rejected (missing KID)       : {stats.rows_missing_kid:,}")
    print(f"  Rejected (duplicate UWI)     : {stats.duplicate_uwis:,}")
    print(f"  Accepted                     : {stats.rows_accepted:,}")
    print(f"")
    print(f"  With coords                  : {stats.rows_with_coords:,} "
          f"({100*stats.rows_with_coords/max(stats.rows_accepted,1):.1f}%)")
    print(f"  Without coords               : {stats.rows_no_coords:,}")
    print(f"  With API                     : {stats.rows_with_api:,}")
    print(f"  Without API                  : {stats.rows_no_api:,}")
    print(f"  With operator                : {stats.rows_with_operator:,}")
    print(f"  Without operator             : {stats.rows_no_operator:,}")
    print(f"  With field name              : {stats.rows_with_field:,}")
    print(f"  Without field name           : {stats.rows_no_field:,}")
    print(f"")
    print(f"  Total elapsed                : {elapsed_total:.1f}s")
    print()


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=DEFAULT_KGS_FILE, help="KGS CSV path")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report, do not write or BCP load")
    ap.add_argument("--skip-bcp", action="store_true",
                    help="Write staging CSVs but skip BCP load step")
    ap.add_argument("--keep-staging", action="store_true",
                    help="Don't delete staging CSVs after load")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"ERROR: source file not found: {src.resolve()}", file=sys.stderr)
        return 1

    print("=" * 70)
    print("KGS LOADER")
    print("=" * 70)
    print(f"  Source file      : {src.resolve()}")
    print(f"  File size        : {src.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  Source label     : '{SOURCE_LABEL}'")
    print(f"  Staging dir      : {STAGING_DIR}")
    print(f"  Dry-run          : {args.dry_run}")
    print()

    t0 = time.time()

    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    stats = LoadStats()
    print("── Phase 1: parse + build staging CSVs ──")
    ext_csv, well_csv, id_csv = build_staging_csvs(src, STAGING_DIR, stats)
    parse_elapsed = time.time() - t0
    print(f"   phase 1 complete: {parse_elapsed:.1f}s")

    if args.dry_run:
        print()
        print("Dry-run — skipping BCP load.")
        print_stats(stats, time.time() - t0)
        return 0

    if args.skip_bcp:
        print()
        print("--skip-bcp specified — staging CSVs kept for inspection:")
        print(f"   {ext_csv}")
        print(f"   {well_csv}")
        print(f"   {id_csv}")
        print_stats(stats, time.time() - t0)
        return 0

    print()
    print("── Phase 2: BCP load into dataview tables ──")

    try:
        bcp_load(ext_csv, "dv_well_ext_kgs", EXT_TABLE_COLUMNS)
        bcp_load(well_csv, "dv_well", DV_WELL_COLUMNS)
        bcp_load(id_csv, "dv_well_identifier", DV_IDENTIFIER_COLUMNS)
    finally:
        if not args.keep_staging:
            for p in (ext_csv, well_csv, id_csv):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    elapsed = time.time() - t0
    print_stats(stats, elapsed)

    print("=" * 70)
    print("LOAD COMPLETE")
    print("=" * 70)
    print()
    print("Next steps:")
    print("  1. Run backfill_h3_bcp.py to populate h3_r4..r7 columns")
    print("  2. Recreate dataview_federation.v_well + density views")
    print("  3. Verify Wells mode in page_well_map.py still works")

    return 0


if __name__ == "__main__":
    sys.exit(main())
