"""
dataview/migration/synonyms.py
=============================
A synonym dictionary for column matching — the missing half of "fingerprints
and synonyms".

WHAT THE FINGERPRINT ALREADY DOES, AND WHAT IT DOESN'T
------------------------------------------------------
import_data/mapping.py::mapping_fingerprint keys on
    sha256(TARGET_TABLE | sorted(source columns))
so a saved mapping is restored only for the SAME target and the EXACT SAME set
of source columns. That's match-once-per-TABLE-PAIR. Add or drop one source
column and the fingerprint misses entirely and every column is re-matched from
scratch.

A synonym is match-once-per-ATTRIBUTE: learn `kb_elevation ≡ KB_ELEV` once and
every table that ever presents `kb_elevation` inherits it, regardless of what
else is in the column set. Across the two dozen child tables still to map, that
is the difference between mapping an attribute once and mapping it repeatedly.

WHY FUZZY MATCHING ISN'T ENOUGH
-------------------------------
build_mapping scores with rapidfuzz (ratio / token_sort_ratio / partial_ratio,
min_score 60). It handles `ground_elevation → GROUND_ELEV` fine. It cannot
handle the case that actually matters:

    dv_well.well_status  →  PPDM well.CURRENT_STATUS   (correct)
                        →  PPDM well.STATUS_TYPE       (also scores well)

Both are real columns on `well`, both foreign-key to r_well_status. There is no
string-similarity signal that prefers the right one, so no threshold tweak fixes
it. It needs a recorded decision — which is what this is.

PRECEDENCE
----------
    per-table synonym  >  global synonym  >  fuzzy auto-match

Per-table exists because some aliases are only true in one context:
`source → PRIMARY_SOURCE` is right on `well`, but on another table `source`
might be the audit column of the same name. Global covers the aliases that hold
everywhere (`kb_elevation ≡ KB_ELEV`).

A synonym NEVER overrides a user's own decision. It replaces auto-matches only
(auto_matched=True) and skips anything the user explicitly cleared
(explicitly_skipped=True) — otherwise a learned alias would keep undoing a
deliberate correction.

LEARNING
--------
learn_from_mapping() records the pairs a user set by hand — MappedColumn entries
with auto_matched=False. Those are exactly the decisions worth keeping: the ones
the fuzzy matcher got wrong or missed. Call it when a mapping is saved and the
dictionary improves as the work gets done, rather than needing to be authored up
front.

USAGE
-----
    from dataview.migration.synonyms import (
        apply_synonyms, learn_from_mapping, build_mapping_with_synonyms)

    cm = build_mapping_with_synonyms(target, target_col_defs, source_cols)
    ...user edits the grid...
    learn_from_mapping(cm)

    py -m dataview.migration.synonyms --list
    py -m dataview.migration.synonyms --add well CURRENT_STATUS well_status
"""
from __future__ import annotations

import json
from pathlib import Path

# Sits beside mapping_cache.json so the two caches live together.
SYNONYMS_PATH = (Path(__file__).resolve().parent.parent
                 / "import_data" / "column_synonyms.json")

SCHEMA_VERSION = 1

# Seeded from the measured dv_well -> PPDM well comparison. Everything here is
# a pair confirmed against both live schemas, not a guess at PPDM convention.
_SEED = {
    "version": SCHEMA_VERSION,
    "global": {
        # Elevation columns: PPDM abbreviates, DataView spells out. True
        # wherever both appear.
        "GROUND_ELEV":        ["ground_elevation"],
        "KB_ELEV":            ["kb_elevation", "kelly_bushing_elevation"],
        "DERRICK_FLOOR_ELEV": ["derrick_floor_elevation"],
        "ROTARY_TABLE_ELEV":  ["rotary_table_elevation"],
        "DEPTH_DATUM_ELEV":   ["depth_datum_elevation"],
    },
    "by_table": {
        "well": {
            # Order matters — first alias present in the source wins, so the
            # primary operator column is listed ahead of the current one.
            "OPERATOR":         ["operator_ba_id", "current_operator_ba_id"],
            "ASSIGNED_FIELD":   ["field_id"],
            # The case fuzzy matching cannot decide: STATUS_TYPE scores just as
            # well and is the wrong answer.
            "CURRENT_STATUS":   ["well_status"],
            "PRIMARY_SOURCE":   ["source"],
            "ENVIRONMENT_TYPE": ["onshore_offshore_ind"],
        },
    },
}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def load(path: Path = SYNONYMS_PATH) -> dict:
    """The dictionary, seeded on first use. A corrupt file falls back to the
    seed rather than silently losing every alias."""
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "global" in d:
                d.setdefault("by_table", {})
                return d
    except Exception:
        pass
    return json.loads(json.dumps(_SEED))      # deep copy


def save(d: dict, path: Path = SYNONYMS_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #
def resolve(target_table: str, target_col: str, source_columns,
            d: dict | None = None) -> str | None:
    """The source column this target column should take, or None.

    Returns the source column in its ORIGINAL case — callers put it straight
    into SQL, and the source may be case-sensitive even when SQL Server isn't.
    """
    d = d if d is not None else load()
    tgt = (target_col or "").upper()
    by_src = {str(c).upper(): str(c) for c in (source_columns or [])}

    tbl = (target_table or "").lower()
    for bucket in (d.get("by_table", {}).get(tbl, {}), d.get("global", {})):
        for alias in bucket.get(tgt, []):
            hit = by_src.get(str(alias).upper())
            if hit:
                return hit
    return None


def add(target_table: str | None, target_col: str, source_col: str,
        d: dict | None = None, path: Path = SYNONYMS_PATH) -> dict:
    """Record an alias. target_table None/'' -> global."""
    d = d if d is not None else load(path)
    tgt = (target_col or "").upper()
    src = str(source_col or "").strip()
    if not tgt or not src:
        return d
    if target_table:
        bucket = d.setdefault("by_table", {}).setdefault(
            str(target_table).lower(), {})
    else:
        bucket = d.setdefault("global", {})
    aliases = bucket.setdefault(tgt, [])
    if not any(a.upper() == src.upper() for a in aliases):
        aliases.append(src)
    save(d, path)
    return d


def remove(target_table: str | None, target_col: str, source_col: str,
           d: dict | None = None, path: Path = SYNONYMS_PATH) -> dict:
    d = d if d is not None else load(path)
    tgt = (target_col or "").upper()
    bucket = (d.get("by_table", {}).get(str(target_table).lower(), {})
              if target_table else d.get("global", {}))
    if tgt in bucket:
        bucket[tgt] = [a for a in bucket[tgt]
                       if a.upper() != str(source_col).upper()]
        if not bucket[tgt]:
            del bucket[tgt]
        save(d, path)
    return d


# --------------------------------------------------------------------------- #
# Apply / learn
# --------------------------------------------------------------------------- #
def apply_synonyms(col_mapping, d: dict | None = None) -> list[tuple[str, str]]:
    """Override auto-matches in a ColumnMapping using the dictionary.

    Returns the [(target_col, source_col)] pairs it changed. Leaves alone:
      * columns the user mapped themselves (auto_matched False)
      * columns the user explicitly cleared (explicitly_skipped True)
      * columns already pointing at the synonym's answer
    so applying twice is a no-op and a learned alias never fights a correction.

    When a synonym claims a source column, any OTHER auto-match holding that
    same source column is released. Without that, the exact case the dictionary
    exists to fix stays half-broken: the synonym gives
    `CURRENT_STATUS <- well_status`, but `STATUS_TYPE` keeps the wrong
    auto-match to `well_status` and both get written. A user's own mapping is
    never released this way — only auto-matches.
    """
    d = d if d is not None else load()
    applied: list[tuple[str, str]] = []
    src_cols = getattr(col_mapping, "source_columns", []) or []
    table = getattr(col_mapping, "target_table", "") or ""
    mapped = getattr(col_mapping, "mapped", [])

    for m in mapped:
        if getattr(m, "explicitly_skipped", False):
            continue
        if not getattr(m, "auto_matched", True) and getattr(m, "source_col", ""):
            continue                       # user's own choice — leave it
        hit = resolve(table, m.ppdm_col, src_cols, d)
        if not hit or hit == getattr(m, "source_col", ""):
            continue
        for other in mapped:
            if other is m:
                continue
            if (getattr(other, "source_col", "") == hit
                    and getattr(other, "auto_matched", True)
                    and not getattr(other, "explicitly_skipped", False)):
                other.source_col = ""
                other.match_score = 0
        m.source_col = hit
        m.match_score = 100
        m.auto_matched = True
        applied.append((m.ppdm_col, hit))
    return applied


def learn_from_mapping(col_mapping, d: dict | None = None,
                       path: Path = SYNONYMS_PATH,
                       table_scoped: bool = True) -> list[tuple[str, str]]:
    """Record the pairs the user set by hand.

    A MappedColumn with auto_matched=False is a deliberate override — precisely
    where the fuzzy matcher was wrong or silent, and the only thing worth
    learning. Auto-matches are not recorded: they're re-derivable, and storing
    them would bloat the dictionary with pairs that never needed help.

    Defaults to table-scoped because an alias proven on one target isn't
    automatically true everywhere; promote it to global by hand once you've
    seen it hold on a second table.
    """
    d = d if d is not None else load(path)
    table = getattr(col_mapping, "target_table", "") or ""
    learned: list[tuple[str, str]] = []

    for m in getattr(col_mapping, "mapped", []):
        src = getattr(m, "source_col", "")
        if not src or getattr(m, "auto_matched", True):
            continue
        if getattr(m, "explicitly_skipped", False):
            continue
        if src.upper() == m.ppdm_col.upper():
            continue                       # exact name — needs no synonym
        if resolve(table, m.ppdm_col, [src], d) == src:
            continue                       # already known
        add(table if table_scoped else None, m.ppdm_col, src, d, path)
        learned.append((m.ppdm_col, src))
    return learned


def build_mapping_with_synonyms(target_table, target_col_defs, source_columns,
                                min_score: int = 60, d: dict | None = None):
    """build_mapping() with the dictionary applied on top.

    Synonyms run AFTER the fuzzy pass rather than instead of it, so unknown
    columns still get a suggestion and the dictionary only has to hold the
    cases fuzzy matching gets wrong.
    """
    from dataview.import_data.mapping import build_mapping
    cm = build_mapping(target_table, target_col_defs, source_columns, min_score)
    apply_synonyms(cm, d)
    return cm


# --------------------------------------------------------------------------- #
# CLI  —  run from the repo root:  py -m dataview.migration.synonyms --list
# --------------------------------------------------------------------------- #
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Column synonym dictionary")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--add", nargs=3, metavar=("TABLE", "TARGET_COL", "SOURCE_COL"),
                    help="use '-' as TABLE for a global alias")
    ap.add_argument("--remove", nargs=3,
                    metavar=("TABLE", "TARGET_COL", "SOURCE_COL"))
    ap.add_argument("--test", nargs=2, metavar=("TABLE", "TARGET_COL"),
                    help="resolve against a comma-separated --source list")
    ap.add_argument("--source", default="",
                    help="comma-separated source columns, for --test")
    ap.add_argument("--path", default=str(SYNONYMS_PATH))
    a = ap.parse_args()
    path = Path(a.path)

    if a.add:
        tbl = None if a.add[0] == "-" else a.add[0]
        add(tbl, a.add[1], a.add[2], path=path)
        print(f"added {a.add[1]} <- {a.add[2]} "
              f"({'global' if tbl is None else tbl})")
        return 0
    if a.remove:
        tbl = None if a.remove[0] == "-" else a.remove[0]
        remove(tbl, a.remove[1], a.remove[2], path=path)
        print("removed")
        return 0
    if a.test:
        hit = resolve(a.test[0], a.test[1],
                      [c.strip() for c in a.source.split(",") if c.strip()],
                      load(path))
        print(f"{a.test[0]}.{a.test[1]} -> {hit or '(no synonym)'}")
        return 0

    d = load(path)
    print(f"-- {path}")
    print(f"-- global: {len(d.get('global', {}))} · "
          f"tables: {len(d.get('by_table', {}))}")
    for col, aliases in sorted(d.get("global", {}).items()):
        print(f"   [global] {col:28} <- {', '.join(aliases)}")
    for tbl, cols in sorted(d.get("by_table", {}).items()):
        for col, aliases in sorted(cols.items()):
            print(f"   [{tbl}] {col:28} <- {', '.join(aliases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
