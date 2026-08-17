"""
doc_flow.py — the 📄 Documents lane of the unified Load Assistant.
==================================================================

Perry's loop: bring documents in → extract → see what was recognised → the AI
DESCRIBES the needed vocabulary changes → you approve → the sandbox takes them
→ re-check proves the diff → promote → next group.

DESIGN LAW (same as the Load Assistant): the AI proposes, the deterministic
recogniser verifies by re-running, the human approves, the OVERLAY remembers.
The AI never edits a pack file and never writes data anywhere. This module
writes exactly two things: `<pack>_sandbox.json` (corrections awaiting
approval-into-vocabulary) and, on promote, `<pack>_overlay.json`. No cat_
rows, no DuckDB, no database connection. Capture stays `docshape capture`;
migration stays promote's job. A testing tool must not become a production
dependency.

ONE SEAM LEFT
-------------
_ai_call(prompt) uses the Load Assistant's client (bulk_dir_loader._ai_api_key
+ DATAVIEW_AI_MODEL), falling back to the SDK's own key for standalone
docshape deployments. The reply stack (fence strip, raw_decode first object,
one self-repair retry, prompt-size cap) is here and stays regardless.
Extraction is WIRED, not guessed: docshape.readers.tables.raw_tables.

WIRING (until page_load_assistant.py grows the 📄 Documents tab):
    app_v3.py  →  _nav_card(c9, "📄", "Doc Assistant",
                            "Extract, teach, approve", "docflow")
                  elif S.app_mode == "docflow":
                      from dataview.file_catalog import doc_flow
                      doc_flow.render(S.engine)      # engine accepted, unused
    Or standalone:  streamlit run doc_flow.py-wrapper calling render().
    CLI (no streamlit, no AI unless asked):
        py doc_flow.py <file-or-dir> [--pack petroleum] [--ai]

Streamlit scars honoured: pending edits harvested before rebuild; approval
checkboxes live in a FORM keyed to a proposal-batch version so they never
re-default mid-edit; no nested expanders; no widget's own key is assigned
after instantiation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

# docshape is a SIBLING of dataview; Streamlit launches from the repo root,
# but belt-and-braces the path the same way page_docshape does.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from docshape.engine.recognise import Recogniser
from docshape.packs import load as load_pack, validate as validate_pack
from docshape.packs.overlay import (
    empty, load_overlay, save_overlay, add_alias, add_shape, set_numeric,
    promote_sandbox, sandbox_path, default_path, load_layered,
)

PACK_NAME_DEFAULT = "petroleum"
MODEL = "claude-sonnet-5"           # same default as ai_propose_tables
MAX_PROMPT_CHARS = 32000            # ~8k tokens — same budget discipline
SAMPLE_ROWS = 3
TABLE_EXTS_FALLBACK = {".pdf", ".docx", ".xlsx", ".xls",
                       ".html", ".htm"}


# ═════════════════════════════════════════════════════════════════════════ #
# 1 · EXTRACTION  (reader seam)
# ═════════════════════════════════════════════════════════════════════════ #
def _readers():
    import docshape.readers as R
    return R


def collect_files(root):
    """Documents under a folder, or [root] for a single file.

    Prefers readers.collect (it already skips ~$ lock stubs and ._ resource
    forks — the Word-lock-stub lesson); falls back to a walk over TABLE_EXTS.
    """
    if os.path.isfile(root):
        return [root]
    R = None
    try:
        R = _readers()
    except Exception:
        pass
    if R is not None and hasattr(R, "collect"):
        try:
            return sorted(str(p) for p in R.collect(root))
        except TypeError:
            pass                              # different signature — fall back
    exts = set(getattr(R, "TABLE_EXTS", TABLE_EXTS_FALLBACK)) if R \
        else set(TABLE_EXTS_FALLBACK)
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.startswith(("~$", "._")):
                continue
            if os.path.splitext(n)[1].lower() in exts:
                out.append(os.path.join(dirpath, n))
    return sorted(out)


def extract_tables(path):
    """One document -> [(table_name, header, data_rows)].

    Wired to the REAL entry point: docshape.readers.tables.raw_tables,
    which returns {table_name: [ {header_cell: value, ...}, ... ]} — one
    dict per row, keys in header order — and {"_error": [{"error": msg}]}
    when a file could not be read. A read failure RAISES here so the
    caller reports a failed FILE, never a silently-empty one (the lock-stub
    lesson, and the lesson of this very seam's first deployment: an adapter
    that guessed wrong returned zero tables per file with no error).
    """
    from docshape.readers.tables import raw_tables
    return _from_raw(raw_tables(path))


def _from_raw(raw):
    """raw_tables output -> [(name, header, rows)].

    Row dicts preserve insertion order, and the readers build them with
    dict(zip(header, ...)), so the first row's keys ARE the header in
    column order. Values are read back by those same keys, never by
    position."""
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"raw_tables returned {type(raw).__name__}, expected dict")
    if "_error" in raw:
        msg = ""
        try:
            msg = str(raw["_error"][0].get("error", ""))
        except Exception:
            pass
        raise RuntimeError(msg or "reader error")
    out = []
    for name, rowdicts in raw.items():
        if not rowdicts:
            continue
        keys = list(rowdicts[0].keys())
        header = [str(k) for k in keys]
        rows = [[rd.get(k, "") for k in keys] for rd in rowdicts]
        out.append((str(name), header, rows))
    return out


# ═════════════════════════════════════════════════════════════════════════ #
# 2 · ANALYSIS  (deterministic — the recogniser, plus propose-style evidence)
# ═════════════════════════════════════════════════════════════════════════ #
def _s(v, width=28):
    t = str(v if v is not None else "").strip()
    return t[:width]


def analyse(pack, files):
    """Run the recogniser over every table in every file.

    Returns one dict per table: shape/score/columns/unmapped from
    read_table, plus file/table/header/sample. Rows are NOT retained —
    identification depends only on the header, so the re-check diff never
    needs them, and a folder of documents should not live in session state.
    A file that fails to read is reported as a FAILURE, not as an
    unrecognised table (the lock-stub lesson from propose.py).
    """
    eng = Recogniser(pack)
    results = []
    for f in files:
        try:
            tabs = extract_tables(f)
        except Exception as e:
            results.append({"file": f, "table": None, "error": str(e)})
            continue
        if not tabs:
            # A pdf that yields zero tables is a FINDING (no text layer,
            # text boxes instead of tables), not something to skip past.
            results.append({"file": f, "table": None, "error": None,
                            "empty": True, "shape": None})
            continue
        for tname, header, rows in tabs:
            r = eng.read_table(header, rows)
            r.pop("rows", None)                      # mapped rows not needed
            r.update({
                "file": f, "table": tname, "header": [str(h) for h in header],
                "sample": [[_s(v) for v in row[:len(header)]]
                           for row in rows[:SAMPLE_ROWS]],
            })
            results.append(r)
    return results


def near_misses(engine, header, top=3):
    """How close each existing shape came, with the missing requireds NAMED.
    This is the evidence the AI reasons over — it keeps the proposal honest."""
    found = {f for _i, _c, f in engine.header_fields(header) if f}
    scored = []
    for name, spec in engine.pack.shapes.items():
        req = spec["required"]
        hits = [f for f in req if f in found]
        missing = [f for f in req if f not in found]
        frac = len(hits) / max(len(req), 1)
        if frac > 0:
            scored.append((frac, name, missing))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"shape": n, "have": round(s, 2), "missing": m}
            for s, n, m in scored[:top]]


def _sig(header):
    return tuple(str(h).strip().lower() for h in header)


def group_unresolved(pack, results):
    """UNKNOWN tables grouped by header signature, ranked by document count —
    approve the highest-leverage correction first (propose --dir's ranking).
    Tables recognised but with unmapped columns form a second, lighter group.
    """
    eng = Recogniser(pack)
    unknown, partial = {}, {}
    for r in results:
        if r.get("error") or not r.get("header"):
            continue
        if r["shape"] == "UNKNOWN":
            g = unknown.setdefault(_sig(r["header"]), {
                "header": r["header"], "files": [], "sample": r["sample"],
                "cell_fields": [
                    {"cell": c, "field": f}
                    for _i, c, f in eng.header_fields(r["header"])],
                "near_misses": near_misses(eng, r["header"]),
            })
            g["files"].append(os.path.basename(r["file"]))
        elif r.get("unmapped"):
            g = partial.setdefault((r["shape"],) + _sig(r["unmapped"]), {
                "shape": r["shape"], "unmapped": r["unmapped"],
                "files": [], "sample": r["sample"], "header": r["header"],
            })
            g["files"].append(os.path.basename(r["file"]))
    rank = lambda d: sorted(d.values(), key=lambda g: -len(g["files"]))
    return rank(unknown), rank(partial)


# ═════════════════════════════════════════════════════════════════════════ #
# 3 · AI PROPOSALS  (the reply stack; contract = vocabulary changes)
# ═════════════════════════════════════════════════════════════════════════ #
_CONTRACT = """\
You are the vocabulary assistant for a deterministic table recogniser.
It matches header cells to canonical FIELDS via alias phrases (token-subset,
order-free), then identifies each table as a SHAPE scored on required fields.
You never see or write data — you only propose VOCABULARY changes, which a
human will approve and the recogniser will re-verify.

Reply with ONLY a JSON object, no prose, no fences:
{
  "summary": "<one or two sentences on what these tables are>",
  "proposals": [
    {"kind":"alias","field":"<existing field>","alias":"<header wording>",
     "why":"<one sentence>"},
    {"kind":"new_field","field":"<snake_case>","aliases":["<wording>",...],
     "numeric":true|false,"why":"<one sentence>"},
    {"kind":"shape","name":"<snake_case — never NAME_ME>",
     "required":["<field>",...],"optional":["<field>",...],
     "target":null,"columns":{},"why":"<one sentence>"}
  ]
}

Rules, in priority order:
1. Prefer an ALIAS on an existing field over a NEW FIELD, and a new field
   over a NEW SHAPE. The cheapest change that makes the table recognisable.
2. A shape's REQUIRED fields must DISCRIMINATE: choose from fields no other
   shape requires (their required sets are given). Required fields that are
   merely present let a greedy shape keep winning the tie-break.
3. An alias is the header's wording, lower-cased, units stripped mentally
   ("Producing Horizon (ft)" -> "producing horizon"). Never invent wordings
   the documents don't use.
4. A word that is both a unit and a meaningful term is NEVER noise; do not
   propose noise entries at all.
5. If a table is genuinely not worth capturing (an invoice, a table of
   contents), say so in "summary" and propose nothing for it.
"""


def build_prompt(pack, unknown_groups, partial_groups, limit=6):
    fields = {f: al[:4] for f, al in sorted(pack.fields.items())}
    shape_reqs = {n: spec["required"] for n, spec in sorted(pack.shapes.items())}
    payload = {
        "pack_fields": fields,
        "shape_required_sets": shape_reqs,
        "unrecognised_tables": [
            {"seen_in_n_documents": len(g["files"]),
             "header": g["header"],
             "cells_already_resolving": g["cell_fields"],
             "closest_shapes": g["near_misses"],
             "sample_rows": g["sample"]}
            for g in unknown_groups[:limit]],
        "recognised_but_unmapped_columns": [
            {"shape": g["shape"], "unmapped": g["unmapped"],
             "seen_in_n_documents": len(g["files"]),
             "sample_rows": g["sample"]}
            for g in partial_groups[:limit]],
    }
    prompt = _CONTRACT + "\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    # Prompt-size cap: shed sample rows first, then whole groups.
    while len(prompt) > MAX_PROMPT_CHARS and (
            payload["unrecognised_tables"] or
            payload["recognised_but_unmapped_columns"]):
        for g in payload["unrecognised_tables"]:
            g.pop("sample_rows", None)
        if len(_CONTRACT) + len(json.dumps(payload)) > MAX_PROMPT_CHARS:
            if payload["recognised_but_unmapped_columns"]:
                payload["recognised_but_unmapped_columns"].pop()
            elif payload["unrecognised_tables"]:
                payload["unrecognised_tables"].pop()
        prompt = _CONTRACT + "\n" + json.dumps(payload, ensure_ascii=False,
                                               indent=1)
    return prompt


def _ai_call(prompt):
    """The SAME client the Load Assistant's ai_propose_tables uses: key via
    bulk_dir_loader._ai_api_key, model via DATAVIEW_AI_MODEL. Falls back to
    the SDK's own env-var key so a standalone docshape deployment (no
    dataview present) still works."""
    import anthropic
    key = None
    try:
        from dataview.import_data.bulk_dir_loader import _ai_api_key
        key = _ai_api_key()
    except Exception:
        pass
    model = os.environ.get("DATAVIEW_AI_MODEL", MODEL)
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    msg = client.messages.create(
        model=model, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content
                   if getattr(b, "type", "") == "text")


def _strip_fences(t):
    t = (t or "").strip()
    t = re.sub(r"^```[A-Za-z]*\s*", "", t)
    return re.sub(r"\s*```$", "", t)


def _first_json(t):
    t = _strip_fences(t)
    i = t.find("{")
    if i < 0:
        head = repr(t[:160] + ("…" if len(t) > 160 else "")) if t.strip() \
            else "(empty reply — possibly the whole budget went to " \
                 "thinking; retrying usually clears it)"
        raise ValueError(f"no JSON object in reply — model said: {head}")
    obj, _ = json.JSONDecoder().raw_decode(t[i:])
    return obj


def ask_ai(prompt, call=_ai_call):
    """Fence strip · raw_decode first object · ONE self-repair retry."""
    raw = call(prompt)
    try:
        return _first_json(raw)
    except Exception as e:
        raw = call(prompt + f"\n\nYour previous reply could not be parsed "
                            f"({e}). Reply again with ONLY the JSON object — "
                            f"your very first character must be '{{'.")
        return _first_json(raw)


def propose(pack, results, call=_ai_call):
    """Analysis groups -> vetted AI proposals. Returns (proposals, notes,
    summary). Proposals are plain dicts; notes[i] is the deterministic
    warnings for proposal i (may be empty)."""
    unknown, partial = group_unresolved(pack, results)
    if not unknown and not partial:
        return [], {}, "Nothing unresolved — every table recognised and mapped."
    reply = ask_ai(build_prompt(pack, unknown, partial), call=call)
    proposals = [p for p in (reply.get("proposals") or [])
                 if isinstance(p, dict) and p.get("kind") in
                 ("alias", "new_field", "shape")]
    return proposals, vet(pack, proposals), str(reply.get("summary") or "")


# ═════════════════════════════════════════════════════════════════════════ #
# 4 · VETTING  (deterministic guards — the AI is not exempt from the lessons)
# ═════════════════════════════════════════════════════════════════════════ #
def _proposed_fields(proposals):
    out = set()
    for p in proposals:
        if p.get("kind") == "new_field":
            out.add(p.get("field"))
    return out


def vet(pack, proposals):
    """Warnings per proposal, from the scars this system already carries:
    an alias that already resolves elsewhere; a shape whose required set is a
    superset of an existing shape's (that shape keeps winning the tie-break —
    the perforations-vs-formation_tops lesson); required fields that do not
    discriminate; fields nothing defines."""
    eng = Recogniser(pack)
    new_fields = _proposed_fields(proposals)
    notes = {}
    for i, p in enumerate(proposals):
        msgs = []
        kind = p.get("kind")
        if kind == "alias":
            cur = eng.field_for(p.get("alias", ""))
            if cur and cur != p.get("field"):
                msgs.append(f"'{p.get('alias')}' already resolves to "
                            f"{cur!r} — approving creates an ambiguity")
            if p.get("field") not in pack.fields \
                    and p.get("field") not in new_fields:
                msgs.append(f"field {p.get('field')!r} does not exist — "
                            f"this will CREATE it with one alias")
        elif kind == "new_field":
            if p.get("field") in pack.fields:
                msgs.append(f"field {p.get('field')!r} already exists — "
                            f"aliases will extend it")
            for a in p.get("aliases") or []:
                cur = eng.field_for(a)
                if cur and cur != p.get("field"):
                    msgs.append(f"'{a}' already resolves to {cur!r}")
        elif kind == "shape":
            req = set(p.get("required") or [])
            if not req:
                msgs.append("no required fields — matches nothing")
            for n, spec in pack.shapes.items():
                if n != p.get("name") and set(spec["required"]) <= req:
                    msgs.append(f"required ⊇ {n}'s required — {n} may keep "
                                f"winning the (score, optional-hits) tie-break")
            others = set()
            for n, spec in pack.shapes.items():
                if n != p.get("name"):
                    others |= set(spec["required"])
            if req and not [f for f in req if f not in others]:
                msgs.append("no required field is unique to this shape — "
                            "not discriminating")
            known = set(pack.fields) | new_fields
            missing = [f for f in list(req) + list(p.get("optional") or [])
                       if f not in known]
            if missing:
                msgs.append(f"fields with no aliases anywhere: {missing} — "
                            f"the shape can never match without them")
        notes[i] = msgs
    return notes


# ═════════════════════════════════════════════════════════════════════════ #
# 5 · SANDBOX  (approved proposals land here — never in the pack)
# ═════════════════════════════════════════════════════════════════════════ #
def apply_to_sandbox(pack_name, proposals, approved, by="doc_flow"):
    """Write the approved proposals into <pack>_sandbox.json.
    new_field entries are applied FIRST so a shape approved in the same batch
    finds its fields. Returns (sandbox_path, entries_written)."""
    sp = sandbox_path(pack_name)
    sb = load_overlay(sp) or empty(pack_name)
    order = {"new_field": 0, "alias": 1, "shape": 2}
    n = 0
    for i in sorted(approved, key=lambda i: order.get(
            proposals[i].get("kind"), 3)):
        p = proposals[i]
        kind = p.get("kind")
        if kind == "alias":
            add_alias(sb, p["field"], p["alias"], by=by, note=p.get("why"))
            n += 1
        elif kind == "new_field":
            for a in p.get("aliases") or []:
                add_alias(sb, p["field"], a, by=by, note=p.get("why"))
            if p.get("numeric"):
                set_numeric(sb, [p["field"]], by=by)
            n += 1
        elif kind == "shape":
            req = list(p.get("required") or [])
            add_shape(sb, p["name"], req, p.get("optional"),
                      p.get("target"), p.get("min_required", len(req)),
                      p.get("columns"), by=by, note=p.get("why"))
            n += 1
    save_overlay(sb, sp)
    return sp, n


def recheck(pack_name, results):
    """Re-identify every analysed header with the sandbox ON and diff.
    Identification depends only on the header, so no re-extraction and no
    retained rows. Also validates the layered pack — a sandbox entry that
    makes the vocabulary incoherent is reported, not discovered later.
    Returns (diff_rows, changed_count, validation_problems)."""
    pack, _ov, _op, _sb, _sp = load_layered(pack_name, use_sandbox=True)
    problems = []
    validate_pack(pack, log=problems.append)
    problems = [p for p in problems if p.lstrip().startswith("!!")]
    eng = Recogniser(pack)
    diff, changed = [], 0
    for r in results:
        if r.get("error") or not r.get("header"):
            continue
        shape, score, cm = eng.identify(r["header"])
        after = shape or "UNKNOWN"
        # COLUMN COVERAGE COUNTS AS A CHANGE. Comparing shape+score only
        # made widening a shape to claim a column look like "0 table(s)
        # change" — the identification is identical BECAUSE that is what
        # widening is supposed to preserve. The whole point of the claim
        # was the column, so the column has to be in the diff.
        n_before = len(r.get("columns") or {})
        n_after = len(cm or {})
        n_cols = len(r.get("header") or [])
        moved = (after != r["shape"]) or (n_after != n_before)
        row = {"file": os.path.basename(r["file"]), "table": r["table"],
               "before": f"{r['shape']} ({r['score']:.2f}) · "
                         f"{n_before}/{n_cols} cols",
               "after": f"{after} ({score:.2f}) · {n_after}/{n_cols} cols",
               "changed": moved}
        changed += moved
        diff.append(row)
    return diff, changed, problems


def promote(pack_name):
    """Fold the sandbox into the overlay, save, and only THEN delete the
    sandbox — a failed write never loses the work. Returns entries folded."""
    sp = sandbox_path(pack_name)
    sb = load_overlay(sp)
    if not sb:
        return 0
    op = default_path(pack_name)
    ov = load_overlay(op) or empty(pack_name)
    ov, n = promote_sandbox(sb, ov)
    save_overlay(ov, op)
    os.remove(sp)
    return n


def discard(pack_name):
    sp = sandbox_path(pack_name)
    if os.path.exists(sp):
        os.remove(sp)
        return True
    return False


# ═════════════════════════════════════════════════════════════════════════ #
# 6 · STREAMLIT PAGE
# ═════════════════════════════════════════════════════════════════════════ #
# ═════════════════════════════════════════════════════════════════════════ #
# 6a · PLAIN LANGUAGE  (what a reader sees; the technical view is opt-in)
# ═════════════════════════════════════════════════════════════════════════ #
# A shape name is a vocabulary term. "core_run (1.00) · 3 unmapped col(s)"
# tells the person who wrote the vocabulary a great deal and everyone else
# nothing. Same information, said as a count of things found.
SHAPE_LABEL = {
    "well_header": ("well header", "well headers"),
    "formation_tops": ("formation top", "formation tops"),
    "directional_survey": ("survey station", "survey stations"),
    "curve_summary": ("log curve", "log curves"),
    "curve_readings": ("curve reading", "curve readings"),
    "log_run_header": ("logging run", "logging runs"),
    "core_run": ("core run", "core runs"),
    "core_sample": ("core sample", "core samples"),
    "casing": ("casing string", "casing strings"),
    "cement_bond": ("cement bond record", "cement bond records"),
    "perforations": ("perforation interval", "perforation intervals"),
    "frac_stage": ("frac stage", "frac stages"),
    "production": ("production period", "production periods"),
    "dst": ("drill stem test", "drill stem tests"),
    "fluid_sample": ("fluid sample", "fluid samples"),
    "fluid_contacts": ("fluid contact", "fluid contacts"),
    "petrophysics": ("petrophysical zone", "petrophysical zones"),
    "rft_pressure_test": ("pressure test station", "pressure test stations"),
    "parameter_stats": ("recorded parameter", "recorded parameters"),
    "key_value": ("reported value", "reported values"),
    "daily_time_log": ("operations entry", "operations entries"),
    "eow_summary_pairs": ("well summary", "well summaries"),
    "operations_npt": ("downtime event", "downtime events"),
}


# Shapes written as label/value PAIRS describe ONE thing over many rows —
# a well header with eight rows is one well, not eight. Reporting the row
# count for these reads as nonsense ("8 well headers"), so they are named
# without a number until the pivot collapses them.
PAIR_SHAPES = {"well_header", "log_run_header", "eow_summary_pairs",
               "doc_header_block"}


def _label(shape, n):
    one, many = SHAPE_LABEL.get(
        shape, (shape.replace("_", " "), shape.replace("_", " ")))
    if shape in PAIR_SHAPES:
        return one
    return f"{n} {one if n == 1 else many}"


def plain_summary(rows):
    """"18 survey stations · 7 log curves · 2 formation tops" — what was
    found, in the words of the thing found rather than the machinery."""
    got = [(r["shape"], r.get("row_count") or 0) for r in rows
           if not r.get("error") and r.get("shape")
           and r["shape"] != "UNKNOWN"]
    got.sort(key=lambda x: -x[1])
    return " · ".join(_label(s, n) for s, n in got)


# ── the flag queue ─────────────────────────────────────────────────────── #
def flags_path():
    return os.path.join(_scratch_dir(), "flags.json")


def load_flags():
    try:
        with open(flags_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def add_flag(r, note=""):
    """Record 'this table looks wrong' for whoever owns the vocabulary.

    A reader should never have to know what a shape is to report that
    something is off. The flag carries the evidence the vocabulary owner
    will need — file, table, header, samples, what it matched — so nobody
    has to re-open the document to act on it."""
    fl = load_flags()
    fl.append({
        "file": r.get("file"), "table": r.get("table"),
        "shape": r.get("shape"), "header": r.get("header"),
        "sample": r.get("sample"), "note": note,
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    tmp = flags_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fl, f, indent=1, ensure_ascii=False)
    os.replace(tmp, flags_path())
    return len(fl)


def clear_flags():
    if os.path.exists(flags_path()):
        os.remove(flags_path())


# ═════════════════════════════════════════════════════════════════════════ #
# 6b · VOCABULARY REQUEST  (send the problem to whoever owns the pack)
# ═════════════════════════════════════════════════════════════════════════ #
def _titles_for(path):
    """{table_name: section title} — the vendor's own words for each table.
    Best effort; an older readers module without the titles sink just
    returns nothing."""
    try:
        from docshape.readers.tables import raw_tables as _rt
        t = {}
        _rt(path, titles=t)
        return t
    except TypeError:
        return {}
    except Exception:
        return {}


def _vocab_hash(pack):
    try:
        from dataview.file_catalog.doc_assess import vocab_hash
        return vocab_hash(pack)
    except Exception:
        return None


def build_request(pack_name, results, note=""):
    """Everything needed to diagnose and fix a vocabulary gap, in one file.

    The point is that nobody has to reconstruct context: which vocabulary
    version this deployment runs, what the tables actually look like, what
    the vendor calls them, how close the existing shapes came, what has
    been taught locally, and what a reader said was wrong. Without this it
    is screenshots and guesswork — which is how a whole afternoon goes to
    'is the file you're running the file you think you're running'.

    Contains no ROW DATA beyond the three sample rows already shown on
    screen; it is evidence about table SHAPES, not about wells.
    """
    pack, ov, _op, _sb, _sp = load_layered(pack_name, use_sandbox=False)
    eng = Recogniser(pack)

    # unknown tables, grouped the way a fix would group them
    groups = {}
    unclaimed = {}
    titles_cache = {}
    for r in results:
        if r.get("error") or not r.get("header"):
            continue
        f = r["file"]
        if f not in titles_cache:
            titles_cache[f] = _titles_for(f)
        title = titles_cache[f].get(r.get("table"), "")
        if r["shape"] == "UNKNOWN":
            fields, unres = [], []
            for _i, cell, fld in eng.header_fields(r["header"]):
                (fields if fld else unres).append(fld or str(cell))
            # Key on WHAT RESOLVED plus how many cells didn't — never on
            # the unresolved wordings themselves. Two vendors writing
            # "Mud Weight (ppg)" and "MW (ppg)" are the same table asking
            # for the same fix; keying on wording splits them into two
            # requests and hides that the fix unlocks both.
            key = (tuple(sorted(set(fields))), len(unres))
            g = groups.setdefault(key, {
                "resolves": sorted(set(fields)),
                "unresolved_cell_count": len(unres),
                "unresolved_wordings": [],
                "section_titles": [], "headers": [], "samples": [],
                "files": [], "near_misses": near_misses(eng, r["header"]),
            })
            for w in unres:
                if w.strip() and w not in g["unresolved_wordings"]:
                    g["unresolved_wordings"].append(w)
            g["files"].append(f)
            if title and title not in g["section_titles"]:
                g["section_titles"].append(title)
            if len(g["headers"]) < 3:
                g["headers"].append(r["header"])
                g["samples"].append(r.get("sample") or [])
        elif r.get("unmapped"):
            u = unclaimed.setdefault(r["shape"], {})
            for c in r["unmapped"]:
                u[str(c)] = u.get(str(c), 0) + 1

    return {
        "kind": "docshape_vocabulary_request",
        "created": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "pack": {
            "name": pack_name,
            "vocab_hash": _vocab_hash(pack),
            "field_count": len(pack.fields),
            "shape_names": sorted(pack.shapes),
        },
        "local_overlay": {
            "fields": (ov or {}).get("fields", {}),
            "shapes": (ov or {}).get("shapes", {}),
            "disabled": (ov or {}).get("disabled", []),
        },
        "reader_flags": load_flags(),
        "unknown_table_groups": [
            {**g, "document_count": len(set(g["files"])),
             "files": sorted(set(g["files"]))[:5]}
            for g in sorted(groups.values(),
                            key=lambda g: -len(set(g["files"])))
        ],
        "unclaimed_columns": {
            s: sorted(cols.items(), key=lambda kv: -kv[1])[:12]
            for s, cols in unclaimed.items()},
        "documents_seen": len({r["file"] for r in results}),
        "documents_unreadable": sorted(
            {os.path.basename(r["file"]) for r in results
             if r.get("error") or r.get("empty")}),
    }


def _snapshot_from(results, pack_name):
    """A vocab_check baseline built from the walk already done — the same
    shape the `snapshot` command writes, without re-reading anything."""
    pack, _ov, _op, _sb, _sp = load_layered(pack_name, use_sandbox=False)
    entries = [{
        "file": os.path.basename(r["file"]), "table": r.get("table"),
        "header": r.get("header") or [], "shape": r.get("shape") or "UNKNOWN",
        "score": r.get("score", 0.0), "rows": r.get("row_count", 0),
    } for r in results if not r.get("error") and r.get("header")]
    return {
        "kind": "docshape_snapshot",
        "created": datetime.now().isoformat(timespec="seconds"),
        "pack": pack_name,
        "shape_names": sorted(pack.shapes),
        "documents": len({e["file"] for e in entries}),
        "entries": entries,
        "unreadable": [{"file": os.path.basename(r["file"]),
                        "error": r.get("error") or "no tables"}
                       for r in results
                       if r.get("error") or r.get("empty")],
    }


def render(engine=None, target=None):        # engine accepted, unused — the
    import streamlit as st                   # page touches no database
    ss = st.session_state

    st.subheader("📄 Document Assistant")
    st.caption("Reads and reports — writes **no data** anywhere. The only "
               "thing this page changes is the vocabulary (sandbox → "
               "overlay). Capture and promotion stay separate acts.")

    ss.setdefault("df_pack", PACK_NAME_DEFAULT)
    c1, c2 = st.columns([3, 1])
    with c1:
        if target:
            # Embedded by the Load Assistant: the host already owns the
            # path box — repeating it here would be two boxes with one
            # meaning. Show what we were handed instead.
            st.caption(f"Working set: `{target}`")
            path = target
        else:
            path = clean_path(st.text_input(
                "Folder or file", key="df_path",
                help="Quotes from Explorer's 'Copy as path' are stripped "
                     "for you."))
    with c2:
        try:
            from docshape.packs import available
            opts = available() or [PACK_NAME_DEFAULT]
        except Exception:
            opts = [PACK_NAME_DEFAULT]
        pack_name = st.selectbox("Vocabulary", opts,
                                 index=opts.index(ss["df_pack"])
                                 if ss["df_pack"] in opts else 0,
                                 key="df_pack_pick")
        ss["df_pack"] = pack_name

    up = None if target else st.file_uploader(
        "…or drop documents", accept_multiple_files=True, key="df_up")

    if st.button("🔎 Extract", key="df_extract", type="primary"):
        files = []
        if path and os.path.exists(path):
            files = collect_files(path)
        for u in up or []:
            dest = os.path.join(_scratch_dir(), u.name)
            with open(dest, "wb") as f:
                f.write(u.getbuffer())
            files.append(dest)
        if not files:
            st.warning("Nothing to read — give a path or drop files.")
        else:
            pack, _ov, _op, _sb, _sp = load_layered(pack_name,
                                                    use_sandbox=True)
            ss["df_results"] = analyse(pack, files)
            ss["df_props"], ss["df_notes"], ss["df_summary"] = None, {}, ""
            ss["df_qi"], ss["df_triage"] = 0, set()      # fresh walk
            ss["df_walked"] = False
            ss["df_ver"] = ss.get("df_ver", 0) + 1       # new checkbox keys

    results = ss.get("df_results")
    if not results:
        return

    # ── WHICH JOB IS THIS? ───────────────────────────────────────────────
    # Reading documents and engineering vocabulary are two different jobs
    # done by two different people on two different schedules. They were
    # on one screen, which made the everyday job look like the rare one.
    # Default is the everyday job; the rest is one toggle away.
    vocab = st.toggle(
        "🔧 Vocabulary tools", value=bool(ss.get("df_vocab")),
        key="df_vocab_tog",
        help="Teach the recogniser new wordings and table types. This is "
             "an occasional admin task — leave it off to just read "
             "documents.")
    ss["df_vocab"] = vocab

    if not vocab:
        _simple_walk(st, ss, results)
        return

    # In tools mode the reader-reported flags come FIRST: they are the
    # queue this mode exists to work, and a report from someone who read
    # the document beats a signature ranked by frequency.
    _flags = load_flags()
    if _flags:
        with st.expander(f"⚑ {len(_flags)} item(s) reported by readers",
                         expanded=True):
            for i, fl in enumerate(_flags[-12:]):
                st.markdown(
                    f"**{os.path.basename(str(fl.get('file') or ''))}** · "
                    f"{fl.get('table')} · "
                    f"{'not recognised' if fl.get('shape') == 'UNKNOWN' else fl.get('shape')}"
                    + (f" — “{fl['note']}”" if fl.get("note") else ""))
                if fl.get("header"):
                    st.caption("header: " + " · ".join(
                        str(h) for h in fl["header"][:8]))
            if st.button("🗑 Clear the flag list", key="df_clearflags"):
                clear_flags()
                st.rerun()

    # ── send the problem to whoever owns the pack ────────────────────────
    # Outside the flags block on purpose: an unrecognised table is worth
    # sending whether or not a reader got around to flagging it.
    with st.expander("📤 Send these to an expert", expanded=False):
        st.caption("Bundles everything a vocabulary fix needs — the "
                   "unrecognised tables grouped as a fix would group them, "
                   "the vendor's own section titles, how close existing "
                   "shapes came, what's been taught locally, the reader "
                   "flags, and which vocabulary version this deployment "
                   "runs. Table shapes only; no well data beyond the "
                   "sample rows already on screen.")
        req_note = st.text_input(
            "Anything to add?", key="df_req_note",
            placeholder="e.g. these are all from the same vendor's "
                        "2019-2021 reports")
        if st.button("📤 Build the request file", key="df_req_build"):
            try:
                req = build_request(pack_name, results, req_note)
                ss["df_req"] = json.dumps(req, indent=1, ensure_ascii=False)
                ss["df_req_n"] = len(req["unknown_table_groups"])
            except Exception as e:
                st.error(f"Couldn't build it: {type(e).__name__}: {e}")
        if ss.get("df_req"):
            st.success(f"{ss.get('df_req_n', 0)} unrecognised group(s) "
                       f"and {len(load_flags())} flag(s) packaged.")
            st.download_button(
                "⬇ 1. vocabulary_request.json",
                data=ss["df_req"], file_name="vocabulary_request.json",
                mime="application/json", key="df_req_dl")
            # The baseline turns "trust me" into "prove it": vocab_check
            # replays these headers against whatever pack comes back.
            try:
                snap = json.dumps(_snapshot_from(results, pack_name),
                                  indent=1, ensure_ascii=False)
                st.download_button(
                    "⬇ 2. baseline.json (so you can verify the answer)",
                    data=snap, file_name="baseline.json",
                    mime="application/json", key="df_base_dl")
            except Exception:
                pass
            st.markdown("**3.** Send those two files, your current "
                        "`petroleum.py`, and the sheet "
                        "`HOW_TO_ANSWER_A_VOCABULARY_REQUEST.md` "
                        "(in `docshape/`). Nothing to fill in — paste "
                        "the message below with them:")
            st.code(
                "Attached: a vocabulary request exported from our document "
                "reader, a baseline of what our documents currently read, "
                "and the pack we are running.\n\n"
                "Please follow HOW_TO_ANSWER_A_VOCABULARY_REQUEST.md and "
                "send back a complete petroleum.py.",
                language=None)
            st.caption("When it comes back, run the check before installing:")
            st.code("py -m dataview.file_catalog.vocab_check check "
                    "--pack-file petroleum.py --baseline baseline.json",
                    language=None)

    # -- the document QUEUE: one at a time, complete → next, gaps → triage - #
    # Perry's spec (Aug 1): "go one by one the document and what was
    # extracted. If complete go on. If missing section triage them." Same
    # rhythm as the load queue — advancing is an explicit click, and the
    # AI step runs over the TRIAGED set, not the whole folder.
    byfile = {}
    for r in results:
        byfile.setdefault(r["file"], []).append(r)
    files_o = list(byfile)

    def _doc_status(rs):
        if any(r.get("error") for r in rs):
            return "✗ failed"
        if any(r.get("shape") == "UNKNOWN" for r in rs):
            return "❓ gaps"
        if all(r.get("empty") for r in rs):
            return "∅ no tables"
        return "✔ complete"

    stat = {f: _doc_status(rs) for f, rs in byfile.items()}
    tri = ss.setdefault("df_triage", set())
    n_c = sum(1 for s in stat.values() if s == "✔ complete")
    n_g = sum(1 for s in stat.values() if s == "❓ gaps")
    n_e = sum(1 for s in stat.values() if s == "∅ no tables")
    n_x = sum(1 for s in stat.values() if s == "✗ failed")
    st.markdown(f"**{len(files_o)} document(s)** · ✔ {n_c} complete · "
                f"❓ {n_g} with gaps"
                + (f" · ∅ {n_e} no tables" if n_e else "")
                + (f" · ✗ {n_x} failed" if n_x else "")
                + (f" · ⚑ {len(tri)} triaged" if tri else ""))

    qi = max(0, min(int(ss.get("df_qi", 0)), len(files_o) - 1))
    f = files_o[qi]
    rs = byfile[f]
    st.markdown(f"### Document {qi + 1} of {len(files_o)} — "
                f"`{os.path.basename(f)}` · {stat[f]}"
                + (" · ⚑ triaged" if f in tri else ""))
    for r in rs:
        if r.get("error"):
            st.error(f"read failed: {r['error']}")
        elif r.get("empty"):
            st.caption("∅ no tables found (rasterised page, text boxes, or "
                       "genuinely tableless — a vocabulary change can't fix "
                       "this one; it's an extraction finding)")
        elif r["shape"] == "UNKNOWN":
            with st.expander(f"❓ {r['table']} — UNRECOGNISED · "
                             f"{r['row_count']} rows", expanded=True):
                _table_card(st, r)
                _fk = f"{ss['df_ver']}_{qi}_{r['table']}"
                ins = st.text_input(
                    "🗣 Describe the fix and let the assistant draft it",
                    key=f"df_say_{_fk}",
                    placeholder="e.g. Producing Horizon means formation; "
                                "this is a bit record table → cat_well_bit")
                if st.button("🤖 Translate to corrections",
                             key=f"df_sayb_{_fk}", disabled=not ins.strip()):
                    pack, *_ = load_layered(pack_name, use_sandbox=True)
                    with st.spinner("Translating…"):
                        try:
                            props, notes, summary = propose_from_instruction(
                                pack, r, ins)
                            ss["df_props"], ss["df_notes"] = props, notes
                            ss["df_summary"] = summary
                            ss["df_ver"] += 1
                            st.success("Drafted — review and approve in the "
                                       "proposals panel below.")
                        except Exception as e:
                            st.error(f"AI call failed: {e}")
                _teach_form(st, ss, pack_name, engine, r, _fk)
        else:
            with st.expander(f"✅ {r['table']} → **{r['shape']}** "
                             f"({r['score']:.2f}) · {r['row_count']} rows"
                             + (f" · {len(r['unmapped'])} unmapped col(s)"
                                if r.get("unmapped") else ""),
                             expanded=bool(r.get("unmapped"))):
                _table_card(st, r)
                if r.get("unmapped"):
                    _claim_form(st, ss, pack_name, engine, r,
                                f"{ss['df_ver']}_{qi}_{r['table']}")
                # RECOGNISED IS NOT THE SAME AS RIGHT. A table can identify
                # at 1.00 and still be wrong — mapped to the wrong shape, a
                # column pointed at the wrong field, rows the reader knows
                # are missing. Until now the describe box lived only on
                # UNRECOGNISED cards, so there was nowhere to say so.
                _rk = f"{ss['df_ver']}_{qi}_{r['table']}_ok"
                _ins2 = st.text_input(
                    "🗣 Something wrong with this one?",
                    key=f"df_saygood_{_rk}",
                    placeholder="e.g. these are perforations, not tops · "
                                "'Fluid' should map to fluid type")
                if st.button("🤖 Translate to corrections",
                             key=f"df_saygoodb_{_rk}",
                             disabled=not _ins2.strip()):
                    pack, *_ = load_layered(pack_name, use_sandbox=True)
                    with st.spinner("Translating…"):
                        try:
                            props, notes, summary = propose_from_instruction(
                                pack, r, _ins2)
                            ss["df_props"], ss["df_notes"] = props, notes
                            ss["df_summary"] = summary
                            ss["df_ver"] += 1
                            st.success("Drafted — review and approve in the "
                                       "proposals panel below.")
                        except Exception as e:
                            st.error(f"AI call failed: {e}")

    def _goto(i):
        ss["df_qi"] = max(0, min(i, len(files_o) - 1))
        st.rerun()

    cB, cN, cT, cS = st.columns([1, 1, 1.4, 1.6])
    if cB.button("⏮ Back", key="df_back", disabled=qi == 0):
        _goto(qi - 1)
    if cN.button("▶ Next", key="df_next", disabled=qi >= len(files_o) - 1):
        _goto(qi + 1)
    if cT.button("⚑ Triage & next", key="df_tri",
                 help="Mark this document's gaps for the assistant, move on."):
        tri.add(f)
        _goto(qi + 1)
    if cS.button("▶▶ Next incomplete", key="df_skip",
                 help="Jump past complete documents."):
        for j in range(qi + 1, len(files_o)):
            if stat[files_o[j]] != "✔ complete":
                _goto(j)
        st.info("No incomplete documents after this one.")

    # -- AI proposals over the TRIAGED set --------------------------------- #
    st.divider()
    tri_results = [r for r in results if r["file"] in tri] if tri else results
    tri_label = (f"the {len(tri)} triaged document(s)" if tri
                 else "everything unresolved")
    any_gap = any(not r.get("error") and r.get("shape") == "UNKNOWN"
                  for r in tri_results) or \
        any(r.get("unmapped") for r in tri_results if not r.get("error"))
    if any_gap:
        if st.button(f"🤖 Describe the needed changes for {tri_label}",
                     key="df_ai"):
            pack, _ov, _op, _sb, _sp = load_layered(pack_name,
                                                    use_sandbox=True)
            with st.spinner("Asking the assistant…"):
                try:
                    props, notes, summary = propose(pack, tri_results)
                    ss["df_props"], ss["df_notes"] = props, notes
                    ss["df_summary"] = summary
                    ss["df_ver"] += 1
                except Exception as e:
                    st.error(f"AI call failed: {e}")
    else:
        st.success("Every table recognised and fully mapped — nothing to "
                   "teach from this group.")

    props = ss.get("df_props")
    if props is not None:
        if ss.get("df_summary"):
            st.info(ss["df_summary"])
        if not props:
            st.caption("No changes proposed.")
        else:
            ver = ss["df_ver"]
            with _form(st, f"df_approve_{ver}"):
                st.markdown("**Proposed vocabulary changes** — approve what "
                            "is right; everything approved goes to the "
                            "**sandbox**, not the live vocabulary.")
                cats = _cat_tables(engine)
                for i, p in enumerate(props):
                    label = _proposal_label(p)
                    st.checkbox(label, key=f"df_ap_{ver}_{i}",
                                value=not ss.get("df_notes", {}).get(i))
                    if p.get("kind") == "shape":
                        # Where the rows LAND is a schema decision the AI
                        # deliberately leaves null — so it's a dropdown
                        # here, not a JSON edit later.
                        if cats:
                            cur = p.get("target")
                            opts = ["(none yet)"] + cats
                            st.selectbox(
                                "→ staging table", opts,
                                index=(opts.index(cur) if cur in opts else 0),
                                key=f"df_tg_{ver}_{i}")
                        else:
                            st.text_input("→ staging table (cat_…, blank = "
                                          "none)", value=p.get("target") or "",
                                          key=f"df_tg_{ver}_{i}")
                    if p.get("why"):
                        st.caption("↳ " + str(p["why"]))
                    for w in ss.get("df_notes", {}).get(i) or []:
                        st.warning(w)
                go = st.form_submit_button("✔ Approve → sandbox")
            if go:
                for i, p in enumerate(props):     # harvest targets first
                    if p.get("kind") == "shape":
                        t = str(ss.get(f"df_tg_{ver}_{i}") or "").strip()
                        p["target"] = None if t in ("", "(none yet)") else t
                approved = [i for i in range(len(props))
                            if ss.get(f"df_ap_{ver}_{i}")]
                if approved:
                    sp, n = apply_to_sandbox(pack_name, props, approved)
                    st.success(f"{n} change(s) written to "
                               f"{os.path.basename(sp)}")
                else:
                    st.warning("Nothing approved.")

    # -- verify + promote -------------------------------------------------- #
    sb = load_overlay(sandbox_path(pack_name))
    if sb and (sb.get("fields") or sb.get("shapes")):
        st.divider()
        st.markdown(f"🧪 **Sandbox**: {len(sb.get('fields') or {})} field "
                    f"entrie(s), {len(sb.get('shapes') or {})} shape(s) "
                    f"awaiting promotion.")
        if st.button("🔄 Re-check this group with the sandbox", key="df_re"):
            diff, changed, problems = recheck(pack_name, results)
            for pr in problems:
                st.error(pr)
            st.markdown(f"**{changed} table(s) change** — a proposal that "
                        f"changes nothing is worth knowing about too.")
            st.dataframe([d for d in diff if d["changed"]] or diff,
                         use_container_width=True)
        cA, cB = st.columns(2)
        with cA:
            if st.button("⬆ Promote sandbox to vocabulary", key="df_promote"):
                n = promote(pack_name)
                st.success(f"{n} entrie(s) folded into the overlay. "
                           f"Hit 🔎 Extract to re-walk this group against "
                           f"the updated vocabulary, or point at the next "
                           f"folder.")
                ss["df_props"] = None
        with cB:
            if st.button("🗑 Discard sandbox", key="df_discard"):
                discard(pack_name)
                st.info("Sandbox cleared — the overlay is untouched.")
                ss["df_props"] = None


def propose_from_instruction(pack, r, instruction, call=_ai_call):
    """The user DESCRIBES the fix ("Producing Horizon means formation;
    this is a bit record table") and the assistant translates it into the
    same proposal contract everything else uses. Their intent leads — the
    prompt forbids inventing corrections they didn't ask for — and the
    deterministic vet guards apply unchanged. Returns (proposals, notes,
    summary), same as propose()."""
    eng = Recogniser(pack)
    payload = {
        "user_instruction": str(instruction),
        "table": {
            "header": r["header"],
            "cells_already_resolving": [
                {"cell": c, "field": f}
                for _i, c, f in eng.header_fields(r["header"])],
            "closest_shapes": near_misses(eng, r["header"]),
            "sample_rows": r.get("sample") or []},
        "pack_fields": {f: al[:4] for f, al in sorted(pack.fields.items())},
        "shape_required_sets": {n: s["required"]
                                for n, s in sorted(pack.shapes.items())},
    }
    prompt = (_CONTRACT
              + "\nThe user has DESCRIBED the fix for this table in their "
                "own words. Translate their instruction into proposals — "
                "follow their intent exactly, resolve their informal "
                "wording against pack_fields, and do NOT invent "
                "corrections they did not ask for.\n"
              + json.dumps(payload, ensure_ascii=False, indent=1))
    reply = ask_ai(prompt, call=call)
    proposals = [p for p in (reply.get("proposals") or [])
                 if isinstance(p, dict) and p.get("kind") in
                 ("alias", "new_field", "shape")]
    return proposals, vet(pack, proposals), str(reply.get("summary") or "")


def _form(st, key):
    """A form where pressing Enter in a text box does NOT submit — typing a
    field name must never fire Teach half-filled. Older Streamlit lacks the
    parameter; degrade silently there."""
    import inspect
    try:
        if "enter_to_submit" in inspect.signature(st.form).parameters:
            return st.form(key=key, enter_to_submit=False)
    except Exception:
        pass
    return st.form(key=key)


def _simple_walk(st, ss, results):
    """The everyday view: one document at a time, what came out of it in
    plain words, and two buttons. No shapes, no dropdowns, no sandbox.

    The only judgement asked of a reader is the one they can actually
    make: does this look like what the document says? Everything they
    flag lands in a queue for whoever owns the vocabulary."""
    byfile = {}
    for r in results:
        byfile.setdefault(r["file"], []).append(r)
    files_o = list(byfile)
    flags = load_flags()
    flagged_files = {f["file"] for f in flags}

    def _state(rs):
        if any(r.get("error") for r in rs):
            return "unreadable"
        if all(r.get("empty") for r in rs):
            return "notables"
        if any(r.get("shape") == "UNKNOWN" for r in rs):
            return "partial"
        return "ok"

    stt = {f: _state(rs) for f, rs in byfile.items()}
    n_ok = sum(1 for v in stt.values() if v == "ok")
    n_part = sum(1 for v in stt.values() if v == "partial")
    n_none = sum(1 for v in stt.values() if v in ("notables", "unreadable"))

    st.markdown(f"**{len(files_o)} document(s)** · ✔ {n_ok} read cleanly"
                + (f" · ❓ {n_part} partly read" if n_part else "")
                + (f" · ⚠ {n_none} couldn't be read" if n_none else "")
                + (f" · ⚑ {len(flags)} flagged" if flags else ""))

    qi = max(0, min(int(ss.get("df_qi", 0)), len(files_o) - 1))
    f = files_o[qi]
    rs = byfile[f]
    state = stt[f]

    st.markdown(f"### {qi + 1} of {len(files_o)} — "
                f"`{os.path.basename(f)}`"
                + ("  ⚑" if f in flagged_files else ""))

    if state == "unreadable":
        st.warning("This file couldn't be opened. Nothing to check — it's "
                   "a file problem, not a reading problem.")
    elif state == "notables":
        st.warning("No tables found in this document — most likely a "
                   "scanned image rather than text. Nothing to check here.")
    else:
        summary = plain_summary(rs)
        if summary:
            st.success("Found: " + summary)
        unknown = [r for r in rs if r.get("shape") == "UNKNOWN"]
        if unknown:
            st.info(f"{len(unknown)} table(s) in this document weren't "
                    f"recognised. That's normal for a new layout — flag it "
                    f"and someone will teach it.")
        # The detail is available, never in the way.
        with st.expander("Show what was read", expanded=False):
            for r in rs:
                if r.get("error") or r.get("empty"):
                    continue
                if r["shape"] == "UNKNOWN":
                    st.markdown(f"**{r['table']}** — not recognised · "
                                f"{r['row_count']} rows")
                else:
                    st.markdown(f"**{r['table']}** — "
                                f"{_label(r['shape'], r['row_count'])}")
                if r.get("sample"):
                    st.table([dict(zip(r["header"], row))
                              for row in r["sample"]])

    def _goto(i):
        ss["df_qi"] = max(0, min(i, len(files_o) - 1))
        st.rerun()

    # "Looks right" is an ANSWER, not a navigation control, so it is never
    # disabled — on the last document there is simply nothing to advance
    # to, and it says "done" instead. Greying it out on the final document
    # (or on a folder holding one) left the reader with no way to say the
    # only thing they were asked to say.
    _last = qi >= len(files_o) - 1
    c1, c2, c3 = st.columns([1, 1.4, 1])
    if c1.button("⏮ Back", key="df_s_back", disabled=qi == 0):
        _goto(qi - 1)
    if c2.button("✔ Looks right — done" if _last else "✔ Looks right — next",
                 key="df_s_ok", type="primary"):
        if _last:
            ss["df_walked"] = True
            st.rerun()
        else:
            _goto(qi + 1)
    if c3.button("⚑ Something's wrong", key="df_s_flag"):
        ss["df_flagging"] = f

    if ss.get("df_flagging") == f:
        with _form(st, f"df_flagform_{qi}"):
            st.markdown("**What looks wrong?** One line is plenty — the "
                        "document, the table and the sample rows are "
                        "attached automatically.")
            note = st.text_input(
                "", key=f"df_flagnote_{qi}",
                placeholder="e.g. the depths are in the wrong columns · "
                            "this table was missed entirely")
            sent = st.form_submit_button("⚑ Send to review")
        if sent:
            for r in rs:
                if r.get("error") or r.get("empty"):
                    continue
                if r["shape"] == "UNKNOWN" or note:
                    add_flag(r, note)
            ss["df_flagging"] = None
            st.success("Flagged — thanks. Moving on.")
            _goto(qi + 1)

    if ss.get("df_walked"):
        st.success(f"✔ Walked all {len(files_o)} document(s)."
                   + (f" {len(flags)} flagged for review."
                      if flags else " Nothing flagged."))

    if flags:
        st.caption(f"⚑ {len(flags)} flagged item(s) waiting for review. "
                   f"Turn on 🔧 Vocabulary tools to work through them.")


def _cat_tables(engine):
    """Live cat_ staging tables for the target dropdown. Reads names only —
    this page still writes no data. Empty list (no engine, no permission)
    degrades to a free-text box, never blocks."""
    if engine is None:
        return []
    try:
        from sqlalchemy import text as _t
        with engine.connect() as cx:
            rows = cx.execute(_t(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME LIKE 'cat[_]%' ORDER BY TABLE_NAME"
            )).fetchall()
        return [str(x[0]) for x in rows]
    except Exception:
        return []


def _claim_form(st, ss, pack_name, engine, r, fkey):
    """Claim the UNMAPPED columns of a table that ALREADY identifies.

    The teach form answers "this table is nothing I know". This answers the
    commoner and, until now, unanswered case: the table IS recognised, the
    rows ARE extracted, and one column is being dropped on the floor.

    TWO CAUSES, and they need different fixes — which is why a single
    "add an alias" button would not have worked:

      1. The WORDING resolves to nothing. "Fluid" matches no alias, so no
         field claims it. Fix = an alias (or a new attribute if the
         vocabulary has no such concept at all).
      2. The wording DOES resolve, but THIS SHAPE does not claim that
         field. formation_tops may know `fluid_type` perfectly well and
         simply not list it. Fix = extend the shape's optional list, which
         means REPLACING the shape (add_shape replaces wholesale), so the
         existing required/optional/target/columns must be carried over or
         they are silently lost.

    This form does both, and says which one it is doing per column.
    """
    pack, _ov, _op, _sb, _sp = load_layered(pack_name, use_sandbox=True)
    eng_r = Recogniser(pack)
    flds = sorted(pack.fields)
    shape = r.get("shape")
    spec = dict((pack.shapes.get(shape) or {})) if shape else {}
    claimed = set(spec.get("required") or []) | set(spec.get("optional") or [])
    unmapped = [c for c in (r.get("unmapped") or [])]
    if not unmapped or not shape:
        return

    # IS THIS THE RIGHT SHAPE AT ALL? Claiming widens a shape, and widening
    # the WRONG shape is how "well_header" quietly becomes a completion
    # table for every future document. Before offering to claim, check
    # whether another shape would explain MORE of this header — if one
    # would, the honest advice is to fix the identification instead.
    _rival, _rn = None, len(r.get("columns") or {})
    for _nm, _sp in (pack.shapes or {}).items():
        if _nm == shape:
            continue
        _f = {eng_r.field_for(c) for c in r["header"]} - {None}
        _req = set(_sp.get("required") or [])
        if not _req or not _req <= _f:
            continue
        _cov = len(_f & (_req | set(_sp.get("optional") or [])))
        if _cov > _rn:
            _rival, _rn = _nm, _cov
    if _rival:
        st.warning(
            f"⚠ **`{_rival}` would explain more of this table** "
            f"({_rn} columns vs {len(r.get('columns') or {})}). Widening "
            f"`{shape}` here would teach it to claim tables like this one "
            f"permanently. Fix the identification first — describe it in the "
            f"box below, or send it to an expert — and only claim columns "
            f"once the shape is right.")

    st.markdown(f"**Claim the unmapped column(s)** — `{shape}` reads this "
                f"table but {len(unmapped)} column(s) reach nothing. Point "
                f"each at an attribute; the shape is widened to keep it.")
    with _form(st, f"df_claim_{fkey}"):
        picks, news = {}, {}
        for ci, cell in enumerate(unmapped):
            cur = eng_r.field_for(cell)
            why = ("resolves to " + cur + " — but " + shape + " doesn't "
                   "list it" if cur and cur not in claimed
                   else "resolves to nothing" if not cur else "already claimed")
            c1, c2 = st.columns([1.4, 1])
            opts = ["—"] + flds
            picks[str(cell)] = c1.selectbox(
                f"“{cell}” ({why})", opts,
                index=(opts.index(cur) if cur in opts else 0),
                key=f"df_cl_c{fkey}_{ci}")
            news[str(cell)] = c2.text_input(
                "…or a NEW attribute (snake_case)", key=f"df_cl_n{fkey}_{ci}")
        go = st.form_submit_button("✔ Claim → sandbox")
    if not go:
        return

    props, notes = [], []
    add_opt = []
    for cell, fld in picks.items():
        new = (news.get(cell) or "").strip()
        target = new or (fld if fld != "—" else "")
        if not target:
            continue
        if new:
            # The column's own wording is the alias — it is the wording the
            # document actually uses, which is the only kind worth storing.
            props.append({"kind": "new_field", "field": new,
                          "aliases": [str(cell)], "numeric": False,
                          "why": f"claimed on {shape}"})
            notes.append(f"new attribute {new} ← “{cell}”")
        elif eng_r.field_for(cell) != fld:
            props.append({"kind": "alias", "field": fld, "alias": str(cell),
                          "why": f"claimed on {shape}"})
            notes.append(f"“{cell}” → {fld}")
        if target not in claimed:
            add_opt.append(target)
            notes.append(f"{shape} now keeps {target}")

    if add_opt:
        # REPLACE the shape carrying everything it already had. Dropping
        # required/target/columns here would quietly redefine the table
        # type while appearing to make a small additive change.
        props.append({
            "kind": "shape", "name": shape,
            "required": list(spec.get("required") or []),
            "optional": sorted(set(spec.get("optional") or []) | set(add_opt)),
            "target": spec.get("target"),
            "min_required": spec.get("min_required",
                                     len(spec.get("required") or [])),
            "columns": dict(spec.get("columns") or {}),
            "why": f"widened to keep {', '.join(sorted(set(add_opt)))}"})

    if not props:
        st.warning("Nothing to claim — pick an attribute or name a new one.")
        return
    for i, msgs in vet(pack, props).items():
        for w in msgs:
            st.warning(w)
    sp, n = apply_to_sandbox(pack_name, props, list(range(len(props))))
    for t in notes:
        st.write("· " + t)
    st.success(f"{n} change(s) → {os.path.basename(sp)} — use 🔄 Re-check "
               f"below to prove them, then ⬆ Promote.")


def _teach_form(st, ss, pack_name, engine, r, fkey):
    """Correct an unrecognised table RIGHT ON ITS CARD.

    This is the answer to "what good is showing me something unidentified
    with no way to identify it" — every choice here is a VOCABULARY entry
    (an alias, an attribute, a shape) written to the sandbox, so the fix
    applies to every future document with that wording, not to this one
    file. The 🤖 button remains the accelerator; this is the manual path
    for when its proposals are wrong or absent."""
    pack, _ov, _op, _sb, _sp = load_layered(pack_name, use_sandbox=True)
    flds = sorted(pack.fields)
    eng_r = Recogniser(pack)
    cats = _cat_tables(engine)
    with _form(st, f"df_fix_{fkey}"):
        st.markdown("**Teach it here** — each header wording you point at a "
                    "field becomes an alias; name a shape and this table "
                    "type is defined. Sandbox first, promote when proven.")
        picks = {}
        for ci, cell in enumerate(r["header"]):
            cur = eng_r.field_for(cell)
            opts = ["—"] + flds
            picks[str(cell)] = st.selectbox(
                f"“{cell}”", opts,
                index=(opts.index(cur) if cur in opts else 0),
                key=f"df_fx_c{fkey}_{ci}")
        c1, c2, c3 = st.columns([1.2, 1.6, 0.6])
        nf_name = c1.text_input("➕ new attribute (snake_case)",
                                key=f"df_fx_nf{fkey}")
        nf_al = c2.text_input("its wordings, comma-separated",
                              key=f"df_fx_na{fkey}")
        nf_num = c3.checkbox("numeric", key=f"df_fx_nn{fkey}")
        st.markdown("**Define the shape** (what this table IS)")
        s1, s2 = st.columns(2)
        sh_name = s1.text_input("shape name (snake_case)",
                                key=f"df_fx_sn{fkey}")
        if cats:
            sh_tgt = s2.selectbox("→ staging table", ["(none yet)"] + cats,
                                  key=f"df_fx_st{fkey}")
            sh_tgt = None if sh_tgt == "(none yet)" else sh_tgt
        else:
            sh_tgt = s2.text_input("→ staging table (cat_…, blank = none)",
                                   key=f"df_fx_st{fkey}") or None
        sh_req = st.multiselect(
            "required — pick fields that DISCRIMINATE (a new attribute "
            "above is added automatically)", flds, key=f"df_fx_sr{fkey}")
        sh_opt = st.multiselect("optional", flds, key=f"df_fx_so{fkey}")
        go = st.form_submit_button("✔ Teach → sandbox")
    if not go:
        return
    props = []
    if nf_name.strip():
        props.append({"kind": "new_field", "field": nf_name.strip(),
                      "aliases": [a.strip() for a in nf_al.split(",")
                                  if a.strip()],
                      "numeric": bool(nf_num), "why": "taught on the card"})
    for cell, f in picks.items():
        if f != "—" and eng_r.field_for(cell) != f:
            props.append({"kind": "alias", "field": f, "alias": cell,
                          "why": "taught on the card"})
    if sh_name.strip():
        req = list(sh_req)
        if nf_name.strip() and nf_name.strip() not in req:
            req.append(nf_name.strip())     # a new attribute discriminates
        props.append({"kind": "shape", "name": sh_name.strip(),
                      "required": req, "optional": list(sh_opt),
                      "target": sh_tgt, "columns": {},
                      "why": "taught on the card"})
    if not props:
        st.warning("Nothing to teach — point a wording at a field, add an "
                   "attribute, or name a shape.")
        return
    for i, msgs in vet(pack, props).items():   # the guards apply to YOU too
        for w in msgs:
            st.warning(w)
    sp, n = apply_to_sandbox(pack_name, props, list(range(len(props))))
    st.success(f"{n} change(s) → {os.path.basename(sp)} — use 🔄 Re-check "
               f"below to prove them, then ⬆ Promote.")


def _table_card(st, r):
    if r.get("columns"):
        st.write({f: c for f, c in r["columns"].items()})
    if r.get("unmapped"):
        st.caption("Unmapped columns: " + " · ".join(r["unmapped"]))
    if r.get("sample"):
        # SAY WHAT THE SAMPLE IS A SAMPLE OF. "Sample values" over three
        # rows of an eight-row table reads as "three rows were extracted",
        # and the reader's job is to spot exactly that kind of loss — so
        # the label must not manufacture a false alarm.
        _n = r.get("row_count") or len(r["sample"])
        _s = len(r["sample"])
        st.caption(f"First {_s} of {_n} row(s)" if _n > _s
                   else f"All {_n} row(s)")
        st.table([dict(zip(r["header"], row)) for row in r["sample"]])


def _proposal_label(p):
    k = p.get("kind")
    if k == "alias":
        return f"ALIAS · '{p.get('alias')}' means **{p.get('field')}**"
    if k == "new_field":
        return (f"NEW FIELD · **{p.get('field')}**"
                f"{' (numeric)' if p.get('numeric') else ''} — "
                + ", ".join(f"'{a}'" for a in p.get("aliases") or []))
    return (f"NEW SHAPE · **{p.get('name')}** requires "
            f"{p.get('required')} → {p.get('target') or '(no target)'}")


def clean_path(p):
    """A pasted path, as pasted. Explorer's "Copy as path" wraps in double
    quotes, PowerShell's Copy-as-path can add a leading `& `, and Word or
    a chat window may have turned the quotes into smart ones. Every one of
    those makes os.path.exists say no about a file that is plainly there,
    and the operator gets to hunt an invisible character.
    """
    s = str(p or "").strip()
    if s.startswith("& "):                      # PowerShell call operator
        s = s[2:].strip()
    for q in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        if s.startswith(q):
            s = s[1:]
        if s.endswith(q):
            s = s[:-1]
    s = s.strip()
    return os.path.expandvars(os.path.expanduser(s)) if s else s


def _scratch_dir():
    d = os.path.join(_HERE, "_doc_flow_uploads")
    os.makedirs(d, exist_ok=True)
    return d


# ═════════════════════════════════════════════════════════════════════════ #
# 7 · CLI  (verify the reader seam before wiring any UI)
# ═════════════════════════════════════════════════════════════════════════ #
def main(argv=None):
    ap = argparse.ArgumentParser(description="doc_flow — extract, report, "
                                             "and (with --ai) propose")
    ap.add_argument("path")
    ap.add_argument("--pack", default=PACK_NAME_DEFAULT)
    ap.add_argument("--ai", action="store_true",
                    help="also ask the assistant for proposals")
    a = ap.parse_args(argv)
    a.path = clean_path(a.path)
    if not os.path.exists(a.path):
        ap.error(f"path not found: {a.path}")   # a typo'd path must FAIL,
                                                # not report an honest zero

    pack, _ov, _op, _sb, _sp = load_layered(a.pack, use_sandbox=True)
    files = collect_files(a.path)
    print(f"{len(files)} document(s)")
    if not files:
        print("  (nothing with a readable extension directly under that "
              "path — pdf/docx/xlsx)")
        return 1
    results = analyse(pack, files)
    for r in results:
        if r.get("error"):
            print(f"  ✗ {os.path.basename(r['file'])}: {r['error']}")
        elif r.get("empty"):
            print(f"  ∅ {os.path.basename(r['file']):40} no tables found")
        else:
            print(f"  {os.path.basename(r['file']):40} {r['table']:12} "
                  f"{r['shape']:22} {r['score']:.2f}  {r['row_count']} rows")
    unknown, partial = group_unresolved(pack, results)
    for g in unknown:
        print(f"\nUNRECOGNISED in {len(g['files'])} document(s): "
              f"{g['header']}")
        for nm in g["near_misses"]:
            print(f"    close: {nm['shape']} ({nm['have']:.2f}) "
                  f"missing {nm['missing']}")
    if a.ai and (unknown or partial):
        props, notes, summary = propose(pack, results)
        print(f"\nAI: {summary}")
        for i, p in enumerate(props):
            print(f"  [{i}] {_proposal_label(p)}")
            for w in notes.get(i) or []:
                print(f"       ⚠ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
