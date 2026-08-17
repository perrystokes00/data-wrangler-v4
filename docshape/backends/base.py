"""
docshape.backends.base
======================
What a storage backend must do, and nothing more.

Only four things actually differ between DuckDB and SQL Server: the names of
types, how identifiers are quoted, the autoincrement idiom, and the mechanism
for inserting many rows. Everything else — information_schema, parameterised
statements, the SQL itself — is the same. So a backend is a thin adapter over
those four, not an abstraction over databases.

DELIBERATELY OUT OF SCOPE
-------------------------
A backend knows types, quoting and inserts. It does NOT know what a review
status is, what provenance means, or which columns a shape needs. Those are
identical on every platform, so they belong in the store — a backend that
knew about REVIEW_STATUS would have to be updated on every platform each time
the review model changed, which is exactly the coupling this split avoids.

LOGICAL TYPES
-------------
The store asks for a MEANING and the backend supplies the spelling:

    TEXT        short identifier or label
    TEXT_LONG   descriptions, JSON payloads, anything unbounded
    NUMBER      a measurement — always floating point, never money
    INT/BIGINT  counts and ordinals
    TIMESTAMP   a point in time
    BOOL        a flag
    IDENTITY    an auto-incrementing surrogate key

NUMBER is float everywhere on purpose. These are measured values — depths,
rates, porosities — read out of documents, and imposing a scale at capture
time throws away precision the document actually stated. Anything that needs
DECIMAL semantics gets it when it moves to a real target, not here.
"""
from __future__ import annotations

TEXT = "TEXT"
TEXT_LONG = "TEXT_LONG"
NUMBER = "NUMBER"
INT = "INT"
BIGINT = "BIGINT"
TIMESTAMP = "TIMESTAMP"
BOOL = "BOOL"
IDENTITY = "IDENTITY"

LOGICAL_TYPES = (TEXT, TEXT_LONG, NUMBER, INT, BIGINT, TIMESTAMP, BOOL,
                 IDENTITY)


class Backend:
    """Interface every backend implements. Subclasses override the mechanics."""

    name = "base"
    #: logical type -> platform declaration
    types: dict = {}
    #: how many rows to send per round trip
    batch_size = 1000

    # -- identifiers ------------------------------------------------------- #
    def quote(self, ident: str) -> str:
        raise NotImplementedError

    def qualified(self, table: str) -> str:
        """Schema-qualified, quoted table name."""
        raise NotImplementedError

    def type_of(self, logical: str) -> str:
        if logical not in self.types:
            raise ValueError(f"{self.name}: no mapping for logical type "
                             f"{logical!r} (known: {sorted(self.types)})")
        return self.types[logical]

    # -- schema ------------------------------------------------------------ #
    def table_exists(self, table: str) -> bool:
        raise NotImplementedError

    def columns(self, table: str) -> set:
        """Existing column names, UPPER-cased for comparison."""
        raise NotImplementedError

    def create_table(self, table: str, coldefs) -> None:
        """coldefs is [(column_name, logical_type)] in order."""
        raise NotImplementedError

    def add_columns(self, table: str, coldefs) -> list:
        """Add any of coldefs the table doesn't have. Returns those added.

        Additive only. A store that grows a new field should widen the table,
        never drop or retype what's there — the rows already captured under
        the old shape have to remain readable.
        """
        have = self.columns(table)
        missing = [(c, t) for c, t in coldefs if c.upper() not in have]
        for col, logical in missing:
            self.execute(f"ALTER TABLE {self.qualified(table)} "
                         f"ADD {self.quote(col)} {self.type_of(logical)}")
        return [c for c, _t in missing]

    def ensure_table(self, table: str, coldefs) -> str:
        """Create, or widen if it exists. Returns 'created' | 'widened' | 'ok'."""
        if not self.table_exists(table):
            self.create_table(table, coldefs)
            return "created"
        return "widened" if self.add_columns(table, coldefs) else "ok"

    # -- data -------------------------------------------------------------- #
    def insert(self, table: str, rows) -> int:
        """Insert row dicts. Keys that aren't columns are IGNORED.

        Ignoring unknown keys rather than failing is deliberate: a pack can
        grow a field before the table has caught up, and losing one column is
        better than losing the batch. The store reports what was dropped.
        """
        raise NotImplementedError

    def delete_where(self, table: str, column: str, value) -> int:
        raise NotImplementedError

    def query(self, sql: str, params=None):
        raise NotImplementedError

    def execute(self, sql: str, params=None):
        raise NotImplementedError

    def close(self) -> None:
        pass

    # -- shared helpers ---------------------------------------------------- #
    def _align(self, table, rows):
        """(ordered column names, row tuples, ignored keys).

        Groups nothing: every row is projected onto the same column list so a
        single executemany covers the batch. Missing keys become NULL.
        """
        have = self.columns(table)
        actual = {}
        for r in rows:
            for k in r:
                if k.upper() in have and k not in actual:
                    actual[k] = True
        keys = list(actual)
        ignored = sorted({k for r in rows for k in r
                          if k.upper() not in have})
        tuples = [tuple(r.get(k) for k in keys) for r in rows]
        return keys, tuples, ignored

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
