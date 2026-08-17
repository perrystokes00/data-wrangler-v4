r"""
quarantine_orphans.py — move unreferenced modules out, reversibly.

    python quarantine_orphans.py --entry app_v4.py --entry selftest.py
    python quarantine_orphans.py --entry app_v4.py --stale-days 14
    python quarantine_orphans.py --entry app_v4.py --apply

DRY RUN BY DEFAULT. Nothing moves until --apply, and --apply MOVES rather
than deletes — into _quarantine/<date>/, keeping the folder structure, with
a manifest so every file can be put back.

WHY NOT JUST DELETE THE CENSUS ORPHAN LIST
------------------------------------------
The census marks a module orphaned when nothing IMPORTS it. That is not the
same as nothing MENTIONING it. A module can be reached by:

    importlib.import_module(f"dataview.{page}")     # name built at runtime
    __import__(module_name)                          # ditto
    PAGES = {"Bulk loader": "page_bulk"}             # a string in a registry

and this tree does all three — six files use a non-literal import. So every
candidate is checked here for its bare NAME appearing anywhere in any .py
file other than itself. A hit does not prove the module is live, but it
proves the question is not settled, and those are held back.

WHAT IT WILL NOT CATCH
----------------------
A name assembled from pieces ("page_" + kind), or held in a non-Python file
(a JSON page registry, a .toml config). Rare, but the reason --apply moves
instead of deleting, and the reason to run selftest.py and click through the
app afterwards rather than trusting this.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import time

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules",
             "_quarantine", "dvpath", ".idea", ".vscode", "build", "dist"}


def _walk(root):
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for n in sorted(names):
            if n.endswith(".py"):
                yield os.path.join(dirpath, n)


def _mod(path, root):
    m = os.path.relpath(path, root)[:-3].replace(os.sep, ".")
    return m[:-9] if m.endswith(".__init__") else m


def _imports(tree, mod):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                parts = mod.split(".")
                base = parts[:max(0, len(parts) - n.level)]
                r = ".".join(base + ([n.module] if n.module else []))
            else:
                r = n.module or ""
            if r:
                out.add(r)
                for a in n.names:
                    out.add(f"{r}.{a.name}")
        elif isinstance(n, ast.Call):
            f = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if f in ("import_module", "__import__") and n.args \
                    and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                out.add(n.args[0].value)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--root", default=".")
    ap.add_argument("--entry", action="append", default=[])
    ap.add_argument("--stale-days", type=int, default=14,
                    help="only consider files untouched this long (default 14)")
    ap.add_argument("--apply", action="store_true",
                    help="actually move them; without this, nothing changes")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    files, mods, trees = {}, {}, {}
    for p in _walk(root):
        try:
            t = ast.parse(open(p, encoding="utf-8", errors="ignore").read())
        except SyntaxError:
            files[p] = set()
            mods[_mod(p, root)] = p
            continue
        m = _mod(p, root)
        trees[p] = t
        files[p] = _imports(t, m)
        mods[m] = p

    entries = [os.path.abspath(os.path.join(root, e)) for e in a.entry]
    entries = [e for e in entries if e in files]
    if not entries:
        print("!! no entry point found — pass --entry app_v4.py")
        return 2

    # reachable set
    live, q = set(entries), list(entries)
    while q:
        for imp in files[q.pop()]:
            for cand in (imp, imp.rsplit(".", 1)[0] if "." in imp else None):
                if cand and cand in mods and mods[cand] not in live:
                    live.add(mods[cand])
                    q.append(mods[cand])
                    break

    has_main = set()
    for p, t in trees.items():
        for n in ast.walk(t):
            if isinstance(n, ast.If) and "__main__" in ast.dump(n.test):
                has_main.add(p)
                break

    now = time.time()
    cands = []
    for p in files:
        if p in live or p in has_main or p in entries:
            continue
        age = (now - os.path.getmtime(p)) / 86400.0
        if age >= a.stale_days:
            cands.append((p, age))

    # ── the check the census cannot do: is the NAME mentioned anywhere? ──
    all_text = {}
    for p in files:
        try:
            all_text[p] = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            all_text[p] = ""

    safe, held = [], []
    for p, age in sorted(cands, key=lambda r: -os.path.getsize(r[0])):
        stem = os.path.basename(p)[:-3]
        pat = re.compile(r"\b" + re.escape(stem) + r"\b")
        hits = [q2 for q2, txt in all_text.items()
                if q2 != p and pat.search(txt)]
        (held if hits else safe).append((p, age, hits))

    def rel(x):
        return os.path.relpath(x, root)

    kb = lambda x: os.path.getsize(x) / 1024.0            # noqa: E731

    print(f"{len(files):,} python file(s) · {len(live):,} reachable from "
          f"{', '.join(rel(e) for e in entries)}")
    print(f"{len(cands):,} orphan(s) untouched {a.stale_days}+ days\n")

    print(f"── HELD BACK — name appears elsewhere ({len(held)}) "
          f"{'─' * 20}")
    print("   Not proof they are live, but the question is not settled.")
    for p, age, hits in held:
        print(f"   {rel(p)}  ({kb(p):.0f} KB, {age:.0f}d)")
        for h in hits[:3]:
            print(f"       mentioned in {rel(h)}")
        if len(hits) > 3:
            print(f"       … and {len(hits) - 3} more")
    print()

    tot = sum(kb(p) for p, _a, _h in safe)
    print(f"── SAFE TO QUARANTINE ({len(safe)}, {tot:.0f} KB) {'─' * 24}")
    print("   Unreachable, no __main__, and the name appears in no other")
    print("   python file in the tree.")
    for p, age, _h in safe:
        print(f"   {rel(p)}  ({kb(p):.0f} KB, {age:.0f}d)")
    print()

    if not a.apply:
        print("DRY RUN — nothing moved. Re-run with --apply to move the SAFE")
        print("list into _quarantine/, then run selftest.py and click through")
        print("the app before deleting anything.")
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest_root = os.path.join(root, "_quarantine", stamp)
    manifest = []
    for p, _age, _h in safe:
        r = os.path.relpath(p, root)
        d = os.path.join(dest_root, r)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.move(p, d)
        manifest.append(r)
    with open(os.path.join(dest_root, "MANIFEST.txt"), "w",
              encoding="utf-8") as f:
        f.write("Moved by quarantine_orphans.py — put a file back with:\n")
        f.write(f"  move _quarantine\\{stamp}\\<path> <path>\n\n")
        f.write("\n".join(manifest))
    print(f"moved {len(manifest)} file(s) to _quarantine\\{stamp}\\")
    print("manifest written. Now: python selftest.py --tier imports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
