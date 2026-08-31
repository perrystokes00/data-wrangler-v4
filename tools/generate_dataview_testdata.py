"""
generate_dataview_testdata.py
=============================
Generates a realistic synthetic Permian Basin / Gulf Coast dataset
and loads it into the DataView schema on SQL Server.

  10 operators · 5 fields · 50 wells · surveys · formation tops ·
  logs · core + photos · DSTs · completions · casing · mud logs ·
  petrophysics · production volumes

Usage:
    python generate_dataview_testdata.py --server "127.0.0.1\\SQLEXPRESS" --database DataView --windows-auth
    python generate_dataview_testdata.py --server "127.0.0.1\\SQLEXPRESS" --database DataView --username sa --password secret
    python generate_dataview_testdata.py ... --wipe   # clear existing data first
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

try:
    from sqlalchemy import create_engine, text
    HAS_SQLA = True
except ImportError:
    HAS_SQLA = False

# ── Reproducible randomness ───────────────────────────────────────────────────
random.seed(42)

# =============================================================================
# HELPERS
# =============================================================================

def _uid() -> str:
    return str(uuid.uuid4()).replace("-", "")[:40]

def _sha1(val: str) -> str:
    return hashlib.sha1(val.upper().encode()).hexdigest()[:40]

def _rnd_date(start: str, end: str) -> datetime:
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end,   "%Y-%m-%d")
    return s + timedelta(days=random.randint(0, (e - s).days))

def _rnd(lo: float, hi: float, dp: int = 4) -> float:
    return round(random.uniform(lo, hi), dp)

def _choice(lst):
    return random.choice(lst)

def _bulk_insert(con, table: str, rows: list[dict]):
    if not rows:
        return
    cols   = list(rows[0].keys())
    params = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    sql = f"INSERT INTO dataview.{table} ({col_list}) VALUES ({params})"
    con.execute(text(sql), rows)

# =============================================================================
# REFERENCE DATA
# =============================================================================

OPERATORS = [
    ("Pioneer Natural Resources",  "PIONEER"),
    ("ConocoPhillips",             "COP"),
    ("Diamondback Energy",         "FANG"),
    ("Occidental Petroleum",       "OXY"),
    ("Devon Energy",               "DVN"),
    ("EOG Resources",              "EOG"),
    ("Coterra Energy",             "CTRA"),
    ("Ovintiv",                    "OVV"),
    ("Marathon Oil",               "MRO"),
    ("SM Energy",                  "SM"),
]

FIELDS = [
    ("Midland Basin",   "MIDLAND",    31.9,  -102.1, "CONVENTIONAL"),
    ("Delaware Basin",  "DELAWARE",   31.5,  -104.0, "UNCONVENTIONAL"),
    ("Spraberry Trend", "SPRABERRY",  32.2,  -101.8, "UNCONVENTIONAL"),
    ("Wolfcamp Play",   "WOLFCAMP",   31.7,  -102.5, "UNCONVENTIONAL"),
    ("Bone Spring",     "BONESPRING", 31.3,  -103.8, "UNCONVENTIONAL"),
]

FORMATIONS = [
    # (name, set, type,       top_ft, thick_ft, age_ma)
    ("Wolfcamp A",    "PERMIAN BASIN", "FORMATION", 7200, 400, 295.0),
    ("Wolfcamp B",    "PERMIAN BASIN", "FORMATION", 7600, 350, 296.5),
    ("Spraberry",     "PERMIAN BASIN", "FORMATION", 6800, 300, 272.0),
    ("Dean",          "PERMIAN BASIN", "MEMBER",    7100, 120, 280.0),
    ("Bone Spring",   "PERMIAN BASIN", "FORMATION", 8000, 500, 298.0),
    ("Clearfork",     "PERMIAN BASIN", "GROUP",     5500, 800, 270.0),
    ("Avalon Shale",  "PERMIAN BASIN", "MEMBER",    7900, 200, 297.0),
    ("Strawn",        "PERMIAN BASIN", "FORMATION", 9200, 400, 307.0),
    ("Canyon",        "PERMIAN BASIN", "GROUP",     8800, 600, 312.0),
    ("Ellenburger",   "PERMIAN BASIN", "GROUP",    12000, 800, 480.0),
]

WELL_TYPES   = ["HORIZONTAL", "HORIZONTAL", "HORIZONTAL", "HORIZONTAL", "DEVELOPMENT",
                "EXPLORATORY", "HORIZONTAL", "HORIZONTAL", "DEVELOPMENT", "OIL"]
WELL_STATUSES = ["ACTIVE", "SHUT_IN", "COMPLETED", "ACTIVE", "ACTIVE",
                 "ABANDONED", "ACTIVE", "SHUT_IN", "COMPLETED", "ACTIVE"]
COUNTIES     = ["Midland", "Martin", "Loving", "Ward", "Reeves",
                "Pecos", "Upton", "Andrews", "Ector", "Crane"]
CURVE_SUITES = {
    "WIRELINE": ["GR", "RHOB", "NPHI", "RT", "DT", "CALI", "SP"],
    "MWD":      ["GR", "AZIM", "INCL", "ROP", "WOB", "RPM", "ECD"],
    "LWD":      ["GR", "RHOB", "NPHI", "RT", "AZIM", "INCL", "ROP"],
}
# Only UOM codes seeded in dv_r_uom; None = leave null for unmapped mnemonics
CURVE_UNITS  = {"GR":"GAPI","RHOB":"G_CC","NPHI":"FRAC","RT":"OHMM","DT":"US_FT",
                "CALI":None,"SP":None,"AZIM":"DEG","INCL":"DEG","ROP":None,
                "WOB":None,"RPM":None,"ECD":"G_CC"}

# =============================================================================
# CONNECTION
# =============================================================================

def _build_engine(args):
    if not HAS_SQLA:
        print("ERROR: sqlalchemy not installed.  pip install sqlalchemy pyodbc")
        sys.exit(1)
    import pyodbc  # noqa
    server   = args.server   or os.getenv("DB_SERVER", "")
    database = args.database or os.getenv("DB_NAME", "DataView")
    driver   = args.driver   or os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
    if not server:
        print("ERROR: No server specified. Pass --server or set DB_SERVER in .env")
        sys.exit(1)
    if args.windows_auth:
        odbc = f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
    else:
        odbc = (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
                f"UID={args.username};PWD={args.password};")
    def _creator():
        return pyodbc.connect(odbc)
    return create_engine("mssql+pyodbc://", creator=_creator,
                         fast_executemany=True, pool_pre_ping=False)

# =============================================================================
# WIPE
# =============================================================================

WIPE_ORDER = [
    "dv_well_petro_zone","dv_well_petro_interp",
    "dv_well_shows","dv_well_mud_log",
    "dv_well_casing",
    "dv_well_stimulation","dv_well_perforation","dv_well_completion",
    "dv_well_pressure","dv_well_dst_period","dv_well_dst",
    "dv_well_core_photo","dv_well_core_sample","dv_well_core",
    "dv_well_log_curve","dv_well_log",
    "dv_strat_interval","dv_well_formation_top",
    "dv_well_dir_srvy_sta","dv_well_dir_srvy_hdr",
    "dv_prod_volume","dv_prod_entity",
    "dv_well_alias","dv_well",
    "dv_field","dv_business_associate",
]

def _wipe(con):
    print("  Wiping existing test data …")
    for tbl in WIPE_ORDER:
        con.execute(text(f"DELETE FROM dataview.{tbl}"))
    print("  Done.")

# =============================================================================
# GENERATORS
# =============================================================================

def gen_operators(con) -> dict[str, str]:
    """Returns {short_name: ba_id}"""
    print("  Inserting operators …")
    result = {}
    rows = []
    for name, short in OPERATORS:
        ba_id = _sha1(name)
        result[short] = ba_id
        rows.append(dict(
            ba_id=ba_id, ba_type="OPERATOR", ba_name=name,
            short_name=short, country="USA", state_province="TX",
            active_ind="Y", row_created_by="TESTGEN",
            source="DATAVIEW"
        ))
    _bulk_insert(con, "dv_business_associate", rows)
    print(f"    {len(rows)} operators")
    return result


def gen_fields(con, ba_map: dict) -> dict[str, str]:
    """Returns {short_name: field_id}"""
    print("  Inserting fields …")
    result = {}
    rows = []
    ops = list(ba_map.values())
    for name, short, lat, lon, play in FIELDS:
        fid = _sha1(name + "USA")
        result[short] = fid
        rows.append(dict(
            field_id=fid, field_name=name, field_type="OIL_GAS",
            country="USA", province_state="Texas",
            basin_name="Permian Basin",
            operator_ba_id=random.choice(ops),
            discovery_date=_rnd_date("1960-01-01","2005-12-31"),
            field_status="ACTIVE", onshore_offshore_ind="ONSHORE",
            surface_latitude=round(lat + _rnd(-0.2,0.2),6),
            surface_longitude=round(lon + _rnd(-0.2,0.2),6),
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
    _bulk_insert(con, "dv_field", rows)
    print(f"    {len(rows)} fields")
    return result


def gen_wells(con, ba_map: dict, field_map: dict) -> list[str]:
    """Returns list of uwi"""
    print("  Inserting wells …")
    uwis = []
    well_rows  = []
    alias_rows = []
    ops    = list(ba_map.values())
    fields = list(field_map.values())

    for i in range(1, 51):
        api   = f"42{random.randint(100,499):03d}{i:05d}0000"
        uwi   = f"US42{random.randint(100,499):03d}{i:05d}0000"
        lat   = _rnd(31.0, 32.5)
        lon   = _rnd(-104.5, -101.0)
        spud  = _rnd_date("2010-01-01", "2024-06-01")
        compl = spud + timedelta(days=random.randint(60, 180))
        td    = _rnd(8000, 15000, 1)
        wtype = _choice(WELL_TYPES)
        wstat = _choice(WELL_STATUSES)
        op    = _choice(ops)
        fid   = _choice(fields)
        county= _choice(COUNTIES)

        uwis.append(uwi)
        well_rows.append(dict(
            uwi=uwi,
            well_name=f"STATE {county[:3].upper()} {i:03d}H" if "HORIZONTAL" in wtype
                      else f"STATE {county[:3].upper()} {i:03d}",
            well_num=f"{i:03d}",
            operator_ba_id=op,
            field_id=fid,
            well_type=wtype,
            well_status=wstat,
            country="USA", province_state="Texas", county=county,
            legal_survey_type="PLSS",
            surface_latitude=lat, surface_longitude=lon,
            ground_elevation=_rnd(2500,3500,1),
            ground_elevation_ouom="FT",
            kb_elevation=_rnd(2510,3510,1),
            kb_elevation_ouom="FT",
            spud_date=spud,
            completion_date=compl,
            final_td=td, final_td_ouom="FT",
            depth_datum="KB",
            depth_datum_elevation=_rnd(2510,3510,1),
            epsg_code=4326,
            api_num=api,
            license_num=f"TX-{i:05d}",
            onshore_offshore_ind="ONSHORE",
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # API alias
        alias_rows.append(dict(
            uwi=uwi, alias_id=_uid()[:20],
            alias_name=api, alias_type="API",
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))

    _bulk_insert(con, "dv_well", well_rows)
    _bulk_insert(con, "dv_well_alias", alias_rows)
    print(f"    {len(well_rows)} wells, {len(alias_rows)} aliases")
    return uwis


def gen_surveys(con, uwis: list[str]):
    print("  Inserting directional surveys …")
    hdr_rows = []
    sta_rows = []
    for uwi in uwis:
        survey_id = _uid()[:20]
        td = _rnd(8000, 15000, 1)
        hdr_rows.append(dict(
            uwi=uwi, survey_id=survey_id,
            survey_type=_choice(["MWD","GYRO","MWD","MWD"]),
            survey_date=_rnd_date("2010-01-01","2024-12-31"),
            depth_datum="KB",
            survey_top_depth=100.0,
            survey_base_depth=td,
            depth_ouom="FT",
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # ~50 stations per well
        md = 0.0
        incl = 0.0
        azim = _rnd(0, 360, 1)
        for s in range(50):
            md   += _rnd(150, 300, 1)
            if md > td:
                break
            incl  = min(90.0, incl + _rnd(-2, 8, 2))
            azim += _rnd(-5, 5, 1)
            tvd   = md * (1 - incl/200)
            sta_rows.append(dict(
                uwi=uwi, survey_id=survey_id,
                station_id=f"{s+1:04d}",
                md=round(md,2), incl=round(incl,2),
                azim=round(azim % 360, 2),
                tvd=round(tvd,2),
                ns_offset=round(md * (incl/100) * 0.5, 2),
                ew_offset=round(md * (incl/100) * 0.3, 2),
                dls=_rnd(0, 3, 3),
                depth_ouom="FT",
                row_created_by="TESTGEN", source="DATAVIEW"
            ))
    _bulk_insert(con, "dv_well_dir_srvy_hdr", hdr_rows)
    _bulk_insert(con, "dv_well_dir_srvy_sta", sta_rows)
    print(f"    {len(hdr_rows)} survey headers, {len(sta_rows)} stations")


def gen_formation_tops(con, uwis: list[str]) -> list[tuple]:
    """Returns [(uwi, strat_unit_id, interp_id)] for use by interval/petro generators"""
    print("  Inserting formation tops …")
    top_rows  = []
    int_rows  = []
    picks = []
    for uwi in uwis:
        # Pick 3 random formations per well
        selected = random.sample(FORMATIONS, 3)
        for form in selected:
            fname, fset, ftype, base_top, thick, age = form
            top_depth  = _rnd(base_top - 200, base_top + 200, 1)
            base_depth = top_depth + _rnd(thick * 0.7, thick * 1.3, 1)
            uid        = _sha1(fname)
            iid        = "1"
            picks.append((uwi, uid, iid))
            top_rows.append(dict(
                uwi=uwi, strat_unit_id=uid, interp_id=iid,
                strat_name_set=fset, strat_unit_name=fname,
                strat_unit_type=ftype, strat_unit_subtype="RESERVOIR",
                age_top_ma=age, age_base_ma=round(age+1.5,1),
                lithology=_choice(["SANDSTONE","LIMESTONE","SHALE","DOLOMITE"]),
                top_depth=top_depth, base_depth=base_depth,
                depth_ouom="FT", depth_datum="KB",
                tvd_top=round(top_depth*0.95,1),
                tvd_base=round(base_depth*0.95,1),
                owc_depth=round(base_depth - _rnd(50,150),1),
                interp_date=_rnd_date("2015-01-01","2024-12-31"),
                confidence_level=_choice(["HIGH","MEDIUM","LOW"]),
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
            # 2 sub-intervals per pick
            mid = (top_depth + base_depth) / 2
            for k, (itop, ibase, itype) in enumerate([
                (top_depth, mid, "PAY"),
                (mid, base_depth, "NON-PAY")
            ]):
                int_rows.append(dict(
                    uwi=uwi, strat_unit_id=uid, interp_id=iid,
                    interval_id=f"{k+1}",
                    interval_type=itype,
                    interval_name=f"{fname} {'Upper' if k==0 else 'Lower'}",
                    top_depth=round(itop,1), base_depth=round(ibase,1),
                    net_thickness=round((ibase-itop)*_rnd(0.3,0.9),1),
                    depth_ouom="FT",
                    porosity=_rnd(0.05, 0.18, 4),
                    water_saturation=_rnd(0.2, 0.65, 4),
                    permeability=_rnd(0.01, 50, 3),
                    perm_ouom="MD",
                    fluid_type=_choice(["OIL","GAS","OIL","CONDENSATE"]),
                    row_created_by="TESTGEN", source="DATAVIEW"
                ))
    _bulk_insert(con, "dv_well_formation_top", top_rows)
    _bulk_insert(con, "dv_strat_interval", int_rows)
    print(f"    {len(top_rows)} formation tops, {len(int_rows)} strat intervals")
    return picks


def gen_logs(con, uwis: list[str]):
    print("  Inserting well logs …")
    log_rows   = []
    curve_rows = []
    for uwi in uwis:
        log_type  = _choice(list(CURVE_SUITES.keys()))
        log_id    = _uid()[:20]
        td        = _rnd(8000,15000,1)
        log_rows.append(dict(
            uwi=uwi, log_id=log_id, log_type=log_type,
            run_num="1",
            log_date=_rnd_date("2010-01-01","2024-12-31"),
            depth_datum="KB", top_depth=100.0, base_depth=td,
            depth_ouom="FT", null_value=-999.25,
            file_path=f"raw\\logs\\{uwi}_{log_type}.las",
            file_format="LAS",
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        for j, mnem in enumerate(CURVE_SUITES[log_type]):
            unit = CURVE_UNITS.get(mnem, "UNITLESS")
            if unit not in ["GAPI","G_CC","FRAC","OHMM","US_FT","DEG","FT"]:
                unit = "UNITLESS"
            curve_rows.append(dict(
                uwi=uwi, log_id=log_id, curve_id=f"{j+1:03d}",
                mnemonic=mnem,
                curve_description=f"{mnem} curve",
                curve_unit=unit if unit in ["GAPI","G_CC","FRAC","OHMM","US_FT","DEG"] else None,
                null_value=-999.25,
                top_depth=100.0, base_depth=td, depth_ouom="FT",
                min_value=_rnd(0,50,4), max_value=_rnd(50,200,4),
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
    _bulk_insert(con, "dv_well_log", log_rows)
    _bulk_insert(con, "dv_well_log_curve", curve_rows)
    print(f"    {len(log_rows)} log runs, {len(curve_rows)} curves")


def gen_core(con, uwis: list[str], ba_map: dict):
    print("  Inserting core data …")
    core_rows   = []
    sample_rows = []
    photo_rows  = []
    ops         = list(ba_map.values())
    cored_wells = random.sample(uwis, 20)
    for uwi in cored_wells:
        core_id  = _uid()[:20]
        top_d    = _rnd(7000,9000,1)
        base_d   = top_d + _rnd(60,300,1)
        attempt  = base_d - top_d
        recovery = attempt * _rnd(0.7,0.98)
        core_rows.append(dict(
            uwi=uwi, core_id=core_id, core_num="1",
            core_type=_choice(["CONVENTIONAL","CONVENTIONAL","SIDEWALL"]),
            core_show=_choice(["OIL","GAS","OIL","TRACE","NONE"]),
            top_depth=top_d, base_depth=base_d,
            depth_ouom="FT", depth_datum="KB",
            core_length=round(attempt,1),
            recovery_length=round(recovery,1),
            recovery_pct=round(recovery/attempt*100,1),
            length_ouom="FT",
            core_date=_rnd_date("2015-01-01","2024-12-31"),
            strat_unit_name=_choice([f[0] for f in FORMATIONS]),
            file_path=f"raw\\core_reports\\{uwi}_core1.pdf",
            photo_count=random.randint(4,12),
            photo_folder_path=f"raw\\core_photos\\{uwi}\\",
            has_uv_photos=_choice(["Y","Y","N"]),
            has_thin_section_photos=_choice(["Y","N","N"]),
            cutting_company_ba_id=_choice(ops),
            analysis_company_ba_id=_choice(ops),
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # 4 samples per core
        for s in range(4):
            depth = top_d + (s+1) * (attempt/5)
            sample_rows.append(dict(
                uwi=uwi, core_id=core_id, sample_id=f"{s+1:03d}",
                sample_type=_choice(["PLUG","PLUG","SIDEWALL"]),
                sample_depth=round(depth,1),
                top_depth=round(depth-0.5,1),
                base_depth=round(depth+0.5,1),
                depth_ouom="FT",
                porosity_frac=_rnd(0.04,0.22,6),
                permeability_air_md=_rnd(0.001,100,4),
                permeability_klinkenberg_md=_rnd(0.001,80,4),
                water_saturation_frac=_rnd(0.15,0.65,6),
                grain_density_g_cc=_rnd(2.60,2.71,4),
                bulk_density_g_cc=_rnd(2.10,2.55,4),
                lithology=_choice(["SANDSTONE","LIMESTONE","DOLOMITE"]),
                visual_porosity=_choice(["GOOD","FAIR","POOR","EXCELLENT"]),
                hydrocarbon_show=_choice(["OIL STAIN","GAS CUT","FLUORESCENCE","NONE"]),
                oil_saturation_frac=_rnd(0.05,0.60,6),
                gas_saturation_frac=_rnd(0.0,0.20,6),
                formation_factor=_rnd(5.0,50.0,4),
                cementation_exponent=_rnd(1.8,2.2,4),
                saturation_exponent=_rnd(1.8,2.2,4),
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
        # 2 photos per core
        for p in range(2):
            photo_rows.append(dict(
                uwi=uwi, core_id=core_id,
                photo_id=_sha1(f"{uwi}{core_id}{p}"),
                photo_type=_choice(["TRAY","SLAB","UV","OVERVIEW"]),
                lighting=_choice(["WHITE","UV","WHITE"]),
                top_depth=round(top_d + p*(attempt/3),1),
                base_depth=round(top_d + (p+1)*(attempt/3),1),
                depth_ouom="FT",
                tray_num=p+1,
                photo_date=_rnd_date("2015-01-01","2024-12-31"),
                file_path=f"raw\\core_photos\\{uwi}\\tray_{p+1:03d}.jpg",
                file_name=f"tray_{p+1:03d}.jpg",
                file_ext=".jpg",
                file_size_kb=_rnd(800,4000,1),
                resolution_dpi=300,
                width_px=4096, height_px=2048,
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
    _bulk_insert(con, "dv_well_core",        core_rows)
    _bulk_insert(con, "dv_well_core_sample", sample_rows)
    _bulk_insert(con, "dv_well_core_photo",  photo_rows)
    print(f"    {len(core_rows)} cores, {len(sample_rows)} samples, {len(photo_rows)} photos")


def gen_dst(con, uwis: list[str], ba_map: dict):
    print("  Inserting DSTs …")
    dst_rows    = []
    period_rows = []
    pres_rows   = []
    ops       = list(ba_map.values())
    dst_wells   = random.sample(uwis, 15)
    for uwi in dst_wells:
        dst_id   = _uid()[:20]
        top_d    = _rnd(7000,10000,1)
        base_d   = top_d + _rnd(50,200,1)
        sipress  = _rnd(3000,7000,1)
        result   = _choice(["OIL","GAS","OIL","CONDENSATE","DRY"])
        dst_rows.append(dict(
            uwi=uwi, dst_id=dst_id, dst_num="1",
            test_type="DST",
            test_date=_rnd_date("2015-01-01","2024-12-31"),
            top_depth=top_d, base_depth=base_d,
            depth_ouom="FT", depth_datum="KB",
            strat_unit_name=_choice([f[0] for f in FORMATIONS]),
            tool_type=_choice(["OPEN_HOLE","CASED_HOLE"]),
            max_shut_in_pressure=sipress,
            final_shut_in_pressure=round(sipress*_rnd(0.9,1.0),1),
            pressure_ouom="PSI",
            max_oil_rate=_rnd(100,2000,1) if result in ["OIL","CONDENSATE"] else 0,
            max_gas_rate=_rnd(500,10000,1) if result in ["GAS","CONDENSATE"] else 0,
            max_water_rate=_rnd(10,200,1),
            rate_ouom="BOPD",
            gor=_rnd(500,5000,1) if result=="OIL" else None,
            api_gravity=_rnd(32,52,1) if result in ["OIL","CONDENSATE"] else None,
            h2s_pct=_rnd(0,0.5,3),
            co2_pct=_rnd(0.1,3.0,3),
            test_result=result,
            file_path=f"raw\\dst_reports\\{uwi}_dst1.pdf",
            contractor_ba_id=_choice(ops),
            perforation_top=top_d + _rnd(10,50,1),
            perforation_base=base_d - _rnd(10,50,1),
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # IFP, FF, ISI, FSI periods
        for seq, ptype in enumerate(["IFP","FF","ISI","FSI"]):
            period_rows.append(dict(
                uwi=uwi, dst_id=dst_id,
                period_id=f"{seq+1:02d}",
                period_type=ptype,
                period_seq=seq+1,
                duration_min=_rnd(30,480,1),
                start_pressure=_rnd(1000,6000,1),
                end_pressure=_rnd(2000,7000,1),
                pressure_ouom="PSI",
                avg_oil_rate=_rnd(0,1500,1),
                avg_gas_rate=_rnd(0,8000,1),
                avg_water_rate=_rnd(0,100,1),
                rate_ouom="BOPD",
                choke_size=_choice(["8/64","12/64","16/64","24/64","32/64"]),
                row_created_by="TESTGEN", source="DATAVIEW"
            ))
        # 2 RFT pressure points per well
        for k in range(2):
            pres_rows.append(dict(
                uwi=uwi, pressure_id=_uid()[:20],
                pressure_type=_choice(["RFT","MDT","BHP"]),
                test_date=_rnd_date("2015-01-01","2024-12-31"),
                depth=_rnd(7000,11000,1), depth_ouom="FT", depth_datum="KB",
                pressure=_rnd(3000,7500,1), pressure_ouom="PSI",
                temperature=_rnd(150,250,1), temperature_ouom="DEGF",
                fluid_type=_choice(["OIL","GAS","WATER"]),
                mobility=_rnd(0.1,100,3),
                strat_unit_name=_choice([f[0] for f in FORMATIONS]),
                tool_type=_choice(["RFT","MDT"]),
                contractor_ba_id=_choice(ops),
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
    _bulk_insert(con, "dv_well_dst",        dst_rows)
    _bulk_insert(con, "dv_well_dst_period", period_rows)
    _bulk_insert(con, "dv_well_pressure",   pres_rows)
    print(f"    {len(dst_rows)} DSTs, {len(period_rows)} periods, {len(pres_rows)} pressure points")


def gen_completions(con, uwis: list[str], ba_map: dict):
    print("  Inserting completions, perforations, stimulations, casing …")
    comp_rows  = []
    perf_rows  = []
    stim_rows  = []
    casing_rows= []
    ops = list(ba_map.values())
    comp_wells = random.sample(uwis, 30)
    for uwi in comp_wells:
        comp_id = _uid()[:20]
        top_d   = _rnd(7000,10000,1)
        base_d  = top_d + _rnd(100,500,1)
        comp_rows.append(dict(
            uwi=uwi, completion_id=comp_id,
            completion_type=_choice(["CASED_PERFORATED","CASED_PERFORATED","OPENHOLE"]),
            completion_date=_rnd_date("2015-01-01","2024-12-31"),
            top_depth=top_d, base_depth=base_d,
            depth_ouom="FT", depth_datum="KB",
            strat_unit_name=_choice([f[0] for f in FORMATIONS]),
            completion_status=_choice(["ACTIVE","ACTIVE","SHUT_IN"]),
            primary_fluid=_choice(["OIL","GAS","OIL","CONDENSATE"]),
            tubing_size_in=_choice([2.375,2.875,3.5]),
            tubing_depth=top_d - 50,
            artificial_lift_type=_choice(["ESP","SUCKER_ROD","GL","NONE","NONE"]),
            operator_ba_id=_choice(ops),
            contractor_ba_id=_choice(ops),
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # Perforations — 2 intervals
        for p in range(2):
            mid = top_d + (base_d-top_d)*(p+1)/3
            perf_rows.append(dict(
                uwi=uwi, completion_id=comp_id,
                perf_id=f"{p+1:02d}",
                perf_date=_rnd_date("2015-01-01","2024-12-31"),
                top_depth=round(mid-15,1), base_depth=round(mid+15,1),
                depth_ouom="FT",
                shot_count=random.randint(30,120),
                shot_density=_rnd(4,6,1), shot_density_ouom="SPF",
                perf_diameter_in=_rnd(0.3,0.5,3),
                gun_type="DP HOLLOW CARRIER",
                phasing_deg=_choice([60,90,120,180]),
                strat_unit_name=_choice([f[0] for f in FORMATIONS]),
                perf_status="OPEN",
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
        # Stimulation — hydraulic frac
        stages = random.randint(20,50)
        stim_rows.append(dict(
            uwi=uwi, completion_id=comp_id, stim_id="01",
            stim_type="HYDRAULIC_FRAC",
            stim_date=_rnd_date("2015-01-01","2024-12-31"),
            top_depth=top_d, base_depth=base_d, depth_ouom="FT",
            stage_count=stages,
            fluid_type=_choice(["SLICKWATER","CROSSLINK","HYBRID"]),
            fluid_volume=stages * _rnd(10000,25000,1),
            fluid_volume_ouom="BBL",
            proppant_type=_choice(["SAND","RESIN_COATED","CERAMIC"]),
            proppant_mesh=_choice(["40/70","30/50","100 mesh"]),
            proppant_mass=stages * _rnd(100000,400000,1),
            proppant_mass_ouom="LB",
            max_treating_pressure=_rnd(6000,12000,1),
            avg_treating_pressure=_rnd(5000,10000,1),
            pressure_ouom="PSI",
            max_pump_rate=_rnd(80,120,1), rate_ouom="BBL_MIN",
            isip=_rnd(4000,8000,1),
            closure_pressure=_rnd(3500,7000,1),
            file_path=f"raw\\frac_reports\\{uwi}_frac.pdf",
            service_co_ba_id=_choice(ops),
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # Casing strings — 3 per well
        for ctype, od, wt, grade, top, base in [
            ("SURFACE",      9.625, 36.0, "K55",  0,     2000),
            ("INTERMEDIATE", 7.0,   26.0, "N80",  0,     6000),
            ("PRODUCTION",   5.5,   20.0, "P110", 0,     top_d+100),
        ]:
            casing_rows.append(dict(
                uwi=uwi, casing_id=_uid()[:20],
                casing_type=ctype,
                set_date=_rnd_date("2015-01-01","2024-12-31"),
                top_depth=float(top), base_depth=float(base),
                depth_ouom="FT", depth_datum="KB",
                od_in=od, weight_lb_ft=wt, grade=grade,
                connection_type=_choice(["BTC","LTC","PH6"]),
                cement_top=float(top),
                cement_base=float(base),
                cement_volume_sacks=_rnd(100,800,1),
                cement_type=_choice(["CLASS G","CLASS H","PREMIUM"]),
                burst_rating_psi=_rnd(3000,10000,1),
                collapse_rating_psi=_rnd(2000,8000,1),
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
    _bulk_insert(con, "dv_well_completion",  comp_rows)
    _bulk_insert(con, "dv_well_perforation", perf_rows)
    _bulk_insert(con, "dv_well_stimulation", stim_rows)
    _bulk_insert(con, "dv_well_casing",      casing_rows)
    print(f"    {len(comp_rows)} completions, {len(perf_rows)} perfs, "
          f"{len(stim_rows)} stims, {len(casing_rows)} casing strings")


def gen_mud_logs(con, uwis: list[str], ba_map: dict):
    print("  Inserting mud logs and shows …")
    log_rows  = []
    show_rows = []
    ops       = list(ba_map.values())
    ml_wells  = random.sample(uwis, 20)
    for uwi in ml_wells:
        ml_id  = _uid()[:20]
        td     = _rnd(8000,15000,1)
        log_rows.append(dict(
            uwi=uwi, mud_log_id=ml_id,
            log_date=_rnd_date("2015-01-01","2024-12-31"),
            top_depth=0.0, base_depth=td, depth_ouom="FT",
            rop_avg=_rnd(30,150,1), rop_ouom="FT_HR",
            mud_type=_choice(["WBM","OBM","SBM"]),
            mud_weight_avg=_rnd(9.5,14.5,2), mud_weight_ouom="PPG",
            file_path=f"raw\\mud_logs\\{uwi}_mudlog.pdf",
            mud_logger_ba_id=_choice(ops),
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # 2–3 shows per well
        for s in range(random.randint(2,3)):
            top_d = _rnd(7000,11000,1)
            show_rows.append(dict(
                uwi=uwi, mud_log_id=ml_id, show_id=f"{s+1:02d}",
                show_type=_choice(["OIL","GAS","OIL_AND_GAS","CONDENSATE","FLUORESCENCE"]),
                show_rating=_choice(["EXCELLENT","GOOD","GOOD","FAIR","POOR"]),
                top_depth=top_d, base_depth=round(top_d+_rnd(10,60),1),
                depth_ouom="FT",
                strat_unit_name=_choice([f[0] for f in FORMATIONS]),
                lithology=_choice(["SANDSTONE","LIMESTONE","DOLOMITE"]),
                total_gas_units=_rnd(100,50000,1),
                c1_pct=_rnd(50,95,3),
                c2_pct=_rnd(2,15,3),
                c3_pct=_rnd(0.5,8,3),
                ic4_pct=_rnd(0.1,2,3),
                nc4_pct=_rnd(0.1,3,3),
                fluorescence_color=_choice(["YELLOW","WHITE","BLUE","BROWN",None]),
                fluorescence_intensity=_choice(["BRIGHT","MODERATE","FAINT",None]),
                row_created_by="TESTGEN", source="DATAVIEW"
            ))
    _bulk_insert(con, "dv_well_mud_log", log_rows)
    _bulk_insert(con, "dv_well_shows",   show_rows)
    print(f"    {len(log_rows)} mud logs, {len(show_rows)} shows")


def gen_petrophysics(con, uwis: list[str], picks: list[tuple], ba_map: dict):
    print("  Inserting petrophysics …")
    interp_rows = []
    zone_rows   = []
    ops         = list(ba_map.values())
    petro_wells = random.sample(uwis, 25)
    picks_by_uwi = {}
    for uwi, sid, iid in picks:
        picks_by_uwi.setdefault(uwi, []).append((sid, iid))

    for uwi in petro_wells:
        interp_id = _uid()[:20]
        interp_rows.append(dict(
            uwi=uwi, interp_id=interp_id,
            interp_name=f"Final Petro {random.randint(2018,2024)}",
            interp_date=_rnd_date("2018-01-01","2024-12-31"),
            software=_choice(["Techlog","Interactive Petrophysics","Petrel","Elan Plus"]),
            software_version=_choice(["2022.1","2023.2","10.4","3.2"]),
            formation_water_resist=_rnd(0.02,0.5,5),
            rw_temperature=_rnd(150,250,1), temperature_ouom="DEGF",
            archie_a=1.0, archie_m=_rnd(1.8,2.2,3), archie_n=_rnd(1.8,2.2,3),
            shale_volume_method=_choice(["GR_LINEAR","GR_LARIONOV","SP"]),
            porosity_method=_choice(["DENSITY","ND_CROSSPLOT","DENSITY"]),
            fluid_density_g_cc=_rnd(0.85,1.0,3),
            matrix_density_g_cc=_rnd(2.65,2.71,3),
            sw_method=_choice(["ARCHIE","SIMANDOUX","INDONESIA"]),
            output_file_path=f"curated\\petro\\{uwi}_petro_final.las",
            interp_status="FINAL",
            analyst_ba_id=_choice(ops),
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # One zone per formation pick
        for sid, iid in picks_by_uwi.get(uwi, [])[:2]:
            fname = next((f[0] for f in FORMATIONS if _sha1(f[0])==sid), "Unknown")
            phi   = _rnd(0.06,0.20,6)
            sw    = _rnd(0.20,0.65,6)
            ntg   = _rnd(0.30,0.90,4)
            gross = _rnd(80,400,1)
            net   = round(gross*ntg,1)
            pay   = "Y" if phi > 0.10 and sw < 0.55 else "N"
            zone_rows.append(dict(
                uwi=uwi, interp_id=interp_id,
                zone_id=_uid()[:20],
                zone_name=fname,
                zone_type="RESERVOIR",
                top_depth=_rnd(7000,10000,1),
                base_depth=_rnd(10001,10400,1),
                depth_ouom="FT", depth_datum="KB",
                strat_unit_id=sid, strat_interp_id=iid,
                strat_unit_name=fname,
                gross_thickness=gross, net_thickness=net,
                net_to_gross=round(ntg,6),
                vsh_avg=_rnd(0.10,0.45,6),
                phi_total_avg=round(phi+0.02,6),
                phi_effective_avg=round(phi,6),
                phi_method="DENSITY",
                sw_avg=round(sw,6),
                sw_min=round(sw*0.8,6),
                sw_max=round(sw*1.2,6),
                sw_method="ARCHIE",
                sh_avg=round(1-sw,6),
                perm_avg_md=_rnd(0.01,80,4),
                perm_geomean_md=_rnd(0.005,50,4),
                perm_method="TIMUR",
                bvw_avg=round(phi*sw,6),
                bvh_avg=round(phi*(1-sw),6),
                fluid_type=_choice(["OIL","GAS","OIL","CONDENSATE"]),
                pay_flag=pay,
                pay_cutoff_phi=0.08,
                pay_cutoff_sw=0.55,
                pay_cutoff_vsh=0.40,
                active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
            ))
    _bulk_insert(con, "dv_well_petro_interp", interp_rows)
    _bulk_insert(con, "dv_well_petro_zone",   zone_rows)
    print(f"    {len(interp_rows)} interpretations, {len(zone_rows)} zones")


def gen_production(con, uwis: list[str], field_map: dict, ba_map: dict):
    print("  Inserting production …")
    entity_rows = []
    vol_rows    = []
    ops    = list(ba_map.values())
    fields = list(field_map.values())
    for uwi in uwis:
        eid = _sha1("PROD" + uwi)
        first_prod = _rnd_date("2015-01-01","2022-01-01")
        ptype = _choice(["OIL","GAS","OIL","CONDENSATE"])
        entity_rows.append(dict(
            prod_entity_id=eid, uwi=uwi,
            field_id=_choice(fields),
            operator_ba_id=_choice(ops),
            prod_entity_type="WELL",
            prod_entity_name=f"PROD-{uwi[-6:]}",
            first_prod_date=first_prod,
            last_prod_date=_rnd_date("2023-01-01","2024-12-31"),
            primary_fluid=ptype,
            active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
        ))
        # 12 months of production
        for m in range(12):
            period = (first_prod + timedelta(days=30*m)).strftime("%Y-%m")
            decline = 1.0 / (1 + 0.1*m)  # simple hyperbolic decline
            for fluid, base_vol in [("OIL",5000), ("GAS",8000), ("WATER",1000)]:
                vol = base_vol * decline * _rnd(0.7,1.3)
                vol_rows.append(dict(
                    prod_entity_id=eid,
                    period_date=period,
                    fluid_type=fluid,
                    volume=round(vol,2),
                    volume_ouom="BBL" if fluid!="GAS" else "MCF",
                    days_on_prod=_rnd(25,30.5,1),
                    avg_daily_rate=round(vol/30,2),
                    rate_ouom="BOPD" if fluid != "GAS" else "MCFD",
                    active_ind="Y", row_created_by="TESTGEN", source="DATAVIEW"
                ))
    # Deduplicate vol_rows on PK (prod_entity_id, period_date, fluid_type)
    seen = set()
    deduped_vol = []
    for r in vol_rows:
        key = (r["prod_entity_id"], r["period_date"], r["fluid_type"])
        if key not in seen:
            seen.add(key)
            deduped_vol.append(r)
    _bulk_insert(con, "dv_prod_entity", entity_rows)
    _bulk_insert(con, "dv_prod_volume", deduped_vol)
    print(f"    {len(entity_rows)} prod entities, {len(deduped_vol)} monthly volumes")


# =============================================================================
# SUMMARY
# =============================================================================

def _summary(con):
    sql = """
        SELECT TABLE_NAME,
               (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS c
                WHERE c.TABLE_SCHEMA='dataview' AND c.TABLE_NAME=t.TABLE_NAME) AS cols
        FROM   INFORMATION_SCHEMA.TABLES t
        WHERE  TABLE_SCHEMA='dataview' AND TABLE_TYPE='BASE TABLE'
        ORDER  BY TABLE_NAME
    """
    tables = con.execute(text(sql)).fetchall()
    print(f"\n  {'Table':<35} {'Rows':>8}")
    print(f"  {'-'*35} {'-'*8}")
    total_rows = 0
    for (tbl, _) in tables:
        cnt = con.execute(text(
            f"SELECT COUNT(*) FROM dataview.[{tbl}]"
        )).scalar()
        print(f"  {tbl:<35} {cnt:>8,}")
        total_rows += cnt
    print(f"\n  Total rows loaded: {total_rows:,}")


# =============================================================================
# CLI + MAIN
# =============================================================================

def _parse_args():
    p = argparse.ArgumentParser(description="Generate synthetic DataView test data.")
    p.add_argument("--server",       default="")
    p.add_argument("--database",     default="DataView_Demo")
    p.add_argument("--windows-auth", action="store_true")
    p.add_argument("--username",     default="")
    p.add_argument("--password",     default="")
    p.add_argument("--driver",       default="ODBC Driver 17 for SQL Server")
    p.add_argument("--wipe",         action="store_true",
                   help="Delete existing test data before inserting")
    return p.parse_args()


def main():
    args = _parse_args()

    print()
    print("=" * 60)
    print("  DataView — Synthetic Test Data Generator")
    print("  Permian Basin dataset · 50 wells")
    print("=" * 60)

    engine = _build_engine(args)
    with engine.connect() as con:
        row = con.execute(text("SELECT @@VERSION")).fetchone()
        print(f"\n  Connected: {str(row[0]).split(chr(10))[0]}")

    with engine.begin() as con:
        if args.wipe:
            _wipe(con)

        print("\n  Generating data …\n")
        ba_map    = gen_operators(con)
        field_map = gen_fields(con, ba_map)
        uwis      = gen_wells(con, ba_map, field_map)
        gen_surveys(con, uwis)
        picks     = gen_formation_tops(con, uwis)
        gen_logs(con, uwis)
        gen_core(con, uwis, ba_map)
        gen_dst(con, uwis, ba_map)
        gen_completions(con, uwis, ba_map)
        gen_mud_logs(con, uwis, ba_map)
        gen_petrophysics(con, uwis, picks, ba_map)
        gen_production(con, uwis, field_map, ba_map)

    print("\n  Row counts:")
    with engine.connect() as con:
        _summary(con)

    print()
    print("=" * 60)
    print("  Test data load complete.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
