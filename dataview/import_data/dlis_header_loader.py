"""
dlis_header_loader.py — extract DLIS headers + curves into bulk-loader staging CSVs.

Mirrors las_header_loader's OUTPUT SHAPE and SIGNATURE so the same pipeline (review → map →
promote) handles DLIS logs with no per-format special-casing:

  well_log.csv        uwi, log_id, log_type, run_num, log_date, top_depth, base_depth,
                      depth_ouom, source
  well_log_curve.csv  uwi, log_id, curve_id, mnemonic, curve_description, curve_unit,
                      min_value, max_value, depth_ouom, source

Column names are the TARGET TABLE's actual names (dataview_schema_full.json), so the loader
auto-maps every one and no function rule is needed. The previous version emitted UWI/LOG_ID/
CURVE_NAME/RUN_NO — pre-DDL-alignment names that mapped nowhere or forced concat rules.

UWI is often absent from DLIS origins → left blank for the review/assign-UWI step. WELL_NAME
is carried so the reviewer can seed a well. Per-curve min/max are read from the frame unless
the file exceeds MAX_SCAN_MB, in which case only mnemonic+unit are emitted (min/max blank).
"""
import os, csv, glob, hashlib

MAX_SCAN_MB = 60          # above this, skip the frame data read and emit mnemonic+unit only
NULL_SENTINELS = (-999.25, -999.2, -9999.0, -999.0)
_INDEX_NAMES = ("TDEP", "DEPT", "DEPTH", "MD")


def entity_id(*parts):
    """Canonical DataView id: SHA1(UTF-16-LE, uppercased, trimmed) as uppercase hex.

    Matches SQL Server HASHBYTES('SHA1', UPPER(LTRIM(RTRIM(x)))), entity_seeder.py's
    ba_id/field_id, and bulk_dir_loader._fn_map_id. Exactly 40 characters — which is
    exactly the width of dv_well_log_curve.curve_id.

    Do NOT swap the encoding for UTF-8: the same name would hash differently here than in
    SQL, and the two systems would disagree about which row is which.
    """
    s = "|".join(str(p if p is not None else "") for p in parts)
    return hashlib.sha1(s.upper().strip().encode("utf-16-le")).hexdigest().upper()


def find_dlis(directory, recursive=False):
    """Every .dlis under `directory`, de-duplicated.

    Globbing '*.dlis' and '*.DLIS' returns each file TWICE on a case-insensitive filesystem
    (Windows/NTFS) — the old code concatenated both lists and sorted, so every log and every
    curve was emitted twice, with no error and no warning. Normalise and de-dup on the real
    path. (las_header_loader.find_las carries the same fix; witsml_header_loader happened to
    be safe because it wrapped its globs in set().)
    """
    hits = []
    for p in ("*.dlis", "*.DLIS"):
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

    Depths were formatted with f"{v:.4g}" — four significant digits. Harmless for a LAS at
    5,000 ft; not harmless for a DLIS index in 0.1-inch units, where 152749 became
    '1.527e+05' and 49 units vanished with no error. The exponent form is also a gamble on
    TRY_CONVERT parsing it into a numeric column.

    repr() round-trips a float exactly and only reaches for exponents on genuinely huge or
    tiny magnitudes; format(v, 'f') pins it to plain decimal and strips the trailing zeros
    that would otherwise pad every value out to 6 places.
    """
    if v == "" or v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = format(f, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _origin_fields(f):
    """well_name, uwi, field, company, run from the first DLIS origin."""
    wn = uwi = field = company = run = ""
    if f.origins:
        o = f.origins[0]
        wn = str(getattr(o, "well_name", "") or "")
        uwi = str(getattr(o, "api_well", "") or getattr(o, "uwi", "") or
                  getattr(o, "well_id", "") or "")
        field = str(getattr(o, "field_name", "") or "")
        company = str(getattr(o, "company", "") or "")
        run = str(getattr(o, "run_number", "") or getattr(o, "run", "") or "")
    return wn, uwi, field, company, run


def _curve_minmax(frame, want_minmax):
    """{channel_name: (min, max)} from frame data, excluding null sentinels.
    {} if skipped or failed — an empty dict means UNKNOWN, and callers emit blank min/max
    rather than a fabricated 0."""
    if not want_minmax or frame is None:
        return {}
    import numpy as np
    out = {}
    try:
        data = frame.curves()
        for n in data.dtype.names:
            col = data[n]
            if col.dtype.kind == "f":
                col = col[np.isfinite(col)]
                for s in NULL_SENTINELS:
                    col = col[col != s]
            if len(col):
                out[n] = (float(np.min(col)), float(np.max(col)))
    except Exception:
        return {}
    return out


def _index_uom(f, frame):
    """Depth unit DETECTED from the index channel, never assumed. '' when unknowable."""
    try:
        names = {str(c.name).upper(): c for c in (f.channels or [])}
        if frame is not None and getattr(frame, "index", None):
            ic = names.get(str(frame.index).upper())
            if ic is not None:
                return str(getattr(ic, "units", "") or "").upper()
        for n in _INDEX_NAMES:
            if n in names:
                return str(getattr(names[n], "units", "") or "").upper()
    except Exception:
        pass
    return ""


def extract_file(path, source="DLIS"):
    """Parse one DLIS file → (log_rows, curve_rows). May raise; the caller traps per file.

    A DLIS physical file can hold SEVERAL logical files. The old code read files[0] only and
    silently dropped the rest. Every logical file becomes its own log row here.
    """
    import dlisio
    stem = os.path.splitext(os.path.basename(path))[0]
    size_mb = os.path.getsize(path) / 1e6 if os.path.exists(path) else 0
    want_minmax = size_mb <= MAX_SCAN_MB
    log_rows, curve_rows = [], []

    with dlisio.dlis.load(path) as files:
        for li, f in enumerate(files, 1):
            wn, uwi, field, company, run = _origin_fields(f)
            frame = f.frames[0] if f.frames else None

            # log_id follows the DataView convention LOG_<uwi> — NOT the filename. A
            # filename-derived id ('LOG_' + a 60-char DLIS name) is what blew past
            # dv_well_log_curve.curve_id at 40 chars. Fall back to the stem only when the
            # origin carries no UWI at all, and suffix multi-logical-file DLIS.
            uwi_key = "".join(ch for ch in str(uwi or "") if ch.isalnum())
            base_id = f"LOG_{uwi_key}" if uwi_key else f"LOG_{stem}"
            log_id = base_id if len(files) == 1 else f"{base_id}_{li}"

            depth_ouom = _index_uom(f, frame)
            mm = _curve_minmax(frame, want_minmax)
            top = base = ""
            seen, rows = set(), []
            for ch in (f.channels or []):
                name = str(ch.name)
                if name in seen:                      # DLIS repeats index channels; keep first
                    continue
                seen.add(name)
                unit = str(getattr(ch, "units", "") or "")
                desc = str(getattr(ch, "long_name", "") or "")
                lo, hi = mm.get(name, ("", ""))
                if name.upper() in _INDEX_NAMES and lo != "" and top == "":
                    top, base = _num(lo), _num(hi)    # depth: full precision, not .4g
                    continue                          # the depth index is the log range
                rows.append({
                    "uwi": uwi, "log_id": log_id,
                    # curve_id is NOT NULL and no source column carries it. Generate it here
                    # so no concat rule is needed — and hash it, because
                    # '{log_id}_{mnemonic}' does not fit 40 chars for real DLIS mnemonics.
                    # Truncating instead would silently collapse two curves into one row.
                    "curve_id": entity_id(uwi, log_id, name),
                    "mnemonic": name,
                    "curve_description": desc, "curve_unit": unit,
                    # min/max land in NUMERIC columns, so .4g was wrong here too: not for
                    # the lost precision, but because it emits 1.527e+05, and an exponent
                    # that TRY_CONVERT rejects becomes a silent NULL.
                    "min_value": _num(lo), "max_value": _num(hi),
                    "depth_ouom": depth_ouom, "source": source})
            curve_rows.extend(rows)
            log_rows.append({
                "uwi": uwi, "log_id": log_id, "log_type": "DLIS", "log_date": "",
                "run_num": run or "1", "top_depth": top, "base_depth": base,
                "depth_ouom": depth_ouom, "source": source,
                "well_name": wn, "file_path": os.path.abspath(path),
                "file_format": "DLIS"})
    return log_rows, curve_rows


def extract_directory(directory, source="DLIS", files=None, recursive=False):
    """Parse every .dlis in a directory (or the given `files`) → (log_rows, curve_rows)."""
    log_rows, curve_rows = [], []
    paths = files if files is not None else find_dlis(directory, recursive)
    seen = set()
    for path in sorted(paths):
        key = os.path.normcase(os.path.abspath(path))    # never parse the same file twice
        if key in seen:
            continue
        seen.add(key)
        try:
            lrs, crs = extract_file(path, source)
            log_rows.extend(lrs)
            curve_rows.extend(crs)
        except Exception as e:
            # A failed file must be VISIBLE, not absent. It lands as a log row whose
            # well_name carries the error, so the review screen shows it instead of the
            # file quietly not existing.
            stem = os.path.splitext(os.path.basename(path))[0]
            log_rows.append({
                "uwi": "", "log_id": f"LOG_{stem}", "log_type": "DLIS", "log_date": "",
                "run_num": "1", "top_depth": "", "base_depth": "", "depth_ouom": "",
                "source": source, "well_name": f"[extract error: {e}]",
                "file_path": os.path.abspath(path), "file_format": "DLIS"})
    return log_rows, curve_rows


def write_staging_csvs(directory, out_dir=None, source="DLIS", files=None, recursive=False):
    """Extract DLIS → well_log.csv + well_log_curve.csv. Returns
    (log_csv_path, curve_csv_path, n_logs, n_curves).

    `files` — an explicit, already-gated and de-duplicated list of paths (what
    bulk_dir_loader computes). When given, `directory` is only the default out_dir. Without
    this parameter the loader's file list is discarded and the extractor re-globs the folder
    — which re-reads files the gate said to skip and misses subfolders on a recursive scan.
    `recursive` — when globbing ourselves (no `files`), walk subfolders too.
    """
    out_dir = out_dir or directory
    os.makedirs(out_dir, exist_ok=True)
    log_rows, curve_rows = extract_directory(directory, source, files=files,
                                             recursive=recursive)
    # target-table column names so the loader auto-maps every one
    # file_path/file_format are real dv_well_log columns. well_name is NOT — it is
    # carried for the UWI gate, which shows it so the reviewer can identify the well.
    # It will surface as an unmapped source column in Match & Map; skip it there once
    # and the decision is recorded.
    log_cols = ["uwi", "log_id", "log_type", "run_num", "log_date", "top_depth",
                "base_depth", "depth_ouom", "file_path", "file_format", "well_name",
                "source"]
    curve_cols = ["uwi", "log_id", "curve_id", "mnemonic", "curve_description",
                  "curve_unit", "min_value", "max_value", "depth_ouom", "source"]
    lp = os.path.join(out_dir, "dlis_well_log.csv")
    cp = os.path.join(out_dir, "dlis_well_log_curve.csv")
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
