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

LAS 3.0 MULTI-SECTION FILES
---------------------------
lasio cannot read one at all — a file with a second data set dies with

    ValueError: Cannot reshape ~A data size (25,) into 2 columns

because it assumes exactly one data block. But 3.0's whole point is that a
file may carry several, each as a <Name>_Definition / <Name>_Parameter /
<Name>_Data triple, and the standard names for them are the ones this catalog
already has tables for: Core, Inclinometry, Tops, Test, Perforation. A 3.0 file
can hold a directional survey as DATA rather than as a PDF somebody has to
read.

So split_las3() parses those directly rather than going through lasio: sections,
typed columns, and the delimiter this module already honours. It does NOT
replace read_las — an ordinary single-curve LAS of any version still goes to
lasio, which is well tested and handles wrapping, and split_las3 is for the
files lasio refuses.

WHAT IT DOES NOT DO
-------------------
Arrays (a column holding several values per row) and the ~Parameter half of
each triple are parsed as plain columns and ignored respectively. Vendor
sections with no _Definition are returned raw rather than typed. This is a
reader for the shapes the standard describes, not a certification against the
wild — no real 3.0 file has been through it yet.

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


# ═══════════════════════════════════════════════════════════════════════════ #
# LAS 3.0 — multi-section files
# ═══════════════════════════════════════════════════════════════════════════ #
# A 3.0 section header is `~Name` optionally followed by an association after a
# pipe: `~Log_Data | Log_Definition`. Everything up to the pipe (or whitespace)
# is the name; 3.0 allows arbitrary names, so nothing is matched against a list.
# THE NAME IS THE FIRST TOKEN AND THE REST OF THE LINE IS PROSE. Real files
# write "~VERSION INFORMATION", "~Well Information", "~CURVE INFORMATION" —
# the trailing words are decoration the standard has always allowed. Requiring
# the name to be the whole line matched none of them, so the first run against
# the two real spec samples found ZERO header sections and concluded neither
# was a 3.0 file. The synthetic fixture never caught it because I wrote it with
# bare "~Version" headers.
#
# An index is part of the name: ~Core[1] and ~Core[2] are two distinct core
# sets in one file, and collapsing them would silently drop the second.
_SECTION_HEAD = re.compile(r"^\s*~\s*([^\s|]+)[^|]*(?:\|\s*(.*?))?\s*$")

# 'Core[1]' -> ('Core', '1'). The base is what a mapping cares about; the index
# is what keeps two sets of the same kind apart.
_SECTION_INDEX = re.compile(r"^(.*?)\[(\d+)\]$")

# A header LINE inside a Definition/Parameter/Well section:
#     MNEM .UNIT   VALUE : DESCRIPTION {F}
# The format brace is 3.0's type declaration and sits at the end of the
# description. Unit may be empty; value may be empty.
# THE UNIT BINDS TIGHT TO THE DOT, with no whitespace allowed between them.
# That is not cosmetic: `VERS.   3.0 : ...` has NO unit, and a pattern that
# lets the unit group skip whitespace captures "3.0" as the unit and leaves
# the value empty — which is how the first cut of this reader decided a 3.0
# file was not a 3.0 file. In LAS the unit is always immediately after the
# dot; a space after the dot means there is none.
_HDR_LINE = re.compile(
    r"^\s*([^.\s]+)\s*\.([^\s:]*)\s*(.*?)\s*:\s*(.*?)\s*$")
_FMT_BRACE = re.compile(r"\{\s*([FSE])[^}]*\}\s*$", re.IGNORECASE)


class Las3Column:
    """One column of a 3.0 data set."""

    __slots__ = ("mnemonic", "unit", "descr", "fmt")

    def __init__(self, mnemonic, unit="", descr="", fmt=""):
        self.mnemonic = mnemonic
        self.unit = unit
        self.descr = descr
        self.fmt = (fmt or "").upper()          # 'F' | 'E' | 'S' | ''

    def __repr__(self):                          # pragma: no cover
        return f"<Las3Column {self.mnemonic}.{self.unit} {{{self.fmt}}}>"


class Las3Set:
    """One <Name>_Definition + <Name>_Data pair, parsed."""

    __slots__ = ("name", "columns", "rows")

    def __init__(self, name, columns, rows):
        self.name = name                         # 'Log', 'Core', 'Tops', …
        self.columns = columns
        self.rows = rows

    @property
    def mnemonics(self):
        return [c.mnemonic for c in self.columns]

    def __len__(self):
        return len(self.rows)

    def __repr__(self):                          # pragma: no cover
        return (f"<Las3Set {self.name} "
                f"{len(self.columns)} cols x {len(self.rows)} rows>")


class Las3File:
    """version / delimiter / well header / named data sets."""

    __slots__ = ("version", "wrap", "delimiter", "well", "sets", "raw_sections")

    def __init__(self, version, wrap, delimiter, well, sets, raw_sections):
        self.version = version
        self.wrap = wrap
        self.delimiter = delimiter
        self.well = well                         # {MNEMONIC: value}
        self.sets = sets                         # {'Log': Las3Set, …}
        self.raw_sections = raw_sections         # {name: [lines]} — everything

    def __repr__(self):                          # pragma: no cover
        return (f"<Las3File v{self.version} "
                f"sets={sorted(self.sets)} well={len(self.well)} fields>")


def _split_raw(text):
    """[(name, association, [body lines])] in file order.

    Comment lines (#) are dropped; blank lines are kept because a data row of
    all-nulls is not the same as no row, and dropping blanks inside a data
    block would silently renumber it.
    """
    out, name, assoc, body = [], None, None, []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _SECTION_HEAD.match(line)
        if m and line.lstrip().startswith("~"):
            if name is not None:
                out.append((name, assoc, body))
            name, assoc, body = m.group(1), (m.group(2) or "").strip(), []
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        out.append((name, assoc, body))
    return out


def _parse_header_lines(body):
    """[(mnem, unit, value, descr, fmt)] from a ~Well/~*_Definition body."""
    out = []
    for line in body:
        if not line.strip():
            continue
        m = _HDR_LINE.match(line)
        if not m:
            continue
        mnem, unit, value, descr = (g.strip() for g in m.groups())
        fmt = ""
        fm = _FMT_BRACE.search(descr)
        if fm:
            fmt = fm.group(1).upper()
            descr = _FMT_BRACE.sub("", descr).strip()
        out.append((mnem, unit, value, descr, fmt))
    return out


def _split_row(line, dlm):
    """One data row -> list of raw strings, honouring quotes.

    3.0 permits a quoted string containing the delimiter — a core description
    is exactly where that happens ("shale, silty"). Splitting naively would
    shift every column after it, which is the kind of wrong that lands in a
    table looking plausible.
    """
    if dlm is None:                       # whitespace-delimited
        return line.split()
    out, cur, q = [], [], False
    for ch in line:
        if ch == '"':
            q = not q
            continue
        if ch == dlm and not q:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return out


def _coerce(raw, fmt, null_value):
    """Typed value, or None for the file's NULL.

    An UNTYPED column is tried as a number and kept as text if that fails —
    the same 'wrong is worse than missing' rule the rest of the codebase uses:
    a description column must not silently become nan, and a depth must not
    silently become the string '541.0'.
    """
    s = (raw or "").strip()
    if s == "":
        return None
    if fmt == "S":
        return s
    try:
        v = float(s)
    except ValueError:
        return s if fmt != "F" else None   # declared numeric but is not
    if null_value is not None and v == null_value:
        return None
    return v


def split_las3(source):
    """Parse a LAS 3.0 file into its named data sets. Returns Las3File.

    Raises ValueError if the file does not declare VERS 3.x — this reader is
    not a general LAS parser and must not be pointed at a 2.0 file that lasio
    already handles better.
    """
    if hasattr(source, "read"):
        text = source.read()
    else:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

    raw = _split_raw(text)
    by_name = {n: b for n, _a, b in raw}
    raw_sections = dict(by_name)

    version = wrap = None
    dlm = None
    for n, _a, body in raw:
        if n.lower().startswith("v"):
            for mnem, _u, value, _d, _f in _parse_header_lines(body):
                mu = mnem.upper()
                if mu == "VERS":
                    version = value
                elif mu == "WRAP":
                    wrap = value.upper().startswith("Y")
                elif mu == "DLM":
                    dlm = _DELIMS.get(value.upper())
            break
    if not str(version or "").startswith("3"):
        raise ValueError(
            f"not a LAS 3.0 file (VERS={version!r}) — read_las handles 1.2/2.0")

    well, null_value = {}, None
    for n, _a, body in raw:
        if n.lower() == "well":
            for mnem, _u, value, _d, _f in _parse_header_lines(body):
                well[mnem.upper()] = value
                if mnem.upper() == "NULL":
                    try:
                        null_value = float(value)
                    except ValueError:
                        null_value = None
            break

    # A DATA SECTION IS ONE THAT NAMES A DEFINITION. That is the rule the real
    # files follow — "~Drilling | Drilling_Definition", "~Core[1] |
    # Core_Definition", "~ASCII | CURVE". My first cut keyed on a "_Data"
    # suffix, which the LAS 3.0 spec samples do not use ANYWHERE; it matched
    # only my own fixture. The association is the signal, and it also tells us
    # which definition to use without guessing at the name.
    #
    # A bare ~Ascii/~A with no association is 2.0's spelling surviving into a
    # 3.0 file; fall back to the conventional definition sections for it.
    sets = {}
    for name, assoc, body in raw:
        low = name.lower()
        bare_ascii = low in ("ascii", "a") and not assoc
        if not assoc and not bare_ascii:
            continue                       # a Definition/Parameter/Other block
        if low.endswith("_definition") or low.endswith("_parameter"):
            continue                       # never data, whatever it associates

        m = _SECTION_INDEX.match(name)
        base = (m.group(1) if m else name)
        if base.lower() in ("ascii", "a"):
            base = "Log"

        defname = assoc or None
        if bare_ascii:
            # CASE-INSENSITIVE, like the association lookup below. Looking for
            # the literal "Curve" found nothing in a file headed
            # "~CURVE INFORMATION" — every generated 3.0 file parsed to zero
            # data sets while its header read perfectly, which is the quiet
            # kind of wrong: no exception, just an empty result.
            #
            # The real spec samples never caught this because they write
            # "~ASCII | CURVE" — an explicit association, which took the other
            # branch. It surfaced only once the generator produced 3.0 files
            # of its own, with a bare ~Ascii. Two sources of files, two
            # different paths through the same function.
            _low = {k.lower(): k for k in by_name}
            defname = next((_low[c] for c in ("log_definition", "curve")
                            if c in _low), None)
        if defname and defname not in by_name:
            # case may differ between the association and the section it names
            # — the spec sample writes "~TEST | TEST_Definition" against a
            # section headed "~Test_Definition".
            defname = next((k for k in by_name
                            if k.lower() == defname.lower()), None)
        if not defname:
            continue
        base = name if m else base         # keep Core[1] / Core[2] distinct
        cols = [Las3Column(m, u, d, f)
                for m, u, _v, d, f in _parse_header_lines(by_name[defname])]
        rows = []
        for line in body:
            if not line.strip():
                continue
            cells = _split_row(line, dlm)
            rows.append([_coerce(cells[i] if i < len(cells) else "",
                                 cols[i].fmt if i < len(cols) else "",
                                 null_value)
                         for i in range(len(cols))])
        sets[base] = Las3Set(base, cols, rows)

    return Las3File(version, bool(wrap), dlm, well, sets, raw_sections)
