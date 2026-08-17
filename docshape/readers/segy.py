"""
docshape.readers.segy
=====================
SEG-Y seismic — header metadata only, read with struct, no library.

A 3200-byte EBCDIC textual header, a 400-byte binary header at fixed offsets,
and 240-byte trace headers. Trace SAMPLES are never read: a survey is
gigabytes, and what matters for a catalogue is the survey name, sample
interval, format, measurement system and the first trace's coordinates —
with the scalar applied, since getting that wrong is what puts a survey on the
wrong continent.

Trace count is computed from the file size rather than read, so a multi-
gigabyte survey costs one 4 KB read.
"""
from __future__ import annotations

import os
import re
import struct

SEGY_HEADER_COLS = [
    ("survey_name", "VARCHAR"), ("line_name", "VARCHAR"),
    ("sample_interval_us", "INTEGER"), ("samples_per_trace", "INTEGER"),
    ("format_code", "INTEGER"), ("format_name", "VARCHAR"),
    ("measurement_system", "VARCHAR"), ("segy_revision", "VARCHAR"),
    ("trace_count", "BIGINT"), ("file_mb", "DOUBLE"),
    ("record_length_ms", "DOUBLE"), ("trace_sorting", "VARCHAR"),
    ("first_cdp", "INTEGER"), ("coord_scalar", "INTEGER"),
    ("coord_units", "VARCHAR"), ("src_x", "DOUBLE"), ("src_y", "DOUBLE"),
    ("grp_x", "DOUBLE"), ("grp_y", "DOUBLE"),
    # The line's SHAPE, not just where it starts. "x1 y1;x2 y2;..." in the
    # file's own CRS — semicolon-separated so it stays one scalar column and
    # every backend can hold it without learning a geometry type. Reprojecting
    # it is the caller's job; this reader does not know what a datum is.
    ("trace_path", "VARCHAR"), ("path_points", "INTEGER"),
    ("textual_header", "VARCHAR"),
]

# How many trace headers to sample along a file for the line path. Five gives
# a usable shape for a straight 2D line; a dogleg or a crooked line wants more.
PATH_SAMPLES = 9

# Data sample format codes (SEG-Y rev 1/2) -> (bytes per sample, name)
_SEGY_FMT = {1: (4, "IBM float"), 2: (4, "int32"), 3: (2, "int16"),
             4: (4, "fixed-point w/ gain"), 5: (4, "IEEE float"),
             6: (8, "IEEE double"), 8: (1, "int8"), 9: (8, "int64"),
             10: (4, "uint32"), 11: (2, "uint16"), 12: (8, "uint64"),
             15: (3, "int24"), 16: (1, "uint8")}
_SORT = {0: "unknown", 1: "as recorded", 2: "CDP ensemble",
         3: "single fold continuous", 4: "horizontally stacked",
         5: "common source", 6: "common receiver", 7: "common offset",
         8: "common mid-point", 9: "common conversion point"}


def _ebcdic(raw):
    """3200-byte textual header -> 40 lines of 80 characters.

    Almost always EBCDIC (cp037); some vendors write ASCII. Decide by which
    decoding yields more printable characters rather than trusting either.
    """
    try:
        e = raw.decode("cp037", errors="replace")
    except Exception:
        e = ""
    a = raw.decode("ascii", errors="replace")
    pick = e if sum(c.isprintable() for c in e) >= sum(
        c.isprintable() for c in a) else a
    lines = [pick[i:i + 80].rstrip() for i in range(0, 3200, 80)]
    return "\n".join(l for l in lines if l.strip())


def _apply_scalar(value, scalar):
    """SEG-Y coordinate scalar: positive multiplies, negative divides."""
    if scalar in (0, None):
        return float(value)
    return float(value) * scalar if scalar > 0 else float(value) / abs(scalar)


def parse_segy(path):
    """Header metadata for one SEG-Y. Reads ~4KB, never the traces."""
    size = os.path.getsize(path)
    _path_pts = []
    with open(path, "rb") as f:
        text_raw = f.read(3200)
        binhdr = f.read(400)
        trace_raw = f.read(240)
        _fh = f          # kept for the sampling pass below
    if len(binhdr) < 400:
        return {}

    def i16(buf, off):
        return struct.unpack(">h", buf[off:off + 2])[0]

    def i32(buf, off):
        return struct.unpack(">i", buf[off:off + 4])[0]

    # Binary header offsets are 1-based in the spec; subtract 3201 for ours.
    interval = i16(binhdr, 16)          # 3217-3218
    samples = i16(binhdr, 20)           # 3221-3222
    fmt = i16(binhdr, 24)               # 3225-3226
    sort = i16(binhdr, 28)              # 3229-3230
    meas = i16(binhdr, 54)              # 3255-3256
    rev = i16(binhdr, 300)              # 3501-3502

    per_sample, fmt_name = _SEGY_FMT.get(fmt, (4, f"code {fmt}"))
    trace_bytes = 240 + (samples * per_sample if samples else 0)
    traces = int((size - 3600) // trace_bytes) if trace_bytes > 240 else None

    out = {
        "sample_interval_us": interval or None,
        "samples_per_trace": samples or None,
        "format_code": fmt or None, "format_name": fmt_name,
        "measurement_system": {1: "meters", 2: "feet"}.get(meas),
        "segy_revision": f"{rev >> 8}.{rev & 0xFF}" if rev else None,
        "trace_count": traces, "file_mb": round(size / 1048576.0, 2),
        "record_length_ms": (round(interval * samples / 1000.0, 1)
                             if interval and samples else None),
        "trace_sorting": _SORT.get(sort),
        "textual_header": _ebcdic(text_raw) or None,
    }

    if len(trace_raw) == 240:
        scalar = i16(trace_raw, 70)     # bytes 71-72
        units = i16(trace_raw, 88)      # bytes 89-90
        out.update({
            "first_cdp": i32(trace_raw, 20) or None,   # bytes 21-24
            "coord_scalar": scalar or None,
            "coord_units": {1: "length", 2: "arcseconds",
                            3: "decimal degrees", 4: "DMS"}.get(units),
            "src_x": _apply_scalar(i32(trace_raw, 72), scalar),   # 73-76
            "src_y": _apply_scalar(i32(trace_raw, 76), scalar),   # 77-80
            "grp_x": _apply_scalar(i32(trace_raw, 80), scalar),   # 81-84
            "grp_y": _apply_scalar(i32(trace_raw, 84), scalar),   # 85-88
        })

    # ── the line's geometry ─────────────────────────────────────────────────
    # A 2D line plotted from its FIRST TRACE is a dot. Sampling a handful of
    # trace headers along the file turns it into a polyline.
    #
    # Trace length is fixed (240 + samples x bytes-per-sample), so each offset
    # is arithmetic — no scanning, no reading of trace SAMPLES, and the cost is
    # a few seeks regardless of whether the file is 3 MB or 300 MB. That is the
    # whole reason this is affordable on a 232-file crawl.
    if traces and traces > 1 and trace_bytes > 240 and len(trace_raw) == 240:
        _scalar = i16(trace_raw, 70)
        _n = min(PATH_SAMPLES, traces)
        _idx = sorted({int(round(i * (traces - 1) / (_n - 1)))
                       for i in range(_n)}) if _n > 1 else [0]
        try:
            with open(path, "rb") as _f2:
                for _i in _idx:
                    _f2.seek(3600 + _i * trace_bytes)
                    _th = _f2.read(240)
                    if len(_th) != 240:
                        break
                    _x = _apply_scalar(i32(_th, 72), _scalar)
                    _y = _apply_scalar(i32(_th, 76), _scalar)
                    # 0,0 is "not populated", not a location off West Africa.
                    if _x or _y:
                        _path_pts.append((_x, _y))
        except OSError:
            pass
    if _path_pts:
        out["trace_path"] = ";".join(f"{x:.2f} {y:.2f}" for x, y in _path_pts)
        out["path_points"] = len(_path_pts)

    # Survey / line name from the textual header — the C-lines are free text,
    # so take the first that names one rather than guessing a fixed position.
    txt = out.get("textual_header") or ""
    # Stop at two-plus spaces or the next "KEY:" — the C-lines pack several
    # fields onto one line, so a greedy match swallows the neighbours
    # ("XL_1420   AREA: SPRABERRY" instead of "XL_1420").
    _tail = r"(?:\s{2,}|\s+[A-Z][A-Z _]{2,}\s*[:=]|$)"
    for pat, key in (
            (r"(?:SURVEY|AREA|PROJECT)\s*[:=]\s*(.+?)" + _tail, "survey_name"),
            (r"(?:3D LINE|LINE NAME|LINE)\s*[:=]\s*(.+?)" + _tail, "line_name")):
        # MULTILINE matters: the textual header is 40 lines, and without it
        # `$` only matches end-of-STRING, so a field on line 1 can't terminate
        # and the search falls through to a later line ("SURVEY: MIDLAND BASIN
        # 3D" was losing to "AREA: SPRABERRY" two lines down).
        m = re.search(pat, txt, re.I | re.M)
        if m:
            out[key] = m.group(1).strip()[:120]
    return out


# ── how a SEG-Y lands in a store ──────────────────────────────────────────
# No identity: a seismic survey is not a well (or a contract), so the store
# leaves the identity column NULL rather than inventing one.
TABLES = {
    "segy_header": [
        ("survey_name", "TEXT"), ("line_name", "TEXT"),
        # TEXT_LONG, not TEXT: nine "x y" pairs is ~200 chars, and a crooked
        # line sampled more finely would overflow a 255-char column. The
        # LOGICAL type is what belongs here — the backend picks the spelling.
        ("trace_path", "TEXT_LONG"), ("path_points", "INT"),
        ("sample_interval_us", "INT"), ("samples_per_trace", "INT"),
        ("format_code", "INT"), ("format_name", "TEXT"),
        ("measurement_system", "TEXT"), ("segy_revision", "TEXT"),
        ("trace_count", "BIGINT"), ("file_mb", "NUMBER"),
        ("record_length_ms", "NUMBER"), ("trace_sorting", "TEXT"),
        ("first_cdp", "INT"), ("coord_scalar", "INT"),
        ("coord_units", "TEXT"), ("src_x", "NUMBER"), ("src_y", "NUMBER"),
        ("grp_x", "NUMBER"), ("grp_y", "NUMBER"),
        ("textual_header", "TEXT_LONG"),
    ],
}


def to_rows(payload):
    return None, {"segy_header": [payload]}, {}
