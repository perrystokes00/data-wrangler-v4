"""
sync_schema.py — regenerate EVERY schema catalog from the live database, then prove it landed.

    python tools\\sync_schema.py                    # regenerate + verify
    python tools\\sync_schema.py --check            # verify only, change nothing
    python tools\\sync_schema.py --database DataView

WHY THIS EXISTS
---------------
Three generators, two live catalogs, no single command — and on 2026-07-17 the loader was
found running on a catalog dated 2026-07-11. A column added to dv_well that morning showed up
in the UI as "source column holds data but maps nowhere", because the database had it and the
catalog didn't. Nothing was broken. Nothing errored. The map of the schema had simply stopped
matching the schema.

Worse, every generator writes to a path relative to ITSELF, and the July 11 refactor moved
them all into tools\\:

    build_fk_catalog.py:33      __file__.parent / "schema_registry"        -> tools\\schema_registry\\
    build_schema_domain.py:33   __file__.parent.parent / "schema_registry" -> <repo>\\schema_registry\\
    gen_schema_docs.py:350      --out default "schema_docs"                -> a folder that moved

None of those is where anything reads. Each would exit 0, print a success line, create a
directory nobody looks in, and leave the real catalog untouched. You would run it, believe the
catalog was fresh, and be wrong in exactly the way that is hardest to notice.

So this script does two things that matter more than generating:
  1. Writes to ABSOLUTE, explicit paths under dataview\\schema_registry\\ — never a path
     relative to whichever script happens to be running.
  2. VERIFIES afterwards, by re-reading each file and looking for a column that must be there.
     A generator that silently wrote nowhere is the failure this is built to catch.

WHAT IT REGENERATES
-------------------
    dataview_fk_catalog.json     v3 loader (bulk_dir_loader)   <- gen_schema_catalog.py
    dataview_schema_full.json    the DDL contract              <- gen_schema_catalog.py
    dataview_schema_domain.json  v2 pipeline (page_pipeline,   <- build_schema_domain.py
                                 dataview\\core\\schema.py)

NOT generate_dataview_schema.py — it emits root key "dataview_schema_domain" while
dataview\\core\\schema.py:46 requires "ppdm_39_schema_domain". Its output would not load.
build_schema_domain.py:413 emits the right key. That is why it, not the other, is called here.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

# Absolute, and derived from THIS file's known position (tools\sync_schema.py -> repo root).
# Everything below is stated outright rather than assembled from a relative path, because
# relative paths are the specific bug this script exists to stop repeating.
REPO = pathlib.Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
REGISTRY = REPO / "dataview" / "schema_registry"

FK_JSON = REGISTRY / "dataview_fk_catalog.json"
FULL_JSON = REGISTRY / "dataview_schema_full.json"
DOMAIN_JSON = REGISTRY / "dataview_schema_domain.json"

DEFAULT_SERVER = r"localhost\SQLEXPRESS"
DEFAULT_DB = "DataView_Demo"
DEFAULT_SCHEMA = "dataview"

# A column that must appear in a current catalog. Change it when the schema moves on — the
# point is that it is something the DATABASE has and an OLD catalog would not.
PROBE = "INVENTORY_ID"


def _stat(p):
    if not p.exists():
        return None
    s = p.stat()
    return {"size": s.st_size,
            "mtime": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}


def _run(label, cmd):
    print(f"\n-- {label}")
    print("   " + " ".join(str(c) for c in cmd))
    # PowerShell's console is cp1252. build_schema_domain.py finishes its work, writes the
    # file, and THEN dies on `print("Done \u2713")` — UnicodeEncodeError on a tick mark, exit
    # code 1, after a completely successful run. Give the child a UTF-8 stdout so a decorative
    # character can't turn a success into a failure.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([str(c) for c in cmd], cwd=str(REPO),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    for line in out.splitlines():
        print("   | " + line)
    if r.returncode != 0:
        print(f"   FAILED rc={r.returncode}")
        for line in err.splitlines()[-15:]:
            print("   ! " + line)
        return False
    if err:
        for line in err.splitlines()[-5:]:
            print("   ! " + line)
    return True


def _verify(path, probe):
    """Does this file exist, parse, and contain the probe column?

    Shape-agnostic on purpose: the three catalogs have three different structures, and a
    verifier that must understand each one is a verifier that breaks when one of them changes.
    Presence of the token in the serialized JSON is a weaker claim than a schema walk — but it
    is a claim that stays true, and it catches the failure that actually happened: a file that
    was never rewritten.
    """
    if not path.exists():
        return False, "does not exist"
    try:
        raw = path.read_text(encoding="utf-8")
        json.loads(raw)                       # must be valid JSON, not a truncated write
    except json.JSONDecodeError as e:
        return False, f"not valid JSON ({e})"
    except OSError as e:
        return False, f"unreadable ({e})"
    n = raw.upper().count(probe.upper())
    if n == 0:
        return False, f"parses, but '{probe}' appears 0 times — this catalog is STALE"
    return True, f"'{probe}' x{n}"


def main():
    ap = argparse.ArgumentParser(
        description="Regenerate and verify every schema catalog the app reads.")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--database", default=DEFAULT_DB)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--probe", default=PROBE,
                    help=f"column that must appear in a current catalog (default: {PROBE})")
    ap.add_argument("--check", action="store_true",
                    help="verify only — regenerate nothing")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the .bak copies")
    args = ap.parse_args()

    files = [("dataview_fk_catalog.json", FK_JSON, "v3 loader (bulk_dir_loader)"),
             ("dataview_schema_full.json", FULL_JSON, "the DDL contract"),
             ("dataview_schema_domain.json", DOMAIN_JSON, "v2 pipeline (page_pipeline, core/schema.py)")]

    print("=" * 74)
    print(f"schema sync — {args.server} · {args.database} · {args.schema}")
    print(f"registry: {REGISTRY}")
    print("=" * 74)

    before = {name: _stat(p) for name, p, _ in files}
    print("\nBEFORE")
    for name, p, who in files:
        b = before[name]
        print(f"  {name:<30} {(b['mtime'] if b else 'MISSING'):<21} {who}")

    if args.check:
        print("\nVERIFY (--check: nothing regenerated)")
        bad = 0
        for name, p, _ in files:
            ok, why = _verify(p, args.probe)
            print(f"  {'PASS' if ok else 'FAIL'}  {name:<30} {why}")
            bad += (not ok)
        return 1 if bad else 0

    if not REGISTRY.is_dir():
        print(f"\nERROR: {REGISTRY} does not exist. Nothing reads a catalog that isn't there.")
        return 2

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        print("\nBACKUP")
        for name, p, _ in files:
            if p.exists():
                bak = p.with_suffix(f".{stamp}.bak")
                bak.write_bytes(p.read_bytes())
                print(f"  {name} -> {bak.name}")
        print("  (dataview_schema_full.json is the reference for the relax_notnull_ddl.sql\n"
              "   restore list — the diff is against the PRE-relax copy, so keep these.)")

    ok = True
    # --out is a DIRECTORY here; writes both fk + full
    ok &= _run("gen_schema_catalog.py  ->  fk_catalog + schema_full",
               [sys.executable, TOOLS / "gen_schema_catalog.py",
                # --db, NOT --database. The two generators disagree on the flag name and
                # argparse exits 2 on the wrong one — which looked like a connection problem
                # and wasn't.
                "--server", args.server, "--db", args.database,
                "--schema", args.schema, "--out", REGISTRY])
    # --out is a FILE here. --schema defaults to 'dbo' in that script, which is wrong for us,
    # and its own default --out points at <repo>\schema_registry\ — a directory nothing reads.
    ok &= _run("build_schema_domain.py  ->  schema_domain",
               [sys.executable, TOOLS / "build_schema_domain.py",
                "--dialect", "sqlserver", "--server", args.server,
                "--database", args.database, "--windows-auth",
                "--schema", args.schema, "--out", DOMAIN_JSON])

    print("\nAFTER")
    for name, p, _ in files:
        b, a = before[name], _stat(p)
        if a is None:
            print(f"  {name:<30} STILL MISSING")
            continue
        moved = (b is None) or (a["mtime"] != b["mtime"])
        delta = "" if b is None else f"  ({a['size'] - b['size']:+,} bytes)"
        print(f"  {name:<30} {a['mtime']}  {'rewritten' if moved else 'UNCHANGED — not rewritten!'}{delta}")

    print("\nVERIFY")
    bad = 0
    counts = {}
    for name, p, _ in files:
        good, why = _verify(p, args.probe)
        print(f"  {'PASS' if good else 'FAIL'}  {name:<30} {why}")
        bad += (not good)
        if good:
            try:
                counts[name] = p.read_text(encoding="utf-8").upper().count(args.probe.upper())
            except OSError:
                pass

    # Presence is not currency, and this is not hypothetical: on 2026-07-17 a seven-week-old
    # dataview_schema_domain.json PASSed with 'INVENTORY_ID' x2 while the v3 catalogs had 39.
    # The probe was in it — from the two tables that had the column back in May. A file can
    # contain the token and still be a museum piece. Compare them against each other: they
    # describe ONE database, so a catalog carrying a fraction of another's count is behind,
    # whatever its own line says.
    if len(counts) > 1:
        top = max(counts.values())
        laggards = {k: v for k, v in counts.items() if top and v < top * 0.5}
        if laggards:
            print(f"\n  ⚠ these describe the SAME database, but '{args.probe}' counts disagree:")
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
                mark = "  <- behind" if k in laggards else ""
                print(f"      {k:<30} x{v}{mark}")
            print("    A catalog can hold the probe and still be stale. Check its date in the")
            print("    BEFORE block — a low count means it predates most of the schema.")
            bad += len(laggards)

    print("\n" + "=" * 74)
    if bad or not ok:
        if not ok:
            print("A generator FAILED (see the ! lines above). Any catalog it owns still holds")
            print("whatever it held before — check the AFTER block: 'UNCHANGED — not rewritten!'")
            print("means that file did not move, whatever its VERIFY line says.")
        if bad:
            print(f"{bad} catalog(s) did not verify. The loader reads these — a stale one shows")
            print("up as 'source column holds data but maps nowhere', NOT as an error.")
        if not bad and not ok:
            print("(Every catalog still VERIFIES — but only because the old files were already")
            print("current. Do not read that as the regeneration having worked.)")
        return 1
    print("All catalogs regenerated and verified.")
    print("Restart the app (Streamlit caches imported modules AND the parsed catalog:")
    print("bulk_dir_loader._catalog_json keys its cache on the PATH, not the mtime — so a")
    print("regenerated file at the same path is not re-read until the process restarts).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
