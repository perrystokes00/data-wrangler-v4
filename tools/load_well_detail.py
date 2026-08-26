r"""Legal location, mud log, aliases -- and anchored shows, pressures, intervals.

REAL WHERE THE DOCUMENTS SAY SO, SYNTHETIC WHERE THEY DO NOT, and the two are
never mixed in one row. Three of these tables are filled from files on the
RMOTC CD; three are generated against facts already in the database. Every row
says which in its source column: PARSED documents get source='MUDLOG' or
'OPERATOR', generated rows get 'SYNTH'.

THE MUD LOG HEADER IS THE FIND. 48X28.LOG is a binary MUD.LOG 4.4b file, but
its header is plain ASCII and carries what nothing else in this dataset does:

    490' FSL 2449' FWL Sec. 28, T39N, R78W
    Lat 43.314785   Long 106.221955
    545'   5760'   Gel - Chem   RMOTC   Mark Milliken

That is the legal location with exact footages, and it is PARSED here rather
than transcribed, so a second CD with a different well needs no edit. Note
T39N: the township was worth reading rather than assuming from the field name.

THE FOOTAGES ARE CONVERTED, NOT COPIED. FSL/FWL become a quarter-quarter by
arithmetic on a 5,280 ft section -- FSL 490 and FWL 2449 land in the SE of the
SW -- and the lab report on the same CD says "SE SW Section 28" independently.
Two sources agreeing is why this is loaded as real.

    python tools/load_well_detail.py                 # plan only
    python tools/load_well_detail.py --apply
    python tools/load_well_detail.py --remove --apply
"""
import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

WELL_NUM = "48-X-28"
BY = "WELL_DETAIL_LOADER"
MUDLOG = os.path.join(
    r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler",
    "training", "Teapot_Dome", "DataSets", "Core", "CD Files", "mudlog",
    "48X28.LOG")
SECTION_FT = 5280.0


def _facts(engine):
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
        # THE PICK IS THE PARENT, so its KEY travels with it. dv_strat_interval
        # has a three-column FK (uwi, strat_unit_id, interp_id) into
        # dv_well_formation_top: an interval is not free-standing, it hangs off
        # a pick somebody actually made. Carrying only (name, depth) here is
        # what made the first version cite "Tensleep A Sandstone" as the
        # strat_unit_id when the real id is "A Sand" -- the display name, not
        # the key. Same shape as the FILE_NAME invariant: right question,
        # wrong key, and it reads perfectly well right up until the FK fires.
        tops = [(str(r[0]), float(r[1]), str(r[2]), r[3]) for r in c.execute(text(
            "SELECT strat_unit_name, top_depth, strat_unit_id, interp_id "
            "FROM dataview.dv_well_formation_top "
            "WHERE uwi = :u AND top_depth IS NOT NULL ORDER BY top_depth"),
            {"u": uwi}).fetchall()]
        plugs = [(float(r[0]), float(r[1]) if r[1] is not None else None,
                  float(r[2]) if r[2] is not None else None)
                 for r in c.execute(text(
                     "SELECT sample_depth, porosity_frac, permeability_air_md "
                     "FROM dataview.dv_well_core_sample WHERE uwi = :u "
                     "AND sample_depth IS NOT NULL"), {"u": uwi}).fetchall()]
        dsts = [dict(zip(("dst_id", "top", "base", "maxp", "finalp", "date",
                          "unit"), r)) for r in c.execute(text(
            "SELECT dst_id, top_depth, base_depth, max_shut_in_pressure, "
            "final_shut_in_pressure, test_date, strat_unit_name "
            "FROM dataview.dv_well_dst WHERE uwi = :u"), {"u": uwi}).fetchall()]
        cores = [(float(r[0]), float(r[1])) for r in c.execute(text(
            "SELECT top_depth, base_depth FROM dataview.dv_well_core "
            "WHERE uwi = :u AND top_depth IS NOT NULL"), {"u": uwi}).fetchall()]
    return {"uwi": uwi, "tops": tops, "plugs": plugs, "dsts": dsts,
            "cores": cores}


def parse_mudlog(path=MUDLOG):
    """The legal location out of the MUD.LOG header. {} if unreadable.

    THE BINARY IS PARSED BY load_mudlog.read_header, NOT HERE. This module
    used to carry its own reader that swept printable runs out of the first
    3000 bytes with regexes, and it was wrong in three ways at once: it
    sorted four separate depth fields and took the min and max (right answer,
    by luck, and it threw away both elevations), and it stripped a trailing
    capital off two strings that was really the next record's tag byte.

    The file is tag/length/value. Reading it by tag is not an improvement on
    the regexes, it is the difference between reading a field and finding a
    number that looks like one -- so there is one reader, and the loader that
    owns dv_well_mud_log owns it.

    Reading the same file from two loaders is fine; WRITING a table from two
    is not. load_mudlog writes the log, this writes the legal location.
    """
    if not os.path.exists(path):
        return {}
    from load_mudlog import read_header, T_LOCATION, T_LATLONG
    hdr = read_header(open(path, "rb").read())
    out = {"file_path": path}
    m = re.search(r"(\d+)'\s*FSL\s+(\d+)'\s*FWL\s*Sec\.?\s*(\d+),?\s*"
                  r"T(\d+)\s*([NS]),?\s*R(\d+)\s*([EW])",
                  hdr.get(T_LOCATION, ""), re.I)
    if m:
        out.update(fsl=float(m.group(1)), fwl=float(m.group(2)),
                   section=int(m.group(3)), township=int(m.group(4)),
                   township_dir=m.group(5).upper(),
                   range_num=int(m.group(6)), range_dir=m.group(7).upper())
    m = re.search(r"Lat\s+([\d.]+)\s+Long\s+(-?[\d.]+)",
                  hdr.get(T_LATLONG, ""))
    if m:
        # Teapot Dome is west of Greenwich; the header omits the sign.
        out.update(lat=float(m.group(1)), lon=-abs(float(m.group(2))))
    return out


def _quarter(fsl, fwl):
    """(quarter_1, quarter_2) from footages -- arithmetic, not a lookup.

    quarter_2 is the quarter section, quarter_1 the quarter of that quarter,
    which is how "SE SW" is read aloud: the SE quarter OF the SW quarter.
    """
    half = SECTION_FT / 2.0
    quarter = SECTION_FT / 4.0
    q2 = ("S" if fsl < half else "N") + ("W" if fwl < half else "E")
    ns = fsl % half
    ew = fwl % half
    q1 = ("S" if ns < quarter else "N") + ("W" if ew < quarter else "E")
    return q1, q2


def build(f, mud):
    uwi, tops, plugs = f["uwi"], f["tops"], f["plugs"]
    out = []

    # -- legal location, from the mud log header ------------------------
    if "section" in mud:
        q1, q2 = _quarter(mud["fsl"], mud["fwl"])
        out.append(("dv_well_legal", {
            "uwi": uwi, "location_type": "SURFACE",
            "section": mud["section"], "township": mud["township"],
            "township_dir": mud["township_dir"], "range_num": mud["range_num"],
            "range_dir": mud["range_dir"], "quarter_1": q1, "quarter_2": q2,
            "footage_1": mud["fsl"], "footage_2": mud["fwl"],
            "principal_meridian": "6TH", "source": "MUDLOG"}))

    # -- aliases: the names this well is actually called ----------------
    for i, (nm, typ) in enumerate([
            ("48-X-28", "OPERATOR"), ("48X28", "OPERATOR"),
            ("48 X 28", "MUDLOG"), ("RMOTC 48-X-28", "COMMON")], 1):
        out.append(("dv_well_alias", {
            "uwi": uwi, "alias_id": "%s-AL%d" % (WELL_NUM, i),
            "alias_name": nm, "alias_type": typ,
            "remark": "spelling seen in the RMOTC source documents",
            "source": "OPERATOR"}))

    # -- pressures: SYNTHETIC, but equal to the DST already loaded ------
    pi = 0
    for d in f["dsts"]:
        for lbl, val in (("SHUT-IN", d.get("maxp")),
                         ("FINAL SHUT-IN", d.get("finalp"))):
            if val is None:
                continue
            pi += 1
            mid = (float(d["top"]) + float(d["base"])) / 2.0
            out.append(("dv_well_pressure", {
                "uwi": uwi, "pressure_id": "%s-PR%d" % (WELL_NUM, pi),
                "pressure_type": lbl, "test_date": d.get("date"),
                "depth": mid, "depth_ouom": "FT", "depth_datum": "KB",
                "pressure": float(val), "pressure_ouom": "PSI",
                "temperature": 118.0, "temperature_ouom": "DEGF",
                "fluid_type": "OIL", "strat_unit_name": d.get("unit"),
                "tool_type": "STRADDLE PACKER",
                "remark": "SYNTHETIC; the same reading as %s, so the two "
                          "tables cannot disagree" % d["dst_id"],
                "source": "SYNTH"}))

    # -- strat intervals: real tops, real plug porosity -----------------
    ii = 0
    for nm, t, sid, iid in tops:
        if "TENSLEEP" not in nm.upper():
            continue
        b = next((d for _n, d, _s, _i in tops if d > t + 0.5), None)
        if b is None:
            continue
        ii += 1
        inside = [p for p in plugs if t <= p[0] <= b]
        phis = [p[1] for p in inside if p[1] is not None]
        ks = [p[2] for p in inside if p[2] is not None]
        sand = "SANDSTONE" in nm.upper()
        out.append(("dv_strat_interval", {
            "uwi": uwi, "interval_id": "%s-SI%02d" % (WELL_NUM, ii),
            # The pick this interval was derived from, by its own key. Not
            # a surrogate: the FK requires all three columns to match a row
            # that exists, so the interval can only ever describe rock
            # somebody logged.
            "strat_unit_id": sid, "interp_id": iid,
            "interval_type": "RESERVOIR" if sand else "SEAL",
            "interval_name": nm, "top_depth": t, "base_depth": b,
            "net_thickness": round((b - t) * (0.65 if sand else 0.15), 1),
            "depth_ouom": "FT",
            "porosity": (sum(phis) / len(phis)) if phis else None,
            "permeability": (sum(ks) / len(ks)) if ks else None,
            "perm_ouom": "MD" if ks else None,
            "water_saturation": 0.38 if sand else 0.72,
            "fluid_type": "OIL" if sand else "WATER",
            "remark": ("porosity and permeability averaged from %d measured "
                       "core plug(s)" % len(inside)) if inside else
                      "no core plug in this interval",
            "source": "SYNTH"}))
    return out


def _write(engine, rows):
    from collections import Counter
    from sqlalchemy import text
    made = Counter()
    with engine.begin() as c:
        # A REFERENCE TABLE IS AN ARMED GUARD, AND THIS LOADER DOES NOT
        # DISARM IT. dv_r_source has an FK from every table written here, so
        # an unregistered source code fails the INSERT -- correctly. The
        # first version of this seeded the missing code itself, which made
        # this a SECOND WRITER to a table the Reference Tables app already
        # owns: that app supplies a curated short_name and long_name
        # (MUDLOG is ML / "Wellsite mudlog"), and a loader inventing its own
        # would either lose to it or quietly overwrite it. So the check
        # stays and the fix is a human decision, named and pointed at the
        # one place that makes it -- which is also the design law: a new
        # coded domain is a decision, not a step.
        _cited = {d["source"] for _t, d in rows if d.get("source")}
        _known = {r[0] for r in c.execute(text(
            "SELECT source FROM dataview.dv_r_source"))}
        _missing = sorted(_cited - _known)
        if _missing:
            raise SystemExit(
                "These source codes are not registered in dv_r_source:"
                "\n    %s\n"
                "Add them in the Reference Tables app (it owns this table "
                "and supplies the short and long names), then re-run."
                % ", ".join(_missing))
        need = {}
        for t, cn in c.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='dataview' AND IS_NULLABLE='NO' "
                "AND COLUMN_DEFAULT IS NULL "
                "AND COLUMN_NAME IN ('active_ind','row_created_date')")):
            need.setdefault(t, set()).add(cn)
        now = datetime.datetime.now()
        for table, d in rows:
            fill = {cn: ("Y" if cn == "active_ind" else now)
                    for cn in need.get(table, ()) if cn not in d}
            if fill:
                d = dict(d, **fill)
            cols = list(d)
            p = dict(d)
            p["__by"] = BY
            c.execute(text(
                "INSERT INTO dataview.[%s] (%s, row_created_by) VALUES (%s, :__by)"
                % (table, ", ".join("[%s]" % x for x in cols),
                   ", ".join(":%s" % x for x in cols))), p)
            made[table] += 1
    return made


def _remove(engine, uwi):
    from collections import Counter
    from sqlalchemy import text
    gone = Counter()
    with engine.begin() as c:
        # dv_well_shows and dv_well_mud_log are NOT here: load_mudlog.py
        # owns them now, and a --remove that deleted another loader's
        # tables would undo work this run never did.
        for t in ["dv_well_pressure", "dv_strat_interval",
                  "dv_well_alias", "dv_well_legal"]:
            gone[t] = c.execute(text(
                "DELETE FROM dataview.[%s] WHERE uwi = :u AND row_created_by = :b"
                % t), {"u": uwi, "b": BY}).rowcount
    return gone


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Legal, mud log, aliases, shows, pressures, intervals.")
    ap.add_argument("--database", default="DataView_Demo")
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

    mud = parse_mudlog()
    if not mud:
        print("No mud log at %s -- the legal location will be skipped."
              % MUDLOG)
    else:
        # Only what this loader actually reads. The logged interval, mud
        # type and mud logger are load_mudlog's to report; printing them
        # here from a dict that no longer carries them showed
        # "MUD.LOG ?" and "None - None ft", which reads as a parse
        # failure rather than a field that moved.
        print("Legal location, from the MUD.LOG header (read by tag)")
        if "section" in mud:
            q1, q2 = _quarter(mud["fsl"], mud["fwl"])
            print("  legal        : %s%s Sec %d T%d%s R%d%s"
                  "  (%.0f' FSL, %.0f' FWL)"
                  % (q1, q2, mud["section"], mud["township"],
                     mud["township_dir"], mud["range_num"], mud["range_dir"],
                     mud["fsl"], mud["fwl"]))
        if "lat" in mud:
            print("  header coords: %.6f, %.6f" % (mud["lat"], mud["lon"]))
        print("  the mud log itself: python tools/load_mudlog.py")
    rows = build(f, mud)
    from collections import Counter
    n = Counter(t for t, _d in rows)
    print()
    for t in sorted(n):
        real = ("real" if t in ("dv_well_legal", "dv_well_alias")
                else "synthetic")
        print("   %-24s %5d   %s" % (t, n[t], real))
    print("   %-24s %5d" % ("TOTAL", sum(n.values())))
    if not a.apply:
        print("\nPLAN ONLY -- nothing written. Re-run with --apply.")
        return 0
    made = _write(engine, rows)
    print("\nWrote %d row(s) across %d table(s)."
          % (sum(made.values()), len(made)))
    print("Undo with:  python tools/load_well_detail.py --remove --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
