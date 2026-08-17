"""
las_header_loader.py — extract LAS file headers into bulk-loader staging CSVs.

Reads a directory of LAS 2.0 files and writes two CSVs shaped for dv_well_log and
dv_well_log_curve, so the existing bulk_dir_loader pipeline (map → FK → promote → verify)
can load them with no new plumbing:

  well_log.csv        UWI, LOG_ID, LOG_TYPE, LOG_DATE, RUN_NO, TOP_DEPTH, BASE_DEPTH, SOURCE
  well_log_curve.csv  UWI, LOG_ID, CURVE_NAME, CURVE_UNIT, SOURCE  (header-only; no min/max)

log_id follows LOG_<uwi>. Intake is HEADER-ONLY: the ~A data section is never read, so no
per-curve min/max is computed. Depth-index curve (first in ~C) is skipped.
"""
import os, csv, glob


def _parse_line(line):
    """LAS mnemonic line 'MNEM.UNIT  VALUE : DESCRIPTION' → (mnem, unit, value, descr)."""
    left, _, descr = line.partition(":")
    left = left.strip()
    mnem, _, rest = left.partition(".")
    mnem = mnem.strip()
    rest = rest.strip()
    parts = rest.split(None, 1)
    if not parts:
        unit, value = "", ""
    elif len(parts) == 1:
        # 'GAPI' alone = unit (curve line, no value); a lone number = value (no unit)
        unit, value = (parts[0], "") if not parts[0].replace(".", "").replace("-", "").isdigit() else ("", parts[0])
    else:
        unit, value = parts[0], parts[1].strip()
    return mnem, unit, value, descr.strip()


def parse_las(path):
    """Parse one LAS file → (well_dict, [curve_dicts], [data_rows])."""
    section = None
    well, curves, data = {}, [], []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("~"):
                u = s[1:2].upper()
                section = {"V": "V", "W": "W", "C": "C", "P": "P", "A": "A", "O": "O"}.get(u)
                continue
            if section in ("W", "P"):                    # merge parameters with well info (RUN, etc.)
                m, unit, val, desc = _parse_line(s)
                well[m.upper()] = {"unit": unit, "value": val, "desc": desc}
            elif section == "C":
                m, unit, val, desc = _parse_line(s)
                import re
                desc = re.sub(r"^\s*\d+\s+", "", desc)   # LAS often prefixes the ordinal: "2  GAMMA RAY"
                curves.append({"mnem": m, "unit": unit, "desc": desc})
            # ~A data section is intentionally NOT read: LAS intake is header-only
            # (~V/~W/~P/~C). Curve VALUE stats (min/max) are not computed at catalog
            # time — that would require scanning every sample of every file.
    return well, curves, data


def _wget(well, *keys, default=""):
    for k in keys:
        if k.upper() in well and well[k.upper()]["value"]:
            return well[k.upper()]["value"]
    return default


def find_las(directory, recursive=False):
    """Every .las under `directory`, de-duplicated. Globbing both *.las and *.LAS returns
    each file TWICE on a case-insensitive filesystem (Windows/NTFS), which silently doubles
    every log and curve — so normalise and de-dup on the real path."""
    pats = ["*.las", "*.LAS"]
    hits = []
    for p in pats:
        hits += (glob.glob(os.path.join(directory, "**", p), recursive=True) if recursive
                 else glob.glob(os.path.join(directory, p)))
    seen, out = set(), []
    for h in hits:
        key = os.path.normcase(os.path.abspath(h))
        if key not in seen:
            seen.add(key)
            out.append(h)
    return sorted(out)


def extract_directory(directory, source="LAS", files=None, recursive=False):
    """Parse every .las in a directory (or the given `files`) → (log_rows, curve_rows)."""
    import re as _re
    log_rows, curve_rows = [], []
    paths = files if files is not None else find_las(directory, recursive)
    seen = set()
    used_log_ids = {}
    for path in sorted(paths):
        key = os.path.normcase(os.path.abspath(path))    # never parse the same file twice
        if key in seen:
            continue
        seen.add(key)
        well, curves, data = parse_las(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        uwi = _wget(well, "UWI", "API", "WELL")
        # LOG_ID follows the DataView convention LOG_<uwi> (matches well_log.csv), NOT the
        # filename — a filename-derived id blows past dv_well_log.log_id / dv_well_log_curve
        # .curve_id (varchar(40)) on its own. Fall back to the filename only if there's no UWI.
        uwi_key = _re.sub(r"[^A-Za-z0-9]", "", str(uwi or ""))
        base_id = f"LOG_{uwi_key}" if uwi_key else f"LOG_{stem}"
        n = used_log_ids.get(base_id, 0) + 1
        used_log_ids[base_id] = n
        log_id = base_id if n == 1 else f"{base_id}_{n}"   # 2nd LAS for a well → _2, _3, …
        # depth unit comes from the LAS itself (STRT .FT / .M) — never assumed
        depth_ouom = (well.get("STRT", {}).get("unit") or "FT").upper()
        log_rows.append({
            "uwi": uwi, "log_id": log_id, "log_type": "LAS",
            "log_date": _wget(well, "DATE", "DATE_LOG"),
            "run_num": _wget(well, "RUN", default="1"),
            "top_depth": _wget(well, "STRT"), "base_depth": _wget(well, "STOP"),
            "depth_ouom": depth_ouom, "source": source})

        # curve identity from ~C only (mnemonic / unit / description). Column 0 is the
        # depth index → skipped. No min/max: header-only intake never reads samples.
        seen_cv = {}
        for ci, c in enumerate(curves):
            if ci == 0:
                continue                                   # depth index, not a log curve
            # curve_id is NOT NULL with no source column — generate it here so no concat
            # rule is needed. log_id already carries the uwi; repeated mnemonics get _2, _3.
            n = seen_cv.get(c["mnem"], 0) + 1
            seen_cv[c["mnem"]] = n
            cid = f'{log_id}_{c["mnem"]}' + ("" if n == 1 else f"_{n}")
            curve_rows.append({
                "uwi": uwi, "log_id": log_id, "curve_id": cid[:40],
                "mnemonic": c["mnem"],
                "curve_description": c["desc"], "curve_unit": c["unit"],
                "depth_ouom": depth_ouom, "source": source})
    return log_rows, curve_rows


def write_staging_csvs(directory, out_dir=None, source="LAS", files=None, recursive=False):
    """Extract a directory of LAS files → well_log.csv + well_log_curve.csv in out_dir
    (defaults to the LAS directory). Returns (log_csv_path, curve_csv_path, n_logs, n_curves).

    `files` — an explicit, already-deduplicated list of LAS paths (what bulk_dir_loader
    passes). When given, `directory` is only used as the default out_dir; this is what makes
    a recursive scan work, since the files may live in subfolders.
    `recursive` — when globbing ourselves (no `files`), walk subfolders too."""
    out_dir = out_dir or directory
    os.makedirs(out_dir, exist_ok=True)
    log_rows, curve_rows = extract_directory(directory, source, files=files, recursive=recursive)
    # target-table column names (INFORMATION_SCHEMA) so the loader auto-maps every one
    log_cols = ["uwi", "log_id", "log_type", "run_num", "log_date", "top_depth", "base_depth",
                "depth_ouom", "source"]
    curve_cols = ["uwi", "log_id", "curve_id", "mnemonic", "curve_description", "curve_unit",
                  "depth_ouom", "source"]
    log_path = os.path.join(out_dir, "well_log.csv")
    curve_path = os.path.join(out_dir, "well_log_curve.csv")
    with open(log_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=log_cols); w.writeheader(); w.writerows(log_rows)
    with open(curve_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=curve_cols); w.writeheader(); w.writerows(curve_rows)
    return log_path, curve_path, len(log_rows), len(curve_rows)


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    lp, cp, nl, nc = write_staging_csvs(d)
    print(f"{nl} logs -> {lp}")
    print(f"{nc} curves -> {cp}")
