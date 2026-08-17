r"""
page_dir_loader.py — Directory Loader (stages 1-2): point at a folder of CSVs, get an
editable load plan. Deterministic, catalog-guided, writes NOTHING yet.

Wire into the app:  page_dir_loader.run(engine)   (aliases render/main/show/app)

Stage 1  pick   — choose directory + catalog, Scan
Stage 2  review — file→table plan (editable), load order, Match-and-Map worklist

Later stages (columns / FK resolve / load) hang off the same session_state spine.
The matching logic here is the twin of load_preflight.py — consolidate into one
shared module when the later stages land.
"""
import os, json, glob, csv, hashlib
from collections import defaultdict
import streamlit as st

# Canonical entity id — MUST match dataview.core.hash_keys.entity_id
# (UPPER+strip -> utf-16-le -> SHA1 -> uppercase hex). Import the real one if present;
# the inline fallback is byte-identical so ids never diverge from what's in the DB.
try:
    from dataview.core.hash_keys import entity_id
except Exception:
    def entity_id(name):
        if name is None: return None
        n = str(name).upper().strip()
        if not n: return None
        return hashlib.sha1(n.encode("utf-16-le")).hexdigest().upper()

# name-based entity FKs we resolve via SHA1 seed (child_col -> (parent_table, name_col))
_ENTITY_FKS = {
    "OPERATOR":          ("DV_BUSINESS_ASSOCIATE", "ba_name"),
    "CURRENT_OPERATOR":  ("DV_BUSINESS_ASSOCIATE", "ba_name"),
    "ORIGINAL_OPERATOR": ("DV_BUSINESS_ASSOCIATE", "ba_name"),
    "LICENSEE":          ("DV_BUSINESS_ASSOCIATE", "ba_name"),
    "FIELD_NAME":        ("DV_FIELD",              "field_name"),
    "FIELD":             ("DV_FIELD",              "field_name"),
}

# ───────────────────────── deterministic core (no Streamlit) ─────────────────────────
def _norm(c): return c.strip().upper().replace(" ", "_")
def _bare(t): return t.split(".")[-1].upper()

def load_catalog(path):
    """Accept rich {fk_constraints,table_cols,table_kind} or plain {table:[fks]}."""
    cat = json.load(open(path, encoding="utf-8"))
    if "fk_constraints" in cat:
        FKC = {_bare(t): [{"child_cols": [c.upper() for c in fk["child_cols"]],
                           "parent_table": _bare(fk["parent_table"])}
                          for fk in fks] for t, fks in cat["fk_constraints"].items()}
        COLS = {_bare(t): {c.upper() for c in cols} for t, cols in cat["table_cols"].items()}
        KIND = {_bare(t): k for t, k in cat.get("table_kind", {}).items()}
        return FKC, COLS, KIND, "rich"
    FKC = defaultdict(list); tables = set()
    for t, fks in cat.items():
        bt = _bare(t); tables.add(bt)
        for fk in fks:
            pt = _bare(fk["ref_table"]); tables.add(pt)
            FKC[bt].append({"child_cols": [c[0].upper() for c in fk["cols"]], "parent_table": pt})
    return dict(FKC), {}, {t: "entity" for t in tables}, "plain"

def is_data_table(t):
    tl = t.lower()
    return tl.startswith("dv_") and not tl.startswith("dv_r_") \
        and tl not in ("dv_global_file_catalog", "dv_wl_file_catalog")
def is_ref_table(t): return t.lower().startswith("dv_r_")

def read_header(path):
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        row = next(csv.reader(fh), [])
    return {_norm(c) for c in row if c.strip() and not c.lower().startswith("unnamed")}

def _hint(fname, table):
    f = fname[:-4].upper().replace("DIR_SURVEY", "DIR_SRVY"); t = table.replace("DV_", "")
    h = 0.15 if (t in f or f.replace("WELL_", "") in t) else 0.0
    h += {("well_picks.csv", "DV_WELL_FORMATION_TOP"): 0.25,
          ("well_dir_survey_hdr.csv", "DV_WELL_DIR_SRVY_HDR"): 0.30,
          ("well_dir_survey_data.csv", "DV_WELL_DIR_SRVY_STA"): 0.30}.get((fname, table), 0.0)
    return h

def _best(cols, fname, candidates):
    if not candidates: return (0.0, 0.0, None)
    ranked = []
    for t, tset in candidates.items():
        ov = len(cols & tset) / max(1, len(cols))
        ranked.append((ov + _hint(fname, t), ov, t))
    ranked.sort(reverse=True)
    return ranked[0]

# generic tokens that must NOT drive a reference match (appear in many table names)
_REF_STOPWORDS = {"ppdm","dv","r","","well","data","hdr","header","dir","survey",
                  "log","core","tbl","table","file","master"}

def _ref_filename_signal(fname, table):
    """Reference files rarely share column names with their dv_r_ table (UOM_ID vs
    UOM_CODE), so lean on a DISTINCTIVE filename token (uom, source, status, datum...)
    shared with the table name. Generic tokens like 'well' are excluded so they can't
    hijack real data files."""
    import re
    ftok = set(re.split(r"[_\W]+", fname[:-4].lower())) - _REF_STOPWORDS
    ttok = set(re.split(r"[_\W]+", table.lower())) - _REF_STOPWORDS
    return 0.55 if (ftok & ttok) else 0.0

XL_EXTS = (".xlsx", ".xlsm", ".xltx", ".xls")
SHEET_DIR = "_xl_sheets"          # sidecar CSVs land here, beside the workbooks


def explode_workbooks(directory, recursive=False):
    """Every sheet of every Excel workbook in `directory` → a sidecar CSV in
    <directory>\\_xl_sheets\\<workbook>__<sheet>.csv, so the rest of the loader
    (header read, sampling, BCP, mapping) treats sheets exactly like CSVs.

    Blank sheets are skipped. Re-scanning rewrites the sidecars, so edits to the
    workbook are picked up. Returns (csv_paths, notes)."""
    import re
    out, notes = [], []
    books = []
    for ext in XL_EXTS:
        if recursive:
            books += glob.glob(os.path.join(directory, "**", f"*{ext}"), recursive=True)
            books += glob.glob(os.path.join(directory, "**", f"*{ext.upper()}"), recursive=True)
        else:
            books += glob.glob(os.path.join(directory, f"*{ext}"))
            books += glob.glob(os.path.join(directory, f"*{ext.upper()}"))
    books = sorted({b for b in books
                    if not os.path.basename(b).startswith("~$")
                    and SHEET_DIR not in os.path.normpath(b).split(os.sep)})
    if not books:
        return out, notes
    import pandas as pd
    dest = os.path.join(directory, SHEET_DIR)
    os.makedirs(dest, exist_ok=True)
    for b in books:
        stem = os.path.splitext(os.path.basename(b))[0]
        try:
            book = pd.read_excel(b, sheet_name=None, dtype=str)     # all sheets
        except Exception as e:
            notes.append(f"{os.path.basename(b)}: unreadable ({e})")
            continue
        for sheet, df in book.items():
            df = df.dropna(how="all").dropna(axis=1, how="all")
            if df.empty or not len(df.columns):
                notes.append(f"{os.path.basename(b)}[{sheet}]: empty — skipped")
                continue
            safe = re.sub(r"[^A-Za-z0-9]+", "_", str(sheet)).strip("_") or "sheet"
            p = os.path.join(dest, f"{stem}__{safe}.csv")
            try:
                df.to_csv(p, index=False, encoding="utf-8")
                out.append(p)
                notes.append(f"{os.path.basename(b)}[{sheet}] → {os.path.basename(p)} "
                             f"({len(df)} rows)")
            except OSError as e:
                notes.append(f"{os.path.basename(b)}[{sheet}]: write failed ({e})")
    return sorted(out), notes


def profile_directory(directory, catalog_path, recursive=False):
    """Pure function → the plan dict the UI renders. No Streamlit, no writes
    (other than exploding any Excel workbooks into sidecar CSVs)."""
    FKC, COLS, KIND, shape = load_catalog(catalog_path)
    DATA = {t: c for t, c in COLS.items() if is_data_table(t)}
    REF  = {t: c for t, c in COLS.items() if is_ref_table(t)}
    if recursive:
        files = sorted(p for p in glob.glob(os.path.join(directory, "**", "*.csv"), recursive=True)
                       if SHEET_DIR not in os.path.normpath(p).split(os.sep))
    else:
        files = sorted(glob.glob(os.path.join(directory, "*.csv")))
    xl_files, xl_notes = explode_workbooks(directory, recursive)   # Excel sheets → CSV, then treat alike
    files = sorted(files + xl_files)
    rows = []
    for path in files:
        f = os.path.basename(path); cols = read_header(path)
        d_adj, d_ov, d_t = _best(cols, f, DATA)
        # reference match = column overlap PLUS a filename token signal
        r_ranked = sorted(((_best(cols, f, {t: c})[1] + _ref_filename_signal(f, t), t)
                           for t, c in REF.items()), reverse=True) if REF else [(0, None)]
        r_ov, r_t = r_ranked[0]
        # a real data-table match ALWAYS wins over a filename-only reference guess
        if r_ov >= 0.5 and d_ov < 0.34 and r_ov > d_ov:
            rows.append(dict(file=f, path=path, table=r_t, score=r_ov, kind="reference", cols=sorted(cols)))
        elif d_ov == 0:
            rows.append(dict(file=f, path=path, table=None, score=0.0, kind="unmatched", cols=sorted(cols)))
        else:
            rows.append(dict(file=f, path=path, table=d_t, score=d_ov, kind="data", cols=sorted(cols)))
    matched = {r["table"] for r in rows if r["table"]}
    ref_tables  = {r["table"] for r in rows if r["table"] and r["kind"] == "reference"}
    data_matched = matched - ref_tables
    # topological load order over the DATA tables (FK graph)
    dep = defaultdict(set)
    for t in data_matched:
        for fk in FKC.get(t, []):
            if fk["parent_table"] in data_matched and fk["parent_table"] != t:
                dep[t].add(fk["parent_table"])
    data_order, seen = [], set()
    while len(seen) < len(data_matched):
        prog = False
        for t in sorted(data_matched):
            if t not in seen and dep[t] <= seen:
                data_order.append(t); seen.add(t); prog = True
        if not prog:
            data_order += [t for t in sorted(data_matched) if t not in seen]; break
    # reference tables ALWAYS load first (they satisfy the data files' FKs)
    order = sorted(ref_tables) + data_order
    # Match-and-Map worklist: declared parents not satisfied by the drop
    need = defaultdict(list)
    for t in matched:
        for fk in FKC.get(t, []):
            if fk["parent_table"] not in matched:      # matched now includes ref tables in the drop
                need[fk["parent_table"]].append((t, "+".join(fk["child_cols"])))
    worklist = [dict(parent=p, kind=KIND.get(p, "?"), blocks=need[p]) for p in sorted(need)]
    data_tables = sorted([t for t in COLS if is_data_table(t)]) or []
    all_ref_tables = sorted([t for t in COLS if is_ref_table(t)]) or []
    return dict(rows=rows, order=order, worklist=worklist, shape=shape,
                data_tables=data_tables, all_ref_tables=all_ref_tables,
                n_files=len(files), ref_tables=sorted(ref_tables), xl_notes=xl_notes)



# ─────────────────── FK resolution (stage 4) — pure logic ───────────────────
def distinct_values(csv_path, column):
    """Distinct non-blank values of one column in a CSV (order-stable)."""
    seen, out = set(), []
    try:
        import csv as _csv
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
            rd = _csv.DictReader(fh)
            norm = {(_norm(c)): c for c in (rd.fieldnames or [])}
            src = norm.get(column)
            if not src: return []
            for row in rd:
                v = (row.get(src) or "").strip()
                if v and v not in seen:
                    seen.add(v); out.append(v)
    except OSError:
        return []
    return sorted(out)

def resolve_fk_plan(values, actions, existing_name_to_id):
    """
    values: list[str] distinct incoming values
    actions: {value: {"add":bool, "remap_to":str|None}}   remap_to is an EXISTING name or "— skip —"
    existing_name_to_id: {existing_name: id} from the parent table
    Returns per-value resolution:  {value: {"action","id","seed_name"}}
    Precedence (per your rules): REMAP wins -> ADD -> else SKIP(null).
    """
    plan = {}
    for v in values:
        a = actions.get(v, {})
        remap = a.get("remap_to")
        if remap and remap not in ("— skip —", "(keep / add)", None, ""):
            # remap: use the EXISTING row's id (fold the variant into the canonical value)
            plan[v] = {"action": "remap", "id": existing_name_to_id.get(remap), "seed_name": None}
        elif a.get("add"):
            plan[v] = {"action": "add", "id": entity_id(v), "seed_name": v}   # SHA1 + seed parent
        else:
            plan[v] = {"action": "skip", "id": None, "seed_name": None}       # explicit null
    return plan


def resolve_ref_plan(values, actions, existing_keys):
    """Reference/parent FK resolution — the value IS the parent key (a code).
    values: distinct incoming child values
    actions: {value: {"add":bool, "remap_to":str|None}}   remap_to is an existing key
    existing_keys: set of keys already in the parent
    Returns {value: {"action","key","seed"}}
      remap   -> fold onto an existing key   (key=remap_to, seed=None)   [wins]
      present -> already in parent            (key=value,    seed=None)
      add     -> seed value as a new key       (key=value,    seed=value)
      skip    -> unresolved & missing          (key=value,    seed=None)  -> FK violation
    """
    plan = {}
    for v in values:
        a = actions.get(v, {})
        remap = a.get("remap_to")
        if remap and remap not in ("— skip —", None, ""):
            plan[v] = {"action": "remap", "key": remap, "seed": None}
        elif v in existing_keys:
            plan[v] = {"action": "present", "key": v, "seed": None}
        elif a.get("add"):
            plan[v] = {"action": "add", "key": v, "seed": v}
        else:
            plan[v] = {"action": "skip", "key": v, "seed": None}
    return plan



# ─────────────── Validation & Normalization (stage 3.5) — pure logic ───────────────
import re as _re
_ISO_RE   = _re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_SLASH_RE = _re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$")
_LAT = {"LAT", "LATITUDE", "SURFACE_LATITUDE", "SURF_LAT"}
_LON = {"LON", "LONG", "LONGITUDE", "SURFACE_LONGITUDE", "SURF_LON", "LNG"}

def _date_parts(v):
    v = v.strip()
    m = _ISO_RE.match(v)
    if m: return ("iso", int(m.group(1)), int(m.group(2)), int(m.group(3)))   # y, mo, da
    m = _SLASH_RE.match(v)
    if m: return ("slash", int(m.group(3)), int(m.group(1)), int(m.group(2))) # y, a, b
    return None

def detect_date_format(values):
    """Return iso | us | intl | ambiguous | mixed | None (not dates)."""
    vals = [v for v in values if v and v.strip()]
    if not vals: return None
    iso = slash = other = 0
    a_gt12 = b_gt12 = False
    for v in vals:
        p = _date_parts(v)
        if not p: other += 1; continue
        if p[0] == "iso": iso += 1
        else:
            slash += 1
            _, _, a, b = p
            if a > 12: a_gt12 = True
            if b > 12: b_gt12 = True
    if iso + slash == 0: return None
    if slash == 0: return "iso"
    if a_gt12 and b_gt12: return "mixed"
    if b_gt12 and not a_gt12: return "us"      # 2nd field is the day -> MM-DD-YYYY
    if a_gt12 and not b_gt12: return "intl"    # 1st field is the day -> DD-MM-YYYY
    return "ambiguous"                         # all <=12, can't tell

def to_iso(v, fmt):
    p = _date_parts(v)
    if not p: return None
    if p[0] == "iso":
        _, y, mo, da = p
    else:
        _, y, a, b = p
        if fmt == "us":   mo, da = a, b
        elif fmt == "intl": mo, da = b, a
        else: return None
    if not (1 <= mo <= 12 and 1 <= da <= 31): return None
    return f"{y:04d}-{mo:02d}-{da:02d}"

def _is_numlike(v):
    s = v.strip().replace(",", "")
    if s == "": return False
    try: float(s); return True
    except ValueError: return False

def _read_column_values(csv_path):
    import csv as _csv
    out = {}
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        rd = _csv.DictReader(fh)
        cols = [c for c in (rd.fieldnames or []) if c and not c.lower().startswith("unnamed")]
        for c in cols: out[c] = []
        for row in rd:
            for c in cols: out[c].append((row.get(c) or ""))
    return out

def validate_file(csv_path):
    """Per-column report: dtype, issue, proposed action, sample. Pure — no writes."""
    colvals = _read_column_values(csv_path)
    report = []
    for col, vals in colvals.items():
        nonblank = [v for v in vals if v and v.strip()]
        n_blank = len(vals) - len(nonblank)
        up = col.upper()
        rec = {"column": col, "n": len(vals), "blank": n_blank,
               "dtype": "text", "level": "ok", "issue": "", "action": "", "sample": ""}
        if not nonblank:
            rec.update(dtype="empty", level="warn", issue="all blank", action="none")
            report.append(rec); continue

        # DATE columns (by name or content)
        looks_date = "DATE" in up or (detect_date_format(nonblank) in ("iso","us","intl","ambiguous","mixed"))
        if looks_date:
            fmt = detect_date_format(nonblank)
            rec["dtype"] = "date"
            if fmt == "iso":
                rec.update(level="ok", issue="ISO", action="none", sample=nonblank[0])
            elif fmt in ("us", "intl"):
                bad = [v for v in nonblank if to_iso(v, fmt) is None]
                ex = nonblank[0]
                rec.update(level="fix", action=f"normalize {fmt.upper()}->ISO",
                           issue=f"{fmt.upper()} dates" + (f", {len(bad)} unparseable" if bad else ""),
                           sample=f"{ex} -> {to_iso(ex, fmt)}")
            elif fmt in ("ambiguous", "mixed"):
                rec.update(level="flag",
                           issue=("ambiguous day/month (all ≤12)" if fmt=="ambiguous"
                                  else "MIXED / contradictory date order"),
                           action="CONFIRM date convention", sample=nonblank[0])
            report.append(rec); continue

        # COORDINATES
        if up in _LAT or up in _LON:
            lo, hi = (-90, 90) if up in _LAT else (-180, 180)
            oor, nonnum = 0, 0
            for v in nonblank:
                if not _is_numlike(v): nonnum += 1; continue
                x = float(v.replace(",", ""))
                if not (lo <= x <= hi): oor += 1
            rec["dtype"] = "coord"
            if nonnum or oor:
                rec.update(level="flag",
                           issue=f"{nonnum} non-numeric, {oor} out of [{lo},{hi}]",
                           action="review before load", sample=nonblank[0])
            else:
                rec.update(level="ok", issue=f"in range", action="none", sample=nonblank[0])
            report.append(rec); continue

        # NUMERIC (mostly num-like)
        numlike = sum(1 for v in nonblank if _is_numlike(v))
        if numlike >= 0.8 * len(nonblank):
            bad = [v for v in nonblank if not _is_numlike(v)]
            rec["dtype"] = "numeric"
            needs_strip = any(("," in v or v != v.strip()) for v in nonblank)
            if bad:
                rec.update(level="flag", issue=f"{len(bad)} non-numeric value(s)",
                           action="review", sample=bad[0])
            elif needs_strip:
                rec.update(level="fix", issue="commas/whitespace", action="strip -> number",
                           sample=f"{nonblank[0]!r}")
            else:
                rec.update(level="ok", issue="numeric", action="none", sample=nonblank[0])
            report.append(rec); continue

        # CATEGORICAL — detect near-duplicate variants (SHUT IN / SHUT_IN)
        distinct = sorted(set(v.strip() for v in nonblank))
        if len(distinct) <= 25:
            canon = {}
            for v in distinct:
                k = v.upper().replace(" ", "_").strip("_")
                canon.setdefault(k, []).append(v)
            variants = {k: vs for k, vs in canon.items() if len(vs) > 1}
            rec["dtype"] = "categorical"
            if variants:
                ex = next(iter(variants.values()))
                rec.update(level="fix", issue=f"{len(variants)} value(s) with variants",
                           action="collapse variants",
                           sample=" / ".join(ex[:3]) + " -> " + list(variants)[0])
            else:
                rec.update(level="ok", issue=f"{len(distinct)} distinct", action="none",
                           sample=distinct[0])
            report.append(rec); continue

        rec.update(sample=nonblank[0], issue=f"{len(set(nonblank))} distinct")
        report.append(rec)
    return report





# ─────────────── Column mapping (stage 3) — fingerprint + suggestion ───────────────
# Provenance / plumbing columns that the loader stamps itself and that drift
# in and out of source files. They must NOT change the column-shape
# fingerprint, or the same logical file keys two different fingerprints and
# the saved mapping stops auto-applying (grid re-prompts for confirmation).
_FP_IGNORE = {"INVENTORY_ID", "SOURCE", "SOURCE_PATH",
              "ROW_CREATED_BY", "ROW_CREATED_DATE",
              "ROW_CHANGED_BY", "ROW_CHANGED_DATE",
              "ACTIVE_IND", "PPDM_GUID", "ROW_QUALITY"}

def fingerprint_cols(cols):
    """Stable hash of a file's column SHAPE. Normalized (strip/upper/_) and
    sorted, so case/order/whitespace never matter; provenance columns in
    _FP_IGNORE are dropped so their presence or absence does not fork the
    fingerprint (that was making saved mappings re-prompt)."""
    import hashlib
    sig = ",".join(sorted(n for c in cols
                          for n in (_norm(c),) if n not in _FP_IGNORE))
    return hashlib.sha1(sig.encode("utf-8")).hexdigest().upper()[:16]

def _fk_of(table, db_col, FKC):
    """(parent_table, kind) for a db column that is a single-col FK, else None.
    kind: 'entity' (BA/field, SHA1-resolved) | 'reference' (dv_r_, code-seeded) | 'parent'."""
    for fk in FKC.get(table.upper(), []):
        if len(fk["child_cols"]) == 1 and fk["child_cols"][0].upper() == db_col.upper():
            p = fk["parent_table"]
            if p in ("DV_BUSINESS_ASSOCIATE", "DV_FIELD"): return (p, "entity")
            if is_ref_table(p): return (p, "reference")
            return (p, "parent")
    return None

def suggest_colmap(csv_cols, table, COLS, FKC, syn=None):
    """Propose {csv_col: db_col_or_'— skip —'}. Order: exact normalized match; then a
    learned synonym (human-confirmed history, keyed by table+source col); then a token
    match to an ENTITY fk column (OPERATOR->operator_ba_id); else skip."""
    tcols = {c.upper(): c.lower() for c in COLS.get(table.upper(), set())}
    valid = set(tcols.values())
    fk_entity_cols = [c for c in tcols if _fk_of(table, c, FKC) and _fk_of(table, c, FKC)[1] == "entity"]
    syn = syn or {}
    out = {}
    for c in csv_cols:
        cu = _norm(c)
        if cu in tcols:                       # exact
            out[c] = tcols[cu]; continue
        if cu in syn and str(syn[cu]).lower() in valid:   # learned synonym
            out[c] = str(syn[cu]).lower(); continue
        # token match to an entity FK col (only entity FKs get this fuzzy help).
        # Rank by most shared tokens, then FEWEST extra tokens so plain OPERATOR
        # prefers operator_ba_id over original_/current_operator_ba_id; name last
        # for determinism.
        ctok = _tok(cu)
        cand = [(len(_tok(fc) & ctok), len(_tok(fc) - ctok), fc)
                for fc in fk_entity_cols if _tok(fc) & ctok]
        if cand:
            cand.sort(key=lambda t: (-t[0], t[1], t[2]))
            out[c] = cand[0][2].lower()
        else:
            out[c] = "— skip —"
    return out

# ─────────────────── Seed + Load bundle (stage 5) — pure assembly ───────────────────
def _tok(x): return set(_re.split(r"[_\W]+", x.lower())) - {"", "id", "dv", "ba"}

def _map_entity_fk_target(csv_col, target_table, FKC):
    """CSV entity column (OPERATOR) -> target FK child col (operator_ba_id) using the
    catalog's declared FKs + token overlap. Returns (fk_col, parent_table) or None."""
    if csv_col not in _ENTITY_FKS: return None
    parent = _ENTITY_FKS[csv_col][0]
    cands = [fk["child_cols"][0] for fk in FKC.get(target_table, [])
             if fk["parent_table"] == parent and len(fk["child_cols"]) == 1]
    if not cands: return None
    ctok = _tok(csv_col)
    best = max(cands, key=lambda c: len(_tok(c) & ctok))
    return (best, parent)

def build_load_bundle(directory, catalog_path, plan, overrides, fk_actions, colmaps):
    """Assemble seeds + per-file load from CONFIRMED column maps. Pure — no writes."""
    FKC, COLS, KIND, shape = load_catalog(catalog_path)
    files = []
    entity_seeds = defaultdict(dict)
    ref_seeds    = defaultdict(set)
    for r in plan["rows"]:
        table = (overrides.get(r["file"], r["table"]) or "").upper()
        if not table:
            continue
        cmap = colmaps.get(r["file"], {})
        direct, fkcols, refcols, norm = {}, {}, {}, {}
        for csv_col, db_col in cmap.items():
            fk = _fk_of(table, db_col, FKC)
            if fk and fk[1] == "entity":
                vals = distinct_values(r["path"], csv_col)
                rp = resolve_fk_plan(vals,
                       {v: fk_actions.get((r["file"], csv_col.upper(), v), {"add": True, "remap_to": None}) for v in vals},
                       {})
                fkcols[db_col] = {"source": csv_col, "parent": fk[0], "resolution": rp}
                for v, p in rp.items():
                    if p["action"] == "add" and p["id"]:
                        entity_seeds[fk[0]][v] = p["id"]
            elif fk and fk[1] == "reference":
                refcols[db_col] = csv_col
                for v in distinct_values(r["path"], csv_col):
                    if v.strip():
                        ref_seeds[fk[0]].add(v.strip())
            else:
                direct[csv_col] = db_col
        for v in validate_file(r["path"]):
            if v["level"] == "fix" and v["column"] in cmap:
                norm[v["column"]] = v["action"]
        try:
            with open(r["path"], encoding="utf-8", errors="replace") as fh:
                nrows = max(0, sum(1 for _ in fh) - 1)
        except OSError:
            nrows = 0
        files.append(dict(file=r["file"], table=table, direct=direct, fkcols=fkcols,
                          refcols=refcols, norm=norm, rows=nrows,
                          mapped=len(cmap), total=len(r["cols"])))
    pos = {t: i for i, t in enumerate(plan["order"])}
    files.sort(key=lambda f: pos.get(f["table"], 999))
    return dict(files=files, entity_seeds={k: dict(v) for k, v in entity_seeds.items()},
                ref_seeds={k: sorted(v) for k, v in ref_seeds.items()}, shape=shape)


def fk_coverage(bundle, catalog_path, matched_tables):
    """Per-file FK checklist: every declared FK -> satisfied / null-unmapped."""
    FKC, COLS, KIND, _ = load_catalog(catalog_path)
    rows = []
    for f in bundle["files"]:
        handled = ({d.lower() for d in f["direct"].values()}
                   | {k.lower() for k in f["fkcols"]}
                   | {k.lower() for k in f["refcols"]})
        for fk in FKC.get(f["table"], []):
            child = [c.lower() for c in fk["child_cols"]]; parent = fk["parent_table"]
            if parent in matched_tables:
                lvl, note = "ok", "parent in drop (load-order)"
            elif all(c in handled for c in child):
                lvl, note = "ok", ("ref seeded" if is_ref_table(parent) else "mapped/resolved")
            else:
                miss = [c for c in child if c not in handled]
                lvl, note = "null", "no source -> NULL (" + "+".join(miss) + ")"
            rows.append(dict(file=f["file"], table=f["table"],
                             fk="+".join(fk["child_cols"]), parent=parent, level=lvl, note=note))
    return rows


# ───────────────────────────────── Streamlit UI ─────────────────────────────────
def _badge(score, kind):
    if kind == "reference": return f"📘 ref {score*100:.0f}%"
    if kind == "unmatched" or score == 0: return "🔴 no match"
    if score >= 0.85: return f"🟢 {score*100:.0f}%"
    if score >= 0.50: return f"🟡 {score*100:.0f}% confirm"
    return f"🔴 {score*100:.0f}% low"

def _default_catalog():
    # dataview\schema_registry\ first (module-anchored, launch-independent — matches
    # fk_catalog.py / page_pipeline.py), then legacy cwd-relative paths as fallbacks.
    _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # ...\dataview
    _canon = os.path.join(_here, "schema_registry", "dataview_fk_catalog.json")
    for c in (_canon,
              "schemas/dataview_fk_catalog.json",
              "schema_registry/dataview_fk_catalog.json",
              "dataview_fk_catalog.json"):
        if os.path.exists(c): return c
    return _canon

def _pick(ss):
    st.subheader("① Choose a directory of CSVs / Excel workbooks")
    ss.dl_dir = st.text_input("Directory", ss.get("dl_dir", ""),
                              placeholder=r"C:\...\well_picks")
    ss.dl_recursive = st.checkbox("Include subdirectories (recursive scan)",
                                  value=ss.get("dl_recursive", False))
    ss.dl_cat = st.text_input("FK catalog (dataview_fk_catalog.json)",
                              ss.get("dl_cat", _default_catalog()))
    if st.button("🔍 Scan", type="primary", use_container_width=True):
        if not os.path.isdir(ss.dl_dir):
            st.error("That directory doesn't exist."); return
        if not os.path.exists(ss.dl_cat):
            st.error("Catalog file not found."); return
        try:
            ss.dl_plan = profile_directory(ss.dl_dir, ss.dl_cat, ss.dl_recursive)
        except Exception as e:
            st.error(f"Scan failed: {e}"); return
        ss.dl_overrides = {}
        ss.dl_stage = "review"; st.rerun()

def _review(ss):
    plan = ss.dl_plan
    st.subheader("② Review the load plan")
    if plan["shape"] != "rich":
        st.warning("Plain catalog (no column lists) — file→table matching is limited. "
                   "Use the rich dataview_fk_catalog.json for full matching.")
    st.caption(f"{plan['n_files']} table file(s) · confirm or correct each file's target table below.")
    _xl = plan.get("xl_notes") or []
    if _xl:
        with st.expander(f"📗 {len(_xl)} Excel sheet(s) expanded to CSV"):
            st.caption("Each workbook sheet is written to `_xl_sheets\\` and loaded like a CSV. "
                       "Re-scan after editing a workbook to refresh them.")
            for n in _xl:
                st.markdown(f"- {n}")

    import pandas as pd
    grid = pd.DataFrame([{"File": r["file"],
                          "→ Table": r["table"] or "(pick a table)",
                          "Match": _badge(r["score"], r["kind"]),
                          "Kind": r["kind"]} for r in plan["rows"]])
    ref_opts  = [t for t in plan.get("all_ref_tables", [])]
    options = (["(pick a table)", "(skip / reference)"]
               + plan["data_tables"] + ref_opts)
    edited = st.data_editor(
        grid, hide_index=True, use_container_width=True, key="dl_grid",
        column_config={
            "File":   st.column_config.TextColumn(disabled=True),
            "→ Table": st.column_config.SelectboxColumn(options=options, required=False),
            "Match":  st.column_config.TextColumn(disabled=True, help="🟢 trust · 🟡 confirm · 🔴 fix · 📘 reference"),
            "Kind":   st.column_config.TextColumn(disabled=True)})
    # capture overrides
    for i, r in enumerate(plan["rows"]):
        chosen = edited.iloc[i]["→ Table"]
        if chosen not in ("(pick a table)", "(skip / reference)"):
            ss.dl_overrides[r["file"]] = chosen

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Load order** (parents → children)")
        if plan["order"]:
            refset = set(plan.get("ref_tables", []))
            for i, t in enumerate(plan["order"], 1):
                tag = "📘 ref " if t in refset else "      "
                st.text(f"{i}. {tag}{t}")
        else:
            st.caption("— nothing mapped yet —")
    with c2:
        st.markdown(f"**Match-and-Map worklist** ({len(plan['worklist'])} parent tables)")
        st.caption("Must be resolved/seeded before load — never silently nulled.")
        for w in plan["worklist"]:
            blk = ", ".join(f"{t}.{c}" for t, c in w["blocks"][:3])
            more = "" if len(w["blocks"]) <= 3 else f" +{len(w['blocks'])-3}"
            st.text(f"• {w['parent']}  ⟵ {blk}{more}")

    st.divider()
    b1, b2 = st.columns([1, 2])
    if b1.button("← Back"):
        ss.dl_stage = "pick"; st.rerun()
    if b2.button("Start loading →", type="primary", use_container_width=True):
        ss.dl_stage = "sequence"; ss.dl_seq_idx = 0; ss.dl_loaded = []; st.rerun()




def _mappable_files(plan, overrides):
    return [r for r in plan["rows"]
            if (overrides.get(r["file"], r["table"])) and r["kind"] != "unmatched"]

def _colmap_stage(ss):
    import pandas as pd
    plan = ss.dl_plan
    FKC, COLS, KIND, _ = load_catalog(ss.dl_cat)
    ss.setdefault("dl_colmaps", {})         # {file: {csv_col: db_col}}
    ss.setdefault("dl_fingerprints", {})    # {fp: {csv_col: db_col}}  (learned)
    files = _mappable_files(plan, ss.get("dl_overrides", {}))
    idx = ss.get("dl_colmap_idx", 0)
    if idx >= len(files):
        ss.dl_stage = "fks"; st.rerun(); return
    r = files[idx]
    table = ss.get("dl_overrides", {}).get(r["file"], r["table"]).upper()
    csv_cols = sorted(r["cols"])
    fp = fingerprint_cols(csv_cols)

    st.subheader(f"③ Map columns — file {idx+1} of {len(files)}")
    st.markdown(f"**{r['file']}**  →  `{table}`  ")
    seen = fp in ss.dl_fingerprints
    st.caption(("🔁 seen this column shape before — mapping pre-filled from memory. "
                if seen else "") +
               "Set each source column to its DB column (or — skip —). "
               "🔗 marks FK columns. Confirm to save + fingerprint.")

    # options = real DB columns for this table + skip
    db_cols = sorted({c.lower() for c in COLS.get(table, set())})
    options = ["— skip —"] + db_cols
    prior = ss.dl_fingerprints.get(fp) or ss.dl_colmaps.get(r["file"])
    suggestion = prior or suggest_colmap(csv_cols, table, COLS, FKC)

    # sample values (first non-blank) for context
    samples = {}
    for c in csv_cols:
        vals = distinct_values(r["path"], c)
        samples[c] = (vals[0] if vals else "")

    def _fk_tag(dbc):
        if dbc == "— skip —": return ""
        fk = _fk_of(table, dbc, FKC)
        return {"entity": "🔗 entity", "reference": "🔗 ref",
                "parent": "🔗 parent"}.get(fk[1], "") if fk else ""

    grid = pd.DataFrame([{
        "Source column": c,
        "Sample": samples[c][:28],
        "→ DB column": suggestion.get(c, "— skip —"),
        "FK": _fk_tag(suggestion.get(c, "— skip —")),
    } for c in csv_cols])
    edited = st.data_editor(
        grid, hide_index=True, use_container_width=True, key=f"cmap_{r['file']}",
        column_config={
            "Source column": st.column_config.TextColumn(disabled=True),
            "Sample":        st.column_config.TextColumn(disabled=True),
            "→ DB column": st.column_config.SelectboxColumn(options=options, required=True),
            "FK":            st.column_config.TextColumn(disabled=True, width="small"),
        })

    # build the map from edits
    cmap = {}
    for i, c in enumerate(csv_cols):
        dbc = edited.iloc[i]["→ DB column"]
        if dbc and dbc != "— skip —":
            cmap[c] = dbc

    # duplicate-target guard (two source cols -> same db col)
    dupes = [v for v in cmap.values() if list(cmap.values()).count(v) > 1]
    if dupes:
        st.error(f"⚠ Two source columns map to the same DB column: {sorted(set(dupes))}. Fix before confirming.")

    st.divider()
    b1, b2, b3 = st.columns([1, 1, 2])
    if b1.button("← Back"):
        ss.dl_stage = "validate"; st.rerun()
    if b2.button("Skip file"):
        ss.dl_colmap_idx = idx + 1; st.rerun()
    if b3.button("✅ Confirm & next →", type="primary", use_container_width=True,
                 disabled=bool(dupes)):
        ss.dl_colmaps[r["file"]] = cmap
        ss.dl_fingerprints[fp] = cmap          # learn this shape
        ss.dl_colmap_idx = idx + 1
        st.rerun()


def _validate_stage(ss):
    import pandas as pd
    plan = ss.dl_plan
    st.subheader("③ Validate & normalize")
    st.caption("Per-column check on each mapped file. 🟢 clean · 🟡 auto-fix "
               "(shown) · 🔴 flag (needs your eyes). Dry run — nothing written.")
    _LVL = {"ok": "🟢", "fix": "🟡", "warn": "🟡", "flag": "🔴"}
    total_fix = total_flag = 0
    for r in plan["rows"]:
        if not r["table"] or r["kind"] == "unmatched":
            continue
        rep = validate_file(r["path"])
        fixes = sum(1 for x in rep if x["level"] == "fix")
        flags = sum(1 for x in rep if x["level"] == "flag")
        total_fix += fixes; total_flag += flags
        # only surface columns that aren't plain-clean text, to keep it readable
        interesting = [x for x in rep if x["level"] != "ok" or x["dtype"] in ("date","coord","numeric","categorical")]
        with st.expander(f"{r['file']}  →  {r['table']}   "
                         f"(🟡 {fixes} fix · 🔴 {flags} flag)",
                         expanded=(flags > 0)):
            grid = pd.DataFrame([{
                "": _LVL.get(x["level"], ""),
                "Column": x["column"], "Type": x["dtype"],
                "Issue": x["issue"], "Action": x["action"], "Example": x["sample"],
            } for x in interesting])
            st.dataframe(grid, hide_index=True, use_container_width=True)
    st.divider()
    if total_flag:
        st.warning(f"🔴 {total_flag} column(s) flagged for review — e.g. ambiguous "
                   "dates or out-of-range coords. You can proceed, but flagged values load as-is "
                   "unless you fix them upstream.")
    else:
        st.success(f"No blocking issues. 🟡 {total_fix} column(s) will be auto-normalized on load.")
    c1, c2 = st.columns([1, 2])
    if c1.button("← Back to plan"):
        ss.dl_stage = "review"; st.rerun()
    if c2.button("Map columns →", type="primary", use_container_width=True):
        ss.dl_stage = "colmap"; ss.dl_colmap_idx = 0; st.rerun()


def _existing_names(engine, table, name_col, id_col):
    """{name: id} from the parent table. Empty if no engine or query fails."""
    if engine is None:
        return {}
    try:
        import pandas as pd
        df = pd.read_sql(f"SELECT {name_col}, {id_col} FROM dataview.{table.lower()}", engine)
        return {str(r[name_col]): str(r[id_col]) for _, r in df.iterrows()
                if str(r[name_col]).strip()}
    except Exception:
        return {}

def _fk_stage(ss, engine):
    import pandas as pd
    plan = ss.dl_plan
    st.subheader("③ Resolve FK values")
    st.caption("For each name-based FK column: ☑ Add seeds a new parent row "
               "(id = SHA1 of the name); or pick an existing value to **remap** a variant "
               "onto it; untouched rows are **skipped** (FK set NULL). Remap wins over Add.")

    # which data files carry entity-FK columns?
    fk_targets = []   # (file, path, column, parent_table, name_col)
    for r in plan["rows"]:
        if r["kind"] != "data" or not r["table"]:
            continue
        for col in r["cols"]:
            if col in _ENTITY_FKS:
                pt, ncol = _ENTITY_FKS[col]
                fk_targets.append((r["file"], r["path"], col, pt, ncol))

    if not fk_targets:
        st.info("No name-based FK columns (operator/field) found in the mapped data files.")
        if st.button("← Back to plan"):
            ss.dl_stage = "review"; st.rerun()
        return

    ss.setdefault("dl_fk_actions", {})   # {(file,col,value): {"add":bool,"remap_to":str}}
    id_col = {"DV_BUSINESS_ASSOCIATE": "ba_id", "DV_FIELD": "field_id"}

    for (fname, path, col, ptable, ncol) in fk_targets:
        vals = distinct_values(path, col)
        if not vals:
            continue
        existing = _existing_names(engine, ptable, ncol, id_col.get(ptable, "id"))
        existing_names = ["— skip —"] + sorted(existing.keys())
        st.markdown(f"**{fname} · `{col}` → {ptable}**  "
                    f"({len(vals)} distinct · {len(existing)} existing rows"
                    + ("" if existing else " · ⚠ no DB connection / empty table") + ")")

        grid = pd.DataFrame([{
            "☑ Add": True if v not in existing else False,   # default: seed new; existing exact matches unchecked
            "Incoming value": v,
            "Map to existing": ("— skip —"),
            "☑ Remap": False,
            "→ id (SHA1)": entity_id(v),
        } for v in vals])

        edited = st.data_editor(
            grid, hide_index=True, use_container_width=True, key=f"fkgrid_{fname}_{col}",
            column_config={
                "☑ Add":        st.column_config.CheckboxColumn(help="Seed a new parent row (id = SHA1 of value)"),
                "Incoming value":    st.column_config.TextColumn(disabled=True),
                "Map to existing":   st.column_config.SelectboxColumn(options=existing_names,
                                        help="Remap this variant onto an existing parent value"),
                "☑ Remap":      st.column_config.CheckboxColumn(help="Use the 'Map to existing' choice"),
                "→ id (SHA1)":   st.column_config.TextColumn(disabled=True, width="small"),
            })

        # capture actions
        for i, v in enumerate(vals):
            row = edited.iloc[i]
            remap_to = row["Map to existing"] if row["☑ Remap"] else None
            ss.dl_fk_actions[(fname, col, v)] = {"add": bool(row["☑ Add"]), "remap_to": remap_to}

        # live resolution summary (Remap > Add > Skip)
        actions = {v: ss.dl_fk_actions[(fname, col, v)] for v in vals}
        rplan = resolve_fk_plan(vals, actions, existing)
        n_add  = sum(1 for p in rplan.values() if p["action"] == "add")
        n_remap= sum(1 for p in rplan.values() if p["action"] == "remap")
        n_skip = sum(1 for p in rplan.values() if p["action"] == "skip")
        st.caption(f"→ will seed **{n_add}** new, remap **{n_remap}**, null **{n_skip}**. "
                   f"(dry run — nothing written)")
        st.divider()

    c1, c2 = st.columns([1, 2])
    if c1.button("← Back to plan"):
        ss.dl_stage = "review"; st.rerun()
    c2.button("Stage 5 (seed + load) — not built yet", disabled=True,
              use_container_width=True)



# ═══════════════════ LINEAR per-table loader (map -> FK -> load -> next) ═══════════════════
def _table_sequence(plan, overrides):
    """(table, file, path) in FK-topological load order, only mapped files."""
    t2f = {}
    for r in plan["rows"]:
        t = (overrides.get(r["file"], r["table"]) or "").upper()
        if t: t2f[t] = r
    return [(t, t2f[t]["file"], t2f[t]["path"], sorted(t2f[t]["cols"]))
            for t in plan["order"] if t in t2f]

def _apply_normalize(colname, value, action, date_fmt_cache):
    v = (value or "").strip()
    if not v: return None
    if action == "collapse variants":
        return v.upper().replace(" ", "_").strip("_")
    if action and action.startswith("normalize"):
        fmt = date_fmt_cache.get(colname)
        iso = to_iso(v, fmt) if fmt else None
        return iso or v
    return v

# UWI / API keys are stored in fixed-width form (dv_well.uwi is char(14)); the CSV often
# carries the dashed display form ('42-329-10001-0000' -> '42329100010000'). Strip
# separators so the value matches the canonical key and fits the column.
_IDENTIFIER_COLS = {"uwi", "api", "api_num", "api_number", "api_no", "api14", "api_14"}
def _coerce_identifier(db_col, val):
    if val and db_col.lower() in _IDENTIFIER_COLS:
        return _re.sub(r"[^0-9A-Za-z]", "", val)
    return val

def _col_distinct(path, csv_col, db_col):
    """Distinct incoming values for a source column, de-separated when the TARGET is an
    identifier key (uwi/api) — so FK matching, resolution, and length checks all use the
    canonical fixed-width form regardless of whether the column is an FK."""
    vals = distinct_values(path, csv_col)
    if db_col.lower() in _IDENTIFIER_COLS:
        return list(dict.fromkeys(_coerce_identifier(db_col, v) for v in vals))
    return vals

def _table_pk(engine, table):
    """Primary-key column list for dataview.<table>, from sys. [] if none/unknown."""
    if engine is None:
        return []
    try:
        import pandas as pd
        q = ("SELECT c.name FROM sys.indexes i "
             "JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
             "JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
             "WHERE i.is_primary_key=1 AND i.object_id=OBJECT_ID(:t) "
             "ORDER BY ic.key_ordinal")
        from sqlalchemy import text
        with engine.connect() as cx:
            return [r[0].lower() for r in cx.execute(text(q), {"t": f"dataview.{table.lower()}"})]
    except Exception:
        return []

def _table_cols_db(engine, table):
    """Lowercase column-name set for dataview.<table>, from sys. Empty on failure."""
    if engine is None:
        return set()
    try:
        import pandas as pd
        from sqlalchemy import text
        df = pd.read_sql(text("SELECT name FROM sys.columns WHERE object_id=OBJECT_ID(:t)"),
                         engine, params={"t": f"dataview.{table.lower()}"})
        return {str(r[0]).lower() for r in df.itertuples(index=False)}
    except Exception:
        return set()


def _seed_reference(engine, parent, values, dry, log):
    """Idempotently seed reference codes as the parent PK (defaults fill the rest)."""
    if not values:
        return
    log.append(f"seed {parent}: {len(values)} new reference value(s)")
    if dry or engine is None:
        return
    pk = _table_pk(engine, parent)
    if not pk:
        log.append(f"  (cannot seed {parent}: no PK)"); return
    pkc = pk[0]
    cols = _table_cols_db(engine, parent)
    colnames, valexpr = [pkc], [":v"]
    if "active_ind" in cols:       colnames.append("active_ind");     valexpr.append("'Y'")
    if "row_created_by" in cols:   colnames.append("row_created_by");  valexpr.append("'DIR_LOADER'")
    if "row_created_date" in cols: colnames.append("row_created_date"); valexpr.append("SYSUTCDATETIME()")
    ins = (f"INSERT INTO dataview.{parent.lower()} ({', '.join(colnames)}) "
           f"VALUES ({', '.join(valexpr)})")
    chk = f"SELECT 1 FROM dataview.{parent.lower()} WHERE {pkc}=:v"
    from sqlalchemy import text
    with engine.begin() as cx:
        for v in values:
            if not cx.execute(text(chk), {"v": v}).first():
                cx.execute(text(ins), {"v": v})


def _apply_table_load(engine, table, path, cmap, fkcols, ref_adds, norm, entity_adds, functions=None, dry=True):
    """Seed entity + reference parents, then load rows. dry=True -> return SQL/counts only.
    Non-entity loads are idempotent: dedupe on PK, then insert only keys not already present."""
    import pandas as pd
    log = []
    # pre-compute date formats for normalized date cols
    dfmt = {}
    for col in norm:
        vals = distinct_values(path, col)
        dfmt[col] = detect_date_format(vals)
    # 1) entity parent seeds
    # source is left NULL: dv_business_associate.source / dv_field.source FK to
    # dv_r_source, and 'DIR_LOADER' isn't a source code. Provenance lives in row_created_by.
    seed_sql = {
        "DV_BUSINESS_ASSOCIATE": ("INSERT INTO dataview.dv_business_associate "
            "(ba_id, ba_type, ba_name, short_name, active_ind, row_created_by, row_changed_by, row_created_date) "
            "VALUES (:id,'COMPANY',:nm,:sn,'Y','DIR_LOADER','DIR_LOADER',SYSUTCDATETIME())"),
        "DV_FIELD": ("INSERT INTO dataview.dv_field "
            "(field_id, field_name, field_type, active_ind, row_created_by, row_changed_by, row_created_date) "
            "VALUES (:id,:nm,'UNKNOWN','Y','DIR_LOADER','DIR_LOADER',SYSUTCDATETIME())"),
    }
    for parent, name_id in entity_adds.items():
        log.append(f"seed {parent}: {len(name_id)} new rows")
        if not dry and parent in seed_sql:
            from sqlalchemy import text
            with engine.begin() as cx:
                for nm, _id in name_id.items():
                    exists = cx.execute(text(f"SELECT 1 FROM dataview.{parent.lower()} WHERE "
                        + ("ba_id" if parent=='DV_BUSINESS_ASSOCIATE' else "field_id") + "=:id"),
                        {"id": _id}).first()
                    if not exists:
                        cx.execute(text(seed_sql[parent]), {"id": _id, "nm": nm, "sn": nm[:40]})
    # 1b) reference parent seeds (values the user chose to Add)
    for parent, values in (ref_adds or {}).items():
        _seed_reference(engine, parent, sorted(values), dry, log)
    # 2) build the dataframe
    import csv as _csv
    rows_out = []
    fk_res  = {c: v["resolution"] for c, v in fkcols.items()}
    fk_kind = {c: v["kind"] for c, v in fkcols.items()}
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rd = _csv.DictReader(fh)
        srcmap = {_norm(c): c for c in (rd.fieldnames or [])}
        for raw in rd:
            out = {}
            for csv_col, db_col in cmap.items():
                real = srcmap.get(_norm(csv_col))
                val = (raw.get(real) or "") if real else ""
                if db_col in fk_res:
                    key = _coerce_identifier(db_col, val.strip())   # de-sep uwi/api FKs
                    r = fk_res[db_col].get(key, {})
                    if fk_kind[db_col] == "entity":
                        out[db_col] = r.get("id")               # SHA1 id
                    else:
                        out[db_col] = r.get("key", key)         # remapped/added/original key
                else:
                    out[db_col] = _coerce_identifier(
                        db_col, _apply_normalize(csv_col, val, norm.get(csv_col), dfmt))
            rows_out.append(out)
    df = pd.DataFrame(rows_out)
    log.append(f"load {table}: {len(df)} rows, cols={list(df.columns)}")

    # HARD GUARD: a columnless frame makes pandas emit `INSERT ... DEFAULT VALUES`,
    # which violates NOT NULL. Never load an empty mapping — no-op instead.
    if df.shape[1] == 0 and not functions:
        log.append("  no columns mapped — nothing loaded (map columns and Apply first)")
        return log

    # derived columns (function rules) — computed over the full frame in file order
    if functions:
        if df.shape[1] == 0:                       # functions-only load: seed the row count
            df = pd.DataFrame(index=range(sum(1 for _ in open(path, encoding="utf-8",
                                                               errors="replace")) - 1))
        df = _apply_functions(df, functions, log)

    if df.shape[1] == 0:
        log.append("  no columns mapped — nothing loaded (map columns and Apply first)")
        return log

    # stamp standard audit columns the loader owns — but only ones that exist on the
    # target and weren't mapped from the CSV. Some tables (dv_well) default these;
    # others (dv_well_core) don't, so supply them rather than sending NULL.
    if len(df):
        tcols = _table_cols_db(engine, table)
        have = {c.lower() for c in df.columns}
        import datetime as _dt
        stamp = {"row_created_by": "DATA_LOADER",
                 "row_created_date": _dt.datetime.utcnow(),
                 "active_ind": "Y"}
        for c, val in stamp.items():
            if c in tcols and c not in have:
                df[c] = val
                log.append(f"  stamped {c}")

    # idempotent insert: skip rows whose PK already exists (staging + anti-join,
    # not per-row). Guards reference/parent tables (e.g. dv_r_uom) that are pre-seeded.
    pk = [c for c in _table_pk(engine, table) if c in {k.lower() for k in df.columns}]
    if pk and len(df):
        before = len(df)
        # 1) dedupe within the batch on the PK
        df = df.drop_duplicates(subset=pk, keep="first")
        # 2) anti-join against existing keys in the table
        try:
            import pandas as pd
            existing = pd.read_sql(f"SELECT {', '.join(pk)} FROM dataview.{table.lower()}", engine)
            if len(existing):
                key = df[pk].astype(str).agg("\u0001".join, axis=1)
                exk = existing[pk].astype(str).agg("\u0001".join, axis=1)
                df = df[~key.isin(set(exk))]
        except Exception as e:
            log.append(f"  (existing-key check skipped: {e})")
        skipped = before - len(df)
        if skipped:
            log.append(f"  {skipped} row(s) already present (matched PK {pk}) — inserting {len(df)} new")

    if not dry:
        if len(df):
            df.to_sql(table.lower(), engine, schema="dataview", if_exists="append", index=False)
        else:
            log.append("  nothing new to insert")
    return log


# ═══════════ mapping memory: dv_column_map (fingerprints + synonyms) ═══════════
# Every confirmed source_column -> target_column is upserted into dv_column_map,
# keyed by the column-shape fingerprint (source_file_pattern) and independently
# queryable per (target_table, source_column) as a synonym store. `source` is left
# NULL to avoid the dv_r_source FK. All writes are best-effort — never block a load.
def _remember_mapping(engine, table, fp, cmap):
    """Upsert one dv_column_map row per confirmed source->db column mapping."""
    if engine is None or not cmap:
        return
    from sqlalchemy import text
    tt = table.upper()
    up = text(
        "MERGE dataview.dv_column_map AS t "
        "USING (SELECT :mid AS map_id) s ON t.map_id = s.map_id "
        "WHEN MATCHED THEN UPDATE SET confidence_score=1.0, mapping_method='DIR_LOADER', "
        "  confirmed_ind='Y', confirmed_by='DIR_LOADER', confirmed_date=SYSUTCDATETIME(), "
        "  active_ind='Y', row_changed_by='DIR_LOADER', row_changed_date=SYSUTCDATETIME() "
        "WHEN NOT MATCHED THEN INSERT (map_id, source_file_pattern, source_column, "
        "  target_table, target_column, confidence_score, mapping_method, confirmed_ind, "
        "  confirmed_by, confirmed_date, active_ind, row_created_by, row_created_date, source) "
        "VALUES (:mid,:fp,:sc,:tt,:tc,1.0,'DIR_LOADER','Y','DIR_LOADER',SYSUTCDATETIME(),"
        "        'Y','DIR_LOADER',SYSUTCDATETIME(),NULL);")
    try:
        with engine.begin() as cx:
            for src, db in cmap.items():
                sc, tc = _norm(src), db.lower()
                mid = entity_id(f"{fp}|{sc}|{tt}|{tc}")
                cx.execute(up, {"mid": mid, "fp": fp, "sc": sc, "tt": tt, "tc": tc})
    except Exception:
        pass


def _fingerprint_lookup(engine, table, fp):
    """{source_col: db_col} previously confirmed for this exact column shape."""
    if engine is None:
        return {}
    try:
        import pandas as pd
        from sqlalchemy import text
        df = pd.read_sql(text(
            "SELECT source_column, target_column FROM dataview.dv_column_map "
            "WHERE source_file_pattern=:fp AND target_table=:tt "
            "AND confirmed_ind='Y' AND active_ind='Y'"),
            engine, params={"fp": fp, "tt": table.upper()})
        return {r.source_column: r.target_column for r in df.itertuples()}
    except Exception:
        return {}


def _synonym_lookup(engine, table, valid_cols=None):
    """{source_col: db_col} winner-by-hits across all shapes seen for this table.
    A synonym pointing at a column no longer on the table is dropped (valid_cols)."""
    if engine is None:
        return {}
    try:
        import pandas as pd
        from sqlalchemy import text
        df = pd.read_sql(text(
            "SELECT source_column, target_column, COUNT(*) AS hits, "
            "MAX(confirmed_date) AS recent FROM dataview.dv_column_map "
            "WHERE target_table=:tt AND confirmed_ind='Y' AND active_ind='Y' "
            "GROUP BY source_column, target_column"),
            engine, params={"tt": table.upper()})
        best = {}
        for r in df.itertuples():
            if valid_cols is not None and str(r.target_column).lower() not in valid_cols:
                continue
            cur = best.get(r.source_column)
            if cur is None or (r.hits, r.recent) > (cur[1], cur[2]):
                best[r.source_column] = (r.target_column, r.hits, r.recent)
        return {k: v[0] for k, v in best.items()}
    except Exception:
        return {}


# ═══════════ derived columns: FUNCTION rules (saved per table in dv_column_map) ═══════════
# A rule computes a target column the CSV doesn't contain. Stored with
# mapping_method='FUNCTION', source_column = json({"fn":..., "arg":...}), pattern '*'.
FUNCTIONS = ["seq_num", "constant", "concat", "coalesce"]
_FN_HELP = {
    "seq_num":  "row number per partition, file order — arg = part_col[,part_col][;order_col]. e.g. uwi,log_id or log_id;top_depth",
    "constant": "stamp a literal — arg = the value",
    "concat":   "build from other columns — arg = template, e.g. CORE_{uwi}_{core_num}",
    "coalesce": "source else default — arg = source_col|default",
}

def _remember_functions(engine, table, functions):
    """Persist a table's function rules; deactivate any FUNCTION rule not in the set."""
    if engine is None:
        return
    from sqlalchemy import text
    tt = table.upper()
    up = text(
        "MERGE dataview.dv_column_map AS t USING (SELECT :mid AS map_id) s ON t.map_id=s.map_id "
        "WHEN MATCHED THEN UPDATE SET source_column=:sc, mapping_method='FUNCTION', "
        "  confirmed_ind='Y', confirmed_by='DIR_LOADER', confirmed_date=SYSUTCDATETIME(), "
        "  active_ind='Y', row_changed_by='DIR_LOADER', row_changed_date=SYSUTCDATETIME() "
        "WHEN NOT MATCHED THEN INSERT (map_id, source_file_pattern, source_column, target_table, "
        "  target_column, confidence_score, mapping_method, confirmed_ind, confirmed_by, "
        "  confirmed_date, active_ind, row_created_by, row_created_date, source) "
        "VALUES (:mid,'*',:sc,:tt,:tc,1.0,'FUNCTION','Y','DIR_LOADER',SYSUTCDATETIME(),"
        "        'Y','DIR_LOADER',SYSUTCDATETIME(),NULL);")
    keep = set()
    try:
        with engine.begin() as cx:
            for f in functions:
                tc = f["target"].lower()
                keep.add(tc)
                mid = entity_id(f"FN|{tt}|{tc}")
                cx.execute(up, {"mid": mid, "sc": json.dumps({"fn": f["fn"], "arg": f.get("arg", "")}),
                                "tt": tt, "tc": tc})
            # deactivate FUNCTION rules that were removed
            cx.execute(text("UPDATE dataview.dv_column_map SET active_ind='N' "
                            "WHERE target_table=:tt AND mapping_method='FUNCTION' "
                            "AND active_ind='Y' AND LOWER(target_column) NOT IN "
                            "(SELECT value FROM STRING_SPLIT(:keep, ','))"),
                       {"tt": tt, "keep": ",".join(keep) or "\x00"})
    except Exception:
        pass

def _function_lookup(engine, table):
    """[{target, fn, arg}] saved function rules for a table."""
    if engine is None:
        return []
    try:
        import pandas as pd
        from sqlalchemy import text
        df = pd.read_sql(text(
            "SELECT target_column, source_column FROM dataview.dv_column_map "
            "WHERE target_table=:tt AND mapping_method='FUNCTION' AND active_ind='Y'"),
            engine, params={"tt": table.upper()})
        out = []
        for r in df.itertuples():
            try:
                spec = json.loads(r.source_column)
            except Exception:
                spec = {}
            out.append({"target": r.target_column, "fn": spec.get("fn", ""), "arg": spec.get("arg", "")})
        return out
    except Exception:
        return []

def _safe_format(template, row):
    class _D(dict):
        def __missing__(self, k): return ""
    return template.format_map(_D({k: ("" if v is None else v) for k, v in row.items()}))

def _apply_functions(df, functions, log):
    """Compute derived columns in place. seq_num uses current (file) row order."""
    import pandas as pd
    lc = {c.lower(): c for c in df.columns}
    for f in functions:
        tgt, fn, arg = f.get("target"), f.get("fn"), (f.get("arg") or "")
        if not tgt or not fn:
            continue
        if fn == "seq_num":
            # arg = "partcol[,partcol...][;ordercol[,ordercol...]]"; order omitted = file order
            part_spec, _, order_spec = arg.partition(";")
            parts  = [lc[p.strip().lower()] for p in part_spec.split(",") if p.strip().lower() in lc]
            orders = [lc[o.strip().lower()] for o in order_spec.split(",") if o.strip().lower() in lc]
            work = df.sort_values(orders, kind="mergesort") if orders else df   # stable
            if parts:
                seq = work.groupby(parts, sort=False).cumcount() + 1
            else:
                seq = pd.Series(range(1, len(work) + 1), index=work.index)
            df[tgt] = seq.reindex(df.index)                                     # back to file order
            log.append(f"  fn {tgt} = seq_num(by {'+'.join(parts) or 'file'}"
                       + (f", order {'+'.join(orders)}" if orders else ", file order") + ")")
        elif fn == "constant":
            df[tgt] = arg; log.append(f"  fn {tgt} = constant('{arg}')")
        elif fn == "concat":
            df[tgt] = df.apply(lambda r: _safe_format(arg, r.to_dict()), axis=1)
            log.append(f"  fn {tgt} = concat('{arg}')")
        elif fn == "coalesce":
            src, _, dflt = arg.partition("|")
            s = lc.get(src.strip().lower())
            if s:
                df[tgt] = df[s].where(df[s].notna() & (df[s].astype(str).str.len() > 0), dflt)
            else:
                df[tgt] = dflt
            log.append(f"  fn {tgt} = coalesce({src}, '{dflt}')")
    return df



def _preflight_load(fkcols):
    """Given the RESOLVED fk structures, will the load satisfy every FK? -> (rows, has_block).
    Entity FKs self-seed (always ok). Reference/parent FKs are ok once every value is
    present / added / remapped; any value still 'skip' is an unresolved FK violation."""
    rows = []
    for db_col, info in fkcols.items():
        parent, kind, rp = info["parent"], info["kind"], info["resolution"]
        if kind == "entity":
            n_add = sum(1 for p in rp.values() if p["action"] == "add")
            rows.append({"col": db_col, "parent": parent, "level": "ok",
                         "msg": f"entity — {n_add} to seed, {len(rp)-n_add} existing/remapped"})
            continue
        skips = [v for v, p in rp.items() if p["action"] == "skip"]
        n_add = sum(1 for p in rp.values() if p["action"] == "add")
        n_re  = sum(1 for p in rp.values() if p["action"] == "remap")
        n_pr  = sum(1 for p in rp.values() if p["action"] == "present")
        if skips:
            ex = ", ".join(map(str, skips[:4])) + ("\u2026" if len(skips) > 4 else "")
            fix = ("Add or remap them in the grid." if info.get("is_ref")
                   else f"Remap them, or load {parent} first (parent rows aren't seeded).")
            rows.append({"col": db_col, "parent": parent, "level": "block",
                         "msg": f"{len(skips)} value(s) not in {parent}: {ex}. {fix}"})
        else:
            rows.append({"col": db_col, "parent": parent, "level": "ok",
                         "msg": f"{n_pr} present, {n_add} to add, {n_re} remapped"})
    return rows, any(r["level"] == "block" for r in rows)


def _table_col_lengths(engine, table):
    """{col_lower: max_char_len or None} for char/varchar/nchar/nvarchar columns."""
    if engine is None:
        return {}
    try:
        import pandas as pd
        from sqlalchemy import text
        df = pd.read_sql(text(
            "SELECT c.name AS n, ty.name AS t, c.max_length AS ml "
            "FROM sys.columns c JOIN sys.types ty ON ty.user_type_id=c.user_type_id "
            "WHERE c.object_id=OBJECT_ID(:t)"), engine, params={"t": f"dataview.{table.lower()}"})
        out = {}
        for r in df.itertuples(index=False):
            nm, tn, ml = str(r.n).lower(), str(r.t).lower(), r.ml
            if ml is None or ml == -1:                # -1 = MAX
                out[nm] = None
            elif tn in ("char", "varchar"):
                out[nm] = int(ml)                     # bytes == chars
            elif tn in ("nchar", "nvarchar"):
                out[nm] = int(ml) // 2                # 2 bytes per char
            else:
                out[nm] = None
        return out
    except Exception:
        return {}


def _preflight_lengths(engine, table, cmap, fkcols, path, norm):
    """Flag mapped columns whose final loaded value exceeds the DB column width.
    Uses the value AS IT WILL LOAD (norm + identifier de-sep + FK id/key)."""
    rows = []
    lengths = _table_col_lengths(engine, table)
    if not lengths:
        return rows, False
    dfmt = {c: detect_date_format(distinct_values(path, c)) for c in norm}
    fk_res = {c: v["resolution"] for c, v in fkcols.items()}
    fk_kind = {c: v["kind"] for c, v in fkcols.items()}
    inv = {db: src for src, db in cmap.items()}
    for db_col, src in inv.items():
        lim = lengths.get(db_col.lower())
        if not lim:
            continue
        longest = ("", 0)
        for v in distinct_values(path, src):
            if db_col in fk_res:                      # value becomes the id/key at load
                k = _coerce_identifier(db_col, str(v).strip())
                r = fk_res[db_col].get(k, {})
                fv = str(r.get("id") if fk_kind[db_col] == "entity" else (r.get("key", k) or ""))
            else:
                fv = _coerce_identifier(db_col, _apply_normalize(src, v, norm.get(src), dfmt)) or ""
            if len(fv) > longest[1]:
                longest = (fv, len(fv))
        if longest[1] > lim:
            rows.append({"col": db_col, "parent": f"char({lim})", "level": "block",
                         "msg": f"value length {longest[1]} exceeds {lim}: '{longest[0][:32]}'"})
    return rows, any(r["level"] == "block" for r in rows)


def _existing_keys(engine, parent):
    """Set of primary-key values already in a reference/parent table (as str)."""
    if engine is None:
        return set()
    pk = _table_pk(engine, parent)
    if not pk:
        return set()
    try:
        import pandas as pd
        from sqlalchemy import text
        df = pd.read_sql(text(f"SELECT {pk[0]} AS k FROM dataview.{parent.lower()}"), engine)
        return set(df["k"].astype(str))
    except Exception:
        return set()


def _build_fk_structs(table, path, cmap, FKC, engine, ss, fname):
    """From the APPLIED cmap + saved fk_actions, resolve EVERY FK column (entity and
    reference/parent). Returns (fkcols, ref_adds, entity_adds), where fkcols carries a
    'kind' + per-value resolution used identically by preview and load."""
    id_col = {"DV_BUSINESS_ASSOCIATE": "ba_id", "DV_FIELD": "field_id"}
    fkcols, entity_adds, ref_adds = {}, defaultdict(dict), defaultdict(set)
    for csv_col, db_col in cmap.items():
        fk = _fk_of(table, db_col, FKC)
        if not fk:
            continue
        parent, kind = fk
        vals = _col_distinct(path, csv_col, db_col)
        if kind == "entity":
            existing = _existing_names(engine, parent,
                        "ba_name" if parent == "DV_BUSINESS_ASSOCIATE" else "field_name",
                        id_col.get(parent, "id"))
            actions = {v: (ss.dl_fk_actions.get((fname, csv_col, v))
                           or {"add": v not in existing, "remap_to": None}) for v in vals}
            rp = resolve_fk_plan(vals, actions, existing)
            fkcols[db_col] = {"source": csv_col, "parent": parent, "kind": "entity", "resolution": rp}
            for v, p in rp.items():
                if p["action"] == "add" and p["id"]:
                    entity_adds[parent][v] = p["id"]
        else:   # reference or parent — the value is the parent key
            existing = _existing_keys(engine, parent)
            is_ref = is_ref_table(parent)   # only dv_r_* codes may be seeded (Add)
            actions = {}
            for v in vals:
                a = ss.dl_fk_actions.get((fname, csv_col, v)) \
                    or {"add": (is_ref and v not in existing), "remap_to": None}
                if not is_ref:                 # can't seed a data-table parent from a child row
                    a = {"add": False, "remap_to": a.get("remap_to")}
                actions[v] = a
            rp = resolve_ref_plan(vals, actions, existing)
            fkcols[db_col] = {"source": csv_col, "parent": parent, "kind": kind,
                              "is_ref": is_ref, "resolution": rp}
            if is_ref:
                for v, p in rp.items():
                    if p["action"] == "add" and p["seed"] is not None:
                        ref_adds[parent].add(p["seed"])
    return fkcols, ref_adds, entity_adds



def _scroll_to_top():
    """Streamlit keeps scroll position across reruns; nudge the main container back to
    the top so a newly-loaded table starts at its heading, not where the button was."""
    try:
        import streamlit.components.v1 as _components
    except Exception:
        return
    _components.html(
        """
        <script>
          const doc = window.parent.document;
          const go = () => {
            const sels = ['section.main', 'section[data-testid="stMain"]',
                          '.stMainBlockContainer', '[data-testid="stAppViewContainer"]'];
            for (const s of sels) { const el = doc.querySelector(s); if (el) el.scrollTo(0, 0); }
            doc.documentElement.scrollTop = 0; doc.body.scrollTop = 0;
          };
          go(); setTimeout(go, 60); setTimeout(go, 180);
        </script>
        """,
        height=0,
    )


def _already_loaded(engine, table, path, csv_cols, COLS):
    """Detect whether this table is already loaded for this file's wells.
    Returns (loaded, detail):
      loaded=True  → every UWI in this file already exists in the target (or, for a table
                     with no UWI column, the target is already populated) — safe to auto-skip.
      loaded=False → not loaded, or only partially — let the load run. detail may still note
                     partial coverage.
    Any failure returns (False, '') so a check error never blocks a real load."""
    import pandas as pd
    from sqlalchemy import text
    tgt = {c.upper() for c in (COLS.get(table.upper()) or COLS.get(table) or set())}
    try:
        if "UWI" in tgt:
            src_uwi = next((c for c in csv_cols
                            if _norm(c) in ("UWI", "API", "UWI14", "API14", "API_UWI")), None)
            if not src_uwi:
                return (False, "")
            col = pd.read_csv(path, usecols=[src_uwi], dtype=str)[src_uwi].dropna()
            batch = sorted({str(v).strip()[:14] for v in col if str(v).strip()})
            if not batch:
                return (False, "")
            found = set()
            with engine.connect() as cx:
                for i in range(0, len(batch), 900):        # SQL Server ~2100-param cap
                    chunk = batch[i:i + 900]
                    ph = ",".join(f":u{j}" for j in range(len(chunk)))
                    params = {f"u{j}": u for j, u in enumerate(chunk)}
                    for r in cx.execute(text(
                            f"SELECT DISTINCT RTRIM(uwi) FROM dataview.{table.lower()} "
                            f"WHERE RTRIM(uwi) IN ({ph})"), params):
                        found.add(r[0])
            if not found:
                return (False, "")
            if len(found) >= len(batch):
                return (True, f"all {len(batch)} well(s) already loaded")
            return (False, f"{len(found)} of {len(batch)} well(s) already present")
        # no UWI column (reference/lookup table): populated == loaded
        with engine.connect() as cx:
            n = cx.execute(text(f"SELECT COUNT(*) FROM dataview.{table.lower()}")).scalar() or 0
        return (n > 0, f"{n} row(s) already present") if n > 0 else (False, "")
    except Exception:
        return (False, "")


def _sequence_stage(ss, engine):
    import pandas as pd
    if ss.pop("dl_scroll_top", False):     # set when advancing to a new table
        _scroll_to_top()
    plan = ss.dl_plan
    FKC, COLS, KIND, _ = load_catalog(ss.dl_cat)
    ss.setdefault("dl_colmaps", {}); ss.setdefault("dl_fingerprints", {})
    ss.setdefault("dl_fk_actions", {}); ss.setdefault("dl_loaded", [])
    seq = _table_sequence(plan, ss.get("dl_overrides", {}))
    idx = ss.get("dl_seq_idx", 0)

    # progress rail
    rail = "  ".join(("✅ " + t if t in ss.dl_loaded else
                      ("➡ " + t if i == idx else "○ " + t))
                     for i, (t, *_ ) in enumerate(seq))
    st.caption(f"Load order: {rail}")

    if idx >= len(seq):
        st.success(f"🎉 All {len(seq)} tables processed. Loaded: {', '.join(ss.dl_loaded) or '(none)'}")
        if st.button("← Start over"):
            ss.dl_stage = "review"; ss.dl_seq_idx = 0; ss.dl_loaded = []; st.rerun()
        return

    table, fname, path, csv_cols = seq[idx]

    # ── auto-skip already-loaded tables (with per-table override to force a reload) ──
    ss.setdefault("dl_autoskipped", {})     # {table: detail}
    ss.setdefault("dl_force", {})           # {table: True}  → user chose to load anyway
    if ss.dl_autoskipped:
        st.caption("Auto-skipped (already loaded): " + " · ".join(
            f"{t} ({d})" for t, d in ss.dl_autoskipped.items()))
        rc = st.columns(min(4, len(ss.dl_autoskipped)) or 1)
        for i, t in enumerate(list(ss.dl_autoskipped)):
            if rc[i % len(rc)].button(f"↻ reload {t}", key=f"dl_reload_{t}"):
                ss.dl_force[t] = True
                ss.dl_autoskipped.pop(t, None)
                ss.dl_seq_idx = next((j for j, s in enumerate(seq) if s[0] == t), idx)
                ss.dl_scroll_top = True; st.rerun()

    if not ss.dl_force.get(table):
        loaded, detail = _already_loaded(engine, table, path, csv_cols, COLS)
        if loaded:
            ss.dl_autoskipped[table] = detail
            if table not in ss.dl_loaded:
                ss.dl_loaded.append(table)
            ss.dl_seq_idx = idx + 1
            st.rerun()

    st.subheader(f"Table {idx+1} of {len(seq)}: `{table}`")
    st.markdown(f"source: **{fname}**")

    # APPLIED mapping, in order of trust: session (this file) -> session (seen shape)
    # -> DB fingerprint (same shape confirmed before) -> synonym-aware suggestion.
    fp = fingerprint_cols(csv_cols)
    valid_cols = {c.lower() for c in COLS.get(table, set())}
    syn = _synonym_lookup(engine, table, valid_cols)
    ss.setdefault("dl_functions", {})
    if table not in ss.dl_functions:                      # load saved rules once per table
        ss.dl_functions[table] = _function_lookup(engine, table)
    functions = [f for f in ss.dl_functions.get(table, []) if f.get("target") and f.get("fn")]
    src_mem = None
    applied = ss.dl_colmaps.get(fname) or ss.dl_fingerprints.get(fp)
    if applied:
        src_mem = "session"
    else:
        fpdb = _fingerprint_lookup(engine, table, fp)
        if fpdb:
            applied, src_mem = fpdb, "saved shape (dv_column_map)"
        else:
            applied = suggest_colmap(csv_cols, table, COLS, FKC, syn)
            src_mem = "synonyms + rules" if syn else "rules"
    cmap = {c: t for c, t in applied.items()
            if c in csv_cols and t not in ("— skip —", "", None)}
    st.caption(f"mapping source: **{src_mem}**"
               + (f" · {len(syn)} learned synonym(s) for this table" if syn else ""))

    db_cols = sorted({c.lower() for c in COLS.get(table, set())})
    options = ["— skip —"] + db_cols
    samples = {c: (distinct_values(path, c)[:1] or [""])[0] for c in csv_cols}
    id_col = {"DV_BUSINESS_ASSOCIATE": "ba_id", "DV_FIELD": "field_id"}

    st.info("Edit the grids freely — nothing recomputes until you press **Apply changes**. "
            "The FK grids reflect the last applied mapping.")

    # ══ everything editable lives inside ONE form: no rerun until submit ══
    with st.form(f"seqform_{fname}", clear_on_submit=False):
        st.markdown("**1 · Map columns**")
        map_grid = pd.DataFrame([{"Source": c, "Sample": samples[c][:26],
                                  "→ DB column": cmap.get(c, "— skip —")} for c in csv_cols])
        map_ed = st.data_editor(
            map_grid, hide_index=True, use_container_width=True, key=f"seqmap_{fname}",
            column_config={"Source": st.column_config.TextColumn(disabled=True),
                           "Sample": st.column_config.TextColumn(disabled=True),
                           "→ DB column": st.column_config.SelectboxColumn(options=options, required=True)})

        st.markdown("**2 · Resolve FK values**")
        fk_eds = {}   # csv_col -> (editor_df, vals, kind)
        any_fk = False
        for csv_col, db_col in cmap.items():
            fk = _fk_of(table, db_col, FKC)
            if not fk:
                continue
            parent, kind = fk
            any_fk = True
            vals = _col_distinct(path, csv_col, db_col)
            if kind == "entity":
                existing = _existing_names(engine, parent,
                            "ba_name" if parent == "DV_BUSINESS_ASSOCIATE" else "field_name",
                            id_col.get(parent, "id"))
                existing_keys = set(existing.keys())
                idfn = lambda v: entity_id(v)
                kindlbl = f"{parent} · seed = SHA1 id"
            else:
                existing_keys = _existing_keys(engine, parent)
                idfn = lambda v: ("in " + parent) if v in existing_keys else "NEW"
                kindlbl = f"{parent} · seed = code"
            opts = ["— skip —"] + sorted(existing_keys)
            miss = sum(1 for v in vals if v not in existing_keys)
            st.caption(f"`{csv_col}` → `{db_col}` ({kindlbl}) · {len(vals)} values · "
                       f"{len(existing_keys)} existing · {miss} missing")
            rows = []
            for v in vals:
                a = ss.dl_fk_actions.get((fname, csv_col, v)) or {}
                rows.append({"☑ Add": a.get("add", v not in existing_keys), "Value": v,
                             "Map to existing": a.get("remap_to") or "— skip —",
                             "☑ Remap": bool(a.get("remap_to")),
                             "status": idfn(v)})
            fg = pd.DataFrame(rows)
            fk_eds[csv_col] = (st.data_editor(
                fg, hide_index=True, use_container_width=True, key=f"seqfk_{fname}_{csv_col}",
                column_config={"☑ Add": st.column_config.CheckboxColumn(help="Seed this value into the parent"),
                               "Value": st.column_config.TextColumn(disabled=True),
                               "Map to existing": st.column_config.SelectboxColumn(options=opts,
                                   help="Fold this value onto an existing parent value"),
                               "☑ Remap": st.column_config.CheckboxColumn(help="Use the 'Map to existing' choice"),
                               "status": st.column_config.TextColumn(disabled=True, width="small")}), vals, kind)
        if not any_fk:
            st.caption("No FK columns in the applied mapping.")

        # ── derived columns: function rules (saved per table) ──
        st.markdown("**4 · Derived columns (functions)** — computed, not from the CSV")
        st.caption(" · ".join(f"`{k}`: {v}" for k, v in _FN_HELP.items()))
        fn_seed = functions or [{"target": "", "fn": "", "arg": ""}]
        fn_grid = pd.DataFrame([{"Target column": f.get("target", ""), "Function": f.get("fn", ""),
                                 "Argument": f.get("arg", "")} for f in fn_seed])
        fn_ed = st.data_editor(
            fn_grid, hide_index=True, use_container_width=True, num_rows="dynamic",
            key=f"seqfn_{table}",
            column_config={
                "Target column": st.column_config.SelectboxColumn(options=[""] + db_cols, required=False),
                "Function": st.column_config.SelectboxColumn(options=[""] + FUNCTIONS, required=False),
                "Argument": st.column_config.TextColumn(help="e.g. seq_num arg = uwi")})

        submitted = st.form_submit_button("✅ Apply changes", type="primary",
                                          use_container_width=True)

    if submitted:
        # persist the map
        new_cmap = {csv_cols[i]: map_ed.iloc[i]["→ DB column"] for i in range(len(csv_cols))
                    if map_ed.iloc[i]["→ DB column"] not in ("— skip —", "", None)}
        ss.dl_colmaps[fname] = new_cmap
        ss.dl_fingerprints[fp] = new_cmap
        # persist the FK choices
        for csv_col, (fe, vals, _kind) in fk_eds.items():
            for i, v in enumerate(vals):
                remap = fe.iloc[i]["Map to existing"] if fe.iloc[i]["☑ Remap"] else None
                ss.dl_fk_actions[(fname, csv_col, v)] = {"add": bool(fe.iloc[i]["☑ Add"]),
                                                         "remap_to": remap}
        # persist function rules for this table
        rules = [{"target": r["Target column"], "fn": r["Function"], "arg": r["Argument"] or ""}
                 for _, r in fn_ed.iterrows()
                 if r["Target column"] and r["Function"]]
        ss.dl_functions[table] = rules
        _remember_functions(engine, table, rules)
        st.rerun()

    # ══ derived views + actions (from APPLIED state only) ══
    dup_targets = sorted({v for v in cmap.values() if list(cmap.values()).count(v) > 1})
    if dup_targets:
        st.error(f"⚠ Two source columns map to the same DB column: {dup_targets}. Fix before loading.")

    fkcols, ref_adds, entity_adds = _build_fk_structs(table, path, cmap, FKC, engine, ss, fname)

    st.markdown("**3 · FK coverage & load**")
    handled = {d.lower() for d in cmap.values()}
    loaded_set = set(ss.dl_loaded)
    cov = []
    for fk in FKC.get(table, []):
        child = [c.lower() for c in fk["child_cols"]]; parent = fk["parent_table"]
        if parent in loaded_set or parent == table:
            lvl = "✅ loaded"
        elif all(c in handled for c in child):
            lvl = "✅ " + ("ref" if is_ref_table(parent) else "resolved")
        else:
            lvl = "⚪ null"
        cov.append({"FK": "+".join(fk["child_cols"]), "Parent": parent, "Status": lvl})
    if cov:
        st.dataframe(pd.DataFrame(cov), hide_index=True, use_container_width=True)

    norm = {_norm(v["column"]): v["action"] for v in validate_file(path)
            if v["level"] == "fix" and _norm(v["column"]) in cmap}
    if norm:
        st.caption("normalize on load: " + ", ".join(f"{k} ({a})" for k, a in norm.items()))

    prev = _apply_table_load(engine, table, path, cmap, fkcols, ref_adds, norm, dict(entity_adds),
                             functions=functions, dry=True)
    with st.expander("Preview (dry run — nothing written)", expanded=True):
        for line in prev:
            st.text("  " + line)

    # pre-flight: given your Add/Remap choices, will every FK be satisfied? ──
    pf_rows, fk_block = _preflight_load(fkcols)
    len_rows, len_block = _preflight_lengths(engine, table, cmap, fkcols, path, norm)
    all_rows = pf_rows + len_rows
    has_block = fk_block or len_block
    if all_rows:
        st.markdown("**Pre-flight — FK readiness & column widths**")
        icon = {"ok": "🟢", "warn": "🟡", "block": "🔴"}
        st.dataframe(pd.DataFrame([{"": icon.get(r["level"], ""), "Column": r["col"],
                                    "Parent / limit": r["parent"], "Check": r["msg"]} for r in all_rows]),
                     hide_index=True, use_container_width=True)
    if fk_block:
        st.error("🔴 Unresolved FK value(s) above — in the grid, check **Add** to seed them "
                 "into the parent or pick **Map to existing** + **Remap**, then **Apply changes**.")
    if len_block:
        st.error("🔴 Value(s) too long for their column above — fix the source data, widen the "
                 "column, or unmap it. (UWI/API keys are de-separated automatically.)")

    no_map = len(cmap) == 0 and not functions
    if no_map:
        st.warning("No columns are mapped for this file. Set the mappings in the grid above and "
                   "click **Apply changes** — or **Skip table** if it's already seeded. "
                   "(Reference tables only auto-map when the CSV headers match the DB column "
                   "names exactly; otherwise map them once and it's remembered.)")
    else:
        st.caption("Preview and Load use the **last applied** mapping — after editing the grid, "
                   "click **Apply changes** before Load.")

    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("← Back"):
        ss.dl_stage = "review"; st.rerun()
    if c2.button("Skip table"):
        ss.dl_seq_idx = idx + 1; ss.dl_scroll_top = True; st.rerun()
    if c3.button(f"✅ Load {table} & next →", type="primary", use_container_width=True,
                 disabled=bool(dup_targets) or has_block or no_map):
        try:
            _apply_table_load(engine, table, path, cmap, fkcols, ref_adds, norm, dict(entity_adds),
                              functions=functions, dry=False)
            _remember_mapping(engine, table, fp, cmap)   # persist fingerprint + synonyms
            ss.dl_loaded.append(table); ss.dl_seq_idx = idx + 1
            ss.dl_scroll_top = True
            st.rerun()
        except Exception as exc:
            try:
                from dataview.import_data import load_diagnostics as _diag
            except Exception:
                import load_diagnostics as _diag
            _diag.render(exc, table=table)


def run(engine=None, dialect=None):
    st.title("📥 Directory Loader")
    st.caption("Point at a folder of CSVs → catalog-guided load plan. Dry run — writes nothing.")
    ss = st.session_state
    ss.setdefault("dl_stage", "pick")
    if ss.dl_stage == "sequence" and ss.get("dl_plan"):
        _sequence_stage(ss, engine)
    elif ss.dl_stage == "review" and ss.get("dl_plan"):
        _review(ss)
    else:
        _pick(ss)

render = main = show = app = run

if __name__ == "__main__":
    run()
