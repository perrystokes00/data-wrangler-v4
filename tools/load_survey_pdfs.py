"""
load_survey_pdfs.py
===================
Loads the 5 training directional survey PDFs into DataView.

Inserts:
  - dv_well_dir_srvy_hdr  (one row per well)
  - dv_well_dir_srvy_sta  (one row per survey station)

Wells must already exist in dv_well. If a UWI is not found the survey
is skipped — run this after the wells are loaded.

Usage:
    python load_survey_pdfs.py --server "127.0.0.1\SQLEXPRESS" --database DataView --windows-auth

    # Point at a different folder of survey PDFs:
    python load_survey_pdfs.py ... --folder "C:\Bulk\raw\surveys"
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import uuid
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber not installed. Run: pip install pdfplumber")

try:
    from sqlalchemy import create_engine, text
    import urllib.parse
except ImportError:
    sys.exit("sqlalchemy not installed. Run: pip install sqlalchemy pyodbc")


# =============================================================================
# SURVEY DATA  (extracted from training PDFs already read in this session)
# =============================================================================

SURVEYS = [
    {
        "file":      "Survey_ANADARKO_1H_Landmark.pdf",
        "uwi":       "42-317-12345-00-00",
        "well_name": "ANADARKO 1H",
        "survey_type": "MWD",
        "contractor":  "Halliburton Drilling Services",
        "stations": [
            (0.00,    0.00, 185.00, 600.00,     0.00,     0.00, 0.00),
            (600.00,  0.00, 185.00, 1200.00,    0.00,     0.00, 0.00),
            (1200.00, 0.00, 185.00, 1800.00,    0.00,     0.00, 0.00),
            (1800.00, 0.00, 185.00, 2400.00,    0.00,     0.00, 0.00),
            (2400.00, 0.00, 185.00, 3000.00,    0.00,     0.00, 0.00),
            (3000.00, 10.80, 185.17, 3596.45,  -56.15,   -5.08, 1.80),
            (3600.00, 21.60, 185.16, 4171.78, -222.62,  -20.12, 1.80),
            (4200.00, 32.40, 185.49, 4705.59, -493.37,  -46.12, 1.80),
            (4800.00, 43.20, 185.02, 5178.98, -859.16,  -78.23, 1.80),
            (5400.00, 54.00, 185.33, 5575.18,-1306.62, -119.94, 1.80),
            (6000.00, 64.80, 185.39, 5880.15,-1820.02, -168.39, 1.80),
            (6600.00, 75.60, 185.19, 6083.10,-2381.40, -219.37, 1.80),
            (7200.00, 86.40, 184.74, 6176.82,-2971.12, -268.22, 1.80),
            (7800.00, 86.60, 183.75, 6213.47,-3568.72, -307.38, 0.03),
            (8400.00, 86.70, 184.24, 6248.53,-4166.05, -351.69, 0.02),
            (9000.00, 86.66, 184.23, 6283.28,-4763.41, -395.88, 0.01),
            (9600.00, 86.83, 184.02, 6317.39,-5360.97, -437.89, 0.03),
            (10200.00,87.02, 184.07, 6349.61,-5958.59, -480.42, 0.03),
            (10800.00,86.85, 184.70, 6381.69,-6555.72, -529.48, 0.03),
            (11400.00,86.74, 184.81, 6415.22,-7152.67, -579.67, 0.02),
            (12000.00,86.66, 185.44, 6449.80,-7748.98, -636.44, 0.01),
            (12600.00,86.79, 184.88, 6484.11,-8345.83, -687.41, 0.02),
            (13200.00,86.84, 184.07, 6517.45,-8943.39, -729.94, 0.01),
            (13800.00,86.81, 183.27, 6550.66,-9541.50, -764.07, 0.01),
            (14400.00,86.67, 182.69, 6584.80,-10139.86,-792.18, 0.02),
            (15000.00,86.66, 181.84, 6619.73,-10738.53,-811.46, 0.00),
        ],
    },
    {
        "file":      "Survey_CONTINENTAL_1H_Simple.pdf",
        "uwi":       "35-101-10045-00-00",
        "well_name": "CONTINENTAL 1H",
        "survey_type": "MWD",
        "contractor":  "Continental Resources",
        "stations": [
            (0.00,    0.00, 92.30, 560.00,   0.00,   0.00, 0.00),
            (560.00,  0.00, 92.30,1120.00,   0.00,   0.00, 0.00),
            (1120.00, 0.00, 92.30,1680.00,   0.00,   0.00, 0.00),
            (1680.00, 0.00, 92.30,2240.00,   0.00,   0.00, 0.00),
            (2240.00,11.20, 92.52,2796.44,  -2.40,  54.51, 2.00),
            (2800.00,22.40, 92.98,3331.69, -10.79, 215.89, 2.00),
            (3360.00,33.60, 92.49,3825.35, -22.20, 478.13, 2.00),
            (3920.00,44.80, 92.30,4258.63, -36.41, 831.21, 2.00),
            (4480.00,56.00, 91.85,4615.02, -50.35,1261.79, 2.00),
            (5040.00,67.20, 92.23,4880.94, -69.47,1753.24, 2.00),
            (5600.00,78.40, 92.42,5046.28, -92.05,2286.86, 2.00),
            (6160.00,89.60, 92.75,5104.72,-118.72,2842.27, 2.00),
            (6720.00,89.77, 92.06,5107.81,-138.80,3401.90, 0.03),
            (7280.00,89.76, 92.15,5110.14,-159.78,3961.50, 0.00),
            (7840.00,89.76, 92.59,5112.50,-185.07,4520.93, 0.00),
            (8400.00,89.89, 92.06,5114.20,-205.19,5080.56, 0.02),
            (8960.00,89.73, 92.29,5116.04,-227.54,5640.11, 0.03),
            (9520.00,89.63, 92.94,5119.15,-256.27,6199.37, 0.02),
            (10080.00,89.62,93.32,5122.83,-288.72,6758.41, 0.00),
            (10640.00,89.56,92.53,5126.86,-313.44,7317.85, 0.01),
            (11200.00,89.48,91.86,5131.56,-331.58,7877.54, 0.01),
            (11760.00,89.37,91.94,5137.18,-350.58,8437.19, 0.02),
            (12320.00,89.31,92.43,5143.61,-374.36,8996.65, 0.01),
            (12880.00,89.37,92.97,5150.03,-403.42,9555.86, 0.01),
            (13440.00,89.49,93.03,5155.60,-432.99,10115.05,0.02),
            (14000.00,89.39,94.02,5161.07,-472.23,10673.64,0.02),
        ],
    },
    {
        "file":      "Survey_DEVON_ENERGY_1H_Baker.pdf",
        "uwi":       "35-059-22104-00-00",
        "well_name": "DEVON ENERGY 1H",
        "survey_type": "MWD",
        "contractor":  "Baker Hughes",
        "stations": [
            (0.00,    0.00, 315.00, 760.00,    0.00,    0.00, 0.00),
            (760.00,  0.00, 315.00,1520.00,    0.00,    0.00, 0.00),
            (1520.00, 0.00, 315.00,2280.00,    0.00,    0.00, 0.00),
            (2280.00, 0.00, 315.00,3040.00,    0.00,    0.00, 0.00),
            (3040.00, 0.00, 315.00,3800.00,    0.00,    0.00, 0.00),
            (3800.00,13.68, 315.34,4552.80,   64.23,  -63.47, 1.80),
            (4560.00,27.36, 315.74,5262.89,  254.58, -248.95, 1.80),
            (5320.00,41.04, 315.45,5889.98,  558.28, -547.93, 1.80),
            (6080.00,54.72, 315.90,6398.49,  962.12, -939.32, 1.80),
            (6840.00,68.40, 315.63,6759.57, 1438.68,-1405.55, 1.80),
            (7600.00,82.08, 315.28,6952.74, 1959.66,-1921.42, 1.80),
            (8360.00,90.00, 315.41,7005.18, 2499.20,-2453.24, 1.04),
            (9120.00,89.81, 316.00,7006.41, 3045.94,-2981.14, 0.02),
            (9880.00,89.67, 315.18,7009.82, 3585.00,-3516.86, 0.02),
            (10640.00,89.80,316.10,7013.32, 4132.65,-4043.80, 0.02),
            (11400.00,89.90,316.74,7015.29, 4686.15,-4564.60, 0.01),
            (12160.00,90.08,316.57,7015.41, 5238.08,-5087.07, 0.02),
            (12920.00,90.01,316.99,7014.85, 5793.78,-5605.53, 0.01),
            (13680.00,90.06,317.36,7014.43, 6352.81,-6120.39, 0.01),
            (14440.00,90.14,317.79,7013.11, 6915.69,-6631.04, 0.01),
            (15200.00,90.03,316.95,7011.95, 7471.09,-7149.83, 0.01),
            (15960.00,90.03,317.83,7011.54, 8034.38,-7660.02, 0.00),
            (16720.00,90.13,317.16,7010.50, 8591.67,-8176.77, 0.01),
            (17480.00,89.95,317.33,7009.99, 9150.44,-8691.91, 0.02),
            (18240.00,89.99,317.68,7010.41, 9712.35,-9203.64, 0.00),
            (19000.00,89.88,317.57,7011.29,10273.34,-9716.36, 0.01),
        ],
    },
    {
        "file":      "Survey_EOG_RESOURCES_3H_Landmark.pdf",
        "uwi":       "42-389-33211-00-00",
        "well_name": "EOG RESOURCES 3H",
        "survey_type": "MWD",
        "contractor":  "SLB Directional",
        "stations": [
            (0.00,    0.00, 225.00, 660.00,    0.00,    0.00, 0.00),
            (660.00,  0.00, 225.00,1320.00,    0.00,    0.00, 0.00),
            (1320.00, 0.00, 225.00,1980.00,    0.00,    0.00, 0.00),
            (1980.00, 0.00, 225.00,2640.00,    0.00,    0.00, 0.00),
            (2640.00, 0.00, 225.00,3300.00,    0.00,    0.00, 0.00),
            (3300.00,13.50, 224.61,3953.91,  -55.10,  -54.35, 2.05),
            (3960.00,27.00, 224.52,4571.69, -217.60, -214.15, 2.05),
            (4620.00,40.50, 224.18,5119.19, -479.97, -469.09, 2.05),
            (5280.00,54.00, 224.18,5566.16, -826.72, -806.09, 2.05),
            (5940.00,67.50, 224.49,5887.90,-1236.56,-1208.70, 2.05),
            (6600.00,81.00, 224.46,6066.64,-1688.86,-1652.63, 2.05),
            (7260.00,80.97, 224.13,6170.04,-2156.74,-2106.49, 0.00),
            (7920.00,80.80, 224.58,6274.55,-2620.94,-2563.88, 0.03),
            (8580.00,80.90, 224.81,6379.45,-3083.25,-3023.08, 0.02),
            (9240.00,81.07, 224.07,6482.86,-3551.57,-3476.49, 0.02),
            (9900.00,80.95, 224.77,6586.04,-4014.37,-3935.58, 0.02),
            (10560.00,81.01,224.80,6689.53,-4476.87,-4394.91, 0.01),
            (11220.00,81.12,225.36,6792.04,-4935.03,-4858.78, 0.02),
            (11880.00,81.14,225.73,6893.80,-5390.20,-5325.77, 0.00),
            (12540.00,81.04,226.14,6996.01,-5841.98,-5795.92, 0.02),
            (13200.00,81.00,226.38,7099.02,-6291.74,-6267.83, 0.01),
            (13860.00,81.04,226.60,7202.03,-6739.68,-6741.48, 0.01),
            (14520.00,81.12,225.75,7304.34,-7194.63,-7208.54, 0.01),
            (15180.00,81.01,225.35,7406.85,-7652.84,-7672.37, 0.02),
            (15840.00,81.19,225.66,7508.96,-8108.54,-8138.76, 0.03),
            (16500.00,81.06,226.03,7610.74,-8561.30,-8608.06, 0.02),
        ],
    },
    {
        "file":      "Survey_PIONEER_NATURAL_2H_Baker.pdf",
        "uwi":       "42-461-20987-00-00",
        "well_name": "PIONEER NATURAL 2H",
        "survey_type": "MWD",
        "contractor":  "Baker Hughes",
        "stations": [
            (0.00,    0.00, 270.50, 720.00,   0.33,   -81.08, 0.00),
            (720.00,  0.00, 270.50,1440.00,   0.33,   -81.08, 0.00),
            (1440.00, 0.00, 270.50,2160.00,   0.33,   -81.08, 0.00),
            (2160.00, 0.00, 270.50,2880.00,   0.33,   -81.08, 0.00),
            (2880.00, 0.00, 270.50,3600.00,   0.33,   -81.08, 0.00),
            (3600.00,12.96, 270.24,4313.88,   0.33,   -81.08, 1.80),
            (4320.00,25.92, 269.74,4991.38,  -0.75,  -320.20, 1.80),
            (5040.00,38.88, 270.14,5598.00,   0.20,  -705.17, 1.80),
            (5760.00,51.84, 270.19,6102.83,   1.91, -1216.38, 1.80),
            (6480.00,64.80, 269.86,6480.15,   0.42, -1827.79, 1.80),
            (7200.00,77.76, 270.29,6710.74,   3.85, -2508.24, 1.80),
            (7920.00,90.00, 270.33,6787.36,   7.98, -3222.77, 1.70),
            (8640.00,89.82, 270.38,6788.51,  12.76, -3942.75, 0.03),
            (9360.00,89.87, 270.98,6790.46,  25.10, -4662.64, 0.01),
            (10080.00,90.01,270.49,6791.22,  31.23, -5382.61, 0.02),
            (10800.00,90.19,270.42,6789.95,  36.50, -6102.59, 0.03),
            (11520.00,90.10,270.52,6788.11,  43.02, -6822.56, 0.01),
            (12240.00,90.05,269.93,6787.16,  42.09, -7542.56, 0.01),
            (12960.00,90.22,270.80,6785.48,  52.20, -8262.49, 0.02),
            (13680.00,90.35,270.67,6781.91,  60.62, -8982.43, 0.02),
            (14400.00,90.44,271.37,6776.95,  77.86, -9702.20, 0.01),
            (15120.00,90.27,271.19,6772.50,  92.85,-10422.03, 0.02),
            (15840.00,90.33,272.15,6768.75, 119.80,-11141.52, 0.01),
            (16560.00,90.41,272.42,6764.13, 150.19,-11860.86, 0.01),
            (17280.00,90.29,272.00,6759.74, 175.37,-12580.41, 0.02),
            (18000.00,90.15,271.61,6756.94, 195.64,-13300.12, 0.02),
        ],
    },
]

# UWI format stored in DB uses "US" prefix — normalise
def _norm_uwi(raw: str) -> str:
    """Strip dashes, add US prefix to match DB format."""
    digits = raw.replace("-", "").replace(" ", "")
    return f"US{digits}" if not digits.startswith("US") else digits


# =============================================================================
# LOADER
# =============================================================================

def load_surveys(engine, surveys: list[dict], wipe_existing: bool = False) -> dict:
    loaded_hdr = 0
    loaded_sta = 0
    skipped    = []
    errors     = []

    with engine.begin() as con:
        for s in surveys:
            uwi_raw  = s["uwi"]
            uwi      = _norm_uwi(uwi_raw)

            # Check well exists
            exists = con.execute(text(
                "SELECT COUNT(*) FROM dataview.dv_well WHERE uwi = :u"
            ), {"u": uwi}).scalar()

            if not exists:
                skipped.append(f"{s['well_name']} — UWI {uwi} not in dv_well")
                continue

            srvy_id = hashlib.sha1(f"{uwi}_SURVEY_PDF".encode()).hexdigest()[:40]

            # Optionally wipe existing
            if wipe_existing:
                con.execute(text(
                    "DELETE FROM dataview.dv_well_dir_srvy_sta WHERE uwi=:u AND srvy_id=:sid"
                ), {"u": uwi, "sid": srvy_id})
                con.execute(text(
                    "DELETE FROM dataview.dv_well_dir_srvy_hdr WHERE uwi=:u AND srvy_id=:sid"
                ), {"u": uwi, "sid": srvy_id})

            # Insert header
            try:
                stations = s["stations"]
                con.execute(text("""
                    IF NOT EXISTS (SELECT 1 FROM dataview.dv_well_dir_srvy_hdr
                                   WHERE uwi=:u AND srvy_id=:sid)
                    INSERT INTO dataview.dv_well_dir_srvy_hdr (
                        uwi, srvy_id, survey_type,
                        survey_top_depth, survey_base_depth,
                        depth_ouom, depth_datum, active_ind,
                        row_created_by, row_created_date, source
                    ) VALUES (
                        :u, :sid, :stype,
                        :top, :base, 'FT', 'KB', 'Y',
                        'LOADER', GETDATE(), 'PDF_SURVEY'
                    )
                """), {
                    "u":    uwi,
                    "sid":  srvy_id,
                    "stype": s["survey_type"],
                    "top":  stations[0][0],
                    "base": stations[-1][0],
                })
                loaded_hdr += 1
            except Exception as e:
                errors.append(f"{s['well_name']} hdr: {e}")
                continue

            # Insert stations
            for seq, (md, inc, azi, tvd, ns, ew, dls) in enumerate(stations, start=1):
                sta_id = hashlib.sha1(f"{srvy_id}_{seq}".encode()).hexdigest()[:40]
                try:
                    con.execute(text("""
                        IF NOT EXISTS (SELECT 1 FROM dataview.dv_well_dir_srvy_sta
                                       WHERE uwi=:u AND srvy_id=:sid AND sta_id=:staid)
                        INSERT INTO dataview.dv_well_dir_srvy_sta (
                            uwi, srvy_id, sta_id, seq_num,
                            md, incl, azim, tvd,
                            ns_offset, ew_offset, dls,
                            depth_ouom,
                            row_created_by, row_created_date, source
                        ) VALUES (
                            :u, :sid, :staid, :seq,
                            :md, :inc, :azi, :tvd,
                            :ns, :ew, :dls,
                            'FT',
                            'LOADER', GETDATE(), 'PDF_SURVEY'
                        )
                    """), {
                        "u": uwi, "sid": srvy_id, "staid": sta_id, "seq": seq,
                        "md": md, "inc": inc, "azi": azi, "tvd": tvd,
                        "ns": ns, "ew": ew, "dls": dls,
                    })
                    loaded_sta += 1
                except Exception as e:
                    errors.append(f"{s['well_name']} sta {seq}: {e}")

            print(f"  ✓ {s['well_name']:25} UWI:{uwi}  {len(stations)} stations")

    return {
        "headers":  loaded_hdr,
        "stations": loaded_sta,
        "skipped":  skipped,
        "errors":   errors,
    }


# =============================================================================
# CLI
# =============================================================================

def _build_engine(args):
    if args.windows_auth:
        cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};"
              f"SERVER={args.server};DATABASE={args.database};"
              f"Trusted_Connection=yes;")
    else:
        cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};"
              f"SERVER={args.server};DATABASE={args.database};"
              f"UID={args.username};PWD={args.password};")
    return create_engine(
        "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(cs),
        fast_executemany=True,
    )


def main():
    ap = argparse.ArgumentParser(description="Load survey PDFs into DataView")
    ap.add_argument("--server",       default=r"127.0.0.1\SQLEXPRESS")
    ap.add_argument("--database",     default="DataView_Demo")
    ap.add_argument("--windows-auth", action="store_true")
    ap.add_argument("--username",     default="")
    ap.add_argument("--password",     default="")
    ap.add_argument("--wipe",         action="store_true",
                    help="Delete existing survey data before loading")
    args = ap.parse_args()

    print(f"\nDataView Survey Loader")
    print("=" * 50)

    engine = _build_engine(args)
    with engine.connect() as con:
        ver = con.execute(text("SELECT @@VERSION")).scalar()
        print(f"Connected: {str(ver)[:60]}")

    print(f"\nLoading {len(SURVEYS)} surveys...\n")
    result = load_surveys(engine, SURVEYS, wipe_existing=args.wipe)

    print(f"\n{'='*50}")
    print(f"  Survey headers loaded : {result['headers']}")
    print(f"  Survey stations loaded: {result['stations']}")
    if result["skipped"]:
        print(f"\n  Skipped ({len(result['skipped'])}):")
        for s in result["skipped"]:
            print(f"    - {s}")
    if result["errors"]:
        print(f"\n  Errors ({len(result['errors'])}):")
        for e in result["errors"][:5]:
            print(f"    - {e}")
    print(f"\nDone.")


if __name__ == "__main__":
    main()
