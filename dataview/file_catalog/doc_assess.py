"""
doc_assess.py — which of my 50,000 documents can I actually read?
=================================================================

A batch READABILITY CENSUS over the File Catalog's inventory (or any
folder). For every document it runs IDENTIFICATION ONLY — headers through
the recogniser, no capture, no rows kept — and stamps a verdict:

    READABLE        every table recognised
    PARTIAL         some recognised, some UNKNOWN
    UNKNOWN_ONLY    tables found, none recognised     -> vocabulary work
    NO_TABLES       readable file, nothing tabular    -> reader/OCR work
    FAILED          could not be read (reason kept)
    NATIVE          LAS/SEG-Y — fixed-shape readers own these
    UNSUPPORTED     extension nothing reads

Verdicts land as DOC_* columns on file_catalog.GLOBAL_FILE_CATALOG
(added if missing; never dropped or retyped — the cat_review_layer rule),
so "which can I read" becomes a GROUP BY and teaching progress becomes a
number per vocabulary version.

WHY IT SCALES
-------------
* Identification is arithmetic on header tokens; the only real cost is
  opening the file. Nothing is extracted twice and nothing is stored.
* Every verdict carries an ASSESS KEY (size:mtime:vocab-hash). Unchanged
  file + unchanged vocabulary = skipped on the next run, so re-running
  after a teach only touches what the teach could have changed.
* Resumable: verdicts commit in batches; kill it and re-run.

THE REPORT is the point: a status histogram, and the UNKNOWN header
signatures ranked by how many documents share them — the highest-leverage
teach first, with the nearest existing shapes and their missing fields
named. One teach can unlock hundreds of documents; this tells you which.

Assessment runs against pack + OVERLAY (the committed vocabulary), never
the sandbox — a census must measure what is established, not what is
being experimented with.

USAGE
-----
    # census a folder, report only, no database
    py -m dataview.file_catalog.doc_assess --in "C:\\docs" --pack petroleum

    # census the inventory and stamp the catalog
    py -m dataview.file_catalog.doc_assess --server "localhost\\SQLEXPRESS" \\
        --database DataView_Demo --pack petroleum [--force] [--limit N] \\
        [--like "%scout%"] [--csv report.csv]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from docshape.engine.recognise import Recogniser
from docshape.packs.overlay import load_layered
from dataview.file_catalog.doc_flow import extract_tables, near_misses

TABLE_EXTS = {".pdf", ".docx", ".xlsx", ".xls", ".html", ".htm"}
NATIVE_EXTS = {".las", ".segy", ".sgy"}
CATALOG = "file_catalog.GLOBAL_FILE_CATALOG"

# Columns this tool owns on the catalog. Added if missing, never removed.
DOC_COLUMNS = [
    ("DOC_STATUS", "varchar(16)"),
    ("DOC_TABLES", "int"),
    ("DOC_RECOGNISED", "int"),
    ("DOC_UNKNOWN", "int"),
    ("DOC_SHAPES", "nvarchar(400)"),
    ("DOC_ERROR", "nvarchar(400)"),
    ("DOC_ASSESS_KEY", "varchar(64)"),
    ("DOC_VOCAB_HASH", "char(12)"),
    ("DOC_ASSESSED_AT", "datetime2"),
]


# ═════════════════════════════════════════════════════════════════════════ #
# vocabulary identity
# ═════════════════════════════════════════════════════════════════════════ #
def vocab_hash(pack):
    """12 hex chars identifying the vocabulary CONTENT (base + overlay).
    Two deployments with identical packs+overlays hash identically; adding
    one alias changes it — which is exactly when re-assessment is due."""
    body = {
        "fields": {f: sorted(a) for f, a in pack.fields.items()},
        "shapes": {n: {"required": sorted(s.get("required", [])),
                       "optional": sorted(s.get("optional", [])),
                       "min_required": s.get("min_required")}
                   for n, s in pack.shapes.items()},
        "noise": sorted(getattr(pack, "noise", []) or []),
        "char_map": dict(getattr(pack, "char_map", {}) or {}),
    }
    return hashlib.sha1(json.dumps(body, sort_keys=True)
                        .encode("utf-8")).hexdigest()[:12]


def assess_key(path, vhash):
    try:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}:{vhash}"
    except OSError:
        return f"?:?:{vhash}"


# ═════════════════════════════════════════════════════════════════════════ #
# one document -> one verdict
# ═════════════════════════════════════════════════════════════════════════ #
def assess_one(engine_r, path):
    """Returns (status, n_tables, n_recognised, n_unknown, shapes_csv,
    error, unknown_headers). Identification only — no rows retained."""
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if name.startswith(("~$", "._")):
        return ("FAILED", 0, 0, 0, "", "lock stub / resource fork", [])
    if ext in NATIVE_EXTS:
        return ("NATIVE", 0, 0, 0, "", "", [])
    if ext not in TABLE_EXTS:
        return ("UNSUPPORTED", 0, 0, 0, "", "", [])
    if not os.path.exists(path):
        return ("FAILED", 0, 0, 0, "", "file not found", [])
    try:
        tabs = extract_tables(path)
    except Exception as e:
        return ("FAILED", 0, 0, 0, "", str(e)[:390], [])
    if not tabs:
        return ("NO_TABLES", 0, 0, 0, "", "", [])
    shapes, unknown_headers = [], []
    for _name, header, _rows in tabs:
        s, _score, _cm = engine_r.identify(header)
        if s:
            shapes.append(s)
        else:
            unknown_headers.append([str(h) for h in header])
    n_t, n_u = len(tabs), len(unknown_headers)
    n_r = n_t - n_u
    status = ("READABLE" if n_u == 0
              else "UNKNOWN_ONLY" if n_r == 0
              else "PARTIAL")
    return (status, n_t, n_r, n_u,
            ",".join(sorted(set(shapes)))[:390], "", unknown_headers)


def extract_with_titles(path):
    """[(name, header, rows, title)] — extraction plus the vendor's own
    words for each table. Same reader, opt-in titles sink."""
    from docshape.readers.tables import raw_tables
    from dataview.file_catalog.doc_flow import _from_raw
    titles = {}
    raw = raw_tables(path, titles=titles)
    return [(n, h, r, titles.get(n, "")) for n, h, r in _from_raw(raw)]


def field_signature(engine_r, header):
    """Cluster key for a table: WHAT THE VOCABULARY RESOLVED, plus how many
    cells it didn't. Header TEXT splits "Time (hrs)" from "Time" into two
    candidates; the resolved field set collapses the same table type across
    vendors into one. Unresolved cells are counted, not named — their
    wordings are what differ."""
    fields, unres = [], 0
    for _i, cell, f in engine_r.header_fields(header):
        if f:
            fields.append(f)
        elif str(cell).strip():
            unres += 1
    return (tuple(sorted(set(fields))), unres)


def _shape_name_from(title, header):
    """A proposed snake_case shape name — the vendor's section title when
    there is one (it names the table type in plain English), else the
    first two header words."""
    src = title or " ".join(str(h) for h in header[:2])
    words = [w for w in re.split(r"[^A-Za-z0-9]+", str(src)) if w]
    stop = {"the", "of", "and", "a", "an", "summary", "report", "table"}
    keep = [w.lower() for w in words if w.lower() not in stop][:3]
    return "_".join(keep) or "unnamed_shape"


def _sig(header):
    return hashlib.sha1("|".join(str(h).strip().lower()
                                 for h in header)
                        .encode("utf-8")).hexdigest()[:12]


# ═════════════════════════════════════════════════════════════════════════ #
# database plumbing (mssql; column-adds via INFORMATION_SCHEMA, the
# cat_review_layer pattern: widen, never drop or retype)
# ═════════════════════════════════════════════════════════════════════════ #
def unique_documents(paths):
    """Collapse byte-identical copies to one representative each.

    The inventory holds the same document under several paths (a OneDrive
    copy, a C:\\Bulk copy); a census that counts both is counting files,
    not documents. Size groups first — a unique size needs no hashing —
    then sha1 only where sizes collide, so the usual corpus hashes a small
    fraction of its bytes. Returns (representatives, {rep: [all copies]});
    non-document extensions pass through untouched.
    """
    by_size, others = defaultdict(list), []
    for p in paths:
        if os.path.splitext(p)[1].lower() in (TABLE_EXTS | NATIVE_EXTS):
            try:
                by_size[os.path.getsize(p)].append(p)
                continue
            except OSError:
                pass
        others.append(p)
    keep, copies = [], {}
    for _size, ps in by_size.items():
        if len(ps) == 1:
            keep.append(ps[0])
            copies[ps[0]] = ps
            continue
        by_hash = defaultdict(list)
        for p in ps:
            h = hashlib.sha1()
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                by_hash[h.hexdigest()].append(p)
            except OSError:
                by_hash[p].append(p)      # unreadable: its own group
        for ps2 in by_hash.values():
            rep = sorted(ps2)[0]
            keep.append(rep)
            copies[rep] = sorted(ps2)
    for p in others:
        keep.append(p)
        copies[p] = [p]
    return sorted(keep), copies


def get_engine(server, database, driver="ODBC Driver 17 for SQL Server"):
    from sqlalchemy import create_engine
    url = (f"mssql+pyodbc://@{server}/{database}"
           f"?driver={driver.replace(' ', '+')}&trusted_connection=yes")
    return create_engine(url, fast_executemany=True)


def ensure_columns(engine, log=print):
    from sqlalchemy import text
    schema, table = CATALOG.split(".")
    with engine.begin() as cx:
        have = {str(r[0]).upper() for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t"),
            {"s": schema, "t": table})}
        for col, typ in DOC_COLUMNS:
            if col.upper() not in have:
                cx.execute(text(
                    f"ALTER TABLE {CATALOG} ADD {col} {typ} NULL"))
                log(f"  + column {col} {typ}")


def inventory_paths(engine, like=None, force=False, vhash=None, limit=None):
    """(FILE_PATH, DOC_ASSESS_KEY) rows from the catalog, NOLOCK — the
    inventory is being written by other tools and a census must read past
    their locks rather than hang (the July 25 lesson)."""
    from sqlalchemy import text
    q = (f"SELECT FILE_PATH, DOC_ASSESS_KEY FROM {CATALOG} WITH (NOLOCK) "
         f"WHERE FILE_PATH IS NOT NULL")
    params = {}
    if like:
        q += " AND FILE_PATH LIKE :lk"
        params["lk"] = like
    q += " ORDER BY FILE_PATH"
    with engine.connect() as cx:
        rows = cx.execute(text(q), params).fetchall()
    out = []
    for p, key in rows:
        p = str(p)
        if not force and key and vhash and str(key).endswith(":" + vhash) \
                and str(key) == assess_key(p, vhash):
            continue                       # unchanged file, same vocabulary
        out.append(p)
        if limit and len(out) >= limit:
            break
    return out


def stamp(engine, batch):
    from sqlalchemy import text
    sql = text(
        f"UPDATE {CATALOG} SET DOC_STATUS=:st, DOC_TABLES=:nt, "
        f"DOC_RECOGNISED=:nr, DOC_UNKNOWN=:nu, DOC_SHAPES=:sh, "
        f"DOC_ERROR=:er, DOC_ASSESS_KEY=:ak, DOC_VOCAB_HASH=:vh, "
        f"DOC_ASSESSED_AT=:at WHERE FILE_PATH=:p")
    with engine.begin() as cx:
        for row in batch:
            cx.execute(sql, row)


# ═════════════════════════════════════════════════════════════════════════ #
# the report
# ═════════════════════════════════════════════════════════════════════════ #
def report(results, sig_index, engine_r, log=print, top=15):
    n = len(results)
    n_copies = sum(r.get("cp", 1) for r in results)
    hist = Counter(r["st"] for r in results)
    log(f"\n{'=' * 62}\nREADABILITY CENSUS — {n} document(s)"
        + (f" across {n_copies} file(s)" if n_copies > n else "")
        + f"\n{'=' * 62}")
    order = ["READABLE", "PARTIAL", "UNKNOWN_ONLY", "NO_TABLES",
             "FAILED", "NATIVE", "UNSUPPORTED"]
    for st in order:
        if hist.get(st):
            pct = 100.0 * hist[st] / max(n, 1)
            log(f"  {st:14} {hist[st]:7,}   {pct:5.1f}%")
    n_t = sum(r["nt"] for r in results)
    n_r = sum(r["nr"] for r in results)
    if n_t:
        log(f"\n  tables: {n_r:,} recognised of {n_t:,} "
            f"({100.0 * n_r / n_t:.1f}%)")
    if sig_index:
        log(f"\nTOP UNKNOWN SIGNATURES — one teach per line, ranked by "
            f"documents unlocked:")
        ranked = sorted(sig_index.items(),
                        key=lambda kv: -len(kv[1]["files"]))
        for sig, info in ranked[:top]:
            hdr = info["header"]
            log(f"\n  [{sig}] {len(info['files'])} document(s)")
            log(f"    header: {hdr[:8]}" + (" …" if len(hdr) > 8 else ""))
            for nm in near_misses(engine_r, hdr)[:2]:
                log(f"    close:  {nm['shape']} ({nm['have']:.2f}) "
                    f"missing {nm['missing']}")
        if len(ranked) > top:
            log(f"\n  … and {len(ranked) - top} more signature(s)")
    fails = [r for r in results if r["st"] == "FAILED" and r["er"]]
    if fails:
        log(f"\nFAILURES ({len(fails)}):")
        for r in fails[:10]:
            log(f"  ✗ {r['p']}\n      {r['er'][:120]}")



# ═════════════════════════════════════════════════════════════════════════ #
# discovery: what shapes SHOULD exist that don't
# ═════════════════════════════════════════════════════════════════════════ #
def discover(engine_r, paths, log=print, top=20, min_docs=1):
    """Corpus-wide shape candidates, ranked by documents unlocked.

    Five signals, each answering a different question:

      CANDIDATE SHAPES   unknown tables clustered by RESOLVED FIELD SET
                         (not header text), so one candidate covers every
                         vendor's wording of the same table
      VARIANT vs SIBLING a candidate that consistently misses the same
                         required field of an existing shape is a VARIANT
                         (widen that shape); one that resolves a different
                         field set is a SIBLING (new shape)
      MISSING FIELDS     unresolved header wordings, ranked — vocabulary
                         gaps, cheaper to fix than shapes
      UNCLAIMED COLUMNS  columns on RECOGNISED tables that no field took;
                         travelling together means a shape is too narrow
      DARK DOCUMENTS     files where NOTHING recognised — usually a whole
                         document class, not a stray gap
    """
    cands = defaultdict(lambda: {"files": [], "headers": [], "titles": [],
                                 "near": Counter(), "unres": Counter()})
    unclaimed = Counter()
    unclaimed_sets = Counter()
    dark = []
    for p in paths:
        try:
            tabs = extract_with_titles(p)
        except Exception:
            continue
        n_known = 0
        for _name, header, _rows, title in tabs:
            s, _sc, cm = engine_r.identify(header)
            if s:
                n_known += 1
                taken = set(cm.values())
                extra = [str(c) for i, c in enumerate(header)
                         if i not in taken and str(c).strip()]
                # PAIR-GRID SHAPES REPORT THEIR VALUES AS COLUMNS. In a
                # label/value grid the reader promotes row 0, so the first
                # pair's VALUES ("2024-01-08", "PERMIAN BASIN 4H") become
                # header cells — and nothing should claim them, because
                # pivot_pair_grid rearranges the whole table at capture. A
                # shape carrying a transform has already declared that its
                # header is not what it appears to be, so flag rather than
                # count: listing these as vocabulary gaps sends the reader
                # hunting for aliases that must never be written.
                if s in (getattr(getattr(engine_r, "pack", None),
                         "transforms", None) or {}):
                    extra = []
                for c in extra:
                    unclaimed[(s, c.strip().lower())] += 1
                if len(extra) > 1:
                    unclaimed_sets[(s, tuple(sorted(
                        c.strip().lower() for c in extra)))] += 1
                continue
            key = field_signature(engine_r, header)
            g = cands[key]
            g["files"].append(p)
            if len(g["headers"]) < 4 and header not in g["headers"]:
                g["headers"].append([str(h) for h in header])
            if title:
                g["titles"].append(title)
            for nm in near_misses(engine_r, header, top=1):
                g["near"][(nm["shape"], tuple(nm["missing"]),
                           round(nm["have"], 2))] += 1
            for _i, cell, f in engine_r.header_fields(header):
                if not f and str(cell).strip():
                    g["unres"][str(cell).strip().lower()] += 1
        if tabs and n_known == 0:
            dark.append(p)

    ranked = sorted(cands.items(),
                    key=lambda kv: -len({f for f in kv[1]["files"]}))
    log(f"\n{'=' * 62}\nSHAPE CANDIDATES\n{'=' * 62}")
    if not ranked:
        log("  none — every table recognised.")
    shown = 0
    for (fields, unres_n), g in ranked:
        ndoc = len(set(g["files"]))
        if ndoc < min_docs:
            continue
        shown += 1
        if shown > top:
            break
        title = Counter(g["titles"]).most_common(1)
        title = title[0][0] if title else ""
        name = _shape_name_from(title, g["headers"][0] if g["headers"] else [])
        log(f"\n  ▸ {name}   — {ndoc} document(s)"
            + (f'   [{title}]' if title else ""))
        log(f"    resolves: {list(fields) or '(nothing)'}"
            + (f"   · {unres_n} unresolved cell(s)" if unres_n else ""))
        near = g["near"].most_common(1)
        if near:
            (shp, missing, have), hits = near[0]
            # A VARIANT nearly IS the existing shape — most of its required
            # fields present, one missing; widening that shape is the
            # cheaper fix. Below that the table resolves a different field
            # set and deserves its own shape. Judged on the SCORE, not on
            # consistency: "misses the same field every time" is vacuously
            # true of a candidate seen once.
            variant = have >= 0.67 and len(missing) <= 1
            log(f"    verdict:  {'VARIANT of ' + shp if variant else 'NEW SHAPE'}"
                f" — nearest {shp} at {have:.2f}, misses {list(missing)}")
            if variant:
                log(f"              → consider lowering {shp}'s "
                    f"min_required, or teaching {list(missing)}")
        if g["unres"]:
            log(f"    teach:    " + ", ".join(
                f"'{w}'" for w, _c in g["unres"].most_common(5)))
        for h in g["headers"][:2]:
            log(f"    header:   {h[:7]}" + (" …" if len(h) > 7 else ""))
        seen_files = sorted(set(g["files"]))
        for fp in seen_files[:3]:
            log(f"    file:     {fp}")
        if len(seen_files) > 3:
            log(f"              … and {len(seen_files) - 3} more document(s)")
    if shown > top:
        log(f"\n  … and {shown - top} more candidate(s)")

    if unclaimed:
        log(f"\n{'=' * 62}\nUNCLAIMED COLUMNS on recognised tables\n{'=' * 62}")
        log("  a field the vocabulary lacks — cheaper than a shape:")
        for (shape, col), c in unclaimed.most_common(12):
            log(f"    {c:6,}x  {shape:22} '{col}'")
        travel = [(k, c) for k, c in unclaimed_sets.most_common(5) if c > 1]
        if travel:
            log("\n  travelling together (a shape may be too narrow):")
            for (shape, cols), c in travel:
                log(f"    {c:6,}x  {shape:22} {list(cols)[:4]}")
    if dark:
        log(f"\n{'=' * 62}\nDARK DOCUMENTS — nothing recognised at all "
            f"({len(dark)})\n{'=' * 62}")
        for p in dark[:10]:
            log(f"    {p}")
        if len(dark) > 10:
            log(f"    … and {len(dark) - 10} more")
    return ranked

def write_discover_csv(path, ranked, engine_r, min_docs=1):
    """Shape candidates as a worksheet: one row per candidate, ordered by
    documents unlocked — the teaching backlog, priced, in a form you can
    sort, filter and assign."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "proposed_name", "documents", "section_title",
                    "verdict", "nearest_shape", "nearest_score",
                    "missing_fields", "resolves_fields", "unresolved_cells",
                    "wordings_to_teach", "sample_header", "sample_paths"])
        rank = 0
        for (fields, unres_n), g in ranked:
            ndoc = len(set(g["files"]))
            if ndoc < min_docs:
                continue
            rank += 1
            title = Counter(g["titles"]).most_common(1)
            title = title[0][0] if title else ""
            hdr = g["headers"][0] if g["headers"] else []
            near = g["near"].most_common(1)
            if near:
                (shp, missing, have), _hits = near[0]
                variant = have >= 0.67 and len(missing) <= 1
                verdict = "VARIANT" if variant else "NEW_SHAPE"
            else:
                shp, missing, have, verdict = "", (), 0.0, "NEW_SHAPE"
            w.writerow([
                rank, _shape_name_from(title, hdr), ndoc, title, verdict,
                shp, f"{have:.2f}" if shp else "",
                " | ".join(missing), " | ".join(fields), unres_n,
                " | ".join(wd for wd, _c in g["unres"].most_common(8)),
                " | ".join(str(h) for h in hdr[:10]),
                # FULL paths, several of them: a candidate is only
                # actionable if you can open the documents behind it.
                " | ".join(sorted(set(g["files"]))[:3]),
            ])
    return rank


def write_csv(path, results):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_path", "status", "tables", "recognised",
                    "unknown", "shapes", "error", "copies"])
        for r in results:
            w.writerow([r["p"], r["st"], r["nt"], r["nr"], r["nu"],
                        r["sh"], r["er"], r.get("cp", 1)])


# ═════════════════════════════════════════════════════════════════════════ #
# main
# ═════════════════════════════════════════════════════════════════════════ #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Readability census: identify every document, stamp "
                    "the catalog, rank the teaching backlog.")
    ap.add_argument("--in", dest="indir",
                    help="folder census, report only — no database")
    ap.add_argument("--server", help=r"e.g. localhost\SQLEXPRESS")
    ap.add_argument("--database", help="e.g. DataView_Demo")
    ap.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
    ap.add_argument("--pack", default="petroleum")
    ap.add_argument("--like", help="FILE_PATH LIKE filter, e.g. %%scout%%")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--discover", action="store_true",
                    help="also report SHAPE CANDIDATES: unknown tables "
                         "clustered by resolved field set, ranked by "
                         "documents unlocked")
    ap.add_argument("--discover-csv",
                    help="with --discover, write the ranked shape "
                         "candidates here as a worksheet")
    ap.add_argument("--min-docs", type=int, default=1,
                    help="with --discover, hide candidates seen in fewer "
                         "than N documents (use 5-10 on a big corpus)")
    ap.add_argument("--unique", action="store_true",
                    help="collapse byte-identical copies: assess one "
                         "representative, stamp every copy")
    ap.add_argument("--force", action="store_true",
                    help="re-assess even when file and vocabulary "
                         "are unchanged")
    ap.add_argument("--csv", help="also write per-document verdicts here")
    a = ap.parse_args(argv)

    if a.discover_csv and not a.discover:
        a.discover = True          # asking for the file means asking for it
    if not a.indir and not (a.server and a.database):
        ap.error("give --in <folder>, or --server and --database")

    pack, _ov, _op, _sb, _sp = load_layered(a.pack, use_sandbox=False)
    eng_r = Recogniser(pack)
    vhash = vocab_hash(pack)
    print(f"vocabulary: {a.pack} + overlay · hash {vhash}")

    engine = None
    if a.indir:
        if not os.path.isdir(a.indir):
            ap.error(f"not a folder: {a.indir}")
        paths = []
        for dirpath, _d, names in os.walk(a.indir):
            for nm in sorted(names):
                paths.append(os.path.join(dirpath, nm))
    else:
        engine = get_engine(a.server, a.database, a.driver)
        ensure_columns(engine)
        paths = inventory_paths(engine, a.like, a.force, vhash, a.limit)
        print(f"{len(paths)} document(s) due for assessment "
              f"(unchanged ones skipped{' — none, --force' if a.force else ''})")
    if a.limit:
        paths = paths[:a.limit]

    copies = {pp: [pp] for pp in paths}
    if a.unique:
        n_files = len(paths)
        paths, copies = unique_documents(paths)
        n_dup = n_files - len(paths)
        print(f"{n_files} file(s) -> {len(paths)} unique document(s)"
              + (f" ({n_dup} duplicate cop{'y' if n_dup == 1 else 'ies'})"
                 if n_dup else ""))

    results, batch = [], []
    sig_index = defaultdict(lambda: {"header": None, "files": []})
    now = datetime.now()
    for i, p in enumerate(paths, start=1):
        st, nt, nr, nu, sh, er, unk = assess_one(eng_r, p)
        results.append({"p": p, "st": st, "nt": nt, "nr": nr, "nu": nu,
                        "sh": sh, "er": er, "cp": len(copies.get(p, [p]))})
        for hdr in unk:
            s = _sig(hdr)
            sig_index[s]["header"] = hdr
            sig_index[s]["files"].append(p)
        if engine is not None:
            for cp in copies.get(p, [p]):   # one verdict, every copy stamped
                batch.append({"st": st, "nt": nt, "nr": nr, "nu": nu,
                              "sh": sh, "er": er,
                              "ak": assess_key(cp, vhash), "vh": vhash,
                              "at": now, "p": cp})
            if len(batch) >= 100:
                stamp(engine, batch)       # commit as we go: resumable
                batch = []
        if i % 250 == 0:
            print(f"  … {i}/{len(paths)}")
    if engine is not None and batch:
        stamp(engine, batch)

    report(results, dict(sig_index), eng_r)
    if a.discover:
        # Re-reads the documents that had unknowns: discovery needs the
        # section titles and per-cell resolutions the census doesn't keep,
        # and keeping them for 50,000 documents to serve one optional
        # report is the wrong trade.
        # READABLE DOCUMENTS ARE NOT FINISHED BUSINESS. Discovery began as
        # "find the table types we cannot read", so it re-read only
        # PARTIAL/UNKNOWN_ONLY — and a corpus where every table
        # identifies then reported "none, every table recognised" while
        # columns were quietly falling out of those very tables. That
        # reads as a clean bill of health and is not one: recognising a
        # table says nothing about how much of it was KEPT.
        #
        # So when there are no unknowns left, sweep the READABLE ones for
        # unclaimed columns instead. That is the whole remaining question
        # at that point, and the answer costs one more pass over a corpus
        # the census has already proved it can read.
        interesting = [r["p"] for r in results
                       if r["st"] in ("PARTIAL", "UNKNOWN_ONLY")]
        if not interesting:
            interesting = [r["p"] for r in results if r["st"] == "READABLE"]
            print(f"\nno unknown tables — sweeping {len(interesting)} "
                  f"readable document(s) for UNCLAIMED COLUMNS instead…")
        else:
            print(f"\ndiscovering over {len(interesting)} document(s) "
                  f"with unknown tables…")
        ranked = discover(eng_r, interesting, min_docs=a.min_docs)
        if a.discover_csv:
            n_c = write_discover_csv(a.discover_csv, ranked, eng_r,
                                     a.min_docs)
            print(f"\n{n_c} candidate(s) written to {a.discover_csv}")
    if a.csv:
        write_csv(a.csv, results)
        print(f"\nverdicts written to {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
