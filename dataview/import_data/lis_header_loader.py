"""
lis_header_loader.py — extract LIS headers + curves into bulk-loader staging CSVs.

Standalone (uses dlisio's LIS reader; no dependency on the app's lis_catalog). Emits the same
shape and SIGNATURE as las_header_loader / dlis_header_loader so the shared review → map →
promote path handles LIS logs with no per-format special-casing:

  lis_well_log.csv        uwi, log_id, log_type, run_num, log_date, top_depth, base_depth,
                          depth_ouom, source
  lis_well_log_curve.csv  uwi, log_id, curve_id, mnemonic, curve_description, curve_unit,
                          min_value, max_value, depth_ouom, source

Column names are the TARGET TABLE's actual names, so the loader auto-maps every one and no
function rule is needed. The previous version emitted UWI/LOG_ID/CURVE_NAME/RUN_NO — pre-DDL
names, which forced _suggest_functions to propose `curve_id = seq_concat(...{seq})` (a key
built from a ROW NUMBER — not stable across runs) and `mnemonic = constant ''` (which would
have stamped every curve's mnemonic as empty).

UWI is usually absent from LIS headers → blank for the review/assign-UWI step; well name and
operator are carried so the reviewer can seed a well.
"""
import os, csv, glob, hashlib

MAX_SCAN_MB = 60
NULL_SENTINELS = (-999.25, -999.2, -9999.0, -999.0)
_INDEX_NAMES = ("DEPT", "DEPTH", "MD", "TDEP")


def entity_id(*parts):
    """Canonical DataView id: SHA1(UTF-16-LE, uppercased, trimmed) as uppercase hex.

    IDENTICAL to dlis_header_loader.entity_id and bulk_dir_loader._fn_map_id, and matches
    SQL Server HASHBYTES('SHA1', UPPER(LTRIM(RTRIM(x)))). Exactly 40 characters — the width
    of dv_well_log_curve.curve_id.

    If you change the recipe, change it in EVERY copy. The same name hashed UTF-8 here and
    UTF-16-LE there produces two different ids for one curve — that exact split between
    entity_seeder.py and the pipeline's FK resolution is already on record.
    """
    s = "|".join(str(p if p is not None else "") for p in parts)
    return hashlib.sha1(s.upper().strip().encode("utf-16-le")).hexdigest().upper()


def find_lis(directory, recursive=False):
    """Every .lis under `directory`, de-duplicated.

    Globbing '*.lis' and '*.LIS' returns each file TWICE on a case-insensitive filesystem
    (Windows/NTFS) — the old code concatenated both lists and sorted, so every log and curve
    was emitted twice, silently. Normalise and de-dup on the real path.
    """
    hits = []
    for p in ("*.lis", "*.LIS"):
        hits += (glob.glob(os.path.join(directory, "**", p), recursive=True) if recursive
                 else glob.glob(os.path.join(directory, p)))
    seen, out = set(), []
    for h in hits:
        key = os.path.normcase(os.path.abspath(h))
        if key not in seen:
            seen.add(key)
            out.append(h)
    return sorted(out)


def _num(v):
    """A depth as text, FULL PRECISION, never scientific notation.

    Identical to dlis_header_loader._num. Values were formatted with f"{v:.4g}" — four
    significant digits — which silently rounded a DLIS/LIS index in 0.1-inch units
    (152749 -> '1.527e+05' -> 152700). Curve min/max used it too: the precision there is
    display-grade and genuinely didn't matter, but the FORMAT did — both columns are NUMERIC,
    and an exponent TRY_CONVERT won't parse becomes a silent NULL. staging_qa caught 47 of
    them in one run.
    """
    if v == "" or v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = format(f, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _wellsite_header(lf):
    """{MNEM: value} from LIS wellsite records (well name, operator, field, date, UWI if any).
    Identity rows look like ('WN  ','UNAL','    ','    ','A/5-1') — short, text value last."""
    hdr = {}
    try:
        for wsd in lf.wellsite_data():
            try:
                rows = wsd.table(simple=True)
            except Exception:
                continue
            for row in rows:
                try:
                    n = len(row)                  # works for tuple/list and numpy void records
                except TypeError:
                    continue
                if n < 2:
                    continue
                first, last = row[0], row[-1]
                mnem = (first.decode() if isinstance(first, bytes) else str(first)).strip().upper()
                val = "" if last is None else (
                    last.decode() if isinstance(last, bytes) else str(last)).strip()
                if mnem and val and n <= 5 and mnem not in hdr:
                    hdr[mnem] = val
    except Exception:
        pass
    return hdr


def _hget(hdr, *keys):
    for k in keys:
        if k.upper() in hdr:
            return hdr[k.upper()]
    return ""


def extract_file(path, source="LIS"):
    """Parse one LIS file → (log_rows, curve_rows). May raise; the caller traps per file.

    A LIS file can hold several logical files, and each logical file several data format
    specs (passes). The old code read files[0] and `break`ed after the first spec — every
    other pass was dropped with no error. Both are walked here.
    """
    from dlisio import lis
    import numpy as np
    stem = os.path.splitext(os.path.basename(path))[0]
    size_mb = os.path.getsize(path) / 1e6 if os.path.exists(path) else 0
    want_minmax = size_mb <= MAX_SCAN_MB
    log_rows, curve_rows = [], []

    with lis.load(path) as files:
        for li, lf in enumerate(files, 1):
            hdr = _wellsite_header(lf)
            well_name = _hget(hdr, "WN", "WELL")
            uwi = _hget(hdr, "UWI", "API", "WI")
            operator = _hget(hdr, "CN", "COMP", "OPERATOR")
            date = _hget(hdr, "DATE")
            run = _hget(hdr, "RUN", "RUN_NO", "RUNN")

            # log_id follows the DataView convention LOG_<uwi> — NOT the filename. A
            # filename-derived id is what pushed curve_id past its 40-char column.
            uwi_key = "".join(ch for ch in str(uwi or "") if ch.isalnum())
            base_id = f"LOG_{uwi_key}" if uwi_key else f"LOG_{stem}"
            log_id = base_id if len(files) == 1 else f"{base_id}_{li}"

            top = base = ""
            depth_ouom = ""
            seen, rows = set(), []
            for fmt in lf.data_format_specs():             # every pass, not just the first
                mm = {}
                if want_minmax:
                    try:
                        curves = lis.curves(lf, fmt)
                        for n in curves.dtype.names:
                            col = curves[n]
                            if col.dtype.kind == "f":
                                col = col[np.isfinite(col)]
                                for s in NULL_SENTINELS:
                                    col = col[col != s]
                            if len(col):
                                mm[str(n).strip()] = (float(np.min(col)), float(np.max(col)))
                    except Exception:
                        mm = {}                            # {} means UNKNOWN, not zero
                for sp in fmt.specs:
                    name = str(getattr(sp, "mnemonic", "")).strip()
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    unit = str(getattr(sp, "units", "") or "").strip()
                    lo, hi = mm.get(name, ("", ""))
                    if name.upper() in _INDEX_NAMES:
                        # the depth index is the log's range and its unit — detected, not assumed
                        if not depth_ouom:
                            depth_ouom = unit.upper()
                        if lo != "" and top == "":
                            top, base = _num(lo), _num(hi)   # depth: full precision, not .4g
                        continue
                    rows.append({
                        "uwi": uwi, "log_id": log_id,
                        # curve_id is NOT NULL and no source column carries it. Generate it
                        # here so no rule is needed — and hash it: it is stable across runs
                        # (unlike a {seq} row number) and always fits 40 chars (unlike a
                        # concat, which would be truncated and collide two curves into one).
                        "curve_id": entity_id(uwi, log_id, name),
                        "mnemonic": name,
                        "curve_description": "", "curve_unit": unit,
                        # min/max land in NUMERIC columns, so .4g was wrong here too: not
                        # for the lost precision, but because it emits 1.527e+05, and an
                        # exponent that TRY_CONVERT rejects becomes a silent NULL.
                        "min_value": _num(lo), "max_value": _num(hi),
                        "depth_ouom": depth_ouom, "source": source})
            # depth_ouom may only be discovered on a later pass — backfill the earlier rows
            for r in rows:
                r["depth_ouom"] = r["depth_ouom"] or depth_ouom
            curve_rows.extend(rows)
            log_rows.append({
                "uwi": uwi, "log_id": log_id, "log_type": "LIS", "log_date": date,
                "run_num": run or "1", "top_depth": top, "base_depth": base,
                "depth_ouom": depth_ouom, "source": source,
                "well_name": well_name, "operator": operator,
                "file_path": os.path.abspath(path), "file_format": "LIS"})
    return log_rows, curve_rows


def extract_directory(directory, source="LIS", files=None, recursive=False):
    """Parse every .lis in a directory (or the given `files`) → (log_rows, curve_rows)."""
    log_rows, curve_rows = [], []
    paths = files if files is not None else find_lis(directory, recursive)
    seen = set()
    for path in sorted(paths):
        key = os.path.normcase(os.path.abspath(path))     # never parse the same file twice
        if key in seen:
            continue
        seen.add(key)
        try:
            lrs, crs = extract_file(path, source)
            log_rows.extend(lrs)
            curve_rows.extend(crs)
        except Exception as e:
            # A failed file must be VISIBLE, not absent.
            stem = os.path.splitext(os.path.basename(path))[0]
            log_rows.append({
                "uwi": "", "log_id": f"LOG_{stem}", "log_type": "LIS", "log_date": "",
                "run_num": "1", "top_depth": "", "base_depth": "", "depth_ouom": "",
                "source": source, "well_name": f"[extract error: {e}]", "operator": "",
                "file_path": os.path.abspath(path), "file_format": "LIS"})
    return log_rows, curve_rows


def write_staging_csvs(directory, out_dir=None, source="LIS", files=None, recursive=False):
    """Extract LIS → lis_well_log.csv + lis_well_log_curve.csv. Returns
    (log_csv_path, curve_csv_path, n_logs, n_curves).

    `files` — an explicit, already-gated and de-duplicated list of paths (what
    bulk_dir_loader computes). Without this parameter the loader's list is discarded and the
    extractor re-globs the folder — re-reading files the gate said to skip and missing
    subfolders on a recursive scan.
    """
    out_dir = out_dir or directory
    os.makedirs(out_dir, exist_ok=True)
    log_rows, curve_rows = extract_directory(directory, source, files=files,
                                             recursive=recursive)
    # file_path/file_format are real dv_well_log columns. well_name is NOT — it is
    # carried for the UWI gate, which shows it so the reviewer can identify the well.
    # It will surface as an unmapped source column in Match & Map; skip it there once
    # and the decision is recorded.
    log_cols = ["uwi", "log_id", "log_type", "run_num", "log_date", "top_depth",
                "base_depth", "depth_ouom", "file_path", "file_format", "well_name",
                "source"]
    curve_cols = ["uwi", "log_id", "curve_id", "mnemonic", "curve_description",
                  "curve_unit", "min_value", "max_value", "depth_ouom", "source"]
    lp = os.path.join(out_dir, "lis_well_log.csv")
    cp = os.path.join(out_dir, "lis_well_log_curve.csv")
    with open(lp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=log_cols, extrasaction="ignore")
        w.writeheader(); w.writerows(log_rows)
    with open(cp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=curve_cols, extrasaction="ignore")
        w.writeheader(); w.writerows(curve_rows)
    return lp, cp, len(log_rows), len(curve_rows)


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    rec = "--recursive" in sys.argv
    lp, cp, nl, nc = write_staging_csvs(d, recursive=rec)
    print(f"{nl} log(s) -> {lp}")
    print(f"{nc} curve(s) -> {cp}")
