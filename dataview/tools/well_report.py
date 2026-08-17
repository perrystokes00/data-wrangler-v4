"""
well_report.py — everything the documents told us about one well.

    python -m dataview.tools.well_report --server localhost\\SQLEXPRESS \\
        --database DataView_Demo --uwi 42999000010000
    python -m dataview.tools.well_report ... --uwi 4299900001 --all --open

Produces a scout-ticket-style HTML page: the well header, then a section per
kind of data, then the source documents it all came from — with a link to
each document that OPENS, because the page is a local file and file:// links
work from a local file even though a browser blocks them from an http page.

WHY IT INTROSPECTS RATHER THAN NAMING TABLES
--------------------------------------------
The list of dv_* tables holding well data changes as the model grows, and a
report that names them goes quietly stale — it keeps working while showing
less, which is the worst failure mode for something people trust. So it asks
the database which tables have a `uwi` column and reports on all of them.
A table added next month appears with no code change.

WHAT "FROM THE FILE CATALOG" MEANS
----------------------------------
Rows derived from documents carry an `inventory_id`; bulk-loaded rows do
not. That is the only reliable discriminator — promote relabels `source` to
'CATALOG' on the way up, so source cannot tell you where a row came from.
Default is document-derived rows only; --all includes everything and marks
the rows that have no document behind them.
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from collections import OrderedDict

# Sections in the order a scout ticket reads: who and where, then the hole,
# then what was found, then what it made. Tables not named here still
# appear, after these, in alphabetical order — the introspection is the
# source of truth and this list only sets a sensible order.
SECTION_ORDER = [
    "dv_well",
    "dv_well_dir_srvy_hdr", "dv_well_dir_srvy_sta",
    "dv_well_casing", "dv_well_perforation",
    "dv_well_formation_top", "dv_well_petro_zone", "dv_well_petro_interp",
    "dv_well_core", "dv_well_dst", "dv_well_log", "dv_well_log_curve",
    "dv_well_completion", "dv_well_stimulation",
    "dv_prod_entity", "dv_prod_volume",
]

SECTION_TITLE = {
    "dv_well": "Well header",
    "dv_well_dir_srvy_hdr": "Directional surveys",
    "dv_well_dir_srvy_sta": "Survey stations",
    "dv_well_casing": "Casing",
    "dv_well_perforation": "Perforations",
    "dv_well_formation_top": "Formation tops",
    "dv_well_petro_zone": "Petrophysical zones",
    "dv_well_petro_interp": "Petrophysical interpretation",
    "dv_well_core": "Cores",
    "dv_well_dst": "Well tests / DST",
    "dv_well_log": "Logs",
    "dv_well_log_curve": "Log curves",
    "dv_well_completion": "Completion",
    "dv_well_stimulation": "Stimulation",
    "dv_prod_entity": "Production entity",
    "dv_prod_volume": "Production",
}

# Never shown as data: audit columns and the provenance key, which the
# report presents separately and more usefully as a document name.
HIDE = {"row_created_by", "row_created_date", "row_changed_by",
        "row_changed_date", "active_ind", "inventory_id", "source_path",
        "promoted", "promoted_at", "captured_at", "cat_row_id"}


# ───────────────────────────── data ─────────────────────────────────────
def well_tables(cx, schema="dataview", prefix="dv_"):
    """Every table in `schema` with a uwi column — asked, not assumed."""
    from sqlalchemy import text
    rows = cx.execute(text(
        "SELECT c.TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS c "
        "JOIN INFORMATION_SCHEMA.TABLES t "
        "  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
        "WHERE c.TABLE_SCHEMA = :s AND LOWER(c.COLUMN_NAME) = 'uwi' "
        "  AND t.TABLE_TYPE = 'BASE TABLE'"), {"s": schema}).fetchall()
    names = [r[0] for r in rows
             if r[0].lower().startswith(prefix)
             and not r[0].lower().startswith(prefix + "r_")]
    order = {n: i for i, n in enumerate(SECTION_ORDER)}
    return sorted(names, key=lambda n: (order.get(n.lower(), 999), n.lower()))


# SQL Server CLR types pyodbc cannot return as-is: asking for one raises
# "ODBC SQL type -151 is not yet supported". They are not skippable —
# whether a well has a computed path is exactly the kind of thing a scout
# ticket should say — so each is converted to a short readable summary in
# the SELECT rather than dropped.
UDT_TYPES = {"geography", "geometry", "hierarchyid"}


def columns_of(cx, schema, table):
    """[(name, data_type)] in ordinal order."""
    from sqlalchemy import text
    return [(r[0], (r[1] or "").lower()) for r in cx.execute(text(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
        "ORDER BY ORDINAL_POSITION"), {"s": schema, "t": table})]


def _select_expr(col, dtype):
    """How to ask for one column so pyodbc can carry the answer back."""
    if dtype in ("geography", "geometry"):
        # A LINESTRING of 300 stations as WKT is unreadable in a table and
        # megabytes on the page. The useful facts are that it EXISTS and
        # how detailed it is.
        return (f"CASE WHEN [{col}] IS NULL THEN NULL ELSE "
                f"CONCAT([{col}].STGeometryType(), ' (',"
                f" [{col}].STNumPoints(), ' points)') END AS [{col}]")
    if dtype == "hierarchyid":
        return f"CAST([{col}] AS nvarchar(200)) AS [{col}]"
    return f"[{col}]"


def fetch_section(cx, schema, table, uwi, docs_only=True, limit=500):
    """Rows for one well from one table, plus the columns worth showing.

    A column that is NULL in every row of this well is dropped: a scout
    ticket shows what is KNOWN, and forty empty columns hide the six that
    are filled. The count is reported so nothing looks lost.
    """
    from sqlalchemy import text
    cols = columns_of(cx, schema, table)
    has_inv = any(c.lower() == "inventory_id" for c, _d in cols)
    show = [(c, d) for c, d in cols if c.lower() not in HIDE]
    if not show:
        return [], [], 0
    sel = ", ".join(_select_expr(c, d) for c, d in show)
    if has_inv:
        sel += ", [inventory_id]"
    where = "WHERE uwi LIKE :u"
    if docs_only and has_inv:
        where += " AND inventory_id IS NOT NULL"
    sql = f"SELECT TOP {int(limit)} {sel} FROM {schema}.{table} {where}"
    try:
        res = cx.execute(text(sql), {"u": f"{uwi}%"})
        keys = list(res.keys())
        rows = [dict(zip(keys, r)) for r in res.fetchall()]
    except Exception as e:
        return [], [], f"query failed: {str(e)[:160]}"
    if not rows:
        return [], [], 0
    # drop columns empty for THIS well
    live = [c for c, _d in show
            if any(r.get(c) not in (None, "") for r in rows)]
    dropped = len(show) - len(live)
    return rows, live, dropped


def source_documents(cx, inventory_ids):
    """inventory_id -> {file_name, file_path, modified} from the catalog."""
    from sqlalchemy import text
    out = {}
    ids = [i for i in inventory_ids if i]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        marks = ", ".join(f":p{j}" for j in range(len(chunk)))
        params = {f"p{j}": v for j, v in enumerate(chunk)}
        try:
            res = cx.execute(text(
                "SELECT INVENTORY_ID, FILE_NAME, FILE_PATH, MODIFIED_DATE "
                f"FROM file_catalog.GLOBAL_FILE_CATALOG "
                f"WHERE INVENTORY_ID IN ({marks})"), params)
            for iid, name, path, mod in res.fetchall():
                out[iid] = {"name": name, "path": path, "modified": mod}
        except Exception:
            break
    return out


# ───────────────────────────── render ───────────────────────────────────
CSS = """
:root{--ink:#1a1a1a;--mut:#6b6b6b;--line:#d8d3c8;--bg:#faf8f4;--hd:#2c3e50;
      --acc:#b8860b}
*{box-sizing:border-box}
body{font:14px/1.55 "Segoe UI",system-ui,sans-serif;color:var(--ink);
     background:var(--bg);margin:0;padding:28px 34px;max-width:1180px}
h1{font-size:22px;margin:0 0 2px;color:var(--hd);letter-spacing:.3px}
.sub{color:var(--mut);margin:0 0 22px;font-size:13px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.9px;
   color:var(--hd);margin:26px 0 8px;padding-bottom:5px;
   border-bottom:2px solid var(--acc)}
table{border-collapse:collapse;width:100%;margin:0 0 6px;font-size:13px;
      background:#fff}
th{background:#f0ece3;text-align:left;font-weight:600;color:var(--hd);
   padding:5px 8px;border:1px solid var(--line);white-space:nowrap}
td{padding:4px 8px;border:1px solid var(--line);vertical-align:top}
tr:nth-child(even) td{background:#fcfbf8}
.num{text-align:right;font-variant-numeric:tabular-nums}
.note{color:var(--mut);font-size:12px;margin:0 0 14px}
.hdr-grid{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:2px 14px;
          background:#fff;border:1px solid var(--line);padding:12px 14px}
.hdr-grid dt{color:var(--mut);font-size:12px}
.hdr-grid dd{margin:0;font-weight:600}
.src{font-size:12px;color:var(--mut)}
.src a{color:var(--acc);text-decoration:none}
.src a:hover{text-decoration:underline}
.docs li{margin-bottom:5px}
.warn{background:#fff6e5;border-left:3px solid var(--acc);padding:8px 12px;
      margin:10px 0;font-size:13px}
/* ORIGIN COLOURS. Deliberately pale — the data must stay readable and the
   colour is a second signal, not the message. Each also carries a left
   border so the distinction survives greyscale printing and colour-blind
   readers, who would otherwise see three identical rows. */
tr.doc    td{background:#f2f8f2}
tr.doc    td:first-child{border-left:3px solid #4a8f4a}
tr.bulk   td{background:#f2f5fa}
tr.bulk   td:first-child{border-left:3px solid #4a6f9f}
tr.orphan td{background:#fdf3ef}
tr.orphan td:first-child{border-left:3px solid #b5562e}
tr:nth-child(even) td{background:inherit}
.empty{color:var(--mut);font-style:italic;background:#f7f6f3;
       border:1px dashed var(--line);padding:9px 12px;margin:0 0 14px;
       font-size:13px}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin:0 0 18px;font-size:12px;
        color:var(--mut)}
.legend span{display:flex;align-items:center;gap:6px}
.sw{width:22px;height:12px;display:inline-block;border:1px solid var(--line)}
.sw.doc{background:#f2f8f2;border-left:3px solid #4a8f4a}
.sw.bulk{background:#f2f5fa;border-left:3px solid #4a6f9f}
.sw.orphan{background:#fdf3ef;border-left:3px solid #b5562e}
.sw.none{background:#f7f6f3;border:1px dashed var(--line)}
.cover{background:#fff;border:1px solid var(--line);padding:10px 14px;
       margin:0 0 18px;font-size:13px}
.cover b{color:var(--hd)}
footer{margin-top:34px;padding-top:12px;border-top:1px solid var(--line);
       color:var(--mut);font-size:12px}
"""


# WHAT COUNTS AS "READ FROM A DOCUMENT"
# -------------------------------------
# NOT "has an inventory_id". That WAS the test, and it stopped being true
# on 3 August when the bulk loader began stamping ids too and registering
# its CSVs in the file catalog. After that every row has an id that
# resolves, and anything keyed on it calls a spreadsheet load "from a
# catalogued document".
#
# The durable distinction is the KIND OF FILE the id points at. A PDF or
# a Word document had to be READ — a recogniser found the table, matched
# a shape, mapped the columns. A CSV or a LAS was PARSED; the structure
# was already there. That difference is the product claim, and it does
# not move when a pipeline is relabelled.
#
# Defined HERE and imported by scorecard.py so the extension list exists
# once. Two copies would drift, and the drift would be invisible.
DOC_EXT = {".pdf", ".docx", ".doc", ".html", ".htm", ".rtf", ".pptx",
           ".msg", ".odt"}
DATA_EXT = {".csv", ".txt", ".tsv", ".dat", ".prn", ".xlsx", ".xls", ".xlsm",
            ".las", ".dlis", ".lis", ".xml", ".segy", ".sgy", ".json",
            ".shp", ".zip"}


def file_class(file_name):
    """'document' | 'data file' | 'other' — from the extension."""
    ext = os.path.splitext(str(file_name or ""))[1].lower()
    if ext in DOC_EXT:
        return "document"
    if ext in DATA_EXT:
        return "data file"
    return "other"


def origin(row, docs):
    """Where did this row come from? Three answers, three colours.

    A scout ticket that shows only what we HAVE is a summary. Showing every
    section a ticket would have — including the empty ones — and colouring
    each row by its provenance turns the same page into a COVERAGE MAP:
    what we know, where it came from, and what is simply absent.
    """
    iid = row.get("inventory_id")
    if not iid:
        return "bulk"          # no source file recorded at all
    d = docs.get(iid)
    if not d:
        return "orphan"        # stamped, but the id resolves to nothing
    return ("doc" if file_class(d.get("name")) == "document"
            else "bulk")       # a data file: parsed, not read


ORIGIN_LABEL = {
    "doc": "read from a document",
    "bulk": "parsed from a data file",
    "orphan": "stamped, but no catalog entry resolves",
}


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.4g}"
    if isinstance(v, int):
        return f"{v:,}"
    s = str(v)
    return s[:10] if len(s) == 19 and s[4] == "-" and s[10] == " " else s


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def render(uwi, sections, docs, docs_only=True, generated=None):
    """Sections is [(table, rows, cols, dropped)]; docs is the id->file map."""
    import datetime as _dt
    generated = generated or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    e = html.escape
    head = next((r for t, r, c, d in sections
                 if t.lower() == "dv_well" and r), [None])[0]
    title = (head or {}).get("well_name") or uwi

    out = [f"<!doctype html><meta charset='utf-8'>"
           f"<title>Well report — {e(str(title))}</title><style>{CSS}</style>",
           f"<h1>{e(str(title))}</h1>",
           f"<p class='sub'>UWI {e(str(uwi))} · assembled "
           f"{'from document-derived rows only' if docs_only else 'from all rows'}"
           f" · {generated}</p>"]

    if head:
        out.append("<h2>Well header</h2><dl class='hdr-grid'>")
        for k, v in head.items():
            if k == "inventory_id" or v in (None, ""):
                continue
            out.append(f"<dt>{e(k)}</dt><dd>{e(_fmt(v))}</dd>")
        out.append("</dl>")
        d = docs.get(head.get("inventory_id"))
        out.append(f"<p class='src'>{e(ORIGIN_LABEL[origin(head, docs)])}"
                   + (f" — {e(str(d['name']))}" if d else "") + "</p>")

    # LEGEND FIRST — a colour nobody can decode is decoration.
    out.append("<div class='legend'>"
               "<span><i class='sw doc'></i>read from a document</span>"
               "<span><i class='sw bulk'></i>parsed from a data file</span>"
               "<span><i class='sw orphan'></i>stamped, catalog entry "
               "missing</span>"
               "<span><i class='sw none'></i>nothing recorded</span></div>")

    n_sec = sum(1 for t, r, c, d in sections if t.lower() != "dv_well")
    n_full = sum(1 for t, r, c, d in sections
                 if t.lower() != "dv_well" and r)
    all_rows = [r for t, rs, c, d in sections for r in rs]
    n_doc = sum(1 for r in all_rows if origin(r, docs) == "doc")
    if all_rows:
        out.append(
            f"<div class='cover'><b>{n_full} of {n_sec}</b> sections hold "
            f"data · <b>{len(all_rows)}</b> row(s), <b>{n_doc}</b> "
            f"({100.0 * n_doc / len(all_rows):.0f}%) read from a "
            f"document · <b>{len(docs)}</b> source file(s)</div>")

    for table, rows, cols, dropped in sections:
        if table.lower() == "dv_well":
            continue
        name = SECTION_TITLE.get(table.lower(), table)
        out.append(f"<h2>{e(name)}</h2>")
        # A FAILED SECTION MUST STILL APPEAR, and so must an EMPTY one:
        # "this well has no DST", "we could not ask about DST" and "we never
        # looked" are three different statements, and a ticket that shows
        # only populated sections silently collapses them into one.
        if isinstance(dropped, str):
            out.append(f"<div class='warn'>{e(dropped)}</div>")
            continue
        if not rows:
            out.append("<p class='empty'>nothing recorded for this well</p>")
            continue
        srcs = {r.get("inventory_id") for r in rows}
        one_src = len(srcs) == 1
        out.append("<table><tr>")
        for c in cols:
            out.append(f"<th>{e(c)}</th>")
        if not one_src:
            out.append("<th>source</th>")
        out.append("</tr>")
        for r in rows:
            out.append(f"<tr class='{origin(r, docs)}'>")
            for c in cols:
                v = r.get(c)
                out.append(f"<td class='{'num' if _isnum(v) else ''}'>"
                           f"{e(_fmt(v))}</td>")
            if not one_src:
                d = docs.get(r.get("inventory_id"))
                out.append(f"<td class='src'>{e(str(d['name'])) if d else ''}"
                           f"</td>")
            out.append("</tr>")
        out.append("</table>")
        bits = [f"{len(rows)} row(s)"]
        kinds = {}
        for r in rows:
            k = origin(r, docs)
            kinds[k] = kinds.get(k, 0) + 1
        if len(kinds) > 1:
            bits.append(" · ".join(f"{n} {ORIGIN_LABEL[k]}"
                                   for k, n in sorted(kinds.items())))
        if dropped:
            bits.append(f"{dropped} column(s) empty for this well, hidden")
        if one_src:
            d = docs.get(next(iter(srcs)))
            if d:
                bits.append(f"source: {d['name']}")
        out.append(f"<p class='note'>{e(' · '.join(bits))}</p>")

    # THE PROVENANCE SECTION IS THE POINT. A scout ticket you cannot trace
    # is a summary; one that names its documents is evidence.
    if docs:
        out.append("<h2>Source documents</h2><ul class='docs'>")
        for iid, d in sorted(docs.items(), key=lambda kv: str(kv[1]["name"])):
            path = (d.get("path") or "").replace("\\", "/")
            link = (f"<a href='file:///{e(path)}'>{e(str(d['name']))}</a>"
                    if path else e(str(d["name"])))
            mod = f" · modified {_fmt(d.get('modified'))}" if d.get("modified") else ""
            out.append(f"<li>{link}<span class='src'>{e(mod)} · "
                       f"{e(str(iid)[:12])}…</span></li>")
        out.append("</ul>")
        out.append("<p class='note'>Links open the original file. They work "
                   "because this page is itself a local file; the same link "
                   "inside the web app would be blocked by the browser.</p>")
    else:
        out.append("<div class='warn'>No source documents resolved. Either "
                   "these rows carry no inventory_id, or the ids are not in "
                   "file_catalog.GLOBAL_FILE_CATALOG.</div>")

    out.append(f"<footer>Data Wrangler · generated {e(generated)} · "
               f"rows shown are those in the database, not the documents — "
               f"a value absent here was either not extracted or not "
               f"promoted.</footer>")
    return "\n".join(out)


# ───────────────────────────── driver ───────────────────────────────────
def connect(server, database):
    """An engine, built the way the loader builds one.

    Deliberately reuses bulk_dir_loader.make_engine rather than rolling
    its own: that function picks the ODBC driver from what is actually
    installed and adds Encrypt=no / TrustServerCertificate for Driver 18,
    which a hand-written connection string gets wrong on exactly the
    machines where it matters. The fallback below exists only so this
    tool still runs if that import fails, and builds the same string.
    """
    try:
        from dataview.import_data.bulk_dir_loader import make_engine
        return make_engine(server, database)
    except Exception:
        pass
    import urllib.parse
    from sqlalchemy import create_engine
    try:
        import pyodbc
        drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        drv = (next((d for d in drivers if "18" in d), None)
               or next((d for d in drivers if "17" in d), None)
               or (drivers[0] if drivers else "ODBC Driver 17 for SQL Server"))
    except Exception:
        drv = "ODBC Driver 17 for SQL Server"
    cs = (f"DRIVER={{{drv}}};SERVER={server};DATABASE={database};"
          f"Trusted_Connection=yes;")
    if "18" in drv:
        cs += "Encrypt=no;TrustServerCertificate=yes;"
    return create_engine("mssql+pyodbc:///?odbc_connect="
                         + urllib.parse.quote_plus(cs), pool_pre_ping=False)


def build(server, database, uwi, schema="dataview", docs_only=False,
          limit=500, log=print):
    eng = connect(server, database)
    sections, ids = [], set()
    with eng.connect() as cx:
        tables = well_tables(cx, schema)
        log(f"{len(tables)} table(s) with a uwi column")
        for t in tables:
            rows, cols, dropped = fetch_section(cx, schema, t, uwi,
                                                docs_only, limit)
            # EVERY table becomes a section, populated or not — the empty
            # ones are the report's most useful statement.
            sections.append((t, rows, cols, dropped))
            ids |= {r.get("inventory_id") for r in rows}
            log(f"  {t}: {len(rows)} row(s)"
                + ("  [" + dropped + "]" if isinstance(dropped, str) else ""))
        docs = source_documents(cx, ids)
    return render(uwi, sections, docs, docs_only), len(sections), len(docs)


def main(argv=None):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    ap = argparse.ArgumentParser(description="Scout-ticket report for a well.")
    ap.add_argument("--server", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--uwi", required=True,
                    help="full UWI or a leading fragment")
    ap.add_argument("--schema", default="dataview")
    ap.add_argument("--docs-only", action="store_true", dest="docs_only",
                    help="show ONLY rows read from a document "
                         "(default: show everything, colour-coded)")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", action="store_true", dest="open_it")
    a = ap.parse_args(argv)

    doc, n_sec, n_doc = build(a.server, a.database, a.uwi, a.schema,
                              docs_only=a.docs_only, limit=a.limit)
    out = a.out or f"well_{a.uwi}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"\n{n_sec} section(s), {n_doc} source document(s) -> {out}")
    if a.open_it:
        try:
            os.startfile(os.path.abspath(out))     # noqa: S606  (Windows)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
