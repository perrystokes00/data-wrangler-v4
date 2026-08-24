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


def _u32(buf, off, big=True):
    return struct.unpack_from(">I" if big else "<I", buf, off)[0]


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
    # TRAILING (?![A-Z0-9]), NOT \b. brecon_3d declares "XLINE_NO" / "ILINE_NO",
    # and \b after LINE fails there because '_' IS a word character -- so both
    # fields went unclassified, the reader fell back to the rev-1 offsets, and
    # 300 of 300 sampled crossline values came back invalid. The lookahead also
    # keeps the prose line "ILINES: 1-457  XLINES: 1 - 318" from matching, which
    # a bare relaxation to \w* would not.
    if re.search(r"\bCROSS[\s_-]*LINE(?![A-Z0-9])|\bX[\s_-]*LINE(?![A-Z0-9])", u):
        return "crossline"
    # "ILINE_NO" as well as "INLINE". The crossline test above runs first, so
    # XLINE never reaches here.
    if re.search(r"\bIN[\s_-]*LINE(?![A-Z0-9])|\bI[\s_-]*LINE(?![A-Z0-9])", u):
        return "inline"
    if re.search(r"\bCDP[\s_-]*X\b|\bX[\s_-]*COORD|\bEASTING\b", u):
        return "cdp_x"
    if re.search(r"\bCDP[\s_-]*Y\b|\bY[\s_-]*COORD|\bNORTHING\b", u):
        return "cdp_y"
    return None


# THE OTHER WAY VENDORS WRITE IT: label, START offset, then size+format --
#     C 6 BYTES FORMAT   (FOR NON STANDARD SEGY HEADERS)
#     C 7 CDP_X          181   4R   CDP_Y          185   4R
#     C 8 ILINE_NO       197   4I   XLINE_NO       201   4I
# No "BYTES" keyword, no range, and the FORMAT is the point: 4R is a 4-byte
# REAL. Read as int32, brecon_3d's easting 2,617,988 arrives as 1177023108 and,
# with the scalar applied, 11,770,231 -- self-consistent, wrong, and it plots.
#
# Two or more spaces before the offset is what separates a declaration from
# prose. The Geoscience Australia form ("CDP-X:73-76/181-184:INT") is
# colon-delimited and does NOT match this, and "SAMPLE RATE  4 MS" does not
# either; both are asserted in selftest rather than assumed.
_DECL_FMT = re.compile(
    r"([A-Z][A-Z0-9_ .\-]{1,22}?)\s{2,}(\d{1,3})\s+([1248])\s*([RIF])\b",
    re.IGNORECASE)


def declared_trace_layout(text):
    """(offsets, formats) read from the textual header.

    offsets -- {'cdp_x': 0-based offset, ...}, as declared_trace_map.
    formats -- {'cdp_x': 'real'|'int', ...}, ONLY where a format is declared.
               Absent means "not stated", which the caller reads as int32 (the
               SEG-Y default). It does not mean int32 was declared.
    """
    offs = declared_trace_map(text)
    fmts = {}
    if not text:
        return offs, fmts
    for lbl, off, size, code in _DECL_FMT.findall(str(text)):
        if size != "4":                 # every geometry field read here is 4 bytes
            continue
        try:
            start = int(off)
        except ValueError:
            continue
        if not (1 <= start <= TRACE_HDR - 3):
            continue
        field = _classify_byte_label(lbl)
        if not field:
            continue
        offs.setdefault(field, start - 1)          # first declaration wins
        fmts.setdefault(field, "real" if code.upper() in ("R", "F") else "int")
    return offs, fmts


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


def _ibm32(u32: int) -> float:
    """One IBM System/360 float. _ibm_to_ieee is numpy-vectorised for SAMPLES;
    a trace-header coordinate is a single word and must not drag numpy in per
    trace. Same arithmetic: sign, 7-bit excess-64 exponent in base SIXTEEN,
    24-bit fraction."""
    if not u32:
        return 0.0
    sign = -1.0 if u32 >> 31 else 1.0
    expo = ((u32 >> 24) & 0x7F) - 64
    mant = (u32 & 0x00FFFFFF) / float(1 << 24)
    return sign * mant * (16.0 ** expo)


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
            declared, declared_fmt = declared_trace_layout(out["textual_header"])
            out["trace_map"] = declared or None
            out["trace_formats"] = declared_fmt or None
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
                # A DECLARED REAL IS NOT SCALED. The bytes 71-72 scalar governs
                # the INTEGER coordinate fields; a vendor writing floats has
                # already put ground units in the word, and dividing brecon's
                # 2,617,988 by its stated -100 would give 26,180.
                _rx = declared_fmt.get("cdp_x") == "real"
                _ry = declared_fmt.get("cdp_y") == "real"
                # WHICH real: the file's own sample-format code says which
                # convention this vendor writes. brecon declares code 1, so 4R
                # is an IBM real -- read, not guessed. Code 5 is IEEE.
                _ieee = (fmt == 5)
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
                    if _rx:
                        _ux = _u32(th, offs["cdp_x"], big)
                        xs.append(struct.unpack(">f" if big else "<f",
                                                th[offs["cdp_x"]:offs["cdp_x"] + 4])[0]
                                  if _ieee else _ibm32(_ux))
                    else:
                        xs.append(_apply_scalar(cx, scalar) if _sx else float(cx))
                    if _ry:
                        _uy = _u32(th, offs["cdp_y"], big)
                        ys.append(struct.unpack(">f" if big else "<f",
                                                th[offs["cdp_y"]:offs["cdp_y"] + 4])[0]
                                  if _ieee else _ibm32(_uy))
                    else:
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
                # JUDGE THE COORDINATES ON THE COORDINATES. This veto used
                # to key on _bad_frac — the share of sampled INLINE / CROSSLINE
                # words that are not valid indices — and threw the coordinates
                # away with them. That premise holds only where those fields
                # ARE indices. A 2D line has no crossline, so bytes 189-196
                # hold whatever the vendor put there (in the Geoscience
                # Australia headers, CDP-STAT statics, routinely negative);
                # "300 of 300 crossline values invalid" is the EXPECTED
                # reading, and it condemned good CDP-X/Y sitting at 181-188.
                #
                # Measured 24 Aug: 228 of 232 seismic files reported no
                # geometry despite clean coordinates. Downstream that reads as
                # "no CRS" — extract_core only writes epsg_code inside
                # `if xs and ys` — so 229 files held as not-georeferenced and
                # the remedy on offer was to arm a fallback CRS that was never
                # missing. The CRS had been read correctly all along.
                #
                # The corruption signal is real; it was measured on the wrong
                # field. A trace header is USABLE when its coordinate pair is
                # non-zero and within a magnitude any CRS can mean. Measured:
                #
                #   GA 2D lines, brecon 3D    300/300 usable  -> keep
                #   filt_mig                   58/301 usable  -> discard
                #
                # filt_mig is why the veto exists — its headers lose alignment
                # after trace 2 and the surviving span was 1,088 x 636 km for a
                # survey ~10 km across — and it still fails, four-fold.
                #
                # ±2e7 is a TYPE bound, not a judgement about survey size:
                # Earth's circumference is ~4.0e7 m and a UTM northing tops out
                # at 1e7, so no projected coordinate in metres exceeds it and
                # every geographic one sits far inside.
                _COORD_MAX = 2e7
                _usable = sum(1 for _x, _y in zip(xs, ys)
                              if _x != 0 and _y != 0
                              and abs(_x) <= _COORD_MAX
                              and abs(_y) <= _COORD_MAX)
                _ok_frac = (_usable / sampled) if sampled else 0.0
                if _ok_frac < 0.75:
                    out["notes"].append(
                        f"only {_ok_frac:.0%} of {sampled} sampled trace "
                        f"headers carry a usable coordinate pair — no geometry "
                        f"is read from them. The header may declare a byte "
                        f"layout or coordinate FORMAT this reader does not "
                        f"honour; a navigation file, where one exists, is the "
                        f"other source")
                    cxr = cyr = None
                    xs = ys = []
                    # The surviving indices PASSED the type check, but passing
                    # it only means a corrupt word happened to land in range —
                    # filt_mig's kept a max inline of 4,225,522 for a survey its
                    # own header says has 345. Same headers, same verdict.
                    out["inline_range"] = None
                    out["crossline_range"] = None

                # A DEGENERATE EXTENT IS NOT A SURVEY. brecon_3d declares its
                # own layout as
                #     C 7 CDP_X          181   4R   CDP_Y          185   4R
                # — "4R" is a 4-byte REAL. This reader takes int32 there, so
                # 1177023108 / scalar 100 becomes an easting of 11,770,231 and
                # 300 traces span 79 m by 114 m. Those numbers are the wrong
                # TYPE read from the right bytes, and they are self-consistent,
                # so no per-value magnitude bound rejects them: the old index
                # veto discarded them only by luck, and dropping that veto let
                # a 79 m "3D survey" through.
                #
                # An outline that plots is worse than no outline, so extent is
                # checked on its own terms: hundreds of sampled traces confined
                # to a couple of hundred metres in BOTH axes is not a survey,
                # whatever the numbers mean. 250 m is comfortably below any real
                # survey here (the smallest genuine one measures ~10 km) and
                # comfortably above brecon.
                #
                # THE UNDERLYING FIX IS TO HONOUR THE DECLARED FORMAT (4R vs
                # 4I) rather than assume int32 — declared_trace_map reads byte
                # POSITIONS today but not the format beside them. Until it
                # does, brecon reports no trace geometry, which is what it did
                # before and is honest.
                if cxr and cyr:
                    _dx = abs(cxr[1] - cxr[0])
                    _dy = abs(cyr[1] - cyr[0])
                    if _dx < 250 and _dy < 250 and sampled >= 20:
                        out["notes"].append(
                            f"trace-header coordinates span only {_dx:.0f} x "
                            f"{_dy:.0f} units over {sampled} traces — too "
                            f"degenerate to be a survey extent, so no geometry "
                            f"is reported (the header may declare a coordinate "
                            f"FORMAT this reader does not honour yet)")
                        cxr = cyr = None
                        xs = ys = []

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


# ---------------------------------------------------------------------------
# 3D slice access: one inline or one crossline out of a volume
# ---------------------------------------------------------------------------

# A scanned volume's trace index, keyed by (path, size, mtime) so an edited or
# replaced file is never served from a stale scan. Small on purpose: the arrays
# are one int32 per trace per axis (1.7 MB for Delft's 211,519 traces), and
# holding every volume anyone browsed would be the leak.
_INDEX_CACHE = {}
_INDEX_CACHE_MAX = 4


def trace_index(path: str):
    """{ils, xls, n_traces, ...} for a 3D volume, or None.

    WHY A WHOLE-FILE SCAN IS AFFORDABLE. Every trace header sits at a fixed
    stride, so the inline and crossline columns can be read with ONE memory map
    and a strided view -- no per-trace seek. Measured: 0.09s for f3.sgy
    (56,726 traces, 51 MB) and 1.51s for delft.sgy (211,519 traces, 816 MB).
    Reading the same headers one seek at a time is minutes.

    THE STRIDE MUST BE EXACT, and that is checked rather than assumed. Teapot's
    filt_mig.sgy drifts -- 74 boundaries in the first 40 MB sit two bytes later
    than arithmetic predicts -- and a strided view over a drifting file reads
    progressively further off-boundary, returning inline numbers that are pure
    noise. Noise here is worse than nothing: it would populate a slice picker
    with plausible numbers that select the wrong traces. So a body that is not
    a whole multiple of the trace length returns None, and the caller says the
    volume has no usable index. (filt_mig has no inline/crossline anyway -- its
    IL/XL bytes hold coordinates -- so nothing is lost.)
    """
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, int(st.st_mtime))
    except OSError:
        return None
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]

    import numpy as np
    h = read_segy_header(path, max_geom_traces=1)
    if not h.get("ok"):
        return None
    ds, bpt = h.get("_data_start"), h.get("_bytes_per_trace")
    nt = int(h.get("n_traces") or 0)
    if not ds or not bpt or nt <= 0:
        return None

    body_bytes = st.st_size - ds
    if body_bytes < nt * bpt or (body_bytes % bpt) != 0:
        # Ragged or drifting: see the docstring. Refuse rather than guess.
        return None

    tmap = h.get("trace_map") or {}
    il_off = int(tmap.get("inline", STD_OFFSETS["inline"]))
    xl_off = int(tmap.get("crossline", STD_OFFSETS["crossline"]))
    big = (h.get("byte_order") != "little")
    dt = ">i4" if big else "<i4"

    try:
        mm = np.memmap(path, dtype=np.uint8, mode="r")
        rows = mm[ds: ds + nt * bpt].reshape(nt, bpt)
        ils = rows[:, il_off:il_off + 4].copy().view(dt).ravel()
        xls = rows[:, xl_off:xl_off + 4].copy().view(dt).ravel()
        del rows, mm
    except Exception:
        return None

    # AN INLINE NUMBER IS AN INDEX, NOT A MEASUREMENT -- the same test the
    # header extractor applies. A relocated or absent field decodes to values
    # like -2,123,710,427, and a picker offering those is worse than a picker
    # offering nothing.
    def _sane(a):
        good = (a > 0) & (a < 10_000_000)
        return good.mean() >= 0.95 if a.size else False

    out = {"n_traces": nt, "il_offset": il_off, "xl_offset": xl_off,
           "ils": ils if _sane(ils) else None,
           "xls": xls if _sane(xls) else None,
           "_data_start": ds, "_bytes_per_trace": bpt,
           "n_samples": int(h.get("n_samples") or 0),
           "format_code": int(h.get("format_code") or 0),
           "sample_interval_us": h.get("sample_interval_us"),
           "byte_order_big": big}
    if out["ils"] is None and out["xls"] is None:
        out = None

    if len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    _INDEX_CACHE[key] = out
    return out


def slice_values(path: str, axis: str = "inline"):
    """Sorted distinct inline (or crossline) numbers in a volume, or []."""
    idx = trace_index(path)
    if not idx:
        return []
    import numpy as np
    a = idx.get("ils" if axis == "inline" else "xls")
    if a is None:
        return []
    return [int(v) for v in np.unique(a)]


def read_slice_samples(path: str, axis: str, value: int, max_traces: int = 1200):
    """(data, times_ms) for ONE inline or crossline -- samples x traces.

    This is the difference between browsing a volume and looking at it. The
    sequential reader hands back "the first N traces", which in a 3D volume is
    a fragment of one inline and tells you nothing about the survey; a named
    inline is a section a geologist can actually interpret.
    """
    import numpy as np
    idx = trace_index(path)
    if not idx:
        return None, None
    a = idx.get("ils" if axis == "inline" else "xls")
    if a is None:
        return None, None
    sel = np.flatnonzero(a == int(value))
    if not sel.size:
        return None, None
    if sel.size > max_traces:
        # Keep the section honest: take a regular decimation across the WHOLE
        # slice rather than the first max_traces, which would silently show one
        # end of the line and call it the section.
        sel = sel[np.linspace(0, sel.size - 1, max_traces).astype(int)]

    ns = idx["n_samples"]
    ds, bpt = idx["_data_start"], idx["_bytes_per_trace"]
    dtype, conv = _NP_FMT.get(idx["format_code"], (">i1", None))
    if not idx["byte_order_big"]:
        dtype = dtype.replace(">", "<")
    isz = np.dtype(dtype).itemsize
    if ns * isz > bpt - TRACE_HDR:
        return None, None

    try:
        mm = np.memmap(path, dtype=np.uint8, mode="r")
        cols = []
        for i in sel:
            off = ds + int(i) * bpt + TRACE_HDR
            raw = mm[off: off + ns * isz]
            v = np.frombuffer(raw.tobytes(), dtype=dtype, count=ns)
            if conv == "ibm":
                with np.errstate(over="ignore", invalid="ignore"):
                    v = _ibm_to_ieee(v)
            else:
                v = v.astype(np.float32)
            cols.append(np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0))
        del mm
    except Exception:
        return None, None
    if not cols:
        return None, None

    _other = idx.get("xls" if axis == "inline" else "ils")
    read_slice_samples.last_stats = {
        "axis": axis, "value": int(value), "traces": len(cols),
        "of": int(np.count_nonzero(a == int(value))),
        "cross_axis": "crossline" if axis == "inline" else "inline",
        "cross_values": ([int(v) for v in _other[sel]]
                         if _other is not None else None)}
    data = np.column_stack(cols)
    si_ms = (idx.get("sample_interval_us") or 0) / 1000.0
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
