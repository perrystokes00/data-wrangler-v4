"""
docshape.backends.mssql
=======================
SQL Server backend — for a site install where capture lands in a shared
database rather than a file.

Differences from DuckDB that actually matter:

  * identifiers quote with [brackets], not "quotes"
  * a SCHEMA is meaningful, and the store's tables should not sit in dbo —
    the default here is `stg`, created on demand
  * NVARCHAR everywhere rather than VARCHAR: document text is whatever the
    document contained, and a degree sign or an en dash in a header should not
    be a collation problem
  * bulk insert goes through pyodbc's fast_executemany, which is one to two
    orders of magnitude faster than the same statement without it

TEXT is NVARCHAR(255) and TEXT_LONG is NVARCHAR(MAX). The split exists because
MAX columns can't be indexed and are stored off-row; using it for every label
would make the review tables unpleasant to query.
"""
from __future__ import annotations

from docshape.backends.base import (Backend, TEXT, TEXT_LONG, NUMBER, INT,
                                    BIGINT, TIMESTAMP, BOOL, IDENTITY)


class MssqlBackend(Backend):
    name = "mssql"
    types = {
        TEXT: "NVARCHAR(255)", TEXT_LONG: "NVARCHAR(MAX)", NUMBER: "FLOAT",
        INT: "INT", BIGINT: "BIGINT", TIMESTAMP: "DATETIME2(7)",
        BOOL: "BIT", IDENTITY: "BIGINT IDENTITY(1,1)",
    }
    batch_size = 5000

    def __init__(self, server=r"localhost\SQLEXPRESS", database="DataView_Demo",
                 schema="stg", driver="ODBC Driver 17 for SQL Server",
                 conn_str=None, trusted=True):
        import pyodbc
        self.schema = schema
        if conn_str is None:
            auth = "Trusted_Connection=yes;" if trusted else ""
            conn_str = (f"DRIVER={{{driver}}};SERVER={server};"
                        f"DATABASE={database};{auth}")
        self.con = pyodbc.connect(conn_str, autocommit=True)
        self._ensure_schema()

    def _ensure_schema(self):
        """Create the schema if absent.

        CREATE SCHEMA must be the first statement in its batch, hence the
        EXEC wrapper. And a parameter marker cannot appear inside EXEC's
        string expression — "IF SCHEMA_ID(?) ... EXEC('CREATE SCHEMA ' +
        QUOTENAME(?))" fails to parse. So the name is validated as an
        identifier and interpolated: it comes from our own configuration, not
        from a document, and validating is what keeps that safe.
        """
        import re
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.schema or ""):
            raise ValueError(
                f"schema name {self.schema!r} is not a plain identifier")
        cur = self.con.cursor()
        cur.execute(f"IF SCHEMA_ID('{self.schema}') IS NULL "
                    f"EXEC('CREATE SCHEMA [{self.schema}]')")
        cur.close()

    # -- identifiers ------------------------------------------------------- #
    def quote(self, ident):
        return "[" + str(ident).replace("]", "]]") + "]"

    def qualified(self, table):
        return f"{self.quote(self.schema)}.{self.quote(table)}"

    # -- schema ------------------------------------------------------------ #
    def table_exists(self, table):
        cur = self.con.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.tables t "
                    "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                    "WHERE s.name = ? AND t.name = ?", self.schema, table)
        n = cur.fetchone()[0]
        cur.close()
        return bool(n)

    def columns(self, table):
        cur = self.con.cursor()
        cur.execute("SELECT c.name FROM sys.columns c "
                    "WHERE c.object_id = OBJECT_ID(?)",
                    f"{self.schema}.{table}")
        out = {r[0].upper() for r in cur.fetchall()}
        cur.close()
        return out

    def create_table(self, table, coldefs):
        cols = [f"{self.quote(c)} {self.type_of(t)}"
                + (" NOT NULL" if t == IDENTITY else " NULL")
                for c, t in coldefs]
        cur = self.con.cursor()
        cur.execute(f"CREATE TABLE {self.qualified(table)} ({', '.join(cols)})")
        cur.close()

    def add_columns(self, table, coldefs):
        # SQL Server won't add an IDENTITY column to an existing table, and a
        # store that grew one later doesn't need it — skip rather than fail.
        have = self.columns(table)
        missing = [(c, t) for c, t in coldefs
                   if c.upper() not in have and t != IDENTITY]
        if missing:
            cols = ", ".join(f"{self.quote(c)} {self.type_of(t)} NULL"
                             for c, t in missing)
            cur = self.con.cursor()
            cur.execute(f"ALTER TABLE {self.qualified(table)} ADD {cols}")
            cur.close()
        return [c for c, _t in missing]

    # -- data -------------------------------------------------------------- #
    def insert(self, table, rows):
        rows = [r for r in (rows or []) if r]
        if not rows:
            return 0
        keys, tuples, ignored = self._align(table, rows)
        if not keys:
            return 0
        self.last_ignored = ignored
        cols = ", ".join(self.quote(k) for k in keys)
        marks = ", ".join("?" * len(keys))
        sql = f"INSERT INTO {self.qualified(table)} ({cols}) VALUES ({marks})"
        cur = self.con.cursor()
        # Without this every row is a separate round trip. With it the driver
        # batches them into a handful.
        cur.fast_executemany = True
        n = 0
        for i in range(0, len(tuples), self.batch_size):
            chunk = tuples[i:i + self.batch_size]
            cur.executemany(sql, chunk)
            n += len(chunk)
        cur.close()
        return n

    def delete_where(self, table, column, value):
        cur = self.con.cursor()
        cur.execute(f"DELETE FROM {self.qualified(table)} "
                    f"WHERE {self.quote(column)} = ?", value)
        n = cur.rowcount
        cur.close()
        return max(n, 0)

    def query(self, sql, params=None):
        cur = self.con.cursor()
        cur.execute(sql, *(params or []))
        out = cur.fetchall()
        cur.close()
        return out

    def execute(self, sql, params=None):
        cur = self.con.cursor()
        cur.execute(sql, *(params or []))
        cur.close()

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
