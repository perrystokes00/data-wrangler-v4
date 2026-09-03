"""Build a public well master FROM THE FILES ON DISK, one state at a time.

WHY NOT DERIVE IT FROM well_master_gold. Not because gold is short of rows --
row for row it is mostly right, and AL, UT, VA and WV reproduce from disk
EXACTLY. The problem is the keys.

    gold  04600300001000   Swallow Rock No. 1   (API state 04 = California)
    disk  46003000010000   Swallow Rock No. 1   (API state 46 = Washington)

Washington writes its state code 046, so its API is ELEVEN digits. Taking the
first ten shifts every digit and drops the last, and all 809 Washington wells
in gold are keyed into California's number space. Kansas is the same failure
from the other end: 37,318 wells whose KID was used as though it were an API,
keyed into state 10, Georgia.

A prefix audit of the whole master finds more of it, but READ EACH FLAG
BEFORE BELIEVING IT -- a shared prefix is not by itself an error:

  * 04 by CA and WA -- REAL, the Washington bug above.
  * 23 by MS and MI -- REAL, and worse than it looks. The 5,465 rows tagged
    MI are in Jasper, Lamar and Jefferson Davis counties at lat 30-34: they
    are MISSISSIPPI wells under Michigan's label. Michigan's own 92,551 wells
    are not in the master at all; nothing in it uses prefix 21.
  * 17 by LA and GO -- NOT AN ERROR. GO is the Gulf of Mexico (GOM_BOEM), and
    offshore Louisiana wells correctly carry Louisiana's code 17, offshore
    Texas 42, offshore Alabama 01, and federal deepwater 60. All 54,663 have
    coordinates and all fall in the Gulf.

That is the identifier-as-a-number failure this codebase already knows well,
and a count check cannot see it: Washington's 809 is exactly the number of
distinct APIs in the file. So this reads the files, and every key carries the
state that published it.

WHAT MAKES A STATE LOADABLE. An identifier that is the agency's own, and
coordinates. Where the agency publishes an API, that is the key. Where it does
not -- Pennsylvania publishes a permit, Indiana a survey number -- the key is
built by a rule written down in the spec and the agency's own value is kept
verbatim in native_well_id. A key is never invented from a name or a sequence.

EVERY STATE RECONCILES BEFORE IT COUNTS. Each load reports rows read, rows
keyed, rows skipped and why. A state whose numbers do not add up is a state
that has not been loaded.

THREE SHAPES OF SOURCE, ONE PIPELINE. A CSV; a point shapefile, whose .dbf
carries latitude and longitude as ordinary columns (six states were set aside
as "needs geometry" and not one of them did); and Texas, which is 254
per-county file PAIRS and gets its own reader. The geometry is never parsed:
the agency's own published numbers are better evidence than a reprojection of
them.

    python tools/build_public_master.py --list
    python tools/build_public_master.py --state UT --preview
    python tools/build_public_master.py --state UT --apply
    python tools/build_public_master.py --all --apply
"""
import argparse
import csv
import datetime
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text          # noqa: E402

ROOT = ("C:/Users/perry/OneDrive/Documents/PPDM/claude_use_ai/wrangler_view/"
        "country/US/data_by_state/")
TABLE = "well_ref.well_master_public_v2"

# One entry per state. `api` is the column carrying the agency's identifier;
# `bounds` is (lat_lo, lat_hi, lon_lo, lon_hi) and is a CHECK, not a filter --
# a well outside its own state is flagged coord_suspect, never dropped.
SPECS = {
 "UT": dict(code="43", file="Utah/all_wells_utah.csv", source="UT_DOGM",
            agency="Utah Division of Oil, Gas and Mining",
            api="API Well Number", lat="Latitude", lon="Longitude",
            well="Well Name", op="Operator", county="County",
            field="Field Name", wtype="Well Type", wstat="Well Status",
            td="TD", bounds=(36.9, 42.1, -114.1, -108.9)),
 "WV": dict(code="47", file="West Virginia/WVDEP_GIS_data_oil_gas_og_wells_wgs84.csv",
            source="WV_DEP", agency="WV Dept of Environmental Protection",
            api="api", lat="GEOM_LATITUDE", lon="GEOM_LONGITUDE",
            well="FarmName", op="RespParty", county="County", field=None,
            wtype="WellType", wstat="WellStatus", td="WellDepth",
            bounds=(37.1, 40.7, -82.7, -77.7)),
 "NY": dict(code="31", file="New York/wellspublic.csv", source="NY_NYSDEC",
            agency="NY State Dept of Environmental Conservation",
            api="API_WellNo", lat="Surface_latitude", lon="Surface_Longitude",
            well="Well_Name", op="Company_name", county="County",
            field="Producing_name", wtype="Well_Type", wstat="Well_Status",
            td="Measured_depth", bounds=(40.4, 45.1, -79.8, -71.8)),
 "AL": dict(code="01", file="Alabama/WellList.csv", source="AL_OGB",
            agency="Geological Survey of Alabama / State Oil and Gas Board",
            api="API", lat="Latitude", lon="Longitude",
            well="WellName", op="Operator", county="County",
            field="FieldName", wtype="TypeDesc", wstat="StatusDesc",
            td="DTD", bounds=(30.1, 35.1, -88.6, -84.8)),
 # Kansas keys 35,820 wells by KID alone -- its own well identifier, present
 # on every row in the file. They are named wells with a status and, for
 # 35,772 of them, a coordinate, so they are wells, not stubs.
 "KS": dict(code="15", file="Kansas/ks_wells.csv", source="KS_KGS",
            agency="Kansas Geological Survey",
            api="API_NUMBER", lat="LATITUDE", lon="LONGITUDE",
            well="LEASE_WELL_NAME", op="CURR_OPERATOR", county=None,
            field="FIELD", wtype=None, wstat="STATUS", td="DEPTH",
            alt=dict(col="KID", prefix="15", width=10, source="KS_KGS_KID"),
            bounds=(36.9, 40.1, -102.2, -94.5)),
 # MICHIGAN WAS NEVER IN THE GOLD MASTER. Nothing there uses prefix 21, and
 # the 5,465 rows labelled MI are Mississippi wells (prefix 23, Jasper and
 # Lamar counties, lat 30-34). Its file publishes a full 14-digit api_num --
 # WellID(10) + Sidetrack(2) + Completion(2) -- and X / Y are already WGS84,
 # so nothing needs reprojecting. mgr_x / mgr_y are Michigan GeoRef metres and
 # are deliberately ignored.
 "MI": dict(code="21", file="Michigan/Michigan_wells.csv", source="MI_EGLE",
            agency="Michigan Dept of Environment, Great Lakes and Energy",
            api="api_num", lat="Y", lon="X",
            well="WellNameFull", op="CompanyName", county="CountyName",
            field="FieldName", wtype="WellType", wstat="WellStatus",
            td="DTD", bounds=(41.6, 48.4, -90.5, -82.1)),
 # Washington's API state code is 46 but the file writes it 046, so the digit
 # string is eleven long -- see `fix` in make_key. Its API carries no sidetrack
 # positions, and 19 of them are shared by more than one well.
 "WA": dict(code="46", file="Washington/wa_wells_wgs84.csv", source="WA_DNR",
            agency="Washington Geological Survey (DNR)",
            api="API_NUMBER", fix="drop_leading_zero",
            lat="GEOM_LATITUDE", lon="GEOM_LONGITUDE",
            well="WELL_NAME", op="COMPANY_NAME", county="COUNTY",
            field=None, wtype=None, wstat="WELL_STATUS", td="DEPTH_FEET",
            alt=dict(col="OIL_GAS_ID", prefix="46", width=8, on_collision=True,
                     source="WA_DNR_OGID"),
            bounds=(45.5, 49.1, -124.9, -116.9)),
 "VA": dict(code="45", file="Virginia/va_wells_wgs84.csv", source="VA_DMME",
            agency="VA Dept of Mines, Minerals and Energy",
            api="API", lat="GEOM_LATITUDE", lon="GEOM_LONGITUDE",
            well=None, op="Company_Name", county="County", field=None,
            wtype="Op_Type", wstat=None, td="Depth",
            bounds=(36.5, 39.5, -83.7, -75.2)),

 # ---- from a CSV -------------------------------------------------------
 # The Gulf of Mexico is not a state and legitimately uses four API state
 # codes; see _codes(). BOEM publishes twelve digits, the last two being the
 # sidetrack, which make_key now keeps.
 "GO": dict(code=("17", "42", "60", "01"),
            file="Gulf_of_Mexico/Borehole.csv", source="GOM_BOEM",
            agency="Bureau of Ocean Energy Management",
            api="API Well Number",
            lat="Surface Latitude*", lon="Surface Longitude*",
            well="Well Name", op="Company Name", county="Bottom Area",
            field=None, wtype="Type Code", wstat="Status Code",
            td="BH Total MD (feet)", bounds=(25.0, 31.0, -98.0, -81.0)),
 # Indiana publishes no API at all -- IGS_ID is the Geological Survey's own
 # well number, and permit_number in this file is empty on every row.
 "IN": dict(code="32", file="Indiana/indiana_wells_coords.csv",
            source="IN_IGS", agency="Indiana Geological & Water Survey",
            api=None, lat="latitude", lon="longitude",
            well="lease_name", op="operator_name", county="county",
            field=None, wtype=None, wstat="status", td=None,
            alt=dict(col="IGS_ID", prefix="32", width=8, source="IN_IGS"),
            bounds=(37.7, 41.8, -88.1, -84.7)),
 # Nine wells in Oregon's permit file are offshore Pacific and carry 56, not
 # Oregon's 36. That is correct, as it is in the Gulf -- so 56 is allowed and
 # only the genuinely odd rows flag (five letter-suffixed IDs that yield nine
 # digits, and one row literally named "test" keyed 99-999-99999).
 "OR": dict(code=("36", "56"), file="Oregon/OG_Permits_01-29-2021.csv",
            source="OR_DOGAMI",
            agency="Oregon Dept of Geology & Mineral Industries",
            api="PermitID", lat="Latitude", lon="Longitude",
            well="WellName", op="Permittee", county="County", field=None,
            wtype="WellType", wstat="Status", td="Depth",
            bounds=(41.9, 46.3, -124.6, -116.4)),
 # Pennsylvania keys on the permit, county-first: 083-02072 -> 37 083 02072.
 "PA": dict(code="37", file="Pennsylvania/Conventional_Wells.csv",
            source="PA_DEP", agency="PA Dept of Environmental Protection",
            api=None, lat="LATITUDE", lon="LONGITUDE",
            well="WELL_NAME", op="OPERATOR", county="COUNTY", field=None,
            wtype="WELL_TYPE", wstat="WELL_STATUS", td=None,
            alt=dict(col="PERMIT_NUMBER", prefix="37", width=8,
                     source="PA_DEP"),
            bounds=(39.6, 42.3, -80.6, -74.6)),

 # ---- from a point shapefile's attributes ------------------------------
 "CA": dict(code="04", reader="shp", file="California/Well.shp",
            source="CA_CALGEM",
            agency="CA Geologic Energy Management Division (CalGEM)",
            api="API", lat="Latitude", lon="Longitude",
            well="LeaseName", op="OperatorNa", county="CountyName",
            field="FieldName", wtype="WellTypeLa", wstat="WellStatus",
            td=None, bounds=(32.5, 42.1, -124.5, -114.1)),
 # COLORADO'S `API` COLUMN HAS NO STATE CODE -- it reads 12316872, which
 # would key every Colorado well into state 12, Illinois. API_Label is the
 # one that carries the 05.
 "CO": dict(code="05", reader="shp",
            file="Colorado/WELLS_SHP.ZIP!Wells.shp", source="CO_COGCC",
            agency="CO Oil & Gas Conservation Commission (ECMC)",
            api="API_Label", lat="Latitude", lon="Longitude",
            well="Well_Name", op="Operator", county=None,
            field="Field_Name", wtype="Well_Class", wstat="Facil_Stat",
            td="Max_MD", bounds=(36.9, 41.1, -109.1, -102.0)),
 # Idaho ships both NAD27 and WGS84 pairs; take the WGS84 one.
 "ID": dict(code="11", reader="shp",
            file="Idaho/DD-3OilAndGas2023.v3.zip"
                 "!DD-3OilAndGas2023_shp/IDOilGasWells.shp",
            source="ID_IGS", agency="Idaho Geological Survey",
            api="API", lat="LatitudeWG", lon="LongitudeW",
            well="WellName", op="Operator", county="County",
            field="FieldName", wtype="WellType", wstat="WellStatus",
            td="TotalDepth", bounds=(41.9, 49.1, -117.3, -110.9)),
 # Louisiana MUST be read from the zip: the extracted .shp/.dbf beside it are
 # unhydrated OneDrive placeholders that raise PermissionError. DBF truncates
 # field names to ten characters, hence SURFACE_LA / SURFACE_LO.
 "LA": dict(code="17", reader="shp",
            file="Louisiana/louisiana_shapefile.zip!Oil_Gas_Wells.shp",
            source="LA_DNR", agency="Louisiana Dept of Natural Resources",
            api="API_NUM", lat="SURFACE_LA", lon="SURFACE_LO",
            well="WELL_NAME", op="ORG_OPER_N", county="PARISH_NAM",
            field="FIELD_NAME", wtype=None, wstat="LEGEND_DES",
            td="MEASURED_D",
            # API_NUM is missing (all zeros) on 15,762 rows and shared by
            # genuinely different wells on 19,505 more. WELL_SERIA is the
            # state's own serial and is unique on all 247,097 rows.
            # PREFIX 179, NOT 17. "17" + serial.zfill(8) puts a six-digit
            # serial into the PARISH positions -- serial 100053 becomes
            # 17-001-00053, a real Louisiana API, and the guard caught 166 of
            # them. Parish 9xx cannot exist (LA uses 001-127), so 179 + a
            # seven-digit serial is a space no real API can reach. The largest
            # serial is 990,702, so seven digits is enough with room to spare.
            alt=dict(col="WELL_SERIA", prefix="179", width=7,
                     on_collision=True, source="LA_DNR_SERIAL"),
            bounds=(28.8, 33.1, -94.1, -88.7)),
 # ND ships api_no formatted and `api` already 14 clean digits.
 "ND": dict(code="33", reader="shp", file="North_Dakota/OGD_Wells.shp",
            source="ND_NDIC",
            agency="ND Industrial Commission, Oil & Gas Division",
            api="api", lat="latitude", lon="longitude",
            well="well_name", op="operator", county="County",
            field="field_name", wtype="well_type", wstat="status",
            td="td", bounds=(45.9, 49.1, -104.1, -96.5)),
 # TEXAS IS 254 COUNTY FILE PAIRS, not one file -- see texas_rows(). Its
 # published API is EIGHT characters because the state code 42 is in the file
 # name, so the reader puts it back before anything sees it.
 "TX": dict(code="42", reader="tx", file="Texas/shapefiles/",
            source="TX_RRC", agency="Texas Railroad Commission",
            api="API10", lat="LAT83", lon="LONG83",
            well="LEASE_NAME", op="OPERATOR", county="COUNTY_FIPS",
            field="FIELD_NAME", wtype="OIL_GAS_CO", wstat=None,
            td="TOTAL_DEPT", bounds=(25.7, 36.6, -106.7, -93.4)),
 # New Mexico has no column named api; `id` is the API (30-045-08708).
 "NM": dict(code="30", reader="shp",
            file="NewMexico/New_Mexico_OCD_Oil_and_Gas_Wells.shp",
            source="NM_OCD", agency="NM Oil Conservation Division (EMNRD)",
            api="id", lat="latitude", lon="longitude",
            well="name", op="ogrid_name", county="county", field=None,
            wtype="type", wstat="status", td="measured_v",
            bounds=(31.2, 37.1, -109.1, -102.9)),
}


def engine(db="WELL_REF"):
    cs = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;"
          "DATABASE=%s;Trusted_Connection=yes;" % db)
    return create_engine("mssql+pyodbc:///?odbc_connect="
                         + urllib.parse.quote_plus(cs),
                         fast_executemany=True)


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _codes(spec):
    """The API state code(s) a key may begin with.

    Usually one. The Gulf of Mexico legitimately uses four -- offshore
    Louisiana keeps LA's 17, offshore Texas 42, offshore Alabama 01, and
    federal deepwater 60 -- so a shared prefix is correct there, not a fault.
    """
    c = spec["code"]
    return (c,) if isinstance(c, str) else tuple(c)


def make_key(raw, spec, used_alt=False):
    """The agency's own digits, never a number this tool invented.

    THE AGENCY'S PRECISION IS KEPT. Where a state publishes a full 14-digit
    API, the last four carry a real suffix -- Kansas has 37,482 wells ending
    0001 and 5,786 ending 0002 -- and truncating to ten collapses a sidetrack
    into its parent. That silently lost 45,829 Kansas rows and 921 New York
    rows. A well published without a suffix pads to ...0000 and its sidetrack
    is ...0001, so the two never collide.

    Where the agency publishes no API at all, `alt` supplies its own
    identifier under the rule already used for PA, IN and AR: the API state
    code, then their number, right-padded to 14. Returns None if there is not
    enough to key on, which the caller counts rather than guesses at.
    """
    digits = re.sub(r"\D", "", raw)
    # SOME AGENCIES WRITE THE STATE CODE WITH A LEADING ZERO. Washington
    # publishes 046-041-00188 -- eleven digits, because the state code 46 is
    # written 046. Taking the first ten of that gives 0460410018: every digit
    # shifted, the last one lost, and the well keyed into API state 04,
    # California. It is a valid-looking key for the wrong state, which is the
    # identifier-read-as-a-number failure in its purest form.
    if spec.get("fix") == "drop_leading_zero" and len(digits) == 11 \
            and digits.startswith("0"):
        digits = digits[1:]
    if used_alt:
        a = spec["alt"]
        if not digits:
            return None
        return (a["prefix"] + digits.zfill(a["width"])).ljust(14, "0")[:14]
    if len(digits) >= 14:
        return digits[:14]
    if len(digits) >= 10:
        # PAD ON THE RIGHT, NEVER TRUNCATE TO TEN FIRST. BOEM publishes
        # twelve digits (012032016500): positions 11-12 are the sidetrack, and
        # cutting to ten before padding discards them exactly as it did for
        # Kansas. A ten-digit API is unaffected -- it pads to the same value.
        return digits.ljust(14, "0")
    return None


def texas_rows():
    """One row per Texas well, from 254 per-county file PAIRS.

    THE RAILROAD COMMISSION SPLITS TEXAS BY COUNTY AND BY ROLE.
    `shapefiles/well{FIPS}s.shp` holds the SURFACE hole -- there are also
    `…b` (bottom hole) and `…l` (lateral) sets, and a bottom hole is a
    different place from the wellhead, so taking the wrong one moves every
    horizontal well to the far end of its lateral. `dbf/api{FIPS}.dbf` holds
    the attributes; the two join on the 8-character API.

    THE API IN THESE FILES IS EIGHT CHARACTERS, NOT TEN: `00347422` is county
    003 plus well 47422, and Texas's state code 42 lives in the FILE NAME, not
    the field. Reading that column as the API keys the well into state 00.
    The state code is put back here, and only here.

    Coordinates: the geometry is NAD27 (EPSG:4267) and LONG27/LAT27 match it,
    so the NAD83 pair is used instead -- closest to WGS84 without reprojecting
    anything. The agency's own numbers, as everywhere else in this tool.
    """
    import fiona                                       # noqa: PLC0415
    base = ROOT + "Texas/"
    fips = sorted(f[4:7] for f in os.listdir(base + "shapefiles")
                  if re.match(r"^well\d{3}s\.shp$", f, re.I))
    for fp in fips:
        # THE DBF IS THE WELL LIST; THE SHAPEFILE IS ONLY WHERE THEY ARE.
        # County 003 has 30,941 wells in the dbf but 25,917 keyable surface
        # points, so driving from the shapefile silently loses every well the
        # RRC has not mapped. Every other state here keeps a well that lacks a
        # coordinate -- New York 1,087, Indiana 1,838 -- so Texas does too.
        coords = {}
        shp = base + "shapefiles/well%ss.shp" % fp
        if os.path.exists(shp):
            with fiona.open(shp) as src:
                for feat in src:
                    p = feat["properties"]
                    k = (p.get("API") or "").strip()
                    # A surface row whose API is just the county code and
                    # whose WELLID is null identifies no well: those are
                    # RELIAB '15' sketches, and they are not locations for
                    # anything we can name.
                    if len(k) == 8 and k not in coords:
                        coords[k] = (p.get("LAT83"), p.get("LONG83"))
        dbf = base + "dbf/api%s.dbf" % fp
        if not os.path.exists(dbf):
            continue
        with fiona.open(dbf) as src:
            for feat in src:
                p = feat["properties"]
                a8 = (p.get("APINUM") or "").strip()
                la, lo = coords.get(a8, (None, None))
                yield {
                    "API10": ("42" + a8) if a8 else "",
                    "LAT83": la, "LONG83": lo,
                    "COUNTY_FIPS": fp,
                    "LEASE_NAME": p.get("LEASE_NAME"),
                    "OPERATOR": p.get("OPERATOR"),
                    "FIELD_NAME": p.get("FIELD_NAME"),
                    "TOTAL_DEPT": p.get("TOTAL_DEPT"),
                    "OIL_GAS_CO": p.get("OIL_GAS_CO"),
                }


def source_rows(st, spec):
    """Yield dict rows from a CSV or a point shapefile, one at a time.

    A POINT SHAPEFILE IS NOT A HARDER SOURCE THAN A CSV. Six states were set
    aside as "geometry, needs unpacking" and none of them did: the .dbf
    carries latitude and longitude as ordinary attribute columns, so only the
    reader differs. The geometry is never touched -- the agency's own numbers
    are better evidence than a reprojection of them.

    `file` may name an entry inside a zip as "archive.zip!inner/file.shp".
    That is how Louisiana MUST be read: its extracted .shp and .dbf are
    OneDrive on-demand placeholders that were never hydrated and raise
    PermissionError, while the .zip beside them reads perfectly.
    """
    if spec.get("reader") == "tx":
        for r in texas_rows():
            yield {k: ("" if v is None else str(v)) for k, v in r.items()}
        return
    if spec.get("reader") == "shp":
        import fiona                                   # noqa: PLC0415
        rel = spec["file"]
        if "!" in rel:
            arc, inner = rel.split("!", 1)
            path = "/vsizip/" + ROOT + arc + "/" + inner
        else:
            path = ROOT + rel
        with fiona.open(path) as src:
            for feat in src:
                yield {k: ("" if v is None else str(v))
                       for k, v in feat["properties"].items()}
        return
    path = os.path.join(ROOT, spec["file"])
    if not os.path.exists(path):
        raise SystemExit("%s: file not found: %s" % (st, path))
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            yield r


def read(st, spec):
    """(rows, stats). Skips are counted by REASON, never silently."""
    get = lambda r, k: (r.get(spec[k]) or "").strip() if spec.get(k) else None
    # uwi14 -> which identifier produced it, so a derived key landing on a real
    # one is seen as a COLLISION and not swallowed by the duplicate count.
    seen, out, clash = {}, [], []
    stats = dict(read=0, no_api=0, short_api=0, duplicate=0, no_coords=0,
                 off_state=0, by_alt=0, wrong_state_code=0, api_shared=0)
    now = datetime.datetime.now()
    for r in source_rows(st, spec):
        stats["read"] += 1
        raw = (r.get(spec["api"]) or "").strip()
        # AN ALL-ZERO IDENTIFIER IS NOT AN IDENTIFIER. Louisiana writes
        # 00000000000000 in API_NUM on 15,762 rows. It is well-formed, it is
        # fourteen digits, and it would key every one of those wells onto the
        # same row -- the placeholder problem wearing an identifier's clothes.
        # Emptying it here sends them to the agency's own serial instead.
        if raw and not re.sub(r"\D", "", raw).strip("0"):
            raw = ""
        src = spec["source"]
        # WHICH BRANCH WAS TAKEN, not which source string came out of it.
        # Inferring this from `src != spec["source"]` silently failed for
        # Indiana and Pennsylvania, whose alt source is deliberately the SAME
        # name as their main one: the permit went down the API path and every
        # one of their 277,162 rows was rejected as "identifier too short".
        via_alt = False
        if not raw and spec.get("alt"):
            # The agency has no API for this well but does have its own
            # identifier. Same rule as PA / IN / AR: the API state code, then
            # their number, and their raw value kept verbatim below.
            raw = (r.get(spec["alt"]["col"]) or "").strip()
            src = spec["alt"].get("source", src)
            if raw:
                via_alt = True
                stats["by_alt"] += 1
        if not raw:
            stats["no_api"] += 1
            continue
        uwi = make_key(raw, spec, used_alt=via_alt)
        if uwi is None:
            stats["short_api"] += 1
            continue
        # What the row IS, independent of what it is keyed by: two rows with
        # the same name in the same place are one well written twice.
        sig = ((get(r, "well") or ""), (r.get(spec["lat"]) or ""),
               (r.get(spec["lon"]) or ""))
        if uwi in seen:
            prev_src, prev_sig = seen[uwi]
            if prev_src != src:
                clash.append(uwi)
            alt = spec.get("alt")
            if sig == prev_sig or via_alt or not (alt and alt.get("on_collision")):
                stats["duplicate"] += 1
                continue
            # A SHARED API OVER GENUINELY DIFFERENT WELLS. Washington's
            # 046-039-00001 covers three wells at three different places, and
            # its API has no sidetrack positions to tell them apart. Dropping
            # them loses real wells; appending a sequence would invent digits
            # this tool has no right to mint. The agency's own unique id is
            # neither -- so the well is keyed by that, and says so.
            araw = (r.get(alt["col"]) or "").strip()
            auwi = make_key(araw, spec, used_alt=True) if araw else None
            if not auwi or auwi in seen:
                stats["duplicate"] += 1
                continue
            uwi, raw, src = auwi, araw, alt.get("source", src)
            stats["api_shared"] += 1
        seen[uwi] = (src, sig)
        lat, lon = num(r.get(spec["lat"])), num(r.get(spec["lon"]))
        # MISSING AND WRONG ARE DIFFERENT FACTS. A well with no coordinate is
        # honestly incomplete; a well sitting outside its own state is a value
        # that will plot, export and get quoted. Counting them together hides
        # the second inside the first, so they are counted apart and the
        # coordinate is nulled rather than kept -- a null is visible, a wrong
        # position is not. 0,0 is the null spelled as a number.
        if lat is None or lon is None or (lat == 0 and lon == 0):
            lat = lon = None
            stats["no_coords"] += 1
            bad = 0
        else:
            b = spec["bounds"]
            bad = 0 if (b[0] <= lat <= b[1] and b[2] <= lon <= b[3]) else 1
            stats["off_state"] += bad
        out.append(dict(
            uwi14=uwi, api_10=uwi[:10],
            native_well_id=raw, native_id_source=src,
            well_name=get(r, "well"), operator_name=get(r, "op"),
            county=get(r, "county"), field_name=get(r, "field"),
            province_state=st, country="US",
            surface_latitude=lat, surface_longitude=lon,
            raw_well_type=get(r, "wtype"), raw_well_status=get(r, "wstat"),
            total_depth=num(r.get(spec["td"])) if spec.get("td") else None,
            primary_source=spec["source"], source_list=spec["source"],
            source_count=1, dup_count=1, quality_score=75,
            uwi_suspect=0, coord_suspect=bad, built_at=now))
        # THE KEY MUST CARRY THE STATE THAT PUBLISHED IT. Kansas's file has a
        # row whose API_NUMBER column holds a KID, keying it into API state 10.
        # One row here, but a whole column read from the wrong place looks
        # exactly the same and would otherwise load in silence.
        if not uwi.startswith(tuple(_codes(spec))):
            out[-1]["uwi_suspect"] = 1
            stats["wrong_state_code"] += 1
    stats["keyed"] = len(out)
    stats["placeholder"] = drop_placeholder_positions(out)
    # A DERIVED KEY MUST NEVER LAND ON A REAL ONE. Kansas's KID keys occupy
    # county positions 100-106 and today collide with nothing, but that is a
    # property of this download, not a guarantee. Two wells sharing a key is
    # the identifier-as-text failure again -- one well wearing another's
    # identity -- so it fails the load rather than being discovered later.
    if clash:
        raise SystemExit(
            "%s: REFUSING TO LOAD -- %d derived key(s) collide with a real "
            "API key, e.g. %s. Change the alt prefix/width before loading."
            % (st, len(clash), ", ".join(sorted(clash)[:5])))
    return out, stats


# DISTINCT WELLS sharing one position, not rows. A sidetrack legitimately
# shares its parent's surface coordinate -- Michigan publishes 34,335 of them
# -- so counting rows makes a heavily-sidetracked well look like a fill value.
# Counting distinct wellbores (the API-10 half of the key) asks the question
# that matters: how many different holes claim to be in the same spot?
PLACEHOLDER_PILE = 25      # beyond doubt a fill value: null the coordinate
DOUBTFUL_PILE = 10         # too many to believe, too few to be certain: flag


def drop_placeholder_positions(rows):
    """Null coordinates that are agency fill values, and say how many.

    WHY THIS IS NOT PARANOIA. West Virginia's file puts 3,922 wells on exactly
    37.04622, -81.00000 -- a longitude with five zeros after the point -- and
    another 3,601 onto twelve county centroids, each pile drawn from ONE county
    (982 wells all in county 085). Those coordinates are inside the state, so
    every range check passes them, and they plot as a tight cluster that looks
    like a field.

    This is the failure CLAUDE.md names: a wrong value defeats every repair
    keyed on "missing". Nulling them makes the gap visible and keeps the well.

    THE TWO TIERS EXIST BECAUSE THE EVIDENCE DIFFERS BY STATE. West Virginia
    separates cleanly -- nothing sits between a pile of 3 and a pile of 3,922.
    Michigan and Kansas decay smoothly, so there is no cut that is obviously
    right. Rather than pick one and destroy data on either side of it, a pile
    of 25+ distinct wells is nulled and a pile of 10-24 keeps its coordinate
    but is flagged coord_suspect. Held, not discarded; visible, not silent.
    """
    pile = {}
    for r in rows:
        if r["surface_latitude"] is not None:
            k = (r["surface_latitude"], r["surface_longitude"])
            pile.setdefault(k, set()).add(r["uwi14"][:10])
    bad = {k for k, w in pile.items() if len(w) >= PLACEHOLDER_PILE}
    iffy = {k for k, w in pile.items()
            if DOUBTFUL_PILE <= len(w) < PLACEHOLDER_PILE}
    n = 0
    for r in rows:
        k = (r["surface_latitude"], r["surface_longitude"])
        if k in bad:
            r["surface_latitude"] = r["surface_longitude"] = None
            r["coord_suspect"] = 1
            n += 1
        elif k in iffy:
            r["coord_suspect"] = 1
    return n


# The full column list, spelled out. It USED to be cloned from
# well_master_gold with SELECT TOP 0 * INTO -- convenient until gold was
# dropped, at which point this tool could no longer create its own table. A
# rebuild must not depend on the thing it replaces.
SCHEMA = """
    uwi14 char(14) NOT NULL, api_10 char(10), well_name nvarchar(300),
    well_num nvarchar(50), operator_name nvarchar(300),
    field_name nvarchar(200), surface_latitude decimal(9,6),
    surface_longitude decimal(9,6), county nvarchar(100),
    province_state char(2), country char(2), raw_well_type nvarchar(200),
    raw_well_status nvarchar(200), std_well_type varchar(40),
    std_well_status varchar(40), total_depth decimal(9,1), spud_date date,
    name_norm nvarchar(400), uwi_suspect bit NOT NULL,
    coord_suspect bit NOT NULL, primary_source nvarchar(120),
    source_list nvarchar(400), source_count int NOT NULL,
    dup_count int NOT NULL, quality_score tinyint NOT NULL,
    built_at datetime2 NOT NULL, kb_elevation numeric(12,2),
    ground_elevation numeric(12,2), elevation_ouom nvarchar(12),
    completion_date date, abandonment_date date,
    bottom_hole_latitude decimal(11,7), bottom_hole_longitude decimal(11,7),
    formation_at_td nvarchar(60), producing_formation nvarchar(60),
    lease_name nvarchar(120), well_profile_type nvarchar(40),
    long_lat_source nvarchar(40), h3_r4 nvarchar(16), h3_r5 nvarchar(16),
    h3_r6 nvarchar(16), h3_r7 nvarchar(16), h3_coord_hash binary(32),
    native_well_id nvarchar(64), native_id_source nvarchar(40)
"""


def ensure_table(e):
    with e.begin() as c:
        c.execute(text("IF OBJECT_ID('%s') IS NULL CREATE TABLE %s (%s)"
                       % (TABLE, TABLE, SCHEMA)))
        c.execute(text("""IF NOT EXISTS (SELECT 1 FROM sys.indexes
              WHERE name='ix_wmp2_uwi14' AND object_id=OBJECT_ID('%s'))
            CREATE INDEX ix_wmp2_uwi14 ON %s (uwi14)""" % (TABLE, TABLE)))


# A state agency's own record beats the offshore file for the same well.
# BOEM's Gulf extract reaches into state waters, so 371 Plaquemines Parish
# wells arrive twice -- once from Louisiana, once from GOM_BOEM. They are the
# same wells, and the state that regulates them publishes the parish, field
# and operator, so Louisiana's row is the better one. Nothing is invented and
# nothing is lost: one row per well, owned by the agency closest to it.
YIELDS_TO_STATES = ("GO",)


def dedupe_cross_state(e):
    """One row per well where two agencies publish the same one.

    TWO RULES, IN ORDER.

    1. The offshore file yields to a state's own file (YIELDS_TO_STATES).

    2. THE KEY'S OWN STATE CODE DECIDES. Louisiana's file carries 12 wells
       whose API begins 42 -- Texas -- in Panola and Shelby counties on the
       Sabine, the river that IS the state line. A well does not belong to
       whichever agency happened to list it; it belongs to the state that
       issued its number. This is the same evidence the wrong_state_code flag
       already raises, put to use rather than just reported.
    """
    removed = 0
    with e.begin() as c:
        removed += c.execute(text("""DELETE a FROM %s a
            WHERE a.province_state IN ('GO')
              AND EXISTS (SELECT 1 FROM %s b WHERE b.uwi14 = a.uwi14
                          AND b.province_state <> a.province_state)"""
                                  % (TABLE, TABLE))).rowcount
    owner = {}
    for st, spec in SPECS.items():
        # The offshore region shares LA's 17 and TX's 42 by design and has
        # already yielded under rule 1. Leaving it in makes every code it
        # touches look ambiguous and disqualifies exactly the ones needed --
        # which is why this rule silently removed nothing on its first run.
        if st in YIELDS_TO_STATES:
            continue
        for code in _codes(spec):
            owner.setdefault(code, []).append(st)
    # Only codes belonging to exactly one state can settle an argument.
    owner = {k: v[0] for k, v in owner.items() if len(v) == 1}
    with e.connect() as c:
        dups = [tuple(r) for r in c.execute(text("""
            SELECT uwi14, LEFT(uwi14, 2) FROM %s
            GROUP BY uwi14 HAVING COUNT(DISTINCT province_state) > 1"""
                                                 % TABLE))]
    drop = [(u, owner[code]) for u, code in dups if code in owner]
    with e.begin() as c:
        for uwi, keep in drop:
            removed += c.execute(text(
                "DELETE FROM %s WHERE uwi14 = :u AND province_state <> :s"
                % TABLE), {"u": uwi, "s": keep}).rowcount
    return removed


def load(e, st, rows):
    cols = list(rows[0].keys())
    sql = ("INSERT INTO %s (%s) VALUES (%s)"
           % (TABLE, ", ".join(cols), ", ".join(":" + c for c in cols)))
    with e.begin() as c:
        c.execute(text("DELETE FROM %s WHERE province_state = :s" % TABLE),
                  {"s": st})
        for i in range(0, len(rows), 2000):
            c.execute(text(sql), rows[i:i + 2000])


def report(st, spec, stats):
    print("%-4s %-34s" % (st, os.path.basename(spec["file"])[:34]))
    print("     rows read       %10s" % format(stats["read"], ","))
    print("     keyed & loaded  %10s" % format(stats["keyed"], ","))
    for k, label in (("by_alt", "keyed from agency id"),
                     ("api_shared", "shared API, keyed from agency id"),
                     ("no_api", "no identifier"), ("short_api", "identifier too short"),
                     ("duplicate", "duplicate key"),
                     ("no_coords", "no coordinate (kept)"),
                     ("off_state", "outside state (flagged)"),
                     ("placeholder", "placeholder position (nulled)"),
                     ("wrong_state_code", "key not in this state (flagged)")):
        if stats[k]:
            print("     %-15s %10s" % (label, format(stats[k], ",")))
    total = stats["keyed"] + stats["no_api"] + stats["short_api"] + stats["duplicate"]
    print("     reconciles      %10s %s"
          % (format(total, ","), "OK" if total == stats["read"] else "*** MISMATCH ***"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        e = engine()
        with e.connect() as c:
            have = {}
            if c.execute(text("SELECT OBJECT_ID('%s')" % TABLE)).scalar():
                have = {r[0]: r[1] for r in c.execute(text(
                    "SELECT province_state, COUNT(*) FROM %s "
                    "GROUP BY province_state" % TABLE))}
        print("%-4s %-38s %10s" % ("st", "source file", "loaded"))
        for st, spec in sorted(SPECS.items()):
            print("%-4s %-38s %10s"
                  % (st, spec["file"][:38], format(have.get(st, 0), ",")))
        print("\ntotal: %s" % format(sum(have.values()), ","))
        return
    targets = sorted(SPECS) if a.all else ([a.state] if a.state else [])
    if not targets:
        raise SystemExit("give --state XX, --all, or --list")
    e = engine()
    if a.apply:
        ensure_table(e)
    for st in targets:
        rows, stats = read(st, SPECS[st])
        report(st, SPECS[st], stats)
        if a.apply:
            load(e, st, rows)
            with e.connect() as c:
                n = c.execute(text("SELECT COUNT(*) FROM %s WHERE "
                                   "province_state=:s" % TABLE), {"s": st}).scalar()
            print("     in the table    %10s" % format(n, ","))
        print()
    if a.apply:
        n = dedupe_cross_state(e)
        if n:
            print("cross-source overlap: %s offshore row(s) removed where the "
                  "state's own file has the same well\n" % format(n, ","))
        with e.connect() as c:
            print("%s now holds %s wells across %d states"
                  % (TABLE,
                     format(c.execute(text("SELECT COUNT(*) FROM %s" % TABLE)).scalar(), ","),
                     c.execute(text("SELECT COUNT(DISTINCT province_state) "
                                    "FROM %s" % TABLE)).scalar()))


if __name__ == "__main__":
    main()
