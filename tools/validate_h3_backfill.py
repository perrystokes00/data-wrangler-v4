"""
validate_h3_backfill.py — confirm H3 backfill is complete and correct.

Run after backfill_h3.py. Returns exit code 0 if every well with valid
surface coordinates has all 4 H3 cells populated + a non-NULL coord
hash; non-zero if any gaps exist.

Checks per source table:
  1. Every (lat IS NOT NULL AND lon IS NOT NULL) row has all 4 H3 cells
  2. No H3 cells exist on rows with NULL coordinates
  3. Hash is non-NULL where H3 cells are populated
  4. Spot-check: random sample of 100 wells — recompute H3 in Python
     and confirm it matches the stored value (catches algorithm drift,
     wrong resolution, etc.)

Exit code 0 on all-pass; 1 on any failure.
"""

from __future__ import annotations

import hashlib
import sys
import random

import h3
import pandas as pd
from sqlalchemy import create_engine, text


CONN_STR = (
    "mssql+pyodbc://@localhost\\SQLEXPRESS/DataView_Demo"
    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
)


TABLES = [
    {
        "label":    "dataview.dv_well",
        "full":     "dataview.dv_well",
        "pk":       "uwi",
        "lat":      "surface_latitude",
        "lon":      "surface_longitude",
    },
    {
        "label":    "dataview_gom.well",
        "full":     "dataview_gom.well",
        "pk":       "well_id",
        "lat":      "surface_latitude",
        "lon":      "surface_longitude",
    },
]


class Results:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    def report(self) -> bool:
        print("\n" + "=" * 72)
        print("H3 BACKFILL VALIDATION REPORT")
        print("=" * 72)
        for name, passed, detail in self.checks:
            mark = "PASS" if passed else "FAIL"
            line = f"  [{mark}]  {name}"
            if detail:
                line += f"   ({detail})"
            print(line)
        n_pass = sum(1 for _, p, _ in self.checks if p)
        n_total = len(self.checks)
        print("-" * 72)
        print(f"  {n_pass}/{n_total} checks passing")
        print("=" * 72 + "\n")
        return n_pass == n_total


def coord_hash(lat: float, lon: float) -> bytes:
    """Must match backfill_h3.py exactly."""
    return hashlib.sha256(f"{lat!r}|{lon!r}".encode("ascii")).digest()


def check_table(con, spec: dict, results: Results) -> None:
    label = spec["label"]
    full  = spec["full"]
    lat   = spec["lat"]
    lon   = spec["lon"]
    pk    = spec["pk"]

    # 1. Coverage — every row with valid coords has all 4 H3 cells
    row = con.execute(text(f"""
        SELECT
            COUNT(*) AS total_with_coords,
            SUM(CASE WHEN h3_r4 IS NULL THEN 1 ELSE 0 END) AS missing_r4,
            SUM(CASE WHEN h3_r5 IS NULL THEN 1 ELSE 0 END) AS missing_r5,
            SUM(CASE WHEN h3_r6 IS NULL THEN 1 ELSE 0 END) AS missing_r6,
            SUM(CASE WHEN h3_r7 IS NULL THEN 1 ELSE 0 END) AS missing_r7,
            SUM(CASE WHEN h3_coord_hash IS NULL THEN 1 ELSE 0 END) AS missing_hash
        FROM {full}
        WHERE {lat} IS NOT NULL AND {lon} IS NOT NULL
    """)).mappings().one()

    total = int(row["total_with_coords"])
    results.add(
        f"{label}: has rows with coords",
        total > 0,
        f"{total:,} wells",
    )

    for res in (4, 5, 6, 7):
        missing = int(row[f"missing_r{res}"])
        results.add(
            f"{label}: h3_r{res} populated",
            missing == 0,
            f"{missing:,} missing",
        )

    missing_hash = int(row["missing_hash"])
    results.add(
        f"{label}: h3_coord_hash populated",
        missing_hash == 0,
        f"{missing_hash:,} missing",
    )

    # 2. No H3 cells where coords are NULL (would be data quality issue)
    null_coord_with_h3 = con.execute(text(f"""
        SELECT COUNT(*) FROM {full}
        WHERE ({lat} IS NULL OR {lon} IS NULL)
          AND (h3_r4 IS NOT NULL OR h3_r5 IS NOT NULL
            OR h3_r6 IS NOT NULL OR h3_r7 IS NOT NULL)
    """)).scalar() or 0
    results.add(
        f"{label}: no orphan H3 on null-coord rows",
        null_coord_with_h3 == 0,
        f"{null_coord_with_h3} orphan(s)",
    )

    # 3. Spot-check — sample 100 rows, recompute H3 in Python, compare
    sample = pd.read_sql(text(f"""
        SELECT TOP 100 {pk} AS pk, {lat} AS lat, {lon} AS lon,
                       h3_r4, h3_r5, h3_r6, h3_r7
        FROM {full}
        WHERE {lat} IS NOT NULL AND {lon} IS NOT NULL
        ORDER BY NEWID()
    """), con)

    mismatches = 0
    for _, r in sample.iterrows():
        for res in (4, 5, 6, 7):
            expected = h3.latlng_to_cell(r["lat"], r["lon"], res)
            actual = r[f"h3_r{res}"]
            if expected != actual:
                mismatches += 1
                if mismatches <= 3:
                    print(f"    DEBUG mismatch: pk={r['pk']}, r{res}, "
                          f"expected={expected}, actual={actual}")

    results.add(
        f"{label}: spot-check matches Python h3",
        mismatches == 0,
        f"{mismatches} of {len(sample) * 4} cell-checks mismatched",
    )


def main() -> int:
    print(f"Connecting to {CONN_STR.split('@')[-1].split('?')[0]} ...")
    engine = create_engine(CONN_STR)
    results = Results()

    try:
        with engine.connect() as con:
            for spec in TABLES:
                check_table(con, spec, results)
    except Exception as exc:
        results.add("Connection / query", False, repr(exc))

    ok = results.report()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
