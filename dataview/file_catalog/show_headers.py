"""
show_headers.py — what did the reader actually see?

The census says WHICH columns fell out; this says what the header was.
Between them there is no guessing left: a column reported unclaimed is
either a wording the vocabulary lacks, a wording that lost a tie, or a
header the reader mangled — and only the raw text tells you which.

    python -m dataview.file_catalog.show_headers --in <folder>
    python -m dataview.file_catalog.show_headers --in <folder> --shape formation_tops
    python -m dataview.file_catalog.show_headers --in <folder> --unclaimed-only
"""
from __future__ import annotations

import argparse
import os
import sys


def _repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def main(argv=None):
    if _repo_root() not in sys.path:
        sys.path.insert(0, _repo_root())
    from docshape.readers.tables import raw_tables
    from docshape.packs import load
    from docshape.packs.overlay import load_layered
    from docshape.engine.recognise import Recogniser

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--pack", default="petroleum")
    ap.add_argument("--shape", default=None,
                    help="only tables identifying as this shape")
    ap.add_argument("--unclaimed-only", action="store_true",
                    help="only tables with at least one unclaimed column")
    a = ap.parse_args(argv)

    try:
        pack, *_ = load_layered(a.pack, use_sandbox=False)
    except Exception:
        pack = load(a.pack)
    eng = Recogniser(pack)

    exts = {".pdf", ".docx", ".xlsx", ".xls", ".html", ".htm"}
    if os.path.isdir(a.src):
        files = []
        for root, _d, names in os.walk(a.src):
            for n in sorted(names):
                if n.startswith("~$") or n.startswith("._"):
                    continue          # Word lock stubs / resource forks
                if os.path.splitext(n)[1].lower() in exts:
                    files.append(os.path.join(root, n))
    else:
        files = [a.src]
    print(f"{len(files)} file(s)\n")
    for f in files:
        try:
            tabs = raw_tables(f)
        except Exception as e:
            print(f"{os.path.basename(f)}: READ FAILED — {e}")
            continue
        for name, rows in tabs.items():
            if not rows or name == "_error":
                continue
            header = list(rows[0].keys())
            shape, score, cm = eng.identify(header)
            taken = set(cm.values())
            unclaimed = [c for i, c in enumerate(header) if i not in taken]
            # PAIR-GRID SHAPES REPORT THEIR VALUES AS COLUMNS. In a
            # label/value grid the reader promotes row 0, so the first
            # pair's VALUES ("2024-01-08", "PERMIAN BASIN 4H") become
            # header cells — and nothing should claim them, because
            # pivot_pair_grid rearranges the whole table at capture. A
            # shape carrying a transform has already declared that its
            # header is not what it appears to be, so flag rather than
            # count: listing these as vocabulary gaps sends the reader
            # hunting for aliases that must never be written.
            pivoted = shape in (getattr(pack, "transforms", None) or {})
            if pivoted and unclaimed:
                unclaimed = []          # not gaps — values, by design
            if a.shape and shape != a.shape:
                continue
            if a.unclaimed_only and not unclaimed:
                continue
            print(f"{os.path.basename(f)} · {name} · "
                  f"{shape or 'UNKNOWN'} ({score:.2f}) "
                  f"{len(cm)}/{len(header)}"
                  + ("   [pair grid — header cells are VALUES; the "
                     "transform rearranges this at capture]"
                     if pivoted else ""))
            for i, c in enumerate(header):
                mark = "  " if i in taken else "??"
                fld = eng.field_for(c)
                # A column can be unclaimed for three DIFFERENT reasons and
                # the fix differs for each, so name which one it is.
                if i in taken:
                    why = ""
                elif not fld:
                    why = "  (resolves to nothing)"
                elif fld in cm:
                    why = f"  (BLOCKED — {header[cm[fld]]!r} filled {fld})"
                else:
                    why = f"  (resolves to {fld}, shape doesn't list it)"
                print(f"   {mark} {c!r:34} -> {str(fld):16}{why}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
