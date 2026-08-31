#!/usr/bin/env python3
r"""
vault_organizer.py
==================
Materialize the curated vault tree from the file catalog. Reads
file_catalog.GLOBAL_FILE_CATALOG (+ FILE_WELL_HEADER, FILE_SEIS_HEADER) and
lays every cataloged file into an entity-organized tree by copy, symlink, or
hardlink. Dry-run by default — prints a plan and writes vault_plan.csv; nothing
on disk changes until you pass --apply.

Layout produced (under <vault-root>/<tier>/):

    wells/<STATE>/<COUNTY>/<UWI>__<WELL_NAME>/<class>/<file>
        class = logs | directional | formation_tops | completion |
                production | reports | spatial   (by report_type, else filetype)
    seismic/<2D|3D|unsorted>/<SURVEY>/<class>/<file>
        class = navigation | segy | interpretation | reports
    spatial/<feature>/<file>                      (GIS not tied to a well)
    _unmatched/needs_ocr/<file>                   (images — identity needs OCR)
    _unmatched/by_filetype/<ext>/<file>           (everything else with no UWI)

REJECT-tier files are skipped. Shapefile / MapInfo sidecars are carried with
their parent (never filed on their own) so a .shp set stays intact.

Usage:
    python vault_organizer.py --vault-root D:\Vault
    python vault_organizer.py --vault-root D:\Vault --mode hardlink --apply
    python vault_organizer.py --vault-root D:\Vault --limit 200      # sample

--mode:
    copy     (default) independent copies
    symlink  symlinks into the original files (Windows: needs Developer Mode
             or admin)
    hardlink same-volume hardlinks — zero extra disk, no admin (recommended on
             a single laptop drive)
"""

import argparse
import csv
import os
import re
import shutil
import sys

# ── extension taxonomy ────────────────────────────────────────────────────────
LOG_EXTS  = {".las", ".dlis", ".dlf", ".lis", ".asc", ".prn"}
DEV_EXTS  = {".dev"}
GIS_EXTS  = {".kml", ".kmz", ".shp", ".geojson", ".gpkg", ".tab", ".mif"}
# multi-feature "layer" formats — reference layers, never nested under one well
VECTOR_LAYER_EXTS = {".shp", ".geojson", ".gpkg", ".tab", ".mif"}
SEGY_EXTS = {".segy", ".sgy", ".seg"}
NAV_EXTS  = {".p190", ".p90", ".p1", ".p2", ".p3"}
IMG_EXTS  = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}

# sidecars are only treated as sidecars when a parent file shares their stem,
# so a standalone .dat (e.g. an RRC daf420.dat) is never mistaken for one.
PARENT_OF_SIDECARS = (".shp", ".tab", ".mif")
SHP_SIDE = {".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix",
            ".fix", ".ain", ".aih", ".atx"}
TAB_SIDE = {".map", ".dat", ".id", ".ind"}
MIF_SIDE = {".mid"}
SIDECAR_EXTS = SHP_SIDE | TAB_SIDE | MIF_SIDE


def norm_uwi(u):
    """Canonical grouping key: the first 14 digits of the UWI. Collapses
    formatting differences and polluted extractions (e.g. a UWI cell that
    swallowed the well name) onto the same well."""
    d = re.sub(r"\D", "", str(u or ""))
    return d[:14] if len(d) >= 14 else d


def _ext(name):
    return os.path.splitext(name or "")[1].lower()


def sanitize(name, default="UNKNOWN"):
    """Make a string safe as a single Windows path component."""
    if name is None:
        return default
    s = str(name).replace("\x00", "").strip()
    for ch in '<>:"/\\|?*':
        s = s.replace(ch, "_")
    s = "".join(c for c in s if ord(c) >= 32)          # drop control chars
    s = " ".join(s.split()).replace(" ", "_")          # collapse whitespace
    s = s.strip("._")
    return (s[:120] or default)


# ── routing (pure functions — unit-testable without a DB) ─────────────────────
def well_class(ext, report_type):
    if ext in LOG_EXTS:
        return "logs"
    if ext in DEV_EXTS:
        return "directional"
    if ext in GIS_EXTS:
        return "spatial"
    rt = (report_type or "").lower()
    if any(k in rt for k in ("direction", "deviat", "survey")):
        return "directional"
    if any(k in rt for k in ("formation", "tops", "marker")):
        return "formation_tops"
    if any(k in rt for k in ("completion", "frac", "stimulat", "perf")):
        return "completion"
    if any(k in rt for k in ("production", "volume", "allocation")):
        return "production"
    if "log" in rt:
        return "logs"
    return "reports"


def seis_class(ext):
    if ext in NAV_EXTS:
        return "navigation"
    if ext in SEGY_EXTS:
        return "segy"
    if ext == ".json":
        return "interpretation"
    if ext in (".pdf", ".docx", ".doc", ".xlsx"):
        return "reports"
    return "misc"


def seis_dim(set_type):
    t = (set_type or "").upper()
    if "3" in t:
        return "3D"
    if "2" in t:
        return "2D"
    return "unsorted"


def route(row, consensus=None):
    """Return a list of path components (the dest dir under the tier), or None
    to skip the file entirely (REJECT). Pass the build_consensus() dict so well
    files key on one resolved identity per well rather than per-file values."""
    tier = (row.get("value_tier") or "").upper()
    if tier == "REJECT":
        return None

    ext = _ext(row.get("file_name"))
    ftg = (row.get("file_type_group") or "")
    uwi = (row.get("uwi") or row.get("matched_uwi") or "").strip()

    # 1) seismic — by group (seismic files rarely carry a UWI)
    if ftg == "Seismic" or ext in SEGY_EXTS or ext in NAV_EXTS:
        survey = sanitize(row.get("survey_name"), "UNSORTED")
        return ["seismic", seis_dim(row.get("seis_set_type")), survey,
                seis_class(ext)]

    # 2) multi-feature vector layers — always top-level spatial, even if a
    #    stray UWI got matched (a well_locations.* layer is not one well's file)
    if ext in VECTOR_LAYER_EXTS:
        return ["spatial", sanitize(row.get("feature_type"), "misc").lower()]

    # 3) well — keyed on the NORMALIZED uwi, with ONE state/county/name per well
    if uwi:
        k = norm_uwi(uwi) or sanitize(uwi)
        res = (consensus or {}).get(k, {})
        state = sanitize(res.get("state") or row.get("state"), "XX")
        county = sanitize(res.get("county") or row.get("county"), "UNKNOWN")
        wname = sanitize(res.get("well_name") or row.get("well_name"), "WELL")
        return ["wells", state, county, f"{k}__{wname}",
                well_class(ext, row.get("report_type"))]

    # 4) GIS not tied to a well (KML/KMZ with no UWI)
    if ftg == "GIS" or ext in GIS_EXTS:
        feature = sanitize(row.get("feature_type"), "misc").lower()
        return ["spatial", feature]

    # 5) unmatched / quarantine
    if ext in IMG_EXTS:
        return ["_unmatched", "needs_ocr"]
    return ["_unmatched", "by_filetype", (ext.lstrip(".") or "noext")]


# ── sidecar handling ──────────────────────────────────────────────────────────
def is_sidecar(path):
    if _ext(path) not in SIDECAR_EXTS:
        return False
    stem = os.path.splitext(path)[0]
    return any(os.path.exists(stem + p) for p in PARENT_OF_SIDECARS)


def sidecars_for(path):
    ext = _ext(path)
    if ext not in PARENT_OF_SIDECARS:
        return []
    stem = os.path.splitext(path)[0]
    side = {".shp": SHP_SIDE, ".tab": TAB_SIDE, ".mif": MIF_SIDE}[ext]
    return [stem + e for e in side if os.path.exists(stem + e)]


# ── canonical identity (one (state, county, name) per UWI) ────────────────────
_GENERIC_NAMES = {"well", "report", "well name", "report well name",
                  "summary report", "well summary", "well report",
                  "unknown", "n/a", "none"}


def _clean_name(name, uwi):
    """A well name worth trusting, or None. Rejects generic labels and values
    that embed the API/UWI digits (concatenated-cell extraction artifacts)."""
    if not name:
        return None
    s = str(name).replace("\x00", "").strip()
    if not s or s.lower() in _GENERIC_NAMES:
        return None
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 8:                 # looks like it carries a UWI/API number
        return None
    if uwi and uwi[:10] and uwi[:10] in s.replace("-", "").replace(" ", ""):
        return None
    return s


def canonical_identity(rows):
    """Resolve one (well_name, state, county) per UWI by majority vote across
    all files sharing that UWI, so every file of a well routes to one folder.
    Cleans bad names first; falls back to the raw majority name if all were
    rejected (better a noisy folder than a fragmented well)."""
    from collections import defaultdict, Counter
    agg = defaultdict(lambda: {"name": Counter(), "raw": Counter(),
                               "state": Counter(), "county": Counter()})
    for r in rows:
        u = (r.get("uwi") or r.get("matched_uwi") or "").strip()
        key = norm_uwi(u)
        if not key:
            continue
        raw = (r.get("well_name") or "").replace("\x00", "").strip()
        if raw:
            agg[key]["raw"][raw] += 1
        clean = _clean_name(r.get("well_name"), key)
        if clean:
            agg[key]["name"][clean] += 1
        st = sanitize(r.get("state"), "") if r.get("state") else ""
        if st:
            agg[key]["state"][st] += 1
        co = sanitize(r.get("county"), "") if r.get("county") else ""
        if co:
            agg[key]["county"][co] += 1

    out = {}
    for u, a in agg.items():
        name = (a["name"].most_common(1)[0][0] if a["name"]
                else a["raw"].most_common(1)[0][0] if a["raw"] else None)
        out[u] = {
            "well_name": name,
            "state":  a["state"].most_common(1)[0][0] if a["state"] else None,
            "county": a["county"].most_common(1)[0][0] if a["county"] else None,
        }
    return out


def build_plan(rows, curated_root):
    """Canonicalize identity per UWI, then route every file (carrying sidecars
    with their parent). Returns (plan, sidecars_carried). Shared by the CLI and
    the pipeline so routing can't drift between them."""
    canon = canonical_identity(rows)
    plan, carried = [], 0
    for r in rows:
        src = r.get("file_path")
        if not src:
            continue
        if is_sidecar(src):
            carried += 1
            continue
        u = (r.get("uwi") or r.get("matched_uwi") or "").strip()
        key = norm_uwi(u)
        if key and key in canon:
            c = canon[key]
            r = {**r,
                 "well_name": c["well_name"] or r.get("well_name"),
                 "state":     c["state"]     or r.get("state"),
                 "county":    c["county"]    or r.get("county")}
        parts = route(r)
        if parts is None:
            continue
        dst_dir = os.path.join(curated_root, *parts)
        for f in [src] + sidecars_for(src):
            plan.append((f, os.path.join(dst_dir, os.path.basename(f)),
                         "/".join(parts)))
    return plan, carried


# ── catalog read (schema-defensive) ───────────────────────────────────────────
def _existing_cols(con, schema, table):
    from sqlalchemy import text
    # sys.columns keyed on OBJECT_ID resolves a single table's columns with a
    # metadata seek. INFORMATION_SCHEMA.COLUMNS was ~5s here because SQL Server
    # materialises the whole per-column catalog view before applying the
    # schema/table filter — the same slow catalog emit seen in promote. A
    # missing table yields OBJECT_ID NULL -> no rows -> empty set (still
    # schema-defensive, matching the old behaviour).
    rows = con.execute(text(
        "SELECT c.name FROM sys.columns c WHERE c.object_id = OBJECT_ID(:full)"),
        {"full": f"{schema}.{table}"}).fetchall()
    return {r[0].upper() for r in rows}


def _pick(prefix, cols, candidates, alias):
    """First existing candidate column, COALESCE'd if several exist, NULL if
    none. (SQL Server rejects COALESCE with a single argument, so a lone
    column is emitted bare.)"""
    have = [f"{prefix}.{c}" for c in candidates if c.upper() in cols]
    if not have:
        expr = "NULL"
    elif len(have) == 1:
        expr = have[0]
    else:
        expr = f"COALESCE({', '.join(have)})"
    # Bounded cast so pyodbc binds (not per-cell SQLGetData) — see fetch_rows note.
    return f"CAST({expr} AS NVARCHAR(400)) AS {alias}"


def _session_waits(con):
    """Cumulative per-session wait totals (ms) by type, for THIS connection's
    SPID. Diffed around a slow statement to name what it actually waited on."""
    from sqlalchemy import text
    try:
        return {r[0]: r[1] for r in con.execute(text(
            "SELECT wait_type, wait_time_ms FROM sys.dm_exec_session_wait_stats "
            "WHERE session_id = @@SPID")).fetchall()}
    except Exception:
        return {}


def fetch_rows(con, schema, limit=None, log=None):
    from sqlalchemy import text
    import time as _t
    _tm = {}
    _t0 = _t.monotonic()
    w = _existing_cols(con, schema, "FILE_WELL_HEADER")
    _tm["wcols"] = _t.monotonic() - _t0
    _t0 = _t.monotonic()
    s = _existing_cols(con, schema, "FILE_SEIS_HEADER")
    _tm["scols"] = _t.monotonic() - _t0

    sel = [
        "g.INVENTORY_ID AS inventory_id",
        # Bounded NVARCHAR casts so pyodbc BINDS these columns instead of falling
        # back to per-cell SQLGetData streaming (a network round-trip per cell,
        # which showed up as ASYNC_NETWORK_IO ≈ 9s on a MAX-typed FILE_PATH).
        "CAST(g.FILE_PATH AS NVARCHAR(4000)) AS file_path",
        "CAST(g.FILE_NAME AS NVARCHAR(400))  AS file_name",
        "CAST(g.FILE_TYPE_GROUP AS NVARCHAR(100)) AS file_type_group",
        "CAST(g.MATCHED_UWI AS NVARCHAR(64))  AS matched_uwi",
        "g.VALUE_TIER   AS value_tier",
        _pick("w", w, ["UWI14", "UWI"], "uwi"),
        _pick("w", w, ["WELL_NAME", "NAME_NORM"], "well_name"),
        _pick("w", w, ["STATE"], "state"),
        _pick("w", w, ["COUNTY"], "county"),
        _pick("w", w, ["REPORT_TYPE"], "report_type"),
        _pick("s", s, ["SURVEY_NAME", "SURVEY", "LINE_NAME", "NAME"], "survey_name"),
        _pick("s", s, ["SEIS_SET_TYPE", "SET_TYPE", "DIMENSION", "DIMS"], "seis_set_type"),
        _pick("w", w, ["FEATURE_TYPE"], "feature_type"),
    ]
    top = f"TOP {int(limit)} " if limit else ""
    sql = (f"SELECT {top}" + ", ".join(sel) +
           f" FROM {schema}.GLOBAL_FILE_CATALOG g "
           f"LEFT JOIN {schema}.FILE_WELL_HEADER w ON w.INVENTORY_ID=g.INVENTORY_ID "
           f"LEFT JOIN {schema}.FILE_SEIS_HEADER s ON s.INVENTORY_ID=g.INVENTORY_ID "
           # Honor the scan's content de-dupe: DUPLICATE_GROUP is stamped on every
           # redundant copy (same FILE_HASH), leaving one canonical row per unique
           # content. Vaulting only the canonical avoids copying identical bytes
           # more than once — same as extract + capture already do.
           f"WHERE g.DUPLICATE_GROUP IS NULL")
    # Placement is a read-only PLAN. The capture stage that runs right before
    # vault can still hold locks on GLOBAL_FILE_CATALOG when this reads, so a
    # normal read blocks ~9s waiting on it. A dirty read is correct for deciding
    # where to copy files (the rows are single-writer and will commit), so read
    # under READ UNCOMMITTED to skip the lock wait entirely. Probed 9.59s → 0.94s.
    con.execute(text("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED"))
    _w0 = _session_waits(con)
    _t0 = _t.monotonic()
    rows = [dict(r._mapping) for r in con.execute(text(sql)).fetchall()]
    _tm["select"] = _t.monotonic() - _t0
    if log:
        log("[vault-fetch] " + " · ".join(f"{k} {v:.2f}s" for k, v in _tm.items())
            + f"  ({len(rows)} rows)")
        if _tm["select"] > 2.0:                # slow → name what it waited on
            _w1 = _session_waits(con)
            diff = sorted(((k, _w1.get(k, 0) - _w0.get(k, 0)) for k in _w1),
                          key=lambda x: -x[1])
            diff = [(k, v) for k, v in diff if v > 0][:4]
            log("[vault-wait] " + (" · ".join(f"{k} {v}ms" for k, v in diff)
                                   or "no session waits recorded (CPU-bound?)"))
    return rows


# ── placement ─────────────────────────────────────────────────────────────────
def _same_content(a, b):
    """True if two files are byte-identical per the scan's SHA-1 fingerprint
    (cheap for large files: size + 1 MB head + 1 MB tail). Falls back to size
    equality only if a file can't be read (locked / online-only)."""
    try:
        from dataview.core.fingerprint import file_fingerprint
        fa = file_fingerprint(a, os.path.getsize(a))
        fb = file_fingerprint(b, os.path.getsize(b))
        return bool(fa) and fa == fb
    except Exception:
        try:
            return os.path.getsize(a) == os.path.getsize(b)
        except Exception:
            return False


def place(src, dst, mode):
    if os.path.exists(dst):
        # Same bytes already at the target name → nothing to do.
        if _same_content(src, dst):
            return "exists"
        # Name taken by *different* content: look for a numbered sibling that
        # already holds these exact bytes before minting a new one — otherwise a
        # re-run would pile up file_1, file_2, … copies of identical content.
        base, e = os.path.splitext(dst)
        n = 1
        while os.path.exists(f"{base}_{n}{e}"):
            if _same_content(src, f"{base}_{n}{e}"):
                return "exists"
            n += 1
        dst = f"{base}_{n}{e}"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if mode == "symlink":
        os.symlink(src, dst)
    elif mode == "hardlink":
        os.link(src, dst)
    else:
        shutil.copy2(src, dst)
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--schema", default="file_catalog")
    ap.add_argument("--vault-root", required=True)
    ap.add_argument("--tier", default="curated")
    ap.add_argument("--mode", choices=["copy", "symlink", "hardlink"],
                    default="copy")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--apply", action="store_true",
                    help="actually place files (default is dry-run)")
    a = ap.parse_args()

    from sqlalchemy import create_engine
    eng = create_engine(
        f"mssql+pyodbc://@{a.server}/{a.database}"
        "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes")

    with eng.connect() as con:
        rows = fetch_rows(con, a.schema, a.limit)
    print(f"[READ] {len(rows):,} cataloged file(s)")

    tier_root = os.path.join(a.vault_root, a.tier)
    plan, sidecars_carried = build_plan(rows, tier_root)
    missing = sum(1 for src, _, _ in plan if not os.path.exists(src))
    skipped_reject = sum(1 for r in rows
                         if (r.get("value_tier") or "").upper() == "REJECT")

    # summary by top bucket
    from collections import Counter
    buckets = Counter(p[2].split("/")[0] for p in plan)
    print(f"[PLAN] {len(plan):,} placements "
          f"({sidecars_carried:,} sidecars carried, "
          f"{skipped_reject:,} REJECT skipped, {missing:,} source missing)")
    for b, n in buckets.most_common():
        print(f"         {b:14} {n:,}")

    plan_csv = os.path.join(a.vault_root, "vault_plan.csv")
    os.makedirs(a.vault_root, exist_ok=True)
    with open(plan_csv, "w", newline="", encoding="utf-8") as f:
        wtr = csv.writer(f)
        wtr.writerow(["source", "dest", "bucket"])
        wtr.writerows(plan)
    print(f"[PLAN] written -> {plan_csv}")

    if not a.apply:
        print("[DRY-RUN] no files moved. Re-run with --apply to materialize.")
        return

    placed = exists = failed = 0
    for src, dst, _ in plan:
        try:
            r = place(src, dst, a.mode)
            placed += (r == "ok")
            exists += (r == "exists")
        except Exception as e:
            failed += 1
            if failed <= 20:
                print(f"   FAIL {os.path.basename(src)}: {e}")
    print(f"[APPLY] {placed:,} placed, {exists:,} already present, "
          f"{failed:,} failed ({a.mode}) -> {tier_root}")


if __name__ == "__main__":
    main()
