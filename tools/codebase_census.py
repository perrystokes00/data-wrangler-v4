r"""
codebase_census.py — what is actually reachable, and what is junk?

    python codebase_census.py                          # report to stdout
    python codebase_census.py --out census.md          # and to a file
    python codebase_census.py --entry app_v3.py --entry selftest.py

READ-ONLY. It never deletes, moves or edits anything. The output is a
list you decide from.

HOW IT DECIDES
--------------
It builds the import graph with the AST — no importing, so nothing runs
and nothing needs its dependencies installed — then marks everything
reachable from the entry points. What's left falls into three piles:

  LIVE      reachable from an entry point. Ships.
  SCRIPT    not reachable, but has `if __name__ == "__main__"`. A tool
            somebody runs by hand. Keep or retire ONE AT A TIME — this
            is where one-off scripts accumulate, and the census can't
            tell a nightly job from an experiment somebody abandoned.
  ORPHAN    not reachable, no __main__, nothing imports it. Nothing can
            call this. The strongest deletion candidates.

WHAT IT CANNOT SEE, and says so
-------------------------------
A module reached only by a DYNAMIC import — `importlib.import_module(name)`
where name is a variable, or a page picked out of a dict — looks orphaned.
Streamlit apps do this constantly. Every unresolvable dynamic import is
reported by file and line under UNRESOLVED, and while any of those exist
the ORPHAN list is a list of CANDIDATES, not a delete list. Check each
name against that section before removing anything.

The honest workflow: run it, read UNRESOLVED first, then work down
ORPHAN largest-first, and for each one grep the repo for its module name
before deleting. The census narrows 300 files to 20 questions; it does
not answer them.
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
import time

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules",
             ".idea", ".vscode", "build", "dist", ".pytest_cache"}


# Things that are OUTPUT, not source. A repo with thousands of files is
# usually not thousands of files of code — it is a few hundred of code and
# the rest is what the code PRODUCED, sitting where it was written.
ARTEFACT_EXT = {
    ".pyc", ".pyo", ".log", ".tmp", ".bak", ".old", ".orig", ".rej",
    ".zip", ".7z", ".gz", ".swp", ".lock", ".pid", ".dmp",
}
ARTEFACT_DIRS = {"__pycache__", "reports", "logs", "_reports", "output",
                 "outputs", "tmp", "temp", "_archive", "_archive_dead",
                 "archive", "backup", "backups", ".ipynb_checkpoints"}
DATA_EXT = {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".json", ".geojson",
            ".las", ".dlis", ".lis", ".segy", ".sgy", ".p190", ".shp", ".dbf",
            ".shx", ".prj", ".pdf", ".docx", ".doc", ".pptx", ".txt", ".xml"}
CODE_EXT = {".py", ".sql", ".ps1", ".bat", ".sh", ".js", ".html", ".css",
            ".yml", ".yaml", ".toml", ".cfg", ".ini", ".md", ".rst"}


def inventory(root):
    """Every file, not just Python. Nothing is skipped — the point is to see
    what is actually in there, including the directories the code census
    deliberately ignores."""
    rows, vcs_n, vcs_b = [], 0, 0
    for dirpath, dirnames, names in os.walk(root):
        # .git is the VERSION CONTROL STORE, not your code. On a real repo it
        # is the majority of the file count — 1,901 of 2,877 on the first run
        # here — and counting it makes the tree look four times worse than it
        # is. Counted separately and reported, never mixed in.
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir.split(os.sep)[0] in (".git", ".hg", ".svn"):
            for n in names:
                vcs_n += 1
                try:
                    vcs_b += os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    pass
            continue
        for n in names:
            path = os.path.join(dirpath, n)
            ext = os.path.splitext(n)[1].lower()
            rel = os.path.relpath(path, root)
            top = rel.split(os.sep)[0] if os.sep in rel else "(root)"
            parts = {q.lower() for q in os.path.dirname(rel).split(os.sep)}
            if ext in ARTEFACT_EXT or (parts & ARTEFACT_DIRS):
                kind = "artefact"
            elif ext in CODE_EXT:
                kind = "code"
            elif ext in DATA_EXT:
                kind = "data"
            else:
                kind = "other"
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            rows.append((rel, top, ext or "(none)", kind, size))
    return rows, vcs_n, vcs_b


def _walk(root):
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for n in sorted(names):
            if n.endswith(".py"):
                yield os.path.join(dirpath, n)


def _module_name(path, root):
    rel = os.path.relpath(path, root)
    mod = rel[:-3].replace(os.sep, ".")
    return mod[:-9] if mod.endswith(".__init__") else mod


class _Scan(ast.NodeVisitor):
    """Imports, __main__ guard, and dynamic-import sites for one file."""

    def __init__(self, mod):
        self.mod = mod
        self.imports = set()
        self.dynamic = []          # (lineno, description)
        self.has_main = False

    def visit_Import(self, node):
        for a in node.names:
            self.imports.add(a.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.level:
            # relative: resolve against this module's package
            parts = self.mod.split(".")
            base = parts[:max(0, len(parts) - node.level)]
            root = ".".join(base + ([node.module] if node.module else []))
        else:
            root = node.module or ""
        if root:
            self.imports.add(root)
            # `from pkg import mod` — mod may itself be a module
            for a in node.names:
                self.imports.add(f"{root}.{a.name}")
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name == "import_module":
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                self.imports.add(node.args[0].value)
            else:
                self.dynamic.append((node.lineno,
                                     "importlib.import_module(<not a literal>)"))
        elif name == "__import__":
            if not (node.args and isinstance(node.args[0], ast.Constant)):
                self.dynamic.append((node.lineno, "__import__(<not a literal>)"))
        self.generic_visit(node)

    def visit_If(self, node):
        src = ast.dump(node.test)
        if "__name__" in src and "__main__" in src:
            self.has_main = True
        self.generic_visit(node)


def scan(root):
    files, mods = {}, {}
    for path in _walk(root):
        mod = _module_name(path, root)
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
        except SyntaxError as e:
            files[path] = {"mod": mod, "imports": set(), "dynamic": [],
                           "main": False, "error": f"SyntaxError line {e.lineno}"}
            mods[mod] = path
            continue
        s = _Scan(mod)
        s.visit(tree)
        files[path] = {"mod": mod, "imports": s.imports, "dynamic": s.dynamic,
                       "main": s.has_main, "error": None}
        mods[mod] = path
    return files, mods


def reachable(files, mods, entries):
    """Everything importable, transitively, from the entry files."""
    seen, queue = set(), []
    for e in entries:
        if e in files:
            seen.add(e)
            queue.append(e)
    while queue:
        path = queue.pop()
        for imp in files[path]["imports"]:
            # a `from pkg import name` may name a module OR an attribute;
            # try the longest match first and fall back to the package
            for cand in (imp, imp.rsplit(".", 1)[0] if "." in imp else None):
                if cand and cand in mods and mods[cand] not in seen:
                    seen.add(mods[cand])
                    queue.append(mods[cand])
                    break
    return seen


def _imported_by(files, mods):
    who = {p: set() for p in files}
    for path, info in files.items():
        for imp in info["imports"]:
            for cand in (imp, imp.rsplit(".", 1)[0] if "." in imp else None):
                if cand and cand in mods and mods[cand] != path:
                    who[mods[cand]].add(path)
                    break
    return who


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--root", default=".")
    ap.add_argument("--entry", action="append", default=[],
                    help="entry-point file, repeatable. Defaults to app*.py "
                         "at the root plus selftest.py.")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    files, mods = scan(root)

    entries = [os.path.abspath(os.path.join(root, e)) for e in a.entry]
    if not entries:
        for p in files:
            base = os.path.basename(p)
            if os.path.dirname(p) == root and (
                    base.startswith("app") or base == "selftest.py"):
                entries.append(p)
    entries = [e for e in entries if e in files]

    live = reachable(files, mods, entries)
    who = _imported_by(files, mods)

    scripts, orphans, broken = [], [], []
    for path, info in sorted(files.items()):
        if info["error"]:
            broken.append((path, info["error"]))
        if path in live:
            continue
        (scripts if info["main"] else orphans).append(path)

    dyn = [(p, ln, msg) for p, i in sorted(files.items())
           for ln, msg in i["dynamic"]]

    # same basename in more than one place — the archived-copy problem
    bybase = {}
    for p in files:
        bybase.setdefault(os.path.basename(p), []).append(p)
    dupes = {b: v for b, v in bybase.items() if len(v) > 1 and b != "__init__.py"}

    L = []

    def out(s=""):
        L.append(s)
        print(s)

    def rel(p):
        return os.path.relpath(p, root)

    def kb(p):
        try:
            return os.path.getsize(p) / 1024.0
        except OSError:
            return 0.0

    def age(p):
        try:
            return (time.time() - os.path.getmtime(p)) / 86400.0
        except OSError:
            return 0.0

    inv, vcs_n, vcs_b = inventory(root)
    tot_n = len(inv)
    tot_mb = sum(r[4] for r in inv) / 1048576.0

    out(f"# Codebase census — {root}")
    out()
    out(f"## What is actually in here — {tot_n:,} file(s), {tot_mb:,.0f} MB")
    if vcs_n:
        out()
        out(f"_Plus {vcs_n:,} file(s) / {vcs_b / 1048576.0:,.0f} MB of version-"
            f"control internals (.git), excluded — that is history, not code._")
    out()
    out("Before asking which PYTHON is dead, see how much of the repo is")
    out("python at all. A tree with thousands of files is usually a few")
    out("hundred of source and the rest is what the code produced, sitting")
    out("where it was written.")
    out()
    bykind = {}
    for _rel, _top, _ext, kind, size in inv:
        c, s = bykind.get(kind, (0, 0))
        bykind[kind] = (c + 1, s + size)
    out("| kind | files | MB |")
    out("|---|---:|---:|")
    for k in ("code", "data", "artefact", "other"):
        c, s = bykind.get(k, (0, 0))
        out(f"| {k} | {c:,} | {s / 1048576.0:,.0f} |")
    out()

    byext = {}
    for _rel, _top, ext, kind, size in inv:
        c, s = byext.get((ext, kind), (0, 0))
        byext[(ext, kind)] = (c + 1, s + size)
    out("### By extension, most files first")
    out()
    out("| ext | kind | files | MB |")
    out("|---|---|---:|---:|")
    for (ext, kind), (c, s) in sorted(byext.items(), key=lambda kv: -kv[1][0])[:25]:
        out(f"| `{ext}` | {kind} | {c:,} | {s / 1048576.0:,.1f} |")
    out()

    bytop = {}
    for _rel, top, _ext, _kind, size in inv:
        c, s = bytop.get(top, (0, 0))
        bytop[top] = (c + 1, s + size)
    out("### By top-level folder")
    out()
    out("A folder that is nearly all data or artefacts does not belong in a")
    out("source package — it belongs beside it, or in .gitignore.")
    out()
    out("| folder | files | MB |")
    out("|---|---:|---:|")
    for top, (c, s) in sorted(bytop.items(), key=lambda kv: -kv[1][0])[:25]:
        out(f"| `{top}` | {c:,} | {s / 1048576.0:,.1f} |")
    out()

    art_n, art_s = bykind.get("artefact", (0, 0))
    if art_n:
        out(f"**{art_n:,} artefact file(s), {art_s / 1048576.0:,.0f} MB** — "
            f"caches, logs, reports, archives, zips. None of it is source. "
            f"Removing it from the repo changes no behaviour, and it is the "
            f"cheapest cut available.")
        out()

    out("---")
    out()
    out(f"## The python question — {len(files):,} python file(s)")
    out(f"- entry point(s): {', '.join(rel(e) for e in entries) or '(none found)'}")
    _apps = [e for e in entries if os.path.basename(e).startswith("app")]
    if not _apps:
        out()
        out("> ⚠ **NO APPLICATION ENTRY POINT WAS USED.** Everything below is")
        out("> reachability from the files listed above only, so LIVE is")
        out("> understated and ORPHAN is overstated — probably badly. Re-run")
        out("> with the real entry, e.g. `--entry app_v3.py`, before deleting")
        out("> anything. If the app is not at the repo root, give its path.")
        out()
    out(f"- **{len(live):,} LIVE** (reachable from an entry point)")
    out(f"- **{len(scripts):,} SCRIPT** (standalone, has a __main__ guard)")
    out(f"- **{len(orphans):,} ORPHAN** (unreachable, no __main__)")
    if broken:
        out(f"- {len(broken)} file(s) do not parse")
    out()

    if dyn:
        out("## ⚠ READ THIS FIRST — dynamic imports the census cannot follow")
        out()
        out("Each of these picks a module by a name computed at runtime, so a")
        out("module reached ONLY this way looks orphaned. While this list is")
        out("non-empty, treat ORPHAN as candidates, not as a delete list.")
        out()
        for p, ln, msg in dyn:
            out(f"- `{rel(p)}:{ln}` — {msg}")
        out()

    if broken:
        out("## Files that do not parse")
        out()
        for p, e in broken:
            out(f"- `{rel(p)}` — {e}")
        out()

    out("## ORPHAN — nothing imports these and they cannot be run")
    out()
    out("Largest first. For each, grep the repo for its module name before")
    out("deleting; a name in the dynamic list above may reach it.")
    out()
    if orphans:
        out("| file | KB | days since change |")
        out("|---|---:|---:|")
        for p in sorted(orphans, key=lambda x: -kb(x)):
            out(f"| `{rel(p)}` | {kb(p):.0f} | {age(p):.0f} |")
    else:
        out("_none_")
    out()

    out("## SCRIPT — runnable, but nothing imports them")
    out()
    out("One-off tools, probes and maintenance jobs. These are NOT dead by")
    out("default — decide one at a time, and consider moving the keepers to")
    out("a `scripts/` folder so the package tree holds only package code.")
    out()
    if scripts:
        out("| file | KB | days since change |")
        out("|---|---:|---:|")
        for p in sorted(scripts, key=lambda x: -kb(x)):
            out(f"| `{rel(p)}` | {kb(p):.0f} | {age(p):.0f} |")
    else:
        out("_none_")
    out()

    if dupes:
        out("## Same filename in more than one place")
        out()
        out("Usually an archived copy. Two files with one name is how a fix")
        out("lands in the copy nobody runs.")
        out()
        for b, v in sorted(dupes.items()):
            out(f"- **{b}**")
            for p in sorted(v):
                mark = " · LIVE" if p in live else ""
                out(f"    - `{rel(p)}` ({kb(p):.0f} KB){mark}")
        out()

    out("## LIVE — reachable, and what pulls each one in")
    out()
    out("A module imported by exactly one other is a candidate for merging;")
    out("one imported by many is load-bearing and worth reading before any")
    out("change.")
    out()
    out("| module | KB | imported by |")
    out("|---|---:|---:|")
    for p in sorted(live, key=lambda x: -kb(x)):
        n = len(who.get(p, ()))
        tag = "ENTRY" if p in entries else (str(n) if n else "—")
        out(f"| `{rel(p)}` | {kb(p):.0f} | {tag} |")
    out()

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(L))
        print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
