"""Build and seed dataview.dv_r_log_mnemonic -- the curve catalogue.

    python tools/seed_log_mnemonics.py                 # plan only
    python tools/seed_log_mnemonics.py --apply
    python tools/seed_log_mnemonics.py --rescan --apply # pick up new mnemonics
    python tools/seed_log_mnemonics.py --misses         # what is unclassified

WHAT THIS IS FOR
----------------
A log plot has to know, for every mnemonic it meets: which family it belongs
to, which track it belongs in, what scale to draw it on, whether that scale is
logarithmic or reversed, and what colour it usually is. Those are conventions,
not data -- resistivity is drawn 0.2-2000 logarithmic because four decades of
useful range cannot be read linearly, and neutron is drawn right-to-left
against density so the two cross where lithology and porosity disagree.

Holding that in a Python list means Perry cannot change it, cannot see it, and
cannot add a mnemonic without a code edit. Holding it in a dv_r_* table means
the Standards Manager picks it up automatically (it enumerates every dv_r_*
table in the schema) and it is editable in the app like any other vocabulary.

THIS TABLE HAS NO FOREIGN KEY, AND THAT IS DELIBERATE.
------------------------------------------------------
promote_catalog._parent_fk_predicates walks sys.foreign_keys and HOLDS any row
whose value in a column pointing at a dv_r_* table is not registered there.
That is right for a controlled vocabulary -- an unregistered well status should
park the row rather than invent one. It would be exactly wrong here: this is a
DISPLAY catalogue, and a curve nobody has catalogued yet must still load and
still plot, just without a preferred scale. Adding an FK from
dv_well_log_curve.mnemonic would turn "we have not described this tool yet"
into "this log will not load", which is a far worse failure than a curve drawn
on an autoscale.

So: no FK, no guard, and `classify()` still answers for mnemonics the table has
never seen.

SEEDED FROM WHAT THE CATALOGUE ACTUALLY HOLDS. The patterns in
dataview/file_catalog/curve_families.py classify; this writes the answer per
mnemonic along with its scale. Run --rescan after loading new logs and it adds
whatever turned up, marking anything it cannot classify so the gap is visible
rather than silent.
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataview.file_catalog.curve_families import (classify, FAMILY_STYLE,
                                                  TRACK_TEMPLATE, FAMILIES)

TABLE = "dv_r_log_mnemonic"
BY = "LOG_MNEMONIC_SEED"

DDL = """
CREATE TABLE dataview.dv_r_log_mnemonic (
    mnemonic          nvarchar(40)  NOT NULL,
    family            nvarchar(20)  NULL,
    family_label      nvarchar(60)  NULL,
    description       nvarchar(160) NULL,
    unit              nvarchar(20)  NULL,
    -- SCALE_MIN GREATER THAN SCALE_MAX MEANS THE TRACK IS REVERSED. That is
    -- how neutron, density-porosity and Sw are conventionally drawn, and
    -- storing it as an ordered pair keeps one column instead of a second
    -- boolean that can disagree with it.
    scale_min         float         NULL,
    scale_max         float         NULL,
    log_scale         char(1)       NULL,
    track_hint        int           NULL,
    colour            nvarchar(12)  NULL,
    line_style        nvarchar(12)  NULL,
    remark            nvarchar(300) NULL,
    active_ind        char(1)       NOT NULL CONSTRAINT DF_dv_r_log_mnem_act DEFAULT ('Y'),
    row_created_by    nvarchar(60)  NOT NULL CONSTRAINT DF_dv_r_log_mnem_by  DEFAULT ('SYSTEM'),
    row_created_date  datetime      NOT NULL CONSTRAINT DF_dv_r_log_mnem_dt  DEFAULT (getdate()),
    row_changed_by    nvarchar(60)  NULL,
    row_changed_date  datetime      NULL,
    CONSTRAINT PK_dv_r_log_mnemonic PRIMARY KEY (mnemonic)
)
"""

# The mnemonics worth carrying whether or not this database has met them yet,
# so a LAS from a modern tool string is described on arrival rather than after
# someone notices the plot is wrong.
EXTRA = [
    "GR", "SGR", "CGR", "ECGR", "HGR",
    "SP", "SSP",
    "CALI", "CAL", "HCAL", "BS",
    "RT", "LLD", "LLS", "MSFL", "SFLU", "ILD", "ILM", "SN",
    "AT10", "AT20", "AT30", "AT60", "AT90",
    "NPHI", "TNPH", "PHIN", "RHOB", "RHOZ", "DRHO", "PEF", "PE",
    "DPHI", "PHIE", "PHIT", "SW", "SWE", "BVW",
    "DT", "DTCO", "DTSM",
]


def _track_of(family):
    for num, _title, fams, _scale, _log in TRACK_TEMPLATE:
        if family in fams:
            return num
    return None


def _row(m):
    fam, label = classify(m)
    if fam in (None, "DEPTH"):
        return {"mnemonic": m, "family": None, "family_label": None,
                "description": None, "unit": None,
                "scale_min": None, "scale_max": None, "log_scale": None,
                "track_hint": None, "colour": None, "line_style": None,
                "remark": ("depth index" if fam == "DEPTH"
                           else "not classified -- set family and scale to "
                                "put this curve on a track")}
    st = FAMILY_STYLE.get(fam, {})
    sc = st.get("scale")
    track = _track_of(fam)
    is_log = "Y" if (track == 3) else "N"
    return {"mnemonic": m, "family": fam, "family_label": label,
            "description": label, "unit": st.get("unit"),
            "scale_min": sc[0] if sc else None,
            "scale_max": sc[1] if sc else None,
            "log_scale": is_log, "track_hint": track,
            "colour": st.get("colour"),
            "line_style": "dash" if st.get("dash") else "solid",
            "remark": None}


def _exists(c):
    from sqlalchemy import text
    return bool(c.execute(text(
        "SELECT OBJECT_ID('dataview.%s')" % TABLE)).scalar())


def catalog_mnemonics(engine):
    from sqlalchemy import text
    with engine.connect() as c:
        if not c.execute(text(
                "SELECT OBJECT_ID('dataview.dv_well_log_curve')")).scalar():
            return []
        return [r[0] for r in c.execute(text(
            "SELECT DISTINCT mnemonic FROM dataview.dv_well_log_curve "
            "WHERE mnemonic IS NOT NULL")) if r[0] and r[0].strip()]


def main(argv=None):
    from sqlalchemy import text
    from dataview.core.dw_utils import make_engine

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rescan", action="store_true",
                    help="add mnemonics that have appeared since last time")
    ap.add_argument("--misses", action="store_true",
                    help="just report what cannot be classified")
    a = ap.parse_args(argv)
    engine = make_engine(a.database)

    seen = catalog_mnemonics(engine)
    want = sorted({m.strip().upper() for m in list(seen) + EXTRA})

    if a.misses:
        bad = [m for m in want if classify(m)[0] is None]
        print("%d of %d mnemonic(s) unclassified" % (len(bad), len(want)))
        for m in bad:
            print("   %s" % m)
        return 0

    with engine.begin() as c:
        fresh = not _exists(c)
        if fresh:
            if not a.apply:
                print("Would CREATE dataview.%s and seed %d mnemonic(s)."
                      % (TABLE, len(want)))
                print("Add --apply.")
                return 0
            c.execute(text(DDL))
            print("Created dataview.%s" % TABLE)

        have = set()
        if not fresh:
            have = {r[0] for r in c.execute(text(
                "SELECT mnemonic FROM dataview.%s" % TABLE))}

        todo = [m for m in want if m not in have]
        if not todo:
            print("Nothing to add: %d mnemonic(s) already catalogued." % len(have))
            return 0
        if not a.apply:
            print("Would add %d mnemonic(s) to dataview.%s (%d already there)."
                  % (len(todo), TABLE, len(have)))
            for m in todo[:20]:
                r = _row(m)
                print("   %-10s %-10s %s"
                      % (m, r["family"] or "-",
                         ("%s .. %s %s" % (r["scale_min"], r["scale_max"],
                                           r["unit"] or ""))
                         if r["scale_min"] is not None else "(no scale)"))
            if len(todo) > 20:
                print("   ... and %d more" % (len(todo) - 20))
            print("Add --apply.")
            return 0

        cols = ("mnemonic", "family", "family_label", "description", "unit",
                "scale_min", "scale_max", "log_scale", "track_hint",
                "colour", "line_style", "remark")
        sql = text(
            "INSERT INTO dataview.%s (%s, row_created_by) VALUES (%s, :by)"
            % (TABLE, ", ".join(cols), ", ".join(":" + x for x in cols)))
        n_ok = n_miss = 0
        for m in todo:
            r = _row(m)
            r["by"] = BY
            c.execute(sql, r)
            if r["family"]:
                n_ok += 1
            else:
                n_miss += 1
        print("Added %d mnemonic(s): %d classified, %d left for review."
              % (len(todo), n_ok, n_miss))

    with engine.connect() as c:
        tot = c.execute(text("SELECT COUNT(*) FROM dataview.%s" % TABLE)).scalar()
        miss = c.execute(text(
            "SELECT COUNT(*) FROM dataview.%s WHERE family IS NULL" % TABLE)).scalar()
        print("dataview.%s now holds %d mnemonic(s); %d need a family."
              % (TABLE, tot, miss))
        print("Edit them in the app: Reference Tables (it lists every dv_r_* "
              "table automatically).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
