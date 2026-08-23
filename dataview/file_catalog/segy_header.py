"""
segy_header.py — dependency-free SEG-Y header reader for cataloging.
====================================================================
Reads exactly what the catalog needs from a SEG-Y file — textual header,
sample interval, samples/trace, data format, trace count, 2D/3D, and the
CDP coordinate bounding box — using only the standard library. No segyio.

Why hand-rolled: SEG-Y (rev 1/2) is a rigid, documented layout — a 3200-byte
EBCDIC textual header, a 400-byte binary header at fixed offsets, then
240-byte trace headers. Cataloging never needs the trace *samples*, so we
read header bytes only and never touch the data. That makes this both
dependency-free and faster than segyio, which indexes the whole file on open
to give trace access we don't use here.

Byte positions follow the SEG-Y rev 1 standard (the same defaults segyio
uses). Real-world files sometimes relocate CDP-X/Y or inline/crossline into
vendor-specific trace-header bytes; when that happens these standard offsets
return zeros/garbage for geometry — the same blind spot segyio has without a
custom header map. Everything is best-effort and never raises: on any problem
a field is left None and a note is recorded.

CLI:  python segy_header.py FILE [FILE ...]
"""
from __future__ import annotations

import os
import re
import struct
from typing import Optional

# ── constants ────────────────────────────────────────────────────────────────
TEXT_HDR = 3200          # textual header bytes (40 lines x 80 cols, EBCDIC)
BIN_HDR = 400            # binary header bytes
TRACE_HDR = 240          # per-trace header bytes
EXT_HDR = 3200           # each extended textual header

# data sample format code -> (description, bytes per sample)
_FORMAT = {
    1: ("4-byte IBM float", 4),
    2: ("4-byte signed int", 4),
    3: ("2-byte signed int", 2),
    4: ("4-byte fixed-point w/ gain (obsolete)", 4),
    5: ("4-byte IEEE float", 4),
    6: ("8-byte IEEE double", 8),
    7: ("3-byte signed int", 3),
    8: ("1-byte signed int", 1),
    9: ("8-byte signed int", 8),
    10: ("4-byte unsigned int", 4),
    11: ("2-byte unsigned int", 2),
    12: ("8-byte unsigned int", 8),
    15: ("3-byte unsigned int", 3),
    16: ("1-byte unsigned int", 1),
}

_MEAS = {1: "meters", 2: "feet"}


def _printable_ratio(s: str) -> float:
    if not s:
        return 0.0
    good = sum(1 for c in s if c == "\n" or 32 <= ord(c) < 127)
    return good / len(s)


def _decode_textual(raw: bytes) -> str:
    """Decode the 3200-byte textual header, trying EBCDIC then ASCII, and
    lay it out as 40 lines of 80 characters (the SEG-Y card-image format)."""
    best = ""
    for enc in ("cp037", "ascii", "latin-1"):
        try:
            txt = raw.decode(enc, errors="replace")
        except Exception:
            continue
        if _printable_ratio(txt) > _printable_ratio(best):
            best = txt
        if _printable_ratio(best) > 0.95:
            break
    # reflow into 40x80 card image
    lines = [best[i:i + 80].rstrip() for i in range(0, min(len(best), 3200), 80)]
    return "\n".join(lines).rstrip()


def _u16(buf, off, big=True):
    return struct.unpack_from(">H" if big else "<H", buf, off)[0]


def _s16(buf, off, big=True):
    return struct.unpack_from(">h" if big else "<h", buf, off)[0]


def _s32(buf, off, big=True):
    return struct.unpack_from(">i" if big else "<i", buf, off)[0]


# ── the trace-header layout the file declares about ITSELF ──────────────────
# SEG-Y's standard positions are a default, not a guarantee, and the good
# vendors say where they actually put things:
#
#     C25 BYTES  81- 84: CDP_X COORD
#     C27 BYTES 181-184: INLINE NUMBER (LINE)
#     C29 BYTES 189-192: CDP_X COORD
#
# Teapot's filt_mig.sgy declares exactly that — inline/crossline at 181-188
# where the standard puts CDP X/Y, and CDP X/Y at 189-196 where the standard
# puts inline/crossline. A conforming reader swaps them and writes the INLINE
# NUMBER into the coordinate column: that is how IL_MIN=-2123710427 reached
# the catalog while looking like a number somebody measured.
#
# READING THIS IS NOT GUESSING. It is the same move crs_from_segy makes for the
# CRS — the processor wrote the answer down, so read it. Nothing here inspects
# coordinate MAGNITUDES to decide which bytes are right; a declaration is
# either present and used, or absent and the standard applies.
#
# Standard positions (0-based offsets into the 240-byte trace header).
STD_OFFSETS = {"cdp_x": 180, "cdp_y": 184, "inline": 188, "crossline": 192}

# The scalar in bytes 71-72 governs bytes 73-88 (source/group coords) and
# 181-188 (CDP X/Y) and NOTHING ELSE. A vendor who relocates CDP X/Y outside
# that range is writing unscaled values, so applying the scalar there would
# divide a correct coordinate by ten.
_SCALAR_GOVERNED = {72, 76, 80, 84, 180, 184}

_BYTES_DECL = re.compile(r"BYTES?\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\s*:?\s*([^\r\n]{0,60})",
                         re.IGNORECASE)


def _classify_byte_label(lbl: str):
    """Which geometry field a declared byte range names, or None."""
    u = lbl.upper()
    # crossline first: 'CROSSLINE' must never fall through to the inline test
    if re.search(r"\bCROSS[\s_-]*LINE\b|\bX[\s_-]*LINE\b", u):
        return "crossline"
    if re.search(r"\bIN[\s_-]*LINE\b", u):
        return "inline"
    if re.search(r"\bCDP[\s_-]*X\b|\bX[\s_-]*COORD|\bEASTING\b", u):
        return "cdp_x"
    if re.search(r"\bCDP[\s_-]*Y\b|\bY[\s_-]*COORD|\bNORTHING\b", u):
        return "cdp_y"
    return None


def declared_trace_map(text: str) -> dict:
    """{'cdp_x': offset, ...} for every geometry field the TEXTUAL header
    states a byte position for. 0-based offsets into the trace header.

    FIRST DECLARATION WINS. filt_mig names CDP_X twice (81-84 and 189-192) and
    both are genuinely populated — 81-84 scaled by bytes 71-72, 189-192 raw.
    Taking the first keeps the choice deterministic and, because 81-84 sits in
    the scalar-governed range, keeps the scalar semantics standard too.

    Only 4-byte fields are accepted: these are all int32 reads, and a declared
    2-byte range means the vendor is describing something else.
    """
    out: dict = {}
    if not text:
        return out
    for a, b, lbl in _BYTES_DECL.findall(str(text)):
        try:
            start, end = int(a), int(b)
        except ValueError:
            continue
        if end - start + 1 != 4 or not (1 <= start <= TRACE_HDR - 3):
            continue
        field = _classify_byte_label(lbl)
        if field and field not in out:
            out[field] = start - 1                 # 1-based bytes -> 0-based
    return out


def _apply_scalar(value: int, scalar: int) -> float:
    """SEG-Y coordinate scalar (trace bytes 71-72): negative => divide,
    positive => multiply, zero => as-is."""
    if scalar > 0:
        return float(value) * scalar
    if scalar < 0:
        return float(value) / abs(scalar)
    return float(value)


def read_segy_header(path: str, *, max_geom_traces: int = 300) -> dict:
    """Catalog a SEG-Y file from its headers alone. Returns a dict of fields
    plus 'notes' (list of caveats) and 'ok' (bool). Never raises."""
    out: dict = {
        "ok": False, "path": path, "notes": [],
        "byte_order": None, "segy_revision": None,
        "sample_interval_us": None, "n_samples": None,
        "trace_length_ms": None,
        "format_code": None, "format_desc": None, "bytes_per_sample": None,
        "measurement_system": None,
        "n_traces": None, "n_ext_text_headers": None,
        "dims": None,
        "trace_map": None, "trace_offsets": None,
        "inline_range": None, "crossline_range": None,
        "cdp_x_range": None, "cdp_y_range": None,
        "cdp_points": [],
        "n_geom_traces_sampled": 0,
        "textual_header": "",
        "_data_start": None, "_bytes_per_trace": None, "_big_endian": None,
    }
    try:
        fsize = os.path.getsize(path)
    except OSError as e:
        out["notes"].append(f"stat failed: {e}")
        return out
    if fsize < TEXT_HDR + BIN_HDR:
        out["notes"].append(f"file too small for SEG-Y headers ({fsize} bytes)")
        return out

    try:
        with open(path, "rb") as f:
            head = f.read(TEXT_HDR + BIN_HDR)          # 3600 bytes
            out["textual_header"] = _decode_textual(head[:TEXT_HDR])
            binh = head[TEXT_HDR:TEXT_HDR + BIN_HDR]    # 400 bytes

            # endianness: trust big-endian (the standard) unless its format
            # code is nonsense and the byte-swapped one is valid
            big = True
            fmt_be = _s16(binh, 24, True)
            if fmt_be not in _FORMAT:
                fmt_le = _s16(binh, 24, False)
                if fmt_le in _FORMAT:
                    big = False
                    out["notes"].append("little-endian detected (non-standard)")
            out["byte_order"] = "big" if big else "little"

            samp_int = _u16(binh, 16, big)              # microseconds
            n_samp = _u16(binh, 20, big)
            fmt = _s16(binh, 24, big)
            meas = _s16(binh, 54, big)
            rev = _s16(binh, 300, big)
            n_ext = _s16(binh, 304, big)

            desc, bps = _FORMAT.get(fmt, (f"unknown code {fmt}", 4))
            if fmt not in _FORMAT:
                out["notes"].append(f"unrecognized format code {fmt}; assuming 4 bytes/sample")

            out["sample_interval_us"] = samp_int or None
            out["n_samples"] = n_samp or None
            out["format_code"] = fmt
            out["format_desc"] = desc
            out["bytes_per_sample"] = bps
            out["measurement_system"] = _MEAS.get(meas, f"code {meas}")
            out["segy_revision"] = rev
            out["n_ext_text_headers"] = n_ext
            if samp_int and n_samp:
                out["trace_length_ms"] = round(n_samp * samp_int / 1000.0, 3)

            # extended textual headers sit between binary header and trace 1
            ext = n_ext if (n_ext and n_ext > 0) else 0
            if n_ext == -1:
                out["notes"].append("variable extended headers (-1); trace count approximate")
            data_start = TEXT_HDR + BIN_HDR + ext * EXT_HDR

            bytes_per_trace = TRACE_HDR + (n_samp * bps if n_samp else 0)
            out["_data_start"] = data_start
            out["_bytes_per_trace"] = bytes_per_trace
            out["_big_endian"] = big
            if bytes_per_trace > TRACE_HDR and fsize > data_start:
                n_traces = (fsize - data_start) // bytes_per_trace
                out["n_traces"] = int(n_traces)
            else:
                n_traces = 0
                out["notes"].append("could not compute trace count "
                                    "(missing samples/trace or short file)")

            # ── geometry sample: read only the 240-byte trace headers ──
            # WHERE to read is decided before the loop: the file's own declared
            # layout if it states one, the rev-1 standard otherwise.
            declared = declared_trace_map(out["textual_header"])
            out["trace_map"] = declared or None
            offs = dict(STD_OFFSETS)
            offs.update(declared)
            out["trace_offsets"] = offs
            _moved = {k: v for k, v in declared.items() if STD_OFFSETS.get(k) != v}
            if _moved:
                out["notes"].append(
                    "textual header declares non-standard trace positions, "
                    "using them: "
                    + ", ".join(f"{k} @ bytes {v + 1}-{v + 4}"
                                for k, v in sorted(_moved.items())))

            if n_traces > 0:
                step = max(1, n_traces // max(1, max_geom_traces))
                xs, ys, ils, xls = [], [], [], []
                sampled = 0
                idx = 0
                _sx = offs["cdp_x"] in _SCALAR_GOVERNED
                _sy = offs["cdp_y"] in _SCALAR_GOVERNED
                while idx < n_traces and sampled < max_geom_traces:
                    toff = data_start + idx * bytes_per_trace
                    f.seek(toff)
                    th = f.read(TRACE_HDR)
                    if len(th) < TRACE_HDR:
                        break
                    scalar = _s16(th, 70, big)           # bytes 71-72
                    cx = _s32(th, offs["cdp_x"], big)
                    cy = _s32(th, offs["cdp_y"], big)
                    il = _s32(th, offs["inline"], big)
                    xl = _s32(th, offs["crossline"], big)
                    xs.append(_apply_scalar(cx, scalar) if _sx else float(cx))
                    ys.append(_apply_scalar(cy, scalar) if _sy else float(cy))
                    ils.append(il)
                    xls.append(xl)
                    sampled += 1
                    idx += step
                out["n_geom_traces_sampled"] = sampled

                def _rng(v):
                    nz = [x for x in v if x != 0]
                    src = nz or v
                    return (min(src), max(src)) if src else None

                # AN INLINE NUMBER IS AN INDEX, NOT A MEASUREMENT. It is a
                # positive integer counting bins, so a negative or billion-scale
                # value is not a survey that is unusually large — it is bytes
                # that are not an inline number, whether from a relocated field
                # or (as in filt_mig) trace headers the file itself has
                # corrupted. Excluding them is a TYPE constraint, not a
                # judgement about magnitude, and letting them through is how
                # IL_MIN=-2123710427 got into the catalog looking like data.
                _IDX_MAX = 10_000_000
                _bad_frac = 0.0

                def _idx_rng(v, what):
                    nonlocal _bad_frac
                    good = [x for x in v if 0 < x <= _IDX_MAX]
                    bad = len(v) - len(good) - sum(1 for x in v if x == 0)
                    if v and bad > 0:
                        _bad_frac = max(_bad_frac, bad / len(v))
                        out["notes"].append(
                            f"{bad} of {len(v)} sampled {what} values were not "
                            f"valid indices and were excluded")
                    return (min(good), max(good)) if good else None

                out["inline_range"] = _idx_rng(ils, "inline")
                out["crossline_range"] = _idx_rng(xls, "crossline")
                cxr = _rng(xs)
                cyr = _rng(ys)

                # A COORDINATE HAS NO TYPE CONSTRAINT TO FAIL, so it cannot be
                # filtered the way an index can — any number is a plausible
                # easting somewhere. But the indices sampled from the SAME
                # trace headers can, and when a large share of them are not
                # indices at all, the headers are unreadable and the
                # coordinates read beside them are not measurements either.
                #
                # filt_mig is the case: 143 of 300 sampled traces fail, because
                # the file's trace headers go out of alignment after trace 2.
                # The surviving min/max spanned ±6e13 — a "bounding box" no
                # reader should hand on. Publishing nothing here is right and
                # costs nothing: the navigation file is the geometry source
                # anyway, and it wins over trace headers by design.
                if _bad_frac > 0.25:
                    out["notes"].append(
                        f"{_bad_frac:.0%} of sampled trace headers are "
                        f"unreadable — no geometry is reported from them "
                        f"(use the survey's navigation file)")
                    cxr = cyr = None
                    xs = ys = []
                    # The surviving indices PASSED the type check, but passing
                    # it only means a corrupt word happened to land in range —
                    # filt_mig's kept a max inline of 4,225,522 for a survey its
                    # own header says has 345. Same headers, same verdict.
                    out["inline_range"] = None
                    out["crossline_range"] = None

                out["cdp_x_range"] = cxr
                out["cdp_y_range"] = cyr
                out["cdp_points"] = list(zip(xs, ys))   # scalar already applied

                # Count distinct VALID indices only. Garbage inflates the
                # distinct count, and a 2D line whose trace headers are partly
                # unreadable would otherwise be called 3D on the strength of
                # the unreadable part.
                n_il = len({x for x in ils if 0 < x <= _IDX_MAX})
                n_xl = len({x for x in xls if 0 < x <= _IDX_MAX})
                if n_il > 1 and n_xl > 1:
                    out["dims"] = "3D"
                elif (cxr and cxr[0] != cxr[1]) or (cyr and cyr[0] != cyr[1]):
                    out["dims"] = "2D"
                else:
                    out["dims"] = "2D?"
                    out["notes"].append(
                        "geometry flat in the trace positions used "
                        + ("(declared by the textual header)" if declared
                           else "(rev-1 standard; header declares none)"))

            out["ok"] = out["sample_interval_us"] is not None
    except Exception as e:
        out["notes"].append(f"{type(e).__name__}: {e}")
    return out


def to_catalog_fields(h: dict) -> dict:
    """Map the raw header dict onto the same field names the light extractor
    uses for SEG-Y, so this can drop into _extract_fields in place of segyio."""
    cx = h.get("cdp_x_range") or (None, None)
    cy = h.get("cdp_y_range") or (None, None)
    return {
        "n_traces": h.get("n_traces"),
        "sample_interval": h.get("sample_interval_us"),
        "n_samples": h.get("n_samples"),
        "seismic_dims": h.get("dims"),               # "2D" / "3D"
        "sample_format": h.get("format_desc"),
        "measurement_system": h.get("measurement_system"),
        "cdp_x_min": cx[0], "cdp_x_max": cx[1],
        "cdp_y_min": cy[0], "cdp_y_max": cy[1],
        "inline_range": h.get("inline_range"),
        "crossline_range": h.get("crossline_range"),
        "textual_header": h.get("textual_header"),
        "segy_notes": "; ".join(h.get("notes") or []),
    }


def sample_trace_rows(path: str, limit: int = 100) -> list:
    """First `limit` trace headers as preview rows:
        [{Trace, CDP, CDP_X, CDP_Y, Offset}, ...]
    Coordinate scalar (trace bytes 71-72) is applied to CDP-X/Y. Dependency-free
    replacement for the segyio header-preview loop. Never raises."""
    rows: list = []
    h = read_segy_header(path, max_geom_traces=1)
    if not h.get("ok"):
        return rows
    big = bool(h.get("_big_endian"))
    ds = h.get("_data_start")
    bpt = h.get("_bytes_per_trace")
    nt = h.get("n_traces") or 0
    if not ds or not bpt or nt <= 0:
        return rows
    # THE SAME positions read_segy_header used, declared map included. Reading
    # 181-188 unconditionally here would print filt_mig's INLINE and CROSSLINE
    # numbers in columns headed CDP_X / CDP_Y — a preview table that quietly
    # contradicts the catalog built from the same file.
    offs = h.get("trace_offsets") or dict(STD_OFFSETS)
    _sx = offs.get("cdp_x", 180) in _SCALAR_GOVERNED
    _sy = offs.get("cdp_y", 184) in _SCALAR_GOVERNED
    n = min(int(nt), int(limit))
    try:
        with open(path, "rb") as f:
            for i in range(n):
                f.seek(ds + i * bpt)
                th = f.read(TRACE_HDR)
                if len(th) < TRACE_HDR:
                    break
                scalar = _s16(th, 70, big)              # bytes 71-72
                _x = _s32(th, offs.get("cdp_x", 180), big)
                _y = _s32(th, offs.get("cdp_y", 184), big)
                rows.append({
                    "Trace": i + 1,
                    "CDP":   _s32(th, 20, big),         # bytes 21-24
                    "Inline":    _s32(th, offs.get("inline", 188), big),
                    "Crossline": _s32(th, offs.get("crossline", 192), big),
                    "CDP_X": _apply_scalar(_x, scalar) if _sx else float(_x),
                    "CDP_Y": _apply_scalar(_y, scalar) if _sy else float(_y),
                    "Offset": _s32(th, 36, big),        # bytes 37-40
                })
    except Exception:
        pass
    return rows


def _ibm_to_ieee(u32):
    """IBM System/360 32-bit float -> IEEE, vectorised over a numpy uint32 array.

    Format code 1 is still the commonest thing on tape-era SEG-Y (Teapot's
    volume is one), and it is NOT IEEE: sign bit, 7-bit excess-64 exponent in
    base SIXTEEN, 24-bit fraction. Reading the bytes as float32 gives numbers
    that are wrong rather than obviously broken, which is the failure mode this
    codebase cares most about.
    """
    import numpy as np
    sign = np.right_shift(u32, 31) & 0x01
    expo = (np.right_shift(u32, 24) & 0x7F).astype(np.int32) - 64
    mant = (u32 & 0x00FFFFFF).astype(np.float64) / float(1 << 24)
    return ((1.0 - 2.0 * sign) * mant * np.power(16.0, expo)).astype(np.float32)


# numpy dtype per SEG-Y sample format code. Codes absent here are read as
# raw int8 blocks and left alone — better a flat trace than an invented one.
_NP_FMT = {1: (">u4", "ibm"), 2: (">i4", None), 3: (">i2", None),
           5: (">f4", None), 8: (">i1", None), 10: (">u4", None),
           11: (">u2", None), 16: (">u1", None)}


def read_trace_samples(path: str, start: int = 0, count: int = 100,
                       skip_blank: bool = True, max_walk: int = 40000):
    """(data, times_ms) — `data` is samples x traces, ready to imshow.

    WHY THIS EXISTS RATHER THAN segyio. segyio refuses a file whose body is not
    an exact multiple of the trace length:

        RuntimeError: trace count inconsistent with file size,
                      trace lengths possibly of non-uniform

    Teapot's filt_mig.sgy is exactly that — 64,979 whole traces plus 5,756
    leftover bytes — so the volume could be catalogued and mapped but not
    LOOKED AT. The refusal is defensible for a library that indexes the whole
    file; it is the wrong answer for a viewer, where reading the 64,979 traces
    that ARE well-formed is obviously more useful than reading none.

    A short final trace is DROPPED, never zero-padded: a padded trace draws as
    a real amplitude that happens to be silent, and this file already taught us
    what invented data looks like on a plot.
    """
    import numpy as np
    h = read_segy_header(path, max_geom_traces=1)
    if not h.get("ok"):
        return None, None
    ds, bpt = h.get("_data_start"), h.get("_bytes_per_trace")
    ns, fmt = h.get("n_samples"), h.get("format_code")
    nt = int(h.get("n_traces") or 0)
    if not ds or not bpt or not ns or nt <= 0:
        return None, None
    dtype, conv = _NP_FMT.get(int(fmt or 0), (">i1", None))
    payload = bpt - TRACE_HDR

    start = max(0, int(start))
    count = max(1, min(int(count), nt - start))
    if count <= 0:
        return None, None

    def _decode(raw):
        """Samples, or None if this offset is not a trace boundary.

        MISALIGNMENT IS DETECTABLE, and that is what makes resync possible.
        An IBM float's exponent is base SIXTEEN, so a word read one or two
        bytes off-boundary lands an arbitrary byte in the exponent field and
        decodes to something like 16^60 — inf in float32. Real seismic
        amplitudes never do that. So "did every sample decode finite" is a
        reliable test for "is this actually where a trace starts".
        """
        if len(raw) < ns * np.dtype(dtype).itemsize:
            return None
        a = np.frombuffer(raw, dtype=dtype, count=ns)
        if conv != "ibm":
            v = a.astype(np.float32)
            return v if np.all(np.isfinite(v)) else None

        # TEST THE FORMAT, NOT THE ANSWER. "Did it decode finite" is too weak:
        # a two-byte-misaligned word decoded to 3.0e38 here — absurd, and
        # finite, so it sailed through and 120 traces of noise were returned as
        # if they were data. The exponent is the tell. In real IBM-float
        # seismic the raw exponent byte clusters tightly around 64 (16^0);
        # misalignment puts an arbitrary data byte in that field and scatters
        # it across the whole 0-127 range. Requiring nearly all non-zero
        # samples inside a generous band tests whether these bytes ARE IBM
        # floats at this offset, which is exactly the question resync asks.
        expo = np.right_shift(a, 24) & 0x7F
        nz = a != 0
        if nz.any():
            e = expo[nz]
            if float(np.mean((e >= 40) & (e <= 80))) < 0.95:
                return None
        with np.errstate(over="ignore", invalid="ignore"):
            v = _ibm_to_ieee(a)
        return v if np.all(np.isfinite(v)) else None

    # Teapot's volume drifts: 74 trace boundaries in the first 40 MB sit two
    # bytes later than the regular stride puts them (682 bytes of accumulated
    # drift over 3,000 traces). Indexing by arithmetic alone therefore reads
    # progressively further off-boundary until every sample is garbage, which
    # is what the first cut of this function produced. Walking WITH RESYNC
    # keeps alignment; a trace that cannot be resynced is dropped and counted,
    # never rendered.
    # ALWAYS WALK FROM THE BEGINNING. Seeking straight to ds + start*bpt is
    # what the arithmetic says and it is wrong on a drifting file: by trace
    # 60,000 the true boundary is some 13 kB past where the multiplication
    # puts it, far outside any sane resync window, and every candidate offset
    # decodes as garbage. The walk is the only thing that knows where trace i
    # actually begins, so `start` is honoured by walking past those traces,
    # not by jumping over them.
    #
    # skip_blank exists because this volume opens on dead traces: filt_mig's
    # first traces are literally zero bytes (survey corner, muted), so a
    # viewer that plots "the first 100 traces" draws an empty rectangle and
    # looks broken. Blank traces are stepped over, keeping alignment, until
    # `count` traces with signal are found or max_walk is exhausted.
    cols, skipped, resyncs, blanks, walked = [], 0, 0, 0, 0
    off = ds
    with open(path, "rb") as f:
        while len(cols) < count and walked < max_walk and off < os.path.getsize(path):
            walked += 1
            f.seek(off + TRACE_HDR)
            v = _decode(f.read(payload))
            if v is None:
                found = False
                for d in range(1, 65):          # a couple of bytes, not a hunt
                    f.seek(off + d + TRACE_HDR)
                    v = _decode(f.read(payload))
                    if v is not None:
                        off += d
                        resyncs += 1
                        found = True
                        break
                if not found:
                    skipped += 1
                    off += bpt
                    continue
            off += bpt
            if walked <= start:
                continue
            if skip_blank and not v.any():
                blanks += 1
                continue
            cols.append(v)
    if not cols:
        return None, None
    read_trace_samples.last_stats = {      # for the viewer's honesty banner
        "traces_walked": walked, "resyncs": resyncs,
        "unreadable": skipped, "blank_skipped": blanks, "plotted": len(cols)}
    data = np.column_stack(cols)
    si_ms = (h.get("sample_interval_us") or 0) / 1000.0
    times = np.arange(ns) * (si_ms or 1.0)
    data.flags.writeable = False
    return data, times


def _fmt_pair(p):
    return f"{p[0]:,} … {p[1]:,}" if p else "—"


def main(argv=None):
    import sys
    paths = argv if argv is not None else sys.argv[1:]
    if not paths:
        print("usage: python segy_header.py FILE [FILE ...]")
        return
    for p in paths:
        h = read_segy_header(p)
        print("=" * 72)
        print(os.path.basename(p))
        print("-" * 72)
        print(f"  ok                : {h['ok']}   ({h['byte_order']}-endian, "
              f"rev {h['segy_revision']})")
        print(f"  sample interval   : {h['sample_interval_us']} µs")
        print(f"  samples / trace   : {h['n_samples']}  "
              f"(trace length {h['trace_length_ms']} ms)")
        print(f"  data format       : {h['format_code']} — {h['format_desc']}")
        print(f"  measurement system: {h['measurement_system']}")
        print(f"  trace count       : {h['n_traces']:,}" if h['n_traces']
              else "  trace count       : —")
        print(f"  dimensionality    : {h['dims']}  "
              f"(sampled {h['n_geom_traces_sampled']} trace headers)")
        print(f"  inline range      : {_fmt_pair(h['inline_range'])}")
        print(f"  crossline range   : {_fmt_pair(h['crossline_range'])}")
        print(f"  CDP-X range       : {_fmt_pair(h['cdp_x_range'])}")
        print(f"  CDP-Y range       : {_fmt_pair(h['cdp_y_range'])}")
        if h["notes"]:
            print(f"  notes             : {'; '.join(h['notes'])}")
        print("  textual header (first 6 lines):")
        for line in (h["textual_header"] or "").splitlines()[:6]:
            print(f"    | {line}")


if __name__ == "__main__":
    main()
