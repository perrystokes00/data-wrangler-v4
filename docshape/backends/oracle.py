"""
docshape.backends.oracle
========================
Oracle backend, via python-oracledb (the successor to cx_Oracle; its `thin`
mode needs no Oracle Client install, which matters for a laptop deployment).

FOUR THINGS DIFFER FROM THE OTHERS, and they are the reason this file exists:

  * BIND PLACEHOLDERS are named (:1, :2), not `?`. Every other backend here
    uses qmark.
  * IDENTIFIERS FOLD TO UPPERCASE unless quoted, and once quoted they are
    case-SENSITIVE forever. We quote everything (several canonical field names
    — date, level, size, number — are reserved), which means every column in
    an Oracle store is lower-case and must be quoted when queried by hand.
  * IDENTITY is GENERATED ALWAYS AS IDENTITY (12c+). On 11g it would need a
    sequence and trigger; not supported here.
  * "CREATE TABLE IF NOT EXISTS" does not exist. Existence is checked against
    user_tables first.

VARCHAR2 maxes at 4000 bytes unless the database runs extended strings, so
TEXT_LONG maps to CLOB. NUMBER without precision is Oracle's arbitrary-scale
numeric — right for measured values, and it round-trips floats without the
surprises of BINARY_DOUBLE.

UNTESTED AGAINST A LIVE INSTANCE. The SQL shape is standard and the type map
is conventional, but verify on the customer's version before relying on it —
particularly the identity clause, which is the one 11g-vs-12c difference that
will fail loudly rather than silently.
"""
from __future__ import annotations

from docshape.backends.base import (Backend, TEXT, TEXT_LONG, NUMBER, INT,
                                    BIGINT, TIMESTAMP, BOOL, IDENTITY)


class OracleBackend(Backend):
    name = "oracle"
    types = {
        TEXT: "VARCHAR2(255)", TEXT_LONG: "CLOB", NUMBER: "NUMBER",
        INT: "NUMBER(10)", BIGINT: "NUMBER(19)", TIMESTAMP: "TIMESTAMP",
        BOOL: "NUMBER(1)",
        IDENTITY: "NUMBER GENERATED ALWAYS AS IDENTITY",
    }
    batch_size = 5000

    def __init__(self, user=None, password=None, dsn=None, schema=None,
                 conn=None, **kw):
        import oracledb
        self.con = conn or oracledb.connect(user=user, password=password,
                                            dsn=dsn, **kw)
        self.con.autocommit = True
        # Oracle's "schema" is a user. Default to the connected one.
        self.schema = (schema or user or "").upper() or None

    # -- identifiers ------------------------------------------------------- #
    def quote(self, ident):
        return '"' + str(ident).replace('"', '""') + '"'

    def qualified(self, table):
        return (f"{self.quote(self.schema)}.{self.quote(table)}"
                if self.schema else self.quote(table))

    # -- schema ------------------------------------------------------------ #
    def table_exists(self, table):
        cur = self.con.cursor()
        cur.execute("SELECT COUNT(*) FROM all_tables "
                    "WHERE table_name = :1 AND owner = NVL(:2, USER)",
                    [table, self.schema])
        n = cur.fetchone()[0]
        cur.close()
        return bool(n)

    def columns(self, table):
        cur = self.con.cursor()
        cur.execute("SELECT column_name FROM all_tab_columns "
                    "WHERE table_name = :1 AND owner = NVL(:2, USER)",
                    [table, self.schema])
        out = {r[0].upper() for r in cur.fetchall()}
        cur.close()
        return out

    def create_table(self, table, coldefs):
        cols = [f"{self.quote(c)} {self.type_of(t)}" for c, t in coldefs]
        cur = self.con.cursor()
        cur.execute(f"CREATE TABLE {self.qualified(table)} ({', '.join(cols)})")
        cur.close()

    def add_columns(self, table, coldefs):
        have = self.columns(table)
        missing = [(c, t) for c, t in coldefs
                   if c.upper() not in have and t != IDENTITY]
        if missing:
            cols = ", ".join(f"{self.quote(c)} {self.type_of(t)}"
                             for c, t in missing)
            cur = self.con.cursor()
            # Oracle wants parentheses around a multi-column ADD.
            cur.execute(f"ALTER TABLE {self.qualified(table)} ADD ({cols})")
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
        marks = ", ".join(f":{i + 1}" for i in range(len(keys)))
        sql = f"INSERT INTO {self.qualified(table)} ({cols}) VALUES ({marks})"
        cur = self.con.cursor()
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
                    f"WHERE {self.quote(column)} = :1", [value])
        n = cur.rowcount
        cur.close()
        return max(n or 0, 0)

    def query(self, sql, params=None):
        cur = self.con.cursor()
        cur.execute(sql, params or [])
        out = cur.fetchall()
        cur.close()
        return out

    def execute(self, sql, params=None):
        cur = self.con.cursor()
        cur.execute(sql, params or [])
        cur.close()

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
