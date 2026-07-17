"""
dead_code.py — what in this tree can never run, and what nothing ever calls.

READ ONLY. Reports. Never edits.

Three findings, in descending order of how much you should trust them:

  1. UNREACHABLE   — statements after return / raise / break / continue, or after a call
                     that never returns (st.rerun(), st.stop(), sys.exit()). This is
                     PROOF, not inference. It is also the one that actually bit:

                         _go_b(ss, directory)
                         st.rerun()
                         if c:
                             _listing(...)      # can never run — st.rerun() raises

                     No linter flags that, because st.rerun() looks like a normal call.

  2. NEVER CALLED  — a function defined here whose name appears nowhere else in the tree.
                     Good signal, but see the caveats: dynamic dispatch defeats it.

  3. NEVER IMPORTED — a module no other module imports. Weakest: entry points, scripts run
                     from the CLI, and modules loaded by string all look dead.

CAVEATS — read before deleting anything:
  * Streamlit dispatches on strings (`_opt_import("dlis_header_loader")`), and app_v3 picks
    pages by `S.app_mode`. Neither is visible to a name scan.
  * The alias line `render = main = show = app = run` keeps `run` alive under four names.
    Handled — aliases are counted as references.
  * A `__main__` block, a pytest test, or a .bat file can be the only caller. .bat and SQL
    are NOT scanned.
  * Reflection (getattr(mod, name)) is invisible.

So: section 1 is a defect list. Sections 2 and 3 are questions.

Usage:
    python dead_code.py --root dataview/import_data
    python dead_code.py --root dataview --report dead.txt
"""
import argparse
import ast
import os
import sys
from collections import defaultdict

_SKIP = {".git", "venv", ".venv", "__pycache__", "node_modules", ".pytest_cache",
         ".mypy_cache", "_ARCHIVE_dead"}

# Calls that never return control to the next statement. Streamlit's rerun/stop raise
# internally; anything after them in the same block is dead and looks perfectly normal.
_NO_RETURN = {"rerun", "stop", "experimental_rerun", "exit", "_exit", "abort"}
_NO_RETURN_QUAL = {"st.rerun", "st.stop", "st.experimental_rerun", "sys.exit", "os._exit"}


def _py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _qual(node):
    """Dotted name of a call target: st.rerun -> 'st.rerun'; rerun -> 'rerun'."""
    f = node.func if isinstance(node, ast.Call) else node
    if isinstance(f, ast.Attribute):
        base = f.value
        if isinstance(base, ast.Name):
            return f"{base.id}.{f.attr}"
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _terminates(stmt):
    """Does this statement guarantee the next one in the block never runs?"""
    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        q = _qual(stmt.value)
        if q in _NO_RETURN_QUAL:
            return True
        # bare rerun()/stop() imported directly
        if "." not in q and q in _NO_RETURN and q not in ("exit", "abort"):
            return True
    return False


def unreachable(tree, path):
    """[(line, why, snippet)] for statements that can never execute."""
    out = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if not _terminates(stmt):
                    continue
                nxt = block[i + 1]
                why = (type(stmt).__name__.lower() if not isinstance(stmt, ast.Expr)
                       else f"{_qual(stmt.value)}() never returns")
                out.append((getattr(nxt, "lineno", 0), why,
                            f"{len(block) - i - 1} statement(s) after it"))
                break                      # one report per block is enough
    return sorted(set(out))


def defined_names(tree):
    """{name: lineno} for module-level defs and classes. Leading-underscore names are
    included — private does not mean unused, and a private helper nothing calls is exactly
    what this is looking for. Dunders are skipped."""
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not (n.name.startswith("__") and n.name.endswith("__")):
                out[n.name] = n.lineno
    return out


def referenced_names(tree):
    """Every bare name and attribute used anywhere — the denominator for 'never called'.

    Includes the RHS of assignments, so `render = main = show = app = run` correctly counts
    as a reference to `run`. Includes string constants, because _opt_import("mod") and
    getattr(m, "fn") pass names as strings.
    """
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            s = n.value.strip()
            if s and len(s) < 100 and s.isidentifier():
                names.add(s)          # "run" in getattr(m, "run") / _opt_import("mod")
    return names


def imported_modules(tree):
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[-1])
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                mods.add(n.module.split(".")[-1])
            for a in n.names:
                mods.add(a.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            s = n.value.strip()
            if s.isidentifier():
                mods.add(s)           # _opt_import("dlis_header_loader")
    return mods


def scan(root):
    root = os.path.abspath(root)
    files = sorted(_py_files(root))
    trees, errs = {}, []
    for p in files:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                trees[p] = ast.parse(fh.read(), filename=p)
        except (SyntaxError, OSError) as e:
            errs.append((os.path.relpath(p, root), str(e).splitlines()[0][:120]))

    unreach = {}
    defs = {}
    all_refs = set()
    all_imports = set()
    for p, t in trees.items():
        rel = os.path.relpath(p, root)
        u = unreachable(t, p)
        if u:
            unreach[rel] = u
        defs[rel] = defined_names(t)
        all_refs |= referenced_names(t)
        all_imports |= imported_modules(t)

    # a def is "never called" if its name appears nowhere as a reference in the whole tree.
    # its own def line is not a reference, so a name used only at its definition is unused.
    never = {}
    for rel, d in defs.items():
        hits = [(n, ln) for n, ln in sorted(d.items(), key=lambda kv: kv[1])
                if n not in all_refs]
        if hits:
            never[rel] = hits

    mod_names = {os.path.splitext(os.path.basename(p))[0] for p in trees}
    unimported = sorted(m for m in mod_names
                        if m not in all_imports and m not in ("__init__",))
    return {"root": root, "files": len(trees), "unreach": unreach, "never": never,
            "unimported": unimported, "errs": errs, "defs": defs}


def report(sc):
    L = []
    w = L.append
    w("=" * 78)
    w(f"dead code — {sc['root']}")
    w(f"{sc['files']} file(s) parsed" + (f", {len(sc['errs'])} unparseable" if sc["errs"] else ""))
    w("=" * 78)

    w("")
    w("1. UNREACHABLE  — proven: these statements cannot execute")
    w("-" * 78)
    if not sc["unreach"]:
        w("  none.")
    else:
        n = sum(len(v) for v in sc["unreach"].values())
        w(f"  {n} block(s). This is the finding to act on — the rest are questions.")
        w("")
        for rel, items in sorted(sc["unreach"].items()):
            for line, why, extra in items:
                w(f"  {rel}:{line}")
                w(f"      unreachable — preceding statement is a {why}; {extra}")
        w("")
        w("  A `st.rerun()` / `st.stop()` raises. Anything after it in the same block is")
        w("  dead and looks completely normal — no linter flags it.")

    w("")
    w("2. NEVER CALLED  — defined here, name appears nowhere in this tree")
    w("-" * 78)
    if not sc["never"]:
        w("  none.")
    else:
        n = sum(len(v) for v in sc["never"].values())
        w(f"  {n} name(s). NOT proof. Verify each before deleting:")
        w("    · called from OUTSIDE this root? re-run with --root at the repo root")
        w("    · called by name from a .bat, a test, or SQL? not scanned")
        w("    · reached via getattr / a string this scan didn't recognise as an identifier?")
        w("")
        for rel, items in sorted(sc["never"].items()):
            w(f"  {rel}")
            for name, ln in items:
                w(f"      :{ln:<5} {name}")

    w("")
    w("3. NEVER IMPORTED  — no module in this tree imports them")
    w("-" * 78)
    if not sc["unimported"]:
        w("  none.")
    else:
        w("  " + ", ".join(sc["unimported"]))
        w("")
        w("  Weakest signal of the three. An entry point, a CLI script run by hand, a")
        w("  page dispatched by string, or anything a .bat launches will all appear here")
        w("  and be perfectly alive.")

    if sc["errs"]:
        w("")
        w("UNPARSEABLE")
        w("-" * 78)
        for rel, e in sc["errs"]:
            w(f"  {rel}: {e}")
        w("  Not scanned. Unknown, not clean.")

    w("")
    w("=" * 78)
    w("Section 1 is proof. Sections 2 and 3 are questions — a name scan cannot see")
    w("dynamic dispatch, and this codebase dispatches on strings in at least two places.")
    w("=" * 78)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Read-only dead code report.")
    ap.add_argument("--root", default=".", help="directory to scan (default: cwd)")
    ap.add_argument("--report", default=None, help="also write the report to this file")
    args = ap.parse_args()
    if not os.path.isdir(args.root):
        print(f"no such directory: {args.root}", file=sys.stderr)
        return 2
    txt = report(scan(args.root))
    print(txt)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        print(f"\nwritten: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
