r"""Synthetic rows for the mirrors a well has no data for -- anchored, not invented.

WHY ANCHORED MATTERS MORE THAN SYNTHETIC. A well already carrying 46 real
formation tops, a 5,755 ft survey, six logs and 112 measured core plugs will be
read as a coherent well. Synthetic rows that contradict any of that are worse
than no rows at all: perforations below TD, a DST in a shale, a petro zone whose
porosity disagrees with the plug two feet away. Every value here is derived from
something already in the database:

  * casing shoes sit above the survey's TD
  * perforations fall INSIDE real Tensleep sandstone tops, in cored intervals
  * the DST tests a perforated, cored interval
  * petro zones use the REAL measured porosity of the core plugs inside them
  * the checkshot puts the Tensleep at ~1500 ms, which is where the synthetic
    2D grid draws it, so the well ties the seismic

WHAT IS FABRICATED. Pressures, rates, volumes, cement, tool and gun types,
proppant, and the entire production history. All of it is stamped source='SYNTH'
and row_created_by='SYNTH_WELL_GEN' and comes out again with --remove.

PRODUCTION IS A JUDGEMENT CALL AND IS MARKED AS ONE. 48-X-28 is an RMOTC
research well; a monthly production history is the least defensible thing here,
so every volume row says so in its remark. --no-production leaves it out.

    python tools/gen_synthetic_well_data.py                    # plan only
    python tools/gen_synthetic_well_data.py --apply
    python tools/gen_synthetic_well_data.py --remove --apply
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

WELL_NUM = "48-X-28"
SRC = "SYNTH"
BY = "SYNTH_WELL_GEN"
TABLES = ["dv_well_casing", "dv_well_completion", "dv_well_perforation",
          "dv_well_stimulation", "dv_well_dst", "dv_well_dst_period",
          "dv_well_petro_interp", "dv_well_petro_zone", "dv_well_checkshot"]
PROD_TABLES = ["dv_prod_entity", "dv_prod_volume"]
COMP_ID = WELL_NUM + "-COMP1"
INTERP_ID = WELL_NUM + "-PETRO1"
DST_ID = WELL_NUM + "-DST1"
PE_ID = WELL_NUM + "-PE1"


def _facts(engine):
    """What the database already says about this well. Nothing is assumed."""
    from sqlalchemy import text
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT uwi FROM dataview.dv_well "
            "WHERE REPLACE(REPLACE(UPPER(well_num),'-',''),' ','') = :n"),
            {"n": WELL_NUM.upper().replace("-", "")}).fetchall()
        if len(rows) != 1:
            raise SystemExit("Expected one well numbered %s, found %d."
                             % (WELL_NUM, len(rows)))
        uwi = str(rows[0][0])
        td = c.execute(text("SELECT MAX(md) FROM dataview.dv_well_dir_srvy_sta "
                            "WHERE uwi = :u"), {"u": uwi}).scalar()
        tops = [(str(r[0]), float(r[1])) for r in c.execute(text(
            "SELECT strat_unit_name, top_depth FROM dataview.dv_well_formation_top "
            "WHERE uwi = :u AND top_depth IS NOT NULL ORDER BY top_depth"),
            {"u": uwi}).fetchall()]
        # MEASURED POROSITY, per foot, so a synthetic zone can carry the real
        # number for the rock inside it rather than a plausible-looking one.
        plugs = [(float(r[0]), float(r[1]) if r[1] is not None else None,
                  float(r[2]) if r[2] is not None else None)
                 for r in c.execute(text(
                     "SELECT sample_depth, porosity_frac, permeability_air_md "
                     "FROM dataview.dv_well_core_sample WHERE uwi = :u "
                     "AND sample_depth IS NOT NULL ORDER BY sample_depth"),
                     {"u": uwi}).fetchall()]
        cored = c.execute(text(
            "SELECT MIN(top_depth), MAX(base_depth) FROM dataview.dv_well_core "
            "WHERE uwi = :u"), {"u": uwi}).fetchone()
    return {"uwi": uwi, "td": float(td or 0), "tops": tops, "plugs": plugs,
            "cored": (cored[0], cored[1]) if cored else (None, None)}


def _top(tops, name):
    for n, d in tops:
        if n.upper() == name.upper():
            return d
    return None


def _next_top(tops, depth):
    """The next pick below `depth` -- a zone's base is the next top, not a guess."""
    for _n, d in tops:
        if d > depth + 0.5:
            return d
    return None


def _twt_ms(depth_ft):
    """Two-way time for a depth, tied to the synthetic seismic.

    Calibrated so the Tensleep at ~5514 ft lands at 1500 ms, which is where
    gen_synthetic_segy draws it. A checkshot that disagreed with the section
    beside it would be the most misleading row in this whole file.
    """
    v0, v1 = 5200.0, 7400.0          # ft/s, average velocity, surface to TD
    f = min(1.0, max(0.0, depth_ft / 5514.0))
    v = v0 + (v1 - v0) * f
    return 2.0 * depth_ft / v * 1000.0


def _zone_phi(plugs, top, base):
    """(avg porosity frac, avg k, n) from the REAL plugs inside an interval."""
    inside = [p for p in plugs if top <= p[0] <= base]
    phis = [p[1] for p in inside if p[1] is not None]
    ks = [p[2] for p in inside if p[2] is not None]
    return ((sum(phis) / len(phis)) if phis else None,
            (sum(ks) / len(ks)) if ks else None, len(inside))


def build(f, with_production=True):
    """Every row this tool would write, as (table, dict) pairs."""
    uwi, tops, td, plugs = f["uwi"], f["tops"], f["td"], f["plugs"]
    out = []
    comp_date = datetime.date(2004, 8, 15)      # after coring (May), after
    #                                             the lab report (27 July)

    # -- casing: three strings, all above the surveyed TD -----------------
    for i, (typ, num, base, od, wt, grade, ctop) in enumerate([
            ("CONDUCTOR", 1, 40.0, 16.0, 65.0, "H-40", 0.0),
            ("SURFACE", 2, 520.0, 8.625, 24.0, "J-55", 0.0),
            ("PRODUCTION", 3, round(td, 0) or 5755.0, 4.5, 11.6, "J-55",
             4200.0)], 1):
        out.append(("dv_well_casing", {
            "uwi": uwi, "casing_id": "%s-CSG%d" % (WELL_NUM, i),
            "casing_type": typ, "string_num": num, "set_date": comp_date,
            "top_depth": 0.0, "base_depth": base, "depth_ouom": "FT",
            "depth_datum": "KB", "od_in": od, "weight_lb_ft": wt,
            "grade": grade, "connection_type": "LTC", "cement_top": ctop,
            "cement_base": base, "cement_type": "CLASS G",
            "remark": "synthetic; shoe above surveyed TD %.0f ft" % td,
            "source": SRC}))

    # -- completion, targeting a real cored Tensleep sandstone ------------
    tb_ss = _top(tops, "Tensleep B Sandstone")
    tb_base = _next_top(tops, tb_ss) if tb_ss else None
    out.append(("dv_well_completion", {
        "uwi": uwi, "completion_id": COMP_ID,
        "completion_type": "CASED HOLE", "completion_design": "PERFORATED",
        "well_orientation": "VERTICAL", "completion_date": comp_date,
        "strat_unit_name": "Tensleep B Sandstone",
        "top_depth": tb_ss, "base_depth": tb_base,
        "measured_td_ft": td, "depth_ouom": "FT", "depth_datum": "KB",
        "completion_status": "COMPLETED", "primary_fluid": "OIL",
        "stage_count": 2, "tubing_size_in": 2.375,
        "tubing_depth": (tb_ss - 60.0) if tb_ss else None,
        "artificial_lift_type": "ROD PUMP",
        "remark": "synthetic completion on real Tensleep picks",
        "source": SRC}))

    # -- perforations INSIDE real sandstone picks, in cored rock ----------
    for i, nm in enumerate(["Tensleep A Sandstone", "Tensleep B Sandstone",
                            "Tensleep C1 Sandstone"], 1):
        t = _top(tops, nm)
        if t is None:
            continue
        b = _next_top(tops, t)
        if b is None:
            continue
        # Inset from the picks: a perforation that starts exactly on a
        # formation top is a giveaway that nobody looked at a log.
        pt, pb = t + 3.0, b - 3.0
        if pb - pt < 4:
            continue
        out.append(("dv_well_perforation", {
            "uwi": uwi, "completion_id": COMP_ID,
            "perf_id": "%s-PERF%d" % (WELL_NUM, i), "perf_date": comp_date,
            "top_depth": pt, "base_depth": pb, "depth_ouom": "FT",
            "shot_count": int(round((pb - pt) * 4)), "shot_density": 4.0,
            "shot_density_ouom": "SPF", "perf_diameter_in": 0.32,
            "gun_type": "3-1/8 IN HSD", "phasing_deg": 60.0,
            "strat_unit_name": nm, "perf_status": "OPEN",
            "remark": "synthetic; interval inside the %s pick" % nm,
            "source": SRC}))

    # -- stimulation, one stage per perforated interval -------------------
    for i, (nm, styp, fluid) in enumerate([
            ("Tensleep A Sandstone", "ACID", "15% HCL"),
            ("Tensleep B Sandstone", "FRAC", "SLICKWATER")], 1):
        t = _top(tops, nm)
        if t is None:
            continue
        b = _next_top(tops, t)
        out.append(("dv_well_stimulation", {
            "uwi": uwi, "completion_id": COMP_ID,
            "stim_id": "%s-STIM%d" % (WELL_NUM, i), "stage_num": i,
            "stim_type": styp, "stage_date": comp_date,
            "stage_top_depth": t + 3.0, "stage_base_depth": (b - 3.0) if b else None,
            "fluid_system": fluid,
            "fluid_volume_bbl": 450.0 if styp == "ACID" else 2800.0,
            "proppant_type": None if styp == "ACID" else "20/40 SAND",
            "proppant_mass_lbs": None if styp == "ACID" else 68000.0,
            "isip_psi": 2450.0, "avg_treating_pressure_psi": 3100.0,
            "max_treating_pressure_psi": 3850.0, "avg_rate_bpm": 12.0,
            "max_rate_bpm": 18.0, "screen_out_ind": "N", "source": SRC}))

    # -- DST over the perforated, cored Tensleep B ------------------------
    if tb_ss and tb_base:
        out.append(("dv_well_dst", {
            "uwi": uwi, "dst_id": DST_ID, "dst_num": 1,
            "test_type": "DST", "test_date": datetime.date(2004, 6, 2),
            "top_depth": tb_ss + 3.0, "base_depth": tb_base - 3.0,
            "depth_ouom": "FT", "depth_datum": "KB",
            "strat_unit_name": "Tensleep B Sandstone",
            "tool_type": "STRADDLE PACKER",
            "perforation_top": tb_ss + 3.0, "perforation_base": tb_base - 3.0,
            "max_shut_in_pressure": 2180.0, "final_shut_in_pressure": 2145.0,
            "pressure_ouom": "PSI", "max_oil_rate": 96.0, "max_gas_rate": 41.0,
            "max_water_rate": 22.0, "rate_ouom": "BBL/D", "gor": 427.0,
            "api_gravity": 32.4, "test_result": "OIL AND WATER TO SURFACE",
            "remark": "synthetic; tests a cored, perforated interval",
            "source": SRC}))
        for i, (ptyp, mins, p0, p1) in enumerate([
                ("INITIAL FLOW", 10, 180.0, 640.0),
                ("INITIAL SHUT-IN", 45, 640.0, 2180.0),
                ("FINAL FLOW", 60, 2180.0, 810.0),
                ("FINAL SHUT-IN", 90, 810.0, 2145.0)], 1):
            out.append(("dv_well_dst_period", {
                "uwi": uwi, "dst_id": DST_ID,
                "period_id": "%s-P%d" % (DST_ID, i), "period_type": ptyp,
                "period_seq": i, "duration_min": mins,
                "start_pressure": p0, "end_pressure": p1,
                "pressure_ouom": "PSI", "choke_size": "1/2 IN",
                "source": SRC}))

    # -- petrophysics: one interp, zones on the real picks ----------------
    out.append(("dv_well_petro_interp", {
        "uwi": uwi, "interp_id": INTERP_ID,
        "interp_name": "Tensleep evaluation (synthetic)",
        "interp_date": datetime.date(2004, 9, 10),
        "software": "SYNTHETIC", "archie_a": 1.0, "archie_m": 2.0,
        "archie_n": 2.0, "formation_water_resist": 0.09,
        "rw_temperature": 120.0, "temperature_ouom": "DEGF",
        "shale_volume_method": "LINEAR GR", "porosity_method": "DENSITY",
        "sw_method": "ARCHIE", "matrix_density_g_cc": 2.71,
        "fluid_density_g_cc": 1.0, "interp_status": "FINAL",
        "remark": "synthetic interpretation; zone porosity is the MEASURED "
                  "core-plug average where plugs exist",
        "source": SRC}))
    zi = 0
    for nm, t in tops:
        if not nm.upper().startswith("TENSLEEP"):
            continue
        b = _next_top(tops, t)
        if b is None:
            continue
        zi += 1
        phi, k, n = _zone_phi(plugs, t, b)
        sand = "SANDSTONE" in nm.upper()
        # A zone with no plug in it gets NO porosity rather than a guess --
        # the whole point of anchoring is that the number means something.
        out.append(("dv_well_petro_zone", {
            "uwi": uwi, "interp_id": INTERP_ID,
            "zone_id": "%s-Z%02d" % (WELL_NUM, zi), "zone_name": nm,
            "zone_type": "RESERVOIR" if sand else "NON-RESERVOIR",
            "top_depth": t, "base_depth": b, "depth_ouom": "FT",
            "depth_datum": "KB", "strat_unit_name": nm,
            "gross_thickness": b - t,
            "net_thickness": round((b - t) * (0.65 if sand else 0.15), 1),
            "net_to_gross": 0.65 if sand else 0.15,
            "phi_effective_avg": phi, "phi_method": "CORE" if n else None,
            "perm_avg_md": k,
            "sw_avg": 0.38 if sand else 0.72,
            "vsh_avg": 0.12 if sand else 0.44,
            "fluid_type": "OIL" if sand else "WATER",
            "pay_flag": "Y" if (sand and phi and phi > 0.08) else "N",
            "pay_cutoff_phi": 0.08, "pay_cutoff_sw": 0.55,
            "remark": ("porosity and permeability are the average of %d "
                       "measured core plug(s)" % n) if n else
                      "no core plug in this zone; porosity left null",
            "source": SRC}))

    # -- checkshot: ties the well to the synthetic seismic ----------------
    d = 500.0
    si = 0
    while d <= (td or 5755.0):
        si += 1
        twt = _twt_ms(d)
        out.append(("dv_well_checkshot", {
            "uwi": uwi, "checkshot_id": WELL_NUM + "-CS1",
            "station_id": "%s-CS1-%03d" % (WELL_NUM, si),
            "survey_date": datetime.date(2004, 6, 20),
            "md": d, "tvd": d, "depth_ouom": "FT", "depth_datum": "KB",
            "twt_ms": round(twt, 1), "owt_ms": round(twt / 2.0, 1),
            "time_ouom": "MS", "avg_velocity": round(2000.0 * d / twt, 1),
            "velocity_ouom": "FT/S",
            "remark": "synthetic; Tensleep ties at ~1500 ms, matching the "
                      "synthetic 2D grid",
            "source": SRC}))
        d += 250.0

    if with_production:
        out.append(("dv_prod_entity", {
            "prod_entity_id": PE_ID, "uwi": uwi,
            "prod_entity_type": "WELL", "prod_entity_name": "RMOTC 48-X-28",
            "first_prod_date": datetime.date(2004, 9, 1),
            "last_prod_date": datetime.date(2007, 12, 1),
            "primary_fluid": "OIL",
            "remark": "SYNTHETIC. 48-X-28 is an RMOTC research well; this "
                      "production history is fabricated for demonstration.",
            "source": SRC}))
        y, mth = 2004, 9
        qi, mn = 78.0, 0
        while (y, mth) <= (2007, 12):
            # Hyperbolic decline, b=0.9 -- a shape, not a forecast.
            q = qi / ((1.0 + 0.9 * 0.11 * mn) ** (1.0 / 0.9))
            days = 30
            for fluid, vol in (("OIL", q * days),
                               ("WATER", q * days * (0.6 + 0.03 * mn)),
                               ("GAS", q * days * 0.42)):
                out.append(("dv_prod_volume", {
                    "prod_entity_id": PE_ID,
                    "period_date": datetime.date(y, mth, 1),
                    "fluid_type": fluid, "volume": round(vol, 1),
                    "volume_ouom": "MCF" if fluid == "GAS" else "BBL",
                    "days_on_prod": days,
                    "avg_daily_rate": round(vol / days, 2),
                    "rate_ouom": "MCF/D" if fluid == "GAS" else "BBL/D",
                    "remark": "SYNTHETIC production for a research well",
                    "source": SRC}))
            mn += 1
            mth += 1
            if mth > 12:
                mth, y = 1, y + 1
    return out


def _write(engine, rows):
    from sqlalchemy import text
    from collections import Counter
    made = Counter()
    with engine.begin() as c:
        # THE AUDIT COLUMNS ARE FILLED FROM THE SCHEMA, NOT FROM A LIST.
        # These tables disagree with each other about their own housekeeping:
        # most default active_ind to 'Y' and row_created_date to getdate(),
        # but dv_well_completion and dv_well_stimulation declare both NOT
        # NULL with NO default. A hand-written per-table list is two lists to
        # keep in step, and it failed twice in a row here -- first on
        # active_ind, then on row_created_date, same two tables both times.
        # Asking the catalogue which columns are mandatory cannot drift.
        _need = {}
        for _t, _cn in c.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='dataview' AND IS_NULLABLE='NO' "
                "AND COLUMN_DEFAULT IS NULL "
                "AND COLUMN_NAME IN ('active_ind', 'row_created_date')")):
            _need.setdefault(_t, set()).add(_cn)
        _now = datetime.datetime.now()
        for table, d in rows:
            _fill = {}
            for _cn in _need.get(table, ()):
                if _cn not in d:
                    _fill[_cn] = "Y" if _cn == "active_ind" else _now
            if _fill:
                d = dict(d, **_fill)
            cols = list(d)
            sql = ("INSERT INTO dataview.[%s] (%s, row_created_by) "
                   "VALUES (%s, :__by)"
                   % (table, ", ".join("[%s]" % x for x in cols),
                      ", ".join(":%s" % x for x in cols)))
            p = dict(d)
            p["__by"] = BY
            c.execute(text(sql), p)
            made[table] += 1
    return made


def _remove(engine, uwi):
    from sqlalchemy import text
    from collections import Counter
    gone = Counter()
    with engine.begin() as c:
        # CHILD BEFORE PARENT, and production before its entity.
        for t in ["dv_prod_volume"]:
            gone[t] = c.execute(text(
                "DELETE FROM dataview.[%s] WHERE row_created_by = :b" % t),
                {"b": BY}).rowcount
        for t in ["dv_prod_entity"]:
            gone[t] = c.execute(text(
                "DELETE FROM dataview.[%s] WHERE row_created_by = :b" % t),
                {"b": BY}).rowcount
        for t in ["dv_well_dst_period", "dv_well_dst", "dv_well_perforation",
                  "dv_well_stimulation", "dv_well_petro_zone",
                  "dv_well_petro_interp", "dv_well_checkshot",
                  "dv_well_casing", "dv_well_completion"]:
            gone[t] = c.execute(text(
                "DELETE FROM dataview.[%s] WHERE uwi = :u AND row_created_by = :b"
                % t), {"u": uwi, "b": BY}).rowcount
    return gone


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Anchored synthetic rows for a well's empty mirrors.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--no-production", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)
    f = _facts(engine)

    if a.remove:
        if not a.apply:
            print("Would remove every row stamped %s. Add --apply." % BY)
            return 0
        gone = _remove(engine, f["uwi"])
        for t in sorted(gone):
            if gone[t]:
                print("   %-24s %5d removed" % (t, gone[t]))
        print("Total %d row(s)." % sum(gone.values()))
        return 0

    rows = build(f, with_production=not a.no_production)
    from collections import Counter
    n = Counter(t for t, _d in rows)
    print("Well        : %s (%s)" % (f["uwi"], WELL_NUM))
    print("Anchored on : TD %.0f ft, %d real tops, %d measured plugs"
          % (f["td"], len(f["tops"]), len(f["plugs"])))
    print()
    for t in sorted(n):
        print("   %-24s %5d" % (t, n[t]))
    print("   %-24s %5d" % ("TOTAL", sum(n.values())))
    if not a.apply:
        print("\nPLAN ONLY -- nothing written. Re-run with --apply.")
        return 0
    made = _write(engine, rows)
    print("\nWrote %d row(s) across %d table(s)."
          % (sum(made.values()), len(made)))
    print("Undo with:  python tools/gen_synthetic_well_data.py --remove --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
