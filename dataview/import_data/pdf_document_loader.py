"""
pdf_document_loader.py — extract oil & gas well documents (PDF) into bulk-loader staging CSVs.

Standalone (pdfplumber). Detects the document type from its text, then extracts the header
(→ dv_well, always) plus any data tables that map to real dv_* targets:

  Scout Ticket        → dv_well, dv_well_casing, dv_well_stimulation, dv_well_dst
  End of Well Report  → dv_well, dv_well_formation_top
  Directional Survey  → dv_well, dv_well_dir_srvy_hdr/_sta
  RFT/MDT Pressure    → dv_well, dv_well_pressure
  Well Test           → dv_well, dv_well_dst, dv_well_dst_period
  Casing/Cementing    → dv_well, dv_well_casing
  Petrophysical       → dv_well, dv_well_petro_interp, dv_well_petro_zone

Generated PKs (casing_id, stim_id, zone_id, …) are emitted as sequence values keyed to their
parent so the set-based promote's seq_num handles them. UWI is often blank → the review gate.
Tables with no dv_* home (NPT events, CBL bond, daily ops) are skipped (left for doc-linking).
"""
import os, csv, glob, hashlib, re


def inventory_id(full_path):
    """The SOURCE DOCUMENT's identity — SHA1(UPPER(abspath), UTF-16-LE), 40 chars.

    Identical to file_gate.inventory_id and the PK of file_catalog.GLOBAL_FILE_CATALOG.
    Stamped on EVERY row of EVERY kind this file produces.

    Why it matters here more than anywhere: one scout ticket feeds 11 target tables. The UWI
    gate used to key on whatever per-table id a row happened to carry — INTERP_ID, SRVY_ID —
    and casing/stim/dst/dst_period/pressure rows carry NONE of those and no FILE_PATH either.
    _file_key returned "" for them, the gate skipped them outright, and they staged with a
    blank uwi: `Cannot insert NULL into 'uwi'`. The screen said "assign in the UWI gate" and
    the gate could not see them. One key per document fixes all eleven at once.
    """
    s = str(full_path).upper().strip()
    return hashlib.sha1(s.encode("utf-16-le")).hexdigest().upper()


def _num(s):
    return re.sub(r"[^0-9.\-]", "", str(s or "")).replace(",", "").strip(".") or ""


def _cell(row, i):
    return str(row[i]).strip() if row and i is not None and i < len(row) and row[i] is not None else ""


def _find_col(head, *cands):
    for i, h in enumerate(head):
        hl = str(h or "").strip().lower()
        for c in cands:
            if hl.startswith(c):
                return i
    return None


def _detect_type(text):
    t = text.lower()
    if "scout ticket" in t: return "scout"
    if "end of well" in t: return "eow"
    if "directional survey" in t or "survey report" in t: return "survey"
    if "rft" in t or "mdt" in t or "formation tester" in t: return "pressure"
    if "well test" in t or "flow test" in t: return "welltest"
    if "casing" in t and "cement" in t: return "casing"
    if "petrophysical" in t: return "petro"
    return "unknown"


# header labels → dv_well columns (shared across all doc types)
_HDR = {"operator": "OPERATOR", "well name": "WELL_NAME", "api": "UWI", "uwi": "UWI",
        "uwi / api": "UWI", "api / uwi": "UWI", "field": "FIELD_NAME", "county": "COUNTY",
        "state": "PROVINCE_STATE", "spud date": "SPUD_DATE", "completion date": "COMPLETION_DATE",
        "total depth": "DRILLERS_TD", "total depth md": "DRILLERS_TD",
        "kb elevation": "KB_ELEV", "status": "STATUS"}


def _header(text, tables):
    out = {}
    # structured key/value cells first
    for t in tables:
        for row in t or []:
            cells = [str(c or "").strip() for c in row]
            for i, c in enumerate(cells):
                if c.endswith(":") and i + 1 < len(cells) and cells[i + 1]:
                    lab = c[:-1].strip().lower()
                    for k, col in _HDR.items():
                        if lab.endswith(k) and col not in out:
                            out[col] = cells[i + 1]
    # free-text 'Label: value' (handles two pairs per line)
    for m in re.finditer(r"([A-Za-z /]+?):\s*([^:\n]*?)(?=\s{2,}[A-Z][A-Za-z /]+?:|\n|$)", text):
        lab = m.group(1).strip().lower(); val = m.group(2).strip()
        for k, col in _HDR.items():
            if lab.endswith(k) and val and col not in out:
                out[col] = val
    return out


def _depth_unit(text):
    """Read the document's stated depth unit rather than assume it."""
    b = (text or "").lower()
    if re.search(r"\(\s*m\s*\)|\bmetres?\b|\bmeters?\b", b) and not re.search(r"\(\s*ft\s*\)", b):
        return "M"
    return "FT"


def extract_file(path, source="PDF"):
    import pdfplumber
    res = {k: [] for k in ("well", "formation", "casing", "stim", "dst", "dst_period",
                           "pressure", "petro_interp", "petro_zone", "srvy_hdr", "srvy_sta")}
    res.update({"file": os.path.basename(path), "uwi": "", "well_name": "", "doc_type": "unknown"})
    stem = os.path.splitext(os.path.basename(path))[0]
    text, tables = "", []
    try:
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages:
                text += (pg.extract_text() or "") + "\n"
                tables.extend(pg.extract_tables() or [])
    except Exception as e:
        res["error"] = str(e); return res

    if not text.strip():                        # image-only PDF (no text layer) -> OCR fallback
        try:
            text, tables = _ocr_reconstruct(path)
            res["ocr"] = True
        except Exception as e:
            res["error"] = f"ocr failed: {e}"; return res

    dt = _detect_type(text); res["doc_type"] = dt
    hdr = _header(text, tables)
    uwi = _num(hdr.get("UWI", "")); wn = hdr.get("WELL_NAME", "")
    res["uwi"], res["well_name"] = uwi, wn
    county = hdr.get("COUNTY", "").split(",")[0].split("\n")[0].strip()

    res["well"].append({
        "uwi": uwi, "well_name": wn, "operator_name": hdr.get("OPERATOR", ""),
        "field_name": hdr.get("FIELD_NAME", ""), "county": county,
        "province_state": hdr.get("PROVINCE_STATE", ""), "well_status": hdr.get("STATUS", ""),
        "spud_date": hdr.get("SPUD_DATE", ""), "completion_date": hdr.get("COMPLETION_DATE", ""),
        "final_td": _num(hdr.get("DRILLERS_TD", "")), "kb_elevation": _num(hdr.get("KB_ELEV", "")),
        "elevation_ouom": _depth_unit(text), "source": source})

    def rows_of(*sig):
        """Yield (head, body) for the first table whose header contains all signature words."""
        for t in tables:
            if not t or not t[0]:
                continue
            head = [str(c or "").strip().lower() for c in t[0]]
            joined = " ".join(head)
            if all(s in joined for s in sig):
                return [str(c or "").strip().lower() for c in t[0]], t[1:]
        return None, []

    # ---- formation tops (EOW) ----
    head, body = rows_of("formation", "top")
    if head:
        i_n, i_md = _find_col(head, "formation"), _find_col(head, "top (ft md)", "top md", "md")
        for k, r in enumerate(body, 1):
            unit = _cell(r, i_n)
            if unit:
                res["formation"].append({"uwi": uwi, "strat_name_set": "PDF_DOC",
                    "strat_unit_id": unit, "strat_unit_name": unit, "interp_id": f"PDF_{stem}"[:40],
                    "interpreter_ba_id": hdr.get("OPERATOR", ""),
                    "interp_date": hdr.get("COMPLETION_DATE", ""), "top_depth": _num(_cell(r, i_md)),
                    "base_depth": "", "depth_ouom": _depth_unit(text), "source": source})

    # ---- casing strings (scout + casing/cementing) ----
    head, body = rows_of("string" if dt == "casing" else "casing")
    if not head:
        head, body = rows_of("string")
    if head:
        i_s = _find_col(head, "string", "casing"); i_od = _find_col(head, "od", "size")
        i_w = _find_col(head, "weight"); i_g = _find_col(head, "grade")
        i_sd = _find_col(head, "shoe depth", "set depth")
        for k, r in enumerate(body, 1):
            s = _cell(r, i_s)
            if s:
                res["casing"].append({"UWI": uwi, "CASING_ID": str(k), "CASING_TYPE": s,
                    "STRING_NUM": str(k), "OD_IN": _num(_cell(r, i_od)), "WEIGHT_LB_FT": _num(_cell(r, i_w)),
                    "GRADE": _cell(r, i_g), "BASE_DEPTH": _num(_cell(r, i_sd)), "SOURCE": source})

    # ---- stimulation / perf stages (scout) ----
    head, body = rows_of("stage", "top")
    if head:
        i_st = _find_col(head, "stage"); i_t = _find_col(head, "top")
        i_b = _find_col(head, "base"); i_fl = _find_col(head, "fluid"); i_pp = _find_col(head, "proppant")
        i_mp = _find_col(head, "max pressure", "max treating"); i_rt = _find_col(head, "rate")
        for k, r in enumerate(body, 1):
            sn = _cell(r, i_st)
            if sn and sn.lower() != "total":
                res["stim"].append({"UWI": uwi, "COMPLETION_ID": "1", "STIM_ID": str(k),
                    "STAGE_NUM": _num(sn), "STIM_TYPE": "FRAC", "STAGE_TOP_DEPTH": _num(_cell(r, i_t)),
                    "STAGE_BASE_DEPTH": _num(_cell(r, i_b)), "FLUID_VOLUME_BBL": _num(_cell(r, i_fl)),
                    "PROPPANT_MASS_LBS": _num(_cell(r, i_pp)), "MAX_TREATING_PRESSURE_PSI": _num(_cell(r, i_mp)),
                    "MAX_RATE_BPM": _num(_cell(r, i_rt)), "SOURCE": source})

    # ---- RFT/MDT pressure points ----
    head, body = rows_of("depth", "pressure")
    if head and dt == "pressure":
        i_d = _find_col(head, "depth (ft md)", "depth"); i_f = _find_col(head, "formation")
        i_p = _find_col(head, "final pressure", "pre-test", "pressure"); i_fl = _find_col(head, "fluid")
        i_m = _find_col(head, "mobility")
        for k, r in enumerate(body, 1):
            d = _cell(r, i_d)
            if d:
                res["pressure"].append({"UWI": uwi, "PRESSURE_ID": str(k), "PRESSURE_TYPE": "RFT_MDT",
                    "TEST_DATE": "", "DEPTH": _num(d), "PRESSURE": _num(_cell(r, i_p)),
                    "FLUID_TYPE": _cell(r, i_fl), "MOBILITY": _num(_cell(r, i_m)),
                    "STRAT_UNIT_NAME": _cell(r, i_f), "TOOL_TYPE": "MDT", "SOURCE": source})

    # ---- well test: DST + periods ----
    if dt == "welltest":
        res["dst"].append({"UWI": uwi, "DST_ID": "1", "DST_NUM": "1", "TEST_TYPE": "FLOW_TEST",
                           "TEST_DATE": hdr.get("COMPLETION_DATE", ""), "SOURCE": source})
        head, body = rows_of("period")
        if head:
            i_p = _find_col(head, "period"); i_t = _find_col(head, "type"); i_du = _find_col(head, "duration")
            i_ch = _find_col(head, "choke"); i_o = _find_col(head, "avg oil", "oil")
            i_g = _find_col(head, "avg gas", "gas"); i_w = _find_col(head, "avg water", "water")
            for k, r in enumerate(body, 1):
                pn = _cell(r, i_p)
                if pn:
                    res["dst_period"].append({"UWI": uwi, "DST_ID": "1", "PERIOD_ID": str(k),
                        "PERIOD_TYPE": _cell(r, i_t), "PERIOD_SEQ": _num(pn),
                        "DURATION_MIN": _num(_cell(r, i_du)), "CHOKE_SIZE": _cell(r, i_ch),
                        "AVG_OIL_RATE": _num(_cell(r, i_o)), "AVG_GAS_RATE": _num(_cell(r, i_g)),
                        "AVG_WATER_RATE": _num(_cell(r, i_w)), "SOURCE": source})

    # ---- DST record from a scout/detail table (Test Date | Type | Top | Base | Result | ...) ----
    # runs for any doc carrying a DST detail table (e.g. scout tickets), unless a well-test
    # already produced the DST above. Only Test Date + Type have a home in dv_well_dst; the
    # richer detail (result, max oil/gas, API gravity) has no target column yet.
    if not res["dst"]:
        d_head, d_body = rows_of("test date", "result")
        if d_head:
            i_dt = _find_col(d_head, "test date"); i_ty = _find_col(d_head, "type")
            for k, r in enumerate(d_body, 1):
                td = _cell(r, i_dt)
                if re.match(r"\d{4}-\d\d-\d\d", td):
                    res["dst"].append({"UWI": uwi, "DST_ID": str(k), "DST_NUM": str(k),
                        "TEST_TYPE": _cell(r, i_ty) or "DST", "TEST_DATE": td, "SOURCE": source})
        else:                                    # OCR headers can wrap/drop; fall back to the data line
            m = re.search(r"(\d{4}-\d\d-\d\d)\s+(DST|FLOW[ _-]?TEST|DRILL[ _-]?STEM)", text, re.I)
            if m:
                res["dst"].append({"UWI": uwi, "DST_ID": "1", "DST_NUM": "1",
                    "TEST_TYPE": m.group(2).upper().replace(" ", "_"), "TEST_DATE": m.group(1),
                    "SOURCE": source})

    # ---- petrophysical: interp + zones ----
    if dt == "petro":
        res["petro_interp"].append({"UWI": uwi, "INTERP_ID": f"PETRO_{stem}",
            "INTERP_NAME": wn or stem, "INTERP_DATE": hdr.get("COMPLETION_DATE", ""), "SOURCE": source})
        head, body = rows_of("zone", "top")
        if head:
            i_z = _find_col(head, "zone"); i_t = _find_col(head, "top"); i_b = _find_col(head, "base")
            i_g = _find_col(head, "gross"); i_np = _find_col(head, "net pay", "net")
            i_ng = _find_col(head, "n/g"); i_phi = _find_col(head, "avg phie", "phie")
            i_sw = _find_col(head, "avg sw", "sw")
            for k, r in enumerate(body, 1):
                z = _cell(r, i_z)
                if z:
                    res["petro_zone"].append({"UWI": uwi, "INTERP_ID": f"PETRO_{stem}", "ZONE_ID": str(k),
                        "ZONE_NAME": z, "TOP_DEPTH": _num(_cell(r, i_t)), "BASE_DEPTH": _num(_cell(r, i_b)),
                        "GROSS_THICKNESS": _num(_cell(r, i_g)), "NET_THICKNESS": _num(_cell(r, i_np)),
                        "NET_TO_GROSS": _num(_cell(r, i_ng)), "PHI_EFFECTIVE_AVG": _num(_cell(r, i_phi)),
                        "SW_AVG": _num(_cell(r, i_sw)), "SOURCE": source})

    # ---- directional survey stations (any doc that carries a survey grid, incl. scout tickets) ----
    # MD + Azi is the distinctive survey signature; fall back to Meas Depth / MD+Incl for other layouts.
    s_head, s_body = rows_of("md", "azi")
    if not s_head:
        s_head, s_body = rows_of("meas depth")
    if not s_head:
        s_head, s_body = rows_of("md", "incl")
    if s_head or dt == "survey":
        srvy_id = (f"{uwi}-SRVY" if uwi else f"SRVY_{stem}")[:40]
        res["srvy_hdr"].append({"uwi": uwi, "survey_id": srvy_id, "source": source})
        if s_head:
            i_md = _find_col(s_head, "meas depth", "md"); i_in = _find_col(s_head, "incl", "inc")
            i_az = _find_col(s_head, "azim", "azi"); i_tv = _find_col(s_head, "tvd")
            for k, r in enumerate(s_body, 1):
                md = _cell(r, i_md)
                if not (md and re.match(r"^[\d,]+(\.\d+)?$", md.strip())):
                    continue
                try:                                    # guard: real stations only (incl 0-120, azi 0-360)
                    inc = float(_num(_cell(r, i_in)) or "999")
                    azi = float(_num(_cell(r, i_az)) or "999")
                except ValueError:
                    continue
                if inc <= 120 and azi <= 360:
                    res["srvy_sta"].append({"uwi": uwi, "survey_id": srvy_id,
                        "station_id": str(len(res["srvy_sta"]) + 1),   # unique within the survey
                        "md": _num(md), "incl": _num(_cell(r, i_in)), "azim": _num(_cell(r, i_az)),
                        "tvd": _num(_cell(r, i_tv)), "depth_ouom": _depth_unit(text), "source": source})
    return res


_COLS = {
    "well": ["uwi", "well_name", "operator_name", "field_name", "county", "province_state",
             "well_status", "spud_date", "completion_date", "final_td", "kb_elevation",
             "elevation_ouom", "source", "inventory_id"],
    "formation": ["uwi", "strat_name_set", "strat_unit_id", "strat_unit_name", "interp_id",
                  "interpreter_ba_id", "interp_date", "top_depth", "base_depth", "depth_ouom",
                  "source", "inventory_id"],
    "casing": ["UWI", "CASING_ID", "CASING_TYPE", "STRING_NUM", "OD_IN", "WEIGHT_LB_FT", "GRADE",
               "BASE_DEPTH", "SOURCE", "inventory_id"],
    "stim": ["UWI", "COMPLETION_ID", "STIM_ID", "STAGE_NUM", "STIM_TYPE", "STAGE_TOP_DEPTH",
             "STAGE_BASE_DEPTH", "FLUID_VOLUME_BBL", "PROPPANT_MASS_LBS", "MAX_TREATING_PRESSURE_PSI",
             "MAX_RATE_BPM", "SOURCE", "inventory_id"],
    "dst": ["UWI", "DST_ID", "DST_NUM", "TEST_TYPE", "TEST_DATE", "SOURCE", "inventory_id"],
    "dst_period": ["UWI", "DST_ID", "PERIOD_ID", "PERIOD_TYPE", "PERIOD_SEQ", "DURATION_MIN",
                   "CHOKE_SIZE", "AVG_OIL_RATE", "AVG_GAS_RATE", "AVG_WATER_RATE", "SOURCE", "inventory_id"],
    "pressure": ["UWI", "PRESSURE_ID", "PRESSURE_TYPE", "TEST_DATE", "DEPTH", "PRESSURE", "FLUID_TYPE",
                 "MOBILITY", "STRAT_UNIT_NAME", "TOOL_TYPE", "SOURCE", "inventory_id"],
    "petro_interp": ["UWI", "INTERP_ID", "INTERP_NAME", "INTERP_DATE", "SOURCE", "inventory_id"],
    "petro_zone": ["UWI", "INTERP_ID", "ZONE_ID", "ZONE_NAME", "TOP_DEPTH", "BASE_DEPTH",
                   "GROSS_THICKNESS", "NET_THICKNESS", "NET_TO_GROSS", "PHI_EFFECTIVE_AVG", "SW_AVG", "SOURCE", "inventory_id"],
    "srvy_hdr": ["uwi", "survey_id", "source", "inventory_id"],
    "srvy_sta": ["uwi", "survey_id", "station_id", "md", "incl", "azim", "tvd",
                 "depth_ouom", "source", "inventory_id"],
}
_FILE = {k: f"pdf_{k}.csv" for k in _COLS}
# kind → (target table, staging suffix)
TARGET = {
    "well": "DV_WELL", "formation": "DV_WELL_FORMATION_TOP", "casing": "DV_WELL_CASING",
    "stim": "DV_WELL_STIMULATION", "dst": "DV_WELL_DST", "dst_period": "DV_WELL_DST_PERIOD",
    "pressure": "DV_WELL_PRESSURE", "petro_interp": "DV_WELL_PETRO_INTERP",
    "petro_zone": "DV_WELL_PETRO_ZONE", "srvy_hdr": "DV_WELL_DIR_SRVY_HDR", "srvy_sta": "DV_WELL_DIR_SRVY_STA",
}


def write_staging_csvs(directory, out_dir=None, source="PDF", files=None):
    out_dir = out_dir or directory
    os.makedirs(out_dir, exist_ok=True)
    paths = files if files is not None else sorted(
        glob.glob(os.path.join(directory, "*.pdf")) + glob.glob(os.path.join(directory, "*.PDF")))
    agg = {k: [] for k in _COLS}
    for p in paths:
        r = extract_file(p, source)
        # ONE id for the whole document, stamped on every row of every kind. Done here
        # rather than in extract_file's ~11 row-append sites: one place to be right, and a
        # new document kind cannot forget it.
        iid = inventory_id(os.path.abspath(p))
        for k in _COLS:
            for row in r.get(k, []):
                row["inventory_id"] = iid
            agg[k].extend(r.get(k, []))
    written = {}
    for k, rows in agg.items():
        if not rows:
            continue
        path = os.path.join(out_dir, _FILE[k])
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_COLS[k]); w.writeheader(); w.writerows(rows)
        written[k] = (path, len(rows))
    return written


# ── OCR fallback for image-only PDFs ────────────────────────────────────────────
# Rasterize each page, OCR to words, rebuild (text, tables) in pdfplumber's shape so the
# extractor body above runs unchanged. text carries synthesized "Label: value" lines for the
# well header (so _header maps dv_well); tables are segmented sub-tables (column header first,
# so rows_of()/_find_col() work). Needs pypdfium2 + pytesseract + the tesseract binary.
# section titles are detected by distinctive multi-word phrases (robust to how tesseract
# renders the em-dash, which varies page to page) rather than by the dash itself.
_OCR_SECTIONS = ("well header", "formation tops", "log analysis", "directional survey",
                 "drill stem", "core runs", "core sample", "core photograph",
                 "completion summary", "frac stage")


def _ocr_words(path, scale=3, conf=25):
    import pypdfium2 as pdfium, pytesseract
    from pytesseract import Output
    pdf = pdfium.PdfDocument(path)
    pages = []
    for pg in pdf:
        img = pg.render(scale=scale).to_pil().convert("L")
        img = img.point(lambda p: 0 if p < 205 else 255)   # darken light-gray section titles/headers
        d = pytesseract.image_to_data(img, output_type=Output.DICT)
        ws = []
        for i in range(len(d["text"])):
            t = (d["text"][i] or "").strip()
            try:
                c = int(d["conf"][i])
            except (ValueError, TypeError):
                c = -1
            if t and c >= conf:
                ws.append({"t": t, "x": d["left"][i], "y": d["top"][i],
                           "w": d["width"][i], "h": d["height"][i]})
        pages.append(ws)
    return pages


def _ocr_bands(ws, tol_frac=0.6):
    import statistics
    if not ws:
        return []
    h = statistics.median(w["h"] for w in ws)
    tol = h * tol_frac
    ws = sorted(ws, key=lambda w: w["y"])
    bands, cur, cy = [], [], None
    for w in ws:
        yc = w["y"] + w["h"] / 2
        if cy is None or abs(yc - cy) <= tol:
            cur.append(w)
            cy = sum(z["y"] + z["h"] / 2 for z in cur) / len(cur)
        else:
            bands.append(sorted(cur, key=lambda z: z["x"]))
            cur, cy = [w], yc
    if cur:
        bands.append(sorted(cur, key=lambda z: z["x"]))
    return bands


def _ocr_cells(band, gap_frac=1.4):
    import statistics
    if not band:
        return []
    h = statistics.median(w["h"] for w in band)
    gap = h * gap_frac
    cells, cur = [], [band[0]]
    for prev, w in zip(band, band[1:]):
        if w["x"] - (prev["x"] + prev["w"]) > gap:
            cells.append(cur)
            cur = [w]
        else:
            cur.append(w)
    cells.append(cur)
    return [{"t": " ".join(z["t"] for z in c), "x": c[0]["x"]} for c in cells]


def _ocr_reconstruct(path):
    all_bands = []
    for ws in _ocr_words(path):
        for b in _ocr_bands(ws):
            cells = _ocr_cells(b)
            if cells:
                all_bands.append(cells)
    text_lines = ["  ".join(c["t"] for c in b) for b in all_bands]
    tables, hdr_lines = [], []
    seg, seg_title = [], ""

    def flush():
        if not seg:
            return
        if "header" in seg_title.lower():
            # alternating label / value bands -> pair each label to the nearest value by x
            for lab_b, val_b in zip(seg[0::2], seg[1::2]):
                for lc in lab_b:
                    if not val_b:
                        continue
                    vc = min(val_b, key=lambda z: abs(z["x"] - lc["x"]))
                    if abs(vc["x"] - lc["x"]) < 250:
                        hdr_lines.append(f'{lc["t"]}: {vc["t"]}')
        elif len(seg) >= 2:
            head = seg[0]
            xcen = [c["x"] for c in head]
            grid = [[c["t"] for c in head]]
            for b in seg[1:]:
                rowc = [""] * len(head)
                for c in b:
                    j = min(range(len(head)), key=lambda k: abs(xcen[k] - c["x"]))
                    rowc[j] = (rowc[j] + " " + c["t"]).strip() if rowc[j] else c["t"]
                grid.append(rowc)
            tables.append(grid)

    for b in all_bands:
        joined = " ".join(c["t"] for c in b)
        is_title = any(s in joined.lower() for s in _OCR_SECTIONS)
        if is_title:
            flush()
            seg, seg_title = [], joined
        else:
            seg.append(b)
    flush()
    text = "\n".join(hdr_lines + [""] + text_lines)
    return text, tables


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    for kind, (p, n) in write_staging_csvs(d).items():
        print(f"{kind:14} {n:3} rows -> {TARGET.get(kind,'?'):26} {os.path.basename(p)}")
