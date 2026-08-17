r"""
make_dist.py — emit a clean copy of the codebase, without the extras.

    python make_dist.py --entry app_v4.py                    # dry run
    python make_dist.py --entry app_v4.py --apply
    python make_dist.py --entry app_v4.py --apply --out C:\Bulk\dist

NOTHING IS DELETED AND THE REPO IS NOT TOUCHED. This copies the files a
running app needs into a new folder. If the result is wrong, delete the
folder and change the arguments.

WHY BUILD RATHER THAN PRUNE
---------------------------
Deleting from the working tree is irreversible in practice — the scripts,
probes and one-off tools are worth keeping where you work, and worthless to
a customer. A build separates "what I develop in" from "what I ship", so
neither has to compromise for the other.

WHAT GOES IN
------------
  1. Every module reachable from the entry points, by import.
  2. Every module whose NAME is mentioned anywhere — Streamlit pages get
     dispatched by string, and reachability alone would drop them. Being
     wrong in this direction ships a file nobody calls; being wrong the
     other way ships an app that crashes on a menu click.
  3. Package __init__.py files along every included path, or the imports
     fail for a reason that takes an hour to find.
  4. Whatever --include names: assets, config, requirements. Defaults
     cover the obvious ones.
  5. Anything --keep names explicitly. USE THIS FOR SUBPROCESS ENTRY
     POINTS — pipeline_proc_runner.py is launched by name, never imported,
     so no import analysis will ever find it, and the detached pipeline
     silently stops working without it.

WHAT STAYS BEHIND
-----------------
Scripts, probes, one-off tools, test corpora, generated exports, caches,
reports, __pycache__, .git, quarantine folders. All of it listed in the
report so a mistake is visible before --apply.

THE CHECK THAT MATTERS
----------------------
After copying, every file is compiled and every import is resolved against
what was actually copied. A dist that is not CLOSED UNDER ITS OWN IMPORTS
is broken, and that is exactly the failure a hand-assembled package makes.
"""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import time

SKIP_DIRS = {"__pycache__", ".git", ".hg", ".svn", ".venv", "venv",
             "_quarantine", "dvpath", "node_modules", "build", "dist",
             ".idea", ".vscode", ".pytest_cache", ".ipynb_checkpoints"}

# Non-python things a running app needs. Globs, relative to the root.
DEFAULT_INCLUDE = [
    "assets/**", ".streamlit/*.toml", "well_icons/**",
    "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "ORIENTATION.md", "README*.md", "LICENSE*",
]


def _walk(root):
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for n in sorted(names):
            yield os.path.join(d, n)


def _mod(p, root):
    m = os.path.relpath(p, root)[:-3].replace(os.sep, ".")
    return m[:-9] if m.endswith(".__init__") else m


def _imports_of(tree, mod):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                parts = mod.split(".")
                base = parts[:max(0, len(parts) - n.level)]
                r = ".".join(base + ([n.module] if n.module else []))
            else:
                r = n.module or ""
            if r:
                out.add(r)
                out |= {f"{r}.{a.name}" for a in n.names}
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
    ap.add_argument("--keep", action="append", default=[],
                    help="extra file to include, repeatable. Use for "
                         "subprocess entry points that are never imported.")
    ap.add_argument("--include", action="append", default=None,
                    help="glob for non-python files, repeatable")
    ap.add_argument("--out", default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    py = {}
    for p in _walk(root):
        if p.endswith(".py"):
            try:
                py[p] = ast.parse(open(p, encoding="utf-8",
                                       errors="ignore").read())
            except SyntaxError:
                py[p] = None

    mods = {_mod(p, root): p for p in py}
    imports = {p: (_imports_of(t, _mod(p, root)) if t else set())
               for p, t in py.items()}

    entries = [os.path.abspath(os.path.join(root, e)) for e in a.entry]
    entries = [e for e in entries if e in py]
    if not entries:
        print("!! no entry point matched — pass --entry app_v4.py")
        return 2

    # 1 · reachable by import — SEEDED WITH --keep, NOT PATCHED IN LATER.
    #
    # An explicitly kept file is usually a SUBPROCESS entry point:
    # pipeline_proc_runner is launched by name and never imported, so no
    # analysis finds it. But it drags a whole cone behind it —
    # pipeline_run, file_inventory, worker_core, catalog_capture. Adding
    # the file after the traversal would copy the runner and leave every
    # module it needs behind, producing a dist whose detached pipeline
    # fails on first use with an ImportError.
    _seed = list(entries)
    for k in a.keep:
        f = os.path.abspath(os.path.join(root, k))
        if f in py:
            _seed.append(f)
    keep, queue = set(_seed), list(_seed)
    while queue:
        for imp in imports[queue.pop()]:
            for cand in (imp, imp.rsplit(".", 1)[0] if "." in imp else None):
                if cand and cand in mods and mods[cand] not in keep:
                    keep.add(mods[cand])
                    queue.append(mods[cand])
                    break

    # 2 · mentioned by name anywhere — pages dispatched by string
    text = {}
    for p in py:
        try:
            text[p] = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            text[p] = ""
    import re
    mentioned = set()
    for p in py:
        if p in keep:
            continue
        stem = os.path.basename(p)[:-3]
        if stem == "__init__":
            continue
        pat = re.compile(r"\b" + re.escape(stem) + r"\b")
        if any(pat.search(s) for q, s in text.items() if q != p and q in keep):
            mentioned.add(p)
    keep |= mentioned

    # 3 · __init__.py along every kept path
    inits = set()
    for p in list(keep):
        d = os.path.dirname(p)
        while d.startswith(root) and d != root:
            ini = os.path.join(d, "__init__.py")
            if os.path.exists(ini) and ini not in keep:
                inits.add(ini)
            d = os.path.dirname(d)
    keep |= inits

    # 4 · explicit keeps that are NOT python (a .sql, a .json) — the
    #     python ones already seeded the traversal above.
    explicit = set()
    for k in a.keep:
        f = os.path.abspath(os.path.join(root, k))
        if not os.path.exists(f):
            print(f"!! --keep {k}: not found")
        elif f not in py:
            explicit.add(f)
    keep |= explicit

    # 5 · non-python includes
    import glob as _glob
    assets = set()
    for pattern in (a.include if a.include is not None else DEFAULT_INCLUDE):
        for f in _glob.glob(os.path.join(root, pattern), recursive=True):
            if os.path.isfile(f) and not any(
                    s in os.path.relpath(f, root).split(os.sep)
                    for s in SKIP_DIRS):
                assets.add(os.path.abspath(f))

    def rel(p):
        return os.path.relpath(p, root)

    def kb(p):
        try:
            return os.path.getsize(p) / 1024.0
        except OSError:
            return 0.0

    left = sorted(set(py) - keep)
    print(f"root        {root}")
    print(f"entry       {', '.join(a.entry)}")
    print()
    print(f"  {len(keep):>4} python file(s)  {sum(kb(p) for p in keep):>9,.0f} KB")
    print(f"       ├─ reachable by import      {len(keep) - len(mentioned) - len(inits):>4}")
    print(f"       ├─ kept because named       {len(mentioned):>4}")
    print(f"       ├─ package __init__         {len(inits):>4}")
    print(f"       └─ --keep and their imports {len(a.keep):>4} seed(s)")
    print(f"  {len(assets):>4} asset file(s)   {sum(kb(p) for p in assets):>9,.0f} KB")
    print(f"  {len(left):>4} LEFT BEHIND     {sum(kb(p) for p in left):>9,.0f} KB")
    print()

    print("── left behind (largest first) " + "─" * 32)
    for p in sorted(left, key=lambda x: -kb(x))[:30]:
        print(f"   {kb(p):>7,.0f} KB  {rel(p)}")
    if len(left) > 30:
        print(f"   … and {len(left) - 30} more")
    print()

    # ── the check: is the kept set closed under its own imports? ─────────
    kept_mods = {_mod(p, root) for p in keep if p.endswith(".py")}
    broken = []
    for p in keep:
        if not p.endswith(".py") or py.get(p) is None:
            continue
        for imp in imports[p]:
            top = imp.split(".")[0]
            if top not in {m.split(".")[0] for m in mods}:
                continue                      # third-party or stdlib
            hit = any(imp == m or imp.rsplit(".", 1)[0] == m
                      for m in kept_mods)
            if not hit and imp in mods:
                broken.append((rel(p), imp))
    print("── closure check " + "─" * 46)
    if broken:
        print(f"   {len(broken)} import(s) point at a module NOT being copied:")
        for f, imp in broken[:15]:
            print(f"     {f} -> {imp}")
        print("   The dist would fail on those. Add them with --keep, or")
        print("   check why they are unreachable.")
    else:
        print("   every in-tree import resolves inside the copied set")
    print()

    if not a.apply:
        print("DRY RUN — nothing copied. Re-run with --apply.")
        return 0

    out = a.out or os.path.join(root, "dist",
                                f"data_wrangler_{time.strftime('%Y%m%d')}")
    out = os.path.abspath(out)
    if os.path.exists(out):
        print(f"!! {out} already exists — remove it or pass a different --out")
        return 2

    n = 0
    for p in sorted(keep | assets):
        d = os.path.join(out, rel(p))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(p, d)
        n += 1
    with open(os.path.join(out, "DIST_MANIFEST.txt"), "w",
              encoding="utf-8") as f:
        f.write(f"built {time.strftime('%Y-%m-%d %H:%M')} from {root}\n")
        f.write(f"entry: {', '.join(a.entry)}\n\n")
        f.write("\n".join(sorted(rel(p) for p in keep | assets)))

    # compile everything that landed
    import py_compile
    bad = []
    for d, dirs, names in os.walk(out):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for nm in names:
            if nm.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(d, nm), doraise=True)
                except Exception as e:
                    bad.append(f"{nm}: {type(e).__name__}")
    print(f"copied {n} file(s) to {out}")
    print(f"compile check: {'all clean' if not bad else bad}")
    print()
    print("NOW: run selftest.py from inside the dist folder. A dist that")
    print("imports clean there is one that will import clean on a machine")
    print("that has never seen the rest of the repo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
