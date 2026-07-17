"""
pdf_probe.py — which PDFs have no text layer, and what does that cost?

READ ONLY. Opens each PDF, times pdfplumber's text extraction, reports which ones come back
empty. An empty text layer is what makes pdf_document_loader fall through to
_ocr_reconstruct: rasterize every page at scale=3, then tesseract each one. Seconds per PAGE,
not per file.

This probe does NOT run the OCR — it identifies the files that would trigger it, and times
the cheap half. The gap between this total and the scan's PDF phase IS the OCR cost.

Usage:
    python pdf_probe.py <directory>
    python pdf_probe.py <directory> --no-recurse
"""
import glob
import os
import sys
import time


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    recurse = "--no-recurse" not in sys.argv
    if not args:
        print("usage: python pdf_probe.py <directory> [--no-recurse]", file=sys.stderr)
        return 2
    d = args[0]
    if not os.path.isdir(d):
        print(f"no such directory: {d}", file=sys.stderr)
        return 2

    pat = os.path.join(d, "**", "*.pdf") if recurse else os.path.join(d, "*.pdf")
    # case-insensitive filesystems return the same path for *.pdf and *.PDF — dedup on the
    # real path or every file is counted twice (the exact bug that doubled the LAS curves).
    seen, paths = set(), []
    for p in glob.glob(pat, recursive=recurse) + glob.glob(pat.replace(".pdf", ".PDF"),
                                                           recursive=recurse):
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            paths.append(p)
    paths.sort()

    try:
        import pdfplumber
    except ImportError:
        print("pdfplumber not installed in this interpreter", file=sys.stderr)
        return 2

    print(f"{len(paths)} PDF(s) under {d}\n")
    print(f"{'sec':>7}  {'pages':>5}  {'chars':>8}  {'':<11} file")
    print("-" * 78)

    total = 0.0
    ocr_needed, ocr_pages = [], 0
    for p in paths:
        t0 = time.perf_counter()
        chars = pages = 0
        err = None
        try:
            with pdfplumber.open(p) as pdf:
                pages = len(pdf.pages)
                for pg in pdf.pages:
                    chars += len(pg.extract_text() or "")
        except Exception as e:
            err = str(e)[:40]
        dt = time.perf_counter() - t0
        total += dt
        flag = ""
        if err:
            flag = "ERROR"
        elif chars == 0:
            flag = "OCR NEEDED"
            ocr_needed.append((p, pages))
            ocr_pages += pages
        print(f"{dt:7.2f}  {pages:5d}  {chars:8d}  {flag:<11} {os.path.basename(p)}"
              + (f"  [{err}]" if err else ""))

    print("-" * 78)
    print(f"{total:7.2f}  total, text layer only (NO ocr)\n")

    if not ocr_needed:
        print("No PDF needs OCR. Whatever the scan's PDF phase is spending its time on,")
        print("it is NOT OCR — this probe just did the same work without it.")
    else:
        print(f"{len(ocr_needed)} of {len(paths)} PDF(s) have NO text layer -> _ocr_reconstruct")
        print(f"    {ocr_pages} page(s) to rasterize at scale=3 and run tesseract over.")
        for p, n in ocr_needed:
            print(f"      {n:3d}p  {os.path.basename(p)}")
        print()
        print("Cost check: scan's 'extract: PDF' phase MINUS this total ~= the OCR cost.")
        print("If that difference is small, OCR is not the answer and the time is elsewhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
