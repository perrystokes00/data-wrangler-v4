"""
repo_audit.py — is `dataview` self-contained, and what at the repo root is actually reachable?

READ ONLY. Reports. Never moves, never deletes, never writes outside its own report file.

Answers three questions with evidence rather than inference:

  1. SHADOW COPIES — the dangerous one. Every extractor import in bulk_dir_loader.py is
     `try: from dataview.import_data import X / except: import X`. If the package import
     fails for any reason, Python silently falls back to a flat copy on sys.path. A stale
     copy of page_dir_loader.py in modules/ or loaders/ would be picked up with no error.
     Same failure family as 44 numbered copies in Downloads: silent, and the app keeps
     saying it worked.

  2. REACHABLE — top-level entries reached from the entry points by static import, or
     named in a string literal (path constants, _opt_import("mod"), open("assets/...")).

  3. ORPHANS — everything else. CANDIDATES for archiving, not a verdict. A static scan
     cannot see a path built at runtime or typed into the UI. Verify before moving.

Usage (from the repo root):
    python repo_audit.py
    python repo_audit.py --root . --entry app_v3.py --report repo_audit.txt
"""
import argparse
import ast
import os
import subprocess
import sys
from collections import defaultdict

# Never flag these — infrastructure, not code.
_IGNORE = {".git", ".streamlit", "venv", ".venv", "__pycache__", ".idea", ".vscode",
           ".gitignore", ".pytest_cache", "node_modules", ".mypy_cache"}

_SKIP_WALK = {".git", "venv", ".venv", "__pycache__", "node_modules", ".pytest_cache",
              ".mypy_cache"}


def _py_files(root):
    """Every .py under root, skipping virtualenvs and caches."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_WALK]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _parse(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return ast.parse(fh.read(), filename=path)
    except (SyntaxError, OSError):
        return None


def _imports_of(tree):
    """(module_names, string_literals) for one parsed file.

    String literals matter as much as imports here: _opt_import("dlis_header_loader")
    and open(r"dataview\\schema_registry\\...") are both invisible to an import scan.
    """
    mods, strings = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:      # absolute only; relative stays in-package
                mods.add(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value.strip()
            if s and len(s) < 300:
                strings.add(s)
    return mods, strings


def _module_index(root):
    """{module_name: [file, ...]} for every importable .py at or below the root.

    A name with more than one file is importable from more than one place — which is
    exactly what makes the try/except fallback able to pick the wrong one.
    """
    idx = defaultdict(list)
    for p in _py_files(root):
        base = os.path.splitext(os.path.basename(p))[0]
        if base == "__init__":
            base = os.path.basename(os.path.dirname(p))
        idx[base].append(os.path.relpath(p, root))
    return idx


def _git_tracked(root):
    """Set of git-tracked paths, or None if this isn't a repo / git isn't on PATH.
    Tracked-ness decides how safe a move is: `git mv` on a tracked file is reversible."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True, timeout=30)
        if out.returncode != 0:
            return None
        return {line.strip().replace("/", os.sep) for line in out.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return None


def _path_like(s, d):
    """Does string `s` reference directory `d` AS A PATH?

    The bare-word test this replaces was wrong and produced confident nonsense:
    'file_catalog' matched the SQL schema prefix in `file_catalog.GLOBAL_FILE_CATALOG`,
    'schemas' matched a WITSML XML namespace URL, 'mapping'/'documents'/'exports'/'vault'
    matched ordinary English in docstrings. Every one was reported REACHABLE with
    evidence that proved nothing.

    A path reference has a separator after the directory name. Require it.
    """
    n = s.replace("\\", "/")
    return (d + "/") in n


def _top_of(relpath):
    return relpath.split(os.sep)[0]


def _scan_group(root, files):
    """(modules, strings, files_parsed) union over one group of files."""
    mods, strings, n = set(), set(), 0
    for p in files:
        tree = _parse(p)
        if tree is None:
            continue
        n += 1
        m, s = _imports_of(tree)
        mods |= m
        strings |= s
    return mods, strings, n


def audit(root, entries, package="dataview", archive="_ARCHIVE_dead"):
    root = os.path.abspath(root)
    pkg_dir = os.path.join(root, package)

    top_level = sorted(e for e in os.listdir(root) if e not in _IGNORE)
    top_dirs = [e for e in top_level if os.path.isdir(os.path.join(root, e))]

    # ── 1. shadow copies ─────────────────────────────────────────────────────────────
    # Scoped honestly: a copy is only IMPORTABLE if its directory can land on sys.path
    # — i.e. if a script is run from inside it. Subdirectories are not on sys.path just
    # by existing. What every copy IS, unconditionally, is a second file with the same
    # name that you might edit instead of the live one.
    pkg_names = set(_module_index(pkg_dir)) if os.path.isdir(pkg_dir) else set()
    outside = defaultdict(list)
    for p in _py_files(root):
        rel = os.path.relpath(p, root)
        if _top_of(rel) == package:
            continue
        base = os.path.splitext(os.path.basename(p))[0]
        if base in pkg_names:
            outside[base].append(rel)
    shadows = {k: sorted(v) for k, v in sorted(outside.items())}

    # ── 2. reachability, SEPARATED BY ENTRY POINT ────────────────────────────────────
    # Lumping app_v3.py together with tools/ and scripts/ let one-off utility scripts
    # vouch for directories the app never touches. "Reachable from the app" and
    # "reachable from some script" are different facts and must not be merged.
    groups = {}
    app_files = list(_py_files(pkg_dir)) if os.path.isdir(pkg_dir) else []
    for e in entries:
        ep = os.path.join(root, e)
        if os.path.isfile(ep):
            app_files.append(ep)
    groups["app"] = app_files
    for e in entries:
        ep = os.path.join(root, e)
        if os.path.isdir(ep):
            groups[e] = list(_py_files(ep))

    scanned = 0
    reach = {}
    for gname, files in groups.items():
        mods, strings, n = _scan_group(root, files)
        scanned += n
        hits = {}
        for d in top_dirs:
            why = []
            if any(m == d or m.startswith(d + ".") for m in mods):
                why.append("imported as a package")
            paths = sorted(s for s in strings if _path_like(s, d))
            if paths:
                why.append("path string: " + ", ".join(repr(h) for h in paths[:2]))
            if why:
                hits[d] = why
        reach[gname] = hits

    tracked = _git_tracked(root)
    return {"root": root, "package": package, "top_level": top_level, "top_dirs": top_dirs,
            "shadows": shadows, "reach": reach, "scanned": scanned, "tracked": tracked,
            "archive": archive,
            "entries": [e for e in entries if os.path.exists(os.path.join(root, e))]}


def report(a):
    L = []
    w = L.append
    arch = a["archive"]
    app_reach = a["reach"].get("app", {})
    other = {g: h for g, h in a["reach"].items() if g != "app"}

    w("=" * 78)
    w(f"repo audit — {a['root']}")
    w(f"package: {a['package']}/   ·   entry points: {', '.join(a['entries']) or '(none found)'}")
    w(f"{a['scanned']} python file(s) parsed")
    w("=" * 78)

    w("")
    w("1. DUPLICATE MODULES  — the same module name in two places")
    w("-" * 78)
    if not a["shadows"]:
        w("  none. Every module under the package is unique in the tree.")
    else:
        w(f"  {len(a['shadows'])} module name(s) exist inside the package AND outside it.")
        w("")
        w("  What this IS: two files you could edit, one of which is not running. Same")
        w("  problem as N numbered copies in a Downloads folder, one level up.")
        w("")
        w("  What this is NOT (usually): a runtime hijack. Subdirectories are not on")
        w("  sys.path merely by existing, so `import X` from the repo root will NOT find")
        w("  a copy in subdir/X.py — the try/except fallback just fails. The hijack is")
        w("  real only when a script is run FROM INSIDE one of these directories, which")
        w("  puts that directory on sys.path[0] and lets its siblings win.")
        w("")
        for name, paths in a["shadows"].items():
            in_arch = [p for p in paths if _top_of(p) == arch]
            live = [p for p in paths if _top_of(p) != arch]
            if not live:
                continue                       # already archived — not a live duplicate
            w(f"  ⚠ {name}")
            w(f"      in package : {a['package']}{os.sep}...{os.sep}{name}.py")
            for p in live:
                w(f"      ALSO AT   : {p}")
            for p in in_arch:
                w(f"      (archived): {p}")
        w("")
        w("  → loaded_modules.txt records which copy actually loaded. That trace is")
        w("    truth; this scan only proves the ambiguity exists.")

    w("")
    w("2. TOP-LEVEL DIRECTORIES")
    w("-" * 78)
    w("  APP      = reached from the package or an entry-point FILE (what runs)")
    w("  script   = reached only from a utility directory (tools/, scripts/ ...)")
    w("  orphan?  = neither — a CANDIDATE, not a verdict")
    w("")
    tracked = a["tracked"]
    for e in a["top_dirs"]:
        if e == a["package"]:
            status, why = "PACKAGE", "the package itself"
        elif e == arch:
            status, why = "ARCHIVE", "the archive itself — nothing to do"
        elif e in app_reach:
            status = "APP"
            why = "; ".join(app_reach[e])
        elif e in other:
            status = "ENTRY"
            why = "scanned as an entry point — not a candidate"
        else:
            vouchers = [g for g, h in other.items() if e in h]
            if vouchers:
                status = "script"
                why = ("reached only from " + ", ".join(f"{v}/" for v in vouchers)
                       + " — the app does not touch it")
            else:
                status, why = "orphan?", "no import and no path string from any scanned file"
        tr = ""
        if tracked is not None:
            n = sum(1 for p in tracked if _top_of(p) == e)
            tr = f"  [{n} tracked]" if n else "  [UNTRACKED — git mv won't save you]"
        w(f"  {e:<24} {status:<9}{tr}")
        w(f"      {why}")

    cands = [e for e in a["top_dirs"]
             if e not in app_reach and e not in (a["package"], arch)
             and e not in other                      # entry points vouch for themselves
             and not any(e in h for h in other.values())]
    w("")
    w("3. ARCHIVE CANDIDATES")
    w("-" * 78)
    if not cands:
        w("  none.")
    else:
        w("  " + ", ".join(cands))
        w("")
        w("  NOT a verdict. A static scan cannot see a path built at runtime, typed into")
        w("  the UI, or read from a config file. Directories holding data, SQL or docs")
        w("  that nothing imports will land here and may still be needed.")
        w("")
        w("  Move, don't delete — one at a time, app green between each:")
        for e in cands:
            w(f"      git mv {e} {arch}/{e}")
        w("")
        w("  Anything UNTRACKED above is not in git. Copy it somewhere safe first;")
        w("  there is no `git checkout` to undo a bad move.")

    w("")
    w("=" * 78)
    w("Static analysis proves a reference EXISTS. It cannot prove one is ABSENT.")
    w("Section 3 is a list of questions, not answers.")
    w("=" * 78)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Read-only repo structure audit.")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--package", default="dataview", help="package name (default: dataview)")
    ap.add_argument("--entry", action="append", default=None,
                    help="entry-point file or dir; repeatable (default: app_v3.py, tools, scripts)")
    ap.add_argument("--report", default=None, help="also write the report to this file")
    args = ap.parse_args()

    entries = args.entry or ["app_v3.py", "tools", "scripts"]
    if not os.path.isdir(args.root):
        print(f"no such directory: {args.root}", file=sys.stderr)
        return 2
    a = audit(args.root, entries, args.package)
    txt = report(a)
    print(txt)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        print(f"\nwritten: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
