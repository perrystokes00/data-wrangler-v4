"""
docshape.propose
================
Point this at a document you have never seen and it tells you what to add to
the pack.

THE WORKFLOW IT REPLACES
------------------------
Run the recogniser, read which tables came back UNKNOWN, squint at the
headers, work out which cells already match a canonical field and which do
not, guess which of them discriminate this table from every existing shape,
then hand-write the entries. That is four steps of judgement, three of which
are mechanical.

This does the mechanical three. For every unrecognised table it reports which
header cells already resolve to a field, which do not, how close each existing
shape came, and then emits ready-to-paste `fields` and `shapes` entries with
the discriminating column already chosen.

WHAT IT WILL NOT DO
-------------------
It does not edit the pack. A proposal is a starting point that a person
should read: the alias lists it suggests are derived from one document's
wording, and the whole value of a pack is that its aliases cover wordings that
document did not use. Pasting without widening them is how you end up with a
vocabulary that only works on the file you built it from.

It also cannot know what a table MEANS. It sees that a column called "Bit No"
exists and that nothing claims it; whether that makes the table a bit record
or a drilling-assembly report is a judgement about the domain.

USAGE
-----
    py -m docshape propose --file report.pdf
    py -m docshape propose --file report.pdf --pack legal
    py -m docshape propose --dir C:\\docs --limit 20
"""
from __future__ import annotations

import os
import re

from docshape.engine.recognise import Recogniser


def _slug(text):
    """Header cell -> a plausible field name."""
    s = re.sub(r"\([^)]*\)", " ", str(text or ""))      # drop the unit
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    return s or "field"


def _alias_guesses(text):
    """Alias phrases a person would plausibly want, from one header cell.

    Deliberately generous: the cell as written, the cell without its unit, and
    the unit-free form with common abbreviations spelled out. A human prunes
    and widens from there.
    """
    raw = " ".join(str(text or "").split())
    bare = re.sub(r"\([^)]*\)", " ", raw)
    bare = " ".join(bare.split())
    out = []
    for cand in (bare.lower(), raw.lower()):
        cand = re.sub(r"[^a-z0-9 /]+", " ", cand)
        cand = " ".join(cand.split())
        if cand and cand not in out:
            out.append(cand)
    return out


def analyse_table(rec, name, rows, log=print, show_rows=2):
    """Report one table and, if unrecognised, propose how to name it."""
    header = list(rows[0].keys()) if rows else []
    if not header:
        return None

    # The readers report a failure to OPEN a file as a one-row table with an
    # "error" column, so it travels back through the same channel as real
    # content. Left unhandled, that arrives here looking like an unrecognised
    # table and this function helpfully proposes an `error` field and a shape
    # to hold it — advice that is confidently, uselessly wrong. A file that
    # could not be read is a different problem from a table we cannot name.
    if header == ["error"] or (len(header) == 1 and name.startswith("_error")):
        _msg = str(rows[0].get(header[0], "")) if rows else ""
        log(f"\n  !! COULD NOT READ THIS FILE")
        log(f"     {_msg}")
        if "PackageNotFound" in _msg:
            log("\n     python-docx raises that when the path does not exist, "
                "or when\n     the file is not a real .docx — a .doc renamed, "
                "an RTF, or a\n     zero-byte/partial copy. Check:")
            log("       Test-Path '<path>'")
            log("       Get-Item '<path>' | Select-Object Name, Length")
            log("     A valid .docx is a zip: its first two bytes are PK.")
        return None
    res = rec.read_table(header, [])
    known, unknown = [], []
    for _i, cell, field in rec.header_fields(header):
        (known if field else unknown).append((cell, field))

    if res["shape"] != "UNKNOWN":
        log(f"\n  [{name}] {len(rows)} row(s)  ->  {res['shape']} "
            f"({res['score']:.2f})"
            + (f"  ->  {res['target']}" if res["target"] else "  (no target)"))
        if res["unmapped"]:
            log(f"      columns nothing claimed: {res['unmapped']}")
            log(f"      (add these to {res['shape']}'s optional list if they "
                f"belong to it)")
        return None

    log(f"\n  [{name}] {len(rows)} row(s)  ->  UNRECOGNISED")
    log(f"      header: {header}")

    if known:
        log("\n      already understood:")
        for cell, field in known:
            log(f"         {str(cell)[:30]:32} -> {field}")
    if unknown:
        log("\n      no field claims these:")
        for cell, _f in unknown:
            log(f"         {cell}")

    # how close did each shape come? the near-misses are usually the answer
    found = {f for _c, f in known}
    near = []
    for shape, spec in rec.pack.shapes.items():
        req = list(spec.get("required", ()))
        hits = [f for f in req if f in found]
        if hits:
            near.append((len(hits) / len(req), shape, hits, req))
    near.sort(reverse=True)
    if near:
        log("\n      closest existing shapes:")
        for score, shape, hits, req in near[:4]:
            miss = [f for f in req if f not in hits]
            log(f"         {shape:22} {len(hits)}/{len(req)}"
                + (f"  missing {miss}" if miss else ""))

    if show_rows and rows:
        log("\n      sample values:")
        for r in rows[:show_rows]:
            vals = [f"{k}={v}" for k, v in list(r.items())[:6]]
            log("         " + ", ".join(vals))

    # ── the proposal ─────────────────────────────────────────────────────
    # The table's id ("p1_t01") says nothing about what it is, and only a
    # person can. Offer a placeholder that is obviously a placeholder.
    shape_name = "NAME_ME"
    new_fields = [(_slug(c), c) for c, _f in unknown]
    log("\n      ── paste into `fields` ──")
    for fname, cell in new_fields:
        aliases = ", ".join(f'"{a}"' for a in _alias_guesses(cell))
        log(f'         "{fname}":{" " * max(1, 16 - len(fname))}[{aliases}],')

    # required = the fields that DISCRIMINATE. A field no other shape requires
    # is what stops a general shape claiming this table.
    other_required = set()
    for spec in rec.pack.shapes.values():
        other_required.update(spec.get("required", ()))
    known_fields = [field for _cell, field in known]

    # `required` must DISCRIMINATE. A field no other shape requires is what
    # stops a general shape claiming this table; a field several shapes
    # already require guarantees a fight this one may lose.
    fresh = [f for f, _c in new_fields]
    req = [f for f in fresh if f not in other_required][:1]
    req += [f for f in known_fields
            if f not in other_required and f not in req][:1]
    if len(req) < 2:
        req = (req + [f for f in fresh if f not in req])[:2]

    seen = set(req)
    optional = []
    for f in fresh + known_fields:
        if f not in seen:
            seen.add(f)
            optional.append(f)

    log("\n      ── paste into `shapes` ──")
    log(f'         "{shape_name}": {{')
    log(f'             "required": {req},')
    log(f'             "optional": {optional[:9]},')
    log(f'             "min_required": 2, "target": None,')
    log("         },")
    log("\n      CHECK BEFORE PASTING: `required` must be fields no other")
    log("      shape requires, or a general shape will keep winning. Widen the")
    log("      aliases beyond this one document's wording.")
    return shape_name


def propose_file(path, pack_name="petroleum", log=print):
    from docshape.packs import load
    from docshape.readers import read_tables, NATIVE_EXTS

    ext = os.path.splitext(path)[1].lower()
    if ext in NATIVE_EXTS:
        log(f"\n{os.path.basename(path)} — parsed natively, no shapes involved")
        return []
    rec = Recogniser(load(pack_name))
    tables = read_tables(path) or {}
    log(f"\n{'=' * 74}")
    log(f"{os.path.basename(path)}  —  {len(tables)} table(s), pack '{pack_name}'")
    log("=" * 74)
    if not tables:
        log("  no tables found. If the document clearly has them, it may have "
            "no text layer — check with a PDF reader before adding shapes.")
        return []
    out = []
    for name, rows in tables.items():
        got = analyse_table(rec, name, rows, log=log)
        if got:
            out.append(got)
    return out


def propose_dir(target, pack_name="petroleum", limit=None, log=print):
    from docshape.readers import collect, TABLE_EXTS
    paths = [p for p in collect(target)
             if os.path.splitext(p)[1].lower() in TABLE_EXTS]
    if limit:
        paths = paths[:limit]
    log(f"-- {len(paths)} document(s)")
    proposed = {}
    for p in paths:
        for shape in propose_file(p, pack_name, log=log):
            proposed[shape] = proposed.get(shape, 0) + 1
    if proposed:
        log(f"\n{'=' * 74}")
        log("shapes proposed, most frequent first — start with the top one")
        for shape, n in sorted(proposed.items(), key=lambda kv: -kv[1]):
            log(f"   {shape:32} {n} document(s)")
    return proposed
