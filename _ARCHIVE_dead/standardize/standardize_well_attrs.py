"""
standardize_well_attrs.py
Rules-based standardizer for WELL_MASTER's raw WELL_TYPE / WELL_STATUS into
canonical codes. Reads BOTH raw columns because they cross-contaminate
(type values carry status, status values carry type). For every distinct raw
value it derives a std_type and/or std_status via ordered pattern rules, with a
confidence and the reason. Unmatched -> REVIEW, and a coverage report (weighted
by row counts) shows how much of the table each pass covers.

    python standardize_well_attrs.py --database WELL_REF        # report only
    python standardize_well_attrs.py --out crosswalk.csv        # write crosswalk

Output crosswalk.csv columns:
    kind (type|status), raw_value, rows, std_type, std_status, confidence, reason
"""
import argparse
import re
import urllib.parse

# ── Canonical sets (RATIONALIZED — dups/contamination removed) ───────────────
STATUS = {"ACTIVE","PRODUCING","SHUT_IN","SUSPENDED","PLUGGED_AND_ABANDONED",
          "DRY_HOLE","DRILLING","PERMITTED","LOCATION","CANCELLED","INJECTING",
          "MONITORING","COMPLETED","ABANDONED","UNKNOWN"}
TYPE   = {"OIL","GAS","OIL_GAS","WATER_SOURCE","DISPOSAL","INJECT",
          "CLASS_II_INJECTION","CBM","GAS_STORAGE","STORAGE","MONITORING",
          "OBSERVATION","SERVICE","CORE_HOLE","STRATIGRAPHIC_TEST","VERTICAL",
          "HORIZONTAL","DIRECTIONAL","EXPLORATORY","DEVELOPMENT","GEOTHERMAL",
          "GAS_INJECTION","OTHER","UNKNOWN"}

def _u(s): return (s or "").strip().upper()

# ── Misfiled-value sets: a type word in the status column (or vice-versa)
#    means THAT field's value is genuinely unknown; the info belongs to the
#    other axis and the builder recovers it by running both classifiers on
#    both raw columns. ─────────────────────────────────────────────────────
TYPE_WORDS_IN_STATUS = {"WATER","OIL","GAS","COAL","BRINE","CONDENSATE","O&G",
    "OIL & GAS","OILGS","OG","GW","OW","CBM","MINERAL"}
STATUS_WORDS_IN_TYPE = {"NEW_DRILL","DEAD PERMIT","RECOMPLETION","RE_ENTER",
    "DRY","DRY HOLE","DRY & ABANDONED","DA","D","DEAD","UNSUCCESSFUL"}


def classify_status(raw):
    s = _u(raw)
    if s in ("", "NULL"): return ("UNKNOWN", "low", "blank")
    if s in TYPE_WORDS_IN_STATUS:
        return ("UNKNOWN", "xfield", "type word in status col")
    rules = [
      (lambda x: "P&A" in x or "PLUG" in x or re.search(r"\bPA\b", x)
                 or "PLUGGED AND ABANDONED" in x
                 or x in ("DAP","DAOP","DAOGP","DAGP","INJWP","SWDP","GASP",
                          "WATERP","OILWIP","CBMP") or x.startswith("PA"),
                 "PLUGGED_AND_ABANDONED", "high", "plugged"),
      (lambda x: "D&A" in x or "DRY" in x or re.search(r"\bDA\b", x)
                 or x.startswith("DAO") or "DRY AND ABANDONED" in x
                 or x == "UNSUCCESSFUL", "DRY_HOLE", "high", "dry hole"),
      (lambda x: "TEMP" in x or re.search(r"\bTA\b", x) or "SUSPEND" in x
                 or "IDLE" in x or "INACTIVE" in x or "IN-ACTIVE" in x
                 or re.search(r"\bIA\b", x), "SUSPENDED", "med", "suspended"),
      (lambda x: "SHUT" in x or re.search(r"\bSI\b", x) or "SIFORDER" in x,
                 "SHUT_IN", "high", "shut-in"),
      (lambda x: "INJECT" in x or "SWD" in x or "DISPOSAL" in x
                 or "WATERFLOOD" in x or "STEAMFLOOD" in x or "EOR" in x,
                 "INJECTING", "med", "injection"),
      (lambda x: "MONIT" in x or "OBSERV" in x or x.startswith("OBS"),
                 "MONITORING", "med", "monitor"),
      (lambda x: "DRILL" in x or "SPUD" in x or x == "DRL", "DRILLING", "med", "drilling"),
      (lambda x: "PERMIT" in x or "APD" in x or x in ("NEW","NEW PERMIT"),
                 "PERMITTED", "med", "permit"),
      (lambda x: "LOCATION" in x or re.search(r"\bLOC\b", x), "LOCATION", "med", "location"),
      (lambda x: "CANCEL" in x or "DENIED" in x or "WITHDRAW" in x
                 or "EXPIRED" in x or "REVOK" in x or "DEAD" in x
                 or "NEVER" in x or "NOT DRILLED" in x or x in ("EX","ND"),
                 "CANCELLED", "med", "cancelled"),
      (lambda x: "PRODUC" in x or re.search(r"\bPR\b", x) or "OILP" in x,
                 "PRODUCING", "med", "producing"),
      (lambda x: "ACTIVE" in x or re.search(r"\bAC\b", x), "ACTIVE", "med", "active"),
      (lambda x: "COMPLET" in x, "COMPLETED", "med", "completed"),
      (lambda x: "ABANDON" in x, "ABANDONED", "med", "abandoned"),
      (lambda x: "UNKNOWN" in x or x == "UNK" or "CONFIDENTIAL" in x
                 or x.startswith("OTHER"), "UNKNOWN", "low", "unknown"),
    ]
    for test, code, conf, why in rules:
        try:
            if test(s): return (code, conf, why)
        except Exception:
            pass
    return ("REVIEW", "review",
            "numeric source code" if s.isdigit() else "unmatched")


def classify_type(raw):
    s = _u(raw)
    if s in ("", "NULL"): return ("UNKNOWN", "low", "blank")
    if s in STATUS_WORDS_IN_TYPE or "DRY AND ABANDONED" in s \
       or "JUNKED AND ABANDONED" in s or "TEMPORARILY ABANDONED" in s:
        return ("UNKNOWN", "xfield", "status word in type col")
    rules = [
      (lambda x: "OIL" in x and "GAS" in x, "OIL_GAS", "high", "oil&gas"),
      (lambda x: x in ("OG","O&G") or "OIL & GAS" in x or "OIL AND GAS" in x,
                 "OIL_GAS", "high", "oil&gas"),
      (lambda x: "CLASS II" in x or "CLASS 2" in x, "CLASS_II_INJECTION", "high", "classII"),
      (lambda x: "DISPOSAL" in x or "SWD" in x or "SALT WATER DISP" in x,
                 "DISPOSAL", "high", "disposal"),
      (lambda x: "INJECT" in x or x == "IW" or "FLOOD" in x or "STEAM" in x,
                 "INJECT", "med", "injection"),
      (lambda x: "WATER" in x, "WATER_SOURCE", "high", "water"),
      (lambda x: "COAL" in x or x == "CBM" or "COALBED" in x or "COAL BED" in x,
                 "CBM", "med", "coal"),
      (lambda x: "STRAT" in x, "STRATIGRAPHIC_TEST", "high", "strat"),
      (lambda x: "CORE" in x, "CORE_HOLE", "high", "core"),
      (lambda x: "STORAGE" in x or x == "GSW", "GAS_STORAGE", "med", "storage"),
      (lambda x: "MONIT" in x, "MONITORING", "high", "monitor"),
      (lambda x: "OBSERV" in x or x.startswith("OBS"), "OBSERVATION", "high", "obs"),
      (lambda x: "GEOTHERM" in x, "GEOTHERMAL", "high", "geo"),
      (lambda x: "HORIZONTAL" in x, "HORIZONTAL", "high", "traj"),
      (lambda x: "VERTICAL" in x, "VERTICAL", "high", "traj"),
      (lambda x: "DIRECTIONAL" in x or "SLANT" in x or "RADIUS" in x,
                 "DIRECTIONAL", "med", "traj"),
      (lambda x: "EXPLOR" in x or "WILDCAT" in x or "OUTPOST" in x or x == "NPW",
                 "EXPLORATORY", "med", "explor"),
      (lambda x: "DEVELOP" in x or x == "DEV", "DEVELOPMENT", "med", "dev"),
      (lambda x: "ENGINEER" in x or "SERVICE" in x or "SUPPLY" in x
                 or ("TEST" in x and "STRAT" not in x), "SERVICE", "low", "service/test"),
      (lambda x: x in ("GAS","G","GW") or "GAS WELL" in x or "GAS PRODUCER" in x,
                 "GAS", "med", "gas"),
      (lambda x: x in ("OIL","O") or "OIL WELL" in x or "OIL PRODUCER" in x,
                 "OIL", "med", "oil"),
      (lambda x: "MINE" in x or "MINERAL" in x or "DRIFT" in x or "SLOPE" in x
                 or "STRIP" in x, "OTHER", "low", "mining"),
      (lambda x: "UNKNOWN" in x or x.startswith("OTHER") or "NOT AVAIL" in x,
                 "UNKNOWN", "low", "unknown"),
    ]
    for test, code, conf, why in rules:
        try:
            if test(s): return (code, conf, why)
        except Exception:
            pass
    return ("REVIEW", "review",
            "cryptic source code" if (len(s) <= 3 or s.isdigit()) else "unmatched")


def _engine(server, database):
    from sqlalchemy import create_engine
    odbc = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
            f"DATABASE={database};Trusted_Connection=yes")
    return create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server",   default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="WELL_REF")
    ap.add_argument("--schema-table", default="well_ref.WELL_MASTER")
    ap.add_argument("--out", default=None, help="write crosswalk CSV here")
    args = ap.parse_args()

    import csv, sys
    from sqlalchemy import text
    eng = _engine(args.server, args.database)
    rows = []
    with eng.begin() as c:
        for kind, col in (("type", "WELL_TYPE"), ("status", "WELL_STATUS")):
            q = text(f"SELECT {col} v, COUNT(*) n FROM {args.schema_table} "
                     f"GROUP BY {col}")
            for v, n in c.execute(q):
                if kind == "type":
                    code, conf, why = classify_type(v)
                    rows.append((kind, v, int(n), code, "", conf, why))
                else:
                    code, conf, why = classify_status(v)
                    rows.append((kind, v, int(n), "", code, conf, why))

    # coverage weighted by rows
    for kind in ("type", "status"):
        sub = [r for r in rows if r[0] == kind]
        tot = sum(r[2] for r in sub)
        rev = sum(r[2] for r in sub if r[5] == "review")
        print(f"{kind}: {len(sub)} distinct values, {tot:,} rows | "
              f"mapped {100*(tot-rev)/max(tot,1):.1f}%  review {rev:,} rows")
        worst = sorted([r for r in sub if r[5] == "review"],
                       key=lambda r: -r[2])[:15]
        for r in worst:
            print(f"    REVIEW  {r[1]!r:35} {r[2]:>8,}")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["kind","raw_value","rows","std_type","std_status","confidence","reason"])
            w.writerows(sorted(rows, key=lambda r: (r[0], -r[2])))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
