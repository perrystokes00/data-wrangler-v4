"""
page_import_witsml.py — DataView WITSML file loader
====================================================
Loads WITSML 1.3.1 / 1.4.1 files into the DataView schema.

Object type → target tables:
  trajectory  → dv_well_dir_srvy_hdr + dv_well_dir_srvy_sta
  log         → dv_well_log + dv_well_log_curve
  mudLog      → dv_well_mud_log
  well        → dv_well
  wellbore    → dv_well

ID convention — matches the rest of the DataView pipeline:
  ba_id    = SHA1(UPPER(company_name))   — 40 hex chars
  field_id = SHA1(UPPER(field_name))     — 40 hex chars
  survey_id, log_id etc. use UUID5 for idempotent re-runs.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import streamlit as st


# ── Shared utilities ──────────────────────────────────────────────────────────

def _sha1_id(value: str) -> str:
    """40-char SHA1 hex of UPPER(STRIP(value)) — matches pipeline convention."""
    return hashlib.sha1(value.upper().strip().encode("utf-8")).hexdigest()

def _uid(*parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(str(p) for p in parts))).upper()

def _trunc(val, n: int) -> Optional[str]:
    if val is None: return None
    s = str(val).strip(); return s[:n] if s else None

def _safe_float(val) -> Optional[float]:
    try: return float(val)
    except (TypeError, ValueError): return None

def _safe_date(val) -> Optional[str]:
    if not val: return None
    s = str(val).strip()[:10]; return s if len(s) == 10 else None


# ── FK seed helpers ───────────────────────────────────────────────────────────

def _ensure_source(con, source: str) -> None:
    from sqlalchemy import text
    con.execute(text("""
        MERGE dataview.dv_r_source AS tgt
        USING (SELECT :src AS source) s ON tgt.source = s.source
        WHEN NOT MATCHED THEN INSERT (
            source, short_name, long_name, active_ind,
            row_created_by, row_created_date
        ) VALUES (:src, :src, :long, 'Y', 'DV_LOADER', GETUTCDATE());
    """), {"src": _trunc(source, 40),
           "long": _trunc(f"DataView importer — {source}", 255)})

def _ensure_ba(con, name: str, source: str) -> str:
    """BA: id = SHA1(UPPER(name)), name stored as-is."""
    from sqlalchemy import text
    if not name or not name.strip(): name = "UNKNOWN"
    ba_id = _sha1_id(name)
    con.execute(text("""
        MERGE dataview.dv_business_associate AS tgt
        USING (SELECT :id AS ba_id) src ON tgt.ba_id = src.ba_id
        WHEN NOT MATCHED THEN INSERT (
            ba_id, ba_name, ba_type, active_ind,
            row_created_by, row_created_date, source
        ) VALUES (:id, :name, 'COMPANY', 'Y',
                  'WITSML_LOADER', GETUTCDATE(), :src);
    """), {"id": ba_id, "name": _trunc(name, 255), "src": _trunc(source, 40)})
    return ba_id

def _ensure_field(con, field_name: str, source: str,
                  op_ba_id: str = None) -> str:
    """Field: id = SHA1(UPPER(field_name))."""
    from sqlalchemy import text
    if not field_name or not field_name.strip(): field_name = "UNKNOWN"
    fid = _sha1_id(field_name)
    con.execute(text("""
        MERGE dataview.dv_field AS tgt
        USING (SELECT :fid AS field_id) src ON tgt.field_id = src.field_id
        WHEN NOT MATCHED THEN INSERT (
            field_id, field_name, operator_ba_id, active_ind,
            row_created_by, row_created_date, source
        ) VALUES (:fid, :fname, :oba, 'Y',
                  'WITSML_LOADER', GETUTCDATE(), :src);
    """), {"fid": fid, "fname": _trunc(field_name, 255),
           "oba": op_ba_id, "src": _trunc(source, 40)})
    return fid

def _ensure_well(con, uwi: str, well_name: str, source: str,
                 field_id: str = None, op_ba_id: str = None) -> None:
    from sqlalchemy import text
    con.execute(text("""
        MERGE dataview.dv_well AS tgt
        USING (SELECT :uwi AS uwi) src ON tgt.uwi = src.uwi
        WHEN NOT MATCHED THEN INSERT (
            uwi, well_name, field_id, operator_ba_id,
            active_ind, row_created_by, row_created_date, source
        ) VALUES (:uwi, :name, :fid, :oba, 'Y',
                  'WITSML_LOADER', GETUTCDATE(), :src)
        WHEN MATCHED THEN UPDATE SET
            well_name      = COALESCE(tgt.well_name, :name),
            field_id       = COALESCE(tgt.field_id, :fid),
            operator_ba_id = COALESCE(tgt.operator_ba_id, :oba),
            row_changed_by = 'WITSML_LOADER',
            row_changed_date = GETUTCDATE();
    """), {"uwi": _trunc(uwi, 40), "name": _trunc(well_name or uwi, 255),
           "fid": field_id, "oba": op_ba_id, "src": _trunc(source, 40)})


# ══════════════════════════════════════════════════════════════════════════════
# XML parsers
# ══════════════════════════════════════════════════════════════════════════════

def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag

def _find_text(el: ET.Element, *tags) -> Optional[str]:
    tags_l = [t.lower() for t in tags]
    for child in el:
        if _strip_ns(child.tag).lower() in tags_l:
            v = (child.text or "").strip()
            return v if v else None
    return None

def _iter_tag(root: ET.Element, tag: str):
    tag_l = tag.lower()
    for el in root.iter():
        if _strip_ns(el.tag).lower() == tag_l:
            yield el


def _parse_trajectory(file_path: str) -> tuple:
    tree = ET.parse(file_path)
    root = tree.getroot()
    hdr = {}; stations = []
    for traj in _iter_tag(root, "trajectory"):
        hdr["uwi"]       = traj.get("uidWell") or _find_text(traj, "uidWell") or ""
        hdr["well_name"] = _find_text(traj, "nameWell")
        hdr["survey_type"] = _find_text(traj, "typeSurveyTool", "typeTrajectory")
        hdr["survey_date"] = _safe_date(_find_text(traj, "dTimTrajStart") or
                                         _find_text(traj, "dTimTrajEnd"))
        common = next((c for c in traj if _strip_ns(c.tag).lower()=="commondata"), None)
        hdr["contractor"] = _find_text(common, "sourceName") if common else None

        mds = []
        for sta in _iter_tag(traj, "trajectoryStation"):
            def _fval(tag):
                el = next((c for c in sta if _strip_ns(c.tag).lower()==tag), None)
                return _safe_float(el.text if el is not None else None)
            md = _fval("md")
            if md is not None: mds.append(md)
            stations.append({
                "uid":  sta.get("uid") or str(len(stations)+1),
                "md":   md,
                "incl": _fval("incl"),
                "azim": _fval("azi") or _fval("azimuth"),
                "tvd":  _fval("tvd"),
                "dls":  _fval("dls"),
            })
        if mds:
            hdr["top_depth"]  = min(mds)
            hdr["base_depth"] = max(mds)
        break
    return hdr, stations


def _parse_log(file_path: str) -> tuple:
    tree = ET.parse(file_path)
    root = tree.getroot()
    hdr = {}; curves = []
    for log in _iter_tag(root, "log"):
        hdr["uwi"]       = log.get("uidWell") or _find_text(log, "uidWell") or ""
        hdr["well_name"] = _find_text(log, "nameWell")
        hdr["contractor"]= _find_text(log, "serviceCompany")
        hdr["run_num"]   = _find_text(log, "runNumber")
        hdr["top_depth"] = _safe_float(_find_text(log, "startIndex"))
        hdr["base_depth"]= _safe_float(_find_text(log, "endIndex"))

        for ci in _iter_tag(log, "logCurveInfo"):
            mn = _find_text(ci, "mnemonic")
            if mn and mn.upper() not in ("DEPT","DEPTH","MD","INDEX"):
                curves.append({
                    "mnemonic": mn,
                    "unit":     _find_text(ci, "unit"),
                    "desc":     _find_text(ci, "curveDescription"),
                    "top":      _safe_float(_find_text(ci, "minIndex")),
                    "base":     _safe_float(_find_text(ci, "maxIndex")),
                })
        if not curves:
            for ld in _iter_tag(log, "logData"):
                ml = _find_text(ld, "mnemonicList")
                if ml:
                    for mn in ml.split(","):
                        mn = mn.strip()
                        if mn and mn.upper() not in ("DEPT","DEPTH","MD"):
                            curves.append({"mnemonic":mn,"unit":None,
                                           "desc":None,"top":None,"base":None})
        break
    return hdr, curves


def _parse_mudlog(file_path: str) -> dict:
    tree = ET.parse(file_path)
    root = tree.getroot()
    hdr  = {}
    for ml in _iter_tag(root, "mudLog"):
        hdr["uwi"]       = ml.get("uidWell") or _find_text(ml, "uidWell") or ""
        hdr["well_name"] = _find_text(ml, "nameWell")
        hdr["contractor"]= _find_text(ml, "mudLogCompany")
        hdr["top_depth"] = _safe_float(_find_text(ml, "startMd"))
        hdr["base_depth"]= _safe_float(_find_text(ml, "endMd"))
        common = next((c for c in ml if _strip_ns(c.tag).lower()=="commondata"), None)
        hdr["log_date"]  = _safe_date(
            _find_text(ml, "dTim") or
            (_find_text(common, "dTimCreation") if common else None))
        break
    return hdr


def _parse_well(file_path: str) -> dict:
    tree = ET.parse(file_path)
    root = tree.getroot()
    hdr  = {}
    for tag in ("well","wellbore"):
        for el in _iter_tag(root, tag):
            hdr["uwi"]       = el.get("uid") or el.get("uidWell") or ""
            hdr["well_name"] = _find_text(el, "name")
            hdr["operator"]  = _find_text(el, "operator", "operatorDiv")
            hdr["field"]     = _find_text(el, "field")
            hdr["state"]     = _find_text(el, "state","provState")
            hdr["county"]    = _find_text(el, "county")
            hdr["spud_date"] = _safe_date(_find_text(el, "dTimSpud"))
            wl = next((c for c in el if _strip_ns(c.tag).lower()=="welllocation"),None)
            if wl:
                hdr["lat"] = _safe_float(_find_text(wl, "latitude"))
                hdr["lon"] = _safe_float(_find_text(wl, "longitude"))
            break
        if hdr: break
    return hdr


# ══════════════════════════════════════════════════════════════════════════════
# Database loaders
# ══════════════════════════════════════════════════════════════════════════════

def _db_trajectory(engine, hdr: dict, stations: list, source: str) -> dict:
    from sqlalchemy import text
    stats   = {"header":0,"stations":0,"skipped":0}
    uwi     = _trunc(hdr.get("uwi"), 40)
    sid     = _uid("TRAJ", uwi, hdr.get("survey_date",""), source)

    with engine.begin() as con:
        _ensure_source(con, source)
        cba = _ensure_ba(con, hdr.get("contractor"), source) \
              if hdr.get("contractor") else None
        _ensure_well(con, uwi, hdr.get("well_name"), source)

        con.execute(text("""
            MERGE dataview.dv_well_dir_srvy_hdr AS tgt
            USING (SELECT :sid AS survey_id, :uwi AS uwi) src
              ON tgt.uwi=src.uwi AND tgt.survey_id=src.survey_id
            WHEN NOT MATCHED THEN INSERT (
                uwi,survey_id,survey_type,survey_date,contractor_ba_id,
                survey_top_depth,survey_base_depth,depth_ouom,active_ind,
                row_created_by,row_created_date,source
            ) VALUES (
                :uwi,:sid,:stype,:sdate,:cba,
                :top,:base,'ft','Y',
                'WITSML_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                survey_type=COALESCE(tgt.survey_type,:stype),
                contractor_ba_id=COALESCE(tgt.contractor_ba_id,:cba),
                row_changed_by='WITSML_LOADER',row_changed_date=GETUTCDATE();
        """), {"uwi":uwi,"sid":_trunc(sid,40),"stype":_trunc(hdr.get("survey_type"),40),
               "sdate":hdr.get("survey_date"),"cba":cba,
               "top":hdr.get("top_depth"),"base":hdr.get("base_depth"),
               "src":_trunc(source,40)})
        stats["header"] = 1

        for sta in stations:
            sta_id = _trunc(sta.get("uid") or str(stats["stations"]+1), 40)
            try:
                con.execute(text("""
                    MERGE dataview.dv_well_dir_srvy_sta AS tgt
                    USING (SELECT :uwi AS uwi,:sid AS survey_id,
                                  :staid AS station_id) src
                      ON tgt.uwi=src.uwi AND tgt.survey_id=src.survey_id
                         AND tgt.station_id=src.station_id
                    WHEN NOT MATCHED THEN INSERT (
                        uwi,survey_id,station_id,
                        md,incl,azim,tvd,dls,depth_ouom,
                        row_created_by,row_created_date,source
                    ) VALUES (
                        :uwi,:sid,:staid,
                        :md,:incl,:azim,:tvd,:dls,'ft',
                        'WITSML_LOADER',GETUTCDATE(),:src
                    )
                    WHEN MATCHED THEN UPDATE SET
                        md=:md,incl=:incl,azim=:azim,tvd=:tvd,dls=:dls;
                """), {"uwi":uwi,"sid":_trunc(sid,40),"staid":sta_id,
                       "md":sta.get("md"),"incl":sta.get("incl"),
                       "azim":sta.get("azim"),"tvd":sta.get("tvd"),
                       "dls":sta.get("dls"),"src":_trunc(source,40)})
                stats["stations"] += 1
            except Exception:
                stats["skipped"] += 1
    return stats


def _db_log(engine, hdr: dict, curves: list, source: str) -> dict:
    from sqlalchemy import text
    stats  = {"logs":0,"curves":0,"skipped":0}
    uwi    = _trunc(hdr.get("uwi"), 40)
    log_id = _uid("LOG", uwi, hdr.get("run_num",""),
                  str(hdr.get("top_depth","")), source)

    with engine.begin() as con:
        _ensure_source(con, source)
        sba = _ensure_ba(con, hdr.get("contractor"), source) \
              if hdr.get("contractor") else None
        _ensure_well(con, uwi, hdr.get("well_name"), source)

        con.execute(text("""
            MERGE dataview.dv_well_log AS tgt
            USING (SELECT :lid AS log_id,:uwi AS uwi) src
              ON tgt.uwi=src.uwi AND tgt.log_id=src.log_id
            WHEN NOT MATCHED THEN INSERT (
                uwi,log_id,log_type,run_num,service_company_ba_id,
                top_depth,base_depth,depth_ouom,file_format,active_ind,
                row_created_by,row_created_date,source
            ) VALUES (
                :uwi,:lid,'WIRELINE',:run,:sba,
                :top,:base,'ft','WITSML','Y',
                'WITSML_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                top_depth=COALESCE(tgt.top_depth,:top),
                base_depth=COALESCE(tgt.base_depth,:base),
                service_company_ba_id=COALESCE(tgt.service_company_ba_id,:sba),
                row_changed_by='WITSML_LOADER',row_changed_date=GETUTCDATE();
        """), {"uwi":uwi,"lid":_trunc(log_id,40),"run":_trunc(hdr.get("run_num"),10),
               "sba":sba,"top":hdr.get("top_depth"),"base":hdr.get("base_depth"),
               "src":_trunc(source,40)})
        stats["logs"] = 1

        for crv in curves:
            mn  = crv.get("mnemonic") or ""
            cid = _uid("CURVE", uwi, log_id, mn)
            try:
                con.execute(text("""
                    MERGE dataview.dv_well_log_curve AS tgt
                    USING (SELECT :uwi AS uwi,:lid AS log_id,
                                  :cid AS curve_id) src
                      ON tgt.uwi=src.uwi AND tgt.log_id=src.log_id
                         AND tgt.curve_id=src.curve_id
                    WHEN NOT MATCHED THEN INSERT (
                        uwi,log_id,curve_id,mnemonic,
                        curve_description,curve_unit,
                        top_depth,base_depth,depth_ouom,active_ind,
                        row_created_by,row_created_date,source
                    ) VALUES (
                        :uwi,:lid,:cid,:mn,
                        :desc,:unit,
                        :top,:base,'ft','Y',
                        'WITSML_LOADER',GETUTCDATE(),:src
                    );
                """), {"uwi":uwi,"lid":_trunc(log_id,40),"cid":_trunc(cid,40),
                       "mn":_trunc(mn,40),"desc":_trunc(crv.get("desc"),255),
                       "unit":_trunc(crv.get("unit"),40),
                       "top":crv.get("top"),"base":crv.get("base"),
                       "src":_trunc(source,40)})
                stats["curves"] += 1
            except Exception:
                stats["skipped"] += 1
    return stats


def _db_mudlog(engine, hdr: dict, source: str) -> dict:
    from sqlalchemy import text
    stats = {"mud_logs":0}
    uwi   = _trunc(hdr.get("uwi"), 40)
    ml_id = _uid("MUDLOG", uwi, hdr.get("log_date",""), source)

    with engine.begin() as con:
        _ensure_source(con, source)
        mlba = _ensure_ba(con, hdr.get("contractor"), source) \
               if hdr.get("contractor") else None
        _ensure_well(con, uwi, hdr.get("well_name"), source)

        # NOT NULL columns that WITSML doesn't supply:
        #   rop_avg → 0.0 (updated when mud report loaded)
        #   rop_ouom → 'm/hr'
        #   mud_type → 'UNKNOWN'
        #   mud_weight_avg → 0.0
        #   log_date → default to today if missing
        con.execute(text("""
            MERGE dataview.dv_well_mud_log AS tgt
            USING (SELECT :uwi AS uwi,:mlid AS mud_log_id) src
              ON tgt.uwi=src.uwi AND tgt.mud_log_id=src.mud_log_id
            WHEN NOT MATCHED THEN INSERT (
                uwi,mud_log_id,log_date,
                top_depth,base_depth,depth_ouom,
                mud_logger_ba_id,
                rop_avg,rop_ouom,
                mud_type,mud_weight_avg,
                active_ind,source,
                row_created_by,row_created_date
            ) VALUES (
                :uwi,:mlid,COALESCE(:ldate,CAST(GETUTCDATE() AS date)),
                :top,:base,'ft',
                :mlba,
                0.0,'m/hr',
                'UNKNOWN',0.0,
                'Y',:src,
                'WITSML_LOADER',GETUTCDATE()
            )
            WHEN MATCHED THEN UPDATE SET
                mud_logger_ba_id=COALESCE(tgt.mud_logger_ba_id,:mlba),
                row_changed_by='WITSML_LOADER',row_changed_date=GETUTCDATE();
        """), {"uwi":uwi,"mlid":_trunc(ml_id,40),
               "ldate":hdr.get("log_date"),
               "top":hdr.get("top_depth") or 0.0,
               "base":hdr.get("base_depth") or 0.0,
               "mlba":mlba,"src":_trunc(source,40)})
        stats["mud_logs"] = 1
    return stats


def _db_well(engine, hdr: dict, source: str) -> dict:
    from sqlalchemy import text
    stats = {"wells":0}
    uwi   = _trunc(hdr.get("uwi"), 40)
    op    = hdr.get("operator")
    field = hdr.get("field")

    with engine.begin() as con:
        _ensure_source(con, source)
        oba = _ensure_ba(con, op, source)    if op    else None
        fid = _ensure_field(con, field, source, op_ba_id=oba) \
              if field else None

        con.execute(text("""
            MERGE dataview.dv_well AS tgt
            USING (SELECT :uwi AS uwi) src ON tgt.uwi=src.uwi
            WHEN NOT MATCHED THEN INSERT (
                uwi,well_name,operator_ba_id,field_id,
                province_state,county,
                surface_latitude,surface_longitude,
                spud_date,active_ind,
                row_created_by,row_created_date,source
            ) VALUES (
                :uwi,:name,:oba,:fid,
                :state,:county,
                :lat,:lon,
                :spud,'Y',
                'WITSML_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                well_name=COALESCE(tgt.well_name,:name),
                operator_ba_id=COALESCE(tgt.operator_ba_id,:oba),
                field_id=COALESCE(tgt.field_id,:fid),
                province_state=COALESCE(tgt.province_state,:state),
                county=COALESCE(tgt.county,:county),
                surface_latitude=COALESCE(tgt.surface_latitude,:lat),
                surface_longitude=COALESCE(tgt.surface_longitude,:lon),
                spud_date=COALESCE(tgt.spud_date,:spud),
                row_changed_by='WITSML_LOADER',
                row_changed_date=GETUTCDATE();
        """), {"uwi":uwi,"name":_trunc(hdr.get("well_name") or uwi,255),
               "oba":oba,"fid":fid,
               "state":_trunc(hdr.get("state"),100),
               "county":_trunc(hdr.get("county"),100),
               "lat":hdr.get("lat"),"lon":hdr.get("lon"),
               "spud":hdr.get("spud_date"),
               "src":_trunc(source,40)})
        stats["wells"] = 1
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit render
# ══════════════════════════════════════════════════════════════════════════════

_OBJ_TARGETS = {
    "trajectory": "dv_well_dir_srvy_hdr  +  dv_well_dir_srvy_sta",
    "log":        "dv_well_log  +  dv_well_log_curve",
    "mudlog":     "dv_well_mud_log",
    "well":       "dv_well",
    "wellbore":   "dv_well",
}


def render(engine) -> None:
    st.caption(
        "Loads WITSML 1.3.1 / 1.4.1 XML files into the DataView schema. "
        "Idempotent — re-running the same file is safe. "
        "ba_id and field_id use **SHA1(UPPER(name))** matching the pipeline convention."
    )

    uploaded = st.file_uploader(
        "Drop WITSML file (.xml)", type=["xml"], key="witsml_loader_upload")
    source_label = st.text_input(
        "Source label", value="WITSML", key="witsml_source")

    if not uploaded:
        st.info("Drop a WITSML .xml file above to begin.")
        return

    with tempfile.NamedTemporaryFile(
            delete=False, suffix=".xml", prefix="witsml_") as tmp:
        tmp.write(uploaded.getbuffer())
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as fh:
            if b"witsml.org/schemas" not in fh.read(500):
                st.error("Not a WITSML file — witsml.org namespace not found.")
                return

        from dataview.file_catalog.witsml_catalog import classify_witsml
        cl       = classify_witsml(tmp_path)
        obj_type = (cl.get("object_type") or "").lower()
        uwi      = (cl.get("uwi") or "").strip()

        c1, c2, c3 = st.columns(3)
        c1.metric("Object type", obj_type or "—")
        c2.metric("Well / UWI",  _trunc(cl.get("well_name") or uwi, 28) or "—")
        c3.metric("Confidence",  f"{cl.get('confidence', 0):.0%}")
        if cl.get("description"): st.caption(cl["description"])
        if cl.get("error"):       st.warning(f"Note: {cl['error']}")

        if not uwi:
            uwi = st.text_input(
                "UWI / API — not found in file, enter manually",
                key="witsml_uwi_override").strip()
            if not uwi:
                st.warning("A UWI is required to load this file.")
                return

        target = _OBJ_TARGETS.get(obj_type)
        if not target:
            st.warning(
                f"Object type **{obj_type or 'unknown'}** is not yet supported. "
                "Supported: trajectory, log, mudLog, well, wellbore.")
            return

        st.info(f"**Target tables:** `{target}`   ·   **UWI:** `{uwi}`")

        if not st.button("🚀 Load into DataView", type="primary",
                         key="witsml_load_btn", use_container_width=True):
            return

        src = (source_label or "WITSML").strip()

        with st.spinner(f"Parsing {uploaded.name}…"):
            try:
                if obj_type == "trajectory":
                    hdr, stations = _parse_trajectory(tmp_path)
                    hdr.setdefault("uwi", uwi)
                elif obj_type == "log":
                    hdr, curves = _parse_log(tmp_path)
                    hdr.setdefault("uwi", uwi)
                elif obj_type == "mudlog":
                    hdr = _parse_mudlog(tmp_path)
                    hdr.setdefault("uwi", uwi)
                else:
                    hdr = _parse_well(tmp_path)
                    hdr.setdefault("uwi", uwi)
            except Exception as e:
                st.error(f"Parse error: {type(e).__name__}: {e}")
                return

        with st.spinner("Writing to DataView…"):
            try:
                if obj_type == "trajectory":
                    stats = _db_trajectory(engine, hdr, stations, src)
                    st.caption(f"Loaded {len(stations)} stations from file.")
                elif obj_type == "log":
                    stats = _db_log(engine, hdr, curves, src)
                    st.caption(f"Loaded {len(curves)} curve definitions from file.")
                elif obj_type == "mudlog":
                    stats = _db_mudlog(engine, hdr, src)
                else:
                    stats = _db_well(engine, hdr, src)
            except Exception as e:
                st.error(f"Load failed: {type(e).__name__}: {e}")
                return

        st.success("✅ Load complete.")
        cols = st.columns(len(stats))
        for col, (k, v) in zip(cols, stats.items()):
            col.metric(k.replace("_"," ").title(), f"{v:,}")

    finally:
        try: os.unlink(tmp_path)
        except Exception: pass
