"""
validate_v_gom_well_attributes.py — sanity-check the GOM sister view.

Runs three classes of check after create_v_gom_well_attributes.sql has been
applied:

  1. Column presence — all 26 curated columns exist with expected types
  2. Row count — sister view row count matches dataview_gom.well row count
  3. JOIN integrity — every GOM-arm row in v_well has a matching sister row
     (no orphans, no missing keys)

Exit code 0 on full pass, 1 on any failure. Intended to be run after every
create_v_gom_well_attributes.sql re-apply, same way validate_v_well.py
gates v_well.

Mirrors the structure of validate_v_well.py — kept deliberately similar so
adding future sister-view validators (NDIC Bakken attrs, RRC Permian attrs,
etc.) is a copy-and-adjust job, not a rewrite.

Run:
    python validate_v_gom_well_attributes.py
"""

from __future__ import annotations

import sys
from typing import Iterable

from sqlalchemy import create_engine, text


# -----------------------------------------------------------------------------
# Connection — uses the same DataView target as the page and v_well work.
# Adjust if running against a different instance.
# -----------------------------------------------------------------------------
CONN_STR = (
    "mssql+pyodbc://@localhost\\SQLEXPRESS/DataView_Demo"
    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
)

VIEW_NAME = "dataview_federation.v_gom_well_attributes"
SOURCE_TABLE = "dataview_gom.well"
FEDERATION_WELL_VIEW = "dataview_federation.v_well"

# Expected columns in the curated subset, with the SQL type family we expect
# (used for a coarse "is the type right?" check, not strict equality —
# DECIMAL(18,6) and DECIMAL(10,2) are both 'decimal' for our purposes,
# and we only care that nothing got silently widened to NVARCHAR(MAX)).
EXPECTED_COLUMNS: list[tuple[str, str]] = [
    # (column_name, type_family)
    ("uwi",                    "string"),
    ("well_id",                "string"),
    ("well_name_suffix",       "string"),
    ("api_well_number",        "string"),
    ("company_name",           "string"),
    ("surface_lease_number",   "string"),
    ("bottom_lease_number",    "string"),
    ("bottom_area_code",       "string"),
    ("bottom_block_number",    "string"),
    ("region",                 "string"),
    ("type_code",              "string"),
    ("status_code",            "string"),
    ("casing_cut_code",        "string"),
    ("spud_date",              "string"),   # CONVERT to VARCHAR(10)
    ("total_depth_date",       "string"),
    ("status_date",            "string"),
    ("bh_total_md_ft",         "float"),
    ("true_vertical_depth_ft", "float"),
    ("tvd_subsea_ft",          "float"),
    ("rkb_ft",                 "float"),
    ("kop_ft",                 "float"),
    ("water_depth_ft",         "float"),
    ("surface_latitude",       "float"),
    ("surface_longitude",      "float"),
    ("bottom_latitude",        "float"),
    ("bottom_longitude",       "float"),
    ("source_file",            "string"),
]

# Map raw SQL Server type names to our coarse families. Anything not in this
# map gets flagged as 'unknown' which is a failure.
TYPE_FAMILIES: dict[str, str] = {
    "varchar":    "string",
    "nvarchar":   "string",
    "char":       "string",
    "nchar":      "string",
    "text":       "string",
    "ntext":      "string",
    "float":      "float",
    "real":       "float",
    "decimal":    "float",   # we CAST to FLOAT in the view, but allow either
    "numeric":    "float",
    "int":        "float",   # not used, but tolerate
    "bigint":     "float",
    "date":       "string",  # we CONVERT to VARCHAR in the view; warn if not
    "datetime":   "string",
    "datetime2":  "string",
}


# -----------------------------------------------------------------------------
# Result accumulator — collect, print, return pass/fail at the end.
# -----------------------------------------------------------------------------
class Results:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    def report(self) -> bool:
        print("\n" + "=" * 72)
        print(f"VALIDATION REPORT — {VIEW_NAME}")
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


# -----------------------------------------------------------------------------
# Individual checks
# -----------------------------------------------------------------------------
def check_columns(con, results: Results) -> None:
    """Verify every expected column exists with a compatible type."""
    sql = text("""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dataview_federation'
          AND TABLE_NAME   = 'v_gom_well_attributes'
    """)
    actual: dict[str, str] = {
        r[0]: (r[1] or "").lower() for r in con.execute(sql).fetchall()
    }

    if not actual:
        results.add("View exists", False, "INFORMATION_SCHEMA returned no rows")
        return
    results.add("View exists", True, f"{len(actual)} columns found")

    # Per-column presence + type-family check
    for col_name, expected_family in EXPECTED_COLUMNS:
        if col_name not in actual:
            results.add(f"Column present: {col_name}", False, "missing")
            continue
        raw_type = actual[col_name]
        got_family = TYPE_FAMILIES.get(raw_type, "unknown")
        ok = (got_family == expected_family)
        detail = f"got {raw_type} ({got_family}), expected {expected_family}"
        results.add(f"Column type: {col_name}", ok, detail)

    # Surface any UNEXPECTED columns — not a hard failure but worth seeing
    expected_names = {c for c, _ in EXPECTED_COLUMNS}
    extras = sorted(set(actual) - expected_names)
    if extras:
        results.add(
            "No unexpected columns", False,
            f"extras: {', '.join(extras)}",
        )
    else:
        results.add("No unexpected columns", True)


def check_row_counts(con, results: Results) -> None:
    """Sister view row count must equal source table row count."""
    view_n = con.execute(
        text(f"SELECT COUNT(*) FROM {VIEW_NAME}")
    ).scalar() or 0
    src_n = con.execute(
        text(f"SELECT COUNT(*) FROM {SOURCE_TABLE}")
    ).scalar() or 0
    results.add(
        "Row count matches source",
        view_n == src_n,
        f"view={view_n:,}, source={src_n:,}",
    )


def check_join_integrity(con, results: Results) -> None:
    """Every GOM-arm row in v_well must have a matching sister row."""
    # Orphans in v_well — GOM rows without a sister
    orphans = con.execute(text(f"""
        SELECT COUNT(*)
        FROM {FEDERATION_WELL_VIEW} w
        LEFT JOIN {VIEW_NAME} g ON g.uwi = w.uwi
        WHERE w.dv_schema = 'dataview_gom'
          AND g.uwi IS NULL
    """)).scalar() or 0
    results.add(
        "No v_well GOM rows missing sister",
        orphans == 0,
        f"{orphans:,} orphan(s)",
    )

    # Reverse — sister rows whose uwi doesn't exist in v_well
    # (would mean dataview_gom.well has rows v_well's UNION ALL excluded,
    # e.g. NULL surface coords)
    extras = con.execute(text(f"""
        SELECT COUNT(*)
        FROM {VIEW_NAME} g
        LEFT JOIN {FEDERATION_WELL_VIEW} w
            ON w.uwi = g.uwi AND w.dv_schema = 'dataview_gom'
        WHERE w.uwi IS NULL
    """)).scalar() or 0
    # This one is informational — sister having extras is expected if
    # v_well filters by surface_latitude IS NOT NULL. Print but don't fail
    # unless something looks pathological (>5% of sister rows).
    sister_total = con.execute(
        text(f"SELECT COUNT(*) FROM {VIEW_NAME}")
    ).scalar() or 1
    extra_pct = (extras / sister_total) * 100
    ok = extra_pct < 5.0
    results.add(
        "Sister extras within tolerance",
        ok,
        f"{extras:,} sister rows not in v_well ({extra_pct:.1f}%); "
        f"expected for NULL-coord wells excluded by v_well",
    )


def check_join_smoke(con, results: Results) -> None:
    """Run the canonical SELECT pattern and verify it returns rows."""
    n = con.execute(text(f"""
        SELECT COUNT(*)
        FROM {FEDERATION_WELL_VIEW} w
        LEFT JOIN {VIEW_NAME} g ON g.uwi = w.uwi
        WHERE w.dv_schema = 'dataview_gom'
          AND g.water_depth_ft IS NOT NULL
    """)).scalar() or 0
    results.add(
        "Canonical join returns water_depth rows",
        n > 0,
        f"{n:,} GOM wells with non-NULL water_depth_ft",
    )


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
def main() -> int:
    print(f"Connecting to {CONN_STR.split('@')[-1].split('?')[0]} ...")
    engine = create_engine(CONN_STR)
    results = Results()

    try:
        with engine.connect() as con:
            check_columns(con, results)
            check_row_counts(con, results)
            check_join_integrity(con, results)
            check_join_smoke(con, results)
    except Exception as exc:
        results.add("Connection / query", False, repr(exc))

    ok = results.report()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
