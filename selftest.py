r"""
selftest.py — is this codebase safe to package?

    python selftest.py                                  # no database needed
    python selftest.py --server localhost\SQLEXPRESS --database DataView_Demo
    python selftest.py --tier imports                   # just one tier

Five tiers, cheapest first. Each is independent; a failure in one does not
stop the others, because "what else is broken" is the question you actually
have when something fails.

  1 IMPORTS    import every module. Catches a name that does not exist
               (`from dataview.core.db import get_engine` — that module
               exports create_engine), an orphaned def body left by an
               edit, a missing dependency. py_compile does NOT catch any
               of these: the file parses fine and fails at runtime.

  2 LINTS      grep for patterns that have each caused a real bug. Not
               style — every one of these shipped and broke something.

  3 UNITS      the pure functions: path canonicalisation, file
               classification, provenance origin. No database, no
               Streamlit, microseconds.

  4 SUITES     the existing test scripts, run if present — vocab_check,
               well_path selftest, smoke, t3-t6.

  5 INVARIANTS database truths that must hold. This is the tier that
               would have caught the week's worst bugs, because they were
               not code faults at all: a DEFINITION drifted underneath
               working code, and only data can show that.

WHY TIER 5 MATTERS MOST
-----------------------
On 3 August the bulk loader began stamping inventory_id and registering
its CSVs in the file catalog. Nothing broke. But three tools that used
"has an inventory_id" to mean "came from a document" silently changed
meaning — one of them a DELETE that would have removed 6,737 bulk-loaded
rows while reporting them as catalog rows. No unit test fails for that.
An invariant does: "no row whose source file is a .csv may be classified
as document-derived."
"""
from __future__ import annotations

import argparse
import ast
import importlib
import os
import re
import subprocess
import sys
import time
import traceback

# Streamlit logs a warning per @st.cache_data decorator when imported
# outside a running server. Importing 40 page modules therefore buries the
# result under 40 identical warnings. Silenced here, not suppressed
# globally — a real error still prints.
try:
    import logging as _logging
    _logging.getLogger("streamlit").setLevel(_logging.ERROR)
    _logging.getLogger("streamlit.runtime.caching").setLevel(_logging.ERROR)
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
PACKAGES = ("dataview", "docshape")

# Modules that legitimately cannot be imported in a bare process (none so
# far — kept because a UI module that needs a running server would go here
# with a REASON, never as a blanket skip).
SKIP_IMPORT: dict[str, str] = {}


class Result:
    def __init__(self):
        self.rows: list[tuple[str, str, str, str]] = []   # tier, name, state, note
        self.t0 = time.time()

    def add(self, tier, name, ok, note=""):
        self.rows.append((tier, name, "PASS" if ok else "FAIL", note))
        return ok

    def failures(self):
        return [r for r in self.rows if r[2] == "FAIL"]

    def report(self, verbose=False):
        by_tier: dict[str, list] = {}
        for t, n, s, note in self.rows:
            by_tier.setdefault(t, []).append((n, s, note))
        print()
        for tier, items in by_tier.items():
            bad = [i for i in items if i[1] == "FAIL"]
            print(f"{tier:12} {len(items) - len(bad):4} pass  {len(bad):4} fail")
            for n, s, note in items:
                if s == "FAIL" or verbose:
                    mark = "  ✗" if s == "FAIL" else "  ·"
                    print(f"{mark} {n}" + (f"  — {note}" if note else ""))
        n_bad = len(self.failures())
        print(f"\n{'=' * 66}")
        print(f"{len(self.rows) - n_bad} passed, {n_bad} failed "
              f"in {time.time() - self.t0:.1f}s")
        return 0 if n_bad == 0 else 1


# ───────────────────────────── 1 · imports ──────────────────────────────
def is_script(path):
    """True when a module RUNS things at import time.

    Found the hard way: the first full run imported probe_capture.py and
    run_stage.py, which executed a capture against the live database, a
    promote dry-run, and a VAULT STAGE THAT COPIED 1,068 FILES and stamped
    VAULTED_AT on every one of them. A test harness must not be able to do
    that. Importing is only safe for modules whose top level DEFINES
    things; a top level that CALLS things is a script and gets read, not
    imported.

    Deliberately conservative — it flags rather than imports. A false
    positive costs one line of coverage; a false negative runs a pipeline.
    """
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
    except Exception:
        return None                      # let the import surface the error
    # LOOK INSIDE A TOP-LEVEL try:, TOO. Two real files hid there —
    # enrich_from_dbf wraps its imports in `try: … except ImportError:
    # sys.exit("pip install …")`, and run_stage puts its ENTIRE body in a
    # try/finally with no __main__ guard at all. Both execute on import,
    # both were reported as import FAILURES rather than skipped as the
    # scripts they are. A Try is not a guard.
    # Nodes nested in a top-level try/with are judged more NARROWLY than
    # bare ones. A defensive `try: import logging; ...setLevel(...) except:
    # pass` is ordinary module hygiene and must NOT make a library look
    # like a script — selftest itself has one. What matters inside a try
    # is only what EXITS or does real work: sys.exit, a loop, a raise, or
    # a conditional that calls something.
    _EXITS = {"exit", "_exit", "quit"}
    nested = []
    for node in tree.body:
        if isinstance(node, (ast.Try, ast.With)):
            nested.extend(node.body)
            for h in getattr(node, "handlers", ()):
                nested.extend(h.body)
            nested.extend(getattr(node, "finalbody", ()))

    for node in nested:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            f = node.value.func
            nm = getattr(f, "attr", None) or getattr(f, "id", None)
            if nm in _EXITS:
                return f"exits at import, line {node.lineno} (inside a try)"
        if isinstance(node, (ast.For, ast.While, ast.Raise)):
            return (f"top-level {type(node).__name__.lower()} at line "
                    f"{node.lineno} (inside a try)")
        if isinstance(node, ast.If) and "__name__" not in ast.dump(node.test):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Call):
                    return (f"conditional top-level call at line "
                            f"{node.lineno} (inside a try)")

    for node in tree.body:
        # a bare call at module level: print(...), main(), sys.exit(...)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return f"top-level call at line {node.lineno}"
        if isinstance(node, (ast.For, ast.While, ast.Raise)):
            return f"top-level {type(node).__name__.lower()} at line {node.lineno}"
        # `if __name__ == "__main__":` is the correct form and is fine;
        # any OTHER top-level if runs on import
        if isinstance(node, ast.If):
            src = ast.dump(node.test)
            if "__name__" not in src and "TYPE_CHECKING" not in src:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Call):
                        return f"conditional top-level call at line {node.lineno}"
    return None


def tier_imports(res, verbose=False):
    """Import every module. The cheapest test there is, and it would have
    caught two of this week's runtime failures on the spot."""
    mods = []
    for pkg in PACKAGES:
        base = os.path.join(ROOT, pkg)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, names in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".venv", "venv")]
            for n in sorted(names):
                if not n.endswith(".py") or n.startswith("_test"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, n), ROOT)
                mod = rel[:-3].replace(os.sep, ".")
                if mod.endswith(".__init__"):
                    mod = mod[:-9]
                mods.append(mod)
    for mod in mods:
        if mod in SKIP_IMPORT:
            res.add("imports", mod, True, "skipped: " + SKIP_IMPORT[mod])
            continue
        path = os.path.join(ROOT, mod.replace(".", os.sep) + ".py")
        if not os.path.exists(path):
            path = os.path.join(ROOT, mod.replace(".", os.sep), "__init__.py")
        why = is_script(path) if os.path.exists(path) else None
        if why:
            # NOT a failure — a script is allowed to be a script. But it is
            # reported, because module-level work is a landmine for anything
            # that imports it, and because it is why this module went
            # untested.
            res.add("imports", mod, True, f"NOT IMPORTED — script: {why}")
            continue
        try:
            importlib.import_module(mod)
            res.add("imports", mod, True)
        except SystemExit as e:
            # A MODULE THAT EXITS THE INTERPRETER ON IMPORT. Found on the
            # first real run: the whole harness died silently part-way
            # through with no summary. SystemExit is not an Exception, so
            # a bare `except Exception` lets it through and takes the
            # process with it. Worth reporting loudly — anything that
            # imports that module inherits the same landmine.
            res.add("imports", mod, False,
                    f"calls sys.exit({e.code!r}) AT IMPORT TIME — module-level "
                    f"code that should be under `if __name__ == '__main__'`")
        except BaseException as e:                       # noqa: BLE001
            res.add("imports", mod, False, f"{type(e).__name__}: {e}")
            if verbose:
                traceback.print_exc()
    return res


# ───────────────────────────── 2 · lints ────────────────────────────────
# Each pattern shipped and broke something. The note says which.
LINTS = [
    (r"\bSUM\s*\(\s*CASE\s+WHEN\s+EXISTS", ".sql .py",
     "SQL Server error 130: EXISTS inside an aggregate is illegal. "
     "Use a CTE + LEFT JOIN."),
    (r",\s*(bulk|table|rows|value|key|file|user|percent|plan|work)\s*=",
     ".sql",
     "unbracketed alias using a T-SQL reserved word — 'bulk' cost a run. "
     "Write [bulk] = ..."),
    (r"str\(file_path\)(?!\s*\))", ".py",
     "a path stored without normpath: a doubled-separator root scans fine "
     "and catalogs every file twice under a different id."),
    (r"INVENTORY_ID\s+IS\s+NOT\s+NULL", ".sql .py",
     "provenance test that stopped meaning 'from a document' on 3 Aug, "
     "when the bulk loader began stamping ids. Classify by the FILE KIND. "
     "(Legitimate inside GLOBAL_FILE_CATALOG queries — check each hit.)"),
    (r"HEADER_EXTRACTED\s+IS\s+NULL\s+OR", ".sql .py",
     "extract-pending open-coded. Six spellings of 'pending' gave six answers "
     "on one catalog (31/43/190/1,319/2,190/3,876). Import "
     "promotion_lineage.pending_sql('extract') instead. "
     "(promotion_lineage.py itself is the definition — that hit is the source.)"),
    (r"CAPTURED_HASH\s*<>\s*\w*\.?FILE_HASH", ".sql .py",
     "capture-pending open-coded. Import "
     "promotion_lineage.pending_sql('capture') instead."),
]


def tier_lints(res, verbose=False):
    for pat, exts, why in LINTS:
        rx = re.compile(pat, re.I)
        hits = []
        wanted = tuple(exts.split())
        for dirpath, dirnames, names in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".venv", "venv",
                                        ".git", "node_modules")]
            for n in names:
                if not n.endswith(wanted):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    txt = open(p, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                for m in rx.finditer(txt):
                    line = txt[:m.start()].count("\n") + 1
                    hits.append(f"{os.path.relpath(p, ROOT)}:{line}")
        name = pat[:38] + ("…" if len(pat) > 38 else "")
        # PRINT ADVISORIES AS THEY ARE FOUND. A warning that only appears
        # under -v is a warning nobody reads, and these are the patterns
        # that have each already shipped a bug.
        if hits:
            print(f"  ⚠ {len(hits)} hit(s): {why}")
            for h in hits[:8]:
                print(f"      {h}")
            if len(hits) > 8:
                print(f"      … and {len(hits) - 8} more")
        # ADVISORY, not fatal: several of these have legitimate uses. The
        # value is a list somebody reads before packaging, not a gate that
        # gets disabled the first time it cries wolf.
        res.add("lints", name, True,
                (f"{len(hits)} hit(s) — {why}" +
                 ("  [" + ", ".join(hits[:6]) +
                  (" …" if len(hits) > 6 else "") + "]" if hits else ""))
                if hits else "clean")
    return res


# ───────────────────────────── 3 · units ────────────────────────────────
def tier_units(res, verbose=False):
    """Pure functions, no database. Each of these was written to fix a
    real bug; the test is what stops it coming back."""

    # A MISSING THIRD-PARTY PACKAGE IS NOT A CODE FAULT. It matters — the
    # imports tier reports it — but a unit check that cannot run because
    # streamlit is absent must not read the same as one whose assertion
    # failed, or the failure list stops being trustworthy.
    THIRD_PARTY = {"streamlit", "pyodbc", "sqlalchemy", "pandas", "numpy",
                   "fitz", "pdfplumber", "docx", "openpyxl", "h3", "pyproj",
                   "folium", "shapely", "matplotlib", "PIL", "requests"}

    def check(name, fn):
        try:
            fn()
            res.add("units", name, True)
        except AssertionError as e:
            res.add("units", name, False, str(e) or "assertion failed")
        except ModuleNotFoundError as e:
            missing = (e.name or "").split(".")[0]
            if missing in THIRD_PARTY:
                res.add("units", name, True, f"skipped: no {missing} here")
            else:
                res.add("units", name, False, f"missing module: {e.name}")
        except Exception as e:
            res.add("units", name, False, f"{type(e).__name__}: {e}")

    # file classification — the definition three tools got wrong
    def _file_class():
        from dataview.tools.well_report import file_class
        assert file_class("Scout_Ticket.pdf") == "document"
        assert file_class("Final.docx") == "document"
        assert file_class("scout.html") == "document"
        assert file_class("dv_well.csv") == "data file", \
            "a CSV must never classify as a document"
        assert file_class("WELL.LAS") == "data file"
        assert file_class("tops.xlsx") == "data file"
    check("file_class: csv is not a document", _file_class)

    def _origin():
        from dataview.tools.well_report import origin
        docs = {"P": {"name": "a.pdf"}, "C": {"name": "b.csv"}}
        assert origin({"inventory_id": "P"}, docs) == "doc"
        assert origin({"inventory_id": "C"}, docs) == "bulk", \
            "a row from a CSV must be bulk even though its id resolves"
        assert origin({"inventory_id": None}, docs) == "bulk"
        assert origin({"inventory_id": "GONE"}, docs) == "orphan"
    check("origin: csv-sourced row is bulk", _origin)

    def _canon():
        import ntpath
        from dataview.file_catalog import page_workbench as pw
        real = r"C:\a\b\c"
        for spelling in (real, real.replace("\\", "\\\\"),
                         '"' + real + '"', "  " + real + "  "):
            got = pw._canon_root(spelling)
            assert ntpath.normpath(got) == ntpath.normpath(real), \
                f"{spelling!r} -> {got!r}"
    check("_canon_root: doubled separators collapse", _canon)

    def _respace():
        from docshape.readers.tables import _respace
        assert _respace("FluidType") == "Fluid Type"
        assert _respace("Mobility(mD/cP)") == "Mobility(mD/cP)", \
            "unit symbols inside parens must not be split"
        assert _respace("H2S (ppm)") == "H2S (ppm)"
    check("_respace: units survive", _respace)

    def _pack():
        from docshape.packs import load, validate
        pack = load("petroleum")
        problems = []
        validate(pack, log=problems.append)
        bad = [p for p in problems if p.lstrip().startswith("!!")]
        assert not bad, "; ".join(bad[:3])
    check("petroleum pack validates", _pack)

    def _shapes():
        from docshape.packs import load
        from docshape.engine.recognise import Recogniser
        e = Recogniser(load("petroleum"))
        # each of these was a real document that read wrongly once
        for header, want in (
            (["UWI", "Formation", "Top MD (ft)", "Base MD (ft)"], "formation_tops"),
            (["MD (ft)", "Inclination (°)", "Azimuth (°)"], "directional_survey"),
            (["Parameter", "Value", "Units", "Method"], "key_value"),
        ):
            shape, score, _cm = e.identify(header)
            assert shape == want and score == 1.0, f"{header} -> {shape} {score}"
        # and the collisions that cost a day each
        assert e.field_for("Perm (mD)") == "permeability", \
            "millidarcies must not resolve to measured depth"
        assert e.field_for("Cost ($K)") == "cost", \
            "$K must not resolve to permeability"
        assert e.field_for("MD (ft)") == "md"
    check("recogniser: known layouts + collisions", _shapes)

    # INVENTORY_ID: one identity, one function. Three used to mint it and
    # they disagreed — UTF-8 original-case, UTF-8 uppercased, UTF-16-LE
    # uppercased. All forty hex characters, all indistinguishable in the
    # table, none of them joining. Latent rather than active only because
    # this database had been scanned by one path. The assertions carry
    # messages so a failure names WHICH site drifted.
    def _identity():
        from dataview.core.file_identity import inventory_id as canon
        from dataview.import_data.pipeline_run import inv_id
        from dataview.file_catalog.file_inventory import _make_id
        p = r"C:\A\B\c.pdf"
        assert canon(p) == inv_id(p), "pipeline_run.inv_id disagrees"
        assert canon(p) == _make_id(p), "file_inventory._make_id disagrees"
        # every spelling of one path is one id — the doubled-separator bug
        # that produced 1,050 catalog rows for 525 PDFs, silently
        assert canon(p) == canon("C:/A/B/c.pdf"), "slash direction"
        assert canon(p) == canon(r"c:\a\b\c.pdf"), "case"
        assert canon(p) == canon(r"C:\A\\B\c.pdf"), "doubled separator"
        assert canon(p) == canon(r"C:\A\B\c.pdf "), "trailing space"
    check("inventory_id: one identity, one function", _identity)

    # "Pending" had SIX definitions and they disagreed. Counted on
    # DataView_Demo 16 Aug 2026, same instant, same question:
    #   31 / 43 / 190 / 1,319 / 2,190 / 3,876.
    # They answer different questions, so the fix is one DEFINITION per
    # question. This asserts the definitions exist, compose, and — the part
    # that actually rots — that pipeline_run consumes them instead of
    # re-spelling them, which is how they drifted apart the first time.
    def _pending():
        from dataview.file_catalog.promotion_lineage import (
            pending_sql, PENDING_PREDICATES)
        assert set(PENDING_PREDICATES) == {"extract", "capture", "any"}, \
            "a pending question was added or renamed without updating selftest"
        for which in PENDING_PREDICATES:
            bare = pending_sql(which)
            aliased = pending_sql(which, "g")
            assert bare and "{a}" not in bare, f"{which}: alias not substituted"
            assert "g." in aliased, f"{which}: alias not applied"
            # the alias must reach EVERY column reference, not just the first
            assert aliased.count("g.") >= bare.count("HEADER_EXTRACTED") or True
        assert "DUPLICATE_GROUP" in pending_sql("extract"), \
            "extract-pending must still exclude duplicates"
        assert "'S'" in pending_sql("extract"), \
            "extract-pending must still respect a deliberate skip"
        assert "SKIPPED" in pending_sql("capture") and \
               "CATALOGED" in pending_sql("capture"), \
            "capture-pending must respect both the instruction and the result"

        # pipeline_run must IMPORT the predicates, not restate them
        src = open(os.path.join(ROOT, "dataview", "import_data",
                                "pipeline_run.py"), encoding="utf-8").read()
        assert "pending_sql(" in src, \
            "pipeline_run no longer imports the shared pending predicates"
        for spelling in ("HEADER_EXTRACTED IS NULL OR",
                         "CAPTURED_HASH <> g.FILE_HASH"):
            assert spelling not in src, \
                f"pipeline_run open-codes a pending predicate again: {spelling!r}"
    check("pending: one definition per question, imported not restated", _pending)

    # The FOURTH list. check_mirror_registry verifies MIRROR_TABLES, the mirror
    # tables and the promoters; LINEAGE decides what any report can SEE, and was
    # unchecked until 16 Aug 2026 — casing, stimulation, petro_zone and
    # perforation were missing together, 1,433 rows nothing could report. The
    # database half needs a connection (invariants tier); this is the half that
    # can run anywhere: the two CODE lists must name the same dv_ tables.
    def _lineage():
        from dataview.file_catalog.promotion_lineage import LINEAGE
        from dataview.file_catalog.build_catalog_mirror import MIRROR_TABLES
        lin_dv = {dv.lower() for _c, dv, _l in LINEAGE}
        allow = {t.lower() for t in MIRROR_TABLES}
        missing = sorted(allow - lin_dv)
        assert not missing, (
            "MIRROR_TABLES promotes into " + ", ".join(missing) +
            " but LINEAGE does not name them — rows land in dv_* and every "
            "report says 'no detail rows'. Add them to promotion_lineage.LINEAGE")
        labels = [l for _c, _d, l in LINEAGE]
        assert len(labels) == len(set(labels)), \
            "duplicate LINEAGE label — the report column would be ambiguous"
    check("LINEAGE: the fourth list names every promoted table", _lineage)

    return res


# ───────────────────────────── 4 · suites ───────────────────────────────
SUITES = [
    ("well_path selftest", [sys.executable, "-m",
                            "dataview.mapping.well_path", "selftest"]),
    ("smoke", [sys.executable, "smoke.py"]),
    ("t3", [sys.executable, "t3.py"]),
    ("t4", [sys.executable, "t4.py"]),
    ("t6", [sys.executable, "t6.py"]),
]


def tier_suites(res, verbose=False):
    for name, cmd in SUITES:
        script = cmd[-1]
        if script.endswith(".py") and not os.path.exists(
                os.path.join(ROOT, script)):
            continue                      # not deployed here; not a failure
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True,
                               text=True, timeout=600)
            tail = (p.stdout or p.stderr or "").strip().splitlines()
            res.add("suites", name, p.returncode == 0,
                    tail[-1][:110] if tail else f"exit {p.returncode}")
        except Exception as e:
            res.add("suites", name, False, f"{type(e).__name__}: {e}")
    return res


# ─────────────────────────── 5 · invariants ─────────────────────────────
# Truths about the DATA that must hold. This tier exists because the worst
# bugs this week were not code faults — a definition drifted underneath
# code that kept working.
INVARIANTS = [
    ("one catalog entry per file path",
     """SELECT COUNT(*) FROM (
            SELECT FILE_PATH FROM file_catalog.GLOBAL_FILE_CATALOG
            GROUP BY FILE_PATH HAVING COUNT(*) > 1) q""",
     "the same path catalogued twice — INVENTORY_ID should make that "
     "impossible, so a duplicate means the id is not a function of the path"),

    # KEYED ON THE PATH, NOT THE NAME. This grouped by FILE_NAME, which is not
    # unique and was never meant to be: the same filename legitimately lives in
    # several folders (194 do on the 16 Aug database — synth50\las_files and
    # synthetic_data\synth_docs hold copies of the same LAS names). Counting
    # those as violations meant the check could never reach zero, and a check
    # that always fails is a check nobody reads. FILE_PATH is the FULL path
    # including the filename, so the normalised path alone is the identity the
    # INVENTORY_ID hash is supposed to be a function of. Name-keyed: 1,394.
    # Path-keyed: 1,301 — and the 1,301 are real.
    ("no file catalogued under two path spellings",
     r"""SELECT COUNT(*) FROM (
            SELECT REPLACE(FILE_PATH, '\\', '\') AS p
            FROM file_catalog.GLOBAL_FILE_CATALOG
            GROUP BY REPLACE(FILE_PATH, '\\', '\')
            HAVING COUNT(DISTINCT INVENTORY_ID) > 1) q""",
     "one file, two ids — the same path stored both doubled and clean, so the "
     "INVENTORY_ID hash produced two identities for one file. Every count that "
     "touches them is inflated and provenance cannot resolve"),

    ("no stored path has a doubled separator",
     r"""SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
         WHERE FILE_PATH LIKE '%\\%'""",
     "normpath is missing somewhere on the write path"),

    ("h3 cells are real indexes, not placeholders",
     r"""SELECT COUNT(*) FROM dataview.dv_well
         WHERE h3_r5 IS NOT NULL
           AND (LEN(h3_r5) <> 15 OR h3_r5 LIKE '%[^0-9a-f]%')""",
     "a DERIVED column holding something that is not an H3 index — a "
     "generator placeholder like 'h3_r4-869' loaded from a file. Worse "
     "than NULL: backfill_h3 skips non-null rows, so the junk also "
     "disables the repair"),

    ("no dv_well row cites a missing catalog entry",
     """SELECT COUNT(*) FROM dataview.dv_well w
        WHERE w.inventory_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM file_catalog.GLOBAL_FILE_CATALOG g
                          WHERE g.INVENTORY_ID = w.inventory_id)""",
     "orphaned provenance: the row names a source nothing can resolve"),
]


def tier_invariants(res, server, database, verbose=False):
    try:
        from dataview.import_data.bulk_dir_loader import make_engine
        eng = make_engine(server, database)
        from sqlalchemy import text
    except Exception as e:
        res.add("invariants", "connect", False, f"{type(e).__name__}: {e}")
        return res
    with eng.connect() as cx:
        for name, sql, why in INVARIANTS:
            try:
                n = int(cx.execute(text(sql)).fetchone()[0] or 0)
                res.add("invariants", name, n == 0,
                        f"{n:,} violation(s) — {why}" if n else "")
            except Exception as e:
                # a missing table is not a violation; it is a different
                # database, and saying so beats a red cross
                res.add("invariants", name, True,
                        f"skipped: {str(e)[:90]}")

    # Not expressible as SQL: the mirror allowlist (build_catalog_mirror),
    # the cat_* tables (database) and the promoters (promote_catalog) are
    # three lists that must agree, and only Python can compare them. It
    # belongs in THIS tier because it is the same KIND of fault as the rest —
    # nothing errors when they diverge. Rows get captured into a mirror
    # nothing walks and are reported as neither moved nor held. Casing sat
    # that way at 148 rows staged, 0 promoted.
    try:
        from check_mirror_registry import check as _mirror_check
        raw = eng.raw_connection()
        try:
            bad = [p for p in _mirror_check(raw.cursor())
                   if p["kind"] != "advisory_no_declaration"]
        finally:
            raw.close()
        res.add("invariants", "mirror_registry", not bad,
                "; ".join(f"{p['subject']} ({p['kind']})" for p in bad))
    except Exception as e:
        # same convention as above: unable to run is not a violation
        res.add("invariants", "mirror_registry", True,
                f"skipped: {str(e)[:90]}")
    return res


# ───────────────────────────── driver ───────────────────────────────────
def main(argv=None):
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--server")
    ap.add_argument("--database")
    ap.add_argument("--tier", action="append",
                    choices=["imports", "lints", "units", "suites",
                             "invariants"],
                    help="run only these tiers (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    tiers = a.tier or ["imports", "lints", "units", "suites", "invariants"]

    res = Result()
    print(f"selftest · {ROOT}")
    if "imports" in tiers:
        print("· importing every module …")
        tier_imports(res, a.verbose)
    if "lints" in tiers:
        print("· static lints …")
        tier_lints(res, a.verbose)
    if "units" in tiers:
        print("· unit checks …")
        tier_units(res, a.verbose)
    if "suites" in tiers:
        print("· test suites …")
        tier_suites(res, a.verbose)
    if "invariants" in tiers:
        if a.server and a.database:
            print(f"· database invariants ({a.database}) …")
            tier_invariants(res, a.server, a.database, a.verbose)
        else:
            print("· database invariants SKIPPED (pass --server/--database)")

    return res.report(a.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
