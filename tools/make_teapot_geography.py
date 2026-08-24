r"""Load the Teapot Dome field outline, leases, reserve boundary and pipelines.

These four layers are REFERENCE GEOMETRY -- map furniture. They do not come
from documents and there is nothing to prove by routing them through the
document pipeline; the shapefile path exists for that when a real GIS export
turns up. What matters is that they agree with the model everything else was
built from: the field polygon is a contour of the reservoir horizon, so the
productive outline IS the dome's structural closure, and the wells inside it
are the ones the production model already made good.

    python tools/make_teapot_geography.py            # counts only
    python tools/make_teapot_geography.py --apply
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

from dataview.migration.synth_field import Surfaces, plan_field   # noqa: E402
from dataview.migration.synth_geography import (                  # noqa: E402
    CREATED_BY, OPERATOR, field_outline, gathering_system,
    reserve_boundary, section_grid, wkt_line, wkt_polygon)

TABLES = ("dv_field", "dv_land_tract", "dv_boundary", "dv_pipeline")


def _geog(wkt):
    """A geography literal that is REORIENTED IF IT SWALLOWED THE PLANET.

    A ring wound the wrong way is not rejected by SQL Server -- it is read as
    the complement, so the polygon becomes the earth minus the field. The tell
    is an area over 10^13 m2, and ReorientObject is the fix. Checked here
    rather than trusted, because nothing downstream would notice.
    """
    return ("CASE WHEN geography::STGeomFromText(:wkt, 4326).MakeValid()"
            ".STArea() > 1e13 "
            "THEN geography::STGeomFromText(:wkt, 4326).MakeValid().ReorientObject() "
            "ELSE geography::STGeomFromText(:wkt, 4326).MakeValid() END")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Load Teapot Dome geography layers. Counts only unless --apply.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--level", type=float, default=0.50,
                    help="field outline contour, as a fraction of the relief")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    import sqlalchemy as sa
    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    print("Building the geography from the structural model ...")
    S = Surfaces()
    wells = plan_field(surfaces=S)
    outline, level = field_outline(S, level_frac=a.level)
    if not outline:
        print("REFUSED: the reservoir horizon has no closing contour at "
              f"level_frac={a.level}. Try a lower value.")
        return 2
    bnd = reserve_boundary()
    secs = section_grid(bnd)
    pipes = gathering_system(wells)

    print(f"   field outline   {len(outline):4,} vertices, "
          f"closing contour at {level:,.0f} ms")
    print(f"   reserve boundary{len(bnd):4,} corners")
    print(f"   lease sections  {len(secs):4,}")
    print(f"   pipelines       {len(pipes):4,} "
          f"({sum(1 for p in pipes if p[0].startswith('Flowline'))} flowlines)")

    if not a.apply:
        print("\nCOUNTS ONLY -- nothing written. Re-run with --apply.")
        return 0

    with engine.begin() as cx:
        for t in TABLES:
            n = cx.execute(sa.text(
                f"DELETE FROM dataview.{t} WHERE row_created_by = :cb"),
                {"cb": CREATED_BY}).rowcount
            if n:
                print(f"   replaced {n:,} existing row(s) in {t}")

        cx.execute(sa.text(f"""
            INSERT INTO dataview.dv_field
              (field_id, field_name, field_type, country, province_state,
               county, basin_name, field_status,
               onshore_offshore_ind, surface_latitude, surface_longitude,
               active_ind, remark, row_created_by, source, geog)
            VALUES ('TEAPOT_DOME', 'Teapot Dome (NPR-3)', 'OIL', 'US', 'WY',
                    'NATRONA', 'Powder River Basin', 'PRODUCING', 'ONSHORE',
                    :la, :lo, 'Y', :rk, :cb, 'SYNTH', {_geog(':wkt')})"""),
            {"wkt": wkt_polygon(outline),
             "la": 43.290, "lo": -106.212, "cb": CREATED_BY,
             "rk": (f"Synthetic. Outline is the {level:,.0f} ms closing contour "
                    f"of the Tensleep horizon, so it is the structure's own "
                    f"closure rather than a drawn boundary.")})

        cx.execute(sa.text(f"""
            INSERT INTO dataview.dv_boundary
              (boundary_id, boundary_name, boundary_type, province_state,
               country, area_km2, active_ind, source, row_created_by, geog)
            VALUES ('NPR3_RESERVE', 'Naval Petroleum Reserve No. 3',
                    'FEDERAL RESERVE', 'WY', 'US', :km, 'Y', 'SYNTH', :cb,
                    {_geog(':wkt')})"""),
            {"wkt": wkt_polygon(bnd), "km": 38.4, "cb": CREATED_BY})

        for num, ring in secs:
            cx.execute(sa.text(f"""
                INSERT INTO dataview.dv_land_tract
                  (land_tract_id, tract_name, lease_number, operator_name,
                   province_state, country, area_km2, active_ind, source,
                   row_created_by, geog)
                VALUES (:id, :nm, :ln, :op, 'WY', 'US', 2.59, 'Y', 'SYNTH', :cb,
                        {_geog(':wkt')})"""),
                {"id": f"NPR3_SEC_{num:03d}", "nm": f"NPR-3 Section {num}",
                 "ln": f"WYW-{160000 + num}", "op": OPERATOR,
                 "wkt": wkt_polygon(ring), "cb": CREATED_BY})

        for i, (nm, pts) in enumerate(pipes, start=1):
            cx.execute(sa.text(f"""
                INSERT INTO dataview.dv_pipeline
                  (pipeline_id, pipeline_name, operator_name, commodity,
                   province_state, country, active_ind, source,
                   row_created_by, geog)
                VALUES (:id, :nm, :op, :cm, 'WY', 'US', 'Y', 'SYNTH', :cb,
                        {_geog(':wkt')})"""),
                {"id": f"NPR3_PL_{i:03d}", "nm": nm[:255], "op": OPERATOR,
                 "cm": "OIL" if nm.startswith("Flowline") else "CRUDE",
                 "wkt": wkt_line(pts), "cb": CREATED_BY})

    with engine.connect() as cx:
        print()
        for t in TABLES:
            n = cx.execute(sa.text(
                f"SELECT COUNT(*) FROM dataview.{t} WHERE row_created_by=:cb"),
                {"cb": CREATED_BY}).scalar()
            bad = cx.execute(sa.text(
                f"SELECT COUNT(*) FROM dataview.{t} "
                f"WHERE row_created_by=:cb AND (geog IS NULL "
                f"  OR geog.STIsValid()=0 OR geog.STArea() > 1e13)"),
                {"cb": CREATED_BY}).scalar()
            print(f"   {t:16s} {n:5,} row(s)" +
                  (f"   {bad} WITH BAD GEOMETRY" if bad else "   all valid"))
        km = cx.execute(sa.text(
            "SELECT SUM(geog.STLength())/1000.0 FROM dataview.dv_pipeline "
            "WHERE row_created_by=:cb"), {"cb": CREATED_BY}).scalar()
        km2 = cx.execute(sa.text(
            "SELECT geog.STArea()/1e6 FROM dataview.dv_field "
            "WHERE field_id='TEAPOT_DOME'")).scalar()
        print(f"\n   field outline area  {float(km2 or 0):,.1f} km2")
        print(f"   pipeline length     {float(km or 0):,.1f} km")
    print("\nLoaded. Tick Fields / Leases / Boundaries / Pipelines on the map.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
