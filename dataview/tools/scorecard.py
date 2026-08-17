"""
scorecard.py — what is in this database, and where it came from.

    python -m dataview.tools.scorecard --server localhost\\SQLEXPRESS \\
        --database DataView_Demo --open

The HTML form of scorecard.sql: rows per table, the bulk-versus-catalog
split, the tables that are empty, and the documents that actually fed the
database.

WHY THE SPLIT IS THE POINT
--------------------------
Bulk-loaded rows came from a data file somebody handed over. Catalogued
rows were READ OUT OF A DOCUMENT by the recogniser. Those are different
claims — "the vendor gave us this" versus "we extracted this, and here is
the page it came from" — and only the second is the hard thing. A single
row count cannot tell them apart; this report exists to.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import os
import sys

# Rows whose provenance CANNOT be established, and why that is a third
# answer rather than a zero.
UNSCORED = "no inventory_id column — provenance cannot be recorded here"

# The document-versus-data-file definition lives in well_report and is
# imported, not copied: two extension lists would drift apart and the
# drift would show up as a percentage nobody could explain.
from dataview.tools.well_report import (          # noqa: E402
    DOC_EXT, DATA_EXT, file_class)


def connect(server, database):
    """The loader's own engine builder; see well_report.connect."""
    try:
        from dataview.tools.well_report import connect as _c
        return _c(server, database)
    except Exception:
        pass
    from dataview.import_data.bulk_dir_loader import make_engine
    return make_engine(server, database)


# ───────────────────────────── data ─────────────────────────────────────
def scan(cx, schema="dataview", prefix="dv[_]%"):
    """[{table, kind, rows, catalog, bulk, orphan, documents}] + the ids."""
    from sqlalchemy import text
    tabs = cx.execute(text(
        "SELECT t.TABLE_NAME, "
        "  CASE WHEN EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS i "
        "                    WHERE i.TABLE_SCHEMA = t.TABLE_SCHEMA "
        "                      AND i.TABLE_NAME = t.TABLE_NAME "
        "                      AND LOWER(i.COLUMN_NAME) = 'inventory_id') "
        "       THEN 1 ELSE 0 END "
        "FROM INFORMATION_SCHEMA.TABLES t "
        "WHERE t.TABLE_SCHEMA = :s AND t.TABLE_TYPE = 'BASE TABLE' "
        "  AND t.TABLE_NAME LIKE :p "
        "ORDER BY t.TABLE_NAME"), {"s": schema, "p": prefix}).fetchall()

    rows, ids = [], set()
    for name, has_inv in tabs:
        kind = ("reference" if name.lower().startswith("dv_r_")
                else "data" if has_inv else "unscored")
        rec = {"table": name, "kind": kind, "rows": 0, "catalog": 0,
               "bulk": 0, "orphan": 0, "documents": 0, "error": None}
        try:
            if has_inv:
                # Group by the source file's extension and classify in
                # Python: one query per table either way, and the
                # extension list stays in ONE place rather than being
                # duplicated as a T-SQL CASE that drifts from it.
                res = cx.execute(text(
                    f"SELECT g.FILE_NAME, "
                    f"       has_id = CASE WHEN t.inventory_id IS NULL "
                    f"                     THEN 0 ELSE 1 END, "
                    f"       n = COUNT_BIG(*) "
                    f"FROM {schema}.[{name}] t "
                    f"LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g "
                    f"       ON g.INVENTORY_ID = t.inventory_id "
                    f"GROUP BY g.FILE_NAME, "
                    f"         CASE WHEN t.inventory_id IS NULL "
                    f"              THEN 0 ELSE 1 END")).fetchall()
                for fname, has_id, n in res:
                    n = int(n or 0)
                    rec["rows"] += n
                    if not has_id:
                        rec["bulk"] += n            # no source file at all
                    elif fname is None:
                        rec["orphan"] += n          # stamped, catalog empty
                    elif file_class(fname) == "document":
                        rec["catalog"] += n
                    else:
                        rec["bulk"] += n            # a data file, parsed
                rec["documents"] = len({f for f, h, _n in res
                                        if h and f and
                                        file_class(f) == "document"})
                if rec["catalog"]:
                    ids |= {x[0] for x in cx.execute(text(
                        f"SELECT DISTINCT t.inventory_id "
                        f"FROM {schema}.[{name}] t "
                        f"JOIN file_catalog.GLOBAL_FILE_CATALOG g "
                        f"  ON g.INVENTORY_ID = t.inventory_id "
                        f"WHERE t.inventory_id IS NOT NULL")).fetchall()}
            else:
                r = cx.execute(text(
                    f"SELECT COUNT_BIG(*) FROM {schema}.[{name}]")).fetchone()
                rec["rows"] = int(r[0] or 0)
        except Exception as e:
            rec["error"] = str(e)[:160]
        rows.append(rec)
    return rows, ids


def documents(cx, ids):
    from sqlalchemy import text
    out, idl = [], [i for i in ids if i]
    for i in range(0, len(idl), 200):
        chunk = idl[i:i + 200]
        marks = ", ".join(f":p{j}" for j in range(len(chunk)))
        prm = {f"p{j}": v for j, v in enumerate(chunk)}
        try:
            out += cx.execute(text(
                "SELECT INVENTORY_ID, FILE_NAME, FILE_PATH, MODIFIED_DATE "
                "FROM file_catalog.GLOBAL_FILE_CATALOG "
                f"WHERE INVENTORY_ID IN ({marks})"), prm).fetchall()
        except Exception:
            break
    return out


def crawled(cx):
    from sqlalchemy import text
    try:
        return int(cx.execute(text(
            "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG"
        )).fetchone()[0] or 0)
    except Exception:
        return 0


# ───────────────────────────── render ───────────────────────────────────
CSS = """
:root{--ink:#16202b;--mut:#6b7684;--line:#dcd8cf;--bg:#faf9f6;--hd:#22303f;
      --doc:#3f8f5a;--bulk:#4a6f9f;--orph:#b5562e;--acc:#b8860b}
*{box-sizing:border-box}
body{font:14px/1.55 "Segoe UI",system-ui,sans-serif;color:var(--ink);
     background:var(--bg);margin:0;padding:30px 36px;max-width:1120px}
h1{font-size:23px;margin:0 0 3px;color:var(--hd)}
.sub{color:var(--mut);margin:0 0 24px;font-size:13px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--hd);
   margin:30px 0 10px;padding-bottom:5px;border-bottom:2px solid var(--acc)}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 6px}
.card{background:#fff;border:1px solid var(--line);border-radius:5px;
      padding:12px 16px;min-width:140px;flex:1}
.card .n{font-size:25px;font-weight:600;color:var(--hd);
         font-variant-numeric:tabular-nums}
.card .l{font-size:11px;color:var(--mut);text-transform:uppercase;
         letter-spacing:.6px;margin-top:2px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th{background:#f1ede4;text-align:left;padding:6px 9px;border:1px solid var(--line);
   color:var(--hd);font-weight:600;white-space:nowrap}
td{padding:5px 9px;border:1px solid var(--line)}
.num{text-align:right;font-variant-numeric:tabular-nums}
/* The proportion bar is the whole report in one glance. Pure CSS: a
   printed page and a browser must show the same thing. */
.bar{display:flex;height:11px;border:1px solid var(--line);min-width:120px}
.bar i{display:block;height:100%}
.bar .d{background:var(--doc)}
.bar .b{background:var(--bulk)}
.bar .o{background:var(--orph)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--mut);margin:8px 0 0}
.legend span{display:flex;align-items:center;gap:5px}
.sw{width:20px;height:10px;display:inline-block;border:1px solid var(--line)}
.note{color:var(--mut);font-size:12px;margin:7px 0 0}
.empty{color:var(--mut);font-size:13px;background:#f6f4f0;
       border:1px dashed var(--line);padding:9px 12px}
.warn{background:#fff6e5;border-left:3px solid var(--acc);padding:8px 12px;
      margin:8px 0;font-size:13px}
a{color:var(--acc)}
footer{margin-top:32px;padding-top:12px;border-top:1px solid var(--line);
       color:var(--mut);font-size:12px}
"""


def _bar(rec):
    t = rec["rows"] or 1
    d = 100.0 * rec["catalog"] / t
    b = 100.0 * rec["bulk"] / t
    o = 100.0 * rec["orphan"] / t
    return (f"<div class='bar'><i class='d' style='width:{d:.1f}%'></i>"
            f"<i class='b' style='width:{b:.1f}%'></i>"
            f"<i class='o' style='width:{o:.1f}%'></i></div>")


def render(recs, docs, n_crawled=0, database="", generated=None):
    e = html.escape
    generated = generated or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    live = [r for r in recs if r["rows"] > 0]
    empty = [r for r in recs if r["rows"] == 0 and not r["error"]]
    broken = [r for r in recs if r["error"]]
    scored = [r for r in live if r["kind"] != "unscored"]

    n_rows = sum(r["rows"] for r in live)
    n_doc = sum(r["catalog"] for r in live)
    n_bulk = sum(r["bulk"] for r in live)
    n_orph = sum(r["orphan"] for r in live)
    # THE DENOMINATOR IS SCORED ROWS ONLY. Counting tables that cannot
    # carry provenance would push the figure down for a reason unrelated
    # to provenance, and a number that moves when an unrelated table
    # grows is not a measure.
    scored_rows = sum(r["rows"] for r in scored)
    pct = (100.0 * n_doc / scored_rows) if scored_rows else 0.0

    o = [f"<!doctype html><meta charset='utf-8'><title>Database scorecard"
         f"{' — ' + e(database) if database else ''}</title><style>{CSS}</style>",
         f"<h1>Database scorecard</h1>",
         f"<p class='sub'>{e(database)} &middot; {generated}</p>",
         "<div class='cards'>",
         f"<div class='card'><div class='n'>{len(live)}<span style='font-size:15px;"
         f"color:#6b7684'> / {len(recs)}</span></div>"
         f"<div class='l'>tables with data</div></div>",
         f"<div class='card'><div class='n'>{n_rows:,}</div>"
         f"<div class='l'>rows</div></div>",
         f"<div class='card'><div class='n'>{pct:.0f}%</div>"
         f"<div class='l'>read from documents</div></div>",
         f"<div class='card'><div class='n'>{len(docs)}"
         f"<span style='font-size:15px;color:#6b7684'> / {n_crawled or '?'}</span>"
         f"</div><div class='l'>documents read / files crawled</div></div>",
         "</div>",
         "<div class='legend'>"
         "<span><i class='sw' style='background:#3f8f5a'></i>read from a document</span>"
         "<span><i class='sw' style='background:#4a6f9f'></i>parsed from a data file</span>"
         "<span><i class='sw' style='background:#b5562e'></i>orphaned id</span>"
         "</div>"]

    o.append("<h2>Tables with data</h2><table><tr><th>table</th><th>kind</th>"
             "<th class='num'>rows</th><th>split</th><th class='num'>read</th>"
             "<th class='num'>parsed</th><th class='num'>orphan</th>"
             "<th class='num'>%&nbsp;read</th><th class='num'>docs</th></tr>")
    for r in sorted(live, key=lambda x: -x["rows"]):
        p = ("&mdash;" if r["kind"] == "unscored"
             else f"{100.0 * r['catalog'] / r['rows']:.0f}%")
        o.append(
            f"<tr><td>{e(r['table'])}</td><td>{e(r['kind'])}</td>"
            f"<td class='num'>{r['rows']:,}</td><td>"
            + ("<span class='note'>not scored</span>" if r["kind"] == "unscored"
               else _bar(r)) +
            f"</td><td class='num'>{r['catalog']:,}</td>"
            f"<td class='num'>{r['bulk']:,}</td>"
            f"<td class='num'>{r['orphan']:,}</td>"
            f"<td class='num'>{p}</td>"
            f"<td class='num'>{r['documents'] or ''}</td></tr>")
    o.append("</table>")
    n_unscored = sum(r["rows"] for r in live if r["kind"] == "unscored")
    if n_unscored:
        o.append(f"<p class='note'>{n_unscored:,} row(s) sit in tables with no "
                 f"inventory_id column and are excluded from the percentage — "
                 f"they cannot carry provenance, and scoring them 0% would say "
                 f"something untrue about them.</p>")

    if broken:
        o.append("<h2>Could not be counted</h2>")
        for r in broken:
            o.append(f"<div class='warn'>{e(r['table'])} &mdash; "
                     f"{e(r['error'])}</div>")

    # EMPTY TABLES ARE A FINDING. A model with sixty tables and data in
    # nine reports the same row count as one with data in fifty.
    o.append(f"<h2>Empty tables &mdash; {len(empty)}</h2>")
    if empty:
        o.append("<div class='empty'>"
                 + " &middot; ".join(e(r["table"]) for r in empty) + "</div>")
    else:
        o.append("<div class='empty'>none &mdash; every table holds data</div>")

    o.append(f"<h2>Documents that fed the database &mdash; {len(docs)}</h2>")
    if docs:
        o.append("<table><tr><th>document</th><th>modified</th><th>path</th></tr>")
        for _iid, name, path, mod in sorted(docs, key=lambda d: str(d[1] or "")):
            href = str(path or "").replace("\\", "/")
            link = (f"<a href='file:///{e(href)}'>{e(str(name))}</a>"
                    if href else e(str(name or "")))
            o.append(f"<tr><td>{link}</td><td>{e(str(mod)[:10] if mod else '')}"
                     f"</td><td class='note'>{e(str(path or ''))}</td></tr>")
        o.append("</table>")
        if n_crawled and n_crawled > len(docs):
            o.append(f"<p class='note'>{n_crawled - len(docs):,} more file(s) "
                     f"are in the catalog but have not contributed a row. "
                     f"Crawled is not the same as read, and the difference is "
                     f"the work still available.</p>")
    else:
        o.append("<div class='empty'>No document has contributed a row. "
                 "Everything here was bulk-loaded.</div>")

    o.append(f"<footer>Data Wrangler &middot; generated {e(generated)} "
             f"&middot; a row counts as READ FROM A DOCUMENT when the file behind it is a pdf/docx/html, something a recogniser had to interpret; a csv/xlsx/las was PARSED and counts as bulk however it was loaded. "
             f"`source` is not used: promote rewrites it.</footer>")
    return "\n".join(o)


# ───────────────────────────── driver ───────────────────────────────────
def build(server, database, schema="dataview", log=print):
    eng = connect(server, database)
    with eng.connect() as cx:
        recs, ids = scan(cx, schema)
        docs = documents(cx, ids)
        n_crawled = crawled(cx)
    log(f"{len(recs)} table(s), {sum(r['rows'] for r in recs):,} row(s), "
        f"{len(docs)} document(s)")
    return render(recs, docs, n_crawled, database), recs, docs


def main(argv=None):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    ap = argparse.ArgumentParser(description="Database scorecard as HTML.")
    ap.add_argument("--server", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--schema", default="dataview")
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", action="store_true", dest="open_it")
    a = ap.parse_args(argv)

    doc, recs, docs = build(a.server, a.database, a.schema)
    out = a.out or f"scorecard_{a.database}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"-> {out}")
    if a.open_it:
        try:
            os.startfile(os.path.abspath(out))     # noqa: S606  (Windows)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
