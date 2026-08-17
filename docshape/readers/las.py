"""
docshape.readers.las
====================
LAS well logs — plain text, no library needed.

A LAS file is mnemonic blocks: ~V version, ~W well header, ~C curve
definitions, ~A the data. The header and curve sections are what belong in a
review store; the DATA section is COUNTED, not stored, because a single file
can hold hundreds of thousands of samples and nobody reviews amplitudes in a
table.

The mnemonic map below is petroleum-specific and lives here rather than in the
pack because it describes a FILE FORMAT, not a vocabulary — a LAS is a LAS
regardless of which domain pack is loaded.
"""
from __future__ import annotations

import re

LAS_HEADER_COLS = [
    ("uwi", "VARCHAR"), ("well_name", "VARCHAR"), ("field_name", "VARCHAR"),
    ("operator", "VARCHAR"), ("service_company", "VARCHAR"),
    ("log_date", "VARCHAR"), ("log_type", "VARCHAR"), ("log_id", "VARCHAR"),
    ("county", "VARCHAR"), ("state", "VARCHAR"), ("country", "VARCHAR"),
    ("latitude", "DOUBLE"), ("longitude", "DOUBLE"),
    ("start_depth", "DOUBLE"), ("stop_depth", "DOUBLE"),
    ("step", "DOUBLE"), ("null_value", "DOUBLE"), ("depth_uom", "VARCHAR"),
    ("version", "VARCHAR"), ("wrap", "VARCHAR"), ("curve_count", "INTEGER"),
    ("sample_count", "INTEGER"),
]
LAS_CURVE_COLS = [
    ("uwi", "VARCHAR"), ("curve_index", "INTEGER"), ("mnemonic", "VARCHAR"),
    ("unit", "VARCHAR"), ("description", "VARCHAR"), ("is_index", "VARCHAR"),
]

# LAS mnemonic -> our column. The standard is loose about which are present,
# so anything unrecognised is kept in _extra_json rather than dropped.
_LAS_MAP = {
    "UWI": "uwi", "API": "uwi", "WELL": "well_name", "FLD": "field_name",
    "COMP": "operator", "SRVC": "service_company", "DATE": "log_date",
    "TYPE": "log_type", "LOG_ID": "log_id", "CNTY": "county", "STAT": "state",
    "CTRY": "country", "LAT": "latitude", "LONG": "longitude",
    "STRT": "start_depth", "STOP": "stop_depth", "STEP": "step",
    "NULL": "null_value", "VERS": "version", "WRAP": "wrap",
}


def parse_las(path):
    """(header_dict, [curve_dicts], extra_dict) — header and curves, no samples."""
    hdr, curves, extra = {}, [], {}
    section, samples, ncol = None, 0, 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            t = line.strip()
            if not t:
                continue
            if t.startswith("~"):
                u = t[1:2].upper()
                section = ("V" if u == "V" else "W" if u == "W" else
                           "C" if u == "C" else "A" if u == "A" else "O")
                continue
            if section == "A":
                samples += 1
                continue
            if section in ("V", "W"):
                # LAS is MNEM.UNIT<spaces>VALUE : DESCRIPTION — the unit sits
                # IMMEDIATELY after the dot with no space, so allowing \s* there
                # made an unlabelled value the "unit" and left the value empty
                # ("UWI .   17-031-10176-0000" gave unit=17-031-... value="").
                m = re.match(r"\s*([^.\s]+)\s*\.(\S*)\s+(.*?)\s*:(.*)$", t)
                if not m:
                    continue
                mnem, unit, value, _desc = m.groups()
                mnem = mnem.rstrip(".").upper()
                col = _LAS_MAP.get(mnem)
                val = value.strip()
                if col:
                    hdr[col] = val
                    if mnem in ("STRT", "STOP", "STEP") and unit:
                        hdr.setdefault("depth_uom", unit.upper())
                elif val:
                    extra[mnem] = val
            elif section == "C":
                m = re.match(r"\s*([\w.\-]+?)\s*\.\s*(\S*)\s*:?\s*(.*)$", t)
                if not m:
                    continue
                mnem, unit, desc = m.groups()
                mnem = mnem.rstrip(".").upper()
                if not mnem:
                    continue
                curves.append({
                    "curve_index": len(curves) + 1, "mnemonic": mnem,
                    "unit": (unit or "").upper() or None,
                    "description": (desc or "").strip() or None,
                    "is_index": "Y" if len(curves) == 0 else "N",
                })
    for k in ("latitude", "longitude", "start_depth", "stop_depth", "step",
              "null_value"):
        if k in hdr:
            try:
                hdr[k] = float(str(hdr[k]).replace(",", ""))
            except ValueError:
                hdr[k] = None
    hdr["curve_count"] = len(curves)
    hdr["sample_count"] = samples
    return hdr, curves, extra


# --------------------------------------------------------------------------- #
# SEG-Y — metadata only, no traces
# --------------------------------------------------------------------------- #
# 232 of the 347 files in Perry's real folder are SEG-Y, and none were being
# read. The format needs no library: a 3200-byte EBCDIC textual header, a
# 400-byte binary header at fixed offsets, and 240-byte trace headers. All
# big-endian, all documented, all struct.unpack.
#
# Trace SAMPLES are never read — a survey is gigabytes and nobody reviews
# amplitudes in a table. What matters here is the georeferencing metadata: the
# survey name from the textual header, sample interval, format, measurement
# system, and the first trace's source coordinates with the scalar applied.
# That is the same information FILE_SEIS_HEADER and DV_SEGY_EPSG already care
# about, and getting the coordinate scalar wrong is what put Australian
# surveys off Norway once before.


# ── how a LAS lands in a store ────────────────────────────────────────────
# The reader declares its own tables so the store never needs to know what a
# curve is. A pack-driven shape table is built from the pack; these are built
# from here.
TABLES = {
    "las_header": [
        ("well_name", "TEXT"), ("field_name", "TEXT"), ("operator", "TEXT"),
        ("service_company", "TEXT"), ("log_date", "TEXT"),
        ("log_type", "TEXT"), ("log_id", "TEXT"), ("county", "TEXT"),
        ("state", "TEXT"), ("country", "TEXT"),
        ("latitude", "NUMBER"), ("longitude", "NUMBER"),
        ("start_depth", "NUMBER"), ("stop_depth", "NUMBER"),
        ("step", "NUMBER"), ("null_value", "NUMBER"),
        ("depth_uom", "TEXT"), ("version", "TEXT"), ("wrap", "TEXT"),
        ("curve_count", "INT"), ("sample_count", "INT"),
    ],
    "las_curve": [
        ("curve_index", "INT"), ("mnemonic", "TEXT"), ("unit", "TEXT"),
        ("description", "TEXT"), ("is_index", "TEXT"),
    ],
}


def to_rows(payload):
    """(header, curves, extra) -> {table: [row dicts]} plus the identity value."""
    hdr, curves, extra = payload
    ident = hdr.get("uwi")
    h = {k: v for k, v in hdr.items() if k != "uwi"}
    return ident, {"las_header": [h], "las_curve": list(curves)}, extra
