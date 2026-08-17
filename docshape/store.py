"""
docshape.store
==============
Capture documents into a store: a table per shape, wherever the backend points.

This is the layer that knows MEANING. The backend knows types and quoting; the
pack knows vocabulary; the readers know file formats. The store decides what a
captured row looks like — its provenance, its review state, and what happens to
the columns nothing claimed.

FOUR GROUPS OF COLUMNS, EVERY TABLE
-----------------------------------
    the shape's own fields   top_md, formation, stage — from the pack
    identity                 whatever the pack calls its subject
    provenance               file, path, which table on which page, content
                             hash, shape and score, when
    review                   status, by, at, note, confidence, extra_json

`extra_json` is not decoration. The recogniser already knows which columns no
field claimed — Centralizers, Float Equipment, HC Pore Vol — and dropping them
is the difference between "we didn't map that" and "that's gone".

REVIEW IS A WHERE CLAUSE
------------------------
review_status runs NEW -> READY -> MIGRATED, with REJECTED and ERROR as
terminal branches. Nothing moves until a human sets READY. There is no trigger
and no listener: the status column IS the queue, so migration runs on a button,
a schedule, or never, and a failure lands in review_note instead of blocking
the reviewer's edit.

CONFIDENCE IS PER ROW, not per file. The same LAS gives a curve set worth
trusting and a lat/long that isn't.

IDENTITY BY CONTENT, NOT PATH
-----------------------------
Documents are keyed on a hash of their CONTENT. The same file captured from two
folders is one document, and re-capturing replaces rather than duplicates. A
path hash cannot do that — two copies of one sample set look like two different
sets, which is a real afternoon lost.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

from docshape.backends import (TEXT, TEXT_LONG, NUMBER, INT, BIGINT,
                               TIMESTAMP, IDENTITY)
from docshape.engine.recognise import Recogniser, to_number, INTERNAL_KEYS
from docshape.readers import (read_tables, read_native, collect,
                              TABLE_EXTS, NATIVE_EXTS)

PROVENANCE = [
    ("doc_file", TEXT), ("doc_path", TEXT_LONG), ("doc_table", TEXT),
    ("doc_sha1", TEXT), ("shape", TEXT), ("shape_score", NUMBER),
    ("captured_at", TIMESTAMP),
]
REVIEW = [
    ("review_status", TEXT), ("reviewed_by", TEXT), ("reviewed_at", TIMESTAMP),
    ("review_note", TEXT_LONG), ("confidence", NUMBER),
    ("extra_json", TEXT_LONG),
]
DOCUMENTS = [
    ("doc_sha1", TEXT), ("doc_file", TEXT), ("doc_path", TEXT_LONG),
    ("doc_ext", TEXT), ("identity", TEXT), ("subject_name", TEXT),
    ("tables_found", INT),
    ("rows_captured", INT), ("unrecognised", INT), ("captured_at", TIMESTAMP),
    ("review_status", TEXT), ("review_note", TEXT_LONG),
]

_LOGICAL = {TEXT, TEXT_LONG, NUMBER, INT, BIGINT, TIMESTAMP, IDENTITY}


def sha1_of(path, chunk=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest().upper()


class Store:
    """A capture store over any backend, driven by any pack."""

    def __init__(self, backend, pack, prefix="doc_"):
        self.db = backend
        self.pack = pack
        self.prefix = prefix
        self.rec = Recogniser(pack)
        self.ident = getattr(pack, "identity_field", None) or "identity"

    # -- schema ------------------------------------------------------------ #
    def _shape_columns(self, shape):
        spec = self.pack.shapes[shape]
        fields = list(dict.fromkeys(
            list(spec.get("required", ())) + list(spec.get("optional", ()))))
        cols = [(self.ident, TEXT)]
        for f in fields:
            if f == self.ident:
                continue
            cols.append((f, NUMBER if f in self.pack.numeric else TEXT))
        return cols + PROVENANCE + REVIEW

    def _native_columns(self, module, table):
        cols = [(self.ident, TEXT)]
        cols += [(c, t) for c, t in module.TABLES[table]]
        return cols + PROVENANCE + REVIEW

    def ensure_schema(self, log=print):
        """Create or widen every table this pack and these readers can fill."""
        made = []
        for shape in self.pack.shapes:
            name = f"{self.prefix}{shape}"
            if self.db.ensure_table(name, self._shape_columns(shape)) == "created":
                made.append(name)
        from docshape.readers import las, segy
        for module in (las, segy):
            for table in module.TABLES:
                name = f"{self.prefix}{table}"
                if self.db.ensure_table(
                        name, self._native_columns(module, table)) == "created":
                    made.append(name)
        if self.db.ensure_table("documents", DOCUMENTS) == "created":
            made.append("documents")
        if made:
            log(f"-- created {len(made)} table(s)")
        return made

    # -- capture ----------------------------------------------------------- #
    def _stamp(self, rec, base, path, table, sha, shape, score, now, extra):
        rec.update({
            "doc_file": base, "doc_path": path, "doc_table": table,
            "doc_sha1": sha, "shape": shape, "shape_score": float(score),
            "captured_at": now, "review_status": "NEW",
            "extra_json": json.dumps(extra, default=str) if extra else None,
        })
        return rec

    def _identity_from_results(self, results):
        """The identity a header-shaped table stated, if one was recognised.

        A document that carries its own header block has already told us who
        it is about, and that value applies to every other table in the file.
        Free, because the recogniser found it on the way past.
        """
        for res in results:
            if res["shape"] == "UNKNOWN":
                continue
            for r in res["rows"]:
                v = r.get(self.ident)
                if v and str(v).strip():
                    got = self.pack.normalise_identity(v)
                    if got:
                        return got
        return None

    def capture_file(self, path, log=print, replace=True):
        base = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        sha = sha1_of(path)
        now = datetime.now()

        if replace:
            self._clear(sha)

        if ext in NATIVE_EXTS:
            return self._capture_native(path, base, ext, sha, now, log)

        results = []
        for name, rows in (read_tables(path) or {}).items():
            if not rows:
                continue
            header = list(rows[0].keys())
            data = [[r.get(k) for k in header] for r in rows]
            res = self.rec.read_table(header, data)
            res["table"] = name
            results.append(res)

        # IDENTITY, best evidence first. Getting this wrong is what leaves a
        # table with rows and no subject — 201 production rows attached to no
        # well at all on the first real run.
        #   1. a header TABLE the recogniser already identified. Strongest:
        #      the document states it in a grid we've parsed.
        #   2. the document's TEXT. Most vendor reports put the API in a
        #      letterhead block, not a table.
        #   3. the FILE NAME. Real archives often name files after the
        #      subject; synthetic sets almost always do.
        doc_ident = self._identity_from_results(results)
        if not doc_ident and hasattr(self.pack, "identity_from_text"):
            from docshape.readers import read_text
            doc_ident = self.pack.identity_from_text(read_text(path))
        if not doc_ident:
            doc_ident = self.pack.identity_from_name(path)
        # Even with no identifier, the document usually NAMES its subject.
        # Recorded so migration can resolve it against a reference table —
        # capture has no database to resolve against, and guessing here would
        # be worse than saying what was printed.
        subject = None
        if hasattr(self.pack, "subject_from_text"):
            from docshape.readers import read_text
            subject = self.pack.subject_from_text(read_text(path))
        total, unknown = 0, 0
        for res in results:
            if res["shape"] == "UNKNOWN":
                unknown += 1
                continue
            payload = []
            for r in res["rows"]:
                rec = {}
                for k, v in r.items():
                    if k in INTERNAL_KEYS:
                        continue
                    rec[k] = (to_number(v) if k in self.pack.numeric
                              else (None if v is None or str(v).strip() == ""
                                    else str(v).strip()))
                iv = rec.get(self.ident) or doc_ident
                rec[self.ident] = self.pack.normalise_identity(iv) if iv else None
                payload.append(self._stamp(
                    rec, base, path, res["table"], sha, res["shape"],
                    res["score"], now, r.get("_extra")))
            if payload:
                total += self.db.insert(f"{self.prefix}{res['shape']}", payload)

        self._record_document(sha, base, path, ext, doc_ident, len(results),
                              total, unknown, now, subject)
        log(f"  {base:44} {total:>5} row(s)"
            + (f"  {self.ident}={doc_ident}" if doc_ident
               else (f"  name={subject}" if subject else "  no identity"))
            + (f"  {unknown} unrecognised" if unknown else ""))
        return total

    def _capture_native(self, path, base, ext, sha, now, log):
        kind, payload = read_native(path)
        if not payload:
            log(f"  {base:44} unreadable")
            return 0
        from docshape.readers import las, segy
        module = las if kind == "las" else segy
        ident, tables, extra = module.to_rows(payload)
        ident = self.pack.normalise_identity(ident) if ident else \
            self.pack.identity_from_name(path)
        total = 0
        for table, rows in tables.items():
            stamped = []
            for r in rows:
                rec = dict(r)
                rec[self.ident] = ident
                stamped.append(self._stamp(rec, base, path, table, sha,
                                           table, 1.0, now, extra))
                extra = None          # only the header row carries the extras
            if stamped:
                total += self.db.insert(f"{self.prefix}{table}", stamped)
        self._record_document(sha, base, path, ext, ident, len(tables),
                              total, 0, now)
        log(f"  {base:44} {total:>5} row(s)  [{kind}]")
        return total

    def _clear(self, sha):
        for shape in list(self.pack.shapes) + self._native_names():
            try:
                self.db.delete_where(f"{self.prefix}{shape}", "doc_sha1", sha)
            except Exception:
                pass
        try:
            self.db.delete_where("documents", "doc_sha1", sha)
        except Exception:
            pass

    def _native_names(self):
        from docshape.readers import las, segy
        return list(las.TABLES) + list(segy.TABLES)

    def _record_document(self, sha, base, path, ext, ident, tables, rows,
                         unknown, now, subject=None):
        self.db.insert("documents", [{
            "doc_sha1": sha, "doc_file": base, "doc_path": path,
            "doc_ext": ext, "identity": ident, "subject_name": subject,
            "tables_found": tables,
            "rows_captured": rows, "unrecognised": unknown,
            "captured_at": now, "review_status": "NEW"}])

    def capture_dir(self, target, log=print):
        if not os.path.exists(target):
            raise FileNotFoundError(f"path does not exist: {target!r}")
        paths = collect(target)
        log(f"-- {len(paths)} document(s) -> {self.db.name}\n")
        total = 0
        for p in paths:
            try:
                total += self.capture_file(p, log=log)
            except Exception as e:
                log(f"  !! {os.path.basename(p)}: {type(e).__name__}: {e}")
        log(f"\n-- {total:,} row(s) captured")
        return total

    # -- review ------------------------------------------------------------ #
    def summary(self, log=print):
        names = [(s, f"{self.prefix}{s}") for s in
                 list(self.pack.shapes) + self._native_names()]
        log(f"\n{'shape':24}{'rows':>8}{'subjects':>10}{'docs':>7}   status")
        log("-" * 78)
        grand = 0
        for shape, tbl in names:
            try:
                n = self.db.query(f"SELECT count(*) FROM {self.db.qualified(tbl)}")[0][0]
            except Exception:
                continue
            if not n:
                continue
            grand += n
            q = self.db.qualified(tbl)
            subj = self.db.query(
                f"SELECT count(DISTINCT {self.db.quote(self.ident)}) FROM {q}")[0][0]
            docs = self.db.query(
                f"SELECT count(DISTINCT {self.db.quote('doc_sha1')}) FROM {q}")[0][0]
            st = self.db.query(
                f"SELECT {self.db.quote('review_status')}, count(*) FROM {q} "
                f"GROUP BY {self.db.quote('review_status')}")
            log(f"{shape:24}{n:>8,}{subj:>10}{docs:>7}   "
                + " ".join(f"{s or 'NEW'}:{c}" for s, c in st))
        log("-" * 78)
        log(f"{'TOTAL':24}{grand:>8,}")
        return grand

    def set_status(self, status="READY", shape=None, where=None, log=print):
        """Mark rows for migration. The status column IS the queue."""
        shapes = [shape] if shape else list(self.pack.shapes) + self._native_names()
        n = 0
        for s in shapes:
            q = self.db.qualified(f"{self.prefix}{s}")
            sql = (f"UPDATE {q} SET {self.db.quote('review_status')} = "
                   f"'{status}'")
            if where:
                sql += f" WHERE {where}"
            try:
                self.db.execute(sql)
                n += self.db.query(
                    f"SELECT count(*) FROM {q} WHERE "
                    f"{self.db.quote('review_status')} = '{status}'")[0][0]
            except Exception:
                continue
        log(f"-- {n:,} row(s) now {status}")
        return n
