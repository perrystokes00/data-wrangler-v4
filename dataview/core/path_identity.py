r"""
path_identity.py  —  Data Wrangler v3
================================================================================
Derive a file's *identity* from its path/name when the file's internal header
didn't yield one:

  • wells   -> a candidate UWI14 (14-char zero-padded API) parsed from the
               filename, then folder segments
  • seismic -> a candidate survey name parsed from the filename

These are *candidates* — a 10-14 digit run in a name can be a date or a job
number — so they're meant to be confirmed in the review grid, not trusted blindly.

Used by the review grid (bulk human keying) and, later, by catalog-on-open as
the fallback rung after internal-header extraction.

Pure-Python, no DB. Importable from the app or runnable as a quick CLI probe:
    py path_identity.py "C:\data\42-329-12345_GR.las" "C:\seis\Permian_3D_2019.segy"
"""
import os
import re
import sys

# A run that starts and ends in a digit and is >=10 chars of digits/-_. — the
# separators get stripped, leaving 10-14 digits for a plausible API.
_TOKEN = re.compile(r"\d[\d\-_.]{8,}\d")


def _basename(path):
    """Last path segment, splitting on both \\ and / regardless of OS."""
    return re.split(r"[\\/]", path or "")[-1]


def _dirname(path):
    parts = re.split(r"[\\/]", path or "")
    return "\\".join(parts[:-1])


def canon_root(p):
    r"""A pasted folder, made canonical: quotes off, separators collapsed.

    Windows treats C:\\a\\b and C:\a\b as the same folder, so a doubled path
    scans perfectly and produces a SECOND catalog entry for every file, with a
    different INVENTORY_ID. That is invisible until somebody counts rows and
    finds twice as many files as exist.

    LIVES HERE, NOT IN page_workbench, because the pipeline needs it too and
    page_workbench imports streamlit. The CLI and the detached pipeline child
    must be able to canonicalise a root without dragging the UI in — every
    other helper in this module is pure-Python for the same reason.
    page_workbench keeps `_canon_root` as an alias for this.
    """
    s = str(p or "").strip()
    if s.startswith("& "):
        s = s[2:].strip()
    for q in ('"', "'", "\u201c", "\u201d"):
        if s.startswith(q):
            s = s[1:]
        if s.endswith(q):
            s = s[:-1]
    s = s.strip()
    if not s:
        return s
    s = os.path.expandvars(os.path.expanduser(s))
    # normpath collapses repeated separators; keep a UNC prefix intact
    unc = s.startswith("\\\\") and not s.startswith("\\\\\\\\")
    out = os.path.normpath(s)
    if unc and not out.startswith("\\\\"):
        out = "\\\\" + out.lstrip("\\")
    return out


# Characters a QUOTE_NONE writer cannot emit without an escapechar. None of
# the four is legal in a Windows path; a value carrying one is already damaged.
_BULK_UNSAFE = {"\t": " ", "\r": " ", "\n": " ", '"': "'"}


def bulk_field(v):
    r"""(text, was_changed) — a value safe to write through bulk_csv_writer.

    Returns was_changed=True when a character had to be substituted, so the
    caller can REPORT it. A silent repair here writes a value that differs from
    the file on disk, and a wrong value outlives a missing one.
    """
    s = "" if v is None else str(v)
    if not any(b in s for b in _BULK_UNSAFE):
        return s, False
    for bad, repl in _BULK_UNSAFE.items():
        s = s.replace(bad, repl)
    return s, True


def bulk_csv_writer(fh):
    r"""A csv.writer whose output BULK INSERT stores VERBATIM.

    NO escapechar, and that is the whole point. Every catalog staging writer
    here once carried escapechar='\\', and with QUOTE_NONE the csv module
    escapes the escape character itself — so each separator in a Windows path
    was doubled on the way out:

        C:\Users\perry\x   ->   C:\\Users\\perry\\x

    BULK INSERT has no escape concept, so the doubled form was stored as-is.
    INVENTORY_ID is a SHA1 of the path, so the same file catalogued under both
    spellings takes two ids and provenance stops resolving. Measured on the
    16 Aug database: 2,094 of 3,876 rows doubled, 1,301 of them a duplicate of
    a file already in the catalog, and 1,317 dv_well rows left citing a source
    nothing could find.

    canon_root() cannot prevent this. It cleans the pasted root on the way IN;
    the doubling happens afterwards, on the way OUT.

    lineterminator is pinned to \r\n because the BULK INSERT statements specify
    ROWTERMINATOR = '0x0D0A'. It matched only by relying on the csv default —
    the coupling is real, so it is stated here rather than assumed.

    Pair with bulk_field(); without an escapechar the writer RAISES on TAB,
    '"', CR and LF, and one bad value would otherwise fail the whole batch.
    """
    import csv
    return csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_NONE,
                      lineterminator="\r\n")


def norm_uwi14(s):
    """Normalize any UWI-ish string to a 14-char API key, or None.
    Strip -_./ and spaces, require all-numeric and 10-14 digits, pad to 14.
    Rejects all-same-digit runs (e.g. 00000000000000)."""
    if not s:
        return None
    d = re.sub(r"\D", "", str(s))
    if not (10 <= len(d) <= 14) or len(set(d)) <= 1:
        return None
    return (d + "0" * 14)[:14]


def uwi14_from_path(path):
    """Best UWI14 candidate from a path. Filename is tried before folders, and
    longer digit runs win. Returns (uwi14, source) where source is 'filename'
    or 'folder', or (None, None) if nothing plausible is found."""
    path = (path or "").replace("/", "\\")
    base = _basename(path)
    folders = _dirname(path)

    def _cands(text):
        out = []
        for m in _TOKEN.finditer(text or ""):
            d = re.sub(r"\D", "", m.group(0))
            if 10 <= len(d) <= 14 and len(set(d)) > 1:
                out.append(d)
        return out

    # (digits, priority): priority 1 = filename, 0 = folder
    scored = [(d, 1) for d in _cands(base)] + [(d, 0) for d in _cands(folders)]
    if not scored:
        return None, None
    d, pri = max(scored, key=lambda t: (len(t[0]), t[1]))   # most digits, then filename
    return (d + "0" * 14)[:14], ("filename" if pri == 1 else "folder")


def survey_from_path(path):
    """Candidate seismic survey name from a file path. Prefers the app's
    seis_filename_parser when available; otherwise cleans up the basename
    (separators -> spaces, collapse). Returns a string or None."""
    base = _basename(path)
    stem = os.path.splitext(base)[0]

    try:                                            # use the app's parser if present
        from dataview.file_catalog.seis_filename_parser import parse_seis_filename
        p = parse_seis_filename(base)
        nm = p.get("survey_name") if isinstance(p, dict) else None
        if nm and str(nm).strip():
            return str(nm).strip()
    except Exception:
        pass

    nm = re.sub(r"[_\-.]+", " ", stem)
    nm = re.sub(r"\s+", " ", nm).strip()
    return nm or None


def wellname_from_path(path):
    """Best-effort well NAME from a LIS/DLIS (or similar) filename, for use as
    a fallback when the file's own origin/wellsite record has no well name.

    No single naming convention exists, so the heuristic picks the token that
    carries the well identity: first a token with letters + digits + a dash
    (e.g. 'A12a-CPP-A2', 'A-5-1'), else a token with letters + digits
    (e.g. 'A151', 'a0501t01'), else the cleaned stem. Operator prefixes
    ('Chevron_'), id prefixes ('G030088972__'), run/curve-type/composite
    suffixes are skipped. Returns a string or None. Heuristic — review
    downstream; never trust over a real internal value.
    """
    stem = _basename(path)
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = stem.strip()
    if not stem:
        return None
    toks = [t for t in re.split(r"[_\s]+", stem) if t]

    def _has_alpha(t): return any(c.isalpha() for c in t)
    def _has_digit(t): return any(c.isdigit() for c in t)

    for t in toks:                         # 1) letters + digits + dash
        if "-" in t and _has_alpha(t) and _has_digit(t):
            return t
    for t in toks:                         # 2) letters + digits
        if _has_alpha(t) and _has_digit(t):
            return t
    return " ".join(toks) or None          # 3) cleaned stem


def identity_from_path(path, kind):
    """Convenience dispatch. kind in {'well','seis'}.
    well -> (uwi14, source); seis -> (survey_name, 'filename')."""
    if kind == "well":
        return uwi14_from_path(path)
    return survey_from_path(path), "filename"


if __name__ == "__main__":
    for p in sys.argv[1:]:
        u, src = uwi14_from_path(p)
        print(f"{p}\n  UWI14  : {u}  ({src})\n  SURVEY : {survey_from_path(p)}")
