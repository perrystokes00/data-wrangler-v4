"""
catalog_docs.py
===============
Document lookup for the mapping app, straight off file_catalog.GLOBAL_FILE_CATALOG
using the identity columns the pipeline now stamps per document:

    UWI14        — every document that resolved to a well
    SURVEY_NAME  — every document that resolved to a seismic survey

No FILE_WELL_HEADER join: the catalog IS the index now, so one well (or survey)
returns ALL its documents — LAS + scout + completion + dir-survey, etc.

API
    list_documents(engine, uwi14=..., survey_name=..., doc_type=None) -> DataFrame
    render_documents(engine, uwi14=..., survey_name=..., key="...")   -> None  (Streamlit)

uwi14 / survey_name each accept a single value OR a list (for multi-select trays).
"""
import os
import sys
import subprocess

_GFC_COLS = None  # cached resolved column names for GLOBAL_FILE_CATALOG


def _columns(engine, schema, table):
    from sqlalchemy import text
    with engine.connect() as c:
        return [r[0] for r in c.execute(text("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t
            ORDER BY ORDINAL_POSITION"""), {"s": schema, "t": table})]


def _pick(cols, *wanted):
    up = {c.upper(): c for c in cols}
    for w in wanted:
        if w.upper() in up:
            return up[w.upper()]
    return None


def _cat_cols(engine):
    """Resolve (and cache) the catalog's real column names once — defensive, so a
    differently-named FILE_NAME / DOC_TYPE doesn't break the read."""
    global _GFC_COLS
    if _GFC_COLS is not None:
        return _GFC_COLS
    cols = _columns(engine, "file_catalog", "GLOBAL_FILE_CATALOG")
    _GFC_COLS = {
        "inv":   _pick(cols, "INVENTORY_ID"),
        "name":  _pick(cols, "FILE_NAME", "FILENAME", "NAME"),
        "path":  _pick(cols, "FILE_PATH", "FILEPATH", "PATH"),
        "ext":   _pick(cols, "FILE_EXT", "EXTENSION", "EXT"),
        "type":  _pick(cols, "DOC_TYPE"),
        "ready": _pick(cols, "CATALOG_READINESS", "READINESS"),
        "uwi":   _pick(cols, "UWI14"),
        "srvy":  _pick(cols, "SURVEY_NAME"),
        "vault": _pick(cols, "VAULT_PATH"),
    }
    return _GFC_COLS


def _as_list(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple, set)):
        vals = [str(x).strip() for x in v if str(x).strip()]
        return vals or None
    s = str(v).strip()
    return [s] if s else None


def list_documents(engine, uwi14=None, survey_name=None, doc_type=None):
    """DataFrame of catalog documents for the given well UWI14(s) and/or survey
    name(s). Columns: inventory_id, file_name, file_path, file_ext, doc_type,
    readiness, uwi14, survey_name. Empty DataFrame if nothing matches."""
    import pandas as pd
    from sqlalchemy import text, bindparam

    c = _cat_cols(engine)
    if not c["inv"] or not (c["uwi"] or c["srvy"]):
        return pd.DataFrame()

    uwis = _as_list(uwi14)
    srvys = _as_list(survey_name)
    if not uwis and not srvys:
        return pd.DataFrame()

    # base-table columns are aliased 'g'; well_name/line_name come from the header
    # tables (LEFT JOINed below on INVENTORY_ID) so the grid can show them.
    sel = [
        f"g.{c['inv']} AS inventory_id",
        (f"g.{c['name']} AS file_name"   if c["name"]  else "NULL AS file_name"),
        (f"g.{c['path']} AS file_path"   if c["path"]  else "NULL AS file_path"),
        (f"g.{c['ext']} AS file_ext"     if c["ext"]   else "NULL AS file_ext"),
        (f"g.{c['type']} AS doc_type"    if c["type"]  else "NULL AS doc_type"),
        (f"g.{c['ready']} AS readiness"  if c["ready"] else "NULL AS readiness"),
        (f"g.{c['uwi']} AS uwi14"        if c["uwi"]   else "NULL AS uwi14"),
        (f"g.{c['srvy']} AS survey_name" if c["srvy"]  else "NULL AS survey_name"),
        (f"g.{c['vault']} AS vault_path" if c["vault"] else "NULL AS vault_path"),
        "wh.WELL_NAME AS well_name",
        "sh.LINE_NAME AS line_name",
    ]
    where, binds, params = [], [], {}
    if uwis and c["uwi"]:
        where.append(f"g.{c['uwi']} IN :uwis")
        binds.append(bindparam("uwis", expanding=True))
        params["uwis"] = uwis
    if srvys and c["srvy"]:
        where.append(f"g.{c['srvy']} IN :srvys")
        binds.append(bindparam("srvys", expanding=True))
        params["srvys"] = srvys
    if not where:
        return pd.DataFrame()

    # LEFT JOIN header tables for WELL_NAME / LINE_NAME (guarded via a subquery so
    # a missing table yields NULLs rather than an error).
    sql = (f"SELECT {', '.join(sel)} "
           f"FROM file_catalog.GLOBAL_FILE_CATALOG g "
           f"LEFT JOIN file_catalog.FILE_WELL_HEADER wh ON wh.INVENTORY_ID = g.{c['inv']} "
           f"LEFT JOIN file_catalog.FILE_SEIS_HEADER sh ON sh.INVENTORY_ID = g.{c['inv']} "
           f"WHERE ({' OR '.join(where)})")
    if doc_type and c["type"]:
        sql += f" AND g.{c['type']} = :dt"
        params["dt"] = doc_type

    stmt = text(sql)
    if binds:
        stmt = stmt.bindparams(*binds)
    with engine.connect() as con:
        res = con.execute(stmt, params)
        rows, keys = res.fetchall(), list(res.keys())
    df = pd.DataFrame(rows, columns=keys)
    if not df.empty:
        # Prefer the governed vault copy; fall back to the original path.
        def _open_path(r):
            v = r.get("vault_path")
            if v is not None and str(v).strip():
                return v
            return r.get("file_path")
        df["open_path"] = df.apply(_open_path, axis=1)
    sort_cols = [x for x in ("uwi14", "well_name", "survey_name", "line_name", "file_name") if x in df.columns]
    if sort_cols and not df.empty:
        df = df.sort_values(sort_cols, na_position="last").reset_index(drop=True)
    return df


def _open_native(path):
    try:
        if os.name == "nt":
            os.startfile(path)                       # noqa: Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return None
    except Exception as e:
        return str(e)


def render_documents(engine, uwi14=None, survey_name=None, doc_type=None, key="cat"):
    """Streamlit: list the documents for a well UWI14 or a survey name, each with
    Open (native app, local) + Download. Uses st.container(border=True), NOT
    st.expander, so it nests safely inside other expanders/panels."""
    import streamlit as st

    df = list_documents(engine, uwi14=uwi14, survey_name=survey_name, doc_type=doc_type)
    label = (f"UWI {uwi14}" if uwi14 else f"survey {survey_name}") or "selection"
    if df.empty:
        st.caption(f"No cataloged documents for {label}.")
        return

    st.write(f"**{len(df)}** document(s) for {label}")
    for i, d in enumerate(df.itertuples()):
        p = getattr(d, "open_path", None) or getattr(d, "file_path", None)
        vaulted = bool(getattr(d, "vault_path", None)
                       and str(getattr(d, "vault_path")).strip())
        name = d.file_name or (os.path.basename(p) if p else "(unnamed)")
        meta = " · ".join(x for x in [d.doc_type, d.readiness, (d.file_ext or ""),
                                      ("📦 vault" if vaulted else "")] if x)
        with st.container(border=True):
            c1, c2, c3 = st.columns([6, 1.3, 1.6])
            with c1:
                st.write(f"**{name}**")
                if meta:
                    st.caption(meta)
            with c2:
                if st.button("Open", key=f"{key}_open_{i}", use_container_width=True):
                    if not p or not os.path.exists(p):
                        st.warning("File not found on disk at the recorded path.")
                    else:
                        err = _open_native(p)
                        st.success("Opened.") if err is None else st.error(err)
            with c3:
                if p and os.path.exists(p):
                    try:
                        with open(p, "rb") as fh:
                            st.download_button("Download", fh.read(), file_name=name,
                                               key=f"{key}_dl_{i}",
                                               use_container_width=True)
                    except Exception as e:
                        st.caption(f"unreadable: {str(e)[:40]}")
                else:
                    st.caption("no file")
    st.caption("Open launches locally on the machine running the app; "
               "Download works anywhere.")
