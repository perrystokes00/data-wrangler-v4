"""
page_import_osdu.py — DataView OSDU JSON file loader
=====================================================
Loads OSDU JSON files into the DataView schema.
Detects the OSDU kind automatically from the 'kind' field.

Schema → target tables:
  osdu_well / osdu_wellbore  → dv_well
  osdu_well_log              → dv_well_log + dv_well_log_curve
  osdu_trajectory            → dv_well_dir_srvy_hdr + dv_well_dir_srvy_sta
  osdu_marker_set            → dv_well_formation_top
  osdu_pressure              → dv_well_dst + dv_well_dst_period
  osdu_production            → dv_prod_entity + dv_prod_volume
  osdu_seismic               → dv_seis_set
  osdu_field                 → dv_field
  (completion/core/scal/horizon/reservoir/document — stub, not yet loaded)

ID convention — matches the DataView pipeline:
  ba_id    = SHA1(UPPER(company_name))
  field_id = SHA1(UPPER(field_name))
"""
from __future__ import annotations

import hashlib
import json as _json
import os
import tempfile
import uuid
from typing import Optional

import streamlit as st


# ── Shared utilities ──────────────────────────────────────────────────────────

def _sha1_id(value: str) -> str:
    return hashlib.sha1(value.upper().strip().encode("utf-8")).hexdigest()

def _uid(*parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(str(p) for p in parts))).upper()

def _trunc(val, n: int) -> Optional[str]:
    if val is None: return None
    s = str(val).strip(); return s[:n] if s else None

def _safe_float(val) -> Optional[float]:
    try: return float(val)
    except (TypeError, ValueError): return None

def _safe_int(val) -> Optional[int]:
    try: return int(val)
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
    """ba_id = SHA1(UPPER(name)), ba_name = name as-is."""
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
                  'OSDU_LOADER', GETUTCDATE(), :src);
    """), {"id": ba_id, "name": _trunc(name, 255), "src": _trunc(source, 40)})
    return ba_id

def _ensure_field(con, field_name: str, source: str,
                  op_ba_id: str = None, basin: str = None) -> str:
    """field_id = SHA1(UPPER(field_name))."""
    from sqlalchemy import text
    if not field_name or not field_name.strip(): field_name = "UNKNOWN"
    fid = _sha1_id(field_name)
    con.execute(text("""
        MERGE dataview.dv_field AS tgt
        USING (SELECT :fid AS field_id) src ON tgt.field_id = src.field_id
        WHEN NOT MATCHED THEN INSERT (
            field_id, field_name, basin_name, operator_ba_id, active_ind,
            row_created_by, row_created_date, source
        ) VALUES (:fid, :fname, :basin, :oba, 'Y',
                  'OSDU_LOADER', GETUTCDATE(), :src);
    """), {"fid": fid, "fname": _trunc(field_name, 255),
           "basin": _trunc(basin, 255), "oba": op_ba_id,
           "src": _trunc(source, 40)})
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
                  'OSDU_LOADER', GETUTCDATE(), :src)
        WHEN MATCHED THEN UPDATE SET
            well_name=COALESCE(tgt.well_name,:name),
            field_id=COALESCE(tgt.field_id,:fid),
            operator_ba_id=COALESCE(tgt.operator_ba_id,:oba),
            row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
    """), {"uwi": _trunc(uwi, 40), "name": _trunc(well_name or uwi, 255),
           "fid": field_id, "oba": op_ba_id, "src": _trunc(source, 40)})


# ══════════════════════════════════════════════════════════════════════════════
# Schema loaders
# ══════════════════════════════════════════════════════════════════════════════

def _load_well(engine, cl: dict, uwi: str, source: str) -> dict:
    from sqlalchemy import text
    stats = {"wells": 0}
    with engine.begin() as con:
        _ensure_source(con, source)
        op  = cl.get("operator")
        oba = _ensure_ba(con, op, source) if op else None
        fld = cl.get("well_field")
        fid = _ensure_field(con, fld, source, op_ba_id=oba) if fld else None
        con.execute(text("""
            MERGE dataview.dv_well AS tgt
            USING (SELECT :uwi AS uwi) src ON tgt.uwi=src.uwi
            WHEN NOT MATCHED THEN INSERT (
                uwi,well_name,operator_ba_id,field_id,
                province_state,county,
                surface_latitude,surface_longitude,
                spud_date,completion_date,final_td,api_num,
                area,active_ind,row_created_by,row_created_date,source
            ) VALUES (
                :uwi,:name,:oba,:fid,
                :state,:county,
                :lat,:lon,
                :spud,:compl,:td,:api,
                :area,'Y','OSDU_LOADER',GETUTCDATE(),:src
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
                completion_date=COALESCE(tgt.completion_date,:compl),
                final_td=COALESCE(tgt.final_td,:td),
                area=COALESCE(tgt.area,:area),
                row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
        """), {"uwi":_trunc(uwi,40),"name":_trunc(cl.get("well_name") or uwi,255),
               "oba":oba,"fid":fid,
               "state":_trunc(cl.get("state"),100),
               "county":_trunc(cl.get("county"),100),
               "lat":_safe_float(cl.get("latitude")),
               "lon":_safe_float(cl.get("longitude")),
               "spud":_safe_date(cl.get("spud_date")),
               "compl":_safe_date(cl.get("rig_release")),
               "td":_safe_float(cl.get("total_depth")),
               "api":_trunc(uwi,20),
               "area":_trunc(area_label.strip() if 'area_label' in dir() else '',100),
               "src":_trunc(source,40)})
        stats["wells"] = 1
    return stats


def _load_well_log(engine, cl: dict, uwi: str, source: str) -> dict:
    from sqlalchemy import text
    stats = {"logs":0,"curves":0,"skipped":0}
    log_id = _uid("LOG", uwi, cl.get("description","")[:40], source)
    svc    = cl.get("contractor")
    start  = _safe_float(str(cl.get("depth_start","")).split()[0]
                         if cl.get("depth_start") else None)
    end    = _safe_float(str(cl.get("depth_stop","")).split()[0]
                         if cl.get("depth_stop") else None)
    with engine.begin() as con:
        _ensure_source(con, source)
        sba = _ensure_ba(con, svc, source) if svc else None
        _ensure_well(con, uwi, cl.get("well_name"), source)
        con.execute(text("""
            MERGE dataview.dv_well_log AS tgt
            USING (SELECT :lid AS log_id,:uwi AS uwi) src
              ON tgt.uwi=src.uwi AND tgt.log_id=src.log_id
            WHEN NOT MATCHED THEN INSERT (
                uwi,log_id,log_type,log_date,service_company_ba_id,
                top_depth,base_depth,depth_ouom,file_format,active_ind,
                row_created_by,row_created_date,source
            ) VALUES (
                :uwi,:lid,'WIRELINE',:ldate,:sba,
                :top,:base,'ft','OSDU','Y',
                'OSDU_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                top_depth=COALESCE(tgt.top_depth,:top),
                base_depth=COALESCE(tgt.base_depth,:base),
                service_company_ba_id=COALESCE(tgt.service_company_ba_id,:sba),
                row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
        """), {"uwi":_trunc(uwi,40),"lid":_trunc(log_id,40),
               "ldate":_safe_date(cl.get("spud_date")),"sba":sba,
               "top":start,"base":end,"src":_trunc(source,40)})
        stats["logs"] = 1
        for mn in cl.get("curve_names",[]):
            cid = _uid("CURVE",uwi,log_id,mn)
            try:
                con.execute(text("""
                    MERGE dataview.dv_well_log_curve AS tgt
                    USING (SELECT :uwi AS uwi,:lid AS log_id,
                                  :cid AS curve_id) src
                      ON tgt.uwi=src.uwi AND tgt.log_id=src.log_id
                         AND tgt.curve_id=src.curve_id
                    WHEN NOT MATCHED THEN INSERT (
                        uwi,log_id,curve_id,mnemonic,
                        top_depth,base_depth,depth_ouom,active_ind,
                        row_created_by,row_created_date,source
                    ) VALUES (
                        :uwi,:lid,:cid,:mn,
                        :top,:base,'ft','Y',
                        'OSDU_LOADER',GETUTCDATE(),:src
                    );
                """), {"uwi":_trunc(uwi,40),"lid":_trunc(log_id,40),
                       "cid":_trunc(cid,40),"mn":_trunc(mn,40),
                       "top":start,"base":end,"src":_trunc(source,40)})
                stats["curves"] += 1
            except Exception: stats["skipped"] += 1
    return stats


def _load_trajectory(engine, cl: dict, uwi: str, source: str) -> dict:
    from sqlalchemy import text
    stats = {"header":0,"stations":0,"skipped":0}
    sid   = _uid("TRAJ", uwi, cl.get("spud_date",""), source)
    sp    = cl.get("survey_params", {}) or {}
    ctr   = cl.get("contractor")
    with engine.begin() as con:
        _ensure_source(con, source)
        cba = _ensure_ba(con, ctr, source) if ctr else None
        _ensure_well(con, uwi, cl.get("well_name"), source)
        con.execute(text("""
            MERGE dataview.dv_well_dir_srvy_hdr AS tgt
            USING (SELECT :sid AS survey_id,:uwi AS uwi) src
              ON tgt.uwi=src.uwi AND tgt.survey_id=src.survey_id
            WHEN NOT MATCHED THEN INSERT (
                uwi,survey_id,survey_type,survey_date,contractor_ba_id,
                survey_top_depth,survey_base_depth,depth_ouom,active_ind,
                row_created_by,row_created_date,source
            ) VALUES (
                :uwi,:sid,:stype,:sdate,:cba,
                :top,:base,'ft','Y',
                'OSDU_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                survey_type=COALESCE(tgt.survey_type,:stype),
                contractor_ba_id=COALESCE(tgt.contractor_ba_id,:cba),
                row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
        """), {"uwi":_trunc(uwi,40),"sid":_trunc(sid,40),
               "stype":_trunc(sp.get("trajectory_type") or cl.get("survey_type"),40),
               "sdate":_safe_date(cl.get("spud_date")),"cba":cba,
               "top":_safe_float(sp.get("kop_ft")),
               "base":_safe_float(cl.get("total_depth")),
               "src":_trunc(source,40)})
        stats["header"] = 1
        for i, sta in enumerate(sp.get("stations",[])):
            sta_id = _trunc(sta.get("StationID") or str(i+1), 40)
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
                        'OSDU_LOADER',GETUTCDATE(),:src
                    );
                """), {"uwi":_trunc(uwi,40),"sid":_trunc(sid,40),"staid":sta_id,
                       "md":_safe_float(sta.get("MeasuredDepth")),
                       "incl":_safe_float(sta.get("Inclination")),
                       "azim":_safe_float(sta.get("Azimuth")),
                       "tvd":_safe_float(sta.get("TrueVerticalDepth")),
                       "dls":_safe_float(sta.get("DogLegSeverity")),
                       "src":_trunc(source,40)})
                stats["stations"] += 1
            except Exception: stats["skipped"] += 1
    return stats


def _load_marker_set(engine, cl: dict, uwi: str, source: str) -> dict:
    from sqlalchemy import text
    stats    = {"tops":0,"skipped":0}
    interp_id = _uid("INTERP", uwi, cl.get("operator",""), source)
    with engine.begin() as con:
        _ensure_source(con, source)
        _ensure_well(con, uwi, cl.get("well_name"), source)
        for mk in cl.get("markers",[]):
            fname = _trunc(mk.get("formation"), 255)
            if not fname: continue
            strat_id = _sha1_id(fname)   # strat_unit_id = SHA1(formation name)
            try:
                con.execute(text("""
                    MERGE dataview.dv_well_formation_top AS tgt
                    USING (SELECT :uwi AS uwi,:sid AS strat_unit_id,
                                  :iid AS interp_id) src
                      ON tgt.uwi=src.uwi AND tgt.strat_unit_id=src.strat_unit_id
                         AND tgt.interp_id=src.interp_id
                    WHEN NOT MATCHED THEN INSERT (
                        uwi,strat_unit_id,interp_id,
                        strat_unit_name,top_depth,tvd_top,
                        depth_ouom,depth_datum,
                        confidence_level,active_ind,
                        row_created_by,row_created_date,source
                    ) VALUES (
                        :uwi,:sid,:iid,
                        :fname,:top,:tvd,
                        'ft','KB',
                        :conf,'Y',
                        'OSDU_LOADER',GETUTCDATE(),:src
                    )
                    WHEN MATCHED THEN UPDATE SET
                        top_depth=:top,tvd_top=:tvd,confidence_level=:conf,
                        row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
                """), {"uwi":_trunc(uwi,40),"sid":_trunc(strat_id,40),
                       "iid":_trunc(interp_id,40),"fname":fname,
                       "top":_safe_float(mk.get("md")),
                       "tvd":_safe_float(mk.get("tvd")),
                       "conf":_trunc(mk.get("quality"),40),
                       "src":_trunc(source,40)})
                stats["tops"] += 1
            except Exception: stats["skipped"] += 1
    return stats


def _load_pressure(engine, cl: dict, uwi: str, source: str) -> dict:
    from sqlalchemy import text
    stats     = {"dst":0,"periods":0,"skipped":0}
    dst_id    = _uid("DST", uwi, cl.get("spud_date",""), source)
    pressures = cl.get("pressures", {})
    ctr       = cl.get("contractor")

    fsip   = (_safe_float(pressures.get("FSIP")) or
              _safe_float(pressures.get("ReservoirPressure")) or 0.0)
    max_sip = _safe_float(pressures.get("ISIP")) or fsip

    top  = _safe_float(str(cl.get("depth_start","")).split()[0]
                       if cl.get("depth_start") else None) or 0.0
    base = _safe_float(str(cl.get("depth_stop","")).split()[0]
                       if cl.get("depth_stop") else None) or top

    with engine.begin() as con:
        _ensure_source(con, source)
        _ensure_well(con, uwi, cl.get("well_name"), source)
        # contractor_ba_id is NOT NULL — default to "UNKNOWN"
        cba = _ensure_ba(con, ctr or "UNKNOWN", source)

        try:
            con.execute(text("""
                MERGE dataview.dv_well_dst AS tgt
                USING (SELECT :uwi AS uwi,:did AS dst_id) src
                  ON tgt.uwi=src.uwi AND tgt.dst_id=src.dst_id
                WHEN NOT MATCHED THEN INSERT (
                    uwi,dst_id,dst_num,test_type,test_date,
                    top_depth,base_depth,depth_ouom,depth_datum,
                    strat_unit_name,tool_type,
                    perforation_top,perforation_base,
                    max_shut_in_pressure,final_shut_in_pressure,pressure_ouom,
                    max_oil_rate,max_gas_rate,max_water_rate,rate_ouom,
                    gor,h2s_pct,co2_pct,test_result,
                    contractor_ba_id,active_ind,source,
                    row_created_by,row_created_date
                ) VALUES (
                    :uwi,:did,1,'DST',COALESCE(:tdate,CAST(GETUTCDATE() AS date)),
                    :top,:base,'ft','KB',
                    COALESCE(:strat,'UNKNOWN'),'DST',
                    CAST(:top AS nvarchar(255)),CAST(:base AS nvarchar(255)),
                    :msip,:fsip,'psi',
                    :oil,:gas,:water,'STBD',
                    :gor,0.0,0.0,'COMPLETED',
                    :cba,'Y',:src,
                    'OSDU_LOADER',GETUTCDATE()
                )
                WHEN MATCHED THEN UPDATE SET
                    final_shut_in_pressure=:fsip,
                    row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
            """), {"uwi":_trunc(uwi,40),"did":_trunc(dst_id,40),
                   "tdate":_safe_date(cl.get("spud_date")),
                   "top":top,"base":base,
                   "strat":_trunc(cl.get("well_field"),40),
                   "msip":max_sip,"fsip":fsip,
                   "oil":_safe_float(cl.get("oil_rate_stbd")) or 0.0,
                   "gas":_safe_float(cl.get("gas_rate_mcfd")) or 0.0,
                   "water":_safe_float(cl.get("water_rate_bwpd")) or 0.0,
                   "gor":_safe_float(pressures.get("GOR")),
                   "cba":cba,"src":_trunc(source,40)})
            stats["dst"] = 1

            # Load flow periods into dv_well_dst_period
            # All NOT NULL columns — use 0.0 defaults for missing values
            for i, fp_key in enumerate(["IFP","IDP","FFP","FDP","ISIP","FSIP"]):
                p_val = _safe_float(pressures.get(fp_key))
                if p_val is None: continue
                period_id = _uid("PERIOD", dst_id, fp_key)
                try:
                    con.execute(text("""
                        MERGE dataview.dv_well_dst_period AS tgt
                        USING (SELECT :uwi AS uwi,:did AS dst_id,
                                      :pid AS period_id) src
                          ON tgt.uwi=src.uwi AND tgt.dst_id=src.dst_id
                             AND tgt.period_id=src.period_id
                        WHEN NOT MATCHED THEN INSERT (
                            uwi,dst_id,period_id,period_type,period_seq,
                            duration_min,start_pressure,end_pressure,pressure_ouom,
                            avg_oil_rate,avg_gas_rate,avg_water_rate,rate_ouom,
                            source,row_created_by,row_created_date
                        ) VALUES (
                            :uwi,:did,:pid,:ptype,:pseq,
                            0.0,0.0,:pval,'psi',
                            0.0,0.0,0.0,'STBD',
                            :src,'OSDU_LOADER',GETUTCDATE()
                        );
                    """), {"uwi":_trunc(uwi,40),"did":_trunc(dst_id,40),
                           "pid":_trunc(period_id,40),"ptype":fp_key,"pseq":i+1,
                           "pval":p_val,"src":_trunc(source,40)})
                    stats["periods"] += 1
                except Exception: pass
        except Exception as e:
            stats["skipped"] += 1
            raise
    return stats


def _load_production(engine, cl: dict, uwi: str, raw: dict, source: str) -> dict:
    from sqlalchemy import text
    stats   = {"entities":0,"volumes":0,"skipped":0}
    pe_id   = _uid("PROD", uwi, source)
    summary = cl.get("production_summary", {}) or {}
    with engine.begin() as con:
        _ensure_source(con, source)
        _ensure_well(con, uwi, cl.get("well_name"), source)
        op  = cl.get("operator")
        oba = _ensure_ba(con, op, source) if op else None
        con.execute(text("""
            MERGE dataview.dv_prod_entity AS tgt
            USING (SELECT :peid AS prod_entity_id) src
              ON tgt.prod_entity_id=src.prod_entity_id
            WHEN NOT MATCHED THEN INSERT (
                prod_entity_id,uwi,operator_ba_id,prod_entity_type,
                prod_entity_name,first_prod_date,last_prod_date,primary_fluid,
                active_ind,row_created_by,row_created_date,source
            ) VALUES (
                :peid,:uwi,:oba,'WELL',
                :name,:first,:last,:fluid,
                'Y','OSDU_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                first_prod_date=COALESCE(tgt.first_prod_date,:first),
                last_prod_date=COALESCE(tgt.last_prod_date,:last),
                row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
        """), {"peid":_trunc(pe_id,40),"uwi":_trunc(uwi,40),"oba":oba,
               "name":_trunc(cl.get("well_name") or uwi,255),
               "first":_safe_date(summary.get("first_production")),
               "last":_safe_date(summary.get("last_production")),
               "fluid":_trunc(summary.get("fluid_type"),40),
               "src":_trunc(source,40)})
        stats["entities"] = 1

        records = (raw.get("data",{}).get("ProductionRecords",[]) or [])
        for rec in records:
            period = _safe_date(rec.get("PeriodDate") or rec.get("ProductionDate"))
            if not period: continue
            period_str = period[:7]
            for fluid, col in [("OIL","OilVolume"),("GAS","GasVolume"),
                                ("WATER","WaterVolume")]:
                vol = _safe_float(rec.get(col))
                if vol is None: continue
                try:
                    con.execute(text("""
                        MERGE dataview.dv_prod_volume AS tgt
                        USING (SELECT :peid AS prod_entity_id,
                                      :pd   AS period_date,
                                      :ft   AS fluid_type) src
                          ON tgt.prod_entity_id=src.prod_entity_id
                             AND tgt.period_date=src.period_date
                             AND tgt.fluid_type=src.fluid_type
                        WHEN NOT MATCHED THEN INSERT (
                            prod_entity_id,period_date,fluid_type,
                            volume,volume_ouom,active_ind,source,
                            row_created_by,row_created_date
                        ) VALUES (
                            :peid,:pd,:ft,:vol,'STB','Y',:src,
                            'OSDU_LOADER',GETUTCDATE()
                        )
                        WHEN MATCHED THEN UPDATE SET volume=:vol;
                    """), {"peid":_trunc(pe_id,40),"pd":period_str,
                           "ft":fluid,"vol":vol,"src":_trunc(source,40)})
                    stats["volumes"] += 1
                except Exception: stats["skipped"] += 1
    return stats


def _load_seismic(engine, cl: dict, source: str) -> dict:
    from sqlalchemy import text
    stats  = {"surveys":0}
    seis_id = _uid("SEIS", cl.get("survey_name",""), source)
    ctr    = cl.get("contractor")
    op     = cl.get("operator")
    acq    = cl.get("acq_params", {}) or {}
    with engine.begin() as con:
        _ensure_source(con, source)
        cba = _ensure_ba(con, ctr, source) if ctr else None
        oba = _ensure_ba(con, op,  source) if op  else None
        con.execute(text("""
            MERGE dataview.dv_seis_set AS tgt
            USING (SELECT :sid AS seis_set_id) src
              ON tgt.seis_set_id=src.seis_set_id
            WHEN NOT MATCHED THEN INSERT (
                seis_set_id,seis_set_name,seis_set_type,survey_date,
                contractor_ba_id,operator_ba_id,survey_area_km2,
                bbox_min_lat,bbox_max_lat,bbox_min_lon,bbox_max_lon,
                epsg_code,active_ind,remark,
                row_created_by,row_created_date,source
            ) VALUES (
                :sid,:name,:stype,:sdate,
                :cba,:oba,:area,
                :bminlat,:bmaxlat,:bminlon,:bmaxlon,
                :epsg,'Y',:remark,
                'OSDU_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                seis_set_name=:name,
                contractor_ba_id=COALESCE(tgt.contractor_ba_id,:cba),
                operator_ba_id=COALESCE(tgt.operator_ba_id,:oba),
                row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
        """), {"sid":_trunc(seis_id,40),
               "name":_trunc(cl.get("survey_name") or "UNKNOWN",255),
               "stype":_trunc(cl.get("seis_set_type"),40),
               "sdate":_safe_date(cl.get("spud_date")),
               "cba":cba,"oba":oba,
               "area":_safe_float(acq.get("area_km2")),
               "bminlat":_safe_float(cl.get("bbox_min_lat")),
               "bmaxlat":_safe_float(cl.get("bbox_max_lat")),
               "bminlon":_safe_float(cl.get("bbox_min_lon")),
               "bmaxlon":_safe_float(cl.get("bbox_max_lon")),
               "epsg":_safe_int(cl.get("epsg_code")),
               "remark":_trunc(cl.get("description"),2000),
               "src":_trunc(source,40)})
        stats["surveys"] = 1
    return stats


def _load_field(engine, cl: dict, source: str) -> dict:
    from sqlalchemy import text
    stats  = {"fields":0}
    fp     = cl.get("field_params", {}) or {}
    op     = cl.get("operator")
    fname  = cl.get("well_name") or cl.get("well_field") or "UNKNOWN"
    fid    = _sha1_id(fname)
    with engine.begin() as con:
        _ensure_source(con, source)
        oba = _ensure_ba(con, op, source) if op else None
        con.execute(text("""
            MERGE dataview.dv_field AS tgt
            USING (SELECT :fid AS field_id) src ON tgt.field_id=src.field_id
            WHEN NOT MATCHED THEN INSERT (
                field_id,field_name,field_type,
                province_state,county,basin_name,
                operator_ba_id,field_status,
                active_ind,remark,
                row_created_by,row_created_date,source
            ) VALUES (
                :fid,:fname,:ftype,
                :state,:county,:basin,
                :oba,:status,
                'Y',:remark,
                'OSDU_LOADER',GETUTCDATE(),:src
            )
            WHEN MATCHED THEN UPDATE SET
                field_name=:fname,
                row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
        """), {"fid":_trunc(fid,40),
               "fname":_trunc(fname,255),
               "ftype":_trunc(fp.get("production_type") or fp.get("fluid_type"),40),
               "state":_trunc(cl.get("state"),100),
               "county":_trunc(cl.get("county"),100),
               "basin":_trunc(fp.get("basin"),255),
               "oba":oba,"status":_trunc(fp.get("field_status"),40),
               "remark":_trunc(cl.get("description"),2000),
               "src":_trunc(source,40)})
        stats["fields"] = 1
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# Dispatcher
# ══════════════════════════════════════════════════════════════════════════════

def _load_completion(engine, cl: dict, uwi: str, source: str) -> dict:
    """OSDU WellboreCompletion → dv_well_completion + dv_well_stimulation."""
    from sqlalchemy import text
    stats = {"completions": 0, "stimulations": 0, "skipped": 0}
    cp    = cl.get("completion_params", {}) or {}

    comp_id   = _uid("COMP", uwi, cl.get("spud_date",""), source)
    comp_type = _trunc(cp.get("completion_type") or "PLUG_PERF", 40)
    comp_date = _safe_date(cl.get("spud_date")) or "1900-01-01"
    formation = _trunc((cp.get("formations") or ["UNKNOWN"])[0], 40)

    top  = _safe_float(str(cl.get("depth_start","")).split()[0]
                       if cl.get("depth_start") else None) or 0.0
    base = _safe_float(str(cl.get("depth_stop","")).split()[0]
                       if cl.get("depth_stop") else None) or top

    op  = cl.get("operator")
    ctr = cl.get("contractor")

    with engine.begin() as con:
        _ensure_source(con, source)
        _ensure_well(con, uwi, cl.get("well_name"), source)
        oba = _ensure_ba(con, op  or "UNKNOWN", source)
        cba = _ensure_ba(con, ctr or "UNKNOWN", source)

        try:
            con.execute(text("""
                MERGE dataview.dv_well_completion AS tgt
                USING (SELECT :uwi AS uwi, :cid AS completion_id) src
                  ON tgt.uwi=src.uwi AND tgt.completion_id=src.completion_id
                WHEN NOT MATCHED THEN INSERT (
                    uwi, completion_id,
                    completion_type, completion_date,
                    top_depth, base_depth, depth_ouom, depth_datum,
                    strat_unit_name, completion_status, primary_fluid,
                    tubing_size_in, tubing_depth, artificial_lift_type,
                    operator_ba_id, contractor_ba_id,
                    active_ind, remark, source,
                    row_created_by, row_created_date
                ) VALUES (
                    :uwi, :cid,
                    :ctype, :cdate,
                    :top, :base, 'ft', 'KB',
                    :strat, 'COMPLETED', 'OIL',
                    COALESCE(:tubing, 0.0), 0.0, 'NONE',
                    :oba, :cba,
                    'Y', :remark, :src,
                    'OSDU_LOADER', GETUTCDATE()
                )
                WHEN MATCHED THEN UPDATE SET
                    completion_type = :ctype,
                    row_changed_by  = 'OSDU_LOADER',
                    row_changed_date = GETUTCDATE();
            """), {
                "uwi":    _trunc(uwi, 40),
                "cid":    _trunc(comp_id, 40),
                "ctype":  comp_type,
                "cdate":  comp_date,
                "top":    top, "base": base,
                "strat":  formation,
                "tubing": _safe_float(cp.get("tubing_in")),
                "oba":    oba, "cba": cba,
                "remark": _trunc(cl.get("description"), 2000),
                "src":    _trunc(source, 40),
            })
            stats["completions"] = 1
        except Exception as e:
            stats["skipped"] += 1
            raise

        # Stimulation — one row summarising the whole frac job
        # dv_well_stimulation has many NOT NULL float columns;
        # use OSDU values where available, 0.0 as defaults elsewhere.
        fluid_vol  = _safe_float(cp.get("total_fluid_bbl")) or 0.0
        prop_mass  = _safe_float(cp.get("total_proppant_lb")) or 0.0
        n_stages   = _safe_int(cp.get("n_stages")) or 1
        stim_id    = _uid("STIM", uwi, comp_id, "1")

        try:
            con.execute(text("""
                MERGE dataview.dv_well_stimulation AS tgt
                USING (SELECT :uwi AS uwi, :cid AS completion_id,
                              :sid AS stim_id) src
                  ON tgt.uwi=src.uwi AND tgt.completion_id=src.completion_id
                     AND tgt.stim_id=src.stim_id
                WHEN NOT MATCHED THEN INSERT (
                    uwi, completion_id, stim_id,
                    stim_type, stim_date,
                    top_depth, base_depth, depth_ouom,
                    stage_count,
                    fluid_type, fluid_volume, fluid_volume_ouom,
                    proppant_type, proppant_mesh, proppant_mass,
                    max_treating_pressure, avg_treating_pressure, pressure_ouom,
                    max_pump_rate, rate_ouom,
                    isip, closure_pressure,
                    contractor_ba_id,
                    active_ind, remark, source,
                    row_created_by, row_created_date
                ) VALUES (
                    :uwi, :cid, :sid,
                    'HYDRAULIC_FRACTURE', :sdate,
                    :top, :base, 'ft',
                    :nstages,
                    'SLICKWATER', :fvol, 'bbl',
                    'SAND', '100 MESH', :pmass,
                    0.0, 0.0, 'psi',
                    0.0, 'bbl/min',
                    0.0, 0.0,
                    :cba,
                    'Y', :remark, :src,
                    'OSDU_LOADER', GETUTCDATE()
                );
            """), {
                "uwi":     _trunc(uwi, 40),
                "cid":     _trunc(comp_id, 40),
                "sid":     _trunc(stim_id, 40),
                "sdate":   comp_date,
                "top":     top, "base": base,
                "nstages": n_stages,
                "fvol":    fluid_vol,
                "pmass":   prop_mass,
                "cba":     cba,
                "remark":  _trunc(cl.get("description"), 2000),
                "src":     _trunc(source, 40),
            })
            stats["stimulations"] = 1
        except Exception:
            stats["skipped"] += 1

    return stats


def _load_core(engine, cl: dict, uwi: str, source: str) -> dict:
    """OSDU WellCoreAnalysis → dv_well_core + dv_well_core_sample."""
    from sqlalchemy import text
    stats = {"cores": 0, "samples": 0, "skipped": 0}

    core_id  = _uid("CORE", uwi, cl.get("spud_date",""), source)
    lab      = cl.get("contractor") or "UNKNOWN"
    top      = _safe_float(str(cl.get("depth_start","")).split()[0]
                           if cl.get("depth_start") else None) or 0.0
    base     = _safe_float(str(cl.get("depth_stop","")).split()[0]
                           if cl.get("depth_stop") else None) or top
    formation = _trunc(cl.get("well_field") or "UNKNOWN", 40)
    core_len  = base - top
    cs        = cl.get("core_stats", {}) or {}
    plugs     = cl.get("plugs", []) or []
    n_plugs   = cl.get("n_plugs", 0) or len(plugs)

    with engine.begin() as con:
        _ensure_source(con, source)
        _ensure_well(con, uwi, cl.get("well_name"), source)
        # cutting_company_ba_id and analysis_company_ba_id are both NOT NULL
        lab_ba = _ensure_ba(con, lab, source)

        try:
            con.execute(text("""
                MERGE dataview.dv_well_core AS tgt
                USING (SELECT :uwi AS uwi, :cid AS core_id) src
                  ON tgt.uwi=src.uwi AND tgt.core_id=src.core_id
                WHEN NOT MATCHED THEN INSERT (
                    uwi, core_id, core_num, core_type, core_show,
                    top_depth, base_depth, depth_ouom, depth_datum,
                    core_length, recovery_length, recovery_pct, length_ouom,
                    core_date, cutting_company_ba_id, analysis_company_ba_id,
                    strat_unit_name, photo_count,
                    has_uv_photos, has_thin_section_photos,
                    active_ind, remark, source,
                    row_created_by, row_created_date
                ) VALUES (
                    :uwi, :cid, 1, 'CONVENTIONAL', 'NONE',
                    :top, :base, 'ft', 'KB',
                    :clen, COALESCE(:reclen, :clen),
                    COALESCE(:recpct, 100.0), 'ft',
                    COALESCE(:cdate, CAST(GETUTCDATE() AS date)),
                    :lba, :lba,
                    :strat, :nphoto,
                    'N', 'N',
                    'Y', :remark, :src,
                    'OSDU_LOADER', GETUTCDATE()
                )
                WHEN MATCHED THEN UPDATE SET
                    strat_unit_name = :strat,
                    row_changed_by  = 'OSDU_LOADER',
                    row_changed_date = GETUTCDATE();
            """), {
                "uwi":    _trunc(uwi, 40),
                "cid":    _trunc(core_id, 40),
                "top":    top, "base": base,
                "clen":   str(round(core_len, 2)),
                "reclen": _safe_float(cs.get("core_recovery")),
                "recpct": _safe_float(cs.get("core_recovery")),
                "cdate":  _safe_date(cl.get("spud_date")),
                "lba":    lab_ba,
                "strat":  formation,
                "nphoto": 0,
                "remark": _trunc(cl.get("description"), 2000),
                "src":    _trunc(source, 40),
            })
            stats["cores"] = 1
        except Exception as e:
            stats["skipped"] += 1
            raise

        # Core samples — one row per plug from the plugs list
        for i, plug in enumerate(plugs):
            samp_id  = _uid("SAMP", uwi, core_id, str(i))
            samp_md  = _safe_float(plug.get("md")) or (top + i)
            try:
                con.execute(text("""
                    MERGE dataview.dv_well_core_sample AS tgt
                    USING (SELECT :uwi AS uwi, :cid AS core_id,
                                  :sid AS sample_id) src
                      ON tgt.uwi=src.uwi AND tgt.core_id=src.core_id
                         AND tgt.sample_id=src.sample_id
                    WHEN NOT MATCHED THEN INSERT (
                        uwi, core_id, sample_id, sample_type,
                        sample_depth, top_depth, base_depth, depth_ouom,
                        porosity_frac, permeability_air_md,
                        permeability_klinkenberg_md, water_saturation_frac,
                        grain_density_g_cc, bulk_density_g_cc,
                        lithology,
                        active_ind, source,
                        row_created_by, row_created_date
                    ) VALUES (
                        :uwi, :cid, :sid, 'PLUG',
                        :md, :md, :md + 0.1, 'ft',
                        COALESCE(:phi, 0.0) / 100.0,
                        COALESCE(:kair, 0.0),
                        COALESCE(:kair, 0.0),
                        0.0,
                        2.65, 2.65,
                        :lith,
                        'Y', :src,
                        'OSDU_LOADER', GETUTCDATE()
                    );
                """), {
                    "uwi":  _trunc(uwi, 40),
                    "cid":  _trunc(core_id, 40),
                    "sid":  _trunc(samp_id, 40),
                    "md":   samp_md,
                    "phi":  _safe_float(plug.get("porosity")),
                    "kair": _safe_float(plug.get("perm_md")),
                    "lith": _trunc(plug.get("lithology"), 40),
                    "src":  _trunc(source, 40),
                })
                stats["samples"] += 1
            except Exception:
                stats["skipped"] += 1

        # Summary stats as additional samples if no plug-level data
        if not plugs and cs:
            avg_phi = _safe_float(cs.get("avg_porosity_pct"))
            avg_k   = _safe_float(cs.get("avg_perm_md"))
            if avg_phi is not None or avg_k is not None:
                samp_id = _uid("SAMP", uwi, core_id, "AVG")
                try:
                    con.execute(text("""
                        MERGE dataview.dv_well_core_sample AS tgt
                        USING (SELECT :uwi AS uwi,:cid AS core_id,
                                      :sid AS sample_id) src
                          ON tgt.uwi=src.uwi AND tgt.core_id=src.core_id
                             AND tgt.sample_id=src.sample_id
                        WHEN NOT MATCHED THEN INSERT (
                            uwi,core_id,sample_id,sample_type,
                            sample_depth,top_depth,base_depth,depth_ouom,
                            porosity_frac,permeability_air_md,
                            permeability_klinkenberg_md,water_saturation_frac,
                            grain_density_g_cc,bulk_density_g_cc,
                            active_ind,remark,source,
                            row_created_by,row_created_date
                        ) VALUES (
                            :uwi,:cid,:sid,'AVERAGE_SUMMARY',
                            :mid,:top,:base,'ft',
                            COALESCE(:phi,0.0)/100.0,
                            COALESCE(:k,0.0),COALESCE(:k,0.0),0.0,
                            2.65,2.65,
                            'Y','Average plug statistics from OSDU',:src,
                            'OSDU_LOADER',GETUTCDATE()
                        );
                    """), {"uwi":_trunc(uwi,40),"cid":_trunc(core_id,40),
                           "sid":_trunc(samp_id,40),
                           "mid":(top+base)/2,"top":top,"base":base,
                           "phi":avg_phi,"k":avg_k,"src":_trunc(source,40)})
                    stats["samples"] += 1
                except Exception:
                    stats["skipped"] += 1

    return stats


def _load_scal(engine, cl: dict, uwi: str, source: str) -> dict:
    """OSDU RockFluidOrganisation (SCAL) → dv_well_core + dv_well_core_sample.

    SCAL data shares the core sample table with conventional core analysis.
    Each rock-fluid system becomes one core header row; each relative
    permeability point becomes one sample row with petrophysical columns.
    """
    from sqlalchemy import text
    stats = {"cores": 0, "samples": 0, "skipped": 0}

    sp       = cl.get("scal_params", {}) or {}
    lab      = cl.get("contractor") or "UNKNOWN"
    top      = _safe_float(str(cl.get("depth_start","")).split()[0]
                           if cl.get("depth_start") else None) or 0.0
    base     = _safe_float(str(cl.get("depth_stop","")).split()[0]
                           if cl.get("depth_stop") else None) or top
    formation = _trunc(cl.get("well_field") or "UNKNOWN", 40)

    with engine.begin() as con:
        _ensure_source(con, source)
        _ensure_well(con, uwi, cl.get("well_name"), source)
        lab_ba = _ensure_ba(con, lab, source)

        core_id = _uid("SCAL", uwi, cl.get("spud_date",""), source)
        core_len = base - top

        try:
            con.execute(text("""
                MERGE dataview.dv_well_core AS tgt
                USING (SELECT :uwi AS uwi,:cid AS core_id) src
                  ON tgt.uwi=src.uwi AND tgt.core_id=src.core_id
                WHEN NOT MATCHED THEN INSERT (
                    uwi,core_id,core_num,core_type,core_show,
                    top_depth,base_depth,depth_ouom,depth_datum,
                    core_length,recovery_length,recovery_pct,length_ouom,
                    core_date,cutting_company_ba_id,analysis_company_ba_id,
                    strat_unit_name,photo_count,
                    has_uv_photos,has_thin_section_photos,
                    active_ind,remark,source,
                    row_created_by,row_created_date
                ) VALUES (
                    :uwi,:cid,1,'SCAL','NONE',
                    :top,:base,'ft','KB',
                    :clen,:clen,100.0,'ft',
                    COALESCE(:cdate,CAST(GETUTCDATE() AS date)),
                    :lba,:lba,
                    :strat,0,'N','N',
                    'Y',:remark,:src,
                    'OSDU_LOADER',GETUTCDATE()
                )
                WHEN MATCHED THEN UPDATE SET
                    row_changed_by='OSDU_LOADER',row_changed_date=GETUTCDATE();
            """), {"uwi":_trunc(uwi,40),"cid":_trunc(core_id,40),
                   "top":top,"base":base,"clen":str(round(core_len,2)),
                   "cdate":_safe_date(cl.get("spud_date")),
                   "lba":lab_ba,"strat":formation,
                   "remark":_trunc(cl.get("description"),2000),
                   "src":_trunc(source,40)})
            stats["cores"] = 1
        except Exception as e:
            stats["skipped"] += 1
            raise

        # Summary stats → one sample row with avg porosity/perm
        avg_phi = _safe_float(sp.get("avg_porosity_pct"))
        avg_k   = _safe_float(sp.get("avg_perm_md"))
        sw_irr  = None  # Swi from first system if available

        samp_id = _uid("SCALSAMP", uwi, core_id, "SUMMARY")
        try:
            con.execute(text("""
                MERGE dataview.dv_well_core_sample AS tgt
                USING (SELECT :uwi AS uwi,:cid AS core_id,
                              :sid AS sample_id) src
                  ON tgt.uwi=src.uwi AND tgt.core_id=src.core_id
                     AND tgt.sample_id=src.sample_id
                WHEN NOT MATCHED THEN INSERT (
                    uwi,core_id,sample_id,sample_type,
                    sample_depth,top_depth,base_depth,depth_ouom,
                    porosity_frac,permeability_air_md,
                    permeability_klinkenberg_md,water_saturation_frac,
                    grain_density_g_cc,bulk_density_g_cc,
                    active_ind,remark,source,
                    row_created_by,row_created_date
                ) VALUES (
                    :uwi,:cid,:sid,'SCAL_SUMMARY',
                    :mid,:top,:base,'ft',
                    COALESCE(:phi,0.0)/100.0,
                    COALESCE(:k,0.0),COALESCE(:k,0.0),0.0,
                    2.65,2.65,
                    'Y',:remark,:src,
                    'OSDU_LOADER',GETUTCDATE()
                );
            """), {"uwi":_trunc(uwi,40),"cid":_trunc(core_id,40),
                   "sid":_trunc(samp_id,40),
                   "mid":(top+base)/2,"top":top,"base":base,
                   "phi":avg_phi,"k":avg_k,
                   "remark":_trunc(
                       f"SCAL summary — {sp.get('n_systems',0)} systems, "
                       f"method: {sp.get('cap_pressure_method','?')}", 2000),
                   "src":_trunc(source,40)})
            stats["samples"] += 1
        except Exception:
            stats["skipped"] += 1

    return stats


_SCHEMA_META = {
    # schema             target tables                                    needs_uwi
    "osdu_well":       ("dv_well",                                        True),
    "osdu_wellbore":   ("dv_well",                                        True),
    "osdu_well_log":   ("dv_well_log  +  dv_well_log_curve",              True),
    "osdu_trajectory": ("dv_well_dir_srvy_hdr  +  dv_well_dir_srvy_sta", True),
    "osdu_marker_set": ("dv_well_formation_top",                          True),
    "osdu_pressure":   ("dv_well_dst  +  dv_well_dst_period",             True),
    "osdu_production": ("dv_prod_entity  +  dv_prod_volume",              True),
    "osdu_seismic":    ("dv_seis_set",                                    False),
    "osdu_field":      ("dv_field",                                       False),
    "osdu_completion": ("dv_well_completion  +  dv_well_stimulation",     True),
    "osdu_core":       ("dv_well_core  +  dv_well_core_sample",           True),
    "osdu_scal":       ("dv_well_core  +  dv_well_core_sample",           True),
}

_NOT_YET = {
    "osdu_horizon":   "dv_strat_interval",
    "osdu_reservoir": "dv_strat_interval",
    "osdu_document":  "catalog only",
}


def _dispatch(schema, engine, cl, uwi, raw, source):
    if schema in ("osdu_well","osdu_wellbore"):
        return _load_well(engine, cl, uwi, source)
    if schema == "osdu_well_log":
        return _load_well_log(engine, cl, uwi, source)
    if schema == "osdu_trajectory":
        return _load_trajectory(engine, cl, uwi, source)
    if schema == "osdu_marker_set":
        return _load_marker_set(engine, cl, uwi, source)
    if schema == "osdu_pressure":
        return _load_pressure(engine, cl, uwi, source)
    if schema == "osdu_production":
        return _load_production(engine, cl, uwi, raw, source)
    if schema == "osdu_seismic":
        return _load_seismic(engine, cl, source)
    if schema == "osdu_field":
        return _load_field(engine, cl, source)
    if schema == "osdu_completion":
        return _load_completion(engine, cl, uwi, source)
    if schema == "osdu_core":
        return _load_core(engine, cl, uwi, source)
    if schema == "osdu_scal":
        return _load_scal(engine, cl, uwi, source)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit render
# ══════════════════════════════════════════════════════════════════════════════

def render(engine) -> None:
    st.caption(
        "Loads OSDU JSON files into the DataView schema. "
        "Detects the OSDU kind automatically. "
        "Idempotent — re-running the same file is safe. "
        "ba_id and field_id use **SHA1(UPPER(name))** matching the pipeline convention."
    )

    uploaded = st.file_uploader(
        "Drop OSDU JSON file (.json)", type=["json"], key="osdu_loader_upload")

    st.caption("— or load from a server-side directory —")
    dir_path = st.text_input(
        "Directory path (all .json files in folder will be loaded)",
        value=st.session_state.get("osdu_loader_dir", ""),
        placeholder=r"C:\WellData\OSDU  or  \\server\share\osdu_exports",
        key="osdu_loader_dir_input",
    )
    if dir_path:
        st.session_state["osdu_loader_dir"] = dir_path

    _cs, _ca = st.columns(2)
    source_label = _cs.text_input(
        "Source label", value="OSDU", key="osdu_source")
    area_label = _ca.text_input(
        "Area", value="", key="osdu_area",
        placeholder="e.g. Permian Basin")

    # ── Resolve source: uploaded file wins over directory path ────────────────
    if not uploaded and not dir_path.strip():
        st.info("Drop a file above or enter a directory path to begin.")
        return

    # Build file list
    if uploaded:
        # Single file via uploader — write to temp, process as list of one
        import tempfile as _tf
        _tmp = _tf.NamedTemporaryFile(
            delete=False, suffix=".json", prefix="osdu_")
        _tmp.write(uploaded.getbuffer())
        _tmp.flush()
        _tmp.close()
        file_list     = [_tmp.name]
        file_labels   = [uploaded.name]
        cleanup_temps = [_tmp.name]
    else:
        # Directory — find all .json files with the petroleum peek
        from pathlib import Path as _Path
        dpath = _Path(dir_path.strip())
        if not dpath.exists():
            st.error(f"Directory not found: `{dir_path}`")
            return
        if not dpath.is_dir():
            st.error(f"Path is not a directory: `{dir_path}`")
            return

        candidates = sorted(dpath.glob("*.json"))
        file_list = []
        file_labels = []
        skipped_non_petroleum = 0
        for p in candidates:
            try:
                with open(p, "rb") as fh:
                    peek = fh.read(100)
                if b'"kind"' in peek or b'"header"' in peek:
                    file_list.append(str(p))
                    file_labels.append(p.name)
                else:
                    skipped_non_petroleum += 1
            except OSError:
                skipped_non_petroleum += 1

        if not file_list:
            st.warning(
                f"No OSDU JSON files found in `{dir_path}`. "
                f"{len(candidates)} .json file(s) found but "
                f"{skipped_non_petroleum} failed the petroleum content check "
                "(no 'kind' or 'header' field in first 100 bytes)."
                if candidates else
                f"No .json files found in `{dir_path}`."
            )
            return

        cleanup_temps = []
        st.info(
            f"Found **{len(file_list)}** OSDU file(s) in `{dpath.name}/`"
            + (f" · {skipped_non_petroleum} non-petroleum JSON skipped"
               if skipped_non_petroleum else "")
        )

    # Show load button — label changes for single vs batch
    btn_label = (
        f"🚀 Load {len(file_list)} file(s) into DataView"
        if len(file_list) > 1
        else "🚀 Load into DataView"
    )
    if not st.button(btn_label, type="primary",
                     key="osdu_load_btn", use_container_width=True):
        # Clean up temp if user didn't click load
        for t in cleanup_temps:
            try: os.unlink(t)
            except Exception: pass
        return

    # ── Process files ─────────────────────────────────────────────────────────
    src = (source_label or "OSDU").strip()
    from dataview.file_catalog.json_well_log_catalog import classify_json_well_log

    total_stats: dict = {}
    errors:      list = []
    skipped:     list = []

    progress = st.progress(0.0, text="Starting…")
    status   = st.empty()

    for i, (fpath, flabel) in enumerate(zip(file_list, file_labels)):
        pct = i / len(file_list)
        progress.progress(pct, text=f"{flabel}  ({i+1}/{len(file_list)})")

        try:
            # Petroleum gate
            with open(fpath, "r", encoding="utf-8-sig", errors="replace") as fh:
                head = fh.read(512)
            if '"kind"' not in head and '"header"' not in head:
                skipped.append((flabel, "not petroleum JSON"))
                continue

            raw = _json.loads(
                open(fpath, "r", encoding="utf-8-sig").read())
            cl     = classify_json_well_log(fpath)
            schema = cl.get("json_schema", "unknown")
            uwi    = (cl.get("uwi") or "").strip()

            if schema in _NOT_YET:
                skipped.append((flabel,
                    f"{schema} — not yet implemented "
                    f"(target: {_NOT_YET[schema]})"))
                continue

            if schema not in _SCHEMA_META:
                skipped.append((flabel, f"unrecognised schema: {schema}"))
                continue

            target, needs_uwi = _SCHEMA_META[schema]

            if needs_uwi and not uwi:
                skipped.append((flabel,
                    f"{schema} — no UWI found in file, "
                    "use the single-file uploader to enter one manually"))
                continue

            stats = _dispatch(schema, engine, cl, uwi, raw, src)
            if stats:
                for k, v in stats.items():
                    total_stats[k] = total_stats.get(k, 0) + v

        except Exception as e:
            errors.append((flabel, f"{type(e).__name__}: {e}"))

    # Clean up temp files
    for t in cleanup_temps:
        try: os.unlink(t)
        except Exception: pass

    progress.empty()
    status.empty()

    # ── Results ───────────────────────────────────────────────────────────────
    n_ok  = len(file_list) - len(errors) - len(skipped)
    n_err = len(errors)
    n_skp = len(skipped)

    if n_err == 0 and n_skp == 0:
        st.success(f"✅ All {len(file_list)} file(s) loaded successfully.")
    elif n_ok > 0:
        st.warning(
            f"✅ {n_ok} loaded · ⚠️ {n_skp} skipped · ❌ {n_err} failed")
    else:
        st.error(f"❌ {n_err} failed · ⚠️ {n_skp} skipped · 0 loaded")

    if total_stats:
        cols = st.columns(len(total_stats))
        for col, (k, v) in zip(cols, total_stats.items()):
            col.metric(k.replace("_", " ").title(), f"{v:,}")

    if skipped:
        st.warning(f"⚠️ {n_skp} skipped")
        for fname, reason in skipped:
            st.caption(f"**{fname}** — {reason}")

    if errors:
        for fname, reason in errors:
            st.error(f"❌ **{fname}** — {reason}")
