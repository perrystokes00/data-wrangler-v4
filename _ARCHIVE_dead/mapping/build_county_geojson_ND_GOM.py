"""
build_county_geojson.py — Build GeoJSON files by Texas petroleum region.

Usage:
    python build_county_geojson.py --region permian
    python build_county_geojson.py --region eagle-ford
    python build_county_geojson.py --region east-texas
    python build_county_geojson.py --region all-regions
    python build_county_geojson.py Andrews Ector Midland
    python build_county_geojson.py --list-regions
"""
import sys
from pathlib import Path
import json
from build_well_geojson import DEFAULT_CONN
from sqlalchemy import create_engine

REGIONS = {
    "permian": {
        "label": "Permian Basin",
        "state": "TX",
        "counties": [
            "Andrews", "Borden", "Crane", "Crockett", "Culberson",
            "Dawson", "Ector", "Gaines", "Glasscock", "Howard",
            "Irion", "Jeff Davis", "Loving", "Martin", "Midland",
            "Mitchell", "Nolan", "Pecos", "Reagan", "Reeves",
            "Schleicher", "Scurry", "Sterling", "Sutton", "Terrell",
            "Terry", "Upton", "Val Verde", "Ward", "Winkler",
            "Yoakum",
        ],
    },
    "eagle-ford": {
        "label": "Eagle Ford Shale",
        "state": "TX",
        "counties": [
            "Atascosa", "Bee", "DeWitt", "Dimmit", "Frio",
            "Gonzales", "Karnes", "La Salle", "Lavaca", "Live Oak",
            "Maverick", "McMullen", "Medina", "Webb", "Wilson",
            "Zapata", "Zavala",
        ],
    },
    "east-texas": {
        "label": "East Texas",
        "state": "TX",
        "counties": [
            "Anderson", "Camp", "Cass", "Cherokee", "Franklin",
            "Freestone", "Gregg", "Harrison", "Henderson", "Houston",
            "Leon", "Limestone", "Marion", "Morris", "Nacogdoches",
            "Navarro", "Panola", "Rusk", "Shelby", "Smith",
            "Titus", "Upshur", "Van Zandt", "Wood",
        ],
    },
    "gulf-coast": {
        "label": "Gulf Coast",
        "state": "TX",
        "counties": [
            "Aransas", "Austin", "Brazoria", "Brooks", "Calhoun",
            "Cameron", "Chambers", "Colorado", "Duval", "Fort Bend",
            "Galveston", "Harris", "Hidalgo", "Jackson", "Jefferson",
            "Jim Hogg", "Jim Wells", "Kenedy", "Kleberg", "Liberty",
            "Matagorda", "Nueces", "Orange", "Refugio", "San Patricio",
            "Starr", "Victoria", "Waller", "Wharton", "Willacy",
        ],
    },
    "north-texas": {
        "label": "North Texas / Barnett",
        "state": "TX",
        "counties": [
            "Clay", "Cooke", "Denton", "Eastland", "Erath",
            "Hood", "Jack", "Johnson", "Montague", "Palo Pinto",
            "Parker", "Shackelford", "Somervell", "Stephens",
            "Tarrant", "Throckmorton", "Wichita", "Wise", "Young",
        ],
    },
    "panhandle": {
        "label": "Texas Panhandle",
        "state": "TX",
        "counties": [
            "Carson", "Collingsworth", "Dallam", "Gray", "Hansford",
            "Hartley", "Hemphill", "Hutchinson", "Lipscomb", "Moore",
            "Ochiltree", "Oldham", "Potter", "Roberts", "Sherman",
            "Wheeler",
        ],
    },
    "south-texas": {
        "label": "South Texas",
        "state": "TX",
        "counties": [
            "Caldwell", "Guadalupe", "Hays", "Kinney", "Uvalde",
            "Bexar", "Comal", "Fayette", "Goliad",
            "Lee", "Milam", "Robertson", "Washington",
        ],
    },
    "central-texas": {
        "label": "Central Texas",
        "state": "TX",
        "counties": [
            "Bell", "Bosque", "Brown", "Burnet", "Coleman",
            "Comanche", "Concho", "Coryell", "Erath", "Falls",
            "Fisher", "Hamilton", "Haskell", "Hill", "Jones",
            "Lampasas", "McCulloch", "McLennan", "Mason", "Menard",
            "Mills", "Runnels", "San Saba", "Stonewall", "Taylor",
            "Tom Green",
        ],
    },

    # ── Kansas Petroleum Regions ──────────────────────────────────────
    "ks-hugoton": {
        "label": "Kansas — Hugoton Embayment",
        "state": "KS",
        "counties": [
            "Finney", "Grant", "Gray", "Hamilton", "Haskell",
            "Hodgeman", "Kearny", "Meade", "Morton", "Seward",
            "Stanton", "Stevens", "Ford", "Clark", "Comanche",
            "Kiowa", "Edwards", "Pawnee", "Ness", "Lane",
            "Scott", "Wichita",
        ],
    },
    "ks-central-uplift": {
        "label": "Kansas — Central Kansas Uplift",
        "state": "KS",
        "counties": [
            "Barton", "Ellsworth", "Lincoln", "McPherson", "Marion",
            "Rice", "Rush", "Russell", "Saline", "Stafford",
            "Ellis", "Osborne", "Rooks", "Smith", "Phillips",
            "Norton", "Graham", "Trego",
        ],
    },
    "ks-sedgwick": {
        "label": "Kansas — Sedgwick Basin",
        "state": "KS",
        "counties": [
            "Butler", "Cowley", "Harvey", "Kingman", "Pratt",
            "Reno", "Sedgwick", "Sumner", "Harper", "Barber",
            "Greenwood", "Elk",
        ],
    },
    "ks-cherokee": {
        "label": "Kansas — Cherokee Platform",
        "state": "KS",
        "counties": [
            "Allen", "Anderson", "Bourbon", "Cherokee", "Chautauqua",
            "Coffey", "Crawford", "Franklin", "Labette", "Linn",
            "Miami", "Montgomery", "Neosho", "Wilson", "Woodson",
            "Wyandotte", "Douglas", "Johnson", "Osage",
        ],
    },
    "ks-salina": {
        "label": "Kansas — Salina Basin",
        "state": "KS",
        "counties": [
            "Cloud", "Dickinson", "Geary", "Jewell", "Mitchell",
            "Morris", "Ottawa", "Republic", "Riley", "Clay",
            "Washington", "Marshall", "Nemaha", "Brown", "Jackson",
            "Pottawatomie", "Wabaunsee",
        ],
    },
    "north-dakota": {
        "label": "North Dakota — Williston Basin",
        "state": "ND",
        "counties": [],
        "state_only": True,
    },
    "gom-all": {
        "label": "GOM — All Areas",
        "counties": [],
        "source_filter": "GOM",
    },
    "gom-west": {
        "label": "GOM — Western Planning Area",
        "counties": [],
        "area_codes": ["HI", "GA", "GB", "GI", "EI", "SS", "WC", "SP", "BA", "MU"],
    },
    "gom-central": {
        "label": "GOM — Central Planning Area",
        "counties": [],
        "area_codes": ["ST", "VR", "MP", "SM", "EC", "MC", "GC", "WD", "MI", "VK"],
    },
}



def query_wells_for_region(engine, info):
    """Query only the wells matching a region's counties, source, or area codes."""
    from sqlalchemy import text
    import json

    counties = info.get("counties", [])
    src_filter = info.get("source_filter", "")
    area_codes = info.get("area_codes", [])
    state = info.get("state", "")

    all_wells = []

    # ── dataview.dv_well ──────────────────────────────────────────
    state_only = info.get("state_only", False)

    if state_only and state:
        sql = f"""
            SELECT w.uwi, w.well_name, w.api_num,
                   w.surface_latitude, w.surface_longitude,
                   w.county, w.province_state, w.well_status,
                   w.well_type, w.source, w.area,
                   ISNULL(w.operator_name, '') AS operator_name,
                   ISNULL(w.field_name, '') AS field_name,
                   '' AS basin_name,
                   CONVERT(VARCHAR(10), w.spud_date, 120) AS spud_date,
                   w.final_td,
                   'dataview' AS _schema
            FROM dataview.dv_well w
            WHERE w.surface_latitude IS NOT NULL
              AND w.surface_longitude IS NOT NULL
              AND w.province_state = '{state}'
            FOR JSON PATH
        """
        try:
            with engine.connect() as con:
                result = con.execute(text(sql))
                chunks = [row[0] for row in result if row[0]]
                if chunks:
                    all_wells.extend(json.loads("".join(chunks)))
        except Exception as e:
            print(f"    query error: {e}")

    elif counties:
        placeholders = ",".join(f"'{c}'" for c in counties)
        state_clause = f"AND w.province_state = '{state}'" if state else ""
        sql = f"""
            SELECT w.uwi, w.well_name, w.api_num,
                   w.surface_latitude, w.surface_longitude,
                   w.county, w.province_state, w.well_status,
                   w.well_type, w.source, w.area,
                   ISNULL(w.operator_name, '') AS operator_name,
                   ISNULL(w.field_name, '') AS field_name,
                   ISNULL(f.basin_name, '') AS basin_name,
                   CONVERT(VARCHAR(10), w.spud_date, 120) AS spud_date,
                   w.final_td,
                   'dataview' AS _schema
            FROM dataview.dv_well w
            LEFT JOIN dataview.dv_field f ON f.field_id = w.field_id
            WHERE w.surface_latitude IS NOT NULL
              AND w.surface_longitude IS NOT NULL
              AND w.county IN ({placeholders})
              {state_clause}
            FOR JSON PATH
        """
        try:
            with engine.connect() as con:
                result = con.execute(text(sql))
                chunks = [row[0] for row in result if row[0]]
                if chunks:
                    all_wells.extend(json.loads("".join(chunks)))
        except Exception as e:
            print(f"    dataview query error: {e}")

    # ── dataview_gom.well (for GOM regions) ───────────────────────
    if src_filter == "GOM" or area_codes:
        area_clause = ""
        if area_codes:
            ac_list = ",".join(f"'{a}'" for a in area_codes)
            area_clause = f"AND w.bottom_area_code IN ({ac_list})"

        sql_gom = f"""
            SELECT CONVERT(VARCHAR(36), w.well_id) AS uwi,
                   w.well_name,
                   w.api_well_number AS api_num,
                   w.surface_latitude,
                   w.surface_longitude,
                   CAST('' AS NVARCHAR(100)) AS county,
                   w.region AS province_state,
                   ISNULL(w.status_code, '') AS well_status,
                   ISNULL(w.type_code, '') AS well_type,
                   'GOM' AS source,
                   'Gulf of America' AS area,
                   ISNULL(w.company_name, '') AS operator_name,
                   ISNULL(w.bottom_area_code, '') AS field_name,
                   '' AS basin_name,
                   CONVERT(VARCHAR(10), w.spud_date, 120) AS spud_date,
                   w.bh_total_md_ft AS final_td,
                   'gom' AS _schema
            FROM dataview_gom.well w
            WHERE w.surface_latitude IS NOT NULL
              AND w.surface_longitude IS NOT NULL
              {area_clause}
            FOR JSON PATH
        """
        try:
            with engine.connect() as con:
                result = con.execute(text(sql_gom))
                chunks = [row[0] for row in result if row[0]]
                if chunks:
                    all_wells.extend(json.loads("".join(chunks)))
        except Exception as e:
            print(f"    GOM query error: {e}")

    # Build GeoJSON features
    features = []
    for w in all_wells:
        lat = w.get("surface_latitude")
        lon = w.get("surface_longitude")
        if lat is None or lon is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "uwi":      w.get("uwi", ""),
                "name":     w.get("well_name", ""),
                "api":      w.get("api_num", ""),
                "operator": w.get("operator_name", ""),
                "field":    w.get("field_name", ""),
                "basin":    w.get("basin_name", ""),
                "county":   w.get("county", ""),
                "state":    w.get("province_state", ""),
                "status":   w.get("well_status", ""),
                "type":     w.get("well_type", ""),
                "source":   w.get("source", ""),
                "area":     w.get("area", ""),
                "spud":     w.get("spud_date", ""),
                "td":       w.get("final_td"),
                "schema":   w.get("_schema", ""),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }

def list_regions():
    print("\nTexas Petroleum Regions:")
    print(f"  {'Region':25s} {'Counties':>8s}  Label")
    print(f"  {'-'*25} {'-'*8}  {'-'*30}")
    total = 0
    for key, info in sorted(REGIONS.items()):
        n = len(info["counties"])
        total += n
        print(f"  {key:25s} {n:>8d}  {info['label']}")
    print(f"\n  Total counties: {total}")
    print(f"\nUsage:")
    print(f"  python build_county_geojson.py --region permian")
    print(f"  python build_county_geojson.py --region all-regions")
    print(f"  python build_county_geojson.py --list-regions")
    print(f"  python build_county_geojson.py Andrews Ector Midland")


def main():
    _skip_existing = "--skip-existing" in sys.argv
    _refresh = "--refresh" in sys.argv

    if "--list-regions" in sys.argv or "--help" in sys.argv or len(sys.argv) == 1:
        list_regions()
        return

    if "--region" in sys.argv:
        idx = sys.argv.index("--region")
        region_key = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""

        if region_key == "all-regions":
            e = create_engine(DEFAULT_CONN)
            out_dir = Path(__file__).parent / "geojson" if "__file__" in dir() else Path("geojson")
            out_dir.mkdir(exist_ok=True)

            for key, info in sorted(REGIONS.items()):
                out = str(out_dir / f"wells_{key.replace('-','_')}.geojson")

                # Skip if file exists and --skip-existing
                if _skip_existing and not _refresh and Path(out).exists():
                    sz = Path(out).stat().st_size / (1024*1024)
                    print(f"  {info['label']:30s} — exists ({sz:.1f} MB), skipping")
                    continue

                print(f"  {info['label']:30s} querying… ", end="", flush=True)
                gj = query_wells_for_region(e, info)
                n = len(gj.get("features", []))

                if not n:
                    print("no wells")
                    continue

                gj["metadata"] = {
                    "region": info["label"],
                    "total_wells": n,
                }
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(gj, f)
                size_mb = Path(out).stat().st_size / (1024 * 1024)
                print(f"{n:>8,} wells  ({size_mb:.1f} MB)")

            print("\nDone!")
            return


        if region_key not in REGIONS:
            print(f"Unknown region: {region_key}")
            list_regions()
            return

        info = REGIONS[region_key]
        counties = [c.upper() for c in info["counties"]]
        label = info["label"]
        out_dir = Path(__file__).parent / "geojson" if "__file__" in dir() else Path("geojson")
        out_dir.mkdir(exist_ok=True)
        out_name = str(out_dir / f"wells_{region_key.replace('-','_')}.geojson")
    else:
        counties = [c.upper() for c in sys.argv[1:] if not c.startswith("-")]
        label = counties[0] if len(counties) == 1 else "Custom"
        out_dir = Path(__file__).parent / "geojson" if "__file__" in dir() else Path("geojson")
        out_dir.mkdir(exist_ok=True)
        out_name = str(out_dir / "wells.geojson")

    print(f"Building GeoJSON for {label}...")
    e = create_engine(DEFAULT_CONN)

    if "--region" in sys.argv:
        gj = query_wells_for_region(e, info)
    else:
        # Individual counties — build a temp info dict
        gj = query_wells_for_region(e, {"counties": [c.title() for c in counties]})

    n = len(gj.get("features", []))
    gj["metadata"] = {"region": label, "total_wells": n}

    with open(out_name, "w", encoding="utf-8") as f:
        json.dump(gj, f)

    size_mb = Path(out_name).stat().st_size / (1024 * 1024)
    print(f"Wrote {n:,} wells → {out_name} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
