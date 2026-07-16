r"""
vault_copy.py  —  Data Wrangler v3
================================================================================
Copy catalogued files into the vault, filed by the curated UWI14 (wells) or the
survey name (seismic). Runs AFTER enrichment — it keys off
file_catalog.FILE_WELL_HEADER.UWI14, which enrich_file_headers.py curates, NOT
GLOBAL_FILE_CATALOG.MATCHED_UWI (which enrichment never updates).

Layout (international-ready; country defaults to US)
---------------------------------------------------
  Wells   :  <vault>\<COUNTRY>\<STATE>\<UWI14>\<WELL_NAME>\<file>
  Seismic :  <vault>\<COUNTRY>\<STATE>\<2D|3D>\<SURVEY_NAME>\<file>

  COUNTRY  : header COUNTRY column if present, else the reference COUNTRY for that
             UWI14 if present, else --default-country (US). Always upper-cased.
  STATE    : wells — header STATE, else reference PROVINCE_STATE for that UWI14,
             else '_NoState'. Seismic has no UWI key, so it uses the seis header
             STATE only (else '_NoState'). Upper-cased.
  WELL_NAME: header WELL_NAME, else '_NoName'.
  2D|3D    : from FILE_SEIS_HEADER.SEIS_SET_TYPE; anything without a clear 2D/3D
             marker goes to '_UnknownDim' (never guessed).

Qualification
-------------
  Wells   : FILE_WELL_HEADER.UWI14 is a real API key (not NULL, not the all-zeros
            key). The catalog score is ignored — a valid well key is the only test.
  Seismic : FILE_SEIS_HEADER.SURVEY_NAME is non-blank AND the file extension is a
            seismic format (--seis-ext, default segy,sgy,seg,segd,sgd,p190,p111).
            The extension gate keeps mis-catalogued GIS files (.shp/.geojson) out
            of the seismic bucket.

Copies are idempotent: a destination that already exists at the same size is
skipped; a name clash at a different size gets a " (2)" suffix. A source file that
has moved or vanished is reported, not fatal. Path segments are sanitised and
length-capped to stay within Windows limits. --dry-run reports the full plan and
writes the CSV without copying. Unmatched files (no valid UWI14, no survey) are
simply not selected.

Deploy / run
------------
    Deploy-Latest vault_copy.py .              # to app root
    py vault_copy.py --dry-run                 # plan only
    py vault_copy.py                            # copy for real
    py vault_copy.py --default-country CA --vault D:\Vault --limit 50

Requires:  pip install pyodbc
"""
import argparse
import csv
import ntpath
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime

try:
    import pyodbc
except ImportError:
    pyodbc = None

DEFAULT_SERVER = r"PERRY\SQLEXPRESS"
DEFAULT_DB     = "DataView"
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"
DEFAULT_REF    = "WELL_REF.well_ref.well_master_gold"
DEFAULT_VAULT  = r"C:\Bulk\Vault"
ZERO_UWI = "00000000000000"

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')      # chars illegal in a Windows path segment
_MAXSEG = 100                                     # cap a segment to keep paths under MAX_PATH


# ── helpers ───────────────────────────────────────────────────────────────────
def sql_conn(a):
    if pyodbc is None:
        sys.exit("pip install pyodbc")
    return pyodbc.connect(
        f"DRIVER={{{a.odbc_driver}}};SERVER={a.server};DATABASE={a.database};"
        "Trusted_Connection=yes;", autocommit=True)


def say(m):
    print(m, flush=True)


def table_cols(cur, fqtn):
    pre = (fqtn.split(".")[0] + ".sys.columns c") if fqtn.count(".") == 2 else "sys.columns c"
    return {r[0].upper() for r in cur.execute(
        f"SELECT c.name FROM {pre} WHERE c.object_id = OBJECT_ID('{fqtn}')").fetchall()}


def safe_seg(s, fallback, maxlen=_MAXSEG):
    """Sanitise one path segment for Windows: strip illegal chars, collapse
    whitespace, drop trailing dots/spaces, cap length. Empty -> fallback."""
    s = (s or "").strip()
    if not s:
        return fallback
    s = _BAD.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > maxlen:
        s = s[:maxlen].strip(" .")
    return s or fallback


def ext_sql(pathcol):
    """Lower-case file extension (no dot) pulled from a path column, T-SQL side."""
    r = f"REVERSE({pathcol})"
    return f"LOWER(REVERSE(LEFT({r}, CHARINDEX('.', {r}) - 1)))"


def _ext_lower(path):
    """Lower-case file extension without the dot (Python side)."""
    return os.path.splitext(str(path or "").replace("/", "\\"))[1].lstrip(".").lower()


def _year_of(val):
    """First 4-digit 19xx/20xx year found in a date value or string, else None."""
    if val is None:
        return None
    m = re.search(r"(?:19|20)\d{2}", str(val))
    return m.group(0) if m else None


def _dim_norm(d):
    """Normalise a seismic dimension to '2D' / '3D' / None."""
    d = (d or "").upper()
    return "3D" if "3D" in d else "2D" if "2D" in d else None


# Navigation / shot-point extensions that vault under P190\<project> rather
# than under 2D|3D (they describe survey geometry, not the seismic volume).
NAV_EXTS = {"p190", "p90", "p1", "p2", "p3"}


def unique_dest(dst, size):
    """Resolve the destination path. Identical file already there -> skip;
    different file at that name -> append ' (2)', ' (3)', …"""
    if not os.path.exists(dst):
        return dst, "copy"
    try:
        if os.path.getsize(dst) == size:
            return dst, "skip-exists"
    except OSError:
        pass
    stem, ext = os.path.splitext(dst)
    n = 2
    while True:
        cand = f"{stem} ({n}){ext}"
        if not os.path.exists(cand):
            return cand, "rename"
        try:
            if os.path.getsize(cand) == size:
                return cand, "skip-exists"
        except OSError:
            pass
        n += 1


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Copy catalogued files into the vault by country/state/UWI14/well and country/survey.")
    p.add_argument("--server", default=DEFAULT_SERVER)
    p.add_argument("--database", default=DEFAULT_DB)
    p.add_argument("--odbc-driver", default=DEFAULT_DRIVER)
    p.add_argument("--ref", default=DEFAULT_REF, help="3-part well master name (state/country fallback)")
    p.add_argument("--vault", default=DEFAULT_VAULT)
    p.add_argument("--default-country", default="US", help="country folder when none is supplied")
    p.add_argument("--seis-ext", default="segy,sgy,seg,segd,sgd,p190,p111",
                   help="file extensions treated as seismic (comma-separated)")
    p.add_argument("--no-wells", action="store_true")
    p.add_argument("--no-seis", action="store_true")
    p.add_argument("--copy-workers", type=int, default=8,
                   help="parallel threads for the vault file copy (I/O-bound). "
                        "Default 8.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report", default=None)
    p.add_argument("--limit", type=int, default=0, help="cap candidates per category (testing)")
    a = p.parse_args()
    conn = sql_conn(a)
    vault(conn, a)


def _record_vaulted(cur, entries):
    """Record vaulted files in file_catalog.VAULT_FILE via staging + MERGE
    (set-based; never a per-row UPDATE loop). Keyed on INVENTORY_ID, last write
    wins. Returns the number of rows recorded."""
    cur.execute("""
        IF OBJECT_ID('file_catalog.VAULT_FILE','U') IS NULL
        CREATE TABLE file_catalog.VAULT_FILE (
            INVENTORY_ID NVARCHAR(64) NOT NULL PRIMARY KEY,
            CATEGORY     NVARCHAR(10),
            SOURCE_PATH  NVARCHAR(1024),
            VAULT_PATH   NVARCHAR(1024),
            UWI14        NVARCHAR(20),
            SURVEY_NAME  NVARCHAR(255),
            ACTION       NVARCHAR(20),
            VAULTED_AT   DATETIME2 NOT NULL
                CONSTRAINT DF_VAULT_FILE_DT DEFAULT SYSUTCDATETIME()
        );""")
    cur.execute("IF OBJECT_ID('tempdb..#vault_stage') IS NOT NULL "
                "DROP TABLE #vault_stage;")
    cur.execute("""
        CREATE TABLE #vault_stage (
            INVENTORY_ID NVARCHAR(64), CATEGORY NVARCHAR(10),
            SOURCE_PATH NVARCHAR(1024), VAULT_PATH NVARCHAR(1024),
            UWI14 NVARCHAR(20), SURVEY_NAME NVARCHAR(255), ACTION NVARCHAR(20));""")

    data = [(
        e["inv"], e.get("category"),
        (e.get("source") or "")[:1024], (e.get("dest") or "")[:1024],
        ((e.get("uwi14") or "")[:20] or None),
        ((e.get("survey") or "")[:255] or None),
        e.get("action"),
    ) for e in entries]
    try:
        cur.fast_executemany = True
    except Exception:
        pass
    cur.executemany(
        "INSERT INTO #vault_stage (INVENTORY_ID,CATEGORY,SOURCE_PATH,"
        "VAULT_PATH,UWI14,SURVEY_NAME,ACTION) VALUES (?,?,?,?,?,?,?)", data)

    cur.execute("""
        MERGE file_catalog.VAULT_FILE AS t
        USING #vault_stage AS s ON t.INVENTORY_ID = s.INVENTORY_ID
        WHEN MATCHED THEN UPDATE SET
            CATEGORY=s.CATEGORY, SOURCE_PATH=s.SOURCE_PATH, VAULT_PATH=s.VAULT_PATH,
            UWI14=s.UWI14, SURVEY_NAME=s.SURVEY_NAME, ACTION=s.ACTION,
            VAULTED_AT=SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT
            (INVENTORY_ID,CATEGORY,SOURCE_PATH,VAULT_PATH,UWI14,SURVEY_NAME,
             ACTION,VAULTED_AT)
            VALUES (s.INVENTORY_ID,s.CATEGORY,s.SOURCE_PATH,s.VAULT_PATH,s.UWI14,
                    s.SURVEY_NAME,s.ACTION,SYSUTCDATETIME());""")
    cur.execute("DROP TABLE #vault_stage;")
    return len(data)


def vault(conn, a, log=print):
    """Core vault copy — callable from the CLI or the app UI. Reads the catalog
    via `conn` (any DBAPI connection) and copies files on disk. `a` carries the
    CLI attributes (vault, default_country, seis_ext, no_wells, no_seis, ref,
    dry_run, report, limit, server, database). `log` receives progress lines.
    Returns (counts dict, report path)."""
    say = log
    dflt_country = (getattr(a, "default_country", "US") or "US").strip() or "US"
    seis_exts = sorted({re.sub(r'[^a-z0-9]', '', e.strip().lower())
                        for e in (a.seis_ext or '').split(',')} - {''})
    say(f"[CONNECT] {getattr(a, 'server', '?')} / {getattr(a, 'database', '?')}")
    say(f"[VAULT  ] {a.vault}   (default country: {dflt_country.upper()})")
    cur = conn.cursor()
    top = f"TOP {a.limit}" if a.limit else ""

    # Optional restriction to a specific set of files (used by the batch UI's
    # per-row Vault checkbox). INVENTORY_IDs are SHA-1 hex, so safe to inline.
    _invf = getattr(a, "inv_filter", None)
    inv_clause = ""
    if _invf:
        _ids = ",".join("'" + re.sub(r"[^0-9A-Fa-f]", "", str(i)) + "'"
                        for i in _invf if str(i).strip())
        if _ids:
            inv_clause = f" AND g.INVENTORY_ID IN ({_ids})"

    try:
        ref_cols = table_cols(cur, a.ref)
    except Exception as e:
        raise RuntimeError(f"Reference {a.ref} not reachable: {e}")
    whc = table_cols(cur, "file_catalog.FILE_WELL_HEADER")
    if not a.no_wells and "UWI14" not in whc:
        raise RuntimeError("FILE_WELL_HEADER has no UWI14 column — run enrichment first.")

    rows = []   # list of dicts

    # ── wells: any file whose header carries a real UWI14 ────────────────────
    if not a.no_wells:
        h_country = "h.COUNTRY" if "COUNTRY" in whc else "NULL"
        h_well    = "h.WELL_NAME" if "WELL_NAME" in whc else "NULL"
        ref_country = "COUNTRY" in ref_cols
        ref_well    = "WELL_NAME" in ref_cols

        rs_country_sel = (", MAX(CASE WHEN NULLIF(LTRIM(RTRIM(COUNTRY)),'') IS NOT NULL "
                          "THEN COUNTRY END) AS country_name") if ref_country else ""
        rs_well_sel = (", MAX(CASE WHEN NULLIF(LTRIM(RTRIM(WELL_NAME)),'') IS NOT NULL "
                       "THEN WELL_NAME END) AS ref_well") if ref_well else ""
        sel_country = (f"COALESCE(NULLIF(LTRIM(RTRIM({h_country})),''), rs.country_name)"
                       if ref_country else f"NULLIF(LTRIM(RTRIM({h_country})),'')")
        # Prefer the reference well name so every file sharing a UWI lands in ONE
        # folder; fall back to the file's own extracted name.
        sel_well = (f"COALESCE(rs.ref_well, NULLIF(LTRIM(RTRIM({h_well})),''))"
                    if ref_well else h_well)

        say("[WELLS ] selecting files with a valid UWI14…")
        cur.execute(f"""
            SELECT DISTINCT {top} g.INVENTORY_ID, g.FILE_PATH, h.UWI14,
                   {sel_country} AS COUNTRY,
                   COALESCE(NULLIF(LTRIM(RTRIM(h.STATE)),''), rs.state_name) AS STATE,
                   {sel_well} AS WELL_NAME
            FROM file_catalog.FILE_WELL_HEADER h
            JOIN file_catalog.GLOBAL_FILE_CATALOG g ON g.INVENTORY_ID = h.INVENTORY_ID
            LEFT JOIN (
                SELECT UWI14,
                       MAX(CASE WHEN NULLIF(LTRIM(RTRIM(PROVINCE_STATE)),'') IS NOT NULL
                                THEN PROVINCE_STATE END) AS state_name{rs_country_sel}{rs_well_sel}
                FROM {a.ref}
                WHERE UWI_SUSPECT = 0 AND UWI14 <> '{ZERO_UWI}'
                  AND UWI14 IN (SELECT UWI14 FROM file_catalog.FILE_WELL_HEADER
                                WHERE UWI14 IS NOT NULL AND UWI14 <> '{ZERO_UWI}')
                GROUP BY UWI14
            ) rs ON rs.UWI14 = h.UWI14
            WHERE h.UWI14 IS NOT NULL AND h.UWI14 <> '{ZERO_UWI}'
              AND NULLIF(LTRIM(RTRIM(g.FILE_PATH)),'') IS NOT NULL{inv_clause}""")
        for inv, fp, uwi, ctry, st, wn in cur.fetchall():
            rows.append({"cat": "WELL", "src": fp, "uwi": uwi, "inv": inv,
                         "country": ctry, "state": st, "well": wn,
                         "survey": None, "dim": None})
        say(f"[WELLS ] candidate files: {sum(1 for r in rows if r['cat'] == 'WELL'):,}")

    # ── seismic: true seismic files (by extension) with a survey name ────────
    if not a.no_seis:
        shc = table_cols(cur, "file_catalog.FILE_SEIS_HEADER")
        if "SURVEY_NAME" not in shc:
            say("[SEIS  ] FILE_SEIS_HEADER has no SURVEY_NAME — skipping seismic")
        elif not seis_exts:
            say("[SEIS  ] no seismic extensions configured — skipping seismic")
        else:
            s_country = "sh.COUNTRY" if "COUNTRY" in shc else "NULL"
            s_state   = "sh.STATE" if "STATE" in shc else "NULL"
            s_dim     = "sh.SEIS_SET_TYPE" if "SEIS_SET_TYPE" in shc else "NULL"
            s_date    = "sh.SURVEY_DATE" if "SURVEY_DATE" in shc else "NULL"
            ext_list  = ",".join("'" + e + "'" for e in seis_exts)
            say(f"[SEIS  ] selecting seismic files ({', '.join(seis_exts)})…")
            cur.execute(f"""
                SELECT DISTINCT {top} g.INVENTORY_ID, g.FILE_PATH, sh.SURVEY_NAME,
                       NULLIF(LTRIM(RTRIM({s_country})),'') AS COUNTRY,
                       NULLIF(LTRIM(RTRIM({s_state})),'')   AS STATE,
                       NULLIF(LTRIM(RTRIM({s_dim})),'')     AS DIM,
                       {s_date} AS SURVEY_DATE
                FROM file_catalog.FILE_SEIS_HEADER sh
                JOIN file_catalog.GLOBAL_FILE_CATALOG g ON g.INVENTORY_ID = sh.INVENTORY_ID
                WHERE NULLIF(LTRIM(RTRIM(sh.SURVEY_NAME)),'') IS NOT NULL
                  AND NULLIF(LTRIM(RTRIM(g.FILE_PATH)),'') IS NOT NULL
                  AND CHARINDEX('.', REVERSE(g.FILE_PATH)) > 0
                  AND {ext_sql('g.FILE_PATH')} IN ({ext_list}){inv_clause}""")
            for inv, fp, sv, ctry, st, dim, sdate in cur.fetchall():
                rows.append({"cat": "SEIS", "src": fp, "uwi": None, "inv": inv,
                             "country": ctry, "state": st, "well": None,
                             "survey": sv, "dim": dim, "survey_date": sdate})
            say(f"[SEIS  ] candidate files: {sum(1 for r in rows if r['cat'] == 'SEIS'):,}")

    # ── per-survey canonical (dim, year) ────────────────────────────────────
    # Volume files (non-nav) define each survey's dimension and year.  Nav
    # (P190) files usually carry neither in their own header, so they inherit
    # the survey's values and land in the SAME 2D|3D\[year]\PROJECT folder as
    # the volume — keeping the P190 with its survey.
    survey_meta = {}   # SURVEY_NAME (upper) -> {"dim": "2D"/"3D"/None, "year": str/None}
    for r in rows:
        if r["cat"] != "SEIS" or _ext_lower(r["src"]) in NAV_EXTS:
            continue                       # only volume files define a survey
        key = (r["survey"] or "").strip().upper()
        if not key:
            continue
        m  = survey_meta.setdefault(key, {"dim": None, "year": None})
        dn = _dim_norm(r.get("dim"))
        if dn == "3D":
            m["dim"] = "3D"                # prefer 3D over 2D
        elif dn == "2D" and m["dim"] != "3D":
            m["dim"] = "2D"
        y = _year_of(r.get("survey_date"))
        if y and (m["year"] is None or y < m["year"]):
            m["year"] = y                  # earliest known acquisition year

    # ── plan (sequential): dest resolution must be ordered so two files never
    #    claim the same path. `claimed` mirrors unique_dest but also reserves
    #    paths chosen THIS run (not yet on disk), which lets the copies run in
    #    parallel below without racing to the same destination. ────────────────
    report = []
    counts = defaultdict(int)
    claimed = {}                          # lowered path -> size reserved this run
    to_copy = []                          # (src, dst, base, cat, action)

    def _resolve_dest(dst, size):
        def _hit(p):
            pl = p.lower()
            if pl in claimed:
                return claimed[pl]
            if os.path.exists(p):
                try:
                    return os.path.getsize(p)
                except OSError:
                    return -1
            return None                   # free
        h = _hit(dst)
        if h is None:
            claimed[dst.lower()] = size
            return dst, "copy"
        if h == size:
            return dst, "skip-exists"
        stem, ext = os.path.splitext(dst)
        n = 2
        while True:
            cand = f"{stem} ({n}){ext}"
            hc = _hit(cand)
            if hc is None:
                claimed[cand.lower()] = size
                return cand, "rename"
            if hc == size:
                return cand, "skip-exists"
            n += 1

    for r in rows:
        cat, src = r["cat"], r["src"]
        fn = ntpath.basename(src)
        country = safe_seg(r["country"] or dflt_country, dflt_country).upper()
        seis_dim = ""
        state_seg = safe_seg(r["state"], "_NoState").upper()
        if cat == "WELL":
            # Vault\COUNTRY\STATE\UWI14\WELL_NAME — every file sharing the UWI14
            # (logs, PDFs, reports…) lands in this one folder.
            dest_dir = os.path.join(
                a.vault, country, state_seg,
                safe_seg(r["uwi"], "_NoUWI"),
                safe_seg(r["well"], "_NoName"))
        else:
            # All seismic files of a survey — volume AND navigation (P190) —
            # land together under  COUNTRY\STATE\2D|3D\[YEAR]\PROJECT.  Nav
            # files inherit the survey's dim/year (resolved from its volume
            # files); fall back to the file's own header if the survey is
            # nav-only.
            project = safe_seg(r["survey"], "_NoSurvey")
            key  = (r["survey"] or "").strip().upper()
            meta = survey_meta.get(key, {})
            seis_dim = (meta.get("dim") or _dim_norm(r.get("dim"))
                        or "_UnknownDim")
            year = meta.get("year") or _year_of(r.get("survey_date"))
            parts = [a.vault, country, state_seg, seis_dim]
            if year:
                parts.append(year)
            parts.append(project)
            dest_dir = os.path.join(*parts)

        base = {"category": cat, "country": country,
                "state": r["state"] or "",
                "dim": seis_dim, "inv": r.get("inv"),
                "uwi14": (r["uwi"] or "") if cat == "WELL" else "",
                "well_name": (r["well"] or "") if cat == "WELL" else "",
                "survey": (r["survey"] or "") if cat == "SEIS" else "",
                "source": src}

        if not os.path.exists(src):
            counts[(cat, "missing-source")] += 1
            report.append({**base, "dest": "", "action": "missing-source"})
            continue
        try:
            size = os.path.getsize(src)
        except OSError as e:
            counts[(cat, "stat-error")] += 1
            report.append({**base, "dest": "", "action": f"stat-error:{e}"})
            continue

        dst, action = _resolve_dest(os.path.join(dest_dir, fn), size)
        if action != "skip-exists" and not a.dry_run:
            to_copy.append((src, dst, base, cat, action))
        else:
            counts[(cat, action)] += 1
            report.append({**base, "dest": dst, "action": action})

    # ── copy (parallel): file copies are I/O-bound, so a thread pool overlaps
    #    the per-file open/stat/copy/close. Threads (not processes) — no CPU work,
    #    no pickling, safe under the GIL. Destinations are pre-resolved above so
    #    no two threads target the same path. ──────────────────────────────────
    if to_copy:
        from concurrent.futures import ThreadPoolExecutor
        _cw = max(1, int(getattr(a, "copy_workers", 8) or 8))

        def _do_copy(item):
            src, dst, base, cat, action = item
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                return (base, cat, action, dst, None)
            except Exception as e:
                return (base, cat, "error", dst, str(e))

        with ThreadPoolExecutor(max_workers=_cw) as pool:
            for base, cat, action, dst, err in pool.map(_do_copy, to_copy):
                if err:
                    counts[(cat, "error")] += 1
                    report.append({**base, "dest": dst, "action": f"error:{err}"})
                else:
                    counts[(cat, action)] += 1
                    report.append({**base, "dest": dst, "action": action})
        log(f"[vault] copied {len(to_copy):,} file(s) across {_cw} thread(s)")

    # ── report + summary ──────────────────────────────────────────────────────
    rpt = a.report or f"vault_copy_{datetime.now():%Y%m%d_%H%M%S}.csv"
    try:
        with open(rpt, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, extrasaction="ignore", fieldnames=[
                "category", "country", "state", "dim", "uwi14", "well_name", "survey",
                "source", "dest", "action"])
            w.writeheader()
            w.writerows(report)
        rpt_note = os.path.abspath(rpt)
    except Exception as e:
        rpt_note = f"(report not written: {e})"

    # ── record vaulted files in the DB (set-based: stage + MERGE) ──────────────
    vault_n = 0
    if not a.dry_run:
        vaulted = {e["inv"]: e for e in report
                   if e.get("inv") and e.get("dest")
                   and e["action"] in ("copy", "skip-exists", "rename")}
        if vaulted:
            try:
                vault_n = _record_vaulted(cur, list(vaulted.values()))
                say(f"[VAULT  ] recorded {vault_n:,} file(s) in "
                    f"file_catalog.VAULT_FILE")
            except Exception as e:
                say(f"[VAULT  ] DB record skipped: {e}")

    say("\n──────── summary ────────")
    for (cat, action), n in sorted(counts.items()):
        say(f"  {cat:5} {action:16} {n:,}")
    say(f"\n{'(dry run — nothing copied) ' if a.dry_run else ''}Report: {rpt_note}")
    return {(f"{c}/{ac}"): n for (c, ac), n in counts.items()}, rpt_note


if __name__ == "__main__":
    main()
