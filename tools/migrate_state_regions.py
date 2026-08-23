"""
migrate_state_regions.py
=========================
One-time migration: reads the current state_regions.py (any format —
2-tuple or 3-tuple), recomputes (center_lat, center_lon, zoom) for
each region from TIGER county centroids using the NEW tighter zoom
thresholds, and writes a new state_regions.py in the 3-tuple format.

WHY THIS SCRIPT EXISTS:
The first version of Region Builder used zoom thresholds that were
one level too generous — Permian, Gulf Coast, and large state regions
were stored with zoom levels that produced whole-state viewports
instead of region-tight viewports. This script retrofits any existing
state_regions.py with the new threshold scale so you don't have to
re-lasso every region.

INPUT:
    state_regions.py      — current file, any tuple format
    tl_2024_us_county.shp — TIGER county shapefile

OUTPUT:
    state_regions.py      — overwritten with new 3-tuple values
    state_regions.py.bak  — backup of the original

RUNTIME: ~5-10 seconds (most of it is TIGER load)

USAGE:
    cd <V3_ROOT>
    python tools/migrate_state_regions.py

REVERSIBLE:
    Restore from state_regions.py.bak if anything looks wrong.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import geopandas as gpd
import os

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── CONFIG ──────────────────────────────────────────────────────────

V3_ROOT = Path(__file__).parent
COUNTIES_SHP = V3_ROOT / "spatial" / "tl_2024_us_county.shp"
SR_FILE      = V3_ROOT / "state_regions.py"
SR_BACKUP    = V3_ROOT / "state_regions.py.bak"

FIPS_TO_USPS = {
    "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT",
    "10":"DE","11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL",
    "18":"IN","19":"IA","20":"KS","21":"KY","22":"LA","23":"ME","24":"MD",
    "25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE",
    "32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND",
    "39":"OH","40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD",
    "47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV",
    "55":"WI","56":"WY",
    "60":"AS","66":"GU","69":"MP","72":"PR","78":"VI",
}


# ── STEP 1: Load TIGER counties ─────────────────────────────────────

def load_counties():
    print("=" * 70)
    print("STEP 1: Load TIGER counties")
    print("=" * 70)
    print(f"Reading: {COUNTIES_SHP}")
    if not COUNTIES_SHP.exists():
        print(f"\u274c Shapefile not found: {COUNTIES_SHP}")
        sys.exit(1)
    gdf = gpd.read_file(COUNTIES_SHP)
    print(f"  \u2713 {len(gdf):,} counties loaded")

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4269")
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    cols_upper = {c.upper(): c for c in gdf.columns}
    if "STUSPS" in cols_upper:
        gdf["state"] = gdf[cols_upper["STUSPS"]]
    elif "STATEFP" in cols_upper:
        gdf["state"] = gdf[cols_upper["STATEFP"]].map(FIPS_TO_USPS)

    if "NAME" in cols_upper:
        gdf["county_name"] = gdf[cols_upper["NAME"]]
    elif "NAMELSAD" in cols_upper:
        gdf["county_name"] = (
            gdf[cols_upper["NAMELSAD"]]
            .str.replace(" County", "", regex=False).str.strip()
        )

    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Geometry is in a geographic CRS"
        )
        gdf["centroid_lat"] = gdf.geometry.centroid.y
        gdf["centroid_lon"] = gdf.geometry.centroid.x

    return gdf[["state", "county_name",
                "centroid_lat", "centroid_lon"]].copy()


# ── STEP 2: Load STATE_REGIONS ──────────────────────────────────────

def load_state_regions():
    print()
    print("=" * 70)
    print("STEP 2: Load existing STATE_REGIONS")
    print("=" * 70)
    if not SR_FILE.exists():
        print(f"\u274c Not found: {SR_FILE}")
        print()
        print("There's no state_regions.py to migrate. If you haven't")
        print("created any state regions yet, use page_region_builder")
        print("to define some first.")
        sys.exit(1)

    sys.path.insert(0, str(V3_ROOT))
    try:
        from dataview.region_builder.state_regions import STATE_REGIONS
    except ImportError as e:
        print(f"\u274c Could not import state_regions: {e}")
        sys.exit(1)
    except SyntaxError as e:
        print(f"\u274c state_regions.py has a syntax error: {e}")
        print("   Check the file or restore from a backup.")
        sys.exit(1)

    n = len(STATE_REGIONS)
    print(f"  \u2713 {n} entries loaded (including any sentinel)")
    return STATE_REGIONS


# ── STEP 3: Recompute centers with new thresholds ───────────────────

def compute_region_center(state, counties, counties_gdf):
    """Compute (center_lat, center_lon, zoom) with NEW thresholds.
    Same logic as Region Builder and migrate_petroleum_regions.py."""
    if not state or not counties:
        return None
    state_gdf = counties_gdf[counties_gdf["state"] == state]
    matching = state_gdf[state_gdf["county_name"].isin(counties)]
    if matching.empty:
        return None
    center_lat = float(matching["centroid_lat"].mean())
    center_lon = float(matching["centroid_lon"].mean())
    lat_span = matching["centroid_lat"].max() - matching["centroid_lat"].min()
    lon_span = matching["centroid_lon"].max() - matching["centroid_lon"].min()
    span = max(lat_span, lon_span, 0.1)
    # NEW thresholds — one zoom level tighter than the original
    if   span < 0.5:  zoom = 10
    elif span < 1.0:  zoom = 9
    elif span < 2.0:  zoom = 8
    elif span < 4.0:  zoom = 7
    elif span < 8.0:  zoom = 6
    else:             zoom = 5
    return (center_lat, center_lon, zoom)


def compute_all_centers(sr, counties_gdf):
    print()
    print("=" * 70)
    print("STEP 3: Recompute centers with new zoom thresholds")
    print("=" * 70)
    centers = {}
    for label, value in sr.items():
        # Handle 2-tuple (legacy) or 3-tuple (already migrated once)
        if len(value) == 2:
            state, counties = value
        else:
            state, counties, _ = value
        center = compute_region_center(state, counties, counties_gdf)
        centers[label] = center
        if center is None:
            print(f"  - {label:35s} (no center — sentinel or empty)")
        else:
            n_matched = len(
                counties_gdf[
                    (counties_gdf["state"] == state)
                    & (counties_gdf["county_name"].isin(counties))
                ]
            )
            print(f"  \u2713 {label:35s} "
                  f"({center[0]:.3f}, {center[1]:.3f}, z={center[2]}) "
                  f"[{n_matched}/{len(counties)} matched]")
    return centers


# ── STEP 4: Rewrite state_regions.py ────────────────────────────────

def format_new_file(sr, centers):
    """Generate the new state_regions.py with 3-tuple format."""
    lines = []
    lines.append('"""')
    lines.append("state_regions.py")
    lines.append("=================")
    lines.append("State-region definitions, originally built via Region")
    lines.append("Builder, then retrofitted by migrate_state_regions.py")
    lines.append("to use updated zoom thresholds.")
    lines.append("")
    lines.append("Each entry is a 3-tuple:")
    lines.append("    (state_code, [county_names], (lat, lon, zoom))")
    lines.append("")
    lines.append("Same shape as petroleum_regions.py so well_map can read")
    lines.append("either interchangeably.")
    lines.append('"""')
    lines.append("")
    lines.append("STATE_REGIONS = {")

    for label, value in sr.items():
        # Source may be 2- or 3-tuple
        if len(value) == 2:
            state, counties = value
        else:
            state, counties, _ = value
        center = centers.get(label)

        # Sentinel row
        if state is None and not counties:
            lines.append(f'    "{label}": (None, [], None),')
            continue

        # Format center
        if center is None:
            center_repr = "None"
        else:
            _clat, _clon, _czoom = center
            center_repr = f"({_clat:.4f}, {_clon:.4f}, {_czoom})"

        # Empty county list
        if not counties:
            lines.append(f'    "{label}": ("{state}", [], {center_repr}),')
            continue

        # Multi-county region — wrap counties at ~5 per line
        lines.append("")
        lines.append(f'    "{label}": ("{state}", [')
        for i in range(0, len(counties), 5):
            chunk = counties[i:i+5]
            quoted = ", ".join(f'"{c}"' for c in chunk)
            lines.append(f"        {quoted},")
        lines.append(f"    ], {center_repr}),")

    lines.append("}")
    return "\n".join(lines) + "\n"


def write_new_file(sr, centers):
    print()
    print("=" * 70)
    print("STEP 4: Write new state_regions.py")
    print("=" * 70)

    if SR_FILE.exists():
        shutil.copy2(SR_FILE, SR_BACKUP)
        print(f"  \u2713 Backup written: {SR_BACKUP.name}")

    new_text = format_new_file(sr, centers)
    SR_FILE.write_text(new_text, encoding="utf-8")
    print(f"  \u2713 Wrote {len(new_text):,} bytes to {SR_FILE.name}")


# ── MAIN ────────────────────────────────────────────────────────────

def main():
    counties_gdf = load_counties()
    sr = load_state_regions()
    centers = compute_all_centers(sr, counties_gdf)
    write_new_file(sr, centers)
    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)
    print()
    print("Next steps:")
    print("  1. Open state_regions.py to verify the new format")
    print("  2. If centers look wrong, restore from .bak:")
    print(f"     Copy-Item {SR_BACKUP.name} {SR_FILE.name} -Force")
    print("  3. Otherwise — restart Streamlit to use the new zoom levels")


if __name__ == "__main__":
    main()
