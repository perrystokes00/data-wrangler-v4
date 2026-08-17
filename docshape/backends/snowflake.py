"""
docshape.backends.snowflake
===========================
Snowflake backend, via snowflake-connector-python.

WHAT'S DIFFERENT, and why it matters more here than elsewhere:

  * UNQUOTED IDENTIFIERS FOLD TO UPPERCASE. Quoting makes them case-sensitive
    forever, so a quoted lower-case column must always be quoted. We quote
    everything for consistency with the other backends — be aware that means
    `SELECT top_md FROM ...` fails by hand and `SELECT "top_md"` works.
  * NO ENFORCED CONSTRAINTS. Snowflake accepts PRIMARY KEY and NOT NULL
    syntax and does not enforce most of it. A capture store doesn't rely on
    them, but don't assume a uniqueness guarantee that isn't there.
  * ROW-BY-ROW INSERTS ARE SLOW AND EXPENSIVE. Every statement is a warehouse
    operation. executemany is fine for the hundreds-of-rows capture this store
    produces, but a bulk migration should go through PUT + COPY INTO or
    write_pandas instead — see `insert_bulk` below.
  * AUTOINCREMENT rather than IDENTITY syntax.

TIMESTAMP maps to TIMESTAMP_NTZ deliberately: capture timestamps are local
wall-clock from the machine doing the reading, and attaching a timezone to
them would imply a precision that isn't there.

UNTESTED AGAINST A LIVE ACCOUNT. Verify warehouse/role/database context before
relying on it — a connection that works but has no warehouse set will create
tables and then fail on the first insert.
"""
from __future__ import annotations

from docshape.backends.base import (Backend, TEXT, TEXT_LONG, NUMBER, INT,
                                    BIGINT, TIMESTAMP, BOOL, IDENTITY)


class SnowflakeBackend(Backend):
    name = "snowflake"
    types = {
        TEXT: "VARCHAR(255)", TEXT_LONG: "VARCHAR", NUMBER: "FLOAT",
        INT: "NUMBER(10,0)", BIGINT: "NUMBER(19,0)",
        TIMESTAMP: "TIMESTAMP_NTZ", BOOL: "BOOLEAN",
        IDENTITY: "NUMBER AUTOINCREMENT",
    }
    batch_size = 10000

    def __init__(self, account=None, user=None, password=None, warehouse=None,
                 database=None, schema="PUBLIC", role=None, conn=None, **kw):
        import snowflake.connector as sc
        self.con = conn or sc.connect(
            account=account, user=user, password=password,
            warehouse=warehouse, database=database, schema=schema,
            role=role, **kw)
        self.schema = schema
        self.database = database

    # -- identifiers ------------------------------------------------------- #
    def quote(self, ident):
        return '"' + str(ident).replace('"', '""') + '"'

    def qualified(self, table):
        parts = [p for p in (self.database, self.schema) if p]
        return ".".join([self.quote(p) for p in parts] + [self.quote(table)])

    # -- schema ------------------------------------------------------------ #
    def table_exists(self, table):
        cur = self.con.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (self.schema.upper(), table.upper()))
            return bool(cur.fetchone()[0])
        finally:
            cur.close()

    def columns(self, table):
        cur = self.con.cursor()
        try:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (self.schema.upper(), table.upper()))
            return {r[0].upper() for r in cur.fetchall()}
        finally:
            cur.close()

    def create_table(self, table, coldefs):
        cols = [f"{self.quote(c)} {self.type_of(t)}" for c, t in coldefs]
        cur = self.con.cursor()
        cur.execute(f"CREATE TABLE IF NOT EXISTS {self.qualified(table)} "
                    f"({', '.join(cols)})")
        cur.close()

    def add_columns(self, table, coldefs):
        have = self.columns(table)
        missing = [(c, t) for c, t in coldefs
                   if c.upper() not in have and t != IDENTITY]
        cur = self.con.cursor()
        for col, logical in missing:
            # Snowflake takes one column per ALTER.
            cur.execute(f"ALTER TABLE {self.qualified(table)} "
                        f"ADD COLUMN {self.quote(col)} {self.type_of(logical)}")
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
        marks = ", ".join(["%s"] * len(keys))
        sql = f"INSERT INTO {self.qualified(table)} ({cols}) VALUES ({marks})"
        cur = self.con.cursor()
        n = 0
        for i in range(0, len(tuples), self.batch_size):
            chunk = tuples[i:i + self.batch_size]
            cur.executemany(sql, chunk)
            n += len(chunk)
        cur.close()
        return n

    def insert_bulk(self, table, rows):
        """Stage-and-COPY path for large loads, via write_pandas.

        Row-by-row inserts are a warehouse operation each; past roughly ten
        thousand rows this is the right call. Falls back to insert() when
        pandas isn't available, so callers need not choose.
        """
        try:
            import pandas as pd
            from snowflake.connector.pandas_tools import write_pandas
        except ImportError:
            return self.insert(table, rows)
        rows = [r for r in (rows or []) if r]
        if not rows:
            return 0
        keys, tuples, ignored = self._align(table, rows)
        self.last_ignored = ignored
        df = pd.DataFrame(tuples, columns=keys)
        ok, _chunks, nrows, _out = write_pandas(
            self.con, df, table.upper(), schema=self.schema,
            database=self.database, quote_identifiers=True)
        return nrows if ok else 0

    def delete_where(self, table, column, value):
        cur = self.con.cursor()
        cur.execute(f"DELETE FROM {self.qualified(table)} "
                    f"WHERE {self.quote(column)} = %s", (value,))
        n = cur.rowcount
        cur.close()
        return max(n or 0, 0)

    def query(self, sql, params=None):
        cur = self.con.cursor()
        try:
            cur.execute(sql, params or None)
            return cur.fetchall()
        finally:
            cur.close()

    def execute(self, sql, params=None):
        cur = self.con.cursor()
        cur.execute(sql, params or None)
        cur.close()

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
