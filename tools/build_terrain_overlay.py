"""Build a hypsometric (terrain-tinted) image from a contour layer.

    python tools/build_terrain_overlay.py --layer "Topography (10 ft)"
    python tools/build_terrain_overlay.py --layer "Topography (10 ft)" --grid 900
    python tools/build_terrain_overlay.py --list

WHY AN IMAGE AND NOT POLYGONS
    The obvious way to fill the bands is to polygonize the contours. Measured
    on Teapot: 438 lines at 100 ft become 325 polygons in 0.2s -- but they are
    one frame-sized polygon plus hundreds of slivers, because contours stop at
    the edge of the survey instead of closing. Every one of those would need
    its elevation INFERRED, and a mis-coloured band is a confident wrong
    picture: it plots, it exports, and it gets quoted. This interpolates a
    surface instead and says so.

WHY IT IS PRECOMPUTED
    The interpolation is the expensive half: 1.65M contour vertices and a
    700x700 grid took 367 seconds. Doing it once and storing a PNG turns the
    map's job into drawing a single image. The contour layer it comes from
    costs ~7s of json.loads on EVERY render, 279 MB of it, so this is faster
    to draw as well as prettier.

WHAT IT IS NOT
    Interpolated between contours, not measured. The lines remain the exact
    data; this is the readable version of them. The registered layer is named
    so that is visible in the layer list rather than buried here.
"""
import argparse
import os
import sys
import time
import warnings

# THE ONE LINE THAT MAKES `python tools/<name>.py` WORK. Python puts the
# SCRIPT's directory on sys.path[0], never the repo root, so `dataview` is
# not importable without this -- the failure that made 26 of 28 tools in
# here unrunnable. A no-op under `python -m`, so it never breaks that form.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

warnings.filterwarnings("ignore")

DEFAULT_DB = "DataView_Demo"
OUT_DIR = os.path.join("C:\\", "Bulk", "terrain")


def _engine(db):
    from sqlalchemy import create_engine
    return create_engine(
        "mssql+pyodbc://@localhost\\SQLEXPRESS/%s"
        "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes" % db)


def _terrain_rgb(z01):
    """Conventional terrain ramp, low to high, as float RGB in 0..1.

    Hand-built rather than matplotlib's "terrain", whose lowest stop is blue
    -- correct for a map that includes water and wrong for a Wyoming dome,
    where the lowest contour is still 4,830 ft of dry ground. Green valley to
    tan to brown to a pale summit.
    """
    import numpy as np
    stops = [
        (0.00, (0.243, 0.443, 0.220)),   # deep green
        (0.25, (0.478, 0.612, 0.298)),   # green
        (0.50, (0.808, 0.780, 0.478)),   # tan
        (0.72, (0.663, 0.510, 0.322)),   # brown
        (0.88, (0.529, 0.404, 0.310)),   # dark brown
        (1.00, (0.949, 0.937, 0.918)),   # pale summit
    ]
    out = np.zeros(z01.shape + (3,), dtype=float)
    for i in range(len(stops) - 1):
        a, ca = stops[i]
        b, cb = stops[i + 1]
        m = (z01 >= a) & (z01 <= b)
        if not m.any():
            continue
        t = (z01[m] - a) / (b - a)
        for c in range(3):
            out[m, c] = ca[c] + (cb[c] - ca[c]) * t
    return out


NAIP_WMS = ("https://imagery.nationalmap.gov/arcgis/services/USGSNAIPPlus"
            "/ImageServer/WMSServer")


def _merc(lon, lat):
    import math
    x = lon * 20037508.34 / 180.0
    y = (math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
         * 20037508.34 / 180.0)
    return x, y


def build_naip(name, db, bbox, mpp, static_dir, infrared=False, tile_px=2048):
    """Bake a NAIP aerial for one extent and register it as an IMAGE layer.

    WHY BAKE IT AT ALL. NAIP is a live WMS with no tile cache: measured over
    Teapot Dome, a 512px request took 13.5s cold and 1.2s warm. That is fine
    for switching a layer on to look at a pad and wrong for a page that
    redraws. Fetched once and served from static/, the same extent draws
    instantly and forever, exactly as the terrain fill does.

    JPEG, NOT PNG. This is photography: the Teapot field at 1.6 m/pixel is
    0.8 MB as JPEG and about six times that as PNG, and the map has to send
    it to a browser. The DEM stays PNG because it needs an alpha channel for
    the ground outside the data; an aerial is opaque everywhere.

    TILED FOR THE SAME REASON THE DEM IS: the service accepts a large size
    and then times out rendering it. Tiles of 2048 are stitched here.
    """
    import numpy as np
    from PIL import Image
    import urllib.parse
    import urllib.request
    from sqlalchemy import text

    W, S, E, N = bbox
    x1, y1 = _merc(W, S)
    x2, y2 = _merc(E, N)
    # Metres per pixel is the honest control: "how much detail", not "how
    # many pixels", which changes meaning with the size of the area.
    #
    # MERCATOR METRES ARE NOT GROUND METRES. Web Mercator stretches by
    # 1/cos(latitude), so at 43 degrees north a "metre" in the projection is
    # 0.73 m on the ground. Dividing the projected span by the requested
    # resolution therefore asked for 37% more pixels than the user wanted at
    # Teapot, and would have quietly asked for double up near the Canadian
    # line. Scale by the cosine and --mpp means what it says: GROUND metres.
    import math as _math
    _coslat = max(0.15, _math.cos(_math.radians((S + N) / 2.0)))
    _proj_mpp = float(mpp) / _coslat
    px = max(256, int(round((x2 - x1) / _proj_mpp)))
    ny = max(256, int(round((y2 - y1) / _proj_mpp)))
    cols = max(1, (px + tile_px - 1) // tile_px)
    rows = max(1, (ny + tile_px - 1) // tile_px)
    tw, th = px // cols, ny // rows
    px, ny = tw * cols, th * rows
    layer = ("USGSNAIPPlus:FalseColorComposite" if infrared
             else "USGSNAIPPlus:NaturalColor")
    print("fetching     : %d x %d as %dx%d tile(s) at ~%.2f m/px ground  [%s]"
          % (px, ny, cols, rows, (x2 - x1) / px * _coslat,
             "infrared" if infrared else "natural colour"), flush=True)

    canvas = Image.new("RGB", (px, ny))
    dx, dy = (x2 - x1) / cols, (y2 - y1) / rows
    t0, got = time.time(), 0
    for r_i in range(rows):
        for c_i in range(cols):
            # Row 0 is the TOP of the image, which is the NORTH edge.
            bx1 = x1 + c_i * dx
            by2 = y2 - r_i * dy
            q = {"service": "WMS", "request": "GetMap", "version": "1.3.0",
                 "layers": layer, "styles": "", "crs": "EPSG:3857",
                 "bbox": "%f,%f,%f,%f" % (bx1, by2 - dy, bx1 + dx, by2),
                 "width": tw, "height": th, "format": "image/jpeg"}
            url = NAIP_WMS + "?" + urllib.parse.urlencode(q)
            # BACK OFF, AND PACE. A whole field at 1.5 m/pixel is dozens of
            # requests, and firing them back to back earned a 502 from a
            # service that answers the same request happily on its own. It
            # is a free public endpoint: waiting is the polite and the
            # working option. Backoff grows, and the tile that failed is
            # named so a permanent failure is not mistaken for a wobble.
            blob = None
            for attempt, wait in enumerate((6, 15, 40)):
                try:
                    with urllib.request.urlopen(url, timeout=600) as resp:
                        blob = resp.read()
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise RuntimeError(
                            "tile r%d c%d failed three times: %s"
                            % (r_i, c_i, exc))
                    print("   tile r%d c%d: %s, waiting %ds"
                          % (r_i, c_i, type(exc).__name__, wait), flush=True)
                    time.sleep(wait)
            time.sleep(0.4)
            tmp = os.path.join(OUT_DIR, "_naip_tile.jpg")
            os.makedirs(OUT_DIR, exist_ok=True)
            open(tmp, "wb").write(blob)
            canvas.paste(Image.open(tmp), (c_i * tw, r_i * th))
            got += len(blob)
            print("   tile %d/%d  %.1f MB  %.0fs"
                  % (r_i * cols + c_i + 1, rows * cols, got / 1e6,
                     time.time() - t0), flush=True)

    os.makedirs(static_dir, exist_ok=True)
    slug = "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()
    fname = "naip_%s%s.jpg" % (slug, "_nir" if infrared else "")
    out = os.path.join(static_dir, fname)
    canvas.save(out, "JPEG", quality=86, optimize=True)
    print("image        : %s  (%.1f MB)" % (out, os.path.getsize(out) / 1e6))

    eng = _engine(db)
    lname = "%s - NAIP %s" % (name, "infrared" if infrared else "aerial")
    lid = ("naip_" + slug + ("_nir" if infrared else ""))[:40]
    with eng.begin() as cn:
        cn.execute(text("DELETE FROM dataview.dv_spatial_layer "
                        "WHERE layer_id = :lid"), {"lid": lid})
        cn.execute(text("""
            INSERT INTO dataview.dv_spatial_layer
                (layer_id, layer_name, layer_type, layer_category,
                 source_type, file_path, feature_count,
                 bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon,
                 geometry_wkt, style_opacity, display_order,
                 active_ind, row_created_by, row_created_date,
                 source, remark)
            VALUES (:lid, :name, 'IMAGE', 'OTHER', 'IMAGE', :png, 1,
                    :minlat, :maxlat, :minlon, :maxlon,
                    NULL, 1.0, 45, 'Y', 'NAIP_BUILDER', GETDATE(),
                    'COMPUTED', :remark)"""),
                   {"lid": lid, "name": lname, "png": out,
                    "minlat": S, "maxlat": N, "minlon": W, "maxlon": E,
                    "remark": ("USGS NAIP %s, ~%.1f m/pixel, baked %s. "
                               "Public domain aerial photography."
                               % ("false colour infrared" if infrared
                                  else "natural colour",
                                  (x2 - x1) / px * _coslat,
                                  time.strftime("%Y-%m-%d")))})
    print("registered   : %s  (layer_id %s)" % (lname, lid))
    print("\nTick it in Registered layers and press Apply to map.")


def build_dem(name, db, bbox, px, alpha, static_dir, keep_tif=True):
    """Colour a real DEM from USGS 3DEP and register it as an IMAGE layer.

    MEASURED GROUND, NOT INTERPOLATED. The Teapot fill is a surface derived
    from contour lines and says so; this is the 3D Elevation Program's bare
    earth model, so it carries no such caveat. Public domain, one request
    rather than tiles: the ImageServer renders the whole extent at the size
    asked for.

    METRES IN, FEET OUT. The service returns F32 metres; everything else in
    this application talks feet, and a hypsometric legend in the wrong unit
    is the kind of confident wrong number that gets quoted.

    THE PNG GOES IN static/, NOT INTO THE MAP. folium base64-embeds a local
    image into the HTML, which for a statewide raster would put tens of
    megabytes into every render. Streamlit serves static/ at /app/static/,
    the browser caches it, and the same trick already carries the 35 MB
    lease geojson.
    """
    import numpy as np
    from PIL import Image
    import urllib.parse
    import urllib.request
    from sqlalchemy import text

    W, S, E, N = bbox
    ny = max(16, int(round(px * (N - S) / (E - W))))
    Image.MAX_IMAGE_PIXELS = None      # a state-sized raster is not a bomb

    # ── TILED, BECAUSE ONE BIG REQUEST TIMES OUT ────────────────────────
    # The service advertises 8000x8000 and returns 504 Gateway Time-out when
    # asked to render a state at that size -- the limit is what it will
    # ACCEPT, not what it will finish. A 1000px request came back in 1.1s,
    # so the work is fine in pieces. Tiles of ~2000px each are stitched
    # here, which also means detail is bounded by patience rather than by
    # the service's ceiling.
    _TILE = 2000
    cols = max(1, (px + _TILE - 1) // _TILE)
    rows = max(1, (ny + _TILE - 1) // _TILE)
    tw, th = px // cols, ny // rows
    px, ny = tw * cols, th * rows          # exact, so the tiles fit flush
    z_m = np.full((ny, px), np.nan, dtype="float32")
    dx, dy = (E - W) / cols, (N - S) / rows
    t0, got = time.time(), 0
    print("fetching     : %d x %d as %dx%d tiles from USGS 3DEP ..."
          % (px, ny, cols, rows), flush=True)
    for r_i in range(rows):
        for c_i in range(cols):
            # Row 0 of the array is the TOP of the image, which is the
            # NORTH edge; tiles are requested from the top down to match.
            w = W + c_i * dx
            n = N - r_i * dy
            q = {"bbox": "%f,%f,%f,%f" % (w, n - dy, w + dx, n),
                 "bboxSR": 4326, "imageSR": 4326,
                 "size": "%d,%d" % (tw, th),
                 "format": "tiff", "pixelType": "F32",
                 "noDataInterpretation": "esriNoDataMatchAny",
                 "interpolation": "RSP_BilinearInterpolation", "f": "image"}
            url = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
                   "3DEPElevation/ImageServer/exportImage?"
                   + urllib.parse.urlencode(q))
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(url, timeout=300) as resp:
                        blob = resp.read()
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise
                    print("   tile %d,%d retry after %s"
                          % (r_i, c_i, type(exc).__name__), flush=True)
                    time.sleep(3)
            tmp = os.path.join(OUT_DIR, "_tile.tif")
            open(tmp, "wb").write(blob)
            z_m[r_i * th:(r_i + 1) * th, c_i * tw:(c_i + 1) * tw] = \
                np.array(Image.open(tmp), dtype="float32")
            got += len(blob)
            print("   tile %d/%d  %.1f MB total  %.0fs"
                  % (r_i * cols + c_i + 1, rows * cols, got / 1e6,
                     time.time() - t0), flush=True)
    tif = os.path.join(OUT_DIR, "%s_dem.npy" % name.lower().replace(" ", "_"))
    os.makedirs(OUT_DIR, exist_ok=True)
    np.save(tif, z_m)
    print("dem          : %s  (%.1f MB fetched in %.0fs)"
          % (tif, got / 1e6, time.time() - t0))
    good = np.isfinite(z_m) & (z_m > -1e30)
    z = np.where(good, z_m * 3.280839895, np.nan)      # metres -> feet
    lo, hi = float(np.nanmin(z)), float(np.nanmax(z))
    print("elevation    : %.0f to %.0f ft  (%.0f ft of relief)"
          % (lo, hi, hi - lo))

    z01 = np.clip((z - lo) / (hi - lo), 0, 1)
    rgb = _terrain_rgb(np.nan_to_num(z01))
    img = np.zeros(z.shape + (4,), dtype=np.uint8)
    img[..., :3] = (rgb * 255).astype(np.uint8)
    img[..., 3] = np.where(good, int(alpha * 255), 0).astype(np.uint8)

    os.makedirs(static_dir, exist_ok=True)
    fname = "terrain_%s.png" % name.lower().replace(" ", "_")
    png = os.path.join(static_dir, fname)
    Image.fromarray(img, "RGBA").save(png, optimize=True)
    print("image        : %s  (%.1f MB)" % (png, os.path.getsize(png) / 1e6))
    if not keep_tif:
        os.remove(tif)

    eng = _engine(db)
    lname = "%s - terrain (DEM)" % name
    lid = "dem_" + "".join(c for c in name if c.isalnum()).lower()[:36]
    with eng.begin() as cn:
        cn.execute(text("DELETE FROM dataview.dv_spatial_layer "
                        "WHERE layer_id = :lid"), {"lid": lid})
        cn.execute(text("""
            INSERT INTO dataview.dv_spatial_layer
                (layer_id, layer_name, layer_type, layer_category,
                 source_type, file_path, feature_count,
                 bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon,
                 geometry_wkt, style_opacity, display_order,
                 active_ind, row_created_by, row_created_date,
                 source, remark)
            VALUES (:lid, :name, 'IMAGE', 'OTHER', 'IMAGE', :png, 1,
                    :minlat, :maxlat, :minlon, :maxlon,
                    NULL, :op, 40, 'Y', 'TERRAIN_BUILDER', GETDATE(),
                    'COMPUTED', :remark)"""),
                   {"lid": lid, "name": lname, "png": png,
                    "minlat": S, "maxlat": N, "minlon": W, "maxlon": E,
                    "op": float(alpha),
                    "remark": ("USGS 3DEP bare earth DEM, %d ft/pixel approx, "
                               "%.0f-%.0f ft. Measured, not interpolated."
                               % (int((E - W) * 364000 / px), lo, hi))})
    print("registered   : %s  (layer_id %s)" % (lname, lid))
    print("\nTick it in Registered layers and press Apply to map.")


def build(layer_name, db, grid_n, every, alpha, reuse=False):
    import numpy as np
    from scipy.interpolate import LinearNDInterpolator
    from PIL import Image
    import geopandas as gpd
    from sqlalchemy import text

    eng = _engine(db)
    with eng.connect() as cn:
        row = cn.execute(text(
            "SELECT layer_id, file_path, source_type FROM dataview.dv_spatial_layer "
            "WHERE layer_name = :n"), {"n": layer_name}).fetchone()
    if not row:
        sys.exit("no registered layer named %r in %s" % (layer_name, db))
    src_id, fpath, stype = row
    print("source layer : %s  (%s)" % (layer_name, stype))
    print("file_path    : %s" % fpath)

    # READ THE SOURCE FILE, NOT THE STORED GEOJSON. The blob is 279 MB and
    # parsing it costs 11s; the GDB it came from reads in 10s and carries the
    # attributes in their original types.
    t0 = time.time()
    if not fpath or not os.path.exists(os.path.dirname(fpath.rstrip("\\/"))):
        sys.exit("source file not reachable: %s" % fpath)
    gdb, layer = os.path.split(fpath)
    gdf = gpd.read_file(gdb, layer=layer).to_crs("EPSG:4326")
    zcol = next((c for c in ("CONTOUR", "ELEVATION", "ELEV", "LEVEL")
                 if c in gdf.columns), None)
    if zcol is None:
        sys.exit("no elevation column in %s" % layer)
    print("read         : %d line(s) in %.1fs, elevation column %s"
          % (len(gdf), time.time() - t0, zcol))

    pts, zs = [], []
    for geom, z in zip(gdf.geometry, gdf[zcol]):
        if geom is None:
            continue
        parts = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        for line in parts:
            for x, y in list(line.coords)[::every]:
                pts.append((x, y))
                zs.append(float(z))
    pts = np.asarray(pts)
    zs = np.asarray(zs)
    print("vertices     : %d (every %dth)" % (len(pts), every))

    minx, miny = pts.min(axis=0)
    maxx, maxy = pts.max(axis=0)
    # SQUARE PIXELS IN THE OUTPUT, so the image is not stretched when Leaflet
    # lays it on the bounds. Latitude and longitude do not span equally here.
    aspect = (maxy - miny) / (maxx - minx)
    nx = grid_n
    ny = max(16, int(round(grid_n * aspect)))
    os.makedirs(OUT_DIR, exist_ok=True)
    png = os.path.join(OUT_DIR, "%s.png" % "".join(
        c if c.isalnum() else "_" for c in layer_name))

    # --reuse SKIPS THE EXPENSIVE HALF, NOT THE BOUNDS. Registering the layer
    # needs the extent, and the extent comes from the same vertices, so the
    # 10s read still happens -- it is the 367s interpolation that is worth
    # not repeating when only the registration failed. (It did: the first run
    # built the image and then hit the FK on `source`, which is the reference
    # guard doing its job.)
    if reuse and os.path.exists(png):
        print("grid         : skipped, reusing %s" % png)
        lo = hi = None
    else:
        t1 = time.time()
        interp = LinearNDInterpolator(pts, zs)
        gx, gy = np.meshgrid(np.linspace(minx, maxx, nx),
                             np.linspace(miny, maxy, ny))
        z = interp(gx, gy)
        print("grid         : %dx%d in %.1fs  (%.1f%% inside the data)"
              % (nx, ny, time.time() - t1, 100.0 * np.isfinite(z).mean()))

        lo, hi = np.nanmin(z), np.nanmax(z)
        z01 = np.clip((z - lo) / (hi - lo), 0, 1)
        rgb = _terrain_rgb(np.nan_to_num(z01))
        img = np.zeros(z.shape + (4,), dtype=np.uint8)
        img[..., :3] = (rgb * 255).astype(np.uint8)
        # TRANSPARENT WHERE THERE IS NO DATA. The interpolator returns NaN
        # outside the contours' hull, and painting those pixels would draw a
        # rectangle of invented ground over the basemap.
        img[..., 3] = np.where(np.isfinite(z),
                               int(alpha * 255), 0).astype(np.uint8)
        # Leaflet's origin is top-left; the grid's is bottom-left.
        img = img[::-1]
        Image.fromarray(img, "RGBA").save(png)

    print("image        : %s  (%.1f MB)" % (png, os.path.getsize(png) / 1e6))
    if lo is not None:
        print("elevation    : %.0f to %.0f ft" % (lo, hi))

    # ── register it like any other layer ────────────────────────────────
    # Same table, same Show grid, same Apply. source_type IMAGE tells the map
    # to lay it on its bounds instead of parsing geometry.
    new_name = "%s - terrain fill" % layer_name.split(" (")[0]
    lid = "terrain_" + "".join(c for c in new_name if c.isalnum()).lower()[:40]
    with eng.begin() as cn:
        cn.execute(text("DELETE FROM dataview.dv_spatial_layer "
                        "WHERE layer_id = :lid"), {"lid": lid})
        cn.execute(text("""
            INSERT INTO dataview.dv_spatial_layer
                (layer_id, layer_name, layer_type, layer_category,
                 source_type, file_path, feature_count,
                 bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon,
                 geometry_wkt, style_opacity, display_order,
                 active_ind, row_created_by, row_created_date,
                 source, remark)
            VALUES (:lid, :name, 'IMAGE', 'OTHER',
                    'IMAGE', :png, 1,
                    :minlat, :maxlat, :minlon, :maxlon,
                    NULL, :op, 50,
                    'Y', 'TERRAIN_BUILDER', GETDATE(),
                    'COMPUTED', :remark)"""),
                   {"lid": lid, "name": new_name, "png": png,
                    "minlat": float(miny), "maxlat": float(maxy),
                    "minlon": float(minx), "maxlon": float(maxx),
                    "op": float(alpha),
                    "remark": "Interpolated from %s. The lines are the "
                              "measured data; this surface is derived."
                              % layer_name})
    print("registered   : %s  (layer_id %s)" % (new_name, lid))
    print("\nTick it in Registered layers and press Apply to map.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", default="Topography (10 ft)")
    ap.add_argument("--database", default=DEFAULT_DB)
    ap.add_argument("--grid", type=int, default=900,
                    help="pixels across; height follows the aspect ratio")
    ap.add_argument("--every", type=int, default=6,
                    help="use every Nth vertex; the interpolation is the "
                         "expensive half and contour vertices are dense")
    ap.add_argument("--alpha", type=float, default=0.75)
    ap.add_argument("--reuse", action="store_true",
                    help="register the PNG that is already built, skipping "
                         "the interpolation (minutes)")
    # ── DEM MODE: a real elevation model, for ground the contours do not
    # cover. STATES is here rather than in the caller so the bbox that gets
    # requested is written down once.
    ap.add_argument("--dem", metavar="STATE",
                    help="build from the USGS 3DEP DEM for a state, e.g. WY")
    ap.add_argument("--px", type=int, default=8000,
                    help="pixels across; 8000 is the service maximum")
    ap.add_argument("--naip", metavar="PLACE",
                    help="bake a NAIP aerial for a SAVED PLACE by name "
                         "(or use --bbox W,S,E,N with --name)")
    ap.add_argument("--infrared", action="store_true",
                    help="false colour composite: vegetation red, disturbed "
                         "ground pale, which is how a pad reads at a glance")
    ap.add_argument("--mpp", type=float, default=1.5,
                    help="ground resolution in metres per pixel (default 1.5; "
                         "NAIP itself is about 1 m, below that is empty zoom)")
    ap.add_argument("--township", metavar="LABEL",
                    help="bake a NAIP aerial for one PLSS township, by its "
                         "label (e.g. \"T39N R78W\") or its plss_id")
    ap.add_argument("--bbox", help="W,S,E,N in degrees, instead of a place")
    ap.add_argument("--name", help="layer name when --bbox is used")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.naip or a.township or (a.bbox and a.name):
        _static = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static")
        if a.township:
            # A TOWNSHIP IS NOT A SAVED PLACE. It comes from
            # dv_plss_township, which stores its own bbox_wkt, so this reads
            # the survey grid rather than asking the user to find corners.
            # Matched on the label OR the plss_id, because the map shows one
            # and the database keys on the other.
            from sqlalchemy import text as _t
            with _engine(a.database).connect() as _cn:
                _row = _cn.execute(_t("""
                    SELECT TOP 1 township_label, bbox_wkt
                      FROM dataview.dv_plss_township
                     WHERE REPLACE(UPPER(township_label),' ','') =
                           REPLACE(UPPER(:q),' ','')
                        OR UPPER(plss_id) = UPPER(:q)
                """), {"q": a.township}).fetchone()
            if not _row:
                sys.exit("no township matching %r. Labels look like "
                         "'T39N R78W'; ids look like 'WY060390N0780W0'."
                         % a.township)
            import re as _re3
            _nums = [float(v) for v in
                     _re3.findall(r"-?\d+\.?\d*", _row.bbox_wkt or "")]
            if len(_nums) < 4:
                sys.exit("township %s has no usable bbox_wkt" % _row[0])
            _lons = _nums[0::2]
            _lats = _nums[1::2]
            _bx = (min(_lons), min(_lats), max(_lons), max(_lats))
            _nm = str(_row.township_label or a.township)
            print("township     : %s  bbox %.4f,%.4f .. %.4f,%.4f"
                  % (_nm, _bx[0], _bx[1], _bx[2], _bx[3]))
            build_naip(_nm, a.database, _bx, a.mpp, _static,
                       infrared=a.infrared)
            return
        if a.bbox:
            try:
                _bx = tuple(float(v) for v in a.bbox.split(","))
                assert len(_bx) == 4
            except Exception:
                sys.exit("--bbox wants W,S,E,N in degrees")
            _nm = a.name or "Area"
        else:
            # RESOLVE THE NAME THE APP USES, so "Teapot Wells" here is the
            # same extent the Go to box moves to. The places live in the
            # user prefs file and the built-in list, not in the database.
            from dataview.mapping.page_well_map import (_saved_places,
                                                        _norm_bounds)
            _places = _saved_places(None)
            _hit = next((k for k in _places
                         if k.strip().lower() == a.naip.strip().lower()), None)
            if _hit is None:
                sys.exit("no saved place named %r. Known: %s"
                         % (a.naip, ", ".join(sorted(_places))[:400]))
            _b = _norm_bounds(_places[_hit])
            if not _b:
                sys.exit("place %r has no usable bounds" % _hit)
            (_s, _w), (_n, _e) = _b[0], _b[1]
            _bx = (_w, _s, _e, _n)
            _nm = _hit
        build_naip(_nm, a.database, _bx, a.mpp, _static, infrared=a.infrared)
        return

    if a.dem:
        STATES = {
            # Generous by a hair so the state edge is inside the raster
            # rather than clipped along it.
            "WY": ("Wyoming", (-111.10, 40.97, -104.02, 45.02)),
            "CO": ("Colorado", (-109.10, 36.97, -102.02, 41.02)),
            "MT": ("Montana", (-116.10, 44.32, -104.02, 49.02)),
            "NM": ("New Mexico", (-109.10, 31.30, -103.00, 37.02)),
            "ND": ("North Dakota", (-104.10, 45.90, -96.52, 49.02)),
            "TX": ("Texas", (-106.70, 25.80, -93.50, 36.52)),
        }
        if a.dem.upper() not in STATES:
            sys.exit("no bbox for %r; known: %s"
                     % (a.dem, ", ".join(sorted(STATES))))
        _nm, _bx = STATES[a.dem.upper()]
        _static = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static")
        build_dem(_nm, a.database, _bx, a.px, a.alpha, _static)
        return

    if a.list:
        from sqlalchemy import text
        with _engine(a.database).connect() as cn:
            for r in cn.execute(text(
                    "SELECT layer_name, layer_type, feature_count "
                    "FROM dataview.dv_spatial_layer ORDER BY layer_name")):
                print("  %-34s %-10s %8s" % (r[0], r[1], r[2]))
        return
    build(a.layer, a.database, a.grid, a.every, a.alpha, a.reuse)


if __name__ == "__main__":
    main()
