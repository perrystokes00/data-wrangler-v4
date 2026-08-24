r"""Create the horizon tables and load the Teapot Dome horizons into them.

The horizons come from the SAME structural model as the Teapot 2D SEG-Y
(synth_seismic.teapot_model), so a pick sits on its reflector by construction.
That is the property worth protecting: a horizon that floats off its reflector
looks like an interpretation, plots, exports, and is wrong everywhere at once.

    python tools/make_teapot_horizons.py                 # dry run
    python tools/make_teapot_horizons.py --apply
    python tools/make_teapot_horizons.py --apply --create   # (re)create tables

--create DROPS AND REBUILDS the three horizon tables. It is a separate flag
from --apply because dropping a table is a decision, not a step -- and these
tables are the only home of an interpretation once one is loaded.
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

from dataview.migration.synth_horizons import (                  # noqa: E402
    CREATED_BY, teapot_horizons)

DDL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "sql", "create_dv_seis_horizon.sql")
TABLES = ("dv_seis_horizon", "dv_seis_horizon_grid",
          "dv_seis_horizon_contour")


def _exists(cx, name):
    import sqlalchemy as sa
    return cx.execute(sa.text(
        "SELECT OBJECT_ID('dataview." + name + "', 'U')")).scalar() is not None


def _create(engine):
    """Run the DDL, batch by batch on GO -- pyodbc has no batch separator."""
    import sqlalchemy as sa
    with open(DDL, encoding="utf-8") as fh:
        script = fh.read()
    batches = [b.strip() for b in script.replace("\r\n", "\n").split("\nGO")
               if b.strip()]
    with engine.begin() as cx:
        for b in batches:
            cx.execute(sa.text(b))
    return len(batches)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Load Teapot Dome horizons. Dry run unless --apply.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--rows", type=int, default=90, help="grid rows")
    ap.add_argument("--cols", type=int, default=70, help="grid columns")
    ap.add_argument("--step", type=float, default=10.0,
                    help="contour interval in ms")
    ap.add_argument("--create", action="store_true",
                    help="DROP and recreate the three horizon tables first")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    import sqlalchemy as sa
    engine = make_engine(a.database)

    print("Building the horizons from the seismic model ...")
    hz = teapot_horizons(nrow=a.rows, ncol=a.cols, contour_step=a.step)
    for meta, (lats, lons, vals), segs in hz:
        print(f"   {meta['horizon_id']}  {meta['horizon_name']:26s} "
              f"{meta['min_value']:7.1f}-{meta['max_value']:7.1f} ms   "
              f"grid {vals.shape[0]}x{vals.shape[1]}   "
              f"{len(segs)} contour(s)")
    n_grid = sum(v[2].size for _m, v, _s in hz)
    n_cont = sum(len(s) for _m, _v, s in hz)
    print(f"\n   {len(hz)} horizon(s), {n_grid:,} grid node(s), "
          f"{n_cont:,} contour(s)")

    with engine.connect() as cx:
        have = {t: _exists(cx, t) for t in TABLES}
    missing = [t for t, ok in have.items() if not ok]
    if missing and not a.create:
        print("\nREFUSED: these tables do not exist -- " + ", ".join(missing))
        print("  Re-run with --create to build them from")
        print("  " + DDL)
        return 2

    if not a.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply"
              + (" --create" if missing else "") + ".")
        return 0

    if a.create:
        print(f"\nCreating tables from {os.path.basename(DDL)} ...")
        print(f"   {_create(engine)} batch(es) run")

    ins_h = sa.text("""
        INSERT INTO dataview.dv_seis_horizon
          (horizon_id, horizon_name, horizon_type, strat_unit_name, seq_no,
           pick_domain, pick_uom, min_value, max_value,
           bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon,
           display_colour, interpreter, active_ind, remark,
           row_created_by, source)
        VALUES (:horizon_id, :horizon_name, :horizon_type, :strat_unit_name,
                :seq_no, :pick_domain, :pick_uom, :min_value, :max_value,
                :bbox_min_lat, :bbox_max_lat, :bbox_min_lon, :bbox_max_lon,
                :display_colour, :interpreter, 'Y', :remark, :cb, :cb)""")
    ins_g = sa.text("""
        INSERT INTO dataview.dv_seis_horizon_grid
          (horizon_id, row_no, col_no, latitude, longitude, value,
           active_ind, row_created_by)
        VALUES (:h, :r, :c, :la, :lo, :v, 'Y', :cb)""")
    ins_c = sa.text("""
        INSERT INTO dataview.dv_seis_horizon_contour
          (horizon_id, contour_id, contour_value, n_points, geog,
           active_ind, row_created_by)
        VALUES (:h, :cid, :val, :n,
                geography::STGeomFromText(:wkt, 4326).MakeValid(),
                'Y', :cb)""")

    with engine.begin() as cx:
        for t in ("dv_seis_horizon_contour", "dv_seis_horizon_grid",
                  "dv_seis_horizon"):
            cx.execute(sa.text("DELETE FROM dataview." + t
                               + " WHERE row_created_by = :cb"), {"cb": CREATED_BY})
        for meta, (lats, lons, vals), segs in hz:
            cx.execute(ins_h, {**meta, "cb": CREATED_BY})
            grid_rows = [
                {"h": meta["horizon_id"], "r": int(i), "c": int(j),
                 "la": float(lats[i]), "lo": float(lons[j]),
                 "v": float(vals[i, j]), "cb": CREATED_BY}
                for i in range(vals.shape[0]) for j in range(vals.shape[1])]
            cx.execute(ins_g, grid_rows)
            crows = []
            for k, (value, pts) in enumerate(segs):
                # LON LAT ORDER. geography 4326 takes longitude first, and
                # swapping them puts Wyoming in the Indian Ocean -- silently,
                # because both numbers are valid coordinates.
                wkt = "LINESTRING(" + ", ".join(
                    f"{lo:.7f} {la:.7f}" for la, lo in pts) + ")"
                crows.append({"h": meta["horizon_id"],
                              "cid": f"{meta['horizon_id']}_C{k:04d}",
                              "val": float(value), "n": len(pts),
                              "wkt": wkt, "cb": CREATED_BY})
            for _cr in crows:
                cx.execute(ins_c, _cr)

    with engine.connect() as cx:
        for t in TABLES:
            n = cx.execute(sa.text(
                "SELECT COUNT(*) FROM dataview." + t
                + " WHERE row_created_by = :cb"), {"cb": CREATED_BY}).scalar()
            print(f"   {t:30s} {n:,} row(s)")
        bad = cx.execute(sa.text("""
            SELECT COUNT(*) FROM dataview.dv_seis_horizon_contour
             WHERE geog IS NULL OR geog.STIsValid() = 0""")).scalar()
        print(f"\n   contours with no or invalid geometry: {bad}")
        if bad:
            print("   ^ those will not draw; the map reads geog directly.")
    print("\nLoaded. The map reads the contours; a section overlay samples the "
          "grid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
