"""
validate_h3_views.py — sanity-check v_well (with H3 cols) and density views.

Run after create_v_well_density_h3.sql.

Checks:
  1. v_well still has all original 20 canonical columns
  2. v_well has the 4 new H3 columns (h3_r4..h3_r7)
  3. v_well row count matches sum of source tables (no rows lost in rebuild)
  4. v_well returns non-NULL H3 cells for every row
  5. Each density view exists and has expected (h3, well_count, dv_schema) shape
  6. Density view well_count totals reconcile to source table counts
  7. Each density view has both dv_schemas represented
  8. Distinct cell counts are within sane bounds per resolution

Exit code 0 on full pass, 1 on any failure.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text


CONN_STR = (
    "mssql+pyodbc://@localhost\\SQLEXPRESS/DataView_Demo"
    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
)


# Canonical 20 columns from Session 1, in expected order:
EXPECTED_V_WELL_COLS = [
    "uwi", "well_name", "api_num",
    "well_type", "well_status",
    "lat", "lon",
    "country", "province_state", "county",
    "basin_name", "field_name", "area", "protraction_area",
    "spud_date", "completion_date", "final_td",
    "operator_name", "source", "dv_schema",
    # Session 3 additions:
    "h3_r4", "h3_r5", "h3_r6", "h3_r7",
]

DENSITY_VIEWS = ["v_well_density_r4", "v_well_density_r5",
                 "v_well_density_r6", "v_well_density_r7"]

EXPECTED_TOTAL_WELLS = 477_108 + 54_675   # 531,783

# Sanity bounds for distinct cell counts per resolution.
# Wide ranges because well distribution is non-uniform; these only
# catch wildly wrong numbers (off-by-resolution, GROUP BY broken).
CELL_COUNT_BOUNDS = {
    4: (10, 1_000),
    5: (100, 10_000),
    6: (1_000, 100_000),
    7: (5_000, 500_000),
}


class Results:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    def report(self) -> bool:
        print("\n" + "=" * 72)
        print("H3 VIEW VALIDATION REPORT")
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


def check_v_well_structure(con, results: Results) -> None:
    """Confirm v_well has all expected columns in the right shape."""
    cols = con.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dataview_federation'
          AND TABLE_NAME = 'v_well'
        ORDER BY ORDINAL_POSITION
    """)).scalars().all()

    if not cols:
        results.add("v_well exists", False, "view not found")
        return
    results.add("v_well exists", True, f"{len(cols)} columns")

    expected_set = set(EXPECTED_V_WELL_COLS)
    actual_set = set(cols)

    missing = expected_set - actual_set
    extra = actual_set - expected_set

    results.add(
        "v_well has all expected columns",
        not missing,
        f"missing: {sorted(missing)}" if missing else "all present",
    )
    results.add(
        "v_well has no unexpected columns",
        not extra,
        f"extras: {sorted(extra)}" if extra else "clean",
    )


def check_v_well_row_count(con, results: Results) -> None:
    """v_well total should equal sum of source tables (post-cleanup counts)."""
    total = con.execute(
        text("SELECT COUNT(*) FROM dataview_federation.v_well")
    ).scalar() or 0
    results.add(
        "v_well row count matches sources",
        total == EXPECTED_TOTAL_WELLS,
        f"got {total:,}, expected {EXPECTED_TOTAL_WELLS:,}",
    )


def check_v_well_h3_coverage(con, results: Results) -> None:
    """Every row in v_well should have non-NULL H3 cells."""
    row = con.execute(text("""
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN h3_r4 IS NULL THEN 1 ELSE 0 END) AS null_r4,
            SUM(CASE WHEN h3_r5 IS NULL THEN 1 ELSE 0 END) AS null_r5,
            SUM(CASE WHEN h3_r6 IS NULL THEN 1 ELSE 0 END) AS null_r6,
            SUM(CASE WHEN h3_r7 IS NULL THEN 1 ELSE 0 END) AS null_r7
        FROM dataview_federation.v_well
    """)).mappings().one()
    for res in (4, 5, 6, 7):
        n_null = int(row[f"null_r{res}"])
        results.add(
            f"v_well: h3_r{res} non-NULL throughout",
            n_null == 0,
            f"{n_null:,} NULLs",
        )


def check_density_view(con, results: Results, view_name: str) -> None:
    """Per-view structure + reconciliation + cell count bounds."""
    resolution = int(view_name[-1])
    full_name = f"dataview_federation.{view_name}"

    # Structure
    cols = con.execute(text(f"""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dataview_federation'
          AND TABLE_NAME = '{view_name}'
        ORDER BY ORDINAL_POSITION
    """)).scalars().all()

    expected_cols = ["h3", "well_count", "dv_schema"]
    results.add(
        f"{view_name}: shape (h3, well_count, dv_schema)",
        cols == expected_cols,
        f"got {cols}",
    )

    # Totals — well_count summed across all rows should equal source counts
    total_wells = con.execute(
        text(f"SELECT SUM(well_count) FROM {full_name}")
    ).scalar() or 0
    results.add(
        f"{view_name}: total wells reconcile",
        total_wells == EXPECTED_TOTAL_WELLS,
        f"got {total_wells:,}, expected {EXPECTED_TOTAL_WELLS:,}",
    )

    # Distinct cell count within sane bounds
    distinct_cells = con.execute(
        text(f"SELECT COUNT(*) FROM {full_name}")
    ).scalar() or 0
    lo, hi = CELL_COUNT_BOUNDS[resolution]
    results.add(
        f"{view_name}: distinct cells in [{lo:,}, {hi:,}]",
        lo <= distinct_cells <= hi,
        f"got {distinct_cells:,}",
    )

    # Both schemas represented
    schemas = sorted(
        con.execute(
            text(f"SELECT DISTINCT dv_schema FROM {full_name}")
        ).scalars().all()
    )
    expected_schemas = sorted(["dataview", "dataview_gom"])
    results.add(
        f"{view_name}: both dv_schemas present",
        schemas == expected_schemas,
        f"got {schemas}",
    )


def main() -> int:
    print(f"Connecting to {CONN_STR.split('@')[-1].split('?')[0]} ...")
    engine = create_engine(CONN_STR)
    results = Results()

    try:
        with engine.connect() as con:
            check_v_well_structure(con, results)
            check_v_well_row_count(con, results)
            check_v_well_h3_coverage(con, results)
            for view in DENSITY_VIEWS:
                check_density_view(con, results, view)
    except Exception as exc:
        results.add("Connection / query", False, repr(exc))

    ok = results.report()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
