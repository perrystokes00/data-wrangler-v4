"""
reload_wy_master.py — rebuild Wyoming in WELL_REF.well_ref.well_master_gold
from the two WOGCC source files. PREVIEW by default; --apply writes.

    python reload_wy_master.py --dir "C:\\...\\data_by_state\\Wyoming"
    python reload_wy_master.py --dir <dir> --apply
    python reload_wy_master.py --dir <dir> --apply --include-permits

WHAT WAS WRONG, AND WHY IT WAS INVISIBLE
----------------------------------------
Two faults, both in how Wyoming was loaded into the master, and one reload
fixes both.

1 · THE WRONG KEY COLUMN. WOGCC publishes the API twice:

       APINO  = 105001          county+well as a NUMBER, leading zeros gone
       CAPINO = 49-001-05001    the canonical, complete API

   The loader took APINO. `105001` right-padded to 10 is `1050010000`, which
   is exactly what the master stores — so all 67,229 Wyoming rows carry a key
   that no join can reach. It stayed hidden because the result LOOKED like a
   key: right length, right shape, and the only symptom was the enrich stage
   reporting "resolvable UWIs: 0", which reads as absent data rather than an
   unusable key.

   THE RULE: an identifier read as a number stops being an identifier. CAPINO
   is read as TEXT here and never cast.

2 · THE WRONG FILE. The folder holds TWO datasets:

       050526PA    67,277 rows   STATUS=PA          plugged and abandoned
       050526WH   142,929 rows   PG/PO/SI/WD/EP…    everything else

   The master's 67,229 rows match PA. So the shipped reference contains
   Wyoming's plugged-and-abandoned wells ONLY — every producing well in the
   state is missing. They are disjoint sets, so both are needed.

THE PERMIT QUESTION — READ THIS BEFORE --apply
----------------------------------------------
73,944 rows of WH carry STATUS='EP', 52% of the file. In WOGCC's coding that
is an EXPIRED PERMIT: a well that was permitted and never drilled.

They are EXCLUDED by default. This reference exists to resolve UWIs found in
documents and to backfill coordinates — and a scout ticket matching a hole
that was never drilled is a wrong answer delivered confidently, which is the
one failure this system is built to avoid. `--include-permits` keeps them,
flagged `uwi_suspect = 1` so a consumer can still tell them apart.

If EP means something else in WOGCC's coding, change _NON_WELL_STATUS below —
that constant is the whole of the decision.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

STATE = "WY"
SOURCE = "WY_WOGCC"

# NOTHING IS EXCLUDED BY STATUS (corrected Aug 10).
#
# This held {"EP"} on my reading that it meant an expired permit — a well
# never drilled. THE DATA SAYS OTHERWISE: 49-025-06391 and four others are
# STATUS=EP on NPR 3 with total depths of 570-840 ft and real coordinates, and
# LAS files exist for them. They were drilled and logged. Excluding them
# dropped 73,943 wells, five of which the File Catalog then could not place.
#
# The status is carried through to raw_well_status, so anyone who wants to
# filter on it can — from the source's own word, not from my reading of it.
# A loader should not decide what a regulator's code means.
_NON_WELL_STATUS = set()

# Statuses that mean the well is abandoned, so STATUSDATE dates the abandonment.
_ABANDONED = {"PA", "TA"}

_DIGITS = re.compile(r"\D")


def sniff(path: str) -> str:
    """Delimiter from the CONTENT, never from the extension.

    050526WH is TAB-delimited and named .txt; renaming it .csv to open it in
    Excel does not make it comma-separated. A comma reader collapses its whole
    header into one mangled column and every field after it is wrong.
    """
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        head = fh.readline()
    return "\t" if head.count("\t") > head.count(",") else ","


def num(v):
    """WOGCC numerics arrive as '6103.00000' — and elevations as '6920 TS',
    a number with a survey qualifier stuck to it. Take the LEADING number and
    ignore the rest, rather than returning None and losing a real elevation.
    Empty and 0 both mean 'not stated' in this source."""
    if v is None:
        return None
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(v))
    if not m:
        return None
    f = float(m.group(1))
    return None if f == 0 else f


def ymd(v):
    """Dates are YYYYMMDD integers; 0 and 19000101 mean 'not stated'."""
    s = _DIGITS.sub("", str(v or ""))
    if len(s) != 8 or s.startswith("0000"):
        return None
    y, m, d = s[:4], s[4:6], s[6:]
    if not ("1900" < y <= "2100") or not ("01" <= m <= "12") or not ("01" <= d <= "31"):
        return None
    return f"{y}-{m}-{d}"


def read_file(path: str, log=print):
    """Rows keyed by CAPINO. BY COLUMN NAME — the two files have the same 52
    columns in DIFFERENT ORDER, so a positional reader would silently take the
    wrong field for every row."""
    out, bad_key = {}, 0
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=sniff(path)):
            cap = _DIGITS.sub("", r.get("CAPINO") or "")
            if len(cap) != 10:
                bad_key += 1
                continue
            out[cap] = r
    log(f"  {os.path.basename(path):<18} {len(out):>7,} row(s)"
        + (f"  ({bad_key:,} with an unusable CAPINO)" if bad_key else ""))
    return out


def to_master(cap: str, r: dict) -> dict:
    lat, lon = num(r.get("LAT")), num(r.get("LON"))
    return {
        "uwi14": cap + "0000",
        "api_10": cap,
        "well_name": (r.get("UNIT_LEASE") or "").strip() or None,
        "well_num": (r.get("WN") or "").strip() or None,
        "operator_name": (r.get("COMPANY") or "").strip() or None,
        "field_name": (r.get("FIELD_NAME") or "").strip() or None,
        "surface_latitude": lat,
        "surface_longitude": lon,
        "county": (r.get("COUNTYTXT") or "").strip().title() or None,
        "province_state": STATE,
        "country": "US",
        "raw_well_type": (r.get("WELL_CLASS") or "").strip() or None,
        "raw_well_status": (r.get("STATUS") or "").strip() or None,
        "total_depth": num(r.get("TD")),
        "spud_date": ymd(r.get("FIRSTSPUD")),
        # A missing coordinate is flagged, not invented. 0/0 is the Gulf of
        # Guinea and the classic way an un-georeferenced well ends up plotted.
        # ── the attributes the master used to drop ──────────────────────
        # Each is here because dv_well can RECEIVE it and WOGCC STATES it —
        # a column failing either test would be clutter or dead weight.
        "kb_elevation": num(r.get("ELEVKB")),
        "ground_elevation": num(r.get("ELEV")),
        "elevation_ouom": "FT" if (num(r.get("ELEVKB")) or num(r.get("ELEV"))) else None,
        "completion_date": ymd(r.get("FIRSTCOMP")),
        # STATUSDATE is the date of WHATEVER the current status is; it is an
        # abandonment date only when the well is abandoned. Reading it as one
        # regardless would date every producing well's abandonment.
        "abandonment_date": (ymd(r.get("STATUSDATE"))
                             if (r.get("STATUS") or "").strip().upper() in _ABANDONED
                             else None),
        "bottom_hole_latitude": num(r.get("BLAT")),
        "bottom_hole_longitude": num(r.get("BLON")),
        "formation_at_td": (r.get("BOTFORM") or "").strip() or None,
        "producing_formation": (r.get("RN") or "").strip() or None,
        "lease_name": (r.get("UNIT_LEASE") or "").strip() or None,
        # HORIZ_DIR is a Y/N flag, not a profile vocabulary. Only the positive
        # case is stated — 'N' means "not horizontal", which is NOT the same as
        # "vertical" (it covers directional and deviated too), so N maps to
        # nothing rather than to a guess.
        "well_profile_type": ("HORIZONTAL"
                              if (r.get("HORIZ_DIR") or "").strip().upper() in ("Y", "H")
                              else None),
        "long_lat_source": SOURCE if (lat is not None and lon is not None) else None,
        "coord_suspect": 1 if (lat is None or lon is None) else 0,
        # uwi_suspect is about the KEY, not the well's status. These keys come
        # from CAPINO and are sound; misusing this to mark a status would be a
        # false claim in a column consumers trust.
        "uwi_suspect": 0,
        "primary_source": SOURCE,
        "source_count": 1,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="WELL_REF")
    ap.add_argument("--dir", required=True, help="the Wyoming source folder")
    ap.add_argument("--apply", action="store_true", help="write; else report only")
    ap.add_argument("--exclude-status", default="",
                    help="comma-separated STATUS codes to drop (default: none). "
                         "The status is kept in raw_well_status either way.")
    a = ap.parse_args()

    # ONE FILE PER DATASET. The folder holds the same data in several formats
    # — 050526PA.csv and 050526PA.txt are identical — and reading both made the
    # union report an overlap of 67,278 when the real overlap between PA and WH
    # is a single well. The rows were fine (same key, same row, overwritten),
    # but a headline number that is wrong by four orders of magnitude is worse
    # than no number: it is the figure you would quote.
    picked = {}
    for f in sorted(os.listdir(a.dir)):
        m = re.search(r"(PA|WH)\.(csv|txt)$", f, re.I)
        if not m:
            continue
        tag, ext = m.group(1).upper(), m.group(2).lower()
        # .csv first only as a tie-break; the delimiter is sniffed from the
        # CONTENT either way, so the extension decides nothing that matters.
        if tag not in picked or (ext == "csv" and picked[tag][1] != "csv"):
            picked[tag] = (os.path.join(a.dir, f), ext)
    if len(picked) < 2:
        print(f"expected a PA and a WH file in {a.dir}; found {sorted(picked)}",
              file=sys.stderr)
        return 2
    files = [picked[t][0] for t in ("PA", "WH")]
    skipped = [f for f in sorted(os.listdir(a.dir))
               if re.search(r"(PA|WH)\.(csv|txt)$", f, re.I)
               and os.path.join(a.dir, f) not in files]
    if skipped:
        print(f"(same data in another format, skipped: {', '.join(skipped)})")

    print("reading:")
    sets = {os.path.basename(f): read_file(f) for f in files}

    # WH wins on overlap: it carries the CURRENT status, PA the historical one.
    merged, overlap = {}, 0
    for name in sorted(sets, key=lambda n: ("WH" in n.upper())):   # PA first, WH last
        for cap, r in sets[name].items():
            if cap in merged:
                overlap += 1
            merged[cap] = r

    drop_codes = {x.strip().upper() for x in a.exclude_status.split(",") if x.strip()} \
                 or _NON_WELL_STATUS
    permits = {c for c, r in merged.items()
               if (r.get("STATUS") or "").strip().upper() in drop_codes}
    keep = {c: r for c, r in merged.items() if c not in permits}

    print(f"\n  union            {len(merged):>7,}   (overlap between the two files: {overlap:,})")
    print(f"  excluded by status {len(permits):>5,}   "
          + (f"({', '.join(sorted(drop_codes))})" if drop_codes else "(none — the source's word is kept)"))
    print(f"  to load          {len(keep):>7,}")

    rows = [to_master(c, r) for c, r in keep.items()]
    no_coord = sum(1 for x in rows if x["coord_suspect"])
    print(f"  without a coordinate {no_coord:>7,}  (flagged coord_suspect)")

    bad = [x for x in rows if not x["uwi14"].startswith("49")]
    print(f"  NOT starting '49'    {len(bad):>7,}  " +
          ("← investigate before applying" if bad else "(good)"))

    print("\n  sample:")
    for x in rows[:5]:
        print(f"    {x['uwi14']}  {str(x['well_name'])[:22]:<22} {x['county']:<10} "
              f"{x['surface_latitude']}, {x['surface_longitude']}  {x['raw_well_status']}")

    if not a.apply:
        print("\n-- report only; re-run with --apply to replace Wyoming.")
        return 0

    import pyodbc
    cn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={a.server};"
        f"DATABASE={a.database};Trusted_Connection=yes;", autocommit=False)
    cur = cn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM well_ref.well_master_gold WHERE province_state = ?", STATE)
        before = cur.fetchone()[0]
        cur.execute("DELETE FROM well_ref.well_master_gold WHERE province_state = ?", STATE)
        cols = list(rows[0])
        cur.fast_executemany = True
        cur.executemany(
            f"INSERT INTO well_ref.well_master_gold ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [[x[c] for c in cols] for x in rows])
        cn.commit()
        print(f"\n-- Wyoming replaced: {before:,} → {len(rows):,}")
    except Exception as e:
        cn.rollback()
        print(f"\n-- ROLLED BACK, nothing changed: {e}", file=sys.stderr)
        return 1
    finally:
        cn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
