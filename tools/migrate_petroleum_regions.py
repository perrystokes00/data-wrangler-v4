"""
migrate_petroleum_regions.py
============================
One-time migration: reads the current petroleum_regions.py (2-tuple
format), computes a (center_lat, center_lon, zoom) for each region
from TIGER county centroids, and writes a new petroleum_regions.py
in the 3-tuple format.

INPUT:
    petroleum_regions.py  — current file with 2-tuple values
    tl_2024_us_county.shp — TIGER county shapefile

OUTPUT:
    petroleum_regions.py  — overwritten with 3-tuple values
    petroleum_regions.py.bak — backup of the original

RUNTIME: ~10 seconds (most of it is TIGER load)

USAGE:
    cd C:\\Users\\perry\\OneDrive\\Documents\\PPDM\\claude_use_ai\\
       data_wrangler\\data_wrangler_v3
    python tools/migrate_petroleum_regions.py

REVERSIBLE:
    Restore from petroleum_regions.py.bak if anything looks wrong.
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
PR_FILE      = V3_ROOT / "petroleum_regions.py"
PR_BACKUP    = V3_ROOT / "petroleum_regions.py.bak"

# Same FIPS→USPS lookup used by Region Builder
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
        print(f"❌ Shapefile not found: {COUNTIES_SHP}")
        sys.exit(1)
    gdf = gpd.read_file(COUNTIES_SHP)
    print(f"  ✓ {len(gdf):,} counties loaded")

    # Reproject to EPSG:4326
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4269")
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    # Find state and name columns
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

    # Precompute centroids
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Geometry is in a geographic CRS"
        )
        gdf["centroid_lat"] = gdf.geometry.centroid.y
        gdf["centroid_lon"] = gdf.geometry.centroid.x

    return gdf[["state", "county_name",
                "centroid_lat", "centroid_lon"]].copy()


# ── STEP 2: Load PETROLEUM_REGIONS from the existing file ───────────

def load_petroleum_regions():
    print()
    print("=" * 70)
    print("STEP 2: Load existing PETROLEUM_REGIONS")
    print("=" * 70)
    if not PR_FILE.exists():
        print(f"❌ Not found: {PR_FILE}")
        sys.exit(1)

    # Import the module to get PETROLEUM_REGIONS
    sys.path.insert(0, str(V3_ROOT))
    try:
        from dataview.region_builder.petroleum_regions import PETROLEUM_REGIONS
    except ImportError as e:
        print(f"❌ Could not import petroleum_regions: {e}")
        sys.exit(1)

    print(f"  ✓ {len(PETROLEUM_REGIONS)} entries loaded "
          f"(including '— none —' sentinel)")
    return PETROLEUM_REGIONS


# ── STEP 3: Compute center for each region ──────────────────────────

def compute_region_center(state, counties, counties_gdf):
    """Mean of selected counties' centroids; zoom based on span."""
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
    # Span-based zoom — bigger span = lower zoom. Thresholds tuned
    # so that Folium's fit_bounds + my span-from-zoom formula in
    # well_map produces a viewport that shows the region tightly
    # without losing context. Earlier thresholds bumped most regions
    # one zoom level too low (Permian, Gulf Coast at z=6 showed
    # whole-state Texas instead of the region's actual footprint).
    if   span < 0.5:  zoom = 10
    elif span < 1.0:  zoom = 9
    elif span < 2.0:  zoom = 8
    elif span < 4.0:  zoom = 7
    elif span < 8.0:  zoom = 6
    else:             zoom = 5
    return (center_lat, center_lon, zoom)


def compute_all_centers(pr, counties_gdf):
    print()
    print("=" * 70)
    print("STEP 3: Compute centers from county centroids")
    print("=" * 70)
    centers = {}
    for label, value in pr.items():
        # Handle both 2-tuple and 3-tuple (in case already migrated)
        if len(value) == 2:
            state, counties = value
        else:
            state, counties, _ = value
        center = compute_region_center(state, counties, counties_gdf)
        centers[label] = center
        if center:
            n_matched = len(
                counties_gdf[
                    (counties_gdf["state"] == state)
                    & (counties_gdf["county_name"].isin(counties))
                ]
            )
            print(f"  ✓ {label:35s} "
                  f"({center[0]:.3f}, {center[1]:.3f}, z={center[2]}) "
                  f"[{n_matched}/{len(counties)} matched]")
        else:
            print(f"  - {label:35s} (no center — sentinel or empty)")
    return centers


# ── STEP 4: Rewrite petroleum_regions.py ────────────────────────────

def format_new_file(pr, centers):
    """Generate the new petroleum_regions.py with 3-tuple format."""
    lines = []
    lines.append('"""')
    lines.append("petroleum_regions.py")
    lines.append("=====================")
    lines.append("PETROLEUM_REGIONS — well-known producing-area definitions,")
    lines.append("mapped to their constituent counties per state.")
    lines.append("")
    lines.append("Each entry is a 3-tuple:")
    lines.append("    (state_code, [county_names], (center_lat, center_lon, zoom))")
    lines.append("")
    lines.append("The center tuple powers auto-zoom in well_map. Centers were")
    lines.append("computed once via migrate_petroleum_regions.py from TIGER")
    lines.append("county centroids. Re-run that script if counties change.")
    lines.append("")
    lines.append("Source of truth: WranglerView prototype_filter.py")
    lines.append("Last sync:        May 26, 2026")
    lines.append("Centers migrated: " + Path(__file__).name)
    lines.append('"""')
    lines.append("")
    lines.append("PETROLEUM_REGIONS = {")

    for label, value in pr.items():
        # Source might be 2-tuple or 3-tuple
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


def write_new_file(pr, centers):
    print()
    print("=" * 70)
    print("STEP 4: Write new petroleum_regions.py")
    print("=" * 70)

    # Backup the original
    if PR_FILE.exists():
        shutil.copy2(PR_FILE, PR_BACKUP)
        print(f"  ✓ Backup written: {PR_BACKUP.name}")

    new_text = format_new_file(pr, centers)
    PR_FILE.write_text(new_text, encoding="utf-8")
    print(f"  ✓ Wrote {len(new_text):,} bytes to {PR_FILE.name}")


# ── MAIN ────────────────────────────────────────────────────────────

def main():
    counties_gdf = load_counties()
    pr = load_petroleum_regions()
    centers = compute_all_centers(pr, counties_gdf)
    write_new_file(pr, centers)
    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)
    print()
    print("Next steps:")
    print("  1. Open petroleum_regions.py to verify the new format")
    print("  2. If centers look wrong, restore from .bak:")
    print(f"     cp {PR_BACKUP.name} {PR_FILE.name}")
    print("  3. Otherwise — well_map will use the new centers on next "
          "Streamlit restart.")


if __name__ == "__main__":
    main()
