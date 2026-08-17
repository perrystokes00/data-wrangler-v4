"""
check_mirror_registry.py — the four lists must agree.

Repo ROOT (so `from dataview.file_catalog import ...` resolves).

    python check_mirror_registry.py --server localhost\\SQLEXPRESS --database DataView_Demo
    python check_mirror_registry.py ... --json      # machine-readable

Exit 0 = all agree · 1 = disagreements found · 2 = could not run the check.

THE PROBLEM THIS EXISTS FOR
---------------------------
A row captured from a document reaches dv_* — and is REPORTABLE once there —
only if FOUR independent things line up:

  1. build_catalog_mirror.MIRROR_TABLES   — the allowlist (code)
  2. file_catalog.cat_*                   — the mirror tables (database)
  3. promote_catalog                      — the generic loop + the dedicated
                                            promoters (code)
  4. promotion_lineage.LINEAGE            — which pairs any report can SEE

(4) was unchecked until 16 Aug 2026 and is the quietest of the four: rows
capture, promote lifts them, and the report says "no detail rows" because it
cannot see the pair at all. Casing, stimulation, petro_zone and perforation
were missing together — 1,433 rows nothing could report.

Nothing checks that they do. When they drift, NOTHING FAILS: capture writes
rows into a mirror, promote walks a different set, and the rows are reported
as neither moved nor held. They just sit there. `cat_well_casing` sat in
exactly that state with 148 rows staged and 0 promoted — the module comment
in build_catalog_mirror.py records it — and cross-checking on 7 Aug 2026
found two more:

    cat_reservoir          exists, in no list, walked by nothing
    cat_well_perforation   in the allowlist, but the table does not exist

Both are the same fault, and both were invisible. This makes them loud.

WHAT IS CHECKED
---------------
  A  every allowlist entry names a real dv_* table
  B  every allowlist entry has a cat_* mirror table
  C  every cat_* mirror is reachable by SOMETHING — the allowlist (generic
     loop) or a dedicated promoter
  D  every cat_* mirror has a dv_* counterpart to promote INTO
  E  no mirror has drifted from its dv_* table's column set
  F  every pair appears in promotion_lineage.LINEAGE — THE FOURTH LIST

(E) matters more than it looks. Promote moves rows by COLUMN NAME
INTERSECTION, so a column added to dv_* after the mirror was built is not an
error — it is a column that silently never carries data.

ON DETECTING PROMOTERS
----------------------
Ideally promote_catalog declares what it handles:

    DEDICATED_PROMOTERS = {
        "cat_field":      "promote_field",
        "cat_land_tract": "promote_land_tract",
        "cat_boundary":   "promote_boundary",
        "cat_pipeline":   "promote_pipeline",
        "cat_log_curve":  "promote_las_catalog",
    }

Add that dict and this check reads a DECLARATION. Until then it falls back to
scanning the module's source for the mirror name, which is good enough to
catch a table nothing mentions but cannot tell a real handler from a mention
in a comment. The fallback says so in its output.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sys

# THIS FILE'S OWN DIRECTORY, FIRST. Without it the app's SHIPPED python
# (an embedded build: sys.path is python312.zip, the runtime, site-packages,
# and `C:\Program Files\Data Wrangler v4\app` — no '' and no script dir) imports
# `dataview` from the DEPLOYED copy, not from the repo this file lives in. The
# check then reads a stale MIRROR_TABLES/LINEAGE and reports drift that does not
# exist in the source — which is exactly what happened on 16 Aug 2026: five
# dv_ tables reported missing from LINEAGE while the repo's LINEAGE named all
# of them. A checker that silently checks a different copy of the code is worse
# than no checker. selftest.py already does this; this file did not.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DV_SCHEMA = "dataview"
CAT_SCHEMA = "file_catalog"


# --------------------------------------------------------------------------
# reading the four lists
# --------------------------------------------------------------------------
def _live_tables(cur, schema: str, prefix: str) -> set[str]:
    """Table names in one schema with one prefix. sys.* not INFORMATION_SCHEMA
    — the latter is a view with per-row permission checks and is measurably
    slower on a large catalog."""
    # `_` is a WILDCARD in T-SQL LIKE, so 'cat_%' also matches catalog_setting,
    # catalog_audit and anything else beginning "cat". Bracket it. This cost a
    # false positive on the first real run.
    pattern = prefix.replace("_", "[_]") + "%"
    cur.execute("""
        SELECT t.name
        FROM sys.tables t WITH (NOLOCK)
        JOIN sys.schemas s WITH (NOLOCK) ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name LIKE ?
    """, schema, pattern)
    return {r[0] for r in cur.fetchall()}


def _columns(cur, schema: str, table: str) -> dict[str, str]:
    """{COLUMN_NAME: type_name}. Types matter — see _classify_drift."""
    cur.execute("""
        SELECT c.name, ty.name
        FROM sys.columns c WITH (NOLOCK)
        JOIN sys.types ty WITH (NOLOCK) ON ty.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(?)
    """, f"{schema}.{table}")
    return {r[0].upper(): r[1].lower() for r in cur.fetchall()}


def _promote_filled() -> set[str]:
    """Columns promote fills ITSELF, read from promote_catalog rather than
    guessed. A mirror is not required to carry these."""
    filled = {"ACTIVE_IND", "SOURCE"}
    try:
        from dataview.file_catalog import promote_catalog as pc
        filled |= {k.upper() for k in getattr(pc, "_AUDIT_FILL", {})}
    except Exception:
        filled |= {"ROW_CREATED_BY", "ROW_CREATED_DATE",
                   "ROW_CHANGED_BY", "ROW_CHANGED_DATE"}
    return filled


def _classify_drift(missing: dict[str, str], dv_table: str,
                    filled: set[str]) -> tuple[set[str], set[str]]:
    """(real, by_design).

    A column absent from a mirror is only a fault if the mirror was SUPPOSED to
    carry it from a document. Three kinds are absent on purpose:

      * geography/geometry — computed at promote time from lat/long or from
        survey stations; no document supplies a geography literal
      * audit columns promote fills itself (_AUDIT_FILL, active_ind, source)
      * the table's own surrogate key, minted during the move

    Everything else is real: a data column that documents could populate and
    that promote will silently never carry, because it moves rows by column
    name intersection."""
    stem = dv_table[3:] if dv_table.lower().startswith("dv_") else dv_table
    surrogate = {f"{stem.upper()}_ID"}
    real, design = set(), set()
    for col, ty in missing.items():
        if ty in ("geography", "geometry") or col in filled or col in surrogate:
            design.add(col)
        else:
            real.add(col)
    return real, design


def _promoter_map() -> tuple[dict[str, str], bool]:
    """(cat_table -> handler, declared?). Prefers a declaration; falls back to
    a source scan."""
    from dataview.file_catalog import promote_catalog as pc

    declared = getattr(pc, "DEDICATED_PROMOTERS", None)
    if isinstance(declared, dict) and declared:
        return {k.lower(): str(v) for k, v in declared.items()}, True

    try:
        src = inspect.getsource(pc)
    except OSError:
        return {}, False

    found: dict[str, str] = {}
    for name in set(re.findall(r"\bcat_[a-z0-9_]+", src)):
        # which function body mentions it — first match is good enough for a
        # human to go and look at
        for m in re.finditer(r"^def (\w+)\(", src, re.M):
            start = m.end()
            end = src.find("\ndef ", start)
            if name in src[start: end if end > 0 else len(src)]:
                found[name] = m.group(1)
                break
        found.setdefault(name, "(mentioned)")
    return found, False


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------
def check(cur) -> list[dict]:
    """Returns a list of problems. Empty list means the four lists agree."""
    from dataview.file_catalog.build_catalog_mirror import (
        MIRROR_TABLES, PROVENANCE, cat_name,
    )

    problems: list[dict] = []

    dv_live = {t.lower() for t in _live_tables(cur, DV_SCHEMA, "dv_")}
    cat_live = {t.lower() for t in _live_tables(cur, CAT_SCHEMA, "cat_")}
    allow = {t.lower() for t in MIRROR_TABLES}
    allow_cat = {cat_name(t).lower() for t in MIRROR_TABLES}
    promoters, declared = _promoter_map()

    def add(kind, subject, detail, fix):
        problems.append({"kind": kind, "subject": subject,
                         "detail": detail, "fix": fix})

    # A · allowlist entry names a dv_ table that exists
    for t in sorted(allow - dv_live):
        add("allowlist_names_missing_dv_table", t,
            f"MIRROR_TABLES names {DV_SCHEMA}.{t}, which does not exist",
            "remove it from MIRROR_TABLES, or create the dv_ table")

    # B · allowlist entry has a mirror
    for t in sorted(allow):
        cn = cat_name(t).lower()
        if cn not in cat_live:
            add("allowlist_has_no_mirror", cn,
                f"MIRROR_TABLES names {t} but {CAT_SCHEMA}.{cn} does not exist "
                f"— promote expects it and captured rows have nowhere to land",
                "python build_catalog_mirror.py --apply")

    # C · every mirror is reachable by something
    for cn in sorted(cat_live - allow_cat):
        if cn in promoters:
            continue
        add("mirror_walked_by_nothing", cn,
            f"{CAT_SCHEMA}.{cn} exists but is in neither MIRROR_TABLES nor any "
            f"promoter — rows captured here are silently stepped past, "
            f"reported as neither moved nor held",
            "add its dv_ table to MIRROR_TABLES, write a dedicated promoter, "
            "or drop the mirror if it is dead")

    # D · every mirror has somewhere to promote INTO
    for cn in sorted(cat_live):
        dv = "dv_" + cn[4:]
        if dv not in dv_live:
            add("mirror_has_no_dv_target", cn,
                f"{CAT_SCHEMA}.{cn} has no {DV_SCHEMA}.{dv} to promote into",
                "create the dv_ table, or drop the mirror")

    # E · column drift
    filled = _promote_filled()
    prov = {p.upper() for p in PROVENANCE}
    for cn in sorted(cat_live):
        dv = "dv_" + cn[4:]
        if dv not in dv_live:
            continue                        # already reported by D
        dv_cols = _columns(cur, DV_SCHEMA, dv)
        cat_cols = _columns(cur, CAT_SCHEMA, cn)
        missing = {c: t for c, t in dv_cols.items()
                   if c not in cat_cols and c not in prov}
        real, design = _classify_drift(missing, dv, filled)
        if real:
            add("mirror_column_drift", cn,
                f"{dv} has {len(real)} data column(s) the mirror lacks: "
                f"{', '.join(sorted(real))} — promote moves rows by column-name "
                f"intersection, so a document stating these can never land them"
                + (f"  (also absent by design: {len(design)} computed/audit/key "
                   f"column(s))" if design else ""),
                "python build_catalog_mirror.py --drop --apply  (rebuilds the "
                "mirror; capture anything still sitting in it FIRST)")

    # F · THE FOURTH LIST — promotion_lineage.LINEAGE
    #
    # A pair can pass A-E perfectly and still be invisible: capture writes rows,
    # promote lifts them, and every report says "no detail rows" because LINEAGE
    # is what any report can SEE. Casing, stimulation, petro_zone and
    # perforation were all missing at once — 1,433 rows in the database that no
    # report could find. CLAUDE.md has recorded this as the unchecked fourth
    # list since 16 Aug; this is the check it was owed.
    try:
        from dataview.file_catalog.promotion_lineage import LINEAGE
    except Exception as e:                       # never silently skip a check
        add("lineage_unreadable", "promotion_lineage",
            f"could not import LINEAGE ({type(e).__name__}: {e}) — the fourth "
            f"list could not be checked at all",
            "fix the import; a check that cannot run must not look like a pass")
        LINEAGE = None

    if LINEAGE is not None:
        lin_dv = {dv.lower() for _cat, dv, _lbl in LINEAGE}
        lin_cat = {c.lower() for c, _dv, _lbl in LINEAGE if c}

        for t in sorted(allow - lin_dv):
            add("dv_table_not_in_lineage", t,
                f"MIRROR_TABLES promotes into {DV_SCHEMA}.{t}, but LINEAGE does "
                f"not name it — rows land and every report says 'no detail rows'",
                f"add a ({cat_name(t)}, {t}, <label>) row to "
                f"promotion_lineage.LINEAGE")

        for dv in sorted(lin_dv - dv_live):
            add("lineage_names_missing_dv_table", dv,
                f"LINEAGE names {DV_SCHEMA}.{dv}, which does not exist",
                "remove the row from LINEAGE, or create the dv_ table")

        for cn in sorted(lin_cat - cat_live):
            add("lineage_names_missing_mirror", cn,
                f"LINEAGE names {CAT_SCHEMA}.{cn} as the staging table, but it "
                f"does not exist — the 'staged' half of the report is dead",
                "set the cat_ entry to None if the pair has no staging table, "
                "or build the mirror")

        for cn in sorted(cat_live - lin_cat):
            dv = "dv_" + cn[4:]
            if dv not in dv_live:
                continue                        # already reported by D
            add("mirror_not_in_lineage", cn,
                f"{CAT_SCHEMA}.{cn} exists and has a dv_ target, but neither "
                f"appears in LINEAGE — anything captured here is unreportable",
                f"add a ({cn}, {dv}, <label>) row to promotion_lineage.LINEAGE")

    if not declared:
        problems.append({
            "kind": "advisory_no_declaration",
            "subject": "promote_catalog",
            "detail": "promote_catalog has no DEDICATED_PROMOTERS dict, so "
                      "promoter detection fell back to scanning source text — "
                      "it cannot tell a real handler from a mention in a comment",
            "fix": "add DEDICATED_PROMOTERS (see this module's docstring)",
        })

    return problems


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        from dataview.file_catalog.build_catalog_mirror import connect
        con = connect(a.server, a.database)
    except Exception as exc:
        print(f"could not connect: {exc}", file=sys.stderr)
        return 2

    try:
        problems = check(con.cursor())
    except Exception as exc:
        print(f"check failed to run: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()

    if a.json:
        print(json.dumps(problems, indent=2))
        return 1 if any(p["kind"] != "advisory_no_declaration" for p in problems) else 0

    real = [p for p in problems if p["kind"] != "advisory_no_declaration"]
    advisory = [p for p in problems if p["kind"] == "advisory_no_declaration"]

    print(f"mirror registry check · {a.server} / {a.database}")
    print("=" * 70)
    if not real:
        print("OK — the allowlist, the mirror tables, the promoters and "
              "LINEAGE agree.")
    else:
        for p in real:
            print(f"\n[{p['kind']}]  {p['subject']}")
            print(f"  {p['detail']}")
            print(f"  fix: {p['fix']}")
        print(f"\n{len(real)} disagreement(s).")

    for p in advisory:
        print(f"\nnote: {p['detail']}\n  {p['fix']}")

    return 1 if real else 0


if __name__ == "__main__":
    raise SystemExit(main())
