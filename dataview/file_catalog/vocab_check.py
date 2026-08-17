"""
vocab_check.py — is this proposed vocabulary safe to install?
=============================================================

An answer to a vocabulary request is a new `petroleum.py`. It arrives as
prose plus a file, and prose cannot be trusted about whether something
BROKE. This script decides that question against YOUR documents, not
against anyone's test fixtures.

    # 1. before you change anything — record what the pack reads today
    py -m dataview.file_catalog.vocab_check snapshot --in C:\\docs ^
        --out baseline.json

    # 2. when a proposed pack comes back — check it against that record
    py -m dataview.file_catalog.vocab_check check --pack-file petroleum.py ^
        --baseline baseline.json

Output is a verdict, not a report:

    FIXED       tables that were unrecognised and now identify
    IMPROVED    tables that identify as something more specific
    REGRESSED   tables that recognised before and don't now, or now
                identify as something DIFFERENT
    UNCHANGED   everything else

Exit code 1 on any regression, so it can gate a deployment.

WHY A SNAPSHOT RATHER THAN RE-READING
-------------------------------------
Identification depends only on the header, so the snapshot stores headers
and verdicts — not rows, not documents. It is small, it is safe to keep in
version control, and it means the check runs in a second without touching
the document store again. It also means a snapshot taken on the customer's
machine can be sent WITH a request, so whoever answers can test against
the real layouts before replying.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from docshape.engine.recognise import Recogniser
from docshape.packs import validate as validate_pack
from docshape.packs.overlay import load_layered


# ═════════════════════════════════════════════════════════════════════════ #
# loading a CANDIDATE pack without installing it
# ═════════════════════════════════════════════════════════════════════════ #
def load_pack_file(path):
    """Import a pack .py from an arbitrary path under a throwaway name.

    Deliberately does NOT copy it into docshape/packs first: the whole
    point is to judge the file before it is allowed near the deployment.
    """
    name = "_candidate_pack_" + os.path.basename(path).replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"not importable: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    for attr in ("fields", "shapes"):
        if not getattr(mod, attr, None):
            raise RuntimeError(f"{path} has no {attr} — not a pack")
    for attr, default in (("numeric", set()), ("columns", {}),
                          ("transforms", {}), ("noise", set()),
                          ("char_map", {}), ("identity_field", None)):
        if not hasattr(mod, attr):
            setattr(mod, attr, default)
    return mod


# ═════════════════════════════════════════════════════════════════════════ #
# snapshot
# ═════════════════════════════════════════════════════════════════════════ #
def take_snapshot(paths, pack_name="petroleum"):
    from dataview.file_catalog.doc_flow import extract_tables
    pack, _ov, _op, _sb, _sp = load_layered(pack_name, use_sandbox=False)
    eng = Recogniser(pack)
    entries, errors = [], []
    for p in paths:
        try:
            tabs = extract_tables(p)
        except Exception as e:
            errors.append({"file": p, "error": str(e)[:200]})
            continue
        for name, header, rows in tabs:
            shape, score, _cm = eng.identify(header)
            entries.append({
                "file": os.path.basename(p), "table": name,
                "header": [str(h) for h in header],
                "shape": shape or "UNKNOWN", "score": round(score, 2),
                "rows": len(rows),
            })
    return {
        "kind": "docshape_snapshot",
        "created": datetime.now().isoformat(timespec="seconds"),
        "pack": pack_name,
        "shape_names": sorted(pack.shapes),
        "documents": len({e["file"] for e in entries}),
        "entries": entries,
        "unreadable": errors,
    }


# ═════════════════════════════════════════════════════════════════════════ #
# static lints — problems visible without any document
# ═════════════════════════════════════════════════════════════════════════ #
def lint(pack, log=print):
    """Coherence problems a proposed pack can carry. Warnings, not errors:
    each one is legitimate in some case, but each is usually a mistake."""
    problems = []
    problems += validate_pack(pack, log=lambda _m: None)

    # a shape whose required set contains another shape's entirely
    for a, sa in pack.shapes.items():
        ra = set(sa.get("required", ()))
        if not ra:
            continue
        for b, sb in pack.shapes.items():
            if a == b:
                continue
            rb = set(sb.get("required", ()))
            if rb and rb < ra:
                problems.append(
                    f"{a}: required {sorted(ra)} contains all of {b}'s "
                    f"{sorted(rb)} — {b} may keep winning the tie-break")

    # an alias claimed by more than one field: the loser is decided by
    # dict order, which is exactly the trap that is hard to see
    owner = {}
    for f, aliases in pack.fields.items():
        for a in aliases:
            key = " ".join(str(a).lower().split())
            owner.setdefault(key, []).append(f)
    for a, fs in sorted(owner.items()):
        if len(fs) > 1:
            problems.append(
                f"alias {a!r} is claimed by {fs} — the first one listed "
                f"wins every time; the others can never match it")

    # a required field with no aliases can never match
    for name, spec in pack.shapes.items():
        for f in spec.get("required", ()):
            if f not in pack.fields:
                problems.append(
                    f"{name}: required field {f!r} has no aliases — this "
                    f"shape can NEVER match")
    for p in problems:
        log(f"  ! {p}")
    return problems


# ═════════════════════════════════════════════════════════════════════════ #
# check
# ═════════════════════════════════════════════════════════════════════════ #
def check(snapshot, pack, log=print):
    """Re-identify every snapshotted header with the proposed pack."""
    eng = Recogniser(pack)
    fixed, improved, regressed, unchanged = [], [], [], []
    for e in snapshot["entries"]:
        before = e["shape"]
        shape, score, _cm = eng.identify(e["header"])
        after = shape or "UNKNOWN"
        row = {**e, "after": after, "after_score": round(score, 2)}
        if before == after:
            unchanged.append(row)
        elif before == "UNKNOWN":
            fixed.append(row)
        elif after == "UNKNOWN":
            regressed.append(row)
        else:
            # a different name is not automatically wrong — but it is
            # never automatically right either, so it is called out.
            regressed.append(row)
    return fixed, improved, regressed, unchanged


def report(snapshot, pack, log=print):
    log(f"\n{'=' * 62}\nVOCABULARY CHECK\n{'=' * 62}")
    log(f"  snapshot: {snapshot.get('documents')} document(s), "
        f"{len(snapshot.get('entries', []))} table(s), "
        f"taken {snapshot.get('created')}")

    log("\n-- coherence --")
    problems = lint(pack, log=log)
    if not problems:
        log("  ok — no incoherent shapes or contested aliases")

    fixed, improved, regressed, unchanged = check(snapshot, pack, log=log)
    log("\n-- against your own documents --")
    log(f"  FIXED      {len(fixed)}")
    log(f"  REGRESSED  {len(regressed)}")
    log(f"  UNCHANGED  {len(unchanged)}")

    for r in fixed:
        log(f"\n  ✔ {r['file']} · {r['table']}")
        log(f"      UNKNOWN  ->  {r['after']} ({r['after_score']:.2f})")
    for r in regressed:
        log(f"\n  ✗ {r['file']} · {r['table']}")
        log(f"      {r['shape']} ({r['score']:.2f})  ->  "
            f"{r['after']} ({r['after_score']:.2f})")
        log(f"      header: {r['header'][:6]}")

    new_shapes = sorted(set(pack.shapes) - set(snapshot.get("shape_names", [])))
    gone = sorted(set(snapshot.get("shape_names", [])) - set(pack.shapes))
    if new_shapes:
        log(f"\n  new shape(s) in this pack: {new_shapes}")
    if gone:
        log(f"  shape(s) REMOVED by this pack: {gone}")

    log("")
    if regressed:
        log("VERDICT: DO NOT INSTALL — something that worked stopped "
            "working. Send the lines marked ✗ back and ask for a fix.")
    elif fixed:
        log("VERDICT: SAFE TO INSTALL — it fixes what it claims and "
            "breaks nothing you had.")
    else:
        log("VERDICT: SAFE, BUT CHANGES NOTHING HERE — nothing regressed, "
            "and nothing you sent got fixed either. Worth asking why.")
    return 1 if regressed else 0


# ═════════════════════════════════════════════════════════════════════════ #
# cli
# ═════════════════════════════════════════════════════════════════════════ #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Prove a proposed vocabulary against your documents.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("snapshot", help="record what the pack reads today")
    s1.add_argument("--in", dest="indir", required=True)
    s1.add_argument("--pack", default="petroleum")
    s1.add_argument("--out", default="baseline.json")

    s2 = sub.add_parser("check", help="test a proposed pack file")
    s2.add_argument("--pack-file", required=True,
                    help="the petroleum.py that came back")
    s2.add_argument("--baseline", default="baseline.json")

    a = ap.parse_args(argv)

    if a.cmd == "snapshot":
        if not os.path.isdir(a.indir):
            ap.error(f"not a folder: {a.indir}")
        from dataview.file_catalog.doc_flow import collect_files
        paths = collect_files(a.indir)
        if not paths:
            ap.error(f"no readable documents under {a.indir}")
        snap = take_snapshot(paths, a.pack)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=1, ensure_ascii=False)
        hist = Counter(e["shape"] for e in snap["entries"])
        print(f"{snap['documents']} document(s), "
              f"{len(snap['entries'])} table(s) -> {a.out}")
        for s, n in hist.most_common():
            print(f"   {n:4}  {s}")
        if snap["unreadable"]:
            print(f"   ({len(snap['unreadable'])} file(s) unreadable — "
                  f"not part of the baseline)")
        return 0

    if not os.path.exists(a.baseline):
        ap.error(f"no baseline at {a.baseline} — run `snapshot` first")
    if not os.path.exists(a.pack_file):
        ap.error(f"no such file: {a.pack_file}")
    with open(a.baseline, encoding="utf-8") as f:
        snap = json.load(f)
    pack = load_pack_file(a.pack_file)
    return report(snap, pack)


if __name__ == "__main__":
    sys.exit(main())
