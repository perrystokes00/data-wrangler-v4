"""
file_gate.py — decide which files actually need extracting, and record every one in
catalog.GLOBAL_FILE_CATALOG (the pipeline's inventory table — NOT
dataview.dv_global_file_catalog, which is a different, largely unused table).

The check is layered so the expensive part runs rarely:

  1. stat()            size + mtime. If they match the catalog row, the file is UNCHANGED —
                       no bytes are read at all. This is the pre-filter, and it's what makes
                       re-scanning a 500-file folder cheap.
  2. quick hash        SHA-256 over the head + tail (64 KB each) plus the size, stored in
                       `file_hash`. Cheap on a 2 GB DLIS; catches almost every real change.
  3. full hash         SHA-256 over the whole file, stored in `file_hash_full`. Only computed
                       when the quick hash differs from the catalog, so a touched-but-identical
                       file costs one quick read, not a full one.

States returned per file:
  new        — not in the catalog
  changed    — content hash differs from the catalog
  touched    — size/mtime moved but content is identical (mtime bump, re-copy) → no re-extract
  moved      — same content, different path (matched on file_hash_full)
  unchanged  — size+mtime identical → skip

`inventory_id` is the DataView entity convention, SHA1 of the full path in UTF-16-LE, upper
hex — 40 chars, exactly GLOBAL_FILE_CATALOG.INVENTORY_ID's width, and the same convention the
pipeline uses. The dataview tables carry an INVENTORY_ID column, so a loaded row can be traced
back to the file it came from.

Nothing here decides policy: it reports states. The caller chooses to skip or force.
"""
from __future__ import annotations
import os
import hashlib
import datetime

HEAD_TAIL = 64 * 1024          # bytes hashed at each end for the quick hash
CHUNK = 1024 * 1024

SKIP_STATES = ("unchanged", "touched", "moved")   # content already catalogued & loaded

# The real inventory table. It is OWNED BY THE PIPELINE — triage, vaulting, worker claims and
# promotion all live in it (VAULTED, TRIAGE_SCORE, PROC_STATUS, PROC_WORKER, PROMOTED_AT,
# VAULT_PATH...). The loader is a guest here: it may write the file-identity columns it is
# responsible for and NOTHING else. Clobbering a triage decision or a worker claim from a
# directory scan would be a far worse bug than any it prevents.
CAT_SCHEMA = "file_catalog"
CAT_TABLE = "GLOBAL_FILE_CATALOG"

# columns the loader owns; everything else in the table is the pipeline's business
_OWNED = ("FILE_PATH", "FILE_NAME", "FILE_EXT", "FILE_SIZE_KB", "FILE_HASH", "FILE_HASH_FULL",
          "DUPLICATE_GROUP", "MODIFIED_DATE", "ROOT_PATH", "FILE_TYPE_GROUP")


def _cat(schema=None, table=None):
    return f"{schema or CAT_SCHEMA}.{table or CAT_TABLE}"


def catalog_exists(engine, schema=None, table=None):
    """Is the inventory table actually there? Named schemas are easy to get wrong (this one
    is `file_catalog`, not `catalog`), and a raw 208 three layers down doesn't say so."""
    from sqlalchemy import text
    try:
        with engine.connect() as cx:
            r = cx.execute(text(
                "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t"),
                {"s": schema or CAT_SCHEMA, "t": table or CAT_TABLE}).first()
        return bool(r)
    except Exception:
        return False


def inventory_id(full_path):
    """DataView canonical id: SHA1(UPPER(path), UTF-16-LE) — matches SQL Server HASHBYTES."""
    s = str(full_path).upper().strip()
    return hashlib.sha1(s.encode("utf-16-le")).hexdigest().upper()


def quick_hash(path, size=None):
    """SHA-256 of head+tail+size — bounded cost regardless of file size."""
    size = os.path.getsize(path) if size is None else size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(HEAD_TAIL))
        if size > HEAD_TAIL * 2:
            fh.seek(-HEAD_TAIL, os.SEEK_END)
            h.update(fh.read(HEAD_TAIL))
    return h.hexdigest().upper()


def full_hash(path):
    """SHA-256 of the whole file, chunked."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest().upper()


def _as_dt(v):
    """Catalog rows may come back as datetime or as a string (driver/dialect dependent).
    Normalize so the pre-filter compares like with like — a failed compare isn't wrong, it
    just costs an unnecessary hash, but it costs it on EVERY file, every scan."""
    if v is None or isinstance(v, datetime.datetime):
        return v
    s = str(v).strip().replace("T", " ")
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s[:26], f)
        except ValueError:
            pass
    return None


def _same_mtime(prev_mtime, mtime, tol=2.0):
    """True when the catalog's modified_date matches the file's, within a couple of seconds
    (filesystem/driver rounding differs; 2s is the classic FAT/NTFS allowance)."""
    a = _as_dt(prev_mtime)
    if a is None or mtime is None:
        return False
    return abs((a - mtime).total_seconds()) <= tol


def _stat(path):
    st = os.stat(path)
    return (round(st.st_size / 1024.0, 3),
            datetime.datetime.utcfromtimestamp(st.st_mtime).replace(microsecond=0))


def _existing(engine, ids, schema=None, table=None):
    """{INVENTORY_ID: row} for the ids we're about to consider — one round trip."""
    from sqlalchemy import text
    out = {}
    if not ids:
        return out
    ids = list(ids)
    cat = _cat(schema, table)
    with engine.connect() as cx:
        for i in range(0, len(ids), 900):                 # SQL Server parameter ceiling
            chunk = ids[i:i + 900]
            ph = ",".join(f":i{j}" for j in range(len(chunk)))
            rows = cx.execute(text(
                f"SELECT INVENTORY_ID, FILE_PATH, FILE_SIZE_KB, MODIFIED_DATE, FILE_HASH, "
                f"FILE_HASH_FULL, CATALOG_STATUS, PROMOTED_AT "
                f"FROM {cat} WHERE INVENTORY_ID IN ({ph})"),
                {f"i{j}": v for j, v in enumerate(chunk)})
            for r in rows:
                out[r[0]] = {"inventory_id": r[0], "full_path": r[1], "file_size_kb": r[2],
                             "modified_date": r[3], "file_hash": r[4], "file_hash_full": r[5],
                             "catalog_status": r[6], "promoted_at": r[7]}
    return out


def _by_content(engine, hashes, schema=None, table=None):
    """{FILE_HASH_FULL: INVENTORY_ID} — to spot a file that merely moved."""
    from sqlalchemy import text
    out = {}
    hashes = [h for h in hashes if h]
    if not hashes:
        return out
    cat = _cat(schema, table)
    with engine.connect() as cx:
        for i in range(0, len(hashes), 900):
            chunk = hashes[i:i + 900]
            ph = ",".join(f":h{j}" for j in range(len(chunk)))
            rows = cx.execute(text(
                f"SELECT FILE_HASH_FULL, MIN(INVENTORY_ID) FROM {cat} "
                f"WHERE FILE_HASH_FULL IN ({ph}) GROUP BY FILE_HASH_FULL"),
                {f"h{j}": v for j, v in enumerate(chunk)})
            for r in rows:
                out[r[0]] = r[1]
    return out


def _type_group(ext):
    """Coarse grouping for FILE_TYPE_GROUP — only set on INSERT, never overwritten."""
    e = (ext or "").lower()
    if e in (".las", ".dlis", ".lis"):
        return "WELL_LOG"
    if e in (".xml", ".wml"):
        return "WITSML"
    if e == ".pdf":
        return "DOCUMENT"
    if e in (".docx", ".doc", ".odt"):
        return "DOCUMENT"
    if e in (".csv", ".xlsx", ".xlsm", ".xltx", ".xls"):
        return "TABULAR"
    return "OTHER"


def classify(engine, paths, schema=None, root=None, force=False, table=None):
    """→ {path: {inventory_id, state, size_kb, modified_date, file_hash, file_hash_full,
                 loaded_ind, reason}}   — decides, does not act."""
    paths = [p for p in paths if os.path.isfile(p)]
    ids = {p: inventory_id(os.path.abspath(p)) for p in paths}
    known = _existing(engine, set(ids.values()), schema, table)

    out = {}
    for p in paths:
        iid = ids[p]
        size_kb, mtime = _stat(p)
        prev = known.get(iid)
        _p = prev or {}
        rec = {"inventory_id": iid, "size_kb": size_kb, "modified_date": mtime,
               "file_hash": None, "file_hash_full": None,
               # this catalog has no ppdm_loaded_ind — "already loaded" means the pipeline
               # marked it CATALOGED or stamped PROMOTED_AT
               "loaded_ind": ("Y" if (_p.get("promoted_at") or
                                      str(_p.get("catalog_status") or "").upper() == "CATALOGED")
                              else "N")}

        if force:
            rec.update(state="new", reason="force re-extract requested")
            rec["file_hash"] = quick_hash(p, os.path.getsize(p))
            rec["file_hash_full"] = full_hash(p)
            out[p] = rec
            continue

        # 1 ── pre-filter: same path, same size, same mtime → don't even open it
        if prev and prev["file_size_kb"] is not None \
                and abs(float(prev["file_size_kb"]) - size_kb) < 0.001 \
                and _same_mtime(prev["modified_date"], mtime):
            rec.update(state="unchanged", file_hash=prev["file_hash"],
                       file_hash_full=prev["file_hash_full"],
                       reason="size and modified_date match the catalog — not read")
            out[p] = rec
            continue

        # 2 ── quick hash (bounded read)
        rec["file_hash"] = quick_hash(p, os.path.getsize(p))
        if prev and prev["file_hash"] and prev["file_hash"] == rec["file_hash"]:
            rec.update(state="touched", file_hash_full=prev["file_hash_full"],
                       reason="mtime/size moved but head+tail+size identical — content unchanged")
            out[p] = rec
            continue

        # 3 ── full hash (only when the cheap checks disagree)
        rec["file_hash_full"] = full_hash(p)
        if prev:
            rec.update(state="changed" if prev["file_hash_full"] != rec["file_hash_full"]
                       else "touched",
                       reason=("content hash differs from catalog"
                               if prev["file_hash_full"] != rec["file_hash_full"]
                               else "content identical despite size/mtime change"))
        else:
            rec.update(state="new", reason="not in the file catalog")
        out[p] = rec

    # a genuinely new path whose content we've already catalogued = a move/copy
    fresh = {r["file_hash_full"] for r in out.values()
             if r["state"] == "new" and r["file_hash_full"]}
    seen = _by_content(engine, fresh, schema, table)
    for p, r in out.items():
        if r["state"] == "new" and r["file_hash_full"] in seen:
            r.update(state="moved",
                     reason=f"same content already catalogued as {seen[r['file_hash_full']][:12]}…")
    return out


def upsert(engine, decisions, source=None, root=None, schema=None, table=None,
           doc_type_group=None, by=None):
    """Write/refresh ONLY the file-identity columns of catalog.GLOBAL_FILE_CATALOG.

    Set-based: one temp table + one MERGE, never per-row pyodbc.

    The pipeline owns this table. On MATCHED we update the columns in _OWNED and nothing
    else — VAULTED, TRIAGE_*, PROC_*, PROMOTED_AT, CATALOG_STATUS, VAULT_PATH and the rest
    are its state, and a directory scan has no business overwriting them. On NOT MATCHED we
    insert a minimal row: identity, plus the NOT NULLs (SCAN_DATE, ROW_CREATED_DATE,
    ROW_CHANGED_DATE, VAULTED=0) and let the pipeline fill in its own columns later.

    `source` / `by` are accepted and ignored — this table has no such columns. (Kept so the
    caller doesn't need to know which catalog it's talking to.)

    Returns (n_rows, note).
    """
    from sqlalchemy import text
    if not decisions:
        return 0, None
    cat = _cat(schema, table)
    rows = []
    for p_, r in decisions.items():
        ap = os.path.abspath(p_)
        ext = (os.path.splitext(ap)[1] or "").lower()
        rows.append({
            "inventory_id": r["inventory_id"], "file_path": ap[:1000],
            "file_name": os.path.basename(ap)[:500], "file_ext": ext[:20],
            "file_size_kb": r["size_kb"], "file_hash": r["file_hash"],
            "file_hash_full": r["file_hash_full"], "duplicate_group": r["file_hash_full"],
            "modified_date": r["modified_date"],
            "file_type_group": (doc_type_group or _type_group(ext))[:50],
            "root_path": (root or os.path.dirname(ap))[:500],
        })
    with engine.begin() as cx:
        cx.execute(text("""
            CREATE TABLE #gfc (
              inventory_id nvarchar(40), file_path nvarchar(1000), file_name nvarchar(500),
              file_ext nvarchar(20), file_size_kb numeric(18,3), file_hash nvarchar(64),
              file_hash_full nvarchar(64), duplicate_group nvarchar(64),
              modified_date datetime2, file_type_group nvarchar(50), root_path nvarchar(500))"""))
        cx.execute(text("""
            INSERT INTO #gfc (inventory_id, file_path, file_name, file_ext, file_size_kb,
                              file_hash, file_hash_full, duplicate_group, modified_date,
                              file_type_group, root_path)
            VALUES (:inventory_id, :file_path, :file_name, :file_ext, :file_size_kb,
                    :file_hash, :file_hash_full, :duplicate_group, :modified_date,
                    :file_type_group, :root_path)"""), rows)
        cx.execute(text(f"""
            MERGE {cat} AS t
            USING #gfc AS s ON t.INVENTORY_ID = s.inventory_id
            WHEN MATCHED THEN UPDATE SET
                t.FILE_PATH = s.file_path, t.FILE_NAME = s.file_name, t.FILE_EXT = s.file_ext,
                t.FILE_SIZE_KB = s.file_size_kb,
                t.FILE_HASH = COALESCE(s.file_hash, t.FILE_HASH),
                t.FILE_HASH_FULL = COALESCE(s.file_hash_full, t.FILE_HASH_FULL),
                t.DUPLICATE_GROUP = COALESCE(s.duplicate_group, t.DUPLICATE_GROUP),
                t.MODIFIED_DATE = s.modified_date, t.ROOT_PATH = s.root_path,
                t.FILE_TYPE_GROUP = COALESCE(t.FILE_TYPE_GROUP, s.file_type_group),
                t.ROW_CHANGED_DATE = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT
                (INVENTORY_ID, FILE_PATH, FILE_NAME, FILE_EXT, FILE_SIZE_KB, FILE_HASH,
                 FILE_HASH_FULL, DUPLICATE_GROUP, MODIFIED_DATE, SCAN_DATE, ROOT_PATH,
                 FILE_TYPE_GROUP, VAULTED, ROW_CREATED_DATE, ROW_CHANGED_DATE)
            VALUES
                (s.inventory_id, s.file_path, s.file_name, s.file_ext, s.file_size_kb,
                 s.file_hash, s.file_hash_full, s.duplicate_group, s.modified_date,
                 SYSUTCDATETIME(), s.root_path, s.file_type_group, 0,
                 SYSUTCDATETIME(), SYSUTCDATETIME());"""))
        cx.execute(text("DROP TABLE #gfc"))
    return len(rows), None


def get_identity(engine, inventory_ids, schema=None, table=None):
    """{INVENTORY_ID: UWI14} for ids that have a REMEMBERED operator assignment.

    Read back at scan time so a UWI typed once is never typed again — the assignment is keyed
    to INVENTORY_ID, so it survives re-runs, Reset, and a restart. It does NOT survive the
    file being moved or renamed (INVENTORY_ID is path-derived); classify() spots that case as
    `moved` via FILE_HASH_FULL, but re-linking the identity across a move is not wired.
    """
    from sqlalchemy import text
    ids = [i for i in (inventory_ids or []) if i]
    if not ids:
        return {}
    cat = _cat(schema, table)
    out = {}
    try:
        with engine.connect() as cx:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ",".join(f":i{j}" for j in range(len(chunk)))
                rs = cx.execute(text(
                    f"SELECT INVENTORY_ID, UWI14 FROM {cat} "
                    f"WHERE UWI14 IS NOT NULL AND INVENTORY_ID IN ({ph})"),
                    {f"i{j}": v for j, v in enumerate(chunk)})
                for r in rs:
                    if r[1] and str(r[1]).strip():
                        out[str(r[0])] = str(r[1]).strip()
    except Exception:
        return {}                       # no saved identities is a valid answer; never fatal
    return out


def set_identity(engine, assignments, schema=None, table=None):
    """Remember an OPERATOR'S UWI assignment: {inventory_id: uwi14}. Returns rows written.

    Writes UWI14 AND NOTHING ELSE. Set-based, one temp table + one UPDATE JOIN.

    This deliberately widens the loader's remit. _OWNED says the loader may write the
    file-identity columns and nothing else, because clobbering a triage decision or a worker
    claim from a directory scan would be worse than any bug it prevents. UWI14 is not in
    _OWNED. It is written here anyway, on purpose, because an operator typing a UWI into the
    gate is the strongest identity claim in the system — stronger than anything inferred —
    and re-typing it on every run is how it gets typed wrong.

    The boundary is kept narrow: MATCHED_UWI and MATCH_METHOD are score_file's inference and
    are NOT touched; TRIAGE_*, PROC_*, VAULTED, PROMOTED_AT are the pipeline's and are NOT
    touched. Two writers, one column, one rule: the operator wins over an extracted UWI14,
    because they are looking at the document and the parser was guessing.

    Pass uwi="" to FORGET an assignment — needed, because a UWI assigned while eyeballing how
    the data flows would otherwise persist silently into every future run of that file.
    """
    from sqlalchemy import text
    rows = [{"inventory_id": k, "uwi14": (v or "").strip()[:14] or None}
            for k, v in (assignments or {}).items() if k]
    if not rows:
        return 0
    cat = _cat(schema, table)
    with engine.begin() as cx:
        cx.execute(text("CREATE TABLE #idn (inventory_id nvarchar(40), uwi14 char(14))"))
        cx.execute(text("INSERT INTO #idn (inventory_id, uwi14) VALUES (:inventory_id, :uwi14)"),
                   rows)
        cx.execute(text(f"""
            UPDATE t SET t.UWI14 = s.uwi14, t.ROW_CHANGED_DATE = SYSUTCDATETIME()
            FROM {cat} AS t
            JOIN #idn AS s ON t.INVENTORY_ID = s.inventory_id"""))
        cx.execute(text("DROP TABLE #idn"))
    return len(rows)


def mark_loaded(engine, inventory_ids, schema=None, catalog_table=None, table=None):
    """Record that a file's rows reached dataview. This table has no ppdm_loaded_ind — the
    equivalents are CATALOG_STATUS and PROMOTED_AT. Still only our own columns."""
    from sqlalchemy import text
    ids = [i for i in (inventory_ids or []) if i]
    if not ids:
        return 0
    cat = _cat(schema, table)
    n = 0
    with engine.begin() as cx:
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            ph = ",".join(f":i{j}" for j in range(len(chunk)))
            params = {f"i{j}": v for j, v in enumerate(chunk)}
            setc = ["CATALOG_STATUS = 'CATALOGED'", "PROMOTED_AT = SYSUTCDATETIME()",
                    "ROW_CHANGED_DATE = SYSUTCDATETIME()"]
            if catalog_table:
                setc.append("CATALOG_TABLE = :ct")
                params["ct"] = str(catalog_table)[:100]
            r = cx.execute(text(f"UPDATE {cat} SET {', '.join(setc)} "
                                f"WHERE INVENTORY_ID IN ({ph})"), params)
            n += r.rowcount or 0
    return n


def summary(decisions):
    """{state: count} for the UI."""
    out = {}
    for r in decisions.values():
        out[r["state"]] = out.get(r["state"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def to_extract(decisions, force=False):
    """Paths that actually need extracting."""
    if force:
        return list(decisions)
    return [p for p, r in decisions.items()
            if r["state"] not in SKIP_STATES or r.get("loaded_ind") != "Y"]
