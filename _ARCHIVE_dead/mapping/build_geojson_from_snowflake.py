"""
build_geojson_from_snowflake.py
===============================
Queries Snowflake WELL_MASTER and builds regional GeoJSON files
for the pydeck map. Same output as build_county_geojson.py but
reads from the federated Snowflake database instead of local SQL Server.

Usage:
    python build_geojson_from_snowflake.py --state TX
    python build_geojson_from_snowflake.py --state KS
    python build_geojson_from_snowflake.py --state ND
    python build_geojson_from_snowflake.py --state GOM
    python build_geojson_from_snowflake.py --state all
    python build_geojson_from_snowflake.py --state TX --county Andrews
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path

try:
    import snowflake.connector
except ImportError:
    sys.exit("pip install snowflake-connector-python")

OUT_DIR = Path("geojson")


def get_conn():
    return snowflake.connector.connect(
        account=os.environ.get("SNOWFLAKE_ACCOUNT", "YDWXNCV-VL88062"),
        user=os.environ.get("SNOWFLAKE_USER", "PMSTOKES00"),
        password=os.environ.get("SNOWFLAKE_PASSWORD", ""),
        database="WELL_FEDERATION",
        warehouse="WV_WH",
        role="ACCOUNTADMIN",
    )


def query_wells(conn, where_clause):
    """Query WELL_MASTER and return GeoJSON features."""
    cur = conn.cursor()
    sql = f"""
        SELECT uwi, well_name, operator_name, field_name,
               surface_latitude, surface_longitude,
               county, province_state, well_status, well_type,
               spud_date, final_td, area, source_list
        FROM WELL_FEDERATION.CURATED.WELL_MASTER
        WHERE surface_latitude IS NOT NULL
          AND surface_longitude IS NOT NULL
          {where_clause}
    """
    t0 = time.time()
    cur.execute(sql)
    columns = [desc[0].lower() for desc in cur.description]

    features = []
    batch = 0
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for row in rows:
            d = dict(zip(columns, row))
            lat = d.get("surface_latitude")
            lon = d.get("surface_longitude")
            if lat is None or lon is None:
                continue
            try:
                lat = float(lat)
                lon = float(lon)
            except (ValueError, TypeError):
                continue
            if lat == 0 and lon == 0:
                continue

            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "uwi": d.get("uwi", ""),
                    "name": d.get("well_name", "") or "",
                    "operator": d.get("operator_name", "") or "",
                    "field": d.get("field_name", "") or "",
                    "county": d.get("county", "") or "",
                    "state": d.get("province_state", "") or "",
                    "status": d.get("well_status", "") or "",
                    "type": d.get("well_type", "") or "",
                    "spud": d.get("spud_date", "") or "",
                    "td": d.get("final_td", "") or "",
                    "area": d.get("area", "") or "",
                    "source": d.get("source_list", "") or "",
                    "schema": "snowflake",
                },
            })
        batch += len(rows)
        print(f"    {batch:,} rows…", flush=True)

    cur.close()
    elapsed = time.time() - t0
    print(f"  Queried {len(features):,} wells ({elapsed:.1f}s)")
    return features


def write_geojson(features, label, out_path):
    """Write features to a GeoJSON file."""
    gj = {
        "type": "FeatureCollection",
        "metadata": {
            "source": "Snowflake WELL_FEDERATION.CURATED.WELL_MASTER",
            "region": label,
            "total_wells": len(features),
        },
        "features": features,
    }
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(gj, f)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  → {out_path} ({size_mb:.1f} MB)")


STATE_MAP = {
    "TX": {"label": "Texas", "where": "AND province_state = 'TX'"},
    "KS": {"label": "Kansas", "where": "AND province_state = 'KS'"},
    "ND": {"label": "North Dakota", "where": "AND province_state = 'ND'"},
    "GOM": {"label": "Gulf of America", "where": "AND province_state = 'Gulf of America'"},
    "CO": {"label": "Colorado", "where": "AND province_state = 'CO'"},
    "OSDU": {"label": "OSDU", "where": "AND source_list LIKE '%OSDU%'"},
}


def main():
    ap = argparse.ArgumentParser(
        description="Build GeoJSON from Snowflake WELL_MASTER")
    ap.add_argument("--state", default="all",
                    help="State code: TX, KS, ND, GOM, all")
    ap.add_argument("--county", default=None,
                    help="Filter by county name (optional)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max wells per state (0 = no limit)")
    args = ap.parse_args()

    print("WranglerView — Build GeoJSON from Snowflake")
    print(f"  State:  {args.state}")
    print(f"  County: {args.county or 'all'}")
    print()

    OUT_DIR.mkdir(exist_ok=True)

    print("  Connecting to Snowflake…", end=" ", flush=True)
    try:
        conn = get_conn()
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")
        return

    t0 = time.time()
    total = 0

    states = [args.state.upper()] if args.state.upper() != "ALL" else list(STATE_MAP.keys())

    for state_key in states:
        if state_key not in STATE_MAP:
            # Treat as a raw province_state value
            info = {"label": state_key, "where": f"AND province_state = '{state_key}'"}
        else:
            info = STATE_MAP[state_key]

        print(f"\n── {info['label']} ──────────────────────────────────")

        where = info["where"]
        if args.county:
            where += f" AND county = '{args.county}'"
        if args.limit:
            where += f" LIMIT {args.limit}"

        features = query_wells(conn, where)

        if not features:
            print("  No wells — skipping")
            continue

        # For Texas, split into regions if > 500K wells
        if state_key == "TX" and len(features) > 500000 and not args.county:
            # Split by first digit of county to create manageable chunks
            from collections import defaultdict
            by_county = defaultdict(list)
            for f in features:
                c = f["properties"].get("county", "Unknown") or "Unknown"
                by_county[c].append(f)

            # Group counties into regions by first letter
            regions = {
                "tx_west": [], "tx_east": [], "tx_south": [],
                "tx_north": [], "tx_central": [],
            }
            for county, wells in sorted(by_county.items()):
                # Simple geographic split by well longitude
                if wells:
                    avg_lon = sum(
                        w["geometry"]["coordinates"][0] for w in wells
                    ) / len(wells)
                    avg_lat = sum(
                        w["geometry"]["coordinates"][1] for w in wells
                    ) / len(wells)

                    if avg_lon < -101:
                        regions["tx_west"].extend(wells)
                    elif avg_lon > -96:
                        regions["tx_east"].extend(wells)
                    elif avg_lat < 29.5:
                        regions["tx_south"].extend(wells)
                    elif avg_lat > 33:
                        regions["tx_north"].extend(wells)
                    else:
                        regions["tx_central"].extend(wells)

            for rkey, rfeats in regions.items():
                if rfeats:
                    out = OUT_DIR / f"sf_{rkey}.geojson"
                    label = rkey.replace("_", " ").title()
                    write_geojson(rfeats, label, out)
                    total += len(rfeats)
        else:
            fname = f"sf_{state_key.lower()}"
            if args.county:
                fname += f"_{args.county.lower().replace(' ','_')}"
            out = OUT_DIR / f"{fname}.geojson"
            write_geojson(features, info["label"], out)
            total += len(features)

    conn.close()

    print(f"\n{'─' * 50}")
    print(f"  Total: {total:,} wells")
    print(f"  Time: {time.time() - t0:.1f}s")
    print(f"  Files: {OUT_DIR}")


if __name__ == "__main__":
    main()
