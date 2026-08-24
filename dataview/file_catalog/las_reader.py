"""
las_reader.py — one way in to lasio, with the DLM the standard defines.

WHY THIS EXISTS
---------------
lasio 0.31 parses a LAS 3.0 HEADER correctly — VERS, WELL, CURVE all come back
right — and then ignores the ~Version section's DLM declaration when it reads
the data. Measured on a four-row 3.0 file:

    DLM. SPACE    4 rows, no nulls              correct
    DLM. COMMA    8 rows, every non-depth nan   silently wrong

Not an exception, not a warning: numbers. That is the worst failure this
codebase recognises — a confident wrong value plots, exports and gets quoted,
while a missing one is visible. So the delimiter is honoured here, before
lasio sees the data.

WHAT IT DOES NOT DO
-------------------
This is not LAS 3.0 support. 3.0 also allows arbitrary extra sections, ~Data
blocks other than ~Ascii, and typed/array columns, none of which are handled;
a 3.0 file using them still parses only as far as lasio manages. What is fixed
is the one case that silently corrupts ordinary curve data.

ONE WAY IN
----------
Every lasio.read in the repo goes through here. There were twenty-one, and a
delimiter fix applied to some of them is worse than none: the same file would
read one way in capture and another in the viewer, which is the shape of every
"two spellings" bug in CLAUDE.md. selftest fails if a direct lasio.read
reappears outside this module.
"""
from __future__ import annotations

import io
import re

# ~A is the LAS 2.0 data section; 3.0 spells it ~Ascii and also allows other
# ~*_Data sections. Only the ASCII curve block is rewritten.
_DATA_SECTION = re.compile(r"^\s*~A", re.IGNORECASE)
_VERS = re.compile(r"^\s*VERS\s*\.\s*([0-9.]+)", re.IGNORECASE)
_DLM = re.compile(r"^\s*DLM\s*\.\s*([A-Za-z]+)", re.IGNORECASE)

# The three the standard names. SPACE needs no rewrite.
_DELIMS = {"COMMA": ",", "TAB": "\t"}


def _sniff(text):
    """(vers, dlm_char) from the header, without parsing the whole file."""
    vers, dlm = None, None
    for line in text.splitlines():
        if _DATA_SECTION.match(line):
            break                        # header is over
        if vers is None:
            m = _VERS.match(line)
            if m:
                vers = m.group(1)
                continue
        if dlm is None:
            m = _DLM.match(line)
            if m:
                dlm = _DELIMS.get(m.group(1).upper())
    return vers, dlm


def _respace(text, dlm_char):
    """Replace the delimiter with spaces INSIDE THE DATA SECTION ONLY.

    Header lines legitimately contain commas — a well name, an operator, a
    description — and rewriting those would corrupt the metadata to fix the
    data. The switch flips on the ~A line and never flips back: in 3.0 a later
    section could follow, but lasio stops at the first data block anyway, so
    rewriting past it changes nothing it will read.
    """
    out, in_data = [], False
    for line in text.splitlines(keepends=True):
        if not in_data and _DATA_SECTION.match(line):
            in_data = True
            out.append(line)
            continue
        out.append(line.replace(dlm_char, " ") if in_data else line)
    return "".join(out)


def read_las(source, **kw):
    """lasio.read, with DLM honoured. Same arguments, same return.

    `source` may be a path or anything lasio accepts; only a readable path is
    sniffed, and any failure falls back to lasio unchanged — a delimiter fix
    must never be the reason a file that used to load stops loading.
    """
    import lasio

    try:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return lasio.read(source, **kw)          # not a path we can pre-read

    try:
        vers, dlm = _sniff(text)
        if dlm:
            return lasio.read(io.StringIO(_respace(text, dlm)), **kw)
    except Exception:
        pass                                     # fall through to plain lasio
    return lasio.read(source, **kw)
