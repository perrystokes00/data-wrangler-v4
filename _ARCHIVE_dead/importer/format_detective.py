"""
format_detective.py
===================
Layer 1 of the Universal Well Data Importer.

Automatically detects file type, encoding, structure, and extracts
a normalized preview DataFrame + metadata — without any prior knowledge
of the format.

Supported formats:
  - CSV / TSV / pipe-delimited
  - Fixed-width (with or without a header)
  - Excel (.xlsx, .xls)
  - JSON (records or column-oriented)
  - MAF016-style multi-record-type fixed-width files

Usage:
    from importer.format_detective import detect

    result = detect("training/Texas/maf016.cc003")
    print(result.summary())
    print(result.preview)          # pandas DataFrame, first 20 rows
    print(result.meta)             # dict of detected properties
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chardet
import pandas as pd


# ── Public API ────────────────────────────────────────────────────────

@dataclass
class DetectionResult:
    file_path:   str
    file_type:   str          # 'csv', 'tsv', 'pipe', 'fixed_width', 'excel', 'json', 'maf016'
    encoding:    str
    delimiter:   str | None   # None for fixed-width / excel / json
    has_header:  bool
    preview:     pd.DataFrame # first 20 rows, normalised column names
    raw_columns: list[str]    # original column names before normalisation
    sample_rows: list[dict]   # first 5 rows as plain dicts (for ML mapper)
    meta:        dict         # extra format-specific info
    warnings:    list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"File      : {self.file_path}",
            f"Type      : {self.file_type}",
            f"Encoding  : {self.encoding}",
            f"Delimiter : {self.delimiter!r}",
            f"Has header: {self.has_header}",
            f"Columns   : {len(self.raw_columns)}",
            f"Preview   : {len(self.preview)} rows",
        ]
        if self.warnings:
            lines.append("Warnings  :")
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


def detect(file_path: str, max_preview_rows: int = 20) -> DetectionResult:
    """
    Main entry point. Returns a DetectionResult for any supported file.
    Raises ValueError for unsupported / unreadable files.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = path.suffix.lower()

    # ── Excel ─────────────────────────────────────────────────────────
    if ext in (".xlsx", ".xls"):
        return _detect_excel(path, max_preview_rows)

    # ── JSON ──────────────────────────────────────────────────────────
    if ext == ".json":
        return _detect_json(path, max_preview_rows)

    # ── Text-based — detect encoding first ────────────────────────────
    encoding = _detect_encoding(path)
    raw = _read_text(path, encoding)

    # MAF016-style fixed-width with record type prefix?
    if _looks_like_maf016(raw):
        return _detect_maf016(path, raw, encoding, max_preview_rows)

    # Fixed-width (no obvious delimiter, consistent line lengths)?
    if _looks_like_fixed_width(raw):
        return _detect_fixed_width(path, raw, encoding, max_preview_rows)

    # Delimited
    return _detect_delimited(path, raw, encoding, max_preview_rows)


# ── Encoding ──────────────────────────────────────────────────────────

def _detect_encoding(path: Path, sample_bytes: int = 65536) -> str:
    with open(path, "rb") as f:
        raw = f.read(sample_bytes)
    result = chardet.detect(raw)
    enc = result.get("encoding") or "latin-1"
    # Normalise common aliases
    enc = enc.lower().replace("-", "_")
    mapping = {"ascii": "latin-1", "iso_8859_1": "latin-1",
               "windows_1252": "latin-1", "utf_8_sig": "utf-8"}
    return mapping.get(enc, enc)


def _read_text(path: Path, encoding: str, max_lines: int = 5000) -> str:
    lines = []
    with open(path, encoding=encoding, errors="replace") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            lines.append(line.rstrip("\n"))
    return "\n".join(lines)


# ── MAF016 detection ──────────────────────────────────────────────────

def _looks_like_maf016(raw: str) -> bool:
    """Lines starting with 30/31/32 + digits, length 200+."""
    lines = [l for l in raw.splitlines() if len(l) >= 200]
    if not lines:
        return False
    hits = sum(1 for l in lines[:50] if re.match(r"^(30|31|32)\d", l))
    return hits / max(len(lines[:50]), 1) > 0.5


def _detect_maf016(path: Path, raw: str, encoding: str,
                   max_rows: int) -> DetectionResult:
    """Parse MAF016 into a preview DataFrame using known column positions."""
    COLS = {
        "rec_type":    (0,  2),
        "district":    (3,  5),
        "county_code": (5,  8),
        "api_seq":     (8,  14),
        "sidetrack":   (14, 15),
        "lease_name":  (16, 71),
        "operator":    (71, 103),
        "total_depth": (103, 109),
        "field_name":  (164, 196),
        "spud_date":   (196, 204),
        "compl_date":  (214, 222),
        "well_code":   (238, 240),
    }
    rows = []
    for line in raw.splitlines():
        if len(line) < 200:
            continue
        if not re.match(r"^(30|31|32)\d", line):
            continue
        row = {}
        for col, (s, e) in COLS.items():
            row[col] = line[s:e].strip() if len(line) >= e else ""
        rows.append(row)
        if len(rows) >= max_rows:
            break

    df = pd.DataFrame(rows)
    return DetectionResult(
        file_path=str(path),
        file_type="maf016",
        encoding=encoding,
        delimiter=None,
        has_header=False,
        preview=df,
        raw_columns=list(COLS.keys()),
        sample_rows=rows[:5],
        meta={
            "record_types": list(df["rec_type"].unique()) if not df.empty else [],
            "col_positions": COLS,
            "total_lines_sampled": len(raw.splitlines()),
        },
    )


# ── Fixed-width detection ─────────────────────────────────────────────

def _looks_like_fixed_width(raw: str) -> bool:
    """Consistent line lengths and no obvious delimiter dominance."""
    lines = [l for l in raw.splitlines() if l.strip()][:100]
    if len(lines) < 5:
        return False
    lengths = [len(l) for l in lines]
    avg = sum(lengths) / len(lengths)
    consistent = sum(1 for l in lengths if abs(l - avg) < 5) / len(lengths)
    if consistent < 0.7:
        return False
    # Check delimiter frequency — fixed-width files rarely have many commas
    sample = "\n".join(lines[:20])
    comma_rate = sample.count(",") / max(len(sample), 1)
    pipe_rate  = sample.count("|") / max(len(sample), 1)
    tab_rate   = sample.count("\t") / max(len(sample), 1)
    return max(comma_rate, pipe_rate, tab_rate) < 0.01


def _detect_fixed_width(path: Path, raw: str, encoding: str,
                         max_rows: int) -> DetectionResult:
    """
    Use pandas read_fwf with inferred column widths.
    Attempts to detect header from first line.
    """
    warnings = []
    lines = [l for l in raw.splitlines() if l.strip()]

    # Try to infer with pandas
    try:
        df = pd.read_fwf(
            io.StringIO(raw),
            nrows=max_rows,
            encoding_errors="replace",
        )
        has_header = True
    except Exception as e:
        warnings.append(f"read_fwf failed ({e}), falling back to raw columns")
        df = pd.DataFrame({"raw_line": lines[:max_rows]})
        has_header = False

    cols = list(df.columns.astype(str))
    df.columns = [_normalise_col(c) for c in cols]

    return DetectionResult(
        file_path=str(path),
        file_type="fixed_width",
        encoding=encoding,
        delimiter=None,
        has_header=has_header,
        preview=df,
        raw_columns=cols,
        sample_rows=df.head(5).to_dict("records"),
        meta={"line_count_sampled": len(lines)},
        warnings=warnings,
    )


# ── Delimited detection ───────────────────────────────────────────────

def _detect_delimiter(raw: str) -> str:
    """Sniff delimiter from first 4KB."""
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
        return dialect.delimiter
    except csv.Error:
        # Count candidates manually
        counts = {d: sample.count(d) for d in [",", "\t", "|", ";"]}
        return max(counts, key=counts.get)


def _has_header(raw: str, delimiter: str) -> bool:
    try:
        return csv.Sniffer().has_header(raw[:4096])
    except csv.Error:
        # Heuristic: first row is header if its values are mostly non-numeric
        first = raw.splitlines()[0].split(delimiter)
        non_numeric = sum(1 for v in first if not v.strip().lstrip("-").replace(".","").isdigit())
        return non_numeric / max(len(first), 1) > 0.6


def _detect_delimited(path: Path, raw: str, encoding: str,
                       max_rows: int) -> DetectionResult:
    warnings = []
    delim    = _detect_delimiter(raw)
    header   = _has_header(raw, delim)

    type_map = {",": "csv", "\t": "tsv", "|": "pipe"}
    file_type = type_map.get(delim, "csv")

    try:
        df = pd.read_csv(
            io.StringIO(raw),
            sep=delim,
            header=0 if header else None,
            nrows=max_rows,
            encoding_errors="replace",
            low_memory=False,
        )
    except Exception as e:
        warnings.append(f"read_csv failed: {e}")
        df = pd.DataFrame()

    cols = list(df.columns.astype(str))
    df.columns = [_normalise_col(c) for c in cols]

    return DetectionResult(
        file_path=str(path),
        file_type=file_type,
        encoding=encoding,
        delimiter=delim,
        has_header=header,
        preview=df,
        raw_columns=cols,
        sample_rows=df.head(5).to_dict("records"),
        meta={"delimiter_char": repr(delim)},
        warnings=warnings,
    )


# ── Excel ─────────────────────────────────────────────────────────────

def _detect_excel(path: Path, max_rows: int) -> DetectionResult:
    warnings = []
    xl = pd.ExcelFile(path)
    sheets = xl.sheet_names

    if len(sheets) > 1:
        warnings.append(f"Multiple sheets: {sheets}. Using first: '{sheets[0]}'")

    df = pd.read_excel(path, sheet_name=sheets[0], nrows=max_rows)
    cols = list(df.columns.astype(str))
    df.columns = [_normalise_col(c) for c in cols]

    return DetectionResult(
        file_path=str(path),
        file_type="excel",
        encoding="binary",
        delimiter=None,
        has_header=True,
        preview=df,
        raw_columns=cols,
        sample_rows=df.head(5).to_dict("records"),
        meta={"sheets": sheets, "active_sheet": sheets[0]},
        warnings=warnings,
    )


# ── JSON ──────────────────────────────────────────────────────────────

def _detect_json(path: Path, max_rows: int) -> DetectionResult:
    warnings = []
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)

    if isinstance(data, list):
        df = pd.DataFrame(data[:max_rows])
    elif isinstance(data, dict):
        # Column-oriented: {"col": [v1,v2,...]}
        try:
            df = pd.DataFrame(data).head(max_rows)
        except Exception:
            warnings.append("Non-tabular JSON structure — flattened to single row")
            df = pd.DataFrame([data])
    else:
        raise ValueError("JSON root must be a list or dict")

    cols = list(df.columns.astype(str))
    df.columns = [_normalise_col(c) for c in cols]

    return DetectionResult(
        file_path=str(path),
        file_type="json",
        encoding="utf-8",
        delimiter=None,
        has_header=True,
        preview=df,
        raw_columns=cols,
        sample_rows=df.head(5).to_dict("records"),
        meta={"root_type": type(data).__name__},
        warnings=warnings,
    )


# ── Helpers ───────────────────────────────────────────────────────────

def _normalise_col(name: str) -> str:
    """Lowercase, strip, replace spaces/special chars with underscores."""
    name = str(name).strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")
    return name or "col"


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python format_detective.py <file_path>")
        sys.exit(1)
    result = detect(sys.argv[1])
    print(result.summary())
    print("\nPreview:")
    print(result.preview.to_string(max_rows=10, max_cols=12))
