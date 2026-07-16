"""
load_core_photos.py
===================
Scans a folder of core photos and registers them in dataview.dv_well_core_photo.

Reads existing dv_well_core records to get core_id / uwi mapping.
Inserts one row per image file found.

Usage:
    python load_core_photos.py
    python load_core_photos.py --folder "C:\\Bulk\\training\\core_data"
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from pathlib import Path

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

try:
    import urllib.parse
    import pandas as pd
    from sqlalchemy import create_engine, text
except ImportError:
    sys.exit("pip install sqlalchemy pyodbc pandas")

# ── Connection ────────────────────────────────────────────────────────
SERVER   = r"127.0.0.1\SQLEXPRESS"
DATABASE = "DataView"

cs  = (f"DRIVER={{ODBC Driver 17 for SQL Server}};"
       f"SERVER={SERVER};DATABASE={DATABASE};Trusted_Connection=yes;")
eng = create_engine(
    "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(cs),
    fast_executemany=True,
)

# ── Photo type detection ──────────────────────────────────────────────
def _photo_type(stem: str) -> str:
    s = stem.lower()
    if "thin"   in s: return "THIN_SECTION"
    if "uv"     in s: return "UV"
    if "tray"   in s: return "OVERVIEW"
    if "ct"     in s: return "CT_SCAN"
    if "plug"   in s: return "PLUG"
    return "OTHER"

def _lighting(photo_type: str, stem: str) -> str:
    if photo_type == "UV":          return "UV"
    if photo_type == "THIN_SECTION": return "PPL"   # plane polarised
    return "WHITE"

def _file_hash(path: Path) -> str:
    h = hashlib.sha1(path.read_bytes()).hexdigest()
    return h

def _image_dims(path: Path):
    if PILImage:
        try:
            with PILImage.open(path) as img:
                return img.width, img.height
        except Exception:
            pass
    return None, None

def main(folder: str) -> None:
    root = Path(folder)
    if not root.exists():
        sys.exit(f"Folder not found: {root}")

    # Load core records for uwi / core_id lookup
    with eng.connect() as con:
        cores_df = pd.read_sql(text("""
            SELECT uwi, core_id, core_num
            FROM dataview.dv_well_core
            WHERE active_ind='Y'
        """), con)

        # Load existing photo paths to avoid duplicates
        existing = set(pd.read_sql(text("""
            SELECT file_path FROM dataview.dv_well_core_photo
        """), con)["file_path"].tolist())

    # Build uwi → core lookup
    core_map = {}
    for _, r in cores_df.iterrows():
        core_map.setdefault(r["uwi"], []).append(
            {"core_id": r["core_id"], "core_num": int(r["core_num"] or 1)})

    rows    = []
    skipped = 0

    # Walk folder — expect subfolders named by UWI
    for uwi_folder in sorted(root.iterdir()):
        if not uwi_folder.is_dir():
            continue
        uwi = uwi_folder.name

        if uwi not in core_map:
            print(f"  Skip {uwi} — not in dv_well_core")
            continue

        cores = core_map[uwi]

        for img_path in sorted(uwi_folder.glob("*")):
            if img_path.suffix.lower() not in (".jpg",".jpeg",".png",".tif",".tiff"):
                continue

            path_str = str(img_path)
            if path_str in existing:
                skipped += 1
                continue

            stem       = img_path.stem
            photo_type = _photo_type(stem)
            lighting   = _lighting(photo_type, stem)
            w, h       = _image_dims(img_path)
            size_kb    = round(img_path.stat().st_size / 1024, 1)
            fhash      = _file_hash(img_path)

            # Match to core_num from filename (tray_001 → core_num=1)
            core_num = 1
            parts    = stem.split("_")
            for p in parts:
                if p.isdigit():
                    core_num = int(p)
                    break

            core_id = next(
                (c["core_id"] for c in cores if c["core_num"] == core_num),
                cores[0]["core_id"]  # fallback to first core
            )

            rows.append({
                "uwi":           uwi,
                "core_id":       core_id,
                "photo_id":      hashlib.sha1(path_str.encode()).hexdigest(),
                "photo_type":    photo_type,
                "lighting":      lighting,
                "tray_num":      core_num,
                "file_path":     path_str,
                "file_name":     img_path.name,
                "file_ext":      img_path.suffix.lower(),
                "file_size_kb":  size_kb,
                "file_hash":     fhash,
                "width_px":      w,
                "height_px":     h,
                "active_ind":    "Y",
                "source":        "DATAVIEW_LOADER",
                "row_created_by":"DATAVIEW_LOADER",
                "row_changed_by":"DATAVIEW_LOADER",
            })

    if not rows:
        print(f"No new images found ({skipped} already registered)")
        return

    print(f"Registering {len(rows)} images ({skipped} already in DB)...")

    df = pd.DataFrame(rows)
    with eng.begin() as con:
        df.to_sql("dv_well_core_photo", con, schema="dataview",
                  if_exists="append", index=False)

        # Update photo_count in dv_well_core
        con.execute(text("""
            UPDATE c
            SET c.photo_count    = p.cnt,
                c.row_changed_date = GETDATE()
            FROM dataview.dv_well_core c
            JOIN (
                SELECT core_id, COUNT(*) cnt
                FROM dataview.dv_well_core_photo
                WHERE active_ind='Y'
                GROUP BY core_id
            ) p ON p.core_id = c.core_id
        """))

    print(f"Done — {len(rows)} photos registered")

    # Summary by type
    type_counts = df.groupby("photo_type").size()
    for t, n in type_counts.items():
        print(f"  {t}: {n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--folder",
        default=r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler\training\core_data",
        help="Root folder containing UWI subfolders with core images",
    )
    args = ap.parse_args()
    main(args.folder)
