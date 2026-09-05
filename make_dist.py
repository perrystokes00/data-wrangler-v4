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
import sys
import time

# ── THIS TOOL MUST NOT DIE OF ITS OWN OUTPUT ─────────────────────────────
# It crashed with UnicodeEncodeError before printing a single count: the
# report draws box characters, and a REDIRECTED stdout on Windows gets the
# ANSI codepage (cp1252) rather than the console's, so the same line that is
# fine in a console window raises the moment anyone pipes it to a file or a
# log. Exactly the scar CLAUDE.md records against _say() -- "the timing
# instrumentation took down the page it was measuring" -- and it lands harder
# here, because this is the tool that builds the thing being shipped and a
# build script is the first thing anyone runs under a redirect or in CI.
#
# errors="replace" not "ignore": a mangled character still shows something is
# there. Wrapped, because reconfigure needs 3.7+ and a build must not fail on
# the way to failing.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SKIP_DIRS = {"__pycache__", ".git", ".hg", ".svn", ".venv", "venv",
             "_quarantine", "dvpath", "node_modules", "build", "dist",
             ".idea", ".vscode", ".pytest_cache", ".ipynb_checkpoints"}

# Non-python things a running app needs. Globs, relative to the root.
#
# .streamlit/config.toml BY NAME, NOT .streamlit/*.toml. The glob was correct
# for what is in that folder today and wrong for what Streamlit puts there by
# convention: secrets.toml is its standard credential store, so the day
# anything writes one, the build ships the customer an ANTHROPIC_API_KEY.
# Nothing had gone wrong yet -- this closes it while it is still cheap.
#
# config.toml itself must ship. enableStaticServing = true is load-bearing:
# it serves /app/static/*.geojson, which the reference-well and lease layers
# are drawn from, so without it those layers silently fail to load in the
# browser where no Python error ever reaches us. The theme is the product's
# identity. Neither is machine-specific, which is the test for shipping a
# config at all.
# well_icons/** IS GONE FROM THIS LIST BECAUSE THE DIRECTORY IS GONE. Its two
# files were named only by setup_wranglerview.py and deploy_federation.py --
# WranglerView-era deployment scripts that do not ship and no longer describe
# this app. Left here it would be an entry pointing at nothing, which is the
# same phantom tidy_root.ps1's $Keep held after run_watcher.bat was deleted:
# harmless until someone reads the list and believes it.
DEFAULT_INCLUDE = [
    "assets/**", ".streamlit/config.toml",
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

    # ── RESOLVE --keep ONCE, AND REFUSE TO BUILD WITHOUT IT ──────────────
    # `--keep pipeline_proc_runner.py` -- the name the docstring gives -- did
    # not resolve, because --keep joins to the ROOT and the file lives at
    # dataview/import_data/. The old code printed one "not found" line and
    # CARRIED ON to a successful-looking build. That is the worst possible
    # outcome for this flag specifically: --keep exists for subprocess entry
    # points no import analysis can find, so the dist that gets shipped is
    # missing exactly the file nothing else would have caught, and the
    # detached pipeline fails on a customer's machine rather than here.
    #
    # A BARE NAME NOW RESOLVES, because that is what the documentation says
    # to type and being right about the path is not the point of the flag.
    # An ambiguous name is an error rather than a guess -- picking one of two
    # files called the same thing is how the wrong one ships.
    _keep_abs, _keep_bad = [], []
    for k in a.keep:
        f = os.path.abspath(os.path.join(root, k))
        if os.path.exists(f):
            _keep_abs.append(f)
            continue
        _hits = [p for p in py if os.path.basename(p) == os.path.basename(k)]
        if len(_hits) == 1:
            print("   --keep %s -> %s" % (k, os.path.relpath(_hits[0], root)))
            _keep_abs.append(_hits[0])
        elif _hits:
            _keep_bad.append("%s: ambiguous, %d files share that name (%s)"
                             % (k, len(_hits),
                                ", ".join(os.path.relpath(h, root) for h in _hits[:4])))
        else:
            _keep_bad.append("%s: not found" % k)
    if _keep_bad:
        print("!! --keep could not be resolved, and a dist without it would")
        print("   be missing a file no import analysis can find:")
        for b in _keep_bad:
            print("     " + b)
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
    for f in _keep_abs:
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
    # Already resolved and already validated above -- a missing --keep is now
    # a refusal to build, so anything reaching here exists. The python ones
    # seeded the traversal; these are the .sql / .json / .bat kind.
    explicit = {f for f in _keep_abs if f not in py}
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

    # ── AN ASSET NOBODY NAMES IS 22 MB OF NOTHING ────────────────────────
    # assets/** shipped six images, and four of them -- 22.3 MB of a 23 MB
    # payload -- were referenced by no code, no installer script and no
    # config: data_wranglerv2.png (13.2 MB), data_wrangler3.png (8.8 MB) and
    # two _old files. Version cruft, on its way to a customer.
    #
    # THE SAME MISTAKE AS .streamlit/*.toml, which is why the report rather
    # than a narrower glob. A glob is right about the folder as it stands and
    # says nothing about what lands in it later; naming files individually
    # trades that for a NEW asset silently not shipping, which is a worse
    # failure because the app then renders a broken image on a customer
    # machine. So the glob stays broad and the BUILD SAYS WHAT IT IS
    # CARRYING -- which is this tool's stated job: "all of it listed in the
    # report so a mistake is visible before --apply".
    #
    # REFERENCES ARE LOOKED FOR EVERYWHERE, not just in the kept set.
    # data_wrangler.ico is named by installer.iss and make_icon.py, neither
    # of which ships, and flagging it would train the reader to ignore this.
    #
    # STRING LITERALS, NOT RAW TEXT, AND THE FIRST VERSION PROVED WHY. It
    # scanned whole files, so the comment two paragraphs above -- which names
    # the very files it was written to describe -- counted as a reference,
    # and the check cleared 22 MB of dead images by reading its own
    # documentation. A file is LOADED by a literal; it is only DISCUSSED in
    # prose. Walking the AST for string constants asks the right question,
    # and it means writing about an asset can never again hide it.
    _ref_text = []
    for _p, _t in py.items():
        if _t is None:
            _ref_text.append(text.get(_p, ""))     # unparseable: fall back
            continue
        for _n in ast.walk(_t):
            if isinstance(_n, ast.Constant) and isinstance(_n.value, str):
                _ref_text.append(_n.value)
    for _ext in (".iss", ".ps1", ".toml", ".bat", ".cfg"):
        for _f in _glob.glob(os.path.join(root, "*" + _ext)):
            try:
                _ref_text.append(open(_f, encoding="utf-8",
                                      errors="ignore").read())
            except OSError:
                pass
    _unref = []
    for _a in assets:
        _base = os.path.basename(_a)
        if not any(_base in _t for _t in _ref_text):
            _unref.append(_a)
    if _unref:
        _mb = sum(os.path.getsize(x) for x in _unref) / 1048576.0
        print("── assets nothing references (%d file(s), %.1f MB) %s"
              % (len(_unref), _mb, "─" * 18))
        for _a in sorted(_unref, key=lambda x: -os.path.getsize(x)):
            print("   %7.2f MB  %s"
                  % (os.path.getsize(_a) / 1048576.0,
                     os.path.relpath(_a, root)))
        print("   These SHIP. Delete them, or narrow --include, if that is")
        print("   not what you want.")
        print()

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
