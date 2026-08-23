"""
dataview/migration/synth_data.py
===============================
Generate a coherent synthetic petroleum dataset keyed on REAL API numbers.

WHY THIS EXISTS
---------------
The previous synthetic set used FIPS state/county codes (Illinois 17, Cook 031).
Oil and gas uses API codes, where Illinois is 12 and Kansas is 15 — the same
digits mean different states in the two systems, so FIPS-coded UWIs decode to
the wrong county against PPDM's area seed and do it silently. This generator
takes its codes from seed_dbo_area_country_state_county.csv, so every UWI
decodes correctly: LEFT(uwi,2) is the state's AREA_ID and LEFT(uwi,5) is the
county's.

IT REFLECTS YOUR SCHEMA RATHER THAN ASSUMING IT
-----------------------------------------------
Column names differ table to table and I've only seen a few of them. So instead
of emitting a fixed layout you'd then have to map, this reads each dv_ table's
real columns from sys.columns and fills what it finds. Add a column to a dv_
table and the next run populates it; rename one and nothing breaks silently.

Filling works in three passes per column:
  1. CONTEXT   — a name already decided for this row's parents (uwi, log_id,
                 the survey id...). This is what keeps children attached to
                 their parents rather than to plausible-looking strangers.
  2. ROLE      — matched from the column name: latitude, elevation, a depth, a
                 date, an indicator, a remark. Roles carry sensible ranges, so
                 a KB elevation isn't 40,000 and a latitude stays on land.
  3. TYPE      — anything unrecognised gets a value valid for its SQL type.

WHAT IT DOESN'T DO
------------------
It doesn't write to the database. It emits CSVs, one per table, so the data
goes in through the loaders that are being tested rather than around them.

USAGE (from the repo root)
--------------------------
    py -m dataview.migration.synth_data --list
    py -m dataview.migration.synth_data --wells 200 --out C:\\synth ^
        --area-seed "C:\\...\\PPDM_REFERENCE_LISTS\\seed_dbo_area_country_state_county.csv"
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import random
from datetime import date, timedelta

from sqlalchemy import text

SRC_SCHEMA = "dataview"

# Approximate onshore bounding boxes, so coordinates land in the right state
# rather than merely being valid floats. County-level placement would need
# polygons; state-level is enough to make a map look right and to exercise
# coordinate handling.
STATE_BBOX = {
    "15": (37.0, 40.0, -102.0, -94.6),    # Kansas
    "42": (26.0, 36.5, -106.6, -93.5),    # Texas
    "35": (33.6, 37.0, -103.0, -94.4),    # Oklahoma
    "30": (31.3, 37.0, -109.0, -103.0),   # New Mexico
    "05": (37.0, 41.0, -109.0, -102.0),   # Colorado
    "49": (41.0, 45.0, -111.0, -104.0),   # Wyoming
}
DEFAULT_STATES = ["15", "42", "35", "30"]

OPERATORS = [
    ("BA_ANADARKO", "Anadarko Petroleum"), ("BA_APACHE", "Apache Corporation"),
    ("BA_CHESAPEAKE", "Chesapeake Energy"), ("BA_CIMAREX", "Cimarex Energy"),
    ("BA_DEVON", "Devon Energy"), ("BA_MURFIN", "Murfin Drilling"),
    ("BA_OXY", "Occidental Petroleum"), ("BA_PIONEER", "Pioneer Natural Res"),
    ("BA_SANDRIDGE", "SandRidge Energy"), ("BA_VESS", "Vess Oil Corporation"),
]
# (field_id, field_name). Wells reference the id, and dv_field is emitted from
# the same list — so well.field_id always resolves. Generating a name alone left
# field_id to the text fallback ("field_id-112"), pointing at fields that never
# existed and producing a wall of unresolved parents at the FK stage.
FIELDS = [("FLD_001", "EL DORADO"), ("FLD_002", "HUGOTON"),
          ("FLD_003", "PANOMA"), ("FLD_004", "SPIVEY-GRABS"),
          ("FLD_005", "CHASE-SILICA"), ("FLD_006", "BEMIS-SHUTTS"),
          ("FLD_007", "TRAPP"), ("FLD_008", "KRAFT-PRUSA"),
          ("FLD_009", "ZENITH"), ("FLD_010", "RHODES")]

# Kansas/Mid-Continent stratigraphy, shallowest first — so tops generated for a
# well are in depth order and geologically plausible rather than random.
STRAT = [
    ("DAKOTA", "Dakota Sandstone", 300), ("KIOWA", "Kiowa Shale", 500),
    ("CHEYENNE", "Cheyenne Sandstone", 620), ("MORRISON", "Morrison Fm", 780),
    ("STONE_CORRAL", "Stone Corral Anhydrite", 1450),
    ("HUTCHINSON", "Hutchinson Salt", 1600), ("CHASE", "Chase Group", 2400),
    ("COUNCIL_GROVE", "Council Grove Group", 2700),
    ("ADMIRE", "Admire Group", 2900), ("LANSING", "Lansing Group", 3200),
    ("KANSAS_CITY", "Kansas City Group", 3350),
    ("MARMATON", "Marmaton Group", 3600), ("CHEROKEE", "Cherokee Group", 3750),
    ("MISSISSIPPIAN", "Mississippian", 3950),
    ("ARBUCKLE", "Arbuckle Group", 4300), ("PRECAMBRIAN", "Precambrian", 4600),
]

# Standard triple-combo curve set — mnemonic, description, unit.
CURVES = [
    ("DEPT", "Depth", "FT"), ("GR", "Gamma Ray", "GAPI"),
    ("CALI", "Caliper", "IN"), ("SP", "Spontaneous Potential", "MV"),
    ("RILD", "Deep Induction Resistivity", "OHMM"),
    ("RILM", "Medium Induction Resistivity", "OHMM"),
    ("RLL3", "Laterolog Resistivity", "OHMM"),
    ("RHOB", "Bulk Density", "G/CC"), ("DRHO", "Density Correction", "G/CC"),
    ("NPHI", "Neutron Porosity", "V/V"), ("DPHI", "Density Porosity", "V/V"),
    ("PEF", "Photoelectric Factor", "B/E"), ("DT", "Sonic Travel Time", "US/F"),
]

_UOM = {"depth": "FT", "elev": "FT", "vol": "BBL", "gas": "MCF",
        "press": "PSI", "temp": "DEGF"}


# --------------------------------------------------------------------------- #
# Area codes — read from PPDM's own seed so the UWIs decode correctly
# --------------------------------------------------------------------------- #
def entity_id(name: str) -> str:
    """The canonical entity id the loader derives from a name.

    Kept for verification only — the generator does NOT write these. It exists
    so a test can confirm Python and SQL Server agree on the digest.

    bulk_dir_loader promotes an "entity" FK by hashing the SOURCE value rather
    than copying it — _id_sql() emits
        UPPER(CONVERT(varchar(40), HASHBYTES('SHA1',
              UPPER(LTRIM(RTRIM(CAST(col AS nvarchar(4000)))))), 2))
    so a well referencing a field must carry the field's NAME, and the field
    table must hold that same hash as its id. Writing a made-up key like
    'FLD_001' on both sides looks consistent and fails: one side gets hashed and
    the other doesn't.

    nvarchar is UTF-16LE on the wire, which is what HASHBYTES sees — encoding as
    UTF-8 here would produce a different digest that never matches.
    """
    return hashlib.sha1(
        (name or "").strip().upper().encode("utf-16-le")).hexdigest().upper()


def load_counties(seed_path, states):
    """[(api_state, api_county5, county_name)] from the PPDM area seed.

    Falls back to a synthesised county list if the seed isn't to hand — usable,
    but the county codes then won't join to PPDM's area rows, which is the whole
    point of using the seed.
    """
    out = []
    if seed_path and os.path.exists(seed_path):
        with open(seed_path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("AREA_TYPE") or "").upper() != "COUNTY":
                    continue
                aid = (r.get("AREA_ID") or "").strip()
                if len(aid) >= 4 and aid[:-3] in states:
                    out.append((aid[:-3].zfill(2), aid.zfill(5),
                                (r.get("PREFERRED_NAME") or "").strip()))
    if not out:
        for s in states:
            for c in range(1, 60, 2):
                out.append((s, f"{s}{c:03d}", f"County {c}"))
    return out


# --------------------------------------------------------------------------- #
# Schema reflection
# --------------------------------------------------------------------------- #
def table_columns(conn, table, schema=SRC_SCHEMA):
    """Insertable columns only.

    COMPUTED columns are excluded: SQL Server rejects any attempt to write one
    ("cannot be modified because it is either a computed column..."), so
    emitting a value for it produces a CSV that cannot load. dv_well_formation_top
    .gross_thickness is one — derived from base_depth minus top_depth. Identity
    columns are excluded for the same reason.
    """
    rows = conn.execute(text(
        "SELECT c.name, ty.name AS type_name, c.max_length, c.is_nullable "
        "FROM sys.columns c "
        "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
        "WHERE c.object_id = OBJECT_ID(:t) "
        "  AND c.is_computed = 0 AND c.is_identity = 0 "
        "ORDER BY c.column_id"),
        {"t": f"{schema}.{table}"}).fetchall()
    out = []
    for n, t, mlen, nullable in rows:
        t = (t or "").lower()
        chars = None
        if t in ("varchar", "char"):
            chars = None if mlen == -1 else int(mlen)
        elif t in ("nvarchar", "nchar"):
            chars = None if mlen == -1 else int(mlen) // 2
        out.append({"name": n, "type": t, "chars": chars,
                    "nullable": bool(nullable)})
    return out


def list_tables(conn, schema=SRC_SCHEMA):
    rows = conn.execute(text(
        "SELECT t.name FROM sys.tables t JOIN sys.schemas s "
        "ON s.schema_id = t.schema_id WHERE s.name = :s ORDER BY t.name"),
        {"s": schema}).fetchall()
    return [r[0] for r in rows
            if r[0].lower().startswith("dv_")
            and not r[0].lower().startswith("dv_r_")]


# --------------------------------------------------------------------------- #
# Value generation
# --------------------------------------------------------------------------- #
_SKIP = {"row_created_by", "row_created_date", "row_changed_by",
         "row_changed_date", "cat_row_id", "promoted", "promoted_at",
         "captured_at", "geog", "h3_coord_hash", "_batch_loaded_at"}


def _role(name: str) -> str:
    n = name.lower()
    if "latitude" in n or n.endswith("_lat") or n == "lat":
        return "lat"
    if "longitude" in n or n.endswith("_lon") or n == "lon":
        return "lon"
    # UNITS ARE CLASSIFIED FIRST, and that ordering is the fix, not a style
    # choice. "depth" was tested before this, so depth_ouom matched "depth"
    # and was filled with a NUMBER (1137.7) instead of "FT"; elevation_ouom
    # matched "elev" the same way, and formation_at_td matched "_td". A unit
    # column is a unit column whatever it measures, so it must be decided
    # before the thing it measures gets a chance to claim it.
    #
    # And a bare `"unit" in n` is NOT a units test: strat_unit_name,
    # strat_unit_type and strat_unit_subtype are STRATIGRAPHIC units —
    # formations — and that clause stamped "FT" into 512 formation names,
    # where it reads as real data rather than as missing data.
    if "ouom" in n or n.endswith("_uom") or n.endswith("_unit") or n == "unit":
        return "uom"
    # A FORMATION IS A NAME even when the column name mentions a depth.
    # "formation_at_td" means the formation AT total depth; it matched "_td"
    # and was filled with 693.1, and a number in a formation column reads as
    # data rather than as absent. Decided before depth for the same reason
    # units are. Columns that really ARE depths (formation_top_depth) keep
    # their measure suffix and fall through.
    if ("formation" in n or "strat" in n) and not any(
            n.endswith(k) for k in ("_depth", "_top", "_base", "_md", "_tvd")):
        return "name"
    if "elev" in n:
        return "elev"
    if any(k in n for k in ("depth", "_td", "td_", "md", "tvd")):
        return "depth"
    if "date" in n or n.endswith("_dt"):
        return "date"
    if n.endswith("_ind"):
        return "ind"
    if "remark" in n or "comment" in n or "description" in n or "desc" in n:
        return "remark"
    if n == "source" or n.endswith("_source"):
        return "source"
    if "azimuth" in n or "azim" in n:
        return "azimuth"
    if "inclination" in n or n in ("incl", "drift"):
        return "incl"
    if "volume" in n or n.startswith("oil") or n.startswith("gas") \
            or n.startswith("water"):
        return "volume"
    if "pressure" in n or n.startswith("psi"):
        return "pressure"
    if "temperature" in n or n.startswith("temp"):
        return "temp"
    if "porosity" in n or "perm" in n or "saturation" in n:
        return "frac"
    if n.endswith("_num") or n.endswith("_no") or n.endswith("_seq"):
        return "seq"
    return ""


def _value(col, ctx, rng):
    """One column's value. Every string result is clamped to the column's
    declared width at the end — a generator that emits data the loader can't
    insert is the generator's bug, and 'value too long' is the way that bug
    shows up three phases later."""
    v = _value_raw(col, ctx, rng)
    chars = col["chars"]
    if chars and isinstance(v, str) and len(v) > chars:
        # A narrow date column isn't a truncated date — it's a coarser one.
        # nvarchar(7) means YYYY-MM, a monthly production period; nvarchar(6)
        # means YYYYMM. Chopping an ISO date to '2015-09' happens to be right
        # here and '2015-0' would not be, so the granularity is chosen
        # explicitly rather than left to a slice.
        if _role(col["name"]) == "date" and len(v) >= 10:
            iso = v[:10]
            if chars >= 10:
                v = iso
            elif chars >= 8:
                v = iso.replace("-", "")        # YYYYMMDD
            elif chars >= 7:
                v = iso[:7]                     # YYYY-MM
            elif chars >= 6:
                v = iso[:7].replace("-", "")    # YYYYMM
            else:
                v = iso[:4]                     # YYYY
        v = v[:chars]
    return v


# Columns that describe WHERE A ROW CAME FROM, not what it says. A generator
# has no honest value for these: the row didn't come from a file on disk, was
# never catalogued, and has no inventory lineage. The type fallback used to
# invent one anyway — "INVENTORY_ID-426", "file_path-801", "catalog_id-142" —
# which is worse than blank. INVENTORY_ID in particular is the key every
# lineage report joins on, so a fabricated value is a key that silently matches
# nothing and looks like real provenance in the table. Emit NULL instead, and
# let the loader/promote path fill these when a row genuinely comes from a file.
PROVENANCE_COLS = {
    "inventory_id", "catalog_id", "cat_row_id", "file_path", "source_path",
    "file_hash", "captured_hash", "promoted", "promoted_at", "captured_at",
    "vaulted_at", "scan_date", "root_path", "_batch_loaded_at",
}


def _value_raw(col, ctx, rng):
    n = col["name"]
    key = n.lower()
    # PROVENANCE IS CHECKED BEFORE ctx, and the order is the point. ctx is
    # built per-row and copied into children with dict(w), so the day anyone
    # sets ctx["inventory_id"] — to thread a real load's id through, say —
    # this guard goes inert with nothing to show for it: no error, just
    # fabricated lineage back in the output. A generator has no honest value
    # for these columns no matter what ctx was told, so nothing upstream is
    # allowed to overrule it.
    if key in PROVENANCE_COLS:          # no honest synthetic value — see above
        return None
    if key in ctx:                      # parent keys and decided values
        return ctx[key]
    role = _role(n)
    t = col["type"]
    chars = col["chars"] or 40

    if role == "lat":
        return round(ctx.get("_lat", 38.0) + rng.uniform(-0.05, 0.05), 6)
    if role == "lon":
        return round(ctx.get("_lon", -98.0) + rng.uniform(-0.05, 0.05), 6)
    if role == "elev":
        return round(ctx.get("_gl", 1500) + rng.uniform(0, 30), 1)
    if role == "depth":
        return round(rng.uniform(500, ctx.get("_td", 4500)), 1)
    if role == "date":
        return (ctx.get("_spud", date(2015, 1, 1))
                + timedelta(days=rng.randint(0, 400))).isoformat()
    if role == "ind":
        return "Y"
    if role == "uom":
        # _UOM has existed all along and was never consulted, so every unit
        # column read "FT" — pressure in feet, flow rate in feet, permeability
        # in feet. Match on the measure the column names, and fall back to FT
        # only when nothing matches (most of them really are depths).
        for _k, _u in _UOM.items():
            if _k in key:
                return _u
        if "rate" in key:
            return "BBL/D"
        if "perm" in key:
            return "MD"
        if "dens" in key:
            return "SPF"
        return "FT"
    if role == "remark":
        return "Synthetic test record"
    if role == "source":
        return "SYNTH"
    if role == "azimuth":
        return round(rng.uniform(0, 360), 2)
    if role == "incl":
        return round(rng.uniform(0, 6), 2)
    if role == "volume":
        return round(rng.uniform(0, 5000), 1)
    if role == "pressure":
        return round(rng.uniform(200, 4000), 1)
    if role == "temp":
        return round(rng.uniform(70, 210), 1)
    if role == "frac":
        return round(rng.uniform(0.02, 0.30), 4)
    if role == "seq":
        return rng.randint(1, 9)

    if t in ("int", "bigint", "smallint", "tinyint"):
        return rng.randint(1, 999)
    if t in ("numeric", "decimal", "float", "real", "money"):
        return round(rng.uniform(0, 1000), 3)
    if t in ("date", "datetime", "datetime2", "smalldatetime"):
        return date(2018, 1, 1).isoformat()
    if t == "bit":
        return 1
    return f"{n[:max(1, chars - 4)]}-{rng.randint(100, 999)}"


def make_row(cols, ctx, rng):
    return {c["name"]: _value(c, ctx, rng) for c in cols
            if c["name"].lower() not in _SKIP}


# --------------------------------------------------------------------------- #
# The dataset
# --------------------------------------------------------------------------- #
def generate(conn, out_dir, n_wells, counties, seed=42, log=print):
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)

    # Remove any CSVs already here. A table that generated last run but not this
    # one would otherwise leave its old file looking current, and the loader has
    # no way to tell — the rows reference wells from a different generation, so
    # the FK stage reports violations that look like a data fault and aren't.
    # (Worse if the file has been opened in Excel since: Excel renders a 14-digit
    #  UWI as 1.50012E+13 and writes that back, destroying the identifier.)
    stale = [f for f in os.listdir(out_dir) if f.lower().endswith(".csv")]
    for f in stale:
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass
    if stale:
        log(f"-- cleared {len(stale)} existing CSV(s) from {out_dir}")

    written = {}

    def emit(table, rows):
        if not rows:
            return
        path = os.path.join(out_dir, f"{table}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        written[table] = len(rows)
        log(f"  {table:28} {len(rows):>7} row(s)")

    def cols(table):
        try:
            return table_columns(conn, table)
        except Exception:
            return []

    # ── wells ──────────────────────────────────────────────────────────────
    wcols = cols("dv_well")
    if not wcols:
        raise RuntimeError("dataview.dv_well not found")

    wells, seq = [], {}
    for i in range(n_wells):
        st, cty5, cty_name = rng.choice(counties)
        seq[cty5] = seq.get(cty5, rng.randint(20000, 21000)) + 1
        uwi = f"{cty5}{seq[cty5]:05d}0000"        # API: SS CCC NNNNN SSSS
        lat0, lat1, lon0, lon1 = STATE_BBOX.get(st, (37.0, 40.0, -102.0, -95.0))
        ba_id, ba_name = rng.choice(OPERATORS)
        fld_id, fld_name = rng.choice(FIELDS)
        gl = rng.randint(600, 4200)
        td = rng.randint(1800, 9500)
        spud = date(2010, 1, 1) + timedelta(days=rng.randint(0, 4200))
        ctx = {
            "uwi": uwi,
            "well_name": f"{rng.choice(['SMITH','JONES','MILLER','BAKER','WEST','NORTH','CARSON','DOYLE'])} {rng.randint(1,36)}-{rng.randint(1,9)}",
            "well_num": f"{rng.randint(1, 12)}",
            "api_num": f"{cty5[:2]}-{cty5[2:]}-{seq[cty5]:05d}",
            # entity FK: the loader hashes the NAME to make ba_id
            "operator_ba_id": ba_name, "current_operator_ba_id": ba_name,
            "operator_name": ba_name,
            "field_id": fld_name,          # entity FK: the loader hashes the name
            "field_name": fld_name,
            "country": "1", "province_state": st, "county": cty5,
            "well_status": rng.choice(["ACTIVE", "INACTIVE", "PLUGGED",
                                       "PRODUCING"]),
            "well_type": rng.choice(["OIL", "GAS", "INJECTION", "DRY"]),
            "onshore_offshore_ind": "ONSHORE",
            "_lat": round(rng.uniform(lat0, lat1), 6),
            "_lon": round(rng.uniform(lon0, lon1), 6),
            "_gl": gl, "_td": td, "_spud": spud,
        }
        ctx["ground_elevation"] = gl
        ctx["kb_elevation"] = gl + rng.randint(8, 30)
        ctx["final_td"] = td
        ctx["spud_date"] = spud.isoformat()
        ctx["completion_date"] = (spud + timedelta(days=rng.randint(20, 120))).isoformat()
        ctx["depth_datum"] = "KB"
        ctx["source"] = "SYNTH"
        wells.append(ctx)

    emit("dv_well", [make_row(wcols, w, rng) for w in wells])

    # dv_field and dv_business_associate are deliberately NOT emitted. They are
    # ENTITY parents, and bulk_dir_loader seeds those itself: Phase 4's "Add"
    # runs _add_sql(), which inserts field_id = SHA1(name) alongside the name.
    # A real source — a scout ticket, a state download — carries "EL DORADO",
    # never an id, so emitting one would model the database's internals rather
    # than the world the data comes from, and would test a path no real load
    # takes. The wells carry names; the loader derives the keys.

    # ── directional surveys: header per deviated well, stations beneath ─────
    hcols, scols = cols("dv_well_dir_srvy_hdr"), cols("dv_well_dir_srvy_sta")
    hdrs, stas = [], []
    for w in wells:
        if rng.random() > 0.35:                 # ~a third are deviated
            continue
        ctx = dict(w)
        ctx["survey_id"] = f"SRVY_{w['uwi']}"
        ctx["dir_srvy_id"] = ctx["survey_id"]
        ctx["survey_type"] = rng.choice(["GYRO", "MWD", "MAGNETIC"])
        if hcols:
            hdrs.append(make_row(hcols, ctx, rng))
        if scols:
            md, incl, azi = 0.0, 0.0, rng.uniform(0, 360)
            for n in range(1, rng.randint(12, 40)):
                md += rng.uniform(80, 130)
                if md > w["_td"]:
                    break
                incl = min(92.0, incl + rng.uniform(0, 3.5))
                azi = (azi + rng.uniform(-4, 4)) % 360
                s = dict(ctx)
                s["station_id"] = f"{ctx['survey_id']}_{n:03d}"
                s["station_no"] = n
                s["md"] = round(md, 2)
                s["measured_depth"] = round(md, 2)
                s["inclination"] = round(incl, 2)
                s["azimuth"] = round(azi, 2)
                s["tvd"] = round(md * math.cos(math.radians(incl)), 2)
                stas.append(make_row(scols, s, rng))
    emit("dv_well_dir_srvy_hdr", hdrs)
    emit("dv_well_dir_srvy_sta", stas)

    # ── logs and curves ────────────────────────────────────────────────────
    lcols, ccols = cols("dv_well_log"), cols("dv_well_log_curve")
    logs, curves = [], []
    for w in wells:
        for run in range(1, rng.randint(1, 3)):
            ctx = dict(w)
            ctx["log_id"] = f"LOG_{w['uwi']}_{run}"
            ctx["log_type"] = rng.choice(["TRIPLE COMBO", "OPEN HOLE",
                                          "CASED HOLE", "MUD LOG"])
            ctx["run_num"] = str(run)
            ctx["top_depth"] = round(rng.uniform(200, 600), 1)
            ctx["base_depth"] = round(w["_td"] - rng.uniform(0, 200), 1)
            ctx["depth_ouom"] = "FT"
            ctx["null_value"] = "-999.25"
            ctx["file_format"] = "LAS"
            ctx["service_company_ba_id"] = rng.choice(
                ["BA_SCHLUMBERGER", "BA_HALLIBURTON", "BA_BAKER"])
            if lcols:
                logs.append(make_row(lcols, ctx, rng))
            if ccols:
                for mnem, descr, unit in rng.sample(CURVES,
                                                    rng.randint(6, len(CURVES))):
                    c = dict(ctx)
                    c["curve_id"] = f"{ctx['log_id']}_{mnem}"
                    c["mnemonic"] = mnem
                    c["mnemonic_alias"] = mnem
                    # Column naming varies by schema — dv_well_log_curve may
                    # call it curve_mnemonic rather than mnemonic. Set both so
                    # the real curve name lands whichever the table uses,
                    # instead of falling through to the type placeholder.
                    c["curve_mnemonic"] = mnem
                    c["curve_name"] = mnem
                    c["curve_description"] = descr
                    c["curve_unit"] = unit
                    c["min_value"] = round(rng.uniform(0, 20), 3)
                    c["max_value"] = round(rng.uniform(40, 260), 3)
                    curves.append(make_row(ccols, c, rng))
    emit("dv_well_log", logs)
    emit("dv_well_log_curve", curves)

    # ── formation tops, then intervals derived FROM them ───────────────────
    # dv_strat_interval has a COMPOSITE FK (uwi, strat_unit_id, interp_id) to
    # dv_well_formation_top, so an interval may only name a pick that exists.
    # Building both tables from the same STRAT list but drawing depths twice
    # gives each pass a different set of surviving picks — the same "one logical
    # set, drawn twice" fault as the production tables. The picks are made ONCE
    # here and both tables read from them.
    picks_by_well = {}
    for w in wells:
        picks, prev = [], 0.0
        for code, name, nominal in STRAT:
            if nominal >= w["_td"] * 0.95:
                break
            d = nominal * rng.uniform(0.85, 1.15)
            if d <= prev:
                continue
            prev = d
            picks.append({"strat_unit_id": code, "strat_name": name,
                          "formation_name": name, "formation": name,
                          "strat_name_set_id": "KANSAS_LEXICON",
                          "strat_name_set": "KANSAS_LEXICON",
                          "interp_id": "TOP", "depth_ouom": "FT",
                          "top": round(d, 1)})
        picks_by_well[w["uwi"]] = picks

    tcols = cols("dv_well_formation_top")
    if tcols:
        rows = []
        for w in wells:
            for p in picks_by_well[w["uwi"]]:
                t = dict(w)
                t.update(p)
                t["top_depth"] = p["top"]
                t["pick_depth"] = p["top"]
                t["base_depth"] = round(p["top"] + rng.uniform(20, 260), 1)
                rows.append(make_row(tcols, t, rng))
        emit("dv_well_formation_top", rows)

    icols = cols("dv_strat_interval")
    if icols:
        rows = []
        for w in wells:
            ps = picks_by_well[w["uwi"]]
            for i, p in enumerate(ps):
                t = dict(w)
                t.update(p)
                # An interval runs from its own pick down to the next one —
                # which is what a stratigraphic interval IS, and it keeps the
                # tops and intervals telling the same story about the well.
                t["top_depth"] = p["top"]
                t["base_depth"] = (ps[i + 1]["top"] if i + 1 < len(ps)
                                   else round(p["top"] + 200, 1))
                rows.append(make_row(icols, t, rng))
        emit("dv_strat_interval", rows)

    # ── core, completions, tests, production ───────────────────────────────
    simple = [
        ("dv_well_core",       0.15, lambda w, i: {
            "core_id": f"CORE_{w['uwi']}_{i}", "core_num": str(i),
            "top_depth": round(rng.uniform(2000, w["_td"]), 1)}),
        ("dv_well_completion", 0.70, lambda w, i: {
            "completion_id": f"COMP_{w['uwi']}_{i}",
            "completion_obs_no": i,
            "top_depth": round(rng.uniform(1500, w["_td"]), 1)}),
        ("dv_well_dst",        0.20, lambda w, i: {
            "test_id": f"DST_{w['uwi']}_{i}", "test_num": str(i),
            "dst_num": str(i),
            "top_depth": round(rng.uniform(2000, w["_td"]), 1)}),
        ("dv_well_perforation", 0.55, lambda w, i: {
            "perforation_id": f"PERF_{w['uwi']}_{i}",
            "top_depth": round(rng.uniform(1800, w["_td"]), 1)}),
    ]
    for tbl, prob, extra in simple:
        tcols = cols(tbl)
        if not tcols:
            continue
        rows = []
        for w in wells:
            if rng.random() > prob:
                continue
            for i in range(1, rng.randint(2, 4)):
                ctx = dict(w)
                ctx.update(extra(w, i))
                rows.append(make_row(tcols, ctx, rng))
        emit(tbl, rows)

    # Production. The producing set is decided ONCE, before either table is
    # built. Drawing it separately per table — which is what a loop over both
    # names does — gives volumes for one set of wells and entities for another,
    # so every volume row references an entity that was never created. The FK
    # stage then reports thousands of unresolved parents and it looks like a
    # loader problem.
    producing = [w for w in wells if rng.random() <= 0.6]
    ent_id = {w["uwi"]: f"PE_{w['uwi']}" for w in producing}

    ecols = cols("dv_prod_entity")
    if ecols:
        rows = []
        for w in producing:
            ctx = dict(w)
            ctx["prod_entity_id"] = ent_id[w["uwi"]]
            ctx["entity_id"] = ent_id[w["uwi"]]
            ctx["prod_entity_type"] = "WELL"
            ctx["entity_type"] = "WELL"
            rows.append(make_row(ecols, ctx, rng))
        emit("dv_prod_entity", rows)

    vcols = cols("dv_prod_volume")
    if vcols:
        rows = []
        for w in producing:
            q0 = rng.uniform(200, 4000)
            start = w["_spud"] + timedelta(days=120)
            for m in range(rng.randint(12, 60)):
                d = start + timedelta(days=30 * m)
                q = q0 * math.exp(-0.035 * m)      # exponential decline
                ctx = dict(w)
                ctx["prod_entity_id"] = ent_id[w["uwi"]]
                ctx["entity_id"] = ent_id[w["uwi"]]
                ctx["production_date"] = d.isoformat()
                ctx["prod_date"] = d.isoformat()
                ctx["period_date"] = d.isoformat()
                ctx["oil_volume"] = round(q, 1)
                ctx["gas_volume"] = round(q * rng.uniform(0.5, 6), 1)
                ctx["water_volume"] = round(q * rng.uniform(0.2, 9), 1)
                ctx["volume_ouom"] = "BBL"
                rows.append(make_row(vcols, ctx, rng))
        emit("dv_prod_volume", rows)

    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate synthetic petroleum data with real API codes")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--schema", default=SRC_SCHEMA)
    ap.add_argument("--wells", type=int, default=200)
    ap.add_argument("--out", default="synth_out")
    ap.add_argument("--seed", type=int, default=42,
                    help="RNG seed — same seed gives the same dataset")
    ap.add_argument("--states", default=",".join(DEFAULT_STATES),
                    help="API state codes, e.g. 15,42,35 (Kansas, Texas, Okla)")
    ap.add_argument("--area-seed",
                    help="path to seed_dbo_area_country_state_county.csv, so "
                         "county codes match PPDM's area rows")
    ap.add_argument("--list", action="store_true",
                    help="list dv_ tables and their column counts, then exit")
    a = ap.parse_args()

    from dataview.core.schema_introspect import make_engine
    engine = make_engine(a.server, a.database)

    with engine.connect() as conn:
        if a.list:
            for t in list_tables(conn, a.schema):
                print(f"   {t:34} {len(table_columns(conn, t, a.schema)):>3} cols")
            return 0

        states = [s.strip().zfill(2) for s in a.states.split(",") if s.strip()]
        counties = load_counties(a.area_seed, states)
        src = "PPDM area seed" if a.area_seed and os.path.exists(a.area_seed) \
            else "FALLBACK (codes will NOT match PPDM areas)"
        print(f"-- {len(counties)} counties across {len(states)} state(s) · {src}")
        print(f"-- {a.wells} wells -> {a.out}")
        written = generate(conn, a.out, a.wells, counties, a.seed)

    print(f"-- {sum(written.values()):,} row(s) across "
          f"{len(written)} file(s) in {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
