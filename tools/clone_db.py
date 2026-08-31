#!/usr/bin/env python3
r"""clone_db.py — make an exact clone of a database on the same SQL Server
instance via BACKUP + RESTORE.

This is the reliable way to get DataView_Demo = DataView: it copies schema AND
data AND views/procs/indexes with the correct schema names, so there's no DDL
scripting, no find-replace (which renamed your schemas), and no CREATE USER
errors. It:

  1. reads DataView's logical file names + data directory
  2. BACKUP DATABASE DataView  (COPY_ONLY, so the backup chain is undisturbed)
  3. drops DataView_Demo if it exists (SINGLE_USER WITH ROLLBACK IMMEDIATE so a
     live app/SSMS connection can't block it)
  4. RESTORE … AS DataView_Demo, moving the files to new paths
  5. re-links the pmstokes00 user to its login (fixes the orphaned-user case)

Usage:
    python clone_db.py
    python clone_db.py --source DataView --target DataView_Demo
    python clone_db.py --bak "C:\Bulk\DataView_clone.bak"
"""
import argparse
import ntpath
import pyodbc


def _conn(server):
    # connect to master: you can't drop/restore a DB you're sitting in
    return pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};DATABASE=master;Trusted_Connection=yes",
        autocommit=True)


def _q(s):                      # escape a path for a N'…' literal
    return s.replace("'", "''")


def main():
    ap = argparse.ArgumentParser(
        description="Clone a database via BACKUP/RESTORE on one instance.")
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--source", default="DataView_Demo")
    ap.add_argument("--target", default="DataView_Demo")
    ap.add_argument("--bak", default=None,
                    help=r"backup file path (default C:\Bulk\<source>_clone.bak)")
    ap.add_argument("--user", default="pmstokes00",
                    help="database user to re-link to its login after restore")
    a = ap.parse_args()
    bak = a.bak or rf"C:\Bulk\{a.source}_clone.bak"

    cn = _conn(a.server)
    cur = cn.cursor()

    # 1) source file layout
    cur.execute(
        "SELECT name, physical_name, type_desc "
        "FROM sys.master_files WHERE database_id = DB_ID(?)", a.source)
    files = cur.fetchall()
    if not files:
        print(f"[ERROR] source database {a.source} not found.")
        return
    moves = []
    for logical, physical, _td in files:
        base = ntpath.basename(physical)
        ddir = ntpath.dirname(physical)
        if a.source in base:
            newbase = base.replace(a.source, a.target)
        else:
            ext = ntpath.splitext(base)[1]
            newbase = f"{a.target}_{logical}{ext}"
        newphys = ntpath.join(ddir, newbase)
        moves.append(f"MOVE N'{_q(logical)}' TO N'{_q(newphys)}'")
        print(f"   {logical:20} -> {newphys}")

    # 2) backup source (COPY_ONLY; Express has no COMPRESSION, so it's omitted)
    print(f"[backup] {a.source} -> {bak}")
    cur.execute(
        f"BACKUP DATABASE [{a.source}] TO DISK = N'{_q(bak)}' "
        f"WITH INIT, COPY_ONLY, STATS = 10")
    while cur.nextset():            # drain STATS / info result sets
        pass

    # 3) drop target if present, forcing other connections off
    print(f"[drop]   {a.target} (if it exists)")
    cur.execute(
        f"IF DB_ID('{a.target}') IS NOT NULL "
        f"BEGIN "
        f"  ALTER DATABASE [{a.target}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"  DROP DATABASE [{a.target}]; "
        f"END")

    # 4) restore as target, moving the files
    print(f"[restore] {a.target}")
    cur.execute(
        f"RESTORE DATABASE [{a.target}] FROM DISK = N'{_q(bak)}' "
        f"WITH {', '.join(moves)}, RECOVERY, REPLACE, STATS = 10")
    while cur.nextset():
        pass

    # 5) re-link the user to its login (no-op if not orphaned / not present)
    if a.user:
        print(f"[fixup]  re-link user {a.user}")
        try:
            cur.execute(
                f"USE [{a.target}]; "
                f"IF EXISTS (SELECT 1 FROM sys.database_principals "
                f"           WHERE name = N'{a.user}' AND type = 'S') "
                f"  ALTER USER [{a.user}] WITH LOGIN = [{a.user}];")
        except pyodbc.Error as e:
            print(f"   (skipped: {e})")

    print(f"[DONE] {a.target} is a clone of {a.source}.")


if __name__ == "__main__":
    main()
