"""
find_dt.py — locate the 'name datetime is not defined' bug.

Run from the repo root:
    python find_dt.py

Finds .py files that CALL bare datetime.now/utcnow/strptime/strftime/today()
but never import datetime in any form (plain, from-import, or inline
__import__). Skips venv / _ARCHIVE / tools. Prints each offending file and
the exact call lines. Also runs a second pass for the shadowing case
(datetime used as a local variable), in case no missing-import file is found.
"""
import os
import re

# Repo root = the directory this script is run from.
ROOT = os.path.abspath(os.path.dirname(__file__)) if "__file__" in dir() else os.getcwd()

CALL   = re.compile(r'(?<![\w.])datetime\.(now|utcnow|strptime|strftime|today)\s*\(')
IMP    = re.compile(r'^\s*(import\s+datetime|from\s+datetime\s+import)', re.M)
INLINE = re.compile(r"__import__\(\s*['\"]datetime['\"]\s*\)")


def _iter_py(root):
    for dp, _dn, fn in os.walk(root):
        low = dp.lower()
        if "venv" in low or "_archive" in low or os.sep + "tools" in low:
            continue
        for f in fn:
            if f.endswith(".py"):
                yield os.path.join(dp, f)


def main():
    print(f"Scanning: {ROOT}\n")
    missing = []
    for p in _iter_py(ROOT):
        try:
            src = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        # drop full-line comments so a commented example doesn't count
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        if CALL.search(code) and not IMP.search(src) and not INLINE.search(code):
            lines = [f"    L{i + 1}: {l.strip()}"
                     for i, l in enumerate(src.splitlines())
                     if CALL.search(l) and not l.lstrip().startswith("#")]
            missing.append(p + "\n" + "\n".join(lines))

    print("=" * 70)
    print("FILES THAT CALL datetime.* WITH NO datetime IMPORT (the bug):")
    print("=" * 70)
    if missing:
        print("\n\n".join(missing))
    else:
        print("  none — no plain missing-import case.")
        print("\n  Checking the SHADOWING case (datetime used as a variable name)...")
        shadow = []
        shadow_pat = re.compile(r'^\s*datetime\s*=', re.M)
        for p in _iter_py(ROOT):
            try:
                src = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            if shadow_pat.search(src):
                lines = [f"    L{i + 1}: {l.strip()}"
                         for i, l in enumerate(src.splitlines())
                         if re.match(r'\s*datetime\s*=', l)]
                shadow.append(p + "\n" + "\n".join(lines))
        if shadow:
            print("  Possible shadowing (datetime assigned as a variable):")
            print("\n\n".join(shadow))
        else:
            print("  No shadowing either. The bug may be a dynamically imported"
                  " or archived module on sys.path — check what the promote step"
                  " actually imports.")
    print("=" * 70)


if __name__ == "__main__":
    main()
