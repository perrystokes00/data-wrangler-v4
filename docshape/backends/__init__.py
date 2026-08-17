"""
docshape.backends
=================
Where captured rows land. One interface, several platforms.

    from docshape.backends import open_backend

    db = open_backend("duckdb", path="capture.duckdb")
    db = open_backend("mssql", database="DataView_Demo", schema="stg")

The backend is chosen at RUN TIME, not compiled in: the same capture runs
against a file on a laptop or a shared schema on a site install, and the
calling code doesn't change. That is the point — a prospect can be given the
file-based version with no database at all, and the same tool later writes
into their server.
"""
from __future__ import annotations

from docshape.backends.base import (Backend, TEXT, TEXT_LONG, NUMBER, INT,
                                    BIGINT, TIMESTAMP, BOOL, IDENTITY,
                                    LOGICAL_TYPES)

__all__ = ["Backend", "open_backend", "TEXT", "TEXT_LONG", "NUMBER", "INT",
           "BIGINT", "TIMESTAMP", "BOOL", "IDENTITY", "LOGICAL_TYPES"]


def open_backend(kind="duckdb", **kw):
    """Open a backend by name. Imports the driver only when asked for.

    Lazy import matters for the portable case: a laptop install needs duckdb
    and nothing else, and should not fail because pyodbc is absent.
    """
    k = (kind or "duckdb").lower()
    if k in ("duckdb", "duck", "file"):
        from docshape.backends.duck import DuckBackend
        return DuckBackend(**kw)
    if k in ("mssql", "sqlserver", "sql_server"):
        from docshape.backends.mssql import MssqlBackend
        return MssqlBackend(**kw)
    if k in ("oracle", "ora"):
        from docshape.backends.oracle import OracleBackend
        return OracleBackend(**kw)
    if k in ("snowflake", "snow"):
        from docshape.backends.snowflake import SnowflakeBackend
        return SnowflakeBackend(**kw)
    raise ValueError(f"unknown backend {kind!r} "
                     f"(have: duckdb, mssql, oracle, snowflake)")
