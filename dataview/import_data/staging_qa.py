"""
staging_qa.py — data-quality report over the STAGING tables, between Stage and Promote.

Route A profiled the CSV in pandas. By the time route B has something to check, the rows are
already in SQL Server as nvarchar — so the whole profile is ONE set-based query per staging
table (conditional aggregates over every mapped column at once), not a row-by-row pass.

What it checks, per mapped column, against the TARGET column's real type:

  🔴 flag  — would fail or lose data at promote
             · TRY_CONVERT to the target type returns NULL for a non-blank value
               (the value silently becomes NULL — the worst outcome, no error, wrong data)
             · LEN exceeds the target's max_len (truncation → 2628, or a silently short key)
             · blank in a NOT NULL target with no default
  🟡 fix   — repairable, and worth repairing before it lands
             · scientific notation in an identifier (Excel turns 42329100010000 into
               4.23291E+13 — the classic mangled-UWI bug)
             · control characters / NULL bytes (seen in scraped operator names)
             · leading/trailing whitespace beyond what LTRIM/RTRIM handles
             · mixed date formats in one column (03/04 vs 2021-06-30)
  ✅ ok    — nothing to say

The report is advisory: it never changes data. It tells you what promote will do to it.
"""
from __future__ import annotations

_NUMERIC = ("numeric", "decimal", "float", "real", "int", "bigint", "smallint", "tinyint",
            "money", "smallmoney")
_DATE = ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset")
_TEXT = ("char", "nchar", "varchar", "nvarchar")


def _q(name):
    return "[" + str(name).replace("]", "]]") + "]"


# identifier target columns promote de-separates before insert — must match
# bulk_dir_loader._IDENT, or the length check and the promote transform disagree.
_IDENT = {"uwi", "api", "api_num", "api_number", "api_no", "api14", "api_14"}


def _col_checks(src, tgt_type, tgt_len, notnull, is_ident=False):
    """Conditional aggregates for one column → list of (alias, sql_expr)."""
    v = f"NULLIF(LTRIM(RTRIM(s.{_q(src)})),'')"
    out = [(f"n_blank__{src}", f"SUM(CASE WHEN {v} IS NULL THEN 1 ELSE 0 END)")]

    t = (tgt_type or "").lower()
    if t in _NUMERIC:
        out.append((f"n_badtype__{src}",
                    f"SUM(CASE WHEN {v} IS NOT NULL AND TRY_CONVERT(float, {v}) IS NULL "
                    f"THEN 1 ELSE 0 END)"))
    elif t in _DATE:
        # Day-first-safe validity check — MUST match bulk_dir_loader._typed's promote
        # transform: try style 105 (dd-mm-yyyy) first, then the default parse. Without
        # this the checker used only the US default and flagged every day-first date
        # (e.g. 18-09-1992) as "won't convert" — false alarms the load wouldn't hit.
        out.append((f"n_badtype__{src}",
                    f"SUM(CASE WHEN {v} IS NOT NULL "
                    f"AND COALESCE(TRY_CONVERT(datetime2, {v}, 105), "
                    f"TRY_CONVERT(datetime2, {v})) IS NULL "
                    f"THEN 1 ELSE 0 END)"))
        # a column holding BOTH d/m/y-ish and ISO values is ambiguous, not just convertible
        out.append((f"n_slash__{src}",
                    f"SUM(CASE WHEN {v} LIKE '%[0-9]/%' THEN 1 ELSE 0 END)"))
        out.append((f"n_iso__{src}",
                    f"SUM(CASE WHEN {v} LIKE '[12][0-9][0-9][0-9]-[0-9][0-9]-%' "
                    f"THEN 1 ELSE 0 END)"))
    if t in _TEXT and tgt_len and tgt_len > 0:
        # Measure the length promote will ACTUALLY insert. Promote de-separates identifier
        # columns only (uwi/api...) — REPLACE(REPLACE(REPLACE(x,'-',''),' ',''),'.','') — so a
        # 17-char '<uwi>-SRVY' fits a char(14) target once stripped and must not be flagged.
        # But a text column (well_name, operator) is inserted verbatim, so stripping its length
        # would UNDER-report a real overflow. Mirror promote exactly: strip iff identifier.
        # `is_ident` is passed in, matching bulk_dir_loader._IDENT on the TARGET column.
        lv = (f"LEN(REPLACE(REPLACE(REPLACE({v},'-',''),' ',''),'.',''))" if is_ident
              else f"LEN({v})")
        out.append((f"n_long__{src}",
                    f"SUM(CASE WHEN {lv} > {int(tgt_len)} THEN 1 ELSE 0 END)"))
        out.append((f"maxlen__{src}", f"MAX({lv})"))

    # Excel mangles long identifiers into scientific notation — 42329100010000 -> 4.23291E+13.
    out.append((f"n_sci__{src}",
                f"SUM(CASE WHEN {v} LIKE '%[0-9]E+[0-9]%' OR {v} LIKE '%[0-9]e+[0-9]%' "
                f"THEN 1 ELSE 0 END)"))
    # control characters / NULL bytes
    out.append((f"n_ctrl__{src}",
                f"SUM(CASE WHEN {v} LIKE '%' + CHAR(0) + '%' OR {v} LIKE '%' + CHAR(9) + '%' "
                f"OR {v} LIKE '%' + CHAR(10) + '%' OR {v} LIKE '%' + CHAR(13) + '%' "
                f"THEN 1 ELSE 0 END)"))
    # whitespace that LTRIM/RTRIM would silently eat (so the stored value differs from source)
    out.append((f"n_ws__{src}",
                f"SUM(CASE WHEN s.{_q(src)} IS NOT NULL AND s.{_q(src)} <> LTRIM(RTRIM(s.{_q(src)})) "
                f"THEN 1 ELSE 0 END)"))
    return out


def profile(engine, stg_table, cmap, coltypes, collens, notnulls=None):
    """One query, every mapped column.

    stg_table — 'stg.dv_well_docx'
    cmap      — {source_col: target_col}
    coltypes  — {target_col_lower: sql type}
    collens   — {target_col_lower: max_len or None}
    notnulls  — {target_col_lower} that are NOT NULL

    → list of per-column dicts, worst first.
    """
    from sqlalchemy import text
    notnulls = notnulls or set()
    if not cmap:
        return []
    aliases, exprs = [], []
    for src, tgt in cmap.items():
        tl = str(tgt).lower()
        for alias, expr in _col_checks(src, coltypes.get(tl), collens.get(tl),
                                       tl in notnulls, is_ident=(tl in _IDENT)):
            aliases.append(alias)
            exprs.append(f"{expr} AS {_q(alias)}")
    sql = f"SELECT COUNT(*) AS n_rows, {', '.join(exprs)} FROM {stg_table} s"
    with engine.connect() as cx:
        row = cx.execute(text(sql)).mappings().first()
    if not row:
        return []
    n = row["n_rows"] or 0

    out = []
    for src, tgt in cmap.items():
        tl = str(tgt).lower()
        g = lambda k: (row.get(f"{k}__{src}") or 0)
        blank, bad, long_, sci = g("n_blank"), g("n_badtype"), g("n_long"), g("n_sci")
        ctrl, ws = g("n_ctrl"), g("n_ws")
        slash, iso = g("n_slash"), g("n_iso")
        maxlen = row.get(f"maxlen__{src}")
        issues, level = [], "ok"

        if bad:
            issues.append(f"{bad} value(s) won't convert to {coltypes.get(tl, '?')} → "
                          f"they load as NULL, silently")
            level = "flag"
        if long_:
            issues.append(f"{long_} value(s) exceed {collens.get(tl)} chars "
                          f"(longest {maxlen}) → truncation error at promote")
            level = "flag"
        if blank and tl in notnulls:
            issues.append(f"{blank} blank(s) into a NOT NULL column → promote will fail")
            level = "flag"
        if sci:
            issues.append(f"{sci} value(s) in scientific notation "
                          f"(Excel mangled a long number, e.g. 4.23291E+13)")
            level = "flag" if level == "flag" else "fix"
        if ctrl:
            issues.append(f"{ctrl} value(s) contain control characters / NULL bytes")
            level = "flag" if level == "flag" else "fix"
        if slash and iso:
            issues.append(f"mixed date formats — {slash} like 03/04/2021 and {iso} like "
                          f"2021-06-30; d/m vs m/d is a guess")
            level = "flag" if level == "flag" else "fix"
        if ws:
            issues.append(f"{ws} value(s) have surrounding whitespace (trimmed on load)")
            level = "flag" if level == "flag" else "fix"
        if blank and tl not in notnulls and blank == n:
            issues.append("every value is blank — column carries no data")
            level = "flag" if level == "flag" else "fix"

        out.append({"source": src, "target": tgt, "type": coltypes.get(tl, ""),
                    "rows": n, "blank": blank, "level": level,
                    "issues": issues or ["—"]})
    rank = {"flag": 0, "fix": 1, "ok": 2}
    return sorted(out, key=lambda r: (rank[r["level"]], r["source"]))


def summary(results):
    """{'flag': n, 'fix': n, 'ok': n}"""
    out = {"flag": 0, "fix": 0, "ok": 0}
    for r in results:
        out[r["level"]] += 1
    return out
