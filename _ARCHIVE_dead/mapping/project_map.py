#!/usr/bin/env python
"""
project_map.py - map and search a Data Wrangler project tree.

Run from your project root (the folder containing app_v3.py).
No third-party dependencies.

  python project_map.py                  # full map of every .py file
  python project_map.py "Flagged only"   # which file(s) contain a string
  python project_map.py --dupes          # list shadow risks vs parked copies
  python project_map.py --audit          # for every shadow: live copy + del cmd
  python project_map.py --imports <name> # where a module is imported, and how

The map shows, per file: line count, last-modified time, the module
docstring's first line, and any st.title / st.tabs / def render|run it
defines - so you can see at a glance which module draws which screen.
It also flags any filename that exists in more than one folder, because
Python imports whichever copy is earliest on sys.path (usually the root
one) and the others silently shadow it.
"""
from __future__ import annotations
import sys, re, ast, warnings
from pathlib import Path
from datetime import datetime

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "env",
             "node_modules", ".idea", "build", "dist"}

# Parked / archived copies live here - present on disk but not on the import
# path, so they don't shadow. Duplicates that are ONLY here are harmless.
ARCHIVE_DIRS = {"backup", "download", "docs", ".vs", "old", "archive",
                "deprecated", "tmp", "_old", "bak"}


def iter_py(root: Path):
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def first_docline(text: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # ignore bad-escape SyntaxWarnings
            doc = ast.get_docstring(ast.parse(text)) or ""
        lines = [ln.strip() for ln in doc.splitlines() if ln.strip()]
        return lines[0] if lines else ""
    except Exception:
        return ""


def ui_markers(text: str) -> str:
    bits = []
    m = re.search(r'st\.title\(\s*["\'](.+?)["\']', text)
    if m:
        bits.append(f'title="{m.group(1)}"')
    tabs = re.search(r'st\.tabs\(\s*\[(.*?)\]', text, re.S)
    if tabs:
        labels = re.findall(r'["\'](.+?)["\']', tabs.group(1))
        if labels:
            bits.append("tabs=[" + " | ".join(labels[:6]) + "]")
    defs = sorted(set(re.findall(r'^\s*def (render\w*|run)\b', text, re.M)))
    if defs:
        bits.append("entry:" + ",".join(defs))
    return "  ".join(bits)


def _collect(root: Path):
    rows, seen = [], {}
    for p in iter_py(root):
        txt = _read(p)
        rel = p.relative_to(root)
        rows.append({
            "rel": str(rel),
            "lines": len(txt.splitlines()),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "doc": first_docline(txt),
            "ui": ui_markers(txt),
        })
        seen.setdefault(p.name, []).append(str(rel))
    rows.sort(key=lambda r: r["rel"].lower())
    return rows, seen


def _is_archived(rel: str) -> bool:
    return any(part.lower() in ARCHIVE_DIRS for part in Path(rel).parts)


def _print_dupes(seen):
    # A real shadow risk = same filename in 2+ LIVE (non-archive) locations,
    # ignoring package markers.
    risk, parked = {}, {}
    for n, ps in seen.items():
        if n == "__init__.py":
            continue
        live = [p for p in ps if not _is_archived(p)]
        if len(live) > 1:
            risk[n] = live
        elif len(ps) > 1:
            parked[n] = ps

    if risk:
        print("\n!!  SHADOW RISK - same module in 2+ live locations:")
        for n, ps in sorted(risk.items()):
            print(f"    {n}")
            for pp in ps:
                print(f"        {pp}")
        print("    -> A bare `import X` takes the root copy; `from modules.X`\n"
              "       takes the modules copy. Pick ONE, delete the other, and\n"
              "       run:  python project_map.py --imports <name>")
    else:
        print("\nNo live shadow conflicts. OK")

    if parked:
        print("\n(parked copies - in backup/download/etc., not on import path:)")
        for n in sorted(parked):
            print(f"    {n}")


def _analyze_imports(root: Path, stem: str):
    """Return (sites, forms) where sites is a list of (relpath, lineno, label,
    line) and forms maps a category ('modules'/'root'/'pkg') to a count."""
    pats = [
        (re.compile(rf'^\s*from\s+modules\.{re.escape(stem)}\s+import', re.M), "from modules.", "modules"),
        (re.compile(rf'^\s*import\s+modules\.{re.escape(stem)}\b', re.M),       "import modules.", "modules"),
        (re.compile(rf'^\s*from\s+{re.escape(stem)}\s+import', re.M),           "from <root>", "root"),
        (re.compile(rf'^\s*import\s+{re.escape(stem)}\b', re.M),                "import <root>", "root"),
        (re.compile(rf'^\s*from\s+[\w.]+\.{re.escape(stem)}\s+import', re.M),   "from <pkg>.", "pkg"),
    ]
    sites, forms = [], {}
    for p in iter_py(root):
        if _is_archived(str(p.relative_to(root))):
            continue  # imports inside backup/download don't count
        for i, line in enumerate(_read(p).splitlines(), 1):
            for rx, label, cat in pats:
                if rx.match(line):
                    sites.append((str(p.relative_to(root)), i, label, line.strip()))
                    forms[cat] = forms.get(cat, 0) + 1
                    break
    return sites, forms


def _verdict(forms) -> str:
    """One of: 'root', 'modules', 'mixed', 'package', 'none'."""
    has_root, has_mod = "root" in forms, "modules" in forms
    if has_root and has_mod:
        return "mixed"
    if has_mod:
        return "modules"
    if has_root:
        return "root"
    if "pkg" in forms:
        return "package"
    return "none"


def cmd_imports(root: Path, name: str):
    stem = name[:-3] if name.endswith(".py") else name
    sites, forms = _analyze_imports(root, stem)
    for rel, i, label, line in sites:
        print(f"{rel}:{i}:  [{label}]  {line[:90]}")
    if not sites:
        print(f"No imports of '{stem}' found.")
        return
    print(f"\n{len(sites)} import site(s). Forms used: " +
          ", ".join(f"{k} x{v}" for k, v in forms.items()))
    v = _verdict(forms)
    if v == "mixed":
        print("!!  MIXED: imported both as root and as modules.<name> - the two\n"
              "    physical copies are both live and WILL drift. Consolidate.")
    elif v == "modules":
        print(f"-> Live copy: modules\\{stem}.py   (delete the root copy)")
    elif v == "root":
        print(f"-> Live copy: root {stem}.py   (delete the modules copy)")


def cmd_audit(root: Path):
    """For every root-vs-modules shadow, name the live copy and the dead one."""
    _, seen = _collect(root)
    print("SHADOW AUDIT - live copy vs delete recommendation")
    print("=" * 60)
    handled = 0
    for basename, paths in sorted(seen.items()):
        if basename == "__init__.py":
            continue
        live_copies = [p for p in paths if not _is_archived(p)]
        if len(live_copies) < 2:
            continue
        has_root = basename in live_copies
        has_mod = f"modules\\{basename}" in live_copies or f"modules/{basename}" in live_copies
        stem = basename[:-3]
        _, forms = _analyze_imports(root, stem)
        v = _verdict(forms)
        handled += 1
        print(f"\n{basename}")
        for p in live_copies:
            print(f"    has: {p}")
        if has_root and has_mod and v in ("root", "modules"):
            dead = f"modules\\{basename}" if v == "root" else basename
            live = basename if v == "root" else f"modules\\{basename}"
            print(f"    LIVE: {live}   (imported {v})")
            print(f"    DEAD: del {dead}")
            # any other live copies (assets\ etc.) are strays to review
            for p in live_copies:
                if p not in (basename, f"modules\\{basename}", f"modules/{basename}"):
                    print(f"    stray (review): {p}")
        elif v == "mixed":
            print("    !! MIXED imports (both root and modules.<name> used).")
            print("       Consolidate by hand - both copies are currently live.")
        elif v == "none":
            print("    not imported anywhere - these are run-directly scripts.")
            print("       Not an import shadow; dedupe by hand if they're stale.")
        else:
            print(f"    verdict: {v} - review by hand "
                  "(run: python project_map.py --imports " + stem + ")")
    if not handled:
        print("\nNo live shadow conflicts. OK")


def cmd_map(root: Path):
    rows, seen = _collect(root)
    w = max((len(r["rel"]) for r in rows), default=20)
    print(f"{'FILE'.ljust(w)}  LINES  MODIFIED          SUMMARY")
    print("-" * (w + 45))
    for r in rows:
        print(f"{r['rel'].ljust(w)}  {r['lines']:5}  {r['mtime']}  {r['doc'][:60]}")
        if r["ui"]:
            print(f"{' ' * w}         {' ' * 16}  {r['ui'][:100]}")
    print(f"\n{len(rows)} python files.")
    _print_dupes(seen)


def cmd_search(root: Path, needle: str):
    hits = 0
    for p in iter_py(root):
        for i, line in enumerate(_read(p).splitlines(), 1):
            if needle in line:
                print(f"{p.relative_to(root)}:{i}:  {line.strip()[:120]}")
                hits += 1
    print(f"\n{hits} match(es) for \"{needle}\"." if hits
          else f"No file contains \"{needle}\".")


def cmd_dupes(root: Path):
    _, seen = _collect(root)
    _print_dupes(seen)


def main():
    root = Path.cwd()
    args = sys.argv[1:]
    if not args:
        cmd_map(root)
    elif args[0] == "--dupes":
        cmd_dupes(root)
    elif args[0] == "--audit":
        cmd_audit(root)
    elif args[0] == "--imports":
        if len(args) < 2:
            print("usage: python project_map.py --imports <module_name>")
        else:
            cmd_imports(root, args[1])
    else:
        cmd_search(root, " ".join(args))


if __name__ == "__main__":
    main()
