#!/usr/bin/env python3
"""
triage_inventory.py — Stage 1 of the File Triage & Promotion pipeline.

Set-based, idempotent identity enrichment + value tiering over the file
inventory. Safe to run frequently (only fills blanks, re-tiers every pass).

Steps:
  1. ensure triage columns exist on GLOBAL_FILE_CATALOG
  2. normalize UWI14 (digits-only API14) on FILE_WELL_HEADER
  3. cross-fill identity from the inventory's own sibling files
  4. reference-fill from WELL_MASTER (name<-UWI only). The reverse direction,
     UWI<-name, was removed 18 Aug 2026: 14.3s of a 24s run to fill 0, seven
     runs running, and unreliable by nature — see the note in reference_fill().
  5. score / tier each file (HIGH / REVIEW / LOW / REJECT)

Nothing is overwritten — only blank values are filled, and every fill records
IDENTITY_SOURCE for audit. Use --dry-run to see counts without writing.

    python triage_inventory.py --dry-run
    python triage_inventory.py                 # apply
    python triage_inventory.py --since 2026-06-01   # incremental (changed/new)
"""
import argparse
import re
import sys
from datetime import datetime

GFC = "file_catalog.GLOBAL_FILE_CATALOG"
FWH = "file_catalog.FILE_WELL_HEADER"
FSH = "file_catalog.FILE_SEIS_HEADER"
ZERO14 = "00000000000000"


# ── identity normalization ───────────────────────────────────────────────────
def norm14(uwi):
    """Canonical digits-only API14, or None when the value isn't an API number.

    - surrogate keys containing letters (e.g. 'KGS_1001234') -> None (skip;
      they are trustworthy UWIs but not reference-matchable by UWI14)
    - strips separators, zero-pads API10/API12 to API14, truncates longer
    """
    s = str(uwi or "").strip()
    if not s or re.search(r"[A-Za-z]", s):
        return None
    d = re.sub(r"\D", "", s)
    if len(d) < 10:
        return None
    u = d[:14] if len(d) >= 14 else d.ljust(14, "0")
    return None if u == ZERO14 else u


def name_norm(s):
    """Match WELL_REF.WELL_MASTER.NAME_NORM exactly: trim, collapse internal
    whitespace to single spaces, uppercase. Punctuation is preserved."""
    s = re.sub(r"\s+", " ", str(s or "").strip()).upper()
    return s or None


# ── connection ────────────────────────────────────────────────────────────────
def sql_conn(a):
    import pyodbc
    cs = (f"DRIVER={{{a.driver}}};SERVER={a.server};"
          f"DATABASE={a.database};Trusted_Connection=yes;")
    return pyodbc.connect(cs, autocommit=False)


def say(m):
    print(m, flush=True)


# ── schema ────────────────────────────────────────────────────────────────────
TRIAGE_COLS = [
    ("VALUE_TIER", "varchar(10)"),
    ("TRIAGE_SCORE", "int"),
    ("TRIAGE_REASON", "varchar(200)"),
    ("LAST_TRIAGED_AT", "datetime2"),
]


def _existing_cols(cur, qualified):
    """UPPER column names of a schema.table via one INFORMATION_SCHEMA read."""
    parts = qualified.split(".")
    sch, tb = (parts[-2], parts[-1]) if len(parts) >= 2 else ("dbo", parts[-1])
    cur.execute("SELECT UPPER(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", sch, tb)
    return {r[0] for r in cur.fetchall()}


def ensure_columns(cur):
    # Reflect existing columns/indexes ONCE, then run ALTER/CREATE only for what's
    # missing — instead of an IF COL_LENGTH / IF NOT EXISTS round-trip per object.
    # On steady state (everything present) this is 3 reads and zero DDL, vs the
    # ~9 round-trips the per-object checks cost (each ~0.3s on a busy server).
    gfc_have = _existing_cols(cur, GFC)
    for col, typ in TRIAGE_COLS:
        if col.upper() not in gfc_have:
            cur.execute(f"ALTER TABLE {GFC} ADD {col} {typ} NULL")
    fwh_have = _existing_cols(cur, FWH)
    for col, typ in (("UWI14", "varchar(14)"),
                     ("IDENTITY_SOURCE", "varchar(30)"),
                     ("NAME_NORM", "varchar(200)")):
        if col.upper() not in fwh_have:
            cur.execute(f"ALTER TABLE {FWH} ADD {col} {typ} NULL")
    cur.execute("SELECT name FROM sys.indexes WHERE object_id = OBJECT_ID(?)", FWH)
    idx = {r[0] for r in cur.fetchall() if r[0]}
    if "IX_FWH_NAME_NORM" not in idx:
        cur.execute(f"CREATE INDEX IX_FWH_NAME_NORM ON {FWH} (NAME_NORM)")
    if "IX_FWH_UWI14" not in idx:
        cur.execute(f"CREATE INDEX IX_FWH_UWI14 ON {FWH} (UWI14)")


# ── step 2: normalize UWI14 + NAME_NORM (python normalize + staged UPDATE) ─────
def normalize_identity(conn, dry):
    cur = conn.cursor()
    cur.execute(f"""
        SELECT h.INVENTORY_ID,
               COALESCE(NULLIF(LTRIM(RTRIM(h.UWI)),''), g.MATCHED_UWI) AS src_uwi,
               h.WELL_NAME AS wn
        FROM {FWH} h
        JOIN {GFC} g ON g.INVENTORY_ID = h.INVENTORY_ID
        WHERE (NULLIF(LTRIM(RTRIM(h.UWI14)),'') IS NULL
               AND COALESCE(NULLIF(LTRIM(RTRIM(h.UWI)),''), g.MATCHED_UWI) IS NOT NULL)
           OR (NULLIF(LTRIM(RTRIM(h.NAME_NORM)),'') IS NULL
               AND NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NOT NULL)
    """)
    # set-based normalize: do UWI14 + NAME_NORM entirely in T-SQL (reusing enrich's
    # u14_sql/nn_sql) instead of a Python round-trip + executemany. ~90x faster.
    from dataview.file_catalog.enrich_file_headers import u14_sql as _u14, nn_sql as _nn
    _src_uwi = "COALESCE(NULLIF(LTRIM(RTRIM(h.UWI)),''), g.MATCHED_UWI)"
    _where = (f"(NULLIF(LTRIM(RTRIM(h.UWI14)),'') IS NULL AND {_src_uwi} IS NOT NULL) "
              f"OR (NULLIF(LTRIM(RTRIM(h.NAME_NORM)),'') IS NULL "
              f"AND NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NOT NULL)")
    if dry:
        n = cur.execute(f"SELECT COUNT(*) FROM {FWH} h "
                        f"JOIN {GFC} g ON g.INVENTORY_ID = h.INVENTORY_ID "
                        f"WHERE {_where}").fetchone()[0]
        say(f"  [dry] would set UWI14/NAME_NORM on {n} file(s)")
        return n
    cur.execute(f"""
        UPDATE h SET
            UWI14     = CASE WHEN NULLIF(LTRIM(RTRIM(h.UWI14)),'') IS NULL
                             THEN {_u14(_src_uwi)} ELSE h.UWI14 END,
            NAME_NORM = CASE WHEN NULLIF(LTRIM(RTRIM(h.NAME_NORM)),'') IS NULL
                             THEN {_nn('h.WELL_NAME')} ELSE h.NAME_NORM END
        FROM {FWH} h JOIN {GFC} g ON g.INVENTORY_ID = h.INVENTORY_ID
        WHERE {_where}
    """)
    nrow = cur.rowcount
    say(f"  set UWI14/NAME_NORM on {nrow} file(s)")
    return nrow


# ── step 3: cross-fill from the inventory itself ──────────────────────────────
def cross_fill(conn, dry):
    cur = conn.cursor()
    total = 0

    # 3a. well name from a sibling with the same UWI14 (most-frequent name)
    name_by_uwi = f"""
        WITH ranked AS (
            SELECT UWI14, WELL_NAME,
                   COUNT(*) AS c,
                   ROW_NUMBER() OVER (PARTITION BY UWI14
                                      ORDER BY COUNT(*) DESC, LEN(WELL_NAME) DESC)
                                  AS rn
            FROM {FWH}
            WHERE NULLIF(LTRIM(RTRIM(WELL_NAME)),'') IS NOT NULL
              AND NULLIF(UWI14,'') IS NOT NULL
            GROUP BY UWI14, WELL_NAME
        ), best AS (SELECT UWI14, WELL_NAME FROM ranked WHERE rn = 1)
    """
    if dry:
        cur.execute(name_by_uwi + f"""
            SELECT COUNT(*) FROM {FWH} h JOIN best b ON h.UWI14 = b.UWI14
            WHERE NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NULL""")
        n = cur.fetchone()[0]
        say(f"  [dry] would cross-fill {n} well name(s) from inventory")
    else:
        cur.execute(name_by_uwi + f"""
            UPDATE h SET WELL_NAME = b.WELL_NAME, IDENTITY_SOURCE = 'inv-xfill-name'
            FROM {FWH} h JOIN best b ON h.UWI14 = b.UWI14
            WHERE NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NULL""")
        n = cur.rowcount
        say(f"  cross-filled {n} well name(s) from inventory")
    total += n

    # 3b. UWI14 from a sibling with the same exact name — only if that name maps
    #     to exactly one UWI across the inventory (collision guard)
    uwi_by_name = f"""
        WITH uniq AS (
            SELECT NAME_NORM, MIN(UWI14) AS UWI14
            FROM {FWH}
            WHERE NULLIF(UWI14,'') IS NOT NULL
              AND NULLIF(LTRIM(RTRIM(NAME_NORM)),'') IS NOT NULL
            GROUP BY NAME_NORM
            HAVING COUNT(DISTINCT UWI14) = 1
        )
    """
    if dry:
        cur.execute(uwi_by_name + f"""
            SELECT COUNT(*) FROM {FWH} h JOIN uniq u ON h.NAME_NORM = u.NAME_NORM
            WHERE NULLIF(h.UWI14,'') IS NULL""")
        n = cur.fetchone()[0]
        say(f"  [dry] would cross-fill {n} UWI(s) from inventory")
    else:
        cur.execute(uwi_by_name + f"""
            UPDATE h SET UWI14 = u.UWI14, IDENTITY_SOURCE = 'inv-xfill-uwi'
            FROM {FWH} h JOIN uniq u ON h.NAME_NORM = u.NAME_NORM
            WHERE NULLIF(h.UWI14,'') IS NULL""")
        n = cur.rowcount
        say(f"  cross-filled {n} UWI(s) from inventory")
    total += n
    return total


# ── step 4: reference fill from WELL_MASTER ───────────────────────────────────
def reference_fill(conn, ref, dry):
    cur = conn.cursor()
    total = 0

    # 4a. name <- UWI14 (safe, deterministic)
    if dry:
        cur.execute(f"""
            SELECT COUNT(*) FROM {FWH} h
            JOIN {ref} r ON h.UWI14 = r.UWI14
            WHERE NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NULL
              AND NULLIF(LTRIM(RTRIM(r.WELL_NAME)),'') IS NOT NULL""")
        n = cur.fetchone()[0]
        say(f"  [dry] would fill {n} well name(s) from reference")
    else:
        cur.execute(f"""
            UPDATE h SET WELL_NAME = r.WELL_NAME, IDENTITY_SOURCE = 'ref-name-by-uwi'
            FROM {FWH} h JOIN {ref} r ON h.UWI14 = r.UWI14
            WHERE NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NULL
              AND NULLIF(LTRIM(RTRIM(r.WELL_NAME)),'') IS NOT NULL""")
        n = cur.rowcount
        say(f"  filled {n} well name(s) from reference")
    total += n

    # 4b / 4c. UWI14 <- WELL_NAME: REMOVED 18 Aug 2026.
    #
    # These were the second copy of the name->UWI lookup deleted from
    # enrich_file_headers pass 1 the same day, and the more expensive of the
    # two. MEASURED on a 20-file load: this step cost 14.3s of a 24s run — the
    # single largest cost in the pipeline — and across seven runs it filled
    # ZERO on every one:
    #
    #     filled 0 UWI(s) from reference (exact+unique)          x7
    #     filled 0 UWI(s) from reference (TD/spud corroborated)  x7
    #
    # 4b aggregated the ENTIRE 4.03M-row gold master on every run
    # (GROUP BY NAME_NORM HAVING COUNT(*) = 1) to find globally unique names;
    # 4c ran a correlated NOT EXISTS back over the same 4M rows per candidate.
    # Neither is incremental — the cost is paid in full whether one file was
    # loaded or a thousand, and it grows with the reference, not the batch.
    #
    # Correctness, not just cost: matching a well by NAME is unreliable against
    # a national master. Enrich pass 1's two lifetime writes were both wrong —
    # a Kansas log (STATE 'KS', COUNTY '15031') assigned a Utah UWI because
    # 'JONES 27-9' also names a well in San Juan County, UT. 4b's uniqueness
    # test and 4c's TD/spud corroboration are stronger tests than pass 1 used,
    # but they answer the same unreliable question, and a wrong UWI is worse
    # than a blank one: blank holds the row in cat_* where it is visible.
    #
    # 4a above is KEPT and is the safe direction — it fills a blank NAME from
    # the reference keyed on UWI14, an identifier, not a name.
    #
    # Identity still resolves from the file's own header (bcp_capture), from
    # the inventory cross-fill (step 3, which matched 17 UWIs on the same run
    # this step matched 0), and from the filename (step 4b-path below).
    return total


# ── step 4b: derive identity from the file path/name ──────────────────────────
_NAME_GROUPS = ("Well Log", "PDF", "Office")   # where a path well-name is plausible


def path_fill(conn, dry):
    """Last-resort identity from the path/name for files that still lack it:
      • wells   -> UWI14 from filename/folder; failing that, a well NAME (only
                   for document types, and only if it has both letters & digits)
      • seismic -> survey name + year + 2D/3D from the filename
    Everything written here is a CANDIDATE tagged IDENTITY_SOURCE='path-*', so it
    lands in the REVIEW queue for light editing rather than auto-promoting.
    Set-based: parse in Python, write back via temp tables (upsert: UPDATE
    existing header rows + INSERT for files that never had one)."""
    try:
        from dataview.core import path_identity as pid
    except Exception:
        say("  path_identity unavailable — skipping path fill")
        return 0
    import uuid as _uuid
    import re as _re
    cur = conn.cursor()

    # ── A) WELLS — UWI, then well name ───────────────────────────────────────
    cur.execute(f"""
        SELECT g.INVENTORY_ID, g.FILE_PATH, g.FILE_TYPE_GROUP
        FROM {GFC} g
        LEFT JOIN {FWH} h ON h.INVENTORY_ID = g.INVENTORY_ID
        WHERE g.FILE_TYPE_GROUP NOT LIKE 'Seismic%'
          AND g.FLAG_DELETE = 0
          AND NULLIF(LTRIM(RTRIM(g.MATCHED_UWI)),'') IS NULL
          AND NULLIF(LTRIM(RTRIM(h.UWI)),'')   IS NULL
          AND NULLIF(LTRIM(RTRIM(h.UWI14)),'') IS NULL
    """)
    well = []   # (inv, u14|None, name|None, name_norm|None, src, hid)
    for inv, path, ftg in cur.fetchall():
        u14, src = pid.uwi14_from_path(path)
        name = None
        if not u14 and (ftg or "") in _NAME_GROUPS:
            cand = pid.wellname_from_path(path)
            if (cand and any(c.isalpha() for c in cand)
                    and any(c.isdigit() for c in cand)):
                name = cand[:200]
        if u14 or name:
            hid = _uuid.uuid5(_uuid.NAMESPACE_URL, str(inv)).hex.upper()
            tag = f"path-{src}" if u14 else "path-name"
            well.append((str(inv), u14, name,
                         name_norm(name) if name else None, tag, hid))

    n_well = 0
    if well and not dry:
        cur.execute("IF OBJECT_ID('tempdb..#pfw') IS NOT NULL DROP TABLE #pfw")
        cur.execute("CREATE TABLE #pfw (inv varchar(64) PRIMARY KEY, "
                    "u14 varchar(14), nm varchar(200), nn varchar(200), "
                    "src varchar(40), hid varchar(64))")
        cur.fast_executemany = True
        cur.executemany("INSERT INTO #pfw (inv,u14,nm,nn,src,hid) "
                        "VALUES (?,?,?,?,?,?)", well)
        cur.execute(f"""
            UPDATE h SET
                UWI       = COALESCE(p.u14, h.UWI),
                UWI14     = COALESCE(p.u14, h.UWI14),
                WELL_NAME = COALESCE(NULLIF(h.WELL_NAME,''), p.nm),
                NAME_NORM = COALESCE(NULLIF(h.NAME_NORM,''), p.nn),
                IDENTITY_SOURCE = p.src
            FROM {FWH} h JOIN #pfw p ON h.INVENTORY_ID = p.inv
        """)
        cur.execute(f"""
            INSERT INTO {FWH} (WELL_HEADER_ID, INVENTORY_ID, UWI, UWI14,
                               WELL_NAME, NAME_NORM, IDENTITY_SOURCE,
                               EXTRACTED_DATE, EXTRACTED_BY)
            SELECT p.hid, p.inv, p.u14, p.u14, p.nm, p.nn, p.src,
                   GETUTCDATE(), 'path-fill'
            FROM #pfw p
            WHERE NOT EXISTS (SELECT 1 FROM {FWH} h WHERE h.INVENTORY_ID = p.inv)
        """)
        # mirror only the UWI into the catalog (a name alone isn't a match)
        cur.execute(f"""
            UPDATE g SET MATCHED_UWI = p.u14
            FROM {GFC} g JOIN #pfw p ON g.INVENTORY_ID = p.inv
            WHERE p.u14 IS NOT NULL
        """)
        cur.execute("DROP TABLE #pfw")
        n_well = len(well)

    # ── B) SEISMIC — survey name + year + 2D/3D ──────────────────────────────
    cur.execute(f"""
        SELECT g.INVENTORY_ID, g.FILE_PATH
        FROM {GFC} g
        LEFT JOIN {FSH} s ON s.INVENTORY_ID = g.INVENTORY_ID
        WHERE g.FILE_TYPE_GROUP LIKE 'Seismic%'
          AND g.FLAG_DELETE = 0
          AND NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)),'') IS NULL
    """)
    seis = []   # (inv, survey, year|None, dim|None, hid)
    for inv, path in cur.fetchall():
        survey = pid.survey_from_path(path)
        if not survey:
            continue
        ym = _re.search(r"(19|20)\d{2}", path or "")
        year = ym.group(0) if ym else None
        low = (path or "").lower()
        dim = "3D" if "3d" in low else ("2D" if "2d" in low else None)
        hid = _uuid.uuid5(_uuid.NAMESPACE_URL, str(inv) + "_s").hex.upper()
        seis.append((str(inv), survey[:255], year, dim, hid))

    n_seis = 0
    if seis and not dry:
        cur.execute("IF OBJECT_ID('tempdb..#pfs') IS NOT NULL DROP TABLE #pfs")
        cur.execute("CREATE TABLE #pfs (inv varchar(64) PRIMARY KEY, "
                    "sn varchar(255), yr varchar(8), dim varchar(8), "
                    "hid varchar(64))")
        cur.fast_executemany = True
        cur.executemany("INSERT INTO #pfs (inv,sn,yr,dim,hid) VALUES (?,?,?,?,?)",
                        seis)
        cur.execute(f"""
            UPDATE s SET
                SURVEY_NAME   = COALESCE(NULLIF(s.SURVEY_NAME,''), p.sn),
                SURVEY_DATE   = COALESCE(NULLIF(s.SURVEY_DATE,''), p.yr),
                SEIS_SET_TYPE = COALESCE(NULLIF(s.SEIS_SET_TYPE,''), p.dim)
            FROM {FSH} s JOIN #pfs p ON s.INVENTORY_ID = p.inv
        """)
        cur.execute(f"""
            INSERT INTO {FSH} (SEIS_HEADER_ID, INVENTORY_ID, SURVEY_NAME,
                               SURVEY_DATE, SEIS_SET_TYPE, EXTRACTED_DATE,
                               EXTRACTED_BY)
            SELECT p.hid, p.inv, p.sn, p.yr, p.dim, GETUTCDATE(), 'path-fill'
            FROM #pfs p
            WHERE NOT EXISTS (SELECT 1 FROM {FSH} s WHERE s.INVENTORY_ID = p.inv)
        """)
        cur.execute("DROP TABLE #pfs")
        n_seis = len(seis)

    if dry:
        say(f"  [dry] would path-fill {len(well)} well + {len(seis)} seismic")
        return len(well) + len(seis)
    say(f"  path-filled {n_well} well (uwi/name) + {n_seis} seismic "
        "(survey/year/dim) from path")
    return n_well + n_seis


# ── step 5: score / tier ──────────────────────────────────────────────────────
# HIGH   = a resolvable UWI (UWI alone is trustworthy) — or a seismic survey
# REVIEW = a name but no confident UWI
# REJECT = on the bad-file blocklist
# LOW    = nothing usable
TIER_SQL = f"""
    UPDATE g SET
        VALUE_TIER = CASE
            WHEN g.FLAG_DELETE = 1 THEN 'REJECT'
            -- a content/manual UWI is trustworthy; a path-derived one is a
            -- candidate that must be confirmed in review first
            WHEN (NULLIF(h.UWI14,'') IS NOT NULL
                  OR NULLIF(LTRIM(RTRIM(h.UWI)),'') IS NOT NULL)
                 AND ISNULL(h.IDENTITY_SOURCE,'') NOT LIKE 'path%' THEN 'HIGH'
            WHEN NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)),'') IS NOT NULL
                 OR g.FILE_TYPE_GROUP LIKE 'Seismic%' THEN 'HIGH'
            -- path-derived UWI, or any well name without a confident UWI
            WHEN NULLIF(h.UWI14,'') IS NOT NULL
                 OR NULLIF(LTRIM(RTRIM(h.UWI)),'') IS NOT NULL
                 OR NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NOT NULL THEN 'REVIEW'
            ELSE 'LOW' END,
        CATALOG_READINESS = CASE
            WHEN g.FLAG_DELETE = 1 THEN 'SKIPPED'
            -- A REJECTION MUST SURVIVE A RE-TRIAGE. Marking a file bad stamps
            -- CATALOG_READINESS='SKIPPED' and does NOT set FLAG_DELETE, so
            -- preserving only the flag un-rejected everything rejected through
            -- the UI. MEASURED 23 Aug: all 8 SKIPPED files had FLAG_DELETE
            -- NULL, and this CASE would have returned every one of them to
            -- READY/REVIEW and back into the pipeline -- including files
            -- rejected minutes earlier for being unreadable.
            -- catalog_readiness._CASE already preserves SKIPPED by name; two
            -- modules owning adjacent halves of one column must agree, and
            -- they did not.
            WHEN g.CATALOG_READINESS = 'SKIPPED' THEN 'SKIPPED'
            WHEN g.CATALOG_READINESS IN ('CATALOGED','PROMOTED')
                 THEN g.CATALOG_READINESS
            -- .las carries its own UWI in the header; _stage_extract skips .las
            -- (capture writes FILE_WELL_HEADER), so there's no header row at triage
            -- time. Mark READY — the UWI resolves at capture. (LAS carries its own UWI.)
            --
            -- ONLY WHILE THAT IS STILL TRUE, and the condition now says so.
            -- The justification is "there is no header row YET"; once capture
            -- has written one, the answer is in front of us and a shortcut
            -- must not overrule it. Six .las files sat at READY with
            -- FILE_WELL_HEADER.UWI and UWI14 both NULL, which is a confident
            -- wrong value: nothing stages, nothing promotes, and no gate can
            -- name a reason because holds are derived from cat_* rows that
            -- were never written. Triple-invisible -- READY says fine, the
            -- backlog says nothing, and the file never moves.
            -- They fall through to REVIEW now, which is exactly what the
            -- IDENTITY_CONFIDENCE arm beside this one already says about a
            -- well name with no confident UWI.
            WHEN LOWER(g.FILE_EXT) = '.las' AND h.INVENTORY_ID IS NULL THEN 'READY'
            WHEN (NULLIF(h.UWI14,'') IS NOT NULL
                  OR NULLIF(LTRIM(RTRIM(h.UWI)),'') IS NOT NULL)
                 AND ISNULL(h.IDENTITY_SOURCE,'') NOT LIKE 'path%' THEN 'READY'
            WHEN NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)),'') IS NOT NULL
                 OR g.FILE_TYPE_GROUP LIKE 'Seismic%' THEN 'READY'
            WHEN g.CATALOG_READINESS = 'AWAITING_UWI' THEN 'AWAITING_UWI'
            WHEN NULLIF(h.UWI14,'') IS NOT NULL
                 OR NULLIF(LTRIM(RTRIM(h.UWI)),'') IS NOT NULL
                 OR NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NOT NULL THEN 'REVIEW'
            ELSE 'NEEDS_UWI' END,
        LAST_TRIAGED_AT = SYSUTCDATETIME()
    FROM {GFC} g
    LEFT JOIN {FWH} h ON h.INVENTORY_ID = g.INVENTORY_ID
    LEFT JOIN {FSH} s ON s.INVENTORY_ID = g.INVENTORY_ID
"""


def score_tier(conn, dry):
    cur = conn.cursor()
    if dry:
        cur.execute(f"""
            SELECT
              SUM(CASE WHEN g.FLAG_DELETE=1 THEN 1 ELSE 0 END) AS reject,
              SUM(CASE WHEN g.FLAG_DELETE=0 AND (
                    ((NULLIF(h.UWI14,'') IS NOT NULL
                      OR NULLIF(LTRIM(RTRIM(h.UWI)),'') IS NOT NULL)
                     AND ISNULL(h.IDENTITY_SOURCE,'') NOT LIKE 'path%')
                    OR NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)),'') IS NOT NULL
                    OR g.FILE_TYPE_GROUP LIKE 'Seismic%') THEN 1 ELSE 0 END) AS high,
              SUM(CASE WHEN g.FLAG_DELETE=0 AND NOT (
                    ((NULLIF(h.UWI14,'') IS NOT NULL
                      OR NULLIF(LTRIM(RTRIM(h.UWI)),'') IS NOT NULL)
                     AND ISNULL(h.IDENTITY_SOURCE,'') NOT LIKE 'path%')
                    OR NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)),'') IS NOT NULL
                    OR g.FILE_TYPE_GROUP LIKE 'Seismic%')
                    AND (NULLIF(h.UWI14,'') IS NOT NULL
                         OR NULLIF(LTRIM(RTRIM(h.UWI)),'') IS NOT NULL
                         OR NULLIF(LTRIM(RTRIM(h.WELL_NAME)),'') IS NOT NULL)
                    THEN 1 ELSE 0 END) AS review
            FROM {GFC} g
            LEFT JOIN {FWH} h ON h.INVENTORY_ID = g.INVENTORY_ID
            LEFT JOIN {FSH} s ON s.INVENTORY_ID = g.INVENTORY_ID""")
        r = cur.fetchone()
        say(f"  [dry] tiers -> HIGH {r.high or 0} · REVIEW {r.review or 0} · "
            f"REJECT {r.reject or 0}")
        return 0
    cur.execute(TIER_SQL)
    n = cur.rowcount
    say(f"  tiered {n} file(s)")
    return n


def summary(conn):
    cur = conn.cursor()
    cur.execute(f"SELECT VALUE_TIER, COUNT(*) FROM {GFC} GROUP BY VALUE_TIER")
    say("\nTier summary:")
    for tier, c in sorted(cur.fetchall(), key=lambda x: str(x[0])):
        say(f"  {tier or '(untiered)':<12} {c}")


DEFAULT_REF = "WELL_REF.well_ref.well_master_public_v2"


def run_all(conn, ref=DEFAULT_REF, dry=False, log=say):
    """Full triage pass on an open DBAPI connection: ensure columns, normalize
    identity, cross-fill from inventory, reference-fill, score/tier. Set-based
    and idempotent, so it's safe to call at the end of every crawl — each new
    batch can resolve identities (and parked AWAITING_UWI wells) for files
    already in the inventory. Commits on success; rolls back on dry-run.
    Returns the {tier: count} summary dict."""
    import time
    cur = conn.cursor()
    _t1 = time.perf_counter()
    log("1) ensure triage columns")
    ensure_columns(cur)
    conn.commit()                       # schema persists even on a dry run
    log(f"     ({time.perf_counter() - _t1:.1f}s)")

    def _step(label, fn, *args):
        t = time.perf_counter()
        log(label)
        r = fn(*args)
        log(f"     ({time.perf_counter() - t:.1f}s)")
        return r

    _step("2) normalize UWI14 + NAME_NORM", normalize_identity, conn, dry)
    _step("3) cross-fill from inventory", cross_fill, conn, dry)
    _step("4) reference fill", reference_fill, conn, ref, dry)
    _step("4b) path / filename fill", path_fill, conn, dry)
    _step("5) score / tier", score_tier, conn, dry)

    if dry:
        conn.rollback()
        log("(dry run — no changes written)")
        return {}
    conn.commit()
    cur.execute(f"SELECT VALUE_TIER, COUNT(*) FROM {GFC} GROUP BY VALUE_TIER")
    return {(t or "(untiered)"): n for t, n in cur.fetchall()}


def run_all_engine(engine, ref=DEFAULT_REF, dry=False, log=say):
    """Convenience for SQLAlchemy callers (the crawl, the Streamlit page).
    Opens a short-lived raw connection, runs the full pass, returns the tier
    summary dict. One line to fold triage into a crawl:

        from dataview.file_catalog import triage_inventory
        triage_inventory.run_all_engine(engine)
    """
    conn = engine.raw_connection()
    try:
        return run_all(conn, ref, dry, log)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="File inventory triage (Stage 1).")
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
    ap.add_argument("--ref", default="WELL_REF.well_ref.well_master_public_v2")
    ap.add_argument("--dry-run", action="store_true",
                    help="report counts without writing")
    a = ap.parse_args()

    say(f"Triage {'(dry run) ' if a.dry_run else ''}"
        f"@ {datetime.now():%Y-%m-%d %H:%M:%S}  {a.server}/{a.database}")
    try:
        conn = sql_conn(a)
    except Exception as e:
        say(f"Connection failed: {e}")
        return 2
    try:
        run_all(conn, a.ref, a.dry_run)
        if not a.dry_run:
            summary(conn)
        return 0
    except Exception as e:
        conn.rollback()
        say(f"\nTriage failed (rolled back): {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
