"""
find_refs.py — every reference to a directory name, with file:line, so YOU can judge.

Why this exists: repo_audit.py only counts a string as a path reference if it contains a
separator ('assets/geo/x.json'). That deliberately rejects bare words, because 'file_catalog'
also happens to be a SQL schema name and 'mapping' is an ordinary English word — without the
separator rule the audit called everything REACHABLE and proved nothing.

But the rule has a cost, and it's this:

    os.path.join("exports", fname)      # literal is "exports" — a BARE WORD
    Path("assets") / "geo" / "x.json"   # literal is "assets"  — a BARE WORD

Both are real runtime references that the audit reports as `orphan?`. Moving a directory on
that evidence would break a path — and if the code degrades gracefully when the file is
missing (as the geography layer does), it breaks it SILENTLY.

This tool takes the opposite trade: report EVERY mention, classify how it's used, and let a
human decide. Noise you can read beats a confident wrong answer.

READ ONLY. Reports. Never moves anything.

Usage (from the repo root):
    python find_refs.py exports assets vault documents
    python find_refs.py --all-candidates
"""
import argparse
import ast
import os
import sys

_SKIP = {".git", "venv", ".venv", "__pycache__", "node_modules", ".pytest_cache",
         ".mypy_cache", ".idea", ".vscode"}

# Calls whose string arguments are almost certainly path components.
_PATH_CALLS = {"join", "Path", "PurePath", "open", "exists", "isdir", "isfile", "makedirs",
               "mkdir", "listdir", "walk", "glob", "iglob", "rglob", "read_text",
               "read_bytes", "write_text", "to_csv", "read_csv", "abspath", "relpath"}


def _py_files(root, skip_dirs=()):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP and d not in skip_dirs]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _callee_name(node):
    """Best-effort name of the thing being called: os.path.join -> 'join', Path -> 'Path'."""
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _path_call_strings(tree):
    """{id(const_node): callee} for string constants passed to a path-ish call.

    This is the whole point of the tool: it catches os.path.join("exports", f), where the
    literal carries no separator and the audit's rule therefore rejects it.
    """
    marked = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name not in _PATH_CALLS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                marked[id(arg)] = name
    return marked


def _classify(s, name, in_path_call):
    """How is this mention being used? Strongest evidence first."""
    n = s.replace("\\", "/")
    if in_path_call:
        return "PATH CALL", f"passed to {in_path_call}() — a runtime path"
    if (name + "/") in n:
        return "PATH", "string contains the directory with a separator"
    if n.strip() == name:
        return "bare word", "could be a path component, a schema name, or prose"
    return "mention", "appears inside a longer string"


def scan(root, names, skip_dirs=()):
    hits = {n: [] for n in names}
    parsed = failed = 0
    for p in _py_files(root, skip_dirs):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=p)
        except (SyntaxError, OSError):
            failed += 1
            continue
        parsed += 1
        marked = _path_call_strings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            s = node.value
            if len(s) > 400:
                continue
            low = s.replace("\\", "/").lower()
            for n in names:
                if n.lower() not in low:
                    continue
                kind, why = _classify(s, n, marked.get(id(node)))
                hits[n].append({"file": os.path.relpath(p, root),
                                "line": getattr(node, "lineno", 0),
                                "kind": kind, "why": why,
                                "text": s if len(s) <= 90 else s[:87] + "..."})
    return hits, parsed, failed


_ORDER = {"PATH CALL": 0, "PATH": 1, "bare word": 2, "mention": 3}


def report(root, names, hits, parsed, failed):
    L = []
    w = L.append
    w("=" * 78)
    w(f"reference scan — {root}")
    w(f"{parsed} file(s) parsed" + (f", {failed} unparseable" if failed else ""))
    w("=" * 78)
    for n in names:
        rows = sorted(hits[n], key=lambda r: (_ORDER[r["kind"]], r["file"], r["line"]))
        w("")
        w(f"{n}/  — {len(rows)} mention(s)")
        w("-" * 78)
        if not rows:
            w("  none. No python file mentions this name in any string.")
            w("  → Safe to move as far as PYTHON is concerned. Batch files, SQL, JSON")
            w("    config and the Streamlit UI are NOT scanned by this tool.")
            continue
        strong = [r for r in rows if r["kind"] in ("PATH CALL", "PATH")]
        for r in rows:
            w(f"  [{r['kind']:<9}] {r['file']}:{r['line']}")
            w(f"      {r['text']!r}")
            w(f"      {r['why']}")
        w("")
        if strong:
            w(f"  → {len(strong)} reference(s) look like real paths. Moving this directory")
            w("    WILL break them unless you update each one. Check whether the code")
            w("    degrades gracefully when the file is missing — if it does, the break")
            w("    will be SILENT.")
        else:
            w("  → No path-shaped reference. The mentions above are likely prose, a SQL")
            w("    schema name, or a variable name. Probably safe — read them and decide.")
    w("")
    w("=" * 78)
    w("This tool over-reports on purpose. It scans PYTHON STRING LITERALS ONLY:")
    w("not .bat files, not SQL, not JSON config, not paths typed into the UI,")
    w("not paths assembled from variables at runtime. Absence of a hit is not proof.")
    w("=" * 78)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Find every reference to a directory name.")
    ap.add_argument("names", nargs="*", help="directory names to look for")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--skip", action="append", default=["_ARCHIVE_dead"],
                    help="directory to skip while scanning; repeatable")
    ap.add_argument("--report", default=None, help="also write the report to this file")
    args = ap.parse_args()

    if not args.names:
        ap.error("name at least one directory, e.g.  python find_refs.py exports assets")
    if not os.path.isdir(args.root):
        print(f"no such directory: {args.root}", file=sys.stderr)
        return 2

    root = os.path.abspath(args.root)
    hits, parsed, failed = scan(root, args.names, skip_dirs=set(args.skip))
    txt = report(root, args.names, hits, parsed, failed)
    print(txt)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        print(f"\nwritten: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
