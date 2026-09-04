"""Load EIA's national basin and play outlines as registered map layers.

WHY EIA AND NOT USGS NOGA. NOGA was asked for first and could not be had: the
modern releases are published one province at a time as ASSESSMENT UNIT
boundaries, certmapper's ArcGIS REST catalogue answers 403, and the two
ScienceBase "Geologic Provinces of the World" items carry a preview JPEG and
no data. EIA publishes what was actually wanted -- 32 lower-48 sedimentary
basins and 50 named tight-oil/shale-gas plays -- as open feature services with
clean attributes. Different outlines, same question answered.

NO NEW TABLE. dv_spatial_layer already stores a layer's GeoJSON inline in
geometry_wkt and _add_shapefile_layer already draws it, so these arrive as two
more entries in the Registered layers panel beside Tensleep Faults and the
PLS section lines. Building a parallel "provinces" mechanism beside a registry
that already does this is the mistake this codebase records six times over.

    python tools/load_eia_geology.py            # both, into DataView_Demo
    python tools/load_eia_geology.py --list     # what is registered now
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request

# The one line app_v4.py uses. Python puts the SCRIPT's directory on sys.path,
# never the repo root, so "python tools/<name>.py" -- how this documents itself
# -- cannot import dataview without it. A no-op under "python -m".
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text          # noqa: E402

_ARC = ("https://services7.arcgis.com/FGr1D95XCGALKXqM/arcgis/rest/services/")

# THE LAYER ID IS PART OF THE SERVICE PATH, not a guess: the basins service
# publishes its single layer as 109, not 0. Asking for 0 returns an empty
# document rather than an error, which reads exactly like "the service is
# down" -- checked, and it is why the id is written down here.
SOURCES = {
    "basins": {
        "url": _ARC + "SedimentaryBasins_US_EIA/FeatureServer/109",
        "layer_name": "EIA Sedimentary Basins (Lower 48)",
        "layer_type": "POLYGON",
        "category": "BOUNDARY",
        "tooltip": "Name,Area_sq_mi",
        # ONE COLOUR PER BASIN. A single brown outline says where the basins
        # are and never which is which; 32 of them touching each other need
        # to be separable at a glance. lease_colour_map is reused rather than
        # a second palette invented -- it already assigns a whole SET at once
        # and pushes apart the collisions a per-name hash would produce.
        "colour_by": "Name",
        # ADJACENCY, NOT IDENTITY. See _colour_by_adjacency.
        "colour_mode": "adjacent",
        # Brown, because the map already reads blue as time and warm as
        # extent -- a basin is neither, so it gets the earth family and a
        # fill light enough to sit under everything else.
        "style": {"color": "#8a5a2b", "weight": 2.0, "opacity": 0.9,
                  "fill_color": "#c98a4b", "fill_opacity": 0.30},
        "order": 20,
    },
    "plays": {
        "url": _ARC + "TightOil_ShaleGas_Plays_Lower48_EIA/FeatureServer/0",
        "layer_name": "EIA Tight Oil / Shale Gas Plays",
        "layer_type": "POLYGON",
        "category": "BOUNDARY",
        "tooltip": "Shale_play,Basin,Age_shale",
        "style": {"color": "#6b21a8", "weight": 1.4, "opacity": 0.9,
                  "fill_color": "#a855f7", "fill_opacity": 0.22},
        "order": 21,
    },
    # ── AND FIELDS, WHICH ARE NOT A NATIONAL DATASET ──────────────────────
    # There is no national field-outline layer, and the two that claim to be
    # one are not: HIFLD's "Oil and Natural Gas Fields" is 224 BASINS wearing
    # a field label (PRODID 'BAS0001', names like NORTHERN ALASKA), and the
    # 1,372-feature layer next to it in the catalogue is Colorado only.
    #
    # Field boundaries are defined by the STATE regulator, so they arrive one
    # state at a time. This is Wyoming's, from the Enhanced Oil Recovery
    # Institute at the University of Wyoming: 1,378 fields carrying a name
    # and an oil/gas/commingled class, and it contains Teapot Dome, Teapot
    # Naval Reserve, Salt Creek and Grass Creek, which is how it was checked.
    "fields_wy": {
        "url": ("https://services8.arcgis.com/GVHDOiLGusXYn1fZ/arcgis/rest/"
                "services/EORI_Oil_Gas_Fields_OilGasComingled_WFL1/"
                "FeatureServer/0"),
        "layer_name": "Oil & Gas Fields - Wyoming (EORI)",
        "layer_type": "POLYGON",
        "category": "BOUNDARY",
        "tooltip": "FLD_NAME,OG_Class",
        # Green, kept away from the basin browns and the play purples so the
        # three can be on together and still be told apart.
        "style": {"color": "#15803d", "weight": 1.2, "opacity": 0.95,
                  "fill_color": "#22c55e", "fill_opacity": 0.12},
        "order": 22,
    },
}


# THE SOURCE CODE MUST ALREADY EXIST. dv_spatial_layer.source is a foreign
# key onto dv_r_source, and there is no 'EIA' code -- the first run died on a
# 547. CREATING ONE HERE IS NOT AN OPTION: a reference table is owned by the
# Reference Tables app, and a loader that seeds its own codes is how a domain
# quietly acquires values nobody registered. So the nearest REGISTERED code is
# used and the real provenance goes in the remark, where it is readable and
# cannot lie about being a coded value.
#
# Add 'EIA' in the Reference Tables app and pass --source EIA; the loader will
# then record it properly.
DEFAULT_SOURCE = "INDUSTRY"


def _conn(database):
    cs = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;"
          "DATABASE=%s;Trusted_Connection=yes;" % database)
    return create_engine("mssql+pyodbc:///?odbc_connect="
                         + urllib.parse.quote_plus(cs))


def fetch(url):
    """The service's features as a GeoJSON dict, in EPSG:4326.

    exceededTransferLimit IS CHECKED, NOT ASSUMED. 32 and 50 features are
    well under any page size, but a silently truncated layer draws most of
    the country and looks complete -- the confident-wrong-value failure with
    a map instead of a number.
    """
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "*",
                                "outSR": "4326", "f": "geojson"})
    req = urllib.request.Request(url + "/query?" + q,
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    doc = json.loads(raw.decode("utf-8", "replace"))
    if doc.get("exceededTransferLimit") or doc.get(
            "properties", {}).get("exceededTransferLimit"):
        raise SystemExit("%s: the service paged the result -- this loader "
                         "does not, so it would register a partial layer"
                         % url)
    feats = doc.get("features") or []
    if not feats:
        raise SystemExit("%s: no features returned" % url)
    return doc, feats


def bbox(feats):
    """(min_lat, max_lat, min_lon, max_lon) over every coordinate."""
    lats, lons = [], []

    def walk(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            lons.append(float(c[0]))
            lats.append(float(c[1]))
            return
        for x in c:
            walk(x)

    for f in feats:
        walk((f.get("geometry") or {}).get("coordinates"))
    if not lats:
        raise SystemExit("no coordinates found")
    return min(lats), max(lats), min(lons), max(lons)


# A palette wide enough to separate neighbours and narrow enough that each
# colour stays distinguishable. Eight is far more than the four a planar map
# provably needs, so the greedy pass below never runs out.
_MAP_PALETTE = ["#8a5a2b", "#2c6e8f", "#6b7f3a", "#8c4a6b", "#3f6b34",
                "#a06a2c", "#4a5a8c", "#7a3a3a"]

# THE FILL OPACITY IS THE KNOB, NOT THE HUE. These eight are already
# saturated; at the 0.08 the basins shipped with, every one of them washed
# out to a grey suggestion and the layer read as "barely coloured". Lifting
# the alpha darkens all thirty-two at once and keeps the adjacency property
# intact, where re-picking hues would have thrown it away to solve a problem
# the hues did not cause. --fill-opacity sets it; see FILL_OPACITY below.


def _colour_by_adjacency(feats, key):
    """Give touching polygons different colours. Returns {name: colour}.

    THIRTY-TWO NAMES CANNOT HAVE THIRTY-TWO DISTINGUISHABLE HUES, and pretending
    otherwise produces a legend nobody can use. What a reader actually needs
    from a basin map is to see where one basin ENDS and the next begins -- which
    is the four-colour problem, not a categorical palette. Neighbours differ;
    two basins a thousand miles apart may share, and it costs nothing because
    they are never compared.

    The first attempt hashed each name into an 8-colour palette and got 8
    distinct colours across 32 basins with no guarantee about which pairs
    collided -- Wyoming came out readable by luck, not by construction.

    Falls back to the hash if shapely is missing: a coloured map with a
    possible neighbour collision beats no colour at all.
    """
    names = [str((f.get("properties") or {}).get(key) or "") for f in feats]
    try:
        from shapely.geometry import shape as _shape
    except Exception:
        from dataview.mapping.geography_layers import lease_colour_map
        print("  shapely missing -- falling back to hashed colours")
        return lease_colour_map(sorted(set(names)))

    geoms = []
    for f in feats:
        try:
            geoms.append(_shape(f["geometry"]))
        except Exception:
            geoms.append(None)
    n = len(feats)
    # HALF A DEGREE, ~56 km, and it is a tuned number not a guess. EIA's
    # basins are scattered islands rather than a contiguous partition, so at
    # a true shared-edge tolerance only 21 pairs are neighbours and the greedy
    # pass collapses to TWO colours -- Powder River, Bighorn and Greater Green
    # River all came out the same brown, which is the thing this was supposed
    # to fix. Measured across 0.01 / 0.1 / 0.25 / 0.5 / 1.0 degrees, the pair
    # count runs 21 / 24 / 29 / 39 / 54 and the Wyoming cluster separates at
    # 0.5. "Near enough to be compared" is the honest meaning here, not
    # "touching".
    bufs = [g.buffer(0.5) if g is not None else None for g in geoms]
    adj = {i: set() for i in range(n)}
    for i in range(n):
        if geoms[i] is None:
            continue
        for j in range(i + 1, n):
            if geoms[j] is None:
                continue
            # A SMALL BUFFER, because "adjacent" in a published boundary set
            # means shared or nearly-shared edge, and two polygons digitised
            # separately rarely touch to the last decimal place.
            try:
                if bufs[i].intersects(geoms[j]):
                    adj[i].add(j)
                    adj[j].add(i)
            except Exception:
                pass
    # Largest-degree first: the constrained polygons get to choose while the
    # palette is still open, which is what keeps a greedy pass from painting
    # itself into a corner.
    # LEAST-USED COLOUR, NOT FIRST AVAILABLE. Taking the first free colour
    # satisfies the neighbour rule and still leaves a lopsided map: with few
    # neighbours it just keeps picking the palette's first entry, so 32 basins
    # came out in 2 colours. Preferring the colour used least so far spreads
    # them evenly -- 8 colours, 4 basins each -- while the neighbour set is
    # still what constrains the choice.
    order = sorted(range(n), key=lambda i: -len(adj[i]))
    from collections import Counter as _Counter
    use = _Counter()
    chosen = {}
    for i in order:
        taken = {chosen[j] for j in adj[i] if j in chosen}
        cand = [c for c in _MAP_PALETTE if c not in taken] or list(_MAP_PALETTE)
        pick = min(cand, key=lambda c: (use[c], _MAP_PALETTE.index(c)))
        chosen[i] = pick
        use[pick] += 1
    clashes = sum(1 for i in range(n) for j in adj[i]
                  if j > i and chosen[i] == chosen[j])
    print("  adjacency colouring: %d polygon(s), %d neighbour pair(s), "
          "%d colour(s), %d clash(es)"
          % (n, sum(len(v) for v in adj.values()) // 2,
             len(set(chosen.values())), clashes))
    return {names[i]: chosen[i] for i in range(n)}


def register(engine, spec, doc, feats, source):
    mn_la, mx_la, mn_lo, mx_lo = bbox(feats)
    nm = spec["layer_name"]
    # STABLE ID FROM THE NAME, so re-running updates the row it wrote last
    # time instead of adding a second copy of the same layer.
    lid = hashlib.sha1(("eia::" + nm).encode("utf-8")).hexdigest()
    st = spec["style"]
    # THE COLOUR IS WRITTEN INTO THE FEATURES, not into the layer row, because
    # dv_spatial_layer holds ONE style and _add_shapefile_layer now reads _c
    # and _fc off a feature when they are there. So the registry keeps saying
    # what the layer's default is, and the exceptions travel with the shapes.
    if spec.get("colour_by"):
        key = spec["colour_by"]
        if spec.get("colour_mode") == "adjacent":
            cmap = _colour_by_adjacency(feats, key)
        else:
            from dataview.mapping.geography_layers import lease_colour_map
            cmap = lease_colour_map(sorted(
                {str((f.get("properties") or {}).get(key) or "")
                 for f in feats}))
        for f in feats:
            pr = f.setdefault("properties", {})
            c = cmap.get(str(pr.get(key) or ""))
            if c:
                pr["_c"], pr["_fc"] = c, c
    params = {
        "id": lid, "nm": nm, "ty": spec["layer_type"],
        "cat": spec["category"], "epsg": 4326,
        "path": spec["url"], "n": len(feats),
        "s": mn_la, "n_": mx_la, "w": mn_lo, "e": mx_lo,
        "gj": json.dumps({"type": "FeatureCollection", "features": feats}),
        "src": source,
        "rem": ("EIA %s, fetched from %s" % (nm, spec["url"]))[:2000],
        "col": st["color"], "wt": st["weight"], "op": st["opacity"],
        "fc": st["fill_color"], "fo": st["fill_opacity"],
        "tip": spec["tooltip"], "ord": spec["order"],
    }
    with engine.begin() as c:
        c.execute(text("DELETE FROM dataview.dv_spatial_layer "
                       "WHERE layer_id = :id OR layer_name = :nm"), params)
        c.execute(text("""
            INSERT INTO dataview.dv_spatial_layer
                (layer_id, layer_name, layer_type, layer_category, epsg_code,
                 file_path, feature_count, bbox_min_lat, bbox_max_lat,
                 bbox_min_lon, bbox_max_lon, active_ind, row_created_by,
                 row_created_date, source, geometry_wkt, source_type,
                 style_color, style_weight, style_opacity, style_fill_color,
                 style_fill_opacity, tooltip_fields, display_order,
                 remark)
            VALUES
                (:id, :nm, :ty, :cat, :epsg, :path, :n, :s, :n_, :w, :e,
                 'Y', 'EIA_LOADER', SYSUTCDATETIME(), :src, :gj, 'GEOJSON',
                 :col, :wt, :op, :fc, :fo, :tip, :ord, :rem)
        """), params)
    return len(feats), (mn_la, mx_la, mn_lo, mx_lo), len(params["gj"])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--only", choices=sorted(SOURCES),
                    help="load just one of them")
    ap.add_argument("--fill-opacity", type=float, default=None,
                    help="override every layer's fill opacity (0-1). "
                         "The basins shipped at 0.08 and read as barely "
                         "coloured on a light basemap.")
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="a code that EXISTS in dv_r_source (default %s)"
                         % DEFAULT_SOURCE)
    ap.add_argument("--list", action="store_true",
                    help="show the registered layers and exit")
    a = ap.parse_args()
    engine = _conn(a.database)

    # APPLIED BEFORE ANYTHING READS A STYLE, so --fill-opacity reaches both
    # the row written to dv_spatial_layer and the per-feature colours drawn
    # from it. Set on every layer rather than one, because the complaint that
    # produced this flag -- "they need to be darker" -- was about the whole
    # geology stack washing out on a light basemap, not one outline.
    if a.fill_opacity is not None:
        if not 0.0 <= a.fill_opacity <= 1.0:
            raise SystemExit("--fill-opacity must be between 0 and 1")
        for _spec in SOURCES.values():
            _spec["style"]["fill_opacity"] = a.fill_opacity

    if a.list:
        with engine.connect() as c:
            for r in c.execute(text(
                    "SELECT layer_name, source_type, feature_count, source "
                    "FROM dataview.dv_spatial_layer ORDER BY display_order, "
                    "layer_name")):
                print("  %-42s %-9s %6s  %s"
                      % (r[0][:42], r[1], r[2], r[3]))
        return

    # CHECKED BEFORE THE FETCH, so a bad code costs a sentence rather than
    # a download followed by a foreign-key number.
    with engine.connect() as c:
        ok = c.execute(text("SELECT 1 FROM dataview.dv_r_source "
                            "WHERE source = :s"), {"s": a.source}).scalar()
    if not ok:
        raise SystemExit(
            "source %r is not registered in dv_r_source. Register it in the "
            "Reference Tables app first, or pass --source with one that is."
            % a.source)

    for key in ([a.only] if a.only else sorted(SOURCES)):
        spec = SOURCES[key]
        print("fetching %s ..." % spec["layer_name"], flush=True)
        doc, feats = fetch(spec["url"])
        n, bb, size = register(engine, spec, doc, feats, a.source)
        print("  registered %d feature(s), %.2f MB inline" % (n, size / 1e6))
        print("  extent %.3f,%.3f .. %.3f,%.3f" % (bb[0], bb[2], bb[1], bb[3]))


if __name__ == "__main__":
    main()
