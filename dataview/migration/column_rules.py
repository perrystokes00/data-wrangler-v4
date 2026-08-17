"""
dataview/migration/column_rules.py
=================================
Values for target columns that DON'T come from a source column.

Synonyms answer "which source column feeds this target column". This answers
the other half: "what if none does". Three kinds of rule, matching what
MappedColumn already supports —

    const      a literal              SOURCE = 'CATALOG'
    expr       a SQL expression       PPDM_GUID = NEWID()
                                      SEQ_NO   = ROW_NUMBER() OVER (...)
    transform  applied to the source  UPPER / LOWER / TRIM

Nothing here is new capability. mapping.py's MappedColumn already carries
const_value, auto_gen_expr and transform, and select_expr resolves them in
priority order (auto_generated > const > source+transform). What was missing is
somewhere to KEEP those decisions between runs — exactly the gap synonyms
filled for column pairing.

Stored beside column_synonyms.json, same global/per-table shape, same
precedence: a per-table rule beats a global one.

A NOTE ON `expr`
----------------
The expression is placed into the generated SELECT verbatim. That's deliberate
— it's what makes NEWID(), GETDATE(), CONCAT(...) and window functions
possible — but it means a rule is executable SQL, not data. Fine for a tool
where the person writing the rule owns the database; worth knowing before
exposing it to anyone who doesn't.

SEQUENCE NUMBERS, CAREFULLY
---------------------------
ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) numbers rows within THIS BATCH,
starting at 1 every run. That's correct for a batch-scoped ordinal and wrong
for a durable key — a second load would re-issue numbers already in the table.
For a durable unique value use NEWID(), or seed from the target's own maximum.
There's a helper for the latter, since getting it wrong produces duplicate keys
that only surface much later.
"""
from __future__ import annotations

import json
from pathlib import Path

RULES_PATH = (Path(__file__).resolve().parent.parent
              / "import_data" / "column_rules.json")

KINDS = ("const", "expr", "transform")

# Ready-made expressions for the cases that come up, so common intent doesn't
# depend on remembering T-SQL. Shown in the UI as a picklist.
PRESETS = {
    "New GUID":                "NEWID()",
    "Current UTC timestamp":   "SYSUTCDATETIME()",
    "Current timestamp":       "GETDATE()",
    "Current user":            "SYSTEM_USER",
    "Row number (this batch)": "ROW_NUMBER() OVER (ORDER BY (SELECT NULL))",
}


def load(path: Path = RULES_PATH) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("global", {})
                d.setdefault("by_table", {})
                return d
    except Exception:
        pass
    return {"global": {}, "by_table": {}}


def save(d: dict, path: Path = RULES_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except Exception:
        return False


def rules_for(target_table: str, d: dict | None = None) -> dict:
    """{TARGET_COL_UPPER: {kind: value}} for one target — per-table overriding
    global, so a table can opt out of a global default."""
    d = d if d is not None else load()
    out = {k.upper(): v for k, v in (d.get("global") or {}).items()}
    out.update({k.upper(): v for k, v
                in (d.get("by_table", {}).get((target_table or "").lower())
                    or {}).items()})
    return out


def set_rule(target_table: str | None, target_col: str, kind: str, value: str,
             d: dict | None = None, path: Path = RULES_PATH) -> dict:
    """Record a rule. target_table None/'' -> global."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    d = d if d is not None else load(path)
    bucket = (d.setdefault("by_table", {}).setdefault(
                  str(target_table).lower(), {})
              if target_table else d.setdefault("global", {}))
    bucket[target_col.upper()] = {kind: value}
    save(d, path)
    return d


def clear_rule(target_table: str | None, target_col: str,
               d: dict | None = None, path: Path = RULES_PATH) -> dict:
    d = d if d is not None else load(path)
    bucket = (d.get("by_table", {}).get(str(target_table).lower(), {})
              if target_table else d.get("global", {}))
    if bucket.pop(target_col.upper(), None) is not None:
        save(d, path)
    return d


def apply_rules(col_mapping, d: dict | None = None) -> list[tuple[str, str]]:
    """Stamp the rules onto a ColumnMapping. Returns [(col, description)].

    Deliberately does NOT clear a source column when setting a constant: a
    column can legitimately have both, and select_expr then emits
    COALESCE(source, constant) — the source value where present, the constant
    as a fallback. That's usually what someone means by "default this column".
    An `expr` rule DOES take over completely, because auto_generated wins
    outright in select_expr and a half-applied expression would be a lie.
    """
    d = d if d is not None else load()
    table = getattr(col_mapping, "target_table", "") or ""
    rules = rules_for(table, d)
    applied: list[tuple[str, str]] = []

    for m in getattr(col_mapping, "mapped", []):
        r = rules.get(getattr(m, "ppdm_col", "").upper())
        if not r:
            continue
        if "expr" in r:
            m.auto_generated = True
            m.auto_gen_expr = r["expr"]
            applied.append((m.ppdm_col, f"expr {r['expr']}"))
        elif "const" in r:
            m.const_value = r["const"]
            applied.append((m.ppdm_col, f"const '{r['const']}'"))
        elif "transform" in r:
            m.transform = r["transform"]
            applied.append((m.ppdm_col, f"transform {r['transform']}"))
    return applied


def build_mapping_with_rules(target_table, target_col_defs, source_columns,
                             min_score: int = 60):
    """The full stack: auto-match, then synonyms, then rules.

    Order matters. Synonyms decide WHERE a value comes from; rules decide what
    it is when nothing does, or how to shape it. Applying rules first would let
    a later synonym silently re-point a column the user had pinned to a
    constant.
    """
    from dataview.migration.synonyms import build_mapping_with_synonyms
    cm = build_mapping_with_synonyms(target_table, target_col_defs,
                                     source_columns, min_score)
    apply_rules(cm)
    return cm


def next_sequence_expr(conn, table, column, db="PPDM39", schema="dbo") -> str:
    """An expression continuing a numeric key from the target's own maximum.

    ROW_NUMBER() alone restarts at 1 every batch, so a second load re-issues
    numbers the table already holds. Offsetting by the current maximum keeps a
    numeric key unique across runs. Read once at build time — the value is
    baked into the SQL, so two concurrent loads would still collide. For a key
    that must survive that, use NEWID().
    """
    from sqlalchemy import text
    try:
        n = conn.execute(text(
            f"SELECT ISNULL(MAX(TRY_CONVERT(bigint, [{column}])), 0) "
            f"FROM [{db}].[{schema}].[{table}]")).scalar() or 0
    except Exception:
        n = 0
    return f"{int(n)} + ROW_NUMBER() OVER (ORDER BY (SELECT NULL))"


# --------------------------------------------------------------------------- #
# CLI — run from the repo root:  py -m dataview.migration.column_rules --list
# --------------------------------------------------------------------------- #
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Target-column value rules")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--set", nargs=4,
                    metavar=("TABLE", "COLUMN", "KIND", "VALUE"),
                    help="TABLE '-' for global; KIND is const|expr|transform")
    ap.add_argument("--clear", nargs=2, metavar=("TABLE", "COLUMN"))
    ap.add_argument("--presets", action="store_true")
    a = ap.parse_args()

    if a.presets:
        for k, v in PRESETS.items():
            print(f"   {k:26} {v}")
        return 0
    if a.set:
        tbl = None if a.set[0] == "-" else a.set[0]
        set_rule(tbl, a.set[1], a.set[2], a.set[3])
        print(f"set {a.set[1]} = [{a.set[2]}] {a.set[3]} "
              f"({'global' if tbl is None else tbl})")
        return 0
    if a.clear:
        tbl = None if a.clear[0] == "-" else a.clear[0]
        clear_rule(tbl, a.clear[1])
        print("cleared")
        return 0

    d = load()
    print(f"-- {RULES_PATH}")
    for col, r in sorted((d.get("global") or {}).items()):
        k, v = next(iter(r.items()))
        print(f"   [global] {col:28} {k:9} {v}")
    for tbl, cols in sorted((d.get("by_table") or {}).items()):
        for col, r in sorted(cols.items()):
            k, v = next(iter(r.items()))
            print(f"   [{tbl}] {col:28} {k:9} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
