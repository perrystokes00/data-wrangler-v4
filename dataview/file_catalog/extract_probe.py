"""
dataview/file_catalog/extract_probe.py
=====================================
What did the extractors actually get out of each document type?

THE PROBLEM THIS SOLVES
-----------------------
After a pipeline run you can see that 116 of 175 files were captured. What you
can't see is WHICH KINDS of document produced nothing — and that's the number
that matters, because a document type with a classifier but no detail loader
fails silently: it extracts, it resolves a UWI, it reports READY, and it writes
no rows. Nothing in the run log says "SCOUT_TICKET produced 0 rows across 17
files", which is exactly the sentence you want.

HOW IT SCORES
-------------
synth_docs writes MANIFEST.csv next to the documents, one row per generated
file: file, expected_uwi, well_name, case. `case` names the hard cases the
generator deliberately produces — filename_only, text_only, dashed_api,
unknown_uwi, no_uwi, image_only — so a failure that was DESIGNED can be told
apart from one that wasn't. Without that split, a harness reports 20% UWI
misses and you can't tell whether the extractor is broken or working exactly as
intended.

Everything else comes from the catalog and the lineage tables via
promotion_lineage, so this measures the SAME definition of "landed" the
scorecards use — no fourth opinion.

IT WRITES NOTHING. Run the pipeline, then run this.

USAGE (from the repo root)
--------------------------
    py -m dataview.file_catalog.extract_probe --manifest C:\\synth_tx_docs\\MANIFEST.csv
    py -m dataview.file_catalog.extract_probe --manifest ... --detail SCOUT_TICKET
    py -m dataview.file_catalog.extract_probe --manifest ... --csv probe.csv
    py -m dataview.file_catalog.extract_probe --no-manifest      # catalog only
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict

from sqlalchemy import text as _t

# Cases synth_docs generates on purpose. A UWI miss on one of these is the
# harness confirming the generator worked, not a defect.
DESIGNED_MISS = {"unknown_uwi", "no_uwi", "image_only"}
DESIGNED_HARD = {"filename_only", "text_only", "dashed_api"} | DESIGNED_MISS


def load_manifest(path):
    """{basename: {expected_uwi, well_name, case}} from synth_docs' MANIFEST."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            fn = os.path.basename((r.get("file") or "").strip())
            if fn:
                out[fn.lower()] = {
                    "expected_uwi": (r.get("expected_uwi") or "").strip(),
                    "well_name": (r.get("well_name") or "").strip(),
                    "case": (r.get("case") or "").strip() or "clean",
                }
    return out


def _norm(u):
    """14-char comparison form, matching what the loaders store."""
    s = re.sub(r"[^0-9A-Za-z]", "", str(u or ""))
    return (s + "0" * 14)[:14] if s else ""


def probe(engine, manifest_path=None, root=None, this_crawl=False, log=print):
    """Per-document-type rollup of what the extractors produced.

    Returns (rows, by_type) where rows is the per-file detail and by_type is
    the aggregate keyed on REPORT_TYPE.
    """
    from dataview.file_catalog import promotion_lineage as lin

    man = load_manifest(manifest_path)
    if manifest_path:
        log(f"-- manifest: {len(man):,} document(s) of ground truth")

    df = lin.file_detail(engine, root=root, this_crawl=this_crawl)
    if df is None or df.empty:
        log("-- no files in scope")
        return [], {}

    rows = []
    for rec in df.to_dict("records"):
        fn = os.path.basename(str(rec.get("file") or ""))
        m = man.get(fn.lower(), {})
        exp = m.get("expected_uwi", "")
        got = rec.get("uwi") or ""
        # A multi-well document's expected_uwi is a pipe-joined list, so
        # membership is the test, not equality.
        exp_set = {_norm(x) for x in exp.split("|") if x.strip()}
        if not exp_set:
            uwi_state = "n/a"
        elif not got:
            uwi_state = "missing"
        elif _norm(got) in exp_set:
            uwi_state = "ok"
        else:
            uwi_state = "wrong"
        rows.append({**rec, "case": m.get("case", ""),
                     "expected_uwi": exp, "uwi_state": uwi_state,
                     "multiwell": len(exp_set) > 1})

    by_type = defaultdict(lambda: {
        "files": 0, "extracted": 0, "captured": 0, "promoted": 0,
        "uwi_ok": 0, "uwi_missing": 0, "uwi_wrong": 0,
        "designed_hard": 0, "tables": defaultdict(int)})
    for r in rows:
        b = by_type[r.get("type") or "?"]
        b["files"] += 1
        b["extracted"] += 1 if r["extract"] == "Y" else 0
        b["captured"] += 1 if r["capture"] == "Y" else 0
        b["promoted"] += 1 if r["promote"] == "Y" else 0
        if r["uwi_state"] == "ok":
            b["uwi_ok"] += 1
        elif r["uwi_state"] == "missing":
            b["uwi_missing"] += 1
        elif r["uwi_state"] == "wrong":
            b["uwi_wrong"] += 1
        if r.get("case") in DESIGNED_HARD:
            b["designed_hard"] += 1
        # detail reads like "tops:12 curves:32(staged)" — sum per label so the
        # rollup says WHICH tables a document type feeds, not just how many.
        for part in str(r.get("detail") or "").split():
            if ":" in part:
                lbl, _, cnt = part.partition(":")
                n = re.match(r"\d+", cnt)
                if n:
                    b["tables"][lbl] += int(n.group(0))
    return rows, dict(by_type)


def render(rows, by_type, log=print):
    log("")
    log(f"{'document type':22}{'files':>6}{'extr':>6}{'capt':>6}{'prom':>6}"
        f"{'uwi ok':>8}{'miss':>6}{'wrong':>7}   tables fed")
    log("-" * 104)
    for name in sorted(by_type, key=lambda k: (-by_type[k]["files"], k)):
        b = by_type[name]
        tables = " ".join(f"{k}:{v:,}" for k, v in
                          sorted(b["tables"].items(), key=lambda kv: -kv[1]))
        log(f"{name[:21]:22}{b['files']:>6}{b['extracted']:>6}"
            f"{b['captured']:>6}{b['promoted']:>6}{b['uwi_ok']:>8}"
            f"{b['uwi_missing']:>6}{b['uwi_wrong']:>7}   {tables or '—'}")
    log("-" * 104)

    # The findings, stated rather than left to be spotted in the grid.
    log("")
    silent = [n for n, b in by_type.items()
              if b["extracted"] and not b["captured"]]
    if silent:
        log("!! EXTRACTED BUT CAPTURED NOTHING — these types have a classifier "
            "and no detail loader:")
        for n in sorted(silent):
            b = by_type[n]
            # A type made entirely of designed hard cases isn't a finding — the
            # generator built those to be unidentifiable. Say so inline rather
            # than leaving it to be worked out.
            note = ""
            if b["designed_hard"] >= b["files"]:
                note = "  (all designed hard cases — expected)"
            elif b["designed_hard"]:
                note = f"  ({b['designed_hard']} designed hard case(s))"
            log(f"     {n:24} {b['files']:>4} file(s), "
                f"{b['uwi_ok']:>3} with a good UWI{note}")
    wrong = [(n, b) for n, b in by_type.items() if b["uwi_wrong"]]
    if wrong:
        log("!! WRONG UWI (resolved to a well the document isn't about):")
        for n, b in sorted(wrong):
            log(f"     {n:24} {b['uwi_wrong']} file(s)")
    mw = [r for r in rows if r.get("multiwell")]
    if mw:
        log("-- multi-well documents (expected_uwi lists several wells):")
        for r in mw:
            n_exp = len([x for x in str(r["expected_uwi"]).split("|") if x.strip()])
            log(f"     {r['file']:44} covers {n_exp} well(s) -> {r['detail']}")
        log("   these carry a UWI per ROW. If the row count looks like one "
            "well's worth, the loader resolved a single file-level UWI and "
            "dropped the rest — a per-row-UWI bug, not a parse failure.")
    undesigned = [r for r in rows
                  if r["uwi_state"] == "missing" and r.get("case")
                  and r["case"] not in DESIGNED_MISS]
    if undesigned:
        log(f"!! {len(undesigned)} file(s) missing a UWI that were NOT "
            f"generated as hard cases:")
        for r in undesigned[:10]:
            log(f"     {r['file']:44} case={r['case']}")


def detail_for(rows, doc_type, log=print, limit=40):
    """Per-file listing for one document type — the follow-up to a bad rollup."""
    sel = [r for r in rows if (r.get("type") or "?") == doc_type]
    if not sel:
        log(f"-- no files of type {doc_type}")
        return
    log(f"\n-- {len(sel)} file(s) of type {doc_type}")
    log(f"{'file':46}{'extr':>5}{'capt':>5}{'prom':>5}  {'case':14}"
        f"{'uwi':>10}  detail")
    for r in sel[:limit]:
        log(f"{str(r['file'])[:45]:46}{r['extract']:>5}{r['capture']:>5}"
            f"{r['promote']:>5}  {(r.get('case') or '')[:13]:14}"
            f"{r['uwi_state']:>10}  {r['detail']}")
    if len(sel) > limit:
        log(f"   … and {len(sel) - limit:,} more")


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="What each document type actually captured, scored against "
                    "synth_docs' MANIFEST.csv")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--manifest", help="path to MANIFEST.csv")
    ap.add_argument("--no-manifest", action="store_true",
                    help="skip ground truth; report the catalog only")
    ap.add_argument("--root", help="limit to one scan root")
    ap.add_argument("--this-crawl", action="store_true",
                    help="limit to files scanned today")
    ap.add_argument("--detail", help="per-file listing for one document type")
    ap.add_argument("--csv", help="write the per-file rows to this path")
    a = ap.parse_args()

    from dataview.core.schema_introspect import make_engine
    eng = make_engine(a.server, a.database, "ODBC Driver 17 for SQL Server")

    rows, by_type = probe(eng,
                          manifest_path=None if a.no_manifest else a.manifest,
                          root=a.root, this_crawl=a.this_crawl)
    if not rows:
        return 1
    render(rows, by_type)
    if a.detail:
        detail_for(rows, a.detail)
    if a.csv:
        keys = ["file", "type", "ext", "extract", "capture", "promote",
                "uwi", "expected_uwi", "uwi_state", "case", "detail", "path"]
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\n-- per-file rows written to {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
