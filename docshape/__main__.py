"""
docshape command line
=====================
    py -m docshape capture --in C:\\docs --backend duckdb --db capture.duckdb
    py -m docshape capture --in C:\\docs --backend mssql --database DataView_Demo
    py -m docshape summary --backend duckdb --db capture.duckdb
    py -m docshape ready   --backend duckdb --db capture.duckdb --where "confidence > 80"
    py -m docshape packs

The backend and the pack are both run-time choices. The same capture runs
against a file on a laptop or a schema on a site install, and nothing in the
recogniser changes.
"""
from __future__ import annotations

import argparse
import os
import sys


def _open(args):
    from docshape.backends import open_backend
    from docshape.packs import load
    b = args.backend
    if b in ("duckdb", "duck", "file"):
        db = open_backend("duckdb", path=args.db)
    elif b in ("mssql", "sqlserver"):
        db = open_backend("mssql", server=args.server, database=args.database,
                          schema=args.schema)
    elif b == "oracle":
        db = open_backend("oracle", user=args.user, password=args.password,
                          dsn=args.dsn, schema=args.schema)
    else:
        db = open_backend("snowflake", account=args.account, user=args.user,
                          password=args.password, warehouse=args.warehouse,
                          database=args.database, schema=args.schema,
                          role=args.role)
    from docshape.store import Store
    return db, Store(db, load(args.pack))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="docshape",
                                 description="Recognise and capture document tables")
    ap.add_argument("command",
                    choices=["capture", "summary", "ready", "packs", "shapes",
                             "propose"])
    ap.add_argument("--in", dest="inp", help="document or folder to capture")
    ap.add_argument("--file", help="single document, for propose")
    ap.add_argument("--dir", help="folder, for propose")
    ap.add_argument("--limit", type=int, help="stop after N documents")
    ap.add_argument("--pack", default="petroleum")
    ap.add_argument("--backend", default="duckdb",
                    choices=["duckdb", "duck", "file", "mssql", "sqlserver",
                             "oracle", "snowflake"])
    ap.add_argument("--db", default="capture.duckdb", help="duckdb file path")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--schema", default="stg")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--dsn", help="oracle: host:port/service")
    ap.add_argument("--account", help="snowflake account identifier")
    ap.add_argument("--warehouse")
    ap.add_argument("--role")
    ap.add_argument("--prefix", default="doc_")
    ap.add_argument("--status", default="READY")
    ap.add_argument("--shape", help="limit --ready to one shape")
    ap.add_argument("--where", help="predicate for --ready")
    a = ap.parse_args(argv)

    if a.command == "packs":
        from docshape.packs import available, load, validate
        for name in available():
            print(f"\n{name}:")
            validate(load(name))
        return 0

    if a.command == "propose":
        from docshape.propose import propose_file, propose_dir
        target = a.file or a.dir or a.inp
        if not target:
            ap.error("propose needs --file or --dir")
        if os.path.isdir(target):
            propose_dir(target, a.pack, a.limit)
        else:
            propose_file(target, a.pack)
        return 0

    if a.command == "shapes":
        from docshape.packs import load
        pack = load(a.pack)
        for name, spec in pack.shapes.items():
            print(f"  {name:22} requires {spec['required']}"
                  f"  -> {spec.get('target') or '(no target)'}")
        return 0

    db, store = _open(a)
    try:
        if a.command == "capture":
            if not a.inp:
                ap.error("capture needs --in")
            store.ensure_schema()
            store.capture_dir(a.inp)
            store.summary()
        elif a.command == "summary":
            store.summary()
        elif a.command == "ready":
            store.set_status(a.status, a.shape, a.where)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
