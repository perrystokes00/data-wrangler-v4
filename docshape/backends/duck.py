"""
docshape.backends.duck
======================
DuckDB backend — a single file, no server, no credentials.

This is the one that makes capture portable: a reviewer can be handed the
.duckdb file itself, and nothing needs a corporate database until the data is
ready to migrate.

ONE CONSTRAINT WORTH KNOWING: DuckDB allows either ONE read-write process or
MANY read-only ones, enforced by a file lock. If a UI has the file open, a
capture run cannot write to it and fails with a lock error rather than a clear
message. Close the viewer before capturing.
"""
from __future__ import annotations

from docshape.backends.base import (Backend, TEXT, TEXT_LONG, NUMBER, INT,
                                    BIGINT, TIMESTAMP, BOOL, IDENTITY)


class DuckBackend(Backend):
    name = "duckdb"
    types = {
        TEXT: "VARCHAR", TEXT_LONG: "VARCHAR", NUMBER: "DOUBLE",
        INT: "INTEGER", BIGINT: "BIGINT", TIMESTAMP: "TIMESTAMP",
        BOOL: "BOOLEAN", IDENTITY: "BIGINT",
    }

    def __init__(self, path=":memory:", read_only=False):
        import duckdb
        self.path = path
        self.con = duckdb.connect(path, read_only=read_only)

    # -- identifiers ------------------------------------------------------- #
    def quote(self, ident):
        # Several canonical field names — show, date, interval, length — are
        # reserved words. Quoting everything is cheaper than knowing which.
        return '"' + str(ident).replace('"', '""') + '"'

    def qualified(self, table):
        return self.quote(table)

    # -- schema ------------------------------------------------------------ #
    def table_exists(self, table):
        return bool(self.con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = ?", [table]).fetchone()[0])

    def columns(self, table):
        return {r[0].upper() for r in self.con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?", [table]).fetchall()}

    def create_table(self, table, coldefs):
        cols = [f"{self.quote(c)} {self.type_of(t)}" for c, t in coldefs]
        self.con.execute(
            f"CREATE TABLE {self.qualified(table)} ({', '.join(cols)})")

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
        self.con.executemany(
            f"INSERT INTO {self.qualified(table)} ({cols}) VALUES ({marks})",
            tuples)
        return len(tuples)

    def delete_where(self, table, column, value):
        before = self.con.execute(
            f"SELECT count(*) FROM {self.qualified(table)}").fetchone()[0]
        self.con.execute(
            f"DELETE FROM {self.qualified(table)} "
            f"WHERE {self.quote(column)} = ?", [value])
        after = self.con.execute(
            f"SELECT count(*) FROM {self.qualified(table)}").fetchone()[0]
        return before - after

    def query(self, sql, params=None):
        return self.con.execute(sql, params or []).fetchall()

    def execute(self, sql, params=None):
        return self.con.execute(sql, params or [])

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
