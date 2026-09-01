"""
modules/doc_catalog_store.py
============================
Disk-catalog any document type into file_catalog.GLOBAL_FILE_CATALOG.

Supports:
  PDF  — directional surveys, formation tops, core data, DST, mud logs
  SHP  — well locations, field boundaries, seismic lines, pipelines etc.
  XLSX — any Excel workbook
  DOCX — any Word document

One record per physical file.  Extra columns are added to
GLOBAL_FILE_CATALOG on first use (idempotent ALTER TABLE).
"""
from __future__ import annotations
import uuid
from pathlib import Path
from sqlalchemy import text


# ── Dialect helpers ───────────────────────────────────────────────────────────

def _now(dialect: str) -> str:
    return {
        "oracle":    "SYSTIMESTAMP",
        "snowflake": "CURRENT_TIMESTAMP()",
    }.get(dialect, "GETDATE()")


def _tbl(dialect: str) -> str:
    if dialect == "oracle":    return "FILE_CATALOG_GLOBAL_FILE_CATALOG"
    if dialect == "snowflake": return '"FILE_CATALOG"."GLOBAL_FILE_CATALOG"'
    return "file_catalog.GLOBAL_FILE_CATALOG"


# ── PPDM targets by report type ───────────────────────────────────────────────
PPDM_TARGETS = {
    # PDF types
    "DIRECTIONAL_SURVEY": "WELL_DIR_SURVEY + WELL_DIR_SRVY_STATION",
    "FORMATION_TOPS":     "WELL_FORMATION",
    "CORE":               "WELL_CORE + WELL_CORE_ANALYSIS",
    "DST":                "WELL_TEST + WELL_TEST_RESULT",
    "MUD_LOG":            "—",
    "COMPLETION_REPORT":  "—",
    # Shapefile types
    "SHP_WELL":           "WELL",
    "SHP_FIELD":          "FIELD",
    "SHP_LEASE":          "LAND_SECTION",
    "SHP_SEISMIC_2D":     "SEIS_SET + SEIS_LINE",
    "SHP_SEISMIC_3D":     "SEIS_SET",
    "SHP_PIPELINE":       "FACILITY",
    "SHP_FACILITY":       "FACILITY",
    "SHP_BOUNDARY":       "—",
    # Office types
    "EXCEL":              "—",
    "WORD":               "—",
    # Fallback
    "UNKNOWN":            "—",
}

# Human-readable labels
DOC_LABELS = {
    "DIRECTIONAL_SURVEY": "Directional Survey",
    "FORMATION_TOPS":     "Formation Tops",
    "CORE":               "Core Data",
    "DST":                "Drill Stem Test",
    "MUD_LOG":            "Mud Log",
    "COMPLETION_REPORT":  "Completion Report",
    "SHP_WELL":           "Well Locations (SHP)",
    "SHP_FIELD":          "Field Boundaries (SHP)",
    "SHP_LEASE":          "Lease / Tract (SHP)",
    "SHP_SEISMIC_2D":     "Seismic 2D Lines (SHP)",
    "SHP_SEISMIC_3D":     "Seismic 3D Survey (SHP)",
    "SHP_PIPELINE":       "Pipeline (SHP)",
    "SHP_FACILITY":       "Facility (SHP)",
    "SHP_BOUNDARY":       "Boundary (SHP)",
    "EXCEL":              "Excel Workbook",
    "WORD":               "Word Document",
    "UNKNOWN":            "Document",
}

# Extra columns to add to GLOBAL_FILE_CATALOG
_EXTRA_COLS_MSSQL = {
    "DOC_TYPE":           "NVARCHAR(40)",
    "REPORT_TYPE":        "NVARCHAR(40)",
    "WELL_NAME":          "NVARCHAR(255)",
    "UWI":                "NVARCHAR(40)",
    "OPERATOR":           "NVARCHAR(255)",
    "PAGE_COUNT":         "INT",
    "RECORD_COUNT":       "INT",
    "SUMMARY_DESCRIPTION":"NVARCHAR(1000)",
    "PPDM_LOADED_IND":    "CHAR(1) DEFAULT 'N'",
    "PPDM_TABLE_TARGET":  "NVARCHAR(255)",
}


# ══════════════════════════════════════════════════════════════════════════════
# Schema bootstrap
# ══════════════════════════════════════════════════════════════════════════════

def ensure_doc_catalog_columns(engine, dialect: str = "mssql") -> list[str]:
    """Add doc-specific columns to GLOBAL_FILE_CATALOG if missing. Idempotent."""
    if dialect != "mssql":
        return []   # Oracle/Snowflake DDL handled separately
    added = []
    try:
        with engine.connect() as con:
            existing = {
                row[0].upper()
                for row in con.execute(text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = 'file_catalog' "
                    "  AND TABLE_NAME   = 'GLOBAL_FILE_CATALOG'"
                ))
            }
        tbl = _tbl(dialect)
        with engine.begin() as con:
            for col, typedef in _EXTRA_COLS_MSSQL.items():
                if col not in existing:
                    con.execute(text(f"ALTER TABLE {tbl} ADD {col} {typedef}"))
                    added.append(col)
    except Exception:
        pass
    return added


# ══════════════════════════════════════════════════════════════════════════════
# Summary builder
# ══════════════════════════════════════════════════════════════════════════════

def build_summary(doc_type: str, meta: dict, records: list | None = None) -> str:
    """
    Build a one-line human-readable summary for the catalog record.

    meta keys used (all optional):
      well_name, uwi, operator, field,
      feature_count, geometry_type,        ← shapefile
      sheet_count, row_count,              ← Excel
      word_count, section_count,           ← Word
      ISIP,                                ← DST
    records: extracted data rows (used for counts/ranges)
    """
    records = records or []
    label   = DOC_LABELS.get(doc_type, doc_type)
    parts   = [label]

    # Well identification
    wn = meta.get("well_name") or meta.get("WELL_NAME")
    if wn:
        parts.append(wn)

    # Type-specific detail
    if doc_type == "DIRECTIONAL_SURVEY" and records:
        parts.append(f"{len(records)} stations")
        mds = [r.get("MD") for r in records if r.get("MD") is not None]
        if mds:
            parts.append(f"MD {min(mds):.0f}–{max(mds):.0f} ft")

    elif doc_type == "FORMATION_TOPS" and records:
        parts.append(f"{len(records)} formations")
        depths = [r.get("DEPTH_TOP_MD") for r in records
                  if r.get("DEPTH_TOP_MD") is not None]
        if depths:
            parts.append(f"TD {max(depths):.0f} ft")

    elif doc_type == "CORE" and records:
        parts.append(f"{len(records)} samples")
        depths = [r.get("DEPTH") or r.get("DEPTH_TOP")
                  for r in records if r.get("DEPTH") or r.get("DEPTH_TOP")]
        if depths:
            parts.append(f"{min(depths):.0f}–{max(depths):.0f} ft")

    elif doc_type == "DST" and records:
        parts.append(f"{len(records)} pressure points")
        isip = meta.get("ISIP")
        if isip:
            parts.append(f"ISIP {isip} psi")

    elif doc_type.startswith("SHP_"):
        fc = meta.get("feature_count")
        gt = meta.get("geometry_type")
        if fc:
            parts.append(f"{fc:,} features")
        if gt:
            parts.append(gt)
        op = meta.get("operator") or meta.get("OPERATOR")
        if op:
            parts.append(op)

    elif doc_type == "EXCEL":
        sc = meta.get("sheet_count")
        rc = meta.get("row_count")
        if sc:
            parts.append(f"{sc} sheet(s)")
        if rc:
            parts.append(f"{rc:,} rows")

    elif doc_type == "WORD":
        wc = meta.get("word_count")
        sc = meta.get("section_count")
        if wc:
            parts.append(f"{wc:,} words")
        if sc:
            parts.append(f"{sc} sections")

    elif records:
        parts.append(f"{len(records)} records")

    return " · ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Main catalog function
# ══════════════════════════════════════════════════════════════════════════════

def catalog_document(
    engine,
    dialect: str,
    file_path: str,
    doc_type: str,
    meta: dict,
    records: list | None = None,
    source: str = "DOC_CATALOG",
    ppdm_loaded: bool = False,
) -> dict:
    """
    Insert or update a GLOBAL_FILE_CATALOG record for any document.

    Parameters
    ----------
    engine      : SQLAlchemy engine
    dialect     : "mssql" | "oracle" | "snowflake"
    file_path   : absolute path to the file
    doc_type    : one of the keys in PPDM_TARGETS / DOC_LABELS
    meta        : dict of well_name, uwi, operator, feature_count, etc.
    records     : extracted data rows (for counts / summary ranges)
    source      : SOURCE value written to the catalog row
    ppdm_loaded : set True after a successful PPDM load

    Returns
    -------
    {"ok": bool, "inventory_id": str, "action": "INSERT"|"UPDATE",
     "summary": str, "error": str|None}
    """
    records  = records or []
    result   = {"ok": False, "inventory_id": None,
                "action": None, "summary": None, "error": None}

    fp   = str(Path(file_path).absolute())
    name = Path(file_path).name
    ext  = Path(file_path).suffix.lower()
    size = round(Path(file_path).stat().st_size / 1024, 1) \
           if Path(file_path).exists() else 0

    summary  = build_summary(doc_type, meta, records)
    ppdm_tbl = PPDM_TARGETS.get(doc_type, "—")
    tbl      = _tbl(dialect)
    now      = _now(dialect)

    result["summary"] = summary

    try:
        ensure_doc_catalog_columns(engine, dialect)

        with engine.begin() as con:
            existing = con.execute(text(
                f"SELECT INVENTORY_ID FROM {tbl} WHERE FILE_PATH = :fp"
            ), {"fp": fp}).fetchone()

            common = {
                "rt":  doc_type[:40],
                "wn":  (meta.get("well_name") or meta.get("WELL_NAME") or "")[:255],
                "uwi": (meta.get("uwi") or meta.get("UWI") or "")[:40],
                "op":  (meta.get("operator") or meta.get("OPERATOR") or "")[:255],
                "pc":  meta.get("page_count", 0) or 0,
                "rc":  len(records),
                "sd":  summary[:1000],
                "pl":  "Y" if ppdm_loaded else "N",
                "pt":  ppdm_tbl[:255],
            }

            if existing:
                inv_id = existing[0]
                con.execute(text(f"""
                    UPDATE {tbl} SET
                        DOC_TYPE            = :rt,
                        REPORT_TYPE         = :rt,
                        WELL_NAME           = :wn,
                        UWI                 = :uwi,
                        OPERATOR            = :op,
                        PAGE_COUNT          = :pc,
                        RECORD_COUNT        = :rc,
                        SUMMARY_DESCRIPTION = :sd,
                        PPDM_LOADED_IND     = :pl,
                        PPDM_TABLE_TARGET   = :pt,
                        CATALOG_STATUS      = 'CATALOGED',
                        ROW_CHANGED_DATE    = {now}
                    WHERE INVENTORY_ID = :iid
                """), {**common, "iid": inv_id})
                result.update({"ok": True, "inventory_id": inv_id,
                               "action": "UPDATE"})
            else:
                inv_id = uuid.uuid4().hex[:40].upper()
                con.execute(text(f"""
                    INSERT INTO {tbl} (
                        INVENTORY_ID, FILE_PATH, FILE_NAME, FILE_EXT,
                        FILE_SIZE_KB, DOC_TYPE, REPORT_TYPE,
                        WELL_NAME, UWI, OPERATOR,
                        PAGE_COUNT, RECORD_COUNT,
                        SUMMARY_DESCRIPTION, CATALOG_STATUS,
                        PPDM_LOADED_IND, PPDM_TABLE_TARGET,
                        SOURCE, ROW_CREATED_BY,
                        ROW_CREATED_DATE, ROW_CHANGED_DATE
                    ) VALUES (
                        :iid, :fp, :fn, :ext,
                        :sz, :rt, :rt,
                        :wn, :uwi, :op,
                        :pc, :rc,
                        :sd, 'CATALOGED',
                        :pl, :pt,
                        :src, 'DataWrangler',
                        {now}, {now}
                    )
                """), {**common,
                       "iid": inv_id, "fp": fp, "fn": name[:255],
                       "ext": ext, "sz": size, "src": source[:40]})
                result.update({"ok": True, "inventory_id": inv_id,
                               "action": "INSERT"})

    except Exception as e:
        result["error"] = str(e)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit helper — drop this widget anywhere after extraction
# ══════════════════════════════════════════════════════════════════════════════

def render_catalog_widget(
    file_path: str,
    doc_type: str,
    meta: dict,
    records: list | None = None,
    widget_key: str = "",
    source: str = "DOC_CATALOG",
):
    """
    Render an optional 'Catalog this document' checkbox + button.
    Call this after any successful extraction — PDF, SHP, XLSX, DOCX.

    Requires st.session_state["engine"] and st.session_state["dialect"].
    """
    import streamlit as st

    engine  = st.session_state.get("engine")
    dialect = st.session_state.get("dialect", "mssql")
    records = records or []

    summary = build_summary(doc_type, meta, records)
    label   = DOC_LABELS.get(doc_type, doc_type)

    st.divider()
    st.markdown("**📂 File Inventory**")
    st.caption(f"*{summary}*")

    _key = f"doc_cat_chk_{widget_key or Path(file_path).stem}"
    do_cat = st.checkbox(
        f"Catalog this {label} in File Inventory",
        value=False,
        key=_key)

    if do_cat:
        if engine is None:
            st.warning("No database connection — connect via the pipeline first.")
            return

        _btn_key = f"doc_cat_btn_{widget_key or Path(file_path).stem}"
        if st.button("📂 Save to Inventory", key=_btn_key, type="primary"):
            with st.spinner("Cataloging…"):
                r = catalog_document(
                    engine=engine,
                    dialect=dialect,
                    file_path=file_path,
                    doc_type=doc_type,
                    meta=meta,
                    records=records,
                    source=source,
                    ppdm_loaded=False,
                )
            if r["ok"]:
                st.success(
                    f"✅ {r['action'].title()}ed in File Inventory — *{summary}*"
                )
            else:
                st.error(f"Catalog failed: {r['error']}")
