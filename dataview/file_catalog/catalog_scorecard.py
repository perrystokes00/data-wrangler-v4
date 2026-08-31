"""
catalog_scorecard.py
=====================
DataView v3 — standalone load/status scorecard for the document catalog.

Reports, from the live database, how much has been captured into the
`file_catalog.cat_*` mirrors and how far along the catalog is:

  • Files loaded — to date / this month / today   (distinct source files)
  • Rows loaded  — to date / this month            (across every mirror)
  • Catalog overview — total / cataloged / extracted / flagged / bad
  • Catalog by file type — total + cataloged per FILE_TYPE_GROUP
  • Data types populated — one row per cat_* mirror: rows, files, this-month,
    promoted / pending

The cat_* mirror tables are discovered dynamically (any table in the
file_catalog schema named cat_* that has an INVENTORY_ID column), so new
mirrors are picked up automatically. Read-only — runs no DDL or DML.

    python catalog_scorecard.py                       # print to console
    python catalog_scorecard.py --month 2026-05       # a specific month
    python catalog_scorecard.py --out scorecard.md    # also write Markdown
    python catalog_scorecard.py --csv types.csv       # data-types table as CSV

Default target: PERRY\\SQLEXPRESS / DataView (Windows auth, ODBC Driver 17).
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone

try:
    import pyodbc
except ImportError:                                   # pragma: no cover
    pyodbc = None


# ─────────────────────────────────────────────────────────────────────────────
# Connection (matches build_catalog_mirror.py / promote_catalog.py)
# ─────────────────────────────────────────────────────────────────────────────
def connect(server: str, database: str):
    cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
          f"DATABASE={database};Trusted_Connection=yes;")
    return pyodbc.connect(cs, autocommit=True)


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no DB — unit-testable)
# ─────────────────────────────────────────────────────────────────────────────
def month_start(month: str | None):
    """Return (start_dt, label). month is 'YYYY-MM' or None (=> current UTC)."""
    if month:
        y, m = month.split("-")
        start = datetime(int(y), int(m), 1)
    else:
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1)
    return start, start.strftime("%Y-%m")


def build_files_union_sql(tables):
    """SQL counting DISTINCT source files (to date / this month / today)
    across every mirror, de-duplicated so a file in several mirrors counts once.
    Placeholders: ? = month_start, ? = today (date)."""
    union = " UNION ALL ".join(
        f"SELECT INVENTORY_ID, CAPTURED_AT FROM file_catalog.[{t}]"
        for t in tables)
    return f"""
        SELECT
            COUNT(DISTINCT INVENTORY_ID) AS files_total,
            COUNT(DISTINCT CASE WHEN CAPTURED_AT >= ?
                                THEN INVENTORY_ID END) AS files_month,
            COUNT(DISTINCT CASE WHEN CAST(CAPTURED_AT AS date) = ?
                                THEN INVENTORY_ID END) AS files_today
        FROM ( {union} ) u
    """


def per_table_sql(table):
    """Per-mirror aggregate. Placeholder: ? = month_start."""
    return f"""
        SELECT
            COUNT(*)                                              AS rows_total,
            SUM(CASE WHEN CAPTURED_AT >= ? THEN 1 ELSE 0 END)     AS rows_month,
            COUNT(DISTINCT INVENTORY_ID)                          AS files,
            SUM(CASE WHEN PROMOTED = 1 THEN 1 ELSE 0 END)         AS promoted
        FROM file_catalog.[{table}]
    """


def _bar(n, total, width=22):
    """A tiny text progress bar for the overview percentages."""
    if not total:
        return "·" * width
    fill = int(round(width * n / total))
    return "█" * fill + "·" * (width - fill)


def _fmt_int(n):
    return f"{int(n or 0):,}"


# ─────────────────────────────────────────────────────────────────────────────
# DB gathering
# ─────────────────────────────────────────────────────────────────────────────
def discover_cat_tables(cur):
    cur.execute("""
        SELECT t.name
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.columns c ON c.object_id = t.object_id
        WHERE s.name = 'file_catalog'
          AND t.name LIKE 'cat[_]%'
          AND c.name = 'INVENTORY_ID'
        ORDER BY t.name
    """)
    return [r[0] for r in cur.fetchall()]


def _table_exists(cur, schema, name):
    cur.execute("SELECT OBJECT_ID(?)", (f"{schema}.{name}",))
    return cur.fetchone()[0] is not None


def gather(cur, month):
    start, label = month_start(month)
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data = {"month_label": label, "tables": [], "has_global": False}

    # ── Catalog overview (GLOBAL_FILE_CATALOG) ──────────────────────────────
    if _table_exists(cur, "file_catalog", "GLOBAL_FILE_CATALOG"):
        data["has_global"] = True
        cur.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN CATALOG_READINESS='CATALOGED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN HEADER_EXTRACTED='Y'          THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ISNULL(FLAG_DELETE,'N')='Y'   THEN 1 ELSE 0 END)
            FROM file_catalog.GLOBAL_FILE_CATALOG
        """)
        tot, cat, ext, flg = cur.fetchone()
        data["overview"] = {"total": tot or 0, "cataloged": cat or 0,
                            "extracted": ext or 0, "flagged": flg or 0}

        cur.execute("""
            SELECT ISNULL(FILE_TYPE_GROUP,'(none)'), COUNT(*),
                   SUM(CASE WHEN CATALOG_READINESS='CATALOGED' THEN 1 ELSE 0 END)
            FROM file_catalog.GLOBAL_FILE_CATALOG
            GROUP BY FILE_TYPE_GROUP
            ORDER BY COUNT(*) DESC
        """)
        data["by_type"] = [(r[0], r[1] or 0, r[2] or 0) for r in cur.fetchall()]
    else:
        data["overview"] = {"total": 0, "cataloged": 0,
                            "extracted": 0, "flagged": 0}
        data["by_type"] = []

    # ── Bad-file blocklist ──────────────────────────────────────────────────
    data["bad"] = 0
    if _table_exists(cur, "file_catalog", "BAD_FILE"):
        cur.execute("SELECT COUNT(*) FROM file_catalog.BAD_FILE")
        data["bad"] = cur.fetchone()[0] or 0

    # ── Vault ───────────────────────────────────────────────────────────────
    data["vault"] = {"total": 0, "month": 0}
    if _table_exists(cur, "file_catalog", "VAULT_FILE"):
        cur.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN VAULTED_AT >= ? THEN 1 ELSE 0 END)
            FROM file_catalog.VAULT_FILE""", (start_s,))
        vt, vm = cur.fetchone()
        data["vault"] = {"total": vt or 0, "month": vm or 0}

    # ── Mirrors ─────────────────────────────────────────────────────────────
    tables = discover_cat_tables(cur)
    rows_total = rows_month = promoted_total = 0
    for t in tables:
        cur.execute(per_table_sql(t), (start_s,))
        rt, rm, fl, pr = cur.fetchone()
        rt, rm, fl, pr = rt or 0, rm or 0, fl or 0, pr or 0
        rows_total     += rt
        rows_month     += rm
        promoted_total += pr
        data["tables"].append({"name": t, "rows": rt, "rows_month": rm,
                               "files": fl, "promoted": pr,
                               "pending": rt - pr})

    if tables:
        cur.execute(build_files_union_sql(tables), (start_s, today_s))
        ft, fm, fd = cur.fetchone()
        files_total, files_month, files_today = ft or 0, fm or 0, fd or 0
    else:
        files_total = files_month = files_today = 0

    data["activity"] = {
        "files_total": files_total, "files_month": files_month,
        "files_today": files_today, "rows_total": rows_total,
        "rows_month": rows_month, "promoted": promoted_total,
        "pending": rows_total - promoted_total,
    }
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Rendering (pure — unit-testable)
# ─────────────────────────────────────────────────────────────────────────────
def render_report(data, server, database):
    o, a = data["overview"], data["activity"]
    ml = data["month_label"]
    L = []
    L.append("=" * 66)
    L.append("  DataView v3 — Catalog Load Scorecard")
    L.append(f"  {server} / {database}")
    L.append(f"  generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  "
             f"month {ml}")
    L.append("=" * 66)

    L.append("\nLOAD ACTIVITY  (captured into cat_* mirrors)")
    L.append(f"  Files loaded — to date : {_fmt_int(a['files_total'])}")
    L.append(f"               — {ml}   : {_fmt_int(a['files_month'])}")
    L.append(f"               — today   : {_fmt_int(a['files_today'])}")
    L.append(f"  Rows  loaded — to date : {_fmt_int(a['rows_total'])}")
    L.append(f"               — {ml}   : {_fmt_int(a['rows_month'])}")
    L.append(f"  Promoted to dv_*       : {_fmt_int(a['promoted'])}  "
             f"(pending {_fmt_int(a['pending'])})")
    v = data.get("vault", {"total": 0, "month": 0})
    L.append(f"  Files vaulted — to date: {_fmt_int(v['total'])}")
    L.append(f"                — {ml}  : {_fmt_int(v['month'])}")

    if data["has_global"]:
        tot = o["total"]
        L.append("\nCATALOG OVERVIEW  (GLOBAL_FILE_CATALOG)")
        L.append(f"  Total files            : {_fmt_int(tot)}")
        L.append(f"  Cataloged   {_bar(o['cataloged'], tot)} "
                 f"{_fmt_int(o['cataloged'])}")
        L.append(f"  Extracted   {_bar(o['extracted'], tot)} "
                 f"{_fmt_int(o['extracted'])}")
        L.append(f"  Flagged                : {_fmt_int(o['flagged'])}")
        L.append(f"  Bad (blocklist)        : {_fmt_int(data['bad'])}")

        if data["by_type"]:
            L.append("\n  By file type:")
            L.append(f"    {'Type':<14}{'Total':>10}{'Cataloged':>12}")
            L.append(f"    {'-'*14}{'-'*10:>10}{'-'*12:>12}")
            for name, t, c in data["by_type"]:
                L.append(f"    {str(name)[:14]:<14}{_fmt_int(t):>10}"
                         f"{_fmt_int(c):>12}")

    L.append("\nDATA TYPES POPULATED  (one row per cat_* mirror)")
    if data["tables"]:
        L.append(f"  {'Mirror':<26}{'Rows':>10}{'Files':>8}"
                 f"{'  ' + ml:>9}{'Promoted':>10}{'Pending':>9}")
        L.append(f"  {'-'*26}{'-'*10:>10}{'-'*8:>8}{'-'*9:>9}"
                 f"{'-'*10:>10}{'-'*9:>9}")
        for t in data["tables"]:
            short = t["name"].replace("cat_", "")
            L.append(f"  {short[:26]:<26}{_fmt_int(t['rows']):>10}"
                     f"{_fmt_int(t['files']):>8}{_fmt_int(t['rows_month']):>9}"
                     f"{_fmt_int(t['promoted']):>10}{_fmt_int(t['pending']):>9}")
    else:
        L.append("  (no cat_* mirrors found — run build_catalog_mirror.py)")

    L.append("\n" + "=" * 66)
    return "\n".join(L)


def render_markdown(data, server, database):
    o, a = data["overview"], data["activity"]
    ml = data["month_label"]
    M = []
    M.append("# DataView v3 — Catalog Load Scorecard\n")
    M.append(f"**Target:** `{server}` / `{database}`  ")
    M.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    M.append(f"**Month:** {ml}\n")

    M.append("## Load activity\n")
    M.append("| Metric | Value |")
    M.append("|---|---:|")
    M.append(f"| Files loaded — to date | {_fmt_int(a['files_total'])} |")
    M.append(f"| Files loaded — {ml} | {_fmt_int(a['files_month'])} |")
    M.append(f"| Files loaded — today | {_fmt_int(a['files_today'])} |")
    M.append(f"| Rows loaded — to date | {_fmt_int(a['rows_total'])} |")
    M.append(f"| Rows loaded — {ml} | {_fmt_int(a['rows_month'])} |")
    M.append(f"| Promoted to dv_* | {_fmt_int(a['promoted'])} |")
    M.append(f"| Pending promotion | {_fmt_int(a['pending'])} |")
    _v = data.get("vault", {"total": 0, "month": 0})
    M.append(f"| Files vaulted — to date | {_fmt_int(_v['total'])} |")
    M.append(f"| Files vaulted — {ml} | {_fmt_int(_v['month'])} |\n")

    if data["has_global"]:
        M.append("## Catalog overview\n")
        M.append("| Metric | Value |")
        M.append("|---|---:|")
        M.append(f"| Total files | {_fmt_int(o['total'])} |")
        M.append(f"| Cataloged | {_fmt_int(o['cataloged'])} |")
        M.append(f"| Extracted | {_fmt_int(o['extracted'])} |")
        M.append(f"| Flagged | {_fmt_int(o['flagged'])} |")
        M.append(f"| Bad (blocklist) | {_fmt_int(data['bad'])} |\n")
        if data["by_type"]:
            M.append("### By file type\n")
            M.append("| Type | Total | Cataloged |")
            M.append("|---|---:|---:|")
            for name, t, c in data["by_type"]:
                M.append(f"| {name} | {_fmt_int(t)} | {_fmt_int(c)} |")
            M.append("")

    M.append("## Data types populated\n")
    M.append(f"| Mirror | Rows | Files | {ml} | Promoted | Pending |")
    M.append("|---|---:|---:|---:|---:|---:|")
    for t in data["tables"]:
        M.append(f"| {t['name'].replace('cat_','')} | {_fmt_int(t['rows'])} | "
                 f"{_fmt_int(t['files'])} | {_fmt_int(t['rows_month'])} | "
                 f"{_fmt_int(t['promoted'])} | {_fmt_int(t['pending'])} |")
    return "\n".join(M) + "\n"


def _pct(n, total):
    return (100.0 * (n or 0) / total) if total else 0.0


def render_html(data, server, database):
    """Self-contained HTML scorecard (inline CSS, no external assets) with a
    petroleum 'depth-rail' aesthetic. Opens in a browser, prints cleanly,
    drops into an email."""
    import html as _html

    o, a = data["overview"], data["activity"]
    ml = data["month_label"]
    esc = _html.escape
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")

    def card(value, label, sub="", accent="amber"):
        sub_html = f'<div class="kpi-sub">{esc(sub)}</div>' if sub else ""
        return (f'<div class="kpi kpi-{accent}">'
                f'<div class="kpi-val">{value}</div>'
                f'<div class="kpi-lbl">{esc(label)}</div>{sub_html}</div>')

    cards = "".join([
        card(_fmt_int(a["files_total"]), "Files loaded", "to date", "amber"),
        card(_fmt_int(a["files_month"]), f"Files · {ml}", "this month", "teal"),
        card(_fmt_int(a["files_today"]), "Files · today", "", "teal"),
        card(_fmt_int(a["rows_total"]),  "Rows loaded", "to date", "amber"),
        card(_fmt_int(o["cataloged"]),   "Cataloged", "files", "green"),
        card(_fmt_int(data.get("vault", {}).get("total", 0)),
             "Vaulted", "files on disk", "teal"),
        card(_fmt_int(a["promoted"]),    "Promoted", f"{_fmt_int(a['pending'])} pending", "slate"),
    ])

    # overview bars
    bars = ""
    if data["has_global"]:
        tot = o["total"]
        def bar(lbl, n, color):
            p = _pct(n, tot)
            return (f'<div class="bar-row"><div class="bar-lbl">{esc(lbl)}</div>'
                    f'<div class="bar-track"><div class="bar-fill {color}" '
                    f'style="width:{p:.1f}%"></div></div>'
                    f'<div class="bar-num">{_fmt_int(n)} '
                    f'<span class="bar-pct">{p:.1f}%</span></div></div>')
        bars = (f'<div class="ov-total">Total files '
                f'<b>{_fmt_int(tot)}</b></div>'
                + bar("Cataloged", o["cataloged"], "amber")
                + bar("Extracted", o["extracted"], "teal")
                + f'<div class="ov-chips">'
                  f'<span class="chip chip-flag">Flagged {_fmt_int(o["flagged"])}</span>'
                  f'<span class="chip chip-bad">Bad {_fmt_int(data["bad"])}</span>'
                  f'</div>')

    # by-type table
    type_rows = "".join(
        f"<tr><td>{esc(str(name))}</td><td class='num'>{_fmt_int(t)}</td>"
        f"<td class='num'>{_fmt_int(c)}</td>"
        f"<td class='num pct'>{_pct(c, t):.0f}%</td></tr>"
        for name, t, c in data["by_type"]) or \
        "<tr><td colspan='4' class='empty'>No catalog rows.</td></tr>"

    # mirror table (data types populated)
    if data["tables"]:
        max_rows = max((t["rows"] for t in data["tables"]), default=0)
        mirror_rows = ""
        for t in data["tables"]:
            w = _pct(t["rows"], max_rows)
            mirror_rows += (
                f"<tr><td><span class='mname'>{esc(t['name'].replace('cat_',''))}</span>"
                f"<div class='minibar'><div style='width:{w:.1f}%'></div></div></td>"
                f"<td class='num'>{_fmt_int(t['rows'])}</td>"
                f"<td class='num'>{_fmt_int(t['files'])}</td>"
                f"<td class='num'>{_fmt_int(t['rows_month'])}</td>"
                f"<td class='num'>{_fmt_int(t['promoted'])}</td>"
                f"<td class='num pending'>{_fmt_int(t['pending'])}</td></tr>")
    else:
        mirror_rows = ("<tr><td colspan='6' class='empty'>No cat_* mirrors "
                       "found — run build_catalog_mirror.py.</td></tr>")

    overview_block = ""
    if data["has_global"]:
        overview_block = f"""
      <section class="panel">
        <h2>Catalog overview</h2>
        <div class="bars">{bars}</div>
        <table class="tbl">
          <thead><tr><th>File type</th><th class="num">Total</th>
            <th class="num">Cataloged</th><th class="num">%</th></tr></thead>
          <tbody>{type_rows}</tbody>
        </table>
      </section>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Catalog Scorecard · {esc(database)}</title>
<style>
  :root {{
    --ink:#16202e; --muted:#64748b; --line:#e3e8ef; --bg:#eef1f5;
    --navy:#0e1726; --navy2:#16263c; --amber:#e8a23d; --teal:#2bb3a3;
    --green:#4caf7d; --slate:#7c8aa0; --red:#d8694e;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:980px; margin:0 auto; padding:0 20px 48px; }}
  header {{ background:linear-gradient(135deg,var(--navy),var(--navy2));
    color:#fff; padding:26px 0; border-bottom:3px solid var(--amber); }}
  header .wrap {{ padding-top:0; padding-bottom:0; position:relative; }}
  header h1 {{ margin:0; font-size:22px; letter-spacing:.3px;
    padding-left:16px; border-left:4px solid var(--amber); }}
  header .meta {{ margin:8px 0 0 20px; color:#aebaccaa; font-size:13px;
    font-variant-numeric:tabular-nums; }}
  header .meta b {{ color:#dfe7f2; font-weight:600; }}
  .kpis {{ display:flex; flex-wrap:wrap; gap:12px; margin:-26px 0 22px; }}
  .kpi {{ flex:1 1 140px; background:#fff; border:1px solid var(--line);
    border-radius:10px; padding:14px 14px 12px; box-shadow:0 6px 18px #16202e0d;
    border-top:3px solid var(--slate); }}
  .kpi-amber{{border-top-color:var(--amber);}}
  .kpi-teal{{border-top-color:var(--teal);}}
  .kpi-green{{border-top-color:var(--green);}}
  .kpi-slate{{border-top-color:var(--slate);}}
  .kpi-val {{ font-size:25px; font-weight:700; letter-spacing:-.5px;
    font-variant-numeric:tabular-nums; }}
  .kpi-lbl {{ font-size:12px; color:var(--ink); margin-top:3px; font-weight:600; }}
  .kpi-sub {{ font-size:11px; color:var(--muted); margin-top:1px; }}
  .panel {{ background:#fff; border:1px solid var(--line); border-radius:12px;
    padding:18px 20px; margin:18px 0; box-shadow:0 4px 14px #16202e0a; }}
  .panel h2 {{ margin:0 0 14px; font-size:15px; letter-spacing:.2px;
    padding-left:10px; border-left:3px solid var(--amber); }}
  .ov-total {{ font-size:13px; color:var(--muted); margin-bottom:10px; }}
  .ov-total b {{ color:var(--ink); font-size:15px; }}
  .bar-row {{ display:grid; grid-template-columns:90px 1fr 150px;
    align-items:center; gap:10px; margin:7px 0; }}
  .bar-lbl {{ font-size:12px; color:var(--muted); }}
  .bar-track {{ background:#eef2f7; border-radius:6px; height:14px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:6px; }}
  .bar-fill.amber{{background:var(--amber);}}
  .bar-fill.teal{{background:var(--teal);}}
  .bar-num {{ text-align:right; font-size:12px; font-variant-numeric:tabular-nums; }}
  .bar-pct {{ color:var(--muted); margin-left:4px; }}
  .ov-chips {{ margin-top:12px; }}
  .chip {{ display:inline-block; font-size:12px; padding:3px 10px;
    border-radius:20px; margin-right:8px; border:1px solid var(--line); }}
  .chip-flag {{ background:#fff7ed; color:#b4540f; border-color:#f3d9b8; }}
  .chip-bad {{ background:#fdf0ed; color:#a13a23; border-color:#f1cabd; }}
  table.tbl {{ width:100%; border-collapse:collapse; margin-top:12px; }}
  .tbl th {{ text-align:left; font-size:11px; text-transform:uppercase;
    letter-spacing:.6px; color:var(--muted); padding:8px 10px;
    border-bottom:2px solid var(--line); }}
  .tbl td {{ padding:8px 10px; border-bottom:1px solid #f0f3f7; font-size:13px;
    font-variant-numeric:tabular-nums; }}
  .tbl tr:last-child td {{ border-bottom:none; }}
  .tbl .num {{ text-align:right; }}
  .tbl .pct {{ color:var(--muted); }}
  .tbl .pending {{ color:#b4540f; }}
  .tbl .empty {{ color:var(--muted); text-align:center; padding:18px; }}
  .mname {{ font-weight:600; }}
  .minibar {{ height:4px; background:#eef2f7; border-radius:3px; margin-top:5px;
    max-width:240px; }}
  .minibar>div {{ height:100%; background:var(--teal); border-radius:3px; }}
  footer {{ color:var(--muted); font-size:11px; text-align:center; margin-top:8px; }}
</style></head>
<body>
  <header><div class="wrap">
    <h1>DataView v3 · Catalog Load Scorecard</h1>
    <div class="meta"><b>{esc(server)}</b> / <b>{esc(database)}</b>
      &nbsp;·&nbsp; generated {esc(gen)} &nbsp;·&nbsp; month <b>{esc(ml)}</b></div>
  </div></header>
  <div class="wrap">
    <div class="kpis">{cards}</div>
    {overview_block}
    <section class="panel">
      <h2>Data types populated</h2>
      <table class="tbl">
        <thead><tr><th>Mirror</th><th class="num">Rows</th>
          <th class="num">Files</th><th class="num">{esc(ml)}</th>
          <th class="num">Promoted</th><th class="num">Pending</th></tr></thead>
        <tbody>{mirror_rows}</tbody>
      </table>
    </section>
    <footer>cat_* mirror tables captured by the document pipeline ·
      promote with <code>promote_catalog.py</code></footer>
  </div>
</body></html>"""


def write_csv(data, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mirror", "rows", "files",
                    f"rows_{data['month_label']}", "promoted", "pending"])
        for t in data["tables"]:
            w.writerow([t["name"], t["rows"], t["files"],
                        t["rows_month"], t["promoted"], t["pending"]])


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server",   default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--month", default=None,
                    help="month for the 'this month' columns, YYYY-MM "
                         "(default: current month, UTC)")
    ap.add_argument("--out", default=None,
                    help="also write the report as Markdown to this path")
    ap.add_argument("--html", default=None,
                    help="write a styled, self-contained HTML report to this path")
    ap.add_argument("--csv", default=None,
                    help="write the data-types table as CSV to this path")
    a = ap.parse_args()

    if pyodbc is None:
        print("pyodbc is not installed (pip install pyodbc).", file=sys.stderr)
        return 2

    try:
        con = connect(a.server, a.database)
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        return 2
    cur = con.cursor()

    try:
        data = gather(cur, a.month)
    except Exception as e:
        print(f"Gather failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()

    print(render_report(data, a.server, a.database))

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(render_markdown(data, a.server, a.database))
        print(f"\n-- Markdown written to {a.out}")
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(render_html(data, a.server, a.database))
        print(f"-- HTML written to {a.html}")
    if a.csv:
        write_csv(data, a.csv)
        print(f"-- CSV written to {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
