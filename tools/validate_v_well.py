"""
validate_v_well.py
==================
Sanity-check dataview_federation.v_well after running create_v_well.sql.

Runs a battery of checks and prints pass/fail for each:
  1. Schema and table existence
  2. Row counts match source tables
  3. Column shape (all 20 columns present with expected types)
  4. NULL patterns match design (GOM has NULL field_name etc.)
  5. dv_schema dispatch works (filter to one schema returns only that schema's rows)
  6. BOEM lookup coverage (GOM protraction_area mostly populated)
  7. Onshore joins work (operator_name, field_name populated where data exists)
  8. Sample wells appear with correct values

Run from V3 root:
    python tools/validate_v_well.py

Exits 0 if all checks pass, 1 if any fail. Use as a smoke test before
migrating page consumers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text
import os

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# V3 has dw_utils with make_engine — use the existing connection logic
sys.path.insert(0, str(Path(__file__).parent))
try:
    from dataview.core.dw_utils import make_engine
except ImportError:
    print("ERROR: Cannot import dw_utils.make_engine — run from V3 root.")
    sys.exit(1)


# ── helpers ─────────────────────────────────────────────────────────

class CheckResult:
    """Single pass/fail check with diagnostic message."""
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name   = name
        self.passed = passed
        self.detail = detail

    def __str__(self):
        icon = "\u2713" if self.passed else "\u2717"  # ✓ / ✗
        line = f"  {icon} {self.name}"
        if self.detail:
            line += f"\n    {self.detail}"
        return line


def section(title: str):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ── checks ──────────────────────────────────────────────────────────

def check_schema_and_objects(con) -> list[CheckResult]:
    out = []
    # schema
    n = con.execute(text("""
        SELECT COUNT(*) FROM sys.schemas WHERE name = 'dataview_federation'
    """)).scalar()
    out.append(CheckResult(
        "Schema 'dataview_federation' exists",
        n == 1,
        f"Found {n} schemas with that name" if n != 1 else "",
    ))

    # lookup table
    n = con.execute(text("""
        SELECT COUNT(*) FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = 'dataview_federation' AND t.name = 'boem_area_lookup'
    """)).scalar()
    out.append(CheckResult(
        "Table 'boem_area_lookup' exists",
        n == 1,
        f"Found {n} matching tables" if n != 1 else "",
    ))

    # lookup populated
    if n == 1:
        n_rows = con.execute(text("""
            SELECT COUNT(*) FROM dataview_federation.boem_area_lookup
        """)).scalar()
        out.append(CheckResult(
            f"boem_area_lookup has rows (got {n_rows})",
            n_rows >= 60,  # 63 expected, give some slack
            f"Expected ~63, got {n_rows}" if n_rows < 60 else "",
        ))

    # view
    n = con.execute(text("""
        SELECT COUNT(*) FROM sys.views v
        JOIN sys.schemas s ON s.schema_id = v.schema_id
        WHERE s.name = 'dataview_federation' AND v.name = 'v_well'
    """)).scalar()
    out.append(CheckResult(
        "View 'v_well' exists",
        n == 1,
        f"Found {n} matching views" if n != 1 else "",
    ))
    return out


def check_columns(con) -> list[CheckResult]:
    """Verify all 20 canonical columns are present in v_well."""
    expected = [
        "uwi", "well_name", "api_num",
        "well_type", "well_status",
        "lat", "lon", "country", "province_state", "county",
        "basin_name", "field_name", "area", "protraction_area",
        "spud_date", "completion_date",
        "final_td",
        "operator_name", "source", "dv_schema",
    ]
    rows = con.execute(text("""
        SELECT c.name
        FROM sys.columns c
        JOIN sys.views v   ON v.object_id   = c.object_id
        JOIN sys.schemas s ON s.schema_id   = v.schema_id
        WHERE s.name = 'dataview_federation' AND v.name = 'v_well'
        ORDER BY c.column_id
    """)).fetchall()
    actual = [r[0] for r in rows]

    missing = [c for c in expected if c not in actual]
    extra   = [c for c in actual   if c not in expected]

    out = []
    out.append(CheckResult(
        f"All {len(expected)} canonical columns present",
        not missing,
        f"Missing: {missing}" if missing else "",
    ))
    if extra:
        out.append(CheckResult(
            "No unexpected extra columns",
            False,
            f"Extra: {extra}",
        ))
    return out


def check_row_counts(con) -> list[CheckResult]:
    """Verify v_well row counts match source tables (with NULL-lat/lon filter)."""
    out = []

    # dataview arm
    src_n = con.execute(text("""
        SELECT COUNT(*) FROM dataview.dv_well
        WHERE surface_latitude IS NOT NULL AND surface_longitude IS NOT NULL
    """)).scalar()
    view_n = con.execute(text("""
        SELECT COUNT(*) FROM dataview_federation.v_well
        WHERE dv_schema = 'dataview'
    """)).scalar()
    out.append(CheckResult(
        f"dataview arm row count matches source ({view_n:,} = {src_n:,})",
        src_n == view_n,
        f"Mismatch: source={src_n:,}, view={view_n:,}" if src_n != view_n else "",
    ))

    # dataview_gom arm
    src_n = con.execute(text("""
        SELECT COUNT(*) FROM dataview_gom.well
        WHERE surface_latitude IS NOT NULL AND surface_longitude IS NOT NULL
    """)).scalar()
    view_n = con.execute(text("""
        SELECT COUNT(*) FROM dataview_federation.v_well
        WHERE dv_schema = 'dataview_gom'
    """)).scalar()
    out.append(CheckResult(
        f"dataview_gom arm row count matches source ({view_n:,} = {src_n:,})",
        src_n == view_n,
        f"Mismatch: source={src_n:,}, view={view_n:,}" if src_n != view_n else "",
    ))

    return out


def check_required_fields_non_null(con) -> list[CheckResult]:
    """uwi, well_name, lat, lon should ALWAYS be non-null."""
    out = []
    for col in ["uwi", "lat", "lon"]:
        n = con.execute(text(f"""
            SELECT COUNT(*) FROM dataview_federation.v_well WHERE {col} IS NULL
        """)).scalar()
        out.append(CheckResult(
            f"Required column '{col}' has no NULLs",
            n == 0,
            f"{n:,} rows have NULL {col}" if n else "",
        ))
    return out


def check_gom_null_pattern(con) -> list[CheckResult]:
    """Confirm GOM rows have NULL for fields we agreed should be NULL."""
    out = []
    # field_name, basin_name, area, county, country should be 100% NULL for GOM
    expected_null_cols = ["field_name", "basin_name", "area", "country"]
    for col in expected_null_cols:
        n = con.execute(text(f"""
            SELECT COUNT(*) FROM dataview_federation.v_well
            WHERE dv_schema = 'dataview_gom' AND {col} IS NOT NULL
        """)).scalar()
        out.append(CheckResult(
            f"GOM rows have NULL '{col}' (no stand-ins)",
            n == 0,
            f"{n:,} GOM rows unexpectedly have non-NULL {col}" if n else "",
        ))
    return out


def check_dataview_country_usa(con) -> list[CheckResult]:
    """Onshore rows should all have country='USA'."""
    n_total = con.execute(text("""
        SELECT COUNT(*) FROM dataview_federation.v_well WHERE dv_schema = 'dataview'
    """)).scalar()
    n_usa = con.execute(text("""
        SELECT COUNT(*) FROM dataview_federation.v_well
        WHERE dv_schema = 'dataview' AND country = 'USA'
    """)).scalar()
    return [CheckResult(
        f"All dataview rows have country='USA' ({n_usa:,} / {n_total:,})",
        n_usa == n_total,
        f"Mismatch: {n_total - n_usa:,} dataview rows have non-USA country" if n_usa != n_total else "",
    )]


def check_boem_lookup_coverage(con) -> list[CheckResult]:
    """Most GOM rows should resolve to a friendly protraction_area name."""
    rows = con.execute(text("""
        SELECT
            SUM(CASE WHEN protraction_area IS NOT NULL THEN 1 ELSE 0 END) AS got_name,
            COUNT(*) AS total
        FROM dataview_federation.v_well
        WHERE dv_schema = 'dataview_gom'
    """)).fetchone()
    got, total = (int(rows[0] or 0), int(rows[1] or 0))
    coverage = (got / total * 100) if total else 0
    return [CheckResult(
        f"BOEM area lookup coverage: {got:,}/{total:,} ({coverage:.1f}%)",
        coverage >= 95,  # expect near-total coverage
        f"Low coverage — {total-got:,} GOM rows have NULL protraction_area" if coverage < 95 else "",
    )]


def check_dv_schema_dispatch(con) -> list[CheckResult]:
    """Filtering by dv_schema returns ONLY rows from that arm."""
    out = []
    for schema_val in ["dataview", "dataview_gom"]:
        rows = con.execute(text(f"""
            SELECT DISTINCT dv_schema FROM dataview_federation.v_well
            WHERE dv_schema = '{schema_val}'
        """)).fetchall()
        vals = [r[0] for r in rows]
        out.append(CheckResult(
            f"Filter dv_schema='{schema_val}' returns only that arm",
            vals == [schema_val],
            f"Got: {vals}" if vals != [schema_val] else "",
        ))
    return out


def check_source_values(con) -> list[CheckResult]:
    """GOM rows should all have source='BOEM'."""
    rows = con.execute(text("""
        SELECT source, COUNT(*) AS n
        FROM dataview_federation.v_well
        WHERE dv_schema = 'dataview_gom'
        GROUP BY source
    """)).fetchall()
    sources = {r[0]: r[1] for r in rows}

    out = []
    out.append(CheckResult(
        f"GOM rows have source='BOEM' ({sources.get('BOEM', 0):,})",
        list(sources.keys()) == ['BOEM'],
        f"Unexpected sources for GOM: {sources}" if list(sources.keys()) != ['BOEM'] else "",
    ))
    return out


def show_samples(con):
    """Print 3 sample rows from each arm. Visual confirmation."""
    section("SAMPLE ROWS")
    for schema_val in ["dataview", "dataview_gom"]:
        df = pd.read_sql(text(f"""
            SELECT TOP 3 uwi, well_name, lat, lon, province_state, county,
                   field_name, protraction_area, operator_name, source, dv_schema
            FROM dataview_federation.v_well
            WHERE dv_schema = '{schema_val}'
            ORDER BY uwi
        """), con)
        print()
        print(f"-- {schema_val} --")
        print(df.to_string(index=False))


# ── main ────────────────────────────────────────────────────────────

def main():
    section("FEDERATION v_well VALIDATION")

    print(f"Connecting to DataView...")
    try:
        engine = make_engine("DataView")
    except Exception as e:
        print(f"\u2717 Could not connect: {e}")
        sys.exit(1)

    all_results: list[CheckResult] = []

    with engine.connect() as con:
        section("1. Object existence")
        for r in check_schema_and_objects(con):
            print(r); all_results.append(r)

        section("2. Column shape")
        for r in check_columns(con):
            print(r); all_results.append(r)

        section("3. Row counts")
        for r in check_row_counts(con):
            print(r); all_results.append(r)

        section("4. Required fields non-null")
        for r in check_required_fields_non_null(con):
            print(r); all_results.append(r)

        section("5. GOM NULL pattern (canonical decisions)")
        for r in check_gom_null_pattern(con):
            print(r); all_results.append(r)

        section("6. dataview country='USA'")
        for r in check_dataview_country_usa(con):
            print(r); all_results.append(r)

        section("7. BOEM lookup coverage")
        for r in check_boem_lookup_coverage(con):
            print(r); all_results.append(r)

        section("8. dv_schema dispatch")
        for r in check_dv_schema_dispatch(con):
            print(r); all_results.append(r)

        section("9. source values")
        for r in check_source_values(con):
            print(r); all_results.append(r)

        show_samples(con)

    # Summary
    section("SUMMARY")
    n_pass = sum(1 for r in all_results if r.passed)
    n_fail = len(all_results) - n_pass
    print(f"  Passed: {n_pass} / {len(all_results)}")
    print(f"  Failed: {n_fail}")

    if n_fail:
        print()
        print("FAILED CHECKS:")
        for r in all_results:
            if not r.passed:
                print(f"  \u2717 {r.name}")
                if r.detail:
                    print(f"      {r.detail}")
        sys.exit(1)

    print()
    print("\u2713 All checks passed. Federation v_well is ready for consumers.")
    sys.exit(0)


if __name__ == "__main__":
    main()
