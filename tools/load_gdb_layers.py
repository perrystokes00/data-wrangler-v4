r"""Load selected File Geodatabase layers into dv_spatial_layer, for the map.

WHY A .gdb NEEDS ITS OWN DOOR. The File Catalog walks FILES, and a .gdb is a
DIRECTORY -- it sees a00000001.gdbtable and never the geodatabase as a unit.
Two extension lists disagree about this and both are half right:
catalog_rules._SHP_EXTS lists '.gdb', extract_core.SHP_EXTS does not, so the
catalog classifies one as spatial and no handler ever opens it. Rather than
teach the file walker about directories, this reads the .gdb directly -- fiona
opens it fine -- and hands each layer to the loader that already exists.

CRS IS THE THING THAT SILENTLY RUINS THIS. The RMOTC geodatabase is NAD27 /
Wyoming State Plane, in feet. Loaded without reprojection every feature lands
in the Gulf of Guinea, which is the same failure the shapefile registrar warns
about for a missing .prj -- except here the CRS is declared, so there is no
excuse. Every layer is converted to EPSG:4326 before it is written, and a layer
whose CRS cannot be determined is REFUSED rather than guessed at.

    python tools/load_gdb_layers.py --gdb <path>            # list what is there
    python tools/load_gdb_layers.py --gdb <path> --apply    # load the selection
    python tools/load_gdb_layers.py --gdb <path> --layer WELLS_2008 --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# The twelve worth having, and what each IS. Category drives the map's colour
# and fill defaults, so a fault line and a lease outline do not arrive looking
# identical. Everything else in the RMOTC .gdb is CAD annotation, ArcInfo
# coverage arcs, or raster auxiliary tables -- 137 layers in, 12 that earn a
# place on a map.
SELECTION = [
    # (layer,                    display name,             category,   tooltip fields)
    ("NPR3_Boundary",            "NPR-3 Boundary",         "BOUNDARY", ["Layer"]),
    ("Structure_Tensleep",       "Tensleep Structure",     "FIELD",    ["Id", "TVDSS"]),
    ("Faults_Tensleep",          "Tensleep Faults",        "FIELD",    ["Id"]),
    ("Structure_2ndWallCreek",   "2nd Wall Creek Structure", "FIELD",  ["Id"]),
    ("Faults_2ndWallCreek",      "2nd Wall Creek Faults",  "FIELD",    ["Id"]),
    ("WELLS_2008",               "Wells (2008)",           "WELL",
     ["API", "Operator", "Name", "Number"]),
    ("CoreInventory",            "Core Inventory",         "WELL",
     ["WELL", "Well_Name", "FORMATION", "SEC"]),
    ("OIL_PIPELINE",             "Oil Pipelines",          "PIPELINE", ["Layer"]),
    ("GAS_Pipelines",            "Gas Pipelines",          "PIPELINE", ["Layer"]),
    ("PLS",                      "PLS Section Lines",      "LEASE",    ["PLS_", "SYMBOL"]),
    ("Contours_10ft",            "Topography (10 ft)",     "OTHER",    []),
    ("Facilities",               "Facilities",             "OTHER",    ["Layer"]),
]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Load File Geodatabase layers into dv_spatial_layer.")
    ap.add_argument("--gdb", required=True, help="path to the .gdb directory")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--layer", action="append",
                    help="only this layer (repeatable); default is the curated set")
    ap.add_argument("--all", action="store_true",
                    help="every layer with geometry, not just the curated set")
    ap.add_argument("--source", default="SHAPEFILE",
                    help="source code; must already exist in dv_r_source")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.gdb):
        print("Not a directory: %s\nA .gdb is a FOLDER, not a file." % a.gdb)
        return 2

    try:
        import fiona
        import geopandas as gpd
    except ImportError as e:
        print("Needs fiona + geopandas: %s" % e)
        return 2

    from dataview.core.dw_utils import make_engine
    # THE SAME TWO FUNCTIONS THE PAGE CALLS. list_source_layers and
    # import_layer live in dv_spatial_loader; this is the headless door
    # onto them, not a second implementation.
    from dataview.mapping.dv_spatial_loader import import_layer

    names = fiona.listlayers(a.gdb)
    print("%s\n%d layer(s) in the geodatabase\n" % (a.gdb, len(names)))

    if a.layer:
        want = [(n, n.replace("_", " "), "OTHER", []) for n in a.layer if n in names]
        missing = [n for n in a.layer if n not in names]
        for m in missing:
            print("   ! no such layer: %s" % m)
    elif a.all:
        want = [(n, n.replace("_", " "), "OTHER", []) for n in names]
    else:
        want = [s for s in SELECTION if s[0] in names]
        absent = [s[0] for s in SELECTION if s[0] not in names]
        for m in absent:
            print("   ! curated layer absent from this .gdb: %s" % m)

    if not want:
        print("Nothing to load.")
        return 2

    print("%-28s %-22s %-9s %8s  %s"
          % ("layer", "as", "category", "features", "crs"))
    print("-" * 92)
    plan = []
    for lname, disp, cat, tips in want:
        try:
            gdf = gpd.read_file(a.gdb, layer=lname)
        except Exception as e:
            print("%-28s  UNREADABLE: %s" % (lname, str(e)[:44]))
            continue
        if gdf.empty or "geometry" not in gdf or gdf.geometry.isna().all():
            print("%-28s  no geometry — skipped" % lname)
            continue
        # REFUSE RATHER THAN GUESS. A layer with no CRS cannot be reprojected,
        # and assuming degrees puts Wyoming in the Gulf of Guinea.
        if gdf.crs is None:
            print("%-28s  NO CRS — refused (cannot reproject safely)" % lname)
            continue
        src_crs = str(gdf.crs).splitlines()[0][:28]
        try:
            gdf = gdf.to_crs("EPSG:4326")
        except Exception as e:
            print("%-28s  reprojection failed: %s" % (lname, str(e)[:40]))
            continue
        # DATES ARE NOT JSON. gdf.to_json() raises "Object of type Timestamp
        # is not JSON serializable" on any datetime column -- WELLS_2008 has
        # one, and it took the whole layer down while eleven others loaded.
        # ISO strings keep the value readable in a tooltip and serialise.
        for _c in gdf.columns:
            if _c != "geometry" and str(gdf[_c].dtype).startswith("datetime"):
                gdf[_c] = gdf[_c].astype(str)
        print("%-28s %-22s %-9s %8d  %s"
              % (lname[:28], disp[:22], cat, len(gdf), src_crs))
        plan.append((lname, disp, cat, tips, gdf))

    if not a.apply:
        print("\n%d layer(s) ready. COUNTS ONLY — nothing written. "
              "Re-run with --apply." % len(plan))
        return 0

    engine = make_engine(a.database)
    total, ok_layers = 0, 0
    print()
    for i, (lname, disp, cat, tips, gdf) in enumerate(plan):
        try:
            res = import_layer(
                engine, a.gdb, layer=lname,
                layer_name=disp,
                layer_category=cat,
                tooltip_fields=tips,
                display_order=i,
                # AN EXISTING CANONICAL CODE. source FKs to dv_r_source and
                # "RMOTC_GDB" is not registered -- the guard rejected all
                # twelve layers, which is what it is for. An import must not
                # mint standards vocabulary for one dataset.
                source=a.source)
            n = res.get("loaded", 0)
            total += n
            ok_layers += 1
            print("   %-28s %s feature(s)%s"
                  % (disp, format(n, ","),
                     ("  " + "; ".join(res["errors"])) if res.get("errors") else ""))
        except Exception as e:
            print("   %-28s FAILED: %s: %s" % (disp, type(e).__name__, str(e)[:60]))
    # SAY WHEN NOTHING LANDED. "0 feature(s) across 12 layer(s)" reads like
    # a tally rather than a total failure -- a load reporting success it
    # did not have.
    if not total:
        print("NOTHING WAS WRITTEN - every layer failed. A 547 on "
              "dv_r_source above means --source is not a registered code.")
        return 1
    print("\n%s feature(s) across %d layer(s) written to dv_spatial_layer."
          % (format(total, ","), ok_layers))
    print("Tick them in the map's '🗺 Registered layers' panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
