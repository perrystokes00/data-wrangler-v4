r"""Generate a synthetic 2D SEG-Y grid over Teapot Dome.

WHY SEG-Y AND NOT NAVIGATION FILES. A nav-only survey is HELD by
promote_catalog._TIED -- on purpose, because navigation with no seismic behind
it describes nothing -- so nav lines never reach dv_seis_line and never draw on
the map. P190 also reduces a multi-line file to ONE bbox today rather than
per-line geometry. Lines on a map therefore mean SEG-Y files, one per line.

WHAT MAKES THE GRID COHERENT. One dome is modelled in ground coordinates and
every line samples that surface, so lines over the crest close, flank lines
dip, and two crossing lines agree where they cross. Per-line random structure
would look right in isolation and fall apart the moment anything was tied.

The names follow the corpus convention -- <LINE>_<STAGE>_<PRODUCT>_Stack_<n>S
-- because that is where processing type actually lives (there is no
processing column anywhere in the schema), and it is what the map's seismic
filters read.

    python tools/make_teapot_2d.py                    # dry run: the plan, no files
    python tools/make_teapot_2d.py --apply
    python tools/make_teapot_2d.py --apply --count 200
"""
import argparse
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dataview.migration.synth_seismic import Dome, write_line    # noqa: E402

DEFAULT_DIR = (r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
               r"\data_wrangler\training\Teapot_Dome\DataSets\Seismic"
               r"\CD files\2D_Seismic\Synthetic_2D_segy")

# Teapot Dome. The 3D survey covers 43.238-43.342 N, -106.251 to -106.173 W and
# the 1,372 Natrona wells spread a little wider; the grid is laid over the
# wells' extent so lines and wells share a map.
CREST_LON, CREST_LAT = -106.212, 43.290
AREA = dict(min_lat=43.205, max_lat=43.455, min_lon=-106.345, max_lon=-106.135)
EPSG = 32613                       # WGS84 / UTM 13N -- Wyoming, metres

# Vintages, each a survey with its own acquisition azimuth. Real 2D coverage
# accumulates in campaigns like this, which is also why one line has several
# processing versions and its neighbour has none.
VINTAGES = [
    ("NPR-3 TEAPOT DOME 1977 2D", "TPD77", 78.0),
    ("NPR-3 TEAPOT DOME 1979 2D", "TPD79", 168.0),
    ("NPR-3 TEAPOT DOME 1982 2D", "TPD82", 45.0),
    ("NPR-3 TEAPOT DOME 1987 2D", "TPD87", 120.0),
    ("NPR-3 TEAPOT DOME 1996 2D", "TPD96", 15.0),
]

# The vocabulary the corpus already uses, and that _seis_stage reads back.
STAGES = ["POSTM", "PRESTM", "PRESDM", "PRE_MIG"]
PRODUCTS = ["RAW", "PROCESSED"]


def _variants(rng, n):
    """n distinct <stage, product> pairs for one physical line."""
    all_pairs = [(s, p) for s in STAGES for p in PRODUCTS
                 if not (s == "PRE_MIG" and p == "PROCESSED")]
    rng.shuffle(all_pairs)
    return all_pairs[:n]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Synthetic 2D SEG-Y over Teapot Dome. Dry run unless --apply.")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--count", type=int, default=1000,
                    help="total FILES to write (lines x processing variants)")
    ap.add_argument("--variants", type=int, default=4,
                    help="processing versions per physical line")
    ap.add_argument("--traces", type=int, default=150)
    ap.add_argument("--samples", type=int, default=500)
    ap.add_argument("--spacing", type=float, default=50.0,
                    help="trace spacing in metres")
    ap.add_argument("--seed", type=int, default=4317)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    try:
        from pyproj import Transformer
    except ImportError:
        print("REFUSED: pyproj is needed to place the grid in UTM.")
        return 2

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{EPSG}", always_xy=True)
    cx, cy = to_utm.transform(CREST_LON, CREST_LAT)
    x0, y0 = to_utm.transform(AREA["min_lon"], AREA["min_lat"])
    x1, y1 = to_utm.transform(AREA["max_lon"], AREA["max_lat"])

    rng = random.Random(a.seed)
    dome = Dome(cx, cy, relief_ms=145, radius_m=3900,
                horizons_ms=[360, 505, 660, 830], rng=rng)

    n_lines = max(1, a.count // max(1, a.variants))
    per_vintage = max(1, n_lines // len(VINTAGES))
    line_m = a.traces * a.spacing

    print(f"Teapot Dome synthetic 2D grid")
    print(f"  crest        {CREST_LAT} / {CREST_LON}  (EPSG {EPSG})")
    print(f"  area         {x1 - x0:,.0f} x {y1 - y0:,.0f} m")
    print(f"  {n_lines} physical line(s) x {a.variants} processing version(s) "
          f"= {n_lines * a.variants} files")
    print(f"  each         {a.traces} traces x {a.samples} samples, "
          f"{a.spacing:.0f} m spacing -> {line_m / 1000:.2f} km, "
          f"~{(a.traces * (240 + a.samples * 4) + 3600) / 1024:,.0f} KB")
    print(f"  total        ~{n_lines * a.variants * (a.traces * (240 + a.samples * 4) + 3600) / 1048576:,.0f} MB")
    print(f"  into         {a.dir}\n")

    if not a.apply:
        for sv, code, az in VINTAGES:
            print(f"   {code}  {sv:34s} azimuth {az:5.1f}  "
                  f"{per_vintage} line(s) x {a.variants}")
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0

    os.makedirs(a.dir, exist_ok=True)
    t0 = time.perf_counter()
    made = 0
    for sv, code, az_deg in VINTAGES:
        for li in range(per_vintage):
            if made >= n_lines * a.variants:
                break
            # A campaign's lines share an azimuth, with the small variation any
            # real acquisition has.
            az = math.radians(az_deg + rng.gauss(0, 1.6))
            # Centre anywhere in the area, inset so the line stays inside it.
            cxx = rng.uniform(x0 + line_m / 2, x1 - line_m / 2)
            my = rng.uniform(y0 + line_m / 2, y1 - line_m / 2)
            half = (a.traces - 1) / 2.0
            xs = [cxx + (i - half) * a.spacing * math.cos(az)
                  for i in range(a.traces)]
            ys = [my + (i - half) * a.spacing * math.sin(az)
                  for i in range(a.traces)]
            line_id = f"{code}-{li + 1:03d}"
            for stage, product in _variants(rng, a.variants):
                if made >= n_lines * a.variants:
                    break
                secs = a.samples * 2 // 1000
                name = f"{line_id}_{stage}_{product}_Stack_{secs}S.segy"
                write_line(os.path.join(a.dir, name), xs, ys, dome, rng,
                           n_samples=a.samples, dt_us=2000, epsg=EPSG,
                           survey=sv, line_name=name, line_no=li + 1)
                made += 1
            if (made % 200) == 0:
                print(f"   {made:,} files  ({time.perf_counter() - t0:,.0f}s)")

    dt = time.perf_counter() - t0
    total = sum(os.path.getsize(os.path.join(a.dir, f))
                for f in os.listdir(a.dir) if f.lower().endswith(".segy"))
    print(f"\n{made:,} file(s) in {dt:,.0f}s -> {total / 1048576:,.0f} MB")
    print(f"   {a.dir}")
    print("\nScan that folder to catalogue them. Each file is one 2D line, so "
          "each becomes one dv_seis_line LINESTRING on the map.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
