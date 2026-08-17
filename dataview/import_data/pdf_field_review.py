"""
pdf_field_review.py — edit extracted PDF fields before they are BCP-staged.

Sits between the UWI gate and the "Stage all" button in bulk_dir_loader.run(). The
scan writes one CSV per document kind to <bulk>/_pdf_extract/pdf_<kind>.csv and records
each in scan["rows"] with extracted=="pdf". This screen reads those CSVs into editable
grids, applies fixes for the three known extraction failures, and writes the corrected
CSVs back in place so staging picks them up with zero change to the extractor or stager.

Fixes wired in:
  1. Blank OPERATOR / UWI      -> flagged per row; editable inline; junk rows deletable.
  2. Mangled casing OD         -> `_num()` strips '/' and '"', so 13-3/8" arrives as
                                  "13-38". od_to_decimal() recovers it to 13.375 and snaps
                                  to the nearest standard casing OD; unrecoverable values
                                  are flagged, never silently guessed.
  3. Thin / broken survey grid -> dynamic row editing + a bulk-paste box that turns pasted
                                  "MD INC AZI TVD" lines into station rows.

Pure helpers (od_to_decimal, parse_survey_paste, validate_frame, autofix_frame) are
Streamlit-free and unit-testable headless.
"""
import os
import re

try:
    import streamlit as st
except Exception:                       # allow headless import for tests
    st = None

try:
    import pandas as pd
except Exception:
    pd = None

# kind -> friendly label + target (fall back to filename parse if the loader isn't importable)
try:
    from dataview.import_data import pdf_document_loader as _pdf
except Exception:
    try:
        import pdf_document_loader as _pdf
    except Exception:
        _pdf = None

_LABEL = {
    "well": "Well header", "formation": "Formation tops", "casing": "Casing strings",
    "stim": "Stimulation stages", "dst": "DST / well test", "dst_period": "DST periods",
    "pressure": "RFT / MDT pressure", "petro_interp": "Petrophysical interp",
    "petro_zone": "Petrophysical zones", "srvy_hdr": "Survey header", "srvy_sta": "Survey stations",
}
# well header first, then the order children are safe to review in
_KIND_ORDER = ["well", "srvy_hdr", "srvy_sta", "dst", "dst_period", "petro_interp",
               "petro_zone", "formation", "casing", "stim", "pressure"]


# ── OD recovery ────────────────────────────────────────────────────────────────
# standard API casing/tubing outside diameters (inches)
_STD_OD = [4.5, 5.0, 5.5, 6.625, 7.0, 7.625, 8.625, 9.625, 10.75, 11.75,
           13.375, 16.0, 18.625, 20.0, 24.0, 30.0]
# squished fraction (slash removed by _num) -> (numerator, denominator)
_SQUISH = {"12": (1, 2), "14": (1, 4), "34": (3, 4), "18": (1, 8), "38": (3, 8),
           "58": (5, 8), "78": (7, 8), "316": (3, 16), "516": (5, 16), "716": (7, 16),
           "916": (9, 16), "1116": (11, 16), "1316": (13, 16), "1516": (15, 16)}


def _fmt_in(x):
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _snap(x):
    """Snap to the nearest standard OD if within 0.02"; return (value, matched_standard)."""
    for s in _STD_OD:
        if abs(x - s) <= 0.02:
            return s, True
    return x, False


def od_to_decimal(raw):
    """Recover a casing OD to decimal inches.

    Returns (value_str, changed, needs_review):
      value_str    — recovered decimal string (or the original if untouched/unrecoverable)
      changed      — True if we altered the value
      needs_review — True if recovery was uncertain and a human should confirm
    """
    r = str(raw or "").strip().strip('"').strip()
    if not r:
        return "", False, False
    # already plain decimal (e.g. "20", "13.375") — snap to standard, never flag
    if re.fullmatch(r"\d+(\.\d+)?", r):
        x = float(r)
        sx, _ = _snap(x)
        out = _fmt_in(sx)
        return out, (out != r), False
    # clean fraction: whole + a/b  (13-3/8, 13 3/8, 9-5/8)
    m = re.fullmatch(r"(\d+)[-\s](\d+)/(\d+)", r)
    if m:
        x = int(m[1]) + int(m[2]) / int(m[3])
        sx, near = _snap(x)
        return _fmt_in(sx), True, (not near)
    # bare fraction a/b
    m = re.fullmatch(r"(\d+)/(\d+)", r)
    if m:
        x = int(m[1]) / int(m[2])
        sx, near = _snap(x)
        return _fmt_in(sx), True, (not near)
    # mangled: whole-squished  ("13-38" -> 13 3/8 -> 13.375)
    m = re.fullmatch(r"(\d+)-(\d+)", r)
    if m and m[2] in _SQUISH:
        n, d = _SQUISH[m[2]]
        x = int(m[1]) + n / d
        sx, near = _snap(x)
        return _fmt_in(sx), True, (not near)
    # unrecoverable — leave as-is, ask a human
    return r, False, True


# ── survey bulk paste ───────────────────────────────────────────────────────────
def parse_survey_paste(text):
    """Turn pasted rows into station dicts.

    Each line contributes MD, INCLINATION, AZIMUTH, TVDSS from its first four numeric
    tokens (MD + at least one more required). Handles tabs, spaces, and commas.
    """
    out = []
    for line in str(text or "").splitlines():
        nums = re.findall(r"-?\d+(?:\.\d+)?", line)
        if len(nums) >= 2:
            md, inc, azi, tvd = (nums + ["", "", "", ""])[:4]
            out.append({"MD": md, "INCLINATION": inc, "AZIMUTH": azi, "TVDSS": tvd})
    return out


# ── validation ───────────────────────────────────────────────────────────────
def row_is_empty(row, ignore=("SOURCE",)):
    for k, v in row.items():
        if k in ignore:
            continue
        if str(v or "").strip():
            return False
    return True


def _looks_number(v):
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", str(v or "").strip()))


def validate_frame(kind, df):
    """Return a list of human-readable issue strings for one kind's frame."""
    issues = []
    cols = list(df.columns)
    n = len(df)
    empties = sum(1 for _, r in df.iterrows() if row_is_empty(r.to_dict()))
    if empties:
        issues.append(f"{empties} empty row(s) — delete before staging")
    if "UWI" in cols:
        blank = sum(1 for v in df["UWI"] if not str(v or "").strip())
        if blank:
            issues.append(f"{blank} row(s) missing UWI — assign here or in the UWI gate")
    if kind == "well" and "OPERATOR" in cols:
        blank = sum(1 for v in df["OPERATOR"] if not str(v or "").strip())
        if blank:
            issues.append(f"{blank} well(s) with blank OPERATOR")
    if kind == "casing" and "OD_IN" in cols:
        bad = sum(1 for v in df["OD_IN"] if od_to_decimal(v)[2])
        if bad:
            issues.append(f"{bad} OD value(s) need review (couldn't recover cleanly)")
    if kind == "srvy_sta":
        for col in ("MD", "INCLINATION", "AZIMUTH"):
            if col in cols:
                bad = sum(1 for v in df[col] if str(v or "").strip() and not _looks_number(v))
                if bad:
                    issues.append(f"{bad} {col} value(s) not numeric — likely a parse error")
        if n == 0:
            issues.append("no station rows — paste the survey grid below")
    return issues


def autofix_frame(kind, df):
    """Apply automatic recovery. Returns (df, summary_dict). Currently: casing OD."""
    summary = {}
    if kind == "casing" and "OD_IN" in df.columns:
        fixed = review = 0
        new = []
        for v in df["OD_IN"]:
            val, changed, needs = od_to_decimal(v)
            new.append(val)
            fixed += int(changed)
            review += int(needs)
        df = df.copy()
        df["OD_IN"] = new
        summary = {"od_fixed": fixed, "od_review": review}
    return df, summary


# ── Streamlit UI ────────────────────────────────────────────────────────────────
def _kind_of(path):
    base = os.path.basename(path)
    m = re.fullmatch(r"pdf_(.+)\.csv", base, re.I)
    return m.group(1).lower() if m else base


def _pdf_rows(scan):
    return [r for r in (scan.get("rows") or []) if r.get("extracted") == "pdf" and r.get("path")]


def _seed(ss, rows):
    """Load frames from disk once per scan signature; re-seed only when files change."""
    sig = []
    for r in rows:
        try:
            sig.append((r["path"], os.path.getmtime(r["path"])))
        except OSError:
            sig.append((r["path"], 0))
    sig = tuple(sig)
    if ss.get("bdl_pdf_sig") == sig and ss.get("bdl_pdf_frames"):
        return
    frames, notes = {}, {}
    for r in rows:
        kind = _kind_of(r["path"])
        try:
            df = pd.read_csv(r["path"], dtype=str, keep_default_na=False)
        except Exception as e:
            # An empty file is recoverable, and used to be terminal: the df[[]] bug above
            # wrote one, then this turned it into a permanent error because a frame that
            # can't be read is a frame Save won't rewrite. _COLS knows what belongs in each
            # kind, so rebuild the header and carry on with zero rows — which is the truth.
            _empty = isinstance(e, getattr(pd.errors, "EmptyDataError", ()))
            _cols = list((getattr(_pdf, "_COLS", {}) or {}).get(kind, [])) if _pdf else []
            if _empty and _cols:
                if "inventory_id" not in [c.lower() for c in _cols]:
                    _cols = _cols + ["inventory_id"]
                frames[r["path"]] = (kind, pd.DataFrame(columns=_cols), None)
                continue
            frames[r["path"]] = (kind, None, str(e))
            continue
        df, summary = autofix_frame(kind, df)
        frames[r["path"]] = (kind, df, None)
        if summary:
            notes[r["path"]] = summary
    ss["bdl_pdf_frames"] = frames
    ss["bdl_pdf_notes"] = notes
    ss["bdl_pdf_sig"] = sig


def render_pdf_review(ss, schema=None):
    """Render the review screen. No-op unless the scan produced PDF extractions."""
    if st is None or pd is None:
        return
    scan = ss.get("bdl_scan")
    if not scan:
        return
    rows = _pdf_rows(scan)
    if not rows:
        return

    _seed(ss, rows)
    frames = ss["bdl_pdf_frames"]
    notes = ss.get("bdl_pdf_notes", {})

    st.divider()
    st.subheader("PDF field review")
    st.caption("Fix extracted values before staging — blank operators/UWIs, mangled casing "
               "sizes (13-3/8\" → 13.375), and survey grids that didn't parse. Edits here are "
               "written back to the staging CSVs, so **Stage** loads the corrected data.")

    # roll-up metrics across all frames
    tot = blank_uwi = blank_op = od_fix = od_rev = empty = 0
    for path, (kind, df, err) in frames.items():
        if df is None:
            continue
        tot += len(df)
        if "UWI" in df.columns:
            blank_uwi += sum(1 for v in df["UWI"] if not str(v or "").strip())
        if kind == "well" and "OPERATOR" in df.columns:
            blank_op += sum(1 for v in df["OPERATOR"] if not str(v or "").strip())
        empty += sum(1 for _, r in df.iterrows() if row_is_empty(r.to_dict()))
        s = notes.get(path, {})
        od_fix += s.get("od_fixed", 0)
        od_rev += s.get("od_review", 0)
    m = st.columns(5)
    m[0].metric("rows", tot)
    m[1].metric("blank UWI", blank_uwi)
    m[2].metric("blank operator", blank_op)
    m[3].metric("OD auto-fixed", od_fix)
    m[4].metric("empty/junk", empty)
    if od_rev:
        st.caption(f"⚠️ {od_rev} casing OD value(s) couldn't be recovered cleanly — check the "
                   "casing grid.")

    # order the frames: well header first, then children
    ordered = sorted(frames.items(), key=lambda kv: (_KIND_ORDER.index(_kind_of(kv[0]))
                     if _kind_of(kv[0]) in _KIND_ORDER else 99))

    for path, (kind, df, err) in ordered:
        label = _LABEL.get(kind, kind)
        target = None
        if _pdf is not None:
            target = _pdf.TARGET.get(kind)
        title = f"{label}  ·  {target or ''}  ({0 if df is None else len(df)} rows)"
        issues = [] if df is None else validate_frame(kind, df)
        if issues:
            title = "⚠️ " + title
        with st.expander(title, expanded=bool(issues) and kind in ("well", "casing", "srvy_sta")):
            if err:
                st.error(f"couldn't read {os.path.basename(path)}: {err}")
                continue
            if kind == "casing":
                st.caption("OD_IN was auto-recovered to decimal inches. Values that couldn't be "
                           "recovered are left untouched — fix them by hand.")

            edited = st.data_editor(
                df, num_rows="dynamic", hide_index=True, use_container_width=True,
                key=f"bdl_pdf_ed_{kind}",
                column_config={"UWI": st.column_config.TextColumn(
                    "UWI", help="Required to promote — must exist in dv_well")}
                if "UWI" in df.columns else None)
            # persist edits back into session state so Save writes the latest
            frames[path] = (kind, edited, None)

            # survey bulk-paste rebuild
            if kind == "srvy_sta":
                # NB: we're already inside an expander (the per-block one above), and
                # Streamlit forbids nesting them — so gate this with a checkbox, not a
                # second expander.
                if st.checkbox("📋 Paste survey stations (MD  INC  AZI  TVD per line)",
                               key=f"bdl_pdf_paste_open_{kind}"):
                    txt = st.text_area("One station per line — spaces, tabs, or commas",
                                       key=f"bdl_pdf_paste_{kind}", height=140,
                                       placeholder="0      0.0    0.0    0\n500    2.1    182.4  499\n...")
                    inherit = {}
                    if len(edited):
                        first = edited.iloc[0].to_dict()
                        for c in ("UWI", "SRVY_ID", "SURVEY_SEQ_NO", "SOURCE"):
                            if c in first:
                                inherit[c] = first[c]
                    if st.button("Add pasted stations", key=f"bdl_pdf_addsta_{kind}"):
                        new = parse_survey_paste(txt)
                        if new:
                            add = pd.DataFrame([{**inherit, **r} for r in new]).reindex(
                                columns=edited.columns, fill_value="")
                            frames[path] = (kind, pd.concat([edited, add], ignore_index=True), None)
                            st.rerun()
                        else:
                            st.warning("No numeric station rows found in the pasted text.")

            for msg in issues:
                st.caption("• " + msg)

    # save — writes every frame back to its CSV path in original column order
    st.write("")
    c1, c2 = st.columns([1, 3])
    if c1.button("💾 Save reviewed CSVs", type="primary", key="bdl_pdf_save"):
        written = []
        for path, (kind, df, err) in frames.items():
            if df is None:
                continue
            # `df[[bool, bool, ...]]` filters rows — but ONLY while the list is non-empty.
            # Delete the last row and the comprehension yields [], and pandas reads df[[]] as
            # "select these zero COLUMNS", not "keep these zero rows". The result has no
            # columns, to_csv writes a bare newline, and the next _seed() can't parse it:
            #   couldn't read pdf_petro_interp.csv: No columns to parse from file
            # After which nothing could repair it — Save skips a frame that failed to read,
            # and the extractor skips a kind with no rows. Deleting the last row of a grid
            # permanently bricked its CSV. .iloc with explicit positions keeps the columns.
            keep = [i for i, (_, r) in enumerate(df.iterrows())
                    if not row_is_empty(r.to_dict())]
            clean = df.iloc[keep] if len(df.columns) else df
            clean.to_csv(path, index=False, encoding="utf-8")
            written.append(f"{os.path.basename(path)} ({len(clean)} rows)")
        ss["bdl_pdf_reviewed"] = True
        # force a re-seed so metrics reflect the saved files
        ss.pop("bdl_pdf_sig", None)
        c2.success("Saved: " + ", ".join(written))
    elif ss.get("bdl_pdf_reviewed"):
        c2.caption("✅ reviewed CSVs saved — staging will load the corrected data.")
