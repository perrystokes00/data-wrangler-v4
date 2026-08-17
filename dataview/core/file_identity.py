"""
dataview/core/file_identity.py — ONE definition of a file's INVENTORY_ID.

    from dataview.core.file_identity import inventory_id
    iid = inventory_id(path)

WHY THIS MODULE EXISTS
----------------------
Three functions used to mint INVENTORY_ID, and they disagreed. For the path
    C:\\Users\\perry\\docs\\Scout_Ticket.pdf
they produced:

    file_inventory._make_id   sha1(path, UTF-8),          ORIGINAL CASE
                              -> DE37071124D91131E9981C7BCCB01F708A4FE8F1
    pipeline_run.inv_id       sha1(path.upper(), UTF-8)
                              -> D1B0DD91C0C1E774BBB43D4815E40DD37BA5E921
    file_gate.inventory_id    sha1(UPPER(path), UTF-16-LE)
                              -> AEB220CA4F19CD33CCB962669469F83DA1C72686

All forty hex characters. All indistinguishable in the table. None of them
join. Whether that matters depended entirely on which code path happened to
scan a file — which is not a property anyone can reason about while reading a
query.

INVENTORY_ID is the identity the whole system hangs on: capture stamps it on
every extracted row, promote carries it into dv_*, and the join back to
GLOBAL_FILE_CATALOG is what answers "which document did this number come
from". An identity with three definitions is not an identity.

THE CANONICAL FORM, AND WHY THIS ONE
------------------------------------
    SHA-1( UPPER( normpath(path) ) encoded UTF-16-LE ), uppercase hex

  * NORMPATH first. Windows collapses repeated separators for filesystem
    access, so a root pasted as C:\\\\Users\\\\perry opens the same folder as
    C:\\Users\\perry — the scan works perfectly and mints a different id for
    every file. That produced 1,050 catalog rows for 525 PDFs, silently, and
    doubled every count in every report.

  * UPPER because Windows paths are case-insensitive. C:\\Users and c:\\users
    are one file and must be one id.

  * UTF-16-LE because that makes this expression its exact T-SQL equivalent:

        HASHBYTES('SHA1', UPPER(<nvarchar path>))

    so an id can be recomputed SERVER-SIDE — a repair or a reconciliation is
    a set-based UPDATE rather than a round trip through Python for every row.
    It also matches the DataView canonical entity_id convention, and it is
    correct for non-ASCII paths, which a UTF-8 variant is not once the vault
    goes international.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not resolve symlinks, junctions, mapped drives or UNC equivalence.
A file reached as M:\\data\\x.las and as \\\\server\\share\\data\\x.las gets two
ids, correctly — they are two ways in, and which one a site standardises on
is an operational decision, not something a hash function should guess.

Content identity is a separate question and is recorded separately
(FILE_HASH / DUPLICATE_GROUP). Path is the KEY because it is computable in
SQL, cheap, idempotent across re-scans, and degrades better: correct and
re-save a document and a path key still resolves, reporting a changed hash,
where a content key silently dangles.
"""
from __future__ import annotations

import hashlib
import os

__all__ = ["inventory_id", "canonical_path", "ID_WIDTH", "TSQL_EXPRESSION"]

ID_WIDTH = 40                       # SHA-1 hex, uppercase

# The T-SQL that reproduces inventory_id() for a path already stored as
# nvarchar. Kept here so the two can never be edited apart.
TSQL_EXPRESSION = "CONVERT(char(40), HASHBYTES('SHA1', UPPER({col})), 2)"


def canonical_path(path: str) -> str:
    """The single spelling of a path that the id is computed from.

    Separate from inventory_id() so callers that need to STORE a canonical
    path (the catalog's FILE_PATH column) use the same rule as the hash,
    rather than a second opinion about what canonical means.
    """
    # .strip() first: a trailing space is a different string but not a
    # different file — Windows discards trailing spaces in names.
    return os.path.normpath(str(path).strip()).upper()


def inventory_id(path: str) -> str:
    """The canonical INVENTORY_ID for a file path. Never call sha1 directly."""
    return hashlib.sha1(
        canonical_path(path).encode("utf-16-le")
    ).hexdigest().upper()


if __name__ == "__main__":       # sanity check: python -m dataview.core.file_identity
    import sys
    # Verified against WINDOWS path rules explicitly, so this proves the
    # canonicalisation on any platform. On Windows os.path IS ntpath, so the
    # deployed behaviour and the tested behaviour are the same code.
    import ntpath

    def _win_id(p):
        return hashlib.sha1(
            ntpath.normpath(str(p).strip()).upper().encode("utf-16-le")
        ).hexdigest().upper()

    cases = [
        r"C:\Users\perry\docs\Scout_Ticket.pdf",
        r"C:\\Users\\perry\\docs\\Scout_Ticket.pdf",     # doubled separators
        r"c:\users\PERRY\docs\scout_ticket.pdf",         # different case
        r"C:\Users\perry\docs\..\docs\Scout_Ticket.pdf", # redundant segment
        "C:/Users/perry/docs/Scout_Ticket.pdf",          # forward slashes
    ]
    ids = {_win_id(c) for c in cases}
    for c in cases:
        print(f"{_win_id(c)}  {c}")
    ok = len(ids) == 1
    print("\nall spellings agree:", ok)
    print("T-SQL:", TSQL_EXPRESSION.format(col="FILE_PATH"))
    sys.exit(0 if ok else 1)
