"""
seed_political.py
=================
Seeds the DataView political reference tables from free public APIs
and curated static data. No API keys required.

Tables seeded:
  dv_country        ~250 rows  — restcountries.com API (ISO 3166)
  dv_province_state  ~80 rows  — Census TIGER (US) + static (Canada/Mexico/UK/AUS)
  dv_county        ~3,200 rows — Census TIGER county API
  dv_basin           ~55 rows  — curated static petroleum basin list
  dv_plss_township    0 rows   — skipped (too large; add separately)
  dv_ocs_block        0 rows   — skipped (add separately from BOEM)

Usage:
    python seed_political.py --server "127.0.0.1\\SQLEXPRESS" --database DataView --windows-auth
    python seed_political.py --server "127.0.0.1\\SQLEXPRESS" --database DataView --username sa --password secret
    python seed_political.py ... --wipe   # clear existing political data first

Requirements: sqlalchemy, pyodbc, requests (all in requirements.txt)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from sqlalchemy import create_engine, text
    HAS_SQLA = True
except ImportError:
    HAS_SQLA = False

# =============================================================================
# CONNECTION
# =============================================================================

def _build_engine(args):
    if not HAS_SQLA:
        _die("sqlalchemy not installed. pip install sqlalchemy pyodbc")
    import pyodbc
    server   = args.server   or os.getenv("DB_SERVER", "")
    database = args.database or os.getenv("DB_NAME", "DataView")
    driver   = args.driver   or os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
    if not server:
        _die("No server specified. Pass --server or set DB_SERVER in .env")
    if args.windows_auth:
        odbc = f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
    else:
        odbc = (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
                f"UID={args.username};PWD={args.password};")
    def _creator():
        return pyodbc.connect(odbc)
    return create_engine("mssql+pyodbc://", creator=_creator,
                         fast_executemany=True, pool_pre_ping=False)


def _die(msg):
    print(f"\n  ERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def _get(url, params=None, timeout=30):
    """HTTP GET with retry."""
    if not HAS_REQUESTS:
        _die("requests not installed. pip install requests")
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == 2:
                raise
            print(f"    Retry {attempt+1}/3 — {exc}")
            time.sleep(2)


def _upsert(con, table, pk_cols, rows):
    """Bulk MERGE upsert — skips existing rows, surfaces errors."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    on_clause   = " AND ".join(f"tgt.[{c}] = src.[{c}]" for c in pk_cols)
    insert_cols = ", ".join(f"[{c}]" for c in cols)
    insert_vals = ", ".join(f"src.[{c}]" for c in cols)
    sql = f"""
        MERGE dataview.{table} AS tgt
        USING (SELECT {", ".join(f":{c} AS [{c}]" for c in cols)}) AS src
        ON ({on_clause})
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals});
    """
    inserted = 0
    errors   = 0
    last_err = None
    for row in rows:
        try:
            con.execute(text(sql), row)
            inserted += 1
        except Exception as e:
            errors  += 1
            last_err = str(e)[:120]
    if errors:
        print(f"    WARNING: {errors} rows skipped — {last_err}")
    return inserted


def _bulk_insert(con, table, rows):
    """Simple bulk INSERT for clean reference data — faster than MERGE."""
    if not rows:
        return 0
    cols        = list(rows[0].keys())
    insert_cols = ", ".join(f"[{c}]" for c in cols)
    insert_vals = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO dataview.{table} ({insert_cols}) VALUES ({insert_vals})"
    con.execute(text(sql), rows)
    return len(rows)


# =============================================================================
# WIPE
# =============================================================================

def _wipe(con):
    print("  Wiping existing political data …")
    # Order matters — children first
    for tbl in ["dv_plss_township", "dv_ocs_block", "dv_county",
                 "dv_basin", "dv_province_state", "dv_country"]:
        con.execute(text(f"DELETE FROM dataview.{tbl}"))
    print("  Done.")


# =============================================================================
# 1. COUNTRIES  (restcountries.com)
# =============================================================================

def seed_countries(con) -> dict[str, str]:
    """
    Fetch all countries from restcountries.com and insert into dv_country.
    Returns {alpha3: alpha3} map for FK use.
    """
    print("  Fetching countries from restcountries.com …")
    data = _get("https://restcountries.com/v3.1/all",
                 params={"fields": "name,cca2,cca3,region,subregion,unMember,currencies"})

    rows = []
    for c in data:
        a3   = c.get("cca3", "")
        a2   = c.get("cca2", "")
        name = c.get("name", {}).get("common", "")
        if not a3 or not name:
            continue
        native = ""
        native_names = c.get("name", {}).get("nativeName", {})
        if native_names:
            first = next(iter(native_names.values()), {})
            native = first.get("common", "")
        currency_code = ""
        currencies = c.get("currencies", {})
        if currencies:
            currency_code = next(iter(currencies.keys()), "")[:3]
        rows.append(dict(
            country_code=a3[:3],
            country_code_a2=a2[:2] if a2 else None,
            country_name=name[:255],
            country_name_local=native[:255] if native else None,
            continent=c.get("region", "")[:40] or None,
            region=c.get("subregion", "")[:100] or None,
            currency_code=currency_code[:3] if currency_code else None,
            active_ind="Y",
            row_created_by="SEEDER",
            source="DATAVIEW"
        ))

    inserted = _upsert(con, "dv_country", ["country_code"], rows)
    print(f"    {inserted} countries inserted ({len(rows)} fetched)")
    return {r["country_code"]: r["country_code"] for r in rows}


# =============================================================================
# 2. PROVINCE / STATE
# =============================================================================

# Static Canada provinces/territories (ISO 3166-2)
_CANADA_PROVINCES = [
    ("CA-AB", "Alberta",                "AB", "PROVINCE", "48"),
    ("CA-BC", "British Columbia",       "BC", "PROVINCE", "59"),
    ("CA-MB", "Manitoba",               "MB", "PROVINCE", "46"),
    ("CA-NB", "New Brunswick",          "NB", "PROVINCE", "13"),
    ("CA-NL", "Newfoundland and Labrador","NL","PROVINCE", "10"),
    ("CA-NS", "Nova Scotia",            "NS", "PROVINCE", "12"),
    ("CA-NT", "Northwest Territories",  "NT", "TERRITORY","61"),
    ("CA-NU", "Nunavut",                "NU", "TERRITORY","62"),
    ("CA-ON", "Ontario",                "ON", "PROVINCE", "35"),
    ("CA-PE", "Prince Edward Island",   "PE", "PROVINCE", "11"),
    ("CA-QC", "Quebec",                 "QC", "PROVINCE", "24"),
    ("CA-SK", "Saskatchewan",           "SK", "PROVINCE", "47"),
    ("CA-YT", "Yukon",                  "YT", "TERRITORY","60"),
]

# Key international states/provinces for petroleum industry
_INTL_PROVINCES = [
    # Mexico
    ("MX-CAM","Campeche",       "CAM","STATE","MEX"),
    ("MX-TAM","Tamaulipas",     "TAM","STATE","MEX"),
    ("MX-VER","Veracruz",       "VER","STATE","MEX"),
    # UK
    ("GB-ENG","England",        "ENG","COUNTRY","GBR"),
    ("GB-SCT","Scotland",       "SCT","COUNTRY","GBR"),
    ("GB-WLS","Wales",          "WLS","COUNTRY","GBR"),
    # Norway
    ("NO-03", "Oslo",           "03", "COUNTY","NOR"),
    # Australia
    ("AU-WA", "Western Australia",    "WA","STATE","AUS"),
    ("AU-QLD","Queensland",           "QLD","STATE","AUS"),
    ("AU-SA", "South Australia",      "SA","STATE","AUS"),
    ("AU-NT", "Northern Territory",   "NT","TERRITORY","AUS"),
    # Saudi Arabia
    ("SA-04", "Eastern Province",     "04","REGION","SAU"),
    # UAE
    ("AE-AZ", "Abu Dhabi",            "AZ","EMIRATE","ARE"),
    # Iraq
    ("IQ-BA", "Basra",                "BA","GOVERNORATE","IRQ"),
    # Brazil
    ("BR-RJ", "Rio de Janeiro",       "RJ","STATE","BRA"),
    ("BR-ES", "Espirito Santo",       "ES","STATE","BRA"),
    ("BR-BA", "Bahia",                "BA","STATE","BRA"),
    # Nigeria
    ("NG-RI", "Rivers",               "RI","STATE","NGA"),
    ("NG-DE", "Delta",                "DE","STATE","NGA"),
    ("NG-AK", "Akwa Ibom",           "AK","STATE","NGA"),
    # Angola
    ("AO-CAB","Cabinda",             "CAB","PROVINCE","AGO"),
    ("AO-ZAI","Zaire",               "ZAI","PROVINCE","AGO"),
    # Kazakhstan
    ("KZ-ATY","Atyrau",              "ATY","REGION","KAZ"),
    ("KZ-MAN","Mangystau",           "MAN","REGION","KAZ"),
]

def seed_province_state(con) -> dict[str, str]:
    """
    Seed US states from Census TIGER + static Canada + key international.
    Returns {province_state_id: country_code} map.
    """
    print("  Fetching US states from Census TIGER …")
    rows = []
    result_map = {}

    # US states from Census TIGER
    try:
        data = _get(
            "https://api.census.gov/data/2020/dec/pl",
            params={"get": "NAME", "for": "state:*"}
        )
        # data[0] is header ['NAME', 'state'], rest are data rows
        fips_to_abbrev = {
            "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO",
            "09":"CT","10":"DE","11":"DC","12":"FL","13":"GA","15":"HI",
            "16":"ID","17":"IL","18":"IN","19":"IA","20":"KS","21":"KY",
            "22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN",
            "28":"MS","29":"MO","30":"MT","31":"NE","32":"NV","33":"NH",
            "34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH",
            "40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD",
            "47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA",
            "54":"WV","55":"WI","56":"WY","60":"AS","66":"GU","69":"MP",
            "72":"PR","78":"VI",
        }
        for row in data[1:]:
            name, fips = row[0], row[1]
            abbrev = fips_to_abbrev.get(fips, "")
            ps_id  = f"US-{abbrev}" if abbrev else f"US-{fips}"
            ps_type = "TERRITORY" if fips in ("60","66","69","72","78") else "STATE"
            rows.append(dict(
                province_state_id=ps_id[:10],
                country_code="USA",
                province_state_name=name[:255],
                province_state_abbrev=abbrev[:10] if abbrev else None,
                province_state_type=ps_type,
                fips_code=fips[:5],
                active_ind="Y",
                row_created_by="SEEDER",
                source="TIGER"
            ))
            result_map[ps_id] = "USA"
        print(f"    {len(rows)} US states/territories from TIGER")
    except Exception as exc:
        print(f"    WARNING: Census TIGER failed — {exc}")

    # Canada
    for ps_id, name, abbrev, ps_type, fips in _CANADA_PROVINCES:
        rows.append(dict(
            province_state_id=ps_id[:10],
            country_code="CAN",
            province_state_name=name[:255],
            province_state_abbrev=abbrev[:10],
            province_state_type=ps_type,
            fips_code=fips[:5],
            active_ind="Y",
            row_created_by="SEEDER",
            source="DATAVIEW"
        ))
        result_map[ps_id] = "CAN"

    # International
    country_map = {
        "MEX":"MEX","GBR":"GBR","NOR":"NOR","AUS":"AUS",
        "SAU":"SAU","ARE":"ARE","IRQ":"IRQ","BRA":"BRA",
        "NGA":"NGA","AGO":"AGO","KAZ":"KAZ",
    }
    for ps_id, name, abbrev, ps_type, cc3 in _INTL_PROVINCES:
        rows.append(dict(
            province_state_id=ps_id[:10],
            country_code=cc3,
            province_state_name=name[:255],
            province_state_abbrev=abbrev[:10],
            province_state_type=ps_type,
            fips_code=None,
            active_ind="Y",
            row_created_by="SEEDER",
            source="DATAVIEW"
        ))
        result_map[ps_id] = cc3

    inserted = _upsert(con, "dv_province_state", ["province_state_id"], rows)
    print(f"    {inserted} province/states inserted ({len(rows)} total)")
    return result_map


# =============================================================================
# 3. US COUNTIES  (Census TIGER)
# =============================================================================

def seed_counties(con):
    """
    Seed US counties for key petroleum states using embedded FIPS data.
    Full US county set requires Census TIGER API access (api.census.gov).
    Covered: TX (254), NM (33), ND (53), WY (23) = 363 counties
    """
    print("  Seeding counties for key petroleum states (embedded FIPS data) …")
    print("    (Full US county load requires Census TIGER API access)")

    STATE_FIPS_TO_PS = {
        "48": ("US-TX", "Texas"),
        "35": ("US-NM", "New Mexico"),
        "38": ("US-ND", "North Dakota"),
        "56": ("US-WY", "Wyoming"),
        "08": ("US-CO", "Colorado"),
        "40": ("US-OK", "Oklahoma"),
        "30": ("US-MT", "Montana"),
        "22": ("US-LA", "Louisiana"),
        "05": ("US-AR", "Arkansas"),
        "20": ("US-KS", "Kansas"),
    }

    TX = [("001","Anderson"),("003","Andrews"),("005","Angelina"),("007","Aransas"),
        ("009","Archer"),("011","Armstrong"),("013","Atascosa"),("015","Austin"),
        ("017","Bailey"),("019","Bandera"),("021","Bastrop"),("023","Baylor"),
        ("025","Bee"),("027","Bell"),("029","Bexar"),("031","Blanco"),
        ("033","Borden"),("035","Bosque"),("037","Bowie"),("039","Brazoria"),
        ("041","Brazos"),("043","Brewster"),("045","Briscoe"),("047","Brooks"),
        ("049","Brown"),("051","Burleson"),("053","Burnet"),("055","Caldwell"),
        ("057","Calhoun"),("059","Callahan"),("061","Cameron"),("063","Camp"),
        ("065","Carson"),("067","Cass"),("069","Castro"),("071","Chambers"),
        ("073","Cherokee"),("075","Childress"),("077","Clay"),("079","Cochran"),
        ("081","Coke"),("083","Coleman"),("085","Collin"),("087","Collingsworth"),
        ("089","Colorado"),("091","Comal"),("093","Comanche"),("095","Concho"),
        ("097","Cooke"),("101","Cottle"),("103","Crane"),("105","Crockett"),
        ("107","Crosby"),("109","Culberson"),("111","Dallam"),("113","Dallas"),
        ("115","Dawson"),("117","Deaf Smith"),("119","Delta"),("121","Denton"),
        ("123","DeWitt"),("125","Dickens"),("127","Dimmit"),("129","Donley"),
        ("131","Duval"),("133","Eastland"),("135","Ector"),("137","Edwards"),
        ("139","Ellis"),("141","El Paso"),("143","Erath"),("145","Falls"),
        ("147","Fannin"),("149","Fayette"),("151","Fisher"),("153","Floyd"),
        ("155","Foard"),("157","Fort Bend"),("159","Franklin"),("161","Freestone"),
        ("163","Frio"),("165","Gaines"),("167","Galveston"),("169","Garza"),
        ("171","Gillespie"),("173","Glasscock"),("175","Goliad"),("177","Gonzales"),
        ("179","Gray"),("181","Grayson"),("183","Gregg"),("185","Grimes"),
        ("187","Guadalupe"),("189","Hale"),("191","Hall"),("193","Hamilton"),
        ("195","Hansford"),("197","Hardeman"),("199","Hardin"),("201","Harris"),
        ("203","Harrison"),("205","Hartley"),("207","Haskell"),("209","Hays"),
        ("211","Hemphill"),("213","Henderson"),("215","Hidalgo"),("217","Hill"),
        ("219","Hockley"),("221","Hood"),("223","Hopkins"),("225","Houston"),
        ("227","Howard"),("229","Hudspeth"),("231","Hunt"),("233","Hutchinson"),
        ("235","Irion"),("237","Jack"),("239","Jackson"),("241","Jasper"),
        ("243","Jeff Davis"),("245","Jefferson"),("247","Jim Hogg"),("249","Jim Wells"),
        ("251","Johnson"),("253","Jones"),("255","Karnes"),("257","Kaufman"),
        ("259","Kendall"),("261","Kenedy"),("263","Kent"),("265","Kerr"),
        ("267","Kimble"),("269","King"),("271","Kinney"),("273","Kleberg"),
        ("275","Knox"),("277","Lamar"),("279","Lamb"),("281","Lampasas"),
        ("283","La Salle"),("285","Lavaca"),("287","Lee"),("289","Leon"),
        ("291","Liberty"),("293","Limestone"),("295","Lipscomb"),("297","Live Oak"),
        ("299","Llano"),("301","Loving"),("303","Lubbock"),("305","Lynn"),
        ("307","McCulloch"),("309","McLennan"),("311","McMullen"),("313","Madison"),
        ("315","Marion"),("317","Martin"),("319","Mason"),("321","Matagorda"),
        ("323","Maverick"),("325","Medina"),("327","Menard"),("329","Midland"),
        ("331","Milam"),("333","Mills"),("335","Mitchell"),("337","Montague"),
        ("339","Montgomery"),("341","Moore"),("343","Morris"),("345","Motley"),
        ("347","Nacogdoches"),("349","Navarro"),("351","Newton"),("353","Nolan"),
        ("355","Nueces"),("357","Ochiltree"),("359","Oldham"),("361","Orange"),
        ("363","Palo Pinto"),("365","Panola"),("367","Parker"),("369","Parmer"),
        ("371","Pecos"),("373","Polk"),("375","Potter"),("377","Presidio"),
        ("379","Rains"),("381","Randall"),("383","Reagan"),("385","Real"),
        ("387","Red River"),("389","Reeves"),("391","Refugio"),("393","Roberts"),
        ("395","Robertson"),("397","Rockwall"),("399","Runnels"),("401","Rusk"),
        ("403","Sabine"),("405","San Augustine"),("407","San Jacinto"),
        ("409","San Patricio"),("411","San Saba"),("413","Schleicher"),("415","Scurry"),
        ("417","Shackelford"),("419","Shelby"),("421","Sherman"),("423","Smith"),
        ("425","Somervell"),("427","Starr"),("429","Stephens"),("431","Sterling"),
        ("433","Stonewall"),("435","Sutton"),("437","Swisher"),("439","Tarrant"),
        ("441","Taylor"),("443","Terrell"),("445","Terry"),("447","Throckmorton"),
        ("449","Titus"),("451","Tom Green"),("453","Travis"),("455","Trinity"),
        ("457","Tyler"),("459","Upshur"),("461","Upton"),("463","Uvalde"),
        ("465","Val Verde"),("467","Van Zandt"),("469","Victoria"),("471","Walker"),
        ("473","Waller"),("475","Ward"),("477","Washington"),("479","Webb"),
        ("481","Wharton"),("483","Wheeler"),("485","Wichita"),("487","Wilbarger"),
        ("489","Willacy"),("491","Williamson"),("493","Wilson"),("495","Winkler"),
        ("497","Wise"),("499","Wood"),("501","Yoakum"),("503","Young"),
        ("505","Zapata"),("507","Zavala"),]

    NM = [("001","Bernalillo"),("003","Catron"),("005","Chaves"),("006","Cibola"),
        ("007","Colfax"),("009","Curry"),("011","De Baca"),("013","Dona Ana"),
        ("015","Eddy"),("017","Grant"),("019","Guadalupe"),("021","Harding"),
        ("023","Hidalgo"),("025","Lea"),("027","Lincoln"),("028","Los Alamos"),
        ("029","Luna"),("031","McKinley"),("033","Mora"),("035","Otero"),
        ("037","Quay"),("039","Rio Arriba"),("041","Roosevelt"),("043","Sandoval"),
        ("045","San Juan"),("047","San Miguel"),("049","Santa Fe"),("051","Sierra"),
        ("053","Socorro"),("055","Taos"),("057","Torrance"),("059","Union"),
        ("061","Valencia"),]

    ND = [("001","Adams"),("003","Barnes"),("005","Benson"),("007","Billings"),
        ("009","Bottineau"),("011","Bowman"),("013","Burke"),("015","Burleigh"),
        ("017","Cass"),("019","Cavalier"),("021","Dickey"),("023","Divide"),
        ("025","Dunn"),("027","Eddy"),("029","Emmons"),("031","Foster"),
        ("033","Golden Valley"),("035","Grand Forks"),("037","Grant"),("039","Griggs"),
        ("041","Hettinger"),("043","Kidder"),("045","La Moure"),("047","Logan"),
        ("049","McHenry"),("051","McIntosh"),("053","McKenzie"),("055","McLean"),
        ("057","Mercer"),("059","Morton"),("061","Mountrail"),("063","Nelson"),
        ("065","Oliver"),("067","Pembina"),("069","Pierce"),("071","Ramsey"),
        ("073","Ransom"),("075","Renville"),("077","Richland"),("079","Rolette"),
        ("081","Sargent"),("083","Sheridan"),("085","Sioux"),("087","Slope"),
        ("089","Stark"),("091","Steele"),("093","Stutsman"),("095","Towner"),
        ("097","Traill"),("099","Walsh"),("101","Ward"),("103","Wells"),
        ("105","Williams"),]

    WY = [("001","Albany"),("003","Big Horn"),("005","Campbell"),("007","Carbon"),
        ("009","Converse"),("011","Crook"),("013","Fremont"),("015","Goshen"),
        ("017","Hot Springs"),("019","Johnson"),("021","Laramie"),("023","Lincoln"),
        ("025","Natrona"),("027","Niobrara"),("029","Park"),("031","Platte"),
        ("033","Sheridan"),("035","Sublette"),("037","Sweetwater"),("039","Teton"),
        ("041","Uinta"),("043","Washakie"),("045","Weston"),]

    STATE_COUNTIES = {
        "48": TX,
        "35": NM,
        "38": ND,
        "56": WY,
    }

    def _county_type(name):
        n = name.upper()
        if "PARISH" in n:       return "PARISH"
        if "BOROUGH" in n:      return "BOROUGH"
        if "MUNICIPALITY" in n: return "MUNICIPALITY"
        if "CENSUS AREA" in n:  return "CENSUS AREA"
        return "COUNTY"

    rows = []
    for state_fips, counties in STATE_COUNTIES.items():
        ps_id, state_name = STATE_FIPS_TO_PS[state_fips]
        for cnty_fips, cnty_name in counties:
            fips_full = f"{state_fips}{cnty_fips}"
            rows.append(dict(
                county_id=f"USA|{state_fips}|{cnty_fips}"[:40],
                province_state_id=ps_id[:10],
                country_code="USA",
                county_name=cnty_name[:255],
                county_type=_county_type(cnty_name),
                fips_state_code=state_fips[:3],
                fips_county_code=cnty_fips[:3],
                fips_full=fips_full[:5],
                tiger_geoid=fips_full[:20],
                active_ind="Y",
                row_created_by="SEEDER",
                source="DATAVIEW"
            ))

    inserted = _bulk_insert(con, "dv_county", rows)
    print(f"    {inserted} counties inserted (TX:{len(TX)} NM:{len(NM)} ND:{len(ND)} WY:{len(WY)})")
    print(f"    Note: Add remaining states when Census TIGER API is accessible")


# =============================================================================
# 4. PETROLEUM BASINS  (curated static)
# =============================================================================

_BASINS = [
    # North America
    ("PERMIAN",       "Permian Basin",              "SEDIMENTARY",    "USA", 31.5,  -102.5,  450000, "UNCONVENTIONAL"),
    ("DJ",            "Denver-Julesburg Basin",     "FORELAND",       "USA", 40.0,  -104.0,  100000, "UNCONVENTIONAL"),
    ("BAKKEN",        "Williston Basin (Bakken)",   "SEDIMENTARY",    "USA", 47.5,  -103.0,  440000, "UNCONVENTIONAL"),
    ("ANADARKO",      "Anadarko Basin",             "SEDIMENTARY",    "USA", 35.5,  -98.0,   130000, "CONVENTIONAL"),
    ("APPALACHIAN",   "Appalachian Basin",          "FORELAND",       "USA", 40.0,  -78.0,   480000, "UNCONVENTIONAL"),
    ("EAGLE_FORD",    "Eagle Ford Basin",           "SEDIMENTARY",    "USA", 28.5,  -99.0,    90000, "UNCONVENTIONAL"),
    ("HAYNESVILLE",   "Haynesville Basin",          "SEDIMENTARY",    "USA", 32.0,  -94.0,    65000, "UNCONVENTIONAL"),
    ("UINTA",         "Uinta Basin",                "SEDIMENTARY",    "USA", 40.0,  -110.0,   40000, "CONVENTIONAL"),
    ("PICEANCE",      "Piceance Basin",             "SEDIMENTARY",    "USA", 39.5,  -108.5,   28000, "CONVENTIONAL"),
    ("SAN_JOAQUIN",   "San Joaquin Basin",          "SEDIMENTARY",    "USA", 36.0,  -119.5,   50000, "CONVENTIONAL"),
    ("GULF_MEXICO",   "Gulf of Mexico Basin",       "PASSIVE_MARGIN", "USA", 25.0,   -90.0,  1500000,"CONVENTIONAL"),
    ("POWDER_RIVER",  "Powder River Basin",         "FORELAND",       "USA", 44.0,  -106.0,   65000, "CONVENTIONAL"),
    ("ARKLA",         "Arkla Basin",                "SEDIMENTARY",    "USA", 33.0,   -94.0,   60000, "CONVENTIONAL"),
    ("MIDCONTINENT",  "Mid-Continent Basin",        "SEDIMENTARY",    "USA", 36.0,   -97.0,  200000, "CONVENTIONAL"),
    ("WESTERN_CANADA","Western Canada Sedimentary", "SEDIMENTARY",    "CAN", 53.0,  -114.0, 1400000, "MIXED"),
    ("MACKENZIE",     "Mackenzie Delta Basin",      "FORELAND",       "CAN", 68.0,  -134.0,  140000, "CONVENTIONAL"),
    ("HIBERNIA",      "Jeanne d'Arc Basin",         "PASSIVE_MARGIN", "CAN", 47.0,   -48.0,   28000, "CONVENTIONAL"),
    ("BURGOS",        "Burgos Basin",               "SEDIMENTARY",    "MEX", 26.0,   -98.5,   65000, "CONVENTIONAL"),
    ("SURESTE",       "Sureste Basin",              "SEDIMENTARY",    "MEX", 18.5,   -93.0,   80000, "CONVENTIONAL"),
    # South America
    ("CAMPOS",        "Campos Basin",               "PASSIVE_MARGIN", "BRA", -22.0,  -40.5,  100000, "CONVENTIONAL"),
    ("SANTOS",        "Santos Basin",               "PASSIVE_MARGIN", "BRA", -25.0,  -44.0,  350000, "CONVENTIONAL"),
    ("MARACAIBO",     "Maracaibo Basin",            "SEDIMENTARY",    "VEN",  10.0,  -72.0,   50000, "CONVENTIONAL"),
    ("LLANOS",        "Llanos Basin",               "FORELAND",       "COL",   5.0,  -72.0,  340000, "CONVENTIONAL"),
    ("NEUQUEN",       "Neuquen Basin",              "FORELAND",       "ARG", -38.0,  -69.0,  148000, "UNCONVENTIONAL"),
    # Europe
    ("NORTH_SEA",     "North Sea Basin",            "SEDIMENTARY",    "GBR",  57.0,    3.0,  750000, "CONVENTIONAL"),
    ("NORWEGIAN",     "Norwegian Continental Shelf","PASSIVE_MARGIN", "NOR",  63.0,    5.0,  800000, "CONVENTIONAL"),
    ("PARIS",         "Paris Basin",                "SEDIMENTARY",    "FRA",  48.5,    2.5,  170000, "CONVENTIONAL"),
    ("PANNONIAN",     "Pannonian Basin",            "RIFT",           "HUN",  47.0,   19.0,  200000, "CONVENTIONAL"),
    # Middle East / Africa
    ("ARABIAN",       "Arabian Basin",              "SEDIMENTARY",    "SAU",  24.0,   50.0, 2200000, "CONVENTIONAL"),
    ("ZAGROS",        "Zagros Fold Belt",           "FORELAND",       "IRN",  32.0,   48.0,  450000, "CONVENTIONAL"),
    ("MESOPOTAMIAN",  "Mesopotamian Basin",         "FORELAND",       "IRQ",  31.5,   47.0,  200000, "CONVENTIONAL"),
    ("RUB_AL_KHALI",  "Rub al Khali Basin",         "SEDIMENTARY",    "SAU",  22.0,   52.0,  780000, "CONVENTIONAL"),
    ("SIRTE",         "Sirte Basin",                "RIFT",           "LBY",  29.0,   19.0,  600000, "CONVENTIONAL"),
    ("NILE_DELTA",    "Nile Delta Basin",           "PASSIVE_MARGIN", "EGY",  31.0,   31.0,  100000, "CONVENTIONAL"),
    ("NIGER_DELTA",   "Niger Delta Basin",          "PASSIVE_MARGIN", "NGA",   5.0,    6.0,   75000, "CONVENTIONAL"),
    ("ANGOLA_MARGIN", "Angola Continental Margin",  "PASSIVE_MARGIN", "AGO",  -8.0,   12.0,  300000, "CONVENTIONAL"),
    ("EAST_AFRICA",   "East African Rift System",   "RIFT",           "KEN",   0.0,   37.0,  800000, "CONVENTIONAL"),
    # FSU / Russia
    ("WEST_SIBERIA",  "West Siberian Basin",        "SEDIMENTARY",    "RUS",  61.0,   75.0, 3500000,"CONVENTIONAL"),
    ("VOLGA_URAL",    "Volga-Ural Basin",           "SEDIMENTARY",    "RUS",  54.0,   53.0,  700000, "CONVENTIONAL"),
    ("CASPIAN",       "Caspian Basin",              "RIFT",           "KAZ",  43.0,   53.0,  700000,"CONVENTIONAL"),
    ("AMU_DARYA",     "Amu Darya Basin",            "SEDIMENTARY",    "TKM",  38.0,   62.0,  350000, "CONVENTIONAL"),
    # Asia Pacific
    ("TARIM",         "Tarim Basin",                "SEDIMENTARY",    "CHN",  40.0,   84.0,  560000, "CONVENTIONAL"),
    ("SICHUAN",       "Sichuan Basin",              "SEDIMENTARY",    "CHN",  30.0,  105.0,  260000, "UNCONVENTIONAL"),
    ("GIPPSLAND",     "Gippsland Basin",            "RIFT",           "AUS", -38.5,  147.0,   46000, "CONVENTIONAL"),
    ("CARNARVON",     "Carnarvon Basin",            "PASSIVE_MARGIN", "AUS", -22.0,  114.0,  450000, "CONVENTIONAL"),
    ("COOPER",        "Cooper Basin",               "SEDIMENTARY",    "AUS", -28.0,  140.0,  130000, "CONVENTIONAL"),
    ("MALAY",         "Malay Basin",                "RIFT",           "MYS",   6.0,  104.0,   85000, "CONVENTIONAL"),
    ("KUTEI",         "Kutei Basin",                "DELTA",          "IDN",   0.0,  117.0,   60000, "CONVENTIONAL"),
    ("KRISHNA_GODAV", "Krishna-Godavari Basin",     "PASSIVE_MARGIN", "IND",  16.0,   82.0,   50000, "CONVENTIONAL"),
    ("MUMBAI",        "Mumbai Offshore Basin",      "PASSIVE_MARGIN", "IND",  18.0,   71.0,  110000, "CONVENTIONAL"),
]


def seed_basins(con):
    print("  Seeding petroleum basins (curated) …")
    rows = []
    for bid, name, btype, cc3, lat, lon, area, play in _BASINS:
        # Clean up any stray quotes in the data
        lat_clean = float(str(lat).replace('"',''))
        lon_clean = float(str(lon).replace('"',''))
        rows.append(dict(
            basin_id=bid[:40],
            basin_name=name[:255],
            basin_type=btype[:40],
            country_code=cc3[:3],
            area_km2=float(area),
            centroid_latitude=lat_clean,
            centroid_longitude=lon_clean,
            primary_play_type=play[:40],
            active_ind="Y",
            row_created_by="SEEDER",
            source="DATAVIEW"
        ))
    inserted = _upsert(con, "dv_basin", ["basin_id"], rows)
    print(f"    {inserted} basins inserted")


# =============================================================================
# SUMMARY
# =============================================================================

def _summary(con):
    tables = ["dv_country","dv_province_state","dv_county","dv_basin",
              "dv_plss_township","dv_ocs_block"]
    print(f"\n  {'Table':<25} {'Rows':>8}")
    print(f"  {'-'*25} {'-'*8}")
    total = 0
    for tbl in tables:
        cnt = con.execute(text(f"SELECT COUNT(*) FROM dataview.{tbl}")).scalar()
        print(f"  {tbl:<25} {cnt:>8,}")
        total += cnt
    print(f"\n  Total political rows: {total:,}")


# =============================================================================
# CLI + MAIN
# =============================================================================

def _parse_args():
    p = argparse.ArgumentParser(description="Seed DataView political reference tables.")
    p.add_argument("--server",       default="")
    p.add_argument("--database",     default="DataView_Demo")
    p.add_argument("--windows-auth", action="store_true")
    p.add_argument("--username",     default="")
    p.add_argument("--password",     default="")
    p.add_argument("--driver",       default="ODBC Driver 17 for SQL Server")
    p.add_argument("--wipe",         action="store_true",
                   help="Clear existing political data before seeding")

    return p.parse_args()


def main():
    args = _parse_args()

    print()
    print("=" * 60)
    print("  DataView — Political Reference Seeder")
    print("=" * 60)

    if not HAS_REQUESTS:
        _die("requests not installed. Run: pip install requests")

    engine = _build_engine(args)
    with engine.connect() as con:
        row = con.execute(text("SELECT @@VERSION")).fetchone()
        print(f"\n  Connected: {str(row[0]).split(chr(10))[0]}")

    print()
    with engine.begin() as con:
        if args.wipe:
            _wipe(con)

    # Commit 1 — countries and states (counties FK to these)
    print("  Seeding …\n")
    with engine.begin() as con:
        seed_countries(con)
        seed_province_state(con)

    # Commit 2 — counties and basins (after states are committed)
    with engine.begin() as con:
        seed_counties(con)
        seed_basins(con)

    print()
    with engine.connect() as con:
        _summary(con)

    print()
    print("=" * 60)
    print("  Political seed complete.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
