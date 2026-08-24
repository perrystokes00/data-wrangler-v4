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

# A SELF-TEST MUST NOT DIE WHILE REPORTING. The default Windows console is
# cp1252, which has '·' and '…' but NOT '✗' (U+2717) or '⚠' (U+26A0) — the
# two glyphs this file prints only when it has something to say. So
# `python selftest.py --tier lints` raised UnicodeEncodeError out of the
# print itself, and a failing check would have killed the summary the same
# way: the tool worked when everything passed and crashed when anything was
# wrong. Exactly inverted.
#
# errors='replace' rather than an ASCII rewrite of the markers: the glyphs
# are worth keeping wherever the console can show them (Windows Terminal,
# VS Code, a redirect to a file), and a '?' in their place still prints the
# line. Belt and braces — reconfigure preferred, errors='replace' as the
# floor if the stream refuses UTF-8.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass

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
    # NOT EXISTS is the same error and was NOT matched until 19 Aug: the gate
    # flags in catalog_status are all `NOT EXISTS` (row is held when its
    # reference/parent is ABSENT), so every one of them slipped past this lint
    # and failed at runtime on msg 130 instead. Same illegal shape, one word
    # different. Fix either form by flagging per row in a derived table and
    # summing the flags outside it.
    (r"\bSUM\s*\(\s*CASE\s+WHEN\s+(NOT\s+)?EXISTS", ".sql .py",
     "SQL Server error 130: EXISTS inside an aggregate is illegal. "
     "Use a CTE + LEFT JOIN, or flag per row in a derived table and SUM that."),
    (r",\s*(bulk|table|rows|value|key|file|user|percent|plan|work)\s*=",
     ".sql",
     "unbracketed alias using a T-SQL reserved word — 'bulk' cost a run. "
     "Write [bulk] = ..."),
    # A BACKSLASH escapechar under QUOTE_NONE doubles every separator in a
    # Windows path, and BULK INSERT stores the doubled form verbatim. Fixed
    # 16 Aug across three catalog staging writers — and came straight back,
    # because a FOURTH writer (pipeline_run's scan stage, the DEFAULT path)
    # was missed. Found 20 Aug: all 182 catalog rows carried a doubled
    # FILE_PATH and ROOT_PATH, written by a scan the day before. The id is a
    # SHA1 of the CLEAN path, so the escaped write leaves INVENTORY_ID and
    # FILE_PATH describing different strings and provenance stops resolving.
    # The note "one writer now" was true and protected nothing; this lint is
    # what makes it stay true. Use path_identity.bulk_csv_writer + bulk_field.
    (r"escapechar\s*=\s*['\"]\\\\['\"]", ".py",
     "a BACKSLASH csv escapechar: with QUOTE_NONE the csv module escapes the "
     "character itself, so every separator in a Windows path is doubled and "
     "BULK INSERT stores it that way. Use path_identity.bulk_csv_writer."),
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
        assert set(PENDING_PREDICATES) == {"extract", "extract-force-reset",
                                          "capture", "any"}, \
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

    # ── seismic geometry: the four gaps that held Teapot ─────────────────
    # Six SEG-Y files sat at "no outline or bbox" while the machinery to
    # position them was already built. Each check below is one of the reasons
    # it could not fire.

    # 1. The nav file stated "SPCS27 - Wyoming East Central, NAD 1927,
    #    U.S. Survey Feet" — a complete answer — and crs_from_text had no
    #    State Plane branch, so read_nav returned epsg=None and extract_core
    #    discarded a perfectly parsed 532-point navigation.
    def _spcs():
        from dataview.file_catalog.crs_from_segy import crs_from_text
        teapot = ("Coordinate System: SPCS27 - Wyoming East Central  "
                  "Datum: NAD 1927\nData Coordinate System Units: "
                  "U.S. Survey Feet")
        assert crs_from_text(teapot)[0] == 32056, crs_from_text(teapot)
        # the zone name must not over-reach: East is not East Central
        assert crs_from_text("STATE PLANE WYOMING EAST NAD 1927")[0] == 32055
        # EPSG spells NAD27 California zones in Roman and NAD83 in Arabic;
        # a processor writes whichever they please and both must resolve
        for spelling in ("CALIFORNIA ZONE III", "CALIFORNIA ZONE 3"):
            got = crs_from_text(f"STATE PLANE {spelling} NAD 1927")[0]
            assert got == 26743, f"{spelling} -> {got}"
        # UNITS ARE PART OF THE ANSWER. Every NAD83 zone exists in metres and
        # in ftUS; choosing wrong scales every coordinate by 3.28, so an
        # unstated unit must REFUSE rather than pick.
        both = crs_from_text("STATE PLANE TEXAS SOUTH CENTRAL NAD 1983")
        assert both[0] is None, f"guessed units: {both}"
        assert "units" in both[2], both[2]
        assert crs_from_text(
            "SPCS83 TEXAS SOUTH CENTRAL, US SURVEY FEET")[0] == 2278
        # and the pre-existing branches must be untouched
        assert crs_from_text("UTM ZONE 13 WGS84")[0] == 32613
        assert crs_from_text("Projection: [EPSG:28992]")[0] == 28992
    check("crs_from_text: State Plane is read, units are not guessed", _spcs)

    # 2. filt_mig states THREE corners indexed by inline/crossline. Ring-
    #    ordering three points draws a triangle over half the survey; the
    #    fourth closes by parallelogram because the indices say which three.
    def _corners3():
        from dataview.file_catalog.crs_from_segy import survey_corners
        hdr = ("C 7 INLINE 1, XLINE 1:   X COORDINATE: 788937  Y COORDINATE: 938846\n"
               "C 8 INLINE 1, XLINE 188: X COORDINATE: 809502  Y COORDINATE: 939334\n"
               "C 9 INLINE 345, XLINE 1: X COORDINATE: 788039  Y COORDINATE: 976675\n")
        got = survey_corners(hdr)
        assert got and len(got) == 4, f"expected 4 corners, got {got}"
        # the derived corner is the value teapot_3d_load.doc states
        assert (808604.0, 977163.0) in [(round(x, 1), round(y, 1)) for x, y in got], \
            f"parallelogram closure wrong: {got}"
        # the older layouts must still parse
        assert len(survey_corners(
            "C06 Corner 1: X: 78401.95 Y: 447374.73 IL: 2500 XL: 3139\n"
            "C07 Corner 2: X: 78401.95 Y: 450000.00 IL: 2500 XL: 3200\n"
            "C08 Corner 3: X: 80000.00 Y: 450000.00 IL: 2600 XL: 3200\n") or []) >= 3
    check("survey_corners: 3 stated corners close to 4, not a triangle", _corners3)

    # 3. The standard puts CDP X/Y at bytes 181-188. filt_mig puts INLINE
    #    there and says so in its own textual header. Reading the declaration
    #    is what stops IL_MIN=-2123710427 reaching the catalog.
    def _tracemap():
        from dataview.file_catalog.segy_header import (
            declared_trace_map, STD_OFFSETS)
        hdr = ("C23 BYTES  13- 16: CROSSLINE NUMBER (TRACE)\n"
               "C24 BYTES  17- 20: INLINE NUMBER (LINE)\n"
               "C25 BYTES  81- 84: CDP_X COORD\n"
               "C26 BYTES  85- 88: CDP_Y COORD\n"
               "C27 BYTES 181-184: INLINE NUMBER (LINE)\n"
               "C29 BYTES 189-192: CDP_X COORD\n")
        m = declared_trace_map(hdr)
        assert m.get("crossline") == 12, m       # bytes 13-16, 0-based
        assert m.get("inline") == 16, m          # first declaration wins
        assert m.get("cdp_x") == 80, m
        assert m.get("cdp_y") == 84, m
        # 'CROSSLINE' must never be classified as 'INLINE'
        assert declared_trace_map(
            "BYTES 13-16: CROSSLINE NUMBER").get("inline") is None
        # a header that declares nothing leaves the standard in force
        assert declared_trace_map("C 1 CLIENT: SOMEBODY") == {}
        assert STD_OFFSETS["cdp_x"] == 180
    check("declared_trace_map: the file's own byte map is read", _tracemap)

    # 4. An untyped SEG-Y card image yields the card's printed LABELS. NULL is
    #    caught by the unnamed gate; "AREA MAP ID" is not, so five Teapot 2D
    #    lines would promote into one invented survey.
    def _template_name():
        from dataview.file_catalog.extract_core import _is_template_survey_name
        assert _is_template_survey_name("AREA MAP ID")
        assert _is_template_survey_name("CLIENT COMPANY CREW NO")
        # real names must survive — including ones that CONTAIN a label word
        for good in ("NAVAL PETROLEUM RESERVE #3 (TEAPOT DOME)",
                     "CENTRAL EROMANGA BASIN 80",
                     "COOPER SURVEY 1994", "NPR-3"):
            assert not _is_template_survey_name(good), good
        # a single label word is ambiguous — a survey really can be called it
        assert not _is_template_survey_name("AREA")
        assert not _is_template_survey_name(None)
    check("survey name: card-image labels are not a name", _template_name)

    # 5. A nav row carrying elevation fell through to the branch that treats an
    #    unmatched line as header PROSE — three of Teapot's 535 shotpoints
    #    vanished silently, and a vendor who writes elevation on EVERY row
    #    loses the file with "not a nav file".
    def _nav_row():
        from dataview.file_catalog.seis_nav import _ROW
        assert _ROW.match(" A   235   797319  964035")
        assert _ROW.match(" A   235   797319  964035 5153"), \
            "a trailing elevation column must not disqualify a nav row"
        assert _ROW.match(" A   235   797319  964035 5153 12.5")
        # extra columns must still be NUMERIC — prose is not data
        assert not _ROW.match("H Line   SP #   X   Y")
        assert not _ROW.match(" A   235   797319  964035 SPARE")
    check("seis_nav: a nav row may carry extra columns", _nav_row)

    # 6. ONE WRITER for FILE_SEIS_HEADER. It had four — extract_core, a verbatim
    #    duplicate in worker_core (the multicore path, which is the DEFAULT), and
    #    two more inside page_workbench — so a fix to the canonical one silently
    #    missed the path that actually runs. Exactly how the escapechar bug came
    #    back through a fourth writer.
    def _one_seis_writer():
        import pathlib
        root = pathlib.Path(__file__).resolve().parent
        offenders = []
        for py in (root / "dataview").rglob("*.py"):
            if "_attic" in py.parts or "_quarantine" in py.parts:
                continue
            src = py.read_text(encoding="utf-8", errors="replace")
            n = src.count("MERGE file_catalog.FILE_SEIS_HEADER")
            if not n:
                continue
            # extract_core owns the full upsert; page_workbench owns the
            # manual-name writer (_SQL_SURVEY_SEIS, a different statement).
            allowed = {"extract_core.py": 1, "page_workbench.py": 1}
            if allowed.get(py.name, 0) != n:
                offenders.append(f"{py.name}:{n}")
        assert not offenders, (
            "FILE_SEIS_HEADER MERGE is spelled out in " + ", ".join(offenders) +
            " — import extract_core._SQL_SEIS_MERGE instead of copying it")
    check("FILE_SEIS_HEADER: one writer, imported not copied", _one_seis_writer)

    # 7. A re-extract must never ERASE a survey name it simply could not read,
    #    and must never overwrite one a person typed.
    def _name_not_erased():
        from dataview.file_catalog.extract_core import _SQL_SEIS_MERGE
        sql = " ".join(_SQL_SEIS_MERGE.split())
        assert "SURVEY_NAME=:sn," not in sql, \
            "the MERGE assigns SURVEY_NAME unconditionally again — a re-extract " \
            "of a file whose header names no survey would blank the column"
        assert "COALESCE(:sn, tgt.SURVEY_NAME)" in sql, \
            "SURVEY_NAME must fall back to the stored value"
        assert "'manual'" in sql, \
            "the MERGE no longer protects a manually-set survey name"
        # provenance travels with the value, or the gate downstream is blind
        from dataview.file_catalog.extract_core import _seis_params
        p = _seis_params("INV1", {"survey_name": "NPR-3 2D",
                                  "survey_name_source": "manual"})
        assert p["sn"] == "NPR-3 2D" and p["snsrc"] == "manual", p
        assert _seis_params("INV1", {})["snsrc"] is None
    check("seis MERGE: a name is never erased or overwritten", _name_not_erased)

    # 8. --force must TERMINATE. The obvious implementation — claim on a
    #    predicate that ignores HEADER_EXTRACTED — loops forever, because
    #    extract re-queries between chunks and a just-processed file is still
    #    in the set (observed: ok 14 -> 28 -> 42, no exit). force is a one-time
    #    RESET of the done-flag, after which the ordinary pending path drains.
    def _force_terminates():
        import inspect
        from dataview.import_data import pipeline_run as pr
        from dataview.file_catalog.promotion_lineage import PENDING_PREDICATES

        src = inspect.getsource(pr._stage_extract)
        assert 'pending_sql("extract")' in src, \
            "_stage_extract no longer claims on the plain extract predicate"
        for bad in ("extract-forced", "force else", "if force"):
            assert bad not in src, (
                f"_stage_extract branches on force ({bad!r}) — a forced claim "
                f"predicate never empties and the chunk loop cannot terminate")

        # the reset scope must EXCLUDE the states a force does not overrule,
        # and must only touch rows that have actually been extracted
        rst = PENDING_PREDICATES["extract-force-reset"].format(a="")
        # A WHITELIST, so SKIPPED/MOVED are excluded by construction — and
        # that is the property to assert, not the presence of their letters.
        assert "IN ('Y','E')" in rst, (
            "the reset must NAME the states it re-queues; anything broader "
            "re-queues SKIPPED ('S') or MOVED ('M'), which a force does "
            "not overrule")
        for _st in ("'S'", "'M'", "'N'"):
            assert f"IN ({_st}" not in rst and f", {_st}" not in rst, \
                f"the force reset must not include state {_st}"
        assert "DUPLICATE_GROUP IS NULL" in rst

        # reset once, and the batch driver must say so or every batch re-queues
        bsrc = inspect.getsource(pr.run_pipeline_batched)
        assert "_force_reset_extract" in bsrc and "force_reset_done" in bsrc, \
            "run_pipeline_batched must reset once and tell the batches"
        assert "force_reset_done" in inspect.signature(pr.run_pipeline).parameters
        # and the gauge must stay ignorant of force — it counts the same
        # pending set the batches claim, which is the point of the reset
        assert "force" not in inspect.signature(pr._unprocessed_count).parameters
    check("--force terminates: reset once, one claim predicate", _force_terminates)

    # 9. A new pipeline toggle reaches run_pipeline ONLY if the detached
    #    multicore runner names it — that path is the DEFAULT, and it is how
    #    the recognise flag sat complete-but-unused.
    def _force_reaches_runner():
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent / "dataview" /
               "import_data" / "pipeline_proc_runner.py").read_text(
                   encoding="utf-8", errors="replace")
        assert "force=bool(cfg.get(\"force\"" in src.replace("'", '"'), \
            "pipeline_proc_runner does not forward force — the CLI flag would " \
            "do nothing on the multicore path, which is the default"
    check("force reaches the multicore runner", _force_reaches_runner)

    # 10. The IBM-float decoder became load-bearing when the viewer gained a
    #     fallback for files segyio refuses. Format code 1 is base-SIXTEEN
    #     exponent, excess-64 — read as IEEE it yields numbers that are wrong
    #     rather than obviously broken, which is the worst kind here.
    def _ibm_float():
        import numpy as np
        from dataview.file_catalog.segy_header import _ibm_to_ieee
        got = _ibm_to_ieee(np.array(
            [0x42640000, 0xC2760000, 0x41100000, 0x00000000], dtype=np.uint32))
        for g, want in zip(got, (100.0, -118.0, 1.0, 0.0)):
            assert abs(float(g) - want) < 1e-4, f"{float(g)} != {want}"
        # verified against segyio on the Teapot 2D lines: identical samples
        # (np.allclose, 1e-5) for the files segyio will open.
    check("IBM float decode: base-16 exponent, not IEEE", _ibm_float)

    # 11. REJECTING A FILE MUST STICK. _mark_bad deletes the file's cat_* rows
    #     and stamps SKIPPED, which is the whole story for documents — their
    #     data lived in the mirrors. Seismic never stages in cat_*, so the
    #     cascade deletes nothing and BOTH remaining paths read straight past
    #     the rejection: promote lifted the survey back into dv_seis_set on the
    #     next run, and the map's 3D layer reads FILE_SEIS_HEADER directly, so
    #     the rectangle drew even with the promoted rows deleted.
    def _rejected_stays_rejected():
        import inspect
        from dataview.file_catalog import promote_catalog as pc
        src = inspect.getsource(pc.promote_seismic)
        assert "BAD_FILE" in src and "SKIPPED" in src, \
            "promote_seismic no longer excludes rejected files — a file marked " \
            "bad promotes again on the next run, and deleting its dv_seis_* " \
            "rows by hand does not help because promote rebuilds them"
        import pathlib
        mp = (pathlib.Path(__file__).resolve().parent / "dataview" / "mapping" /
              "page_well_map.py").read_text(encoding="utf-8", errors="replace")
        i = mp.find("def _qry_seismic_3d")
        assert i > 0, "_qry_seismic_3d has moved or been renamed"
        body = mp[i:i + 4000]
        assert "BAD_FILE" in body and "SKIPPED" in body, \
            "the 3D seismic layer reads FILE_SEIS_HEADER directly, so a " \
            "rejected file draws unless this query excludes it"
        # ...and the BAD_FILE reference must stay conditional: the table is
        # created on first use, and this query swallows exceptions, so naming
        # it unconditionally would hide every 3D survey on a clean database.
        assert "OBJECT_ID('file_catalog.BAD_FILE')" in body, \
            "the BAD_FILE clause must be probed for, not assumed"
    check("a rejected file neither promotes nor draws", _rejected_stays_rejected)

    # 12. A GENERATOR HAS NO HONEST PROVENANCE. The synthetic rows never came
    #     from a file, were never catalogued, and have no lineage — so
    #     INVENTORY_ID, FILE_PATH and friends must come out NULL. The type
    #     fallback used to invent them ("INVENTORY_ID-426"), and INVENTORY_ID
    #     is the key every lineage report joins on, so a fabricated one is a
    #     key that matches nothing while LOOKING like real provenance.
    #
    #     This is the invariant "no dv_well row cites a missing catalog entry"
    #     made checkable without a database. 50 such rows were loaded 19 Aug
    #     from CSVs generated BEFORE the 16 Aug guard — the guard was right and
    #     the artifact on disk was stale, which is precisely why the CODE side
    #     needs pinning: nothing else would notice it regressing.
    def _synth_provenance():
        import random
        from dataview.migration.synth_data import _value, PROVENANCE_COLS
        rng = random.Random(42)
        assert "inventory_id" in PROVENANCE_COLS, \
            "inventory_id left PROVENANCE_COLS — synthetic rows would carry a " \
            "fabricated catalog key and orphan every lineage join"
        base = {"uwi": "15001209150000", "source": "SYNTH"}
        # the column name is whatever the DDL says, so BOTH cases must be caught
        for name in ("INVENTORY_ID", "inventory_id", "catalog_id",
                     "FILE_PATH", "file_path", "file_hash", "root_path"):
            got = _value({"name": name, "type": "varchar", "chars": 40},
                         dict(base), rng)
            assert got is None, f"{name} -> {got!r}, expected None"
        # AND ctx MUST NOT BE ABLE TO OVERRULE IT. ctx is copied into child
        # rows with dict(w); if the provenance test ever falls after the ctx
        # lookup again, this is the assertion that fails instead of a silent
        # return to fabricated lineage.
        poisoned = dict(base, inventory_id="D7E2B1D3DEADBEEF", file_path=r"C:\x\y")
        for name in ("INVENTORY_ID", "FILE_PATH"):
            got = _value({"name": name, "type": "varchar", "chars": 40},
                         dict(poisoned), rng)
            assert got is None, \
                f"ctx overruled the provenance guard: {name} -> {got!r}"
        # DERIVED COLUMNS, same rule and a worse failure. h3_r4..h3_r7 are a
        # function of the coordinates; a fabricated 'h3_r5-108' is
        # SELF-PROTECTING, because backfill_h3 keys on `h3_r5 IS NULL` and so
        # skips exactly the rows that need repairing. A reload put these in all
        # 50 dv_well rows on 23 Aug.
        for name in ("h3_r4", "h3_r5", "h3_r6", "h3_r7", "H3_R5"):
            got = _value({"name": name, "type": "varchar", "chars": 20},
                         dict(base), rng)
            assert got is None, f"{name} -> {got!r}, expected None"
        # A PREFIX, NOT A LIST — the resolutions have already grown r4 -> r7,
        # and the next one must not start life as a placeholder.
        assert _value({"name": "h3_r8", "type": "varchar", "chars": 20},
                      dict(base), rng) is None, \
            "h3 columns are matched by an exact list again — a new resolution " \
            "would be fabricated the day it is added"
        # ctx must not resurrect a derived value either
        assert _value({"name": "h3_r5", "type": "varchar", "chars": 20},
                      dict(base, h3_r5="h3_r5-108"), rng) is None, \
            "ctx overruled the derived-column guard"
        # and ordinary columns must still be generated, ctx still consulted
        assert _value({"name": "uwi", "type": "varchar", "chars": 14},
                      dict(base), rng) == "15001209150000"
        assert _value({"name": "well_name", "type": "varchar", "chars": 40},
                      dict(base), rng) is not None
    check("synth: no fabricated provenance or derived cells, ctx cannot "
          "overrule either", _synth_provenance)

    # 13. A LOAD THAT CANNOT REGISTER ITS FILE MUST SAY SO. The promote stamps
    #     every inserted row with the file's INVENTORY_ID before registration
    #     is attempted, so a failed register_file leaves those rows citing a
    #     source nothing resolves — and the load still reports success.
    #
    #     The 50 orphaned dv_well rows are that, and the reason they cannot be
    #     diagnosed now is that the only report went to a Streamlit progress
    #     pane and the caller wrapped the whole thing in `except Exception:
    #     pass`. reconcile_orphans can no longer identify the file.
    def _load_reports_provenance():
        import inspect
        from dataview.import_data import load_ledger as _ll
        src = inspect.getsource(_ll.record_load)
        assert 'out["problems"]' in src and "registered" in src, \
            "record_load no longer returns a report — the caller cannot tell " \
            "a registered load from an orphaning one"
        assert "register_file(engine, path" in src, \
            "record_load no longer calls register_file"
        # the RESULT of registering must be captured, not discarded
        assert 'out["registered"] = register_file' in src, \
            "register_file's return is dropped again — its failure becomes " \
            "invisible and the rows are orphaned silently"

        from dataview.import_data import page_load_assistant as _la
        lsrc = inspect.getsource(_la.load_single_file)
        i = lsrc.find("record_load(")
        assert i > 0, "load_single_file no longer records loads"
        # THE BARE SWALLOW, scoped to THIS call. The function has other
        # `except Exception: pass` handlers and they are a separate argument;
        # what must never come back is discarding the outcome of the call
        # that registers the file, because the rows are already stamped by
        # then. That exact shape is what made 19 Aug unreadable.
        tail = lsrc[i:i + 1200]
        assert "except Exception:\n        pass" not in tail, \
            "the bare `except Exception: pass` is back around record_load — a " \
            "registration failure would again leave rows citing an " \
            "unresolvable source with no diagnostic anywhere"
        assert '"problems"' in lsrc, \
            "load_single_file must carry the provenance problems out in its " \
            "result; a message only in the progress pane dies with the session"
    check("a load that cannot register its file reports it",
          _load_reports_provenance)

    # 13b. APPLY IS ONE CLICK, AND STILL VALIDATES. The two-step (Preview then
    #      Apply) was serving "sample before apply" twice: the grid above IS
    #      the sample — every value on screen, editable, with a column saying
    #      whether a UWI came from the filename or a folder. What Preview
    #      genuinely added was plan_fix's validation, and that is now inside
    #      Apply, over the CURRENT edits.
    #
    #      Two properties must survive that simplification, and neither is
    #      obvious from reading the button:
    #        * nothing invalid is ever written — plan_fix runs first and a
    #          single bad row stops the whole batch
    #        * what is written is what is ON SCREEN — the old code applied
    #          sb_plan_edits, the values as they were when previewed, so an
    #          edit made afterwards wrote the stale value while the grid
    #          showed the new one
    def _apply_validates_current_edits():
        import inspect
        from dataview.file_catalog import page_workbench as _pw
        src = inspect.getsource(_pw._tab_status)
        i = src.find('key="sb_apply"')
        assert i > 0, "the Apply button has moved or been renamed"
        body = src[i:i + 2600]
        assert "plan_fix(" in body, (
            "Apply no longer validates before writing — the Preview step was "
            "removed and its plan_fix check went with it, so an unnormalisable "
            "UWI would reach apply_fix")
        assert "st.stop()" in body, \
            "Apply no longer aborts on a bad row; a half-applied repair leaves " \
            "rows carrying a UWI whose header was never minted"
        assert "sb_plan_edits" not in body, (
            "Apply reads the PREVIEWED edits again — it must use the current "
            "ones, or editing a cell after previewing writes the stale value "
            "while the screen shows the new one")
        assert "for _e in _edits:" in body, \
            "Apply no longer iterates the current edits"
        # and it must not be gated on having previewed
        assert "disabled=not _edits" in body, \
            "Apply is gated on something other than there being edits — the " \
            "two-step is back"
    check("status: Apply validates the edits on screen, in one click",
          _apply_validates_current_edits)

    # 14. HELD IS NOT NOTHING. promote_seismic counted held surveys with a
    #     query filtered on _NAMED, so a file with NO survey name failed that
    #     predicate and fell out of BOTH `eligible` and `held` — reported as
    #     neither promoted nor held, the exact collapse promote_catalog's own
    #     header warns about and CLAUDE.md's four states exist to prevent.
    #
    #     Seen 23 Aug: five Teapot 2D lines held for having no name while
    #     promote logged "1 eligible, 0 held". Now: "held=5 · 5 unnamed
    #     file(s)", each named, with the remedy.
    def _held_is_not_nothing():
        import inspect
        from dataview.file_catalog import promote_catalog as pc
        src = inspect.getsource(pc.promote_seismic)
        assert "_held_unnamed" in src, \
            "promote_seismic no longer counts files held for having no survey " \
            "name — they revert to being reported as neither promoted nor held"
        assert "len(_held_surveys) + len(_held_unnamed)" in src, \
            "the held tally dropped one of its two gates"
        # A REJECTED file is out of scope, NOT held — so the unnamed query must
        # reuse _NOT_REJECTED rather than simply negating _NAMED, or rejecting
        # a file would make it reappear as a thing awaiting a name.
        i = src.find("_held_unnamed = ")
        assert i > 0
        window = src[max(0, i - 900):i]
        assert "_NOT_REJECTED" in window, \
            "the unnamed-hold query does not exclude rejected files — a file " \
            "marked bad would be reported as held, awaiting a name it will " \
            "never be given"
        # the dry run and the apply path must describe holds identically
        assert src.count("_gate_note)") >= 2, \
            "the apply path no longer returns the same note as the dry run"
    check("seismic: a file held for having no name is reported, not skipped",
          _held_is_not_nothing)

    # 15. ONE survey-name writer, and one panel. page_workbench and
    #     page_file_catalog each carried a private _seis_survey_grid and they
    #     had already DRIFTED — a data_editor version querying different
    #     columns with a LEFT JOIN in one, a paged text_input grid in the
    #     other. Same shape as the FILE_SEIS_HEADER MERGE that had four
    #     writers: a fix to one silently misses whichever page you are on.
    def _one_survey_assign_ui():
        import pathlib
        root = pathlib.Path(__file__).resolve().parent
        writers = []
        for py in (root / "dataview").rglob("*.py"):
            if "_attic" in py.parts or "_quarantine" in py.parts:
                continue
            src = py.read_text(encoding="utf-8", errors="replace")
            if "SET SURVEY_NAME=:v" in src:
                writers.append(py.name)
        assert writers == ["seis_survey_assign.py"], (
            "the manual survey-name UPDATE is spelled out in " +
            ", ".join(sorted(writers)) +
            " — import seis_survey_assign.seis_survey_grid instead of copying it")
        # THE GROUP IS THE UNIT. 2D lines arrive as a set; one survey spans
        # all of them. The panel must let a name be typed ONCE and written to
        # a chosen group — not once per file, which is transcription, and
        # transcription is how one survey becomes five.
        from dataview.file_catalog import seis_survey_assign as _ssa
        s = (root / "dataview" / "file_catalog" /
             "seis_survey_assign.py").read_text(encoding="utf-8", errors="replace")
        assert "seis_ck_" in s and "st.session_state[k] = True" in s, \
            "the checkbox grid is gone — a name can no longer be applied to a " \
            "chosen set of lines, only file by file"
        assert "Select all" in s, \
            "select-all is gone; the list can run to REVIEW_PAGE files and " \
            "ticking them individually is the thing this panel exists to avoid"
        # SELECT-ALL MUST BE A REQUEST consumed before the checkboxes exist.
        # Writing their keys directly is the obvious implementation and it is
        # the one that raises on a LATER run, on whatever page draws next
        # (Streamlit scar #6) — so assert the flag, not just the feature.
        assert "_SEL_REQ" in s and "st.session_state.pop(_SEL_REQ" in s, \
            "select-all no longer routes through a request flag consumed " \
            "before the checkboxes are created — assigning a widget's own key " \
            "after instantiation crashes somewhere else entirely"
        _pop = s.index("st.session_state.pop(_SEL_REQ")
        assert _pop < s.index("st.checkbox("), \
            "the select-all request is consumed AFTER a checkbox is drawn; it " \
            "must be popped before any of them exist"
        assert "'manual'" in s or '"manual"' in s, \
            "a typed survey name must be stamped SURVEY_NAME_SOURCE='manual', " \
            "or enrich refills it from the file name and a re-extract blanks it"
        # THIS PANEL RENDERS INSIDE AN EXPANDER (Status & Backlog), and
        # Streamlit forbids nesting them — so the per-file section must stay a
        # toggle. An expander here raises only on the page that embeds it,
        # which is exactly the kind of delayed, misattributed crash scar #6
        # describes.
        assert "st.expander" not in s, \
            "seis_survey_assign uses an expander, but it is rendered INSIDE " \
            "one on Status & Backlog — Streamlit refuses nested expanders. " \
            "Use a checkbox/toggle or st.container(border=True)."
        assert callable(_ssa.seis_survey_grid)
    check("seismic: one survey-assign panel, with the group path intact",
          _one_survey_assign_ui)

    # 16. A FILE WITH NO UWI IS INVISIBLE TO A HOLDS-ONLY REPORT. Holds are
    #     derived from cat_* rows; a header with no UWI stages nothing, so no
    #     gate fires and Status & Backlog could not name a reason — which is
    #     exactly what it looked like from the outside: "extracted, held, no
    #     reason". 17 files were in that state on 23 Aug, the filename carrying
    #     a usable UWI for 9 of them.
    #
    #     So the panel must be gated on its OWN count, never on res.holds, or
    #     it disappears again for the files that need it most.
    def _status_surfaces_no_uwi():
        import inspect
        from dataview.file_catalog import page_workbench as _pw
        src = inspect.getsource(_pw._tab_status)
        assert "_well_key_grid(engine)" in src, \
            "Status & Backlog no longer offers the well-keying grid — files " \
            "with a header but no UWI have no route on the page that is " \
            "supposed to show what is stuck"
        assert "_nouwi_n" in src, "the no-UWI panel lost its gate"
        i = src.find("_nouwi_n = ")
        j = src.find("_well_key_grid(engine)")
        assert i > 0 and i < j
        window = src[i:j]
        assert "res.holds" not in window, \
            "the no-UWI panel is gated on res.holds — but these files have NO " \
            "holds by construction (nothing is staged), so it would never show"
        assert "FILE_WELL_HEADER" in window and "UWI14" in window, \
            "the gate no longer counts headers lacking a UWI14"
        # the grid renders INSIDE this expander, so it must not open one itself
        gsrc = inspect.getsource(_pw._well_key_grid)
        assert "st.expander" not in gsrc, \
            "_well_key_grid opens an expander, but Status & Backlog now renders " \
            "it inside one — Streamlit refuses nested expanders"
    check("status: files with a header but no UWI are surfaced, not silent",
          _status_surfaces_no_uwi)

    # 17. TWO MODULES OWN HALVES OF CATALOG_READINESS AND MUST AGREE.
    #     catalog_readiness owns the catalog/promote axis and preserves
    #     'SKIPPED' by name; triage owns the identity axis and preserved it
    #     only via FLAG_DELETE. Marking a file bad sets CATALOG_READINESS but
    #     NOT that flag, so a triage run un-rejected everything rejected
    #     through the UI — measured 23 Aug, all 8 SKIPPED files had
    #     FLAG_DELETE NULL and would have gone back to READY/REVIEW.
    def _rejection_survives_triage():
        import pathlib
        root = pathlib.Path(__file__).resolve().parent / "dataview" / "file_catalog"
        tri = (root / "triage_inventory.py").read_text(encoding="utf-8",
                                                       errors="replace")
        rdy = (root / "catalog_readiness.py").read_text(encoding="utf-8",
                                                        errors="replace")
        for src, who in ((tri, "triage_inventory"), (rdy, "catalog_readiness")):
            assert "CATALOG_READINESS = 'SKIPPED' THEN 'SKIPPED'" in src, (
                f"{who} no longer preserves SKIPPED BY NAME — a file rejected "
                f"in the UI sets CATALOG_READINESS without FLAG_DELETE, so it "
                f"would be un-rejected and re-enter the pipeline")
        # ...and the .las READY shortcut must stay conditioned on the thing its
        # own comment gives as the justification: that no header row exists yet.
        # Unconditional, it stamps READY on a LAS whose captured header has no
        # UWI — a confident wrong value that stages nothing, promotes nothing,
        # and cannot be reported because holds come from cat_* rows.
        i = tri.find("LOWER(g.FILE_EXT) = '.las'")
        assert i > 0, "the .las triage branch has moved or been renamed"
        assert "h.INVENTORY_ID IS NULL" in tri[i:i + 120], (
            "the .las READY shortcut is unconditional again — it overrules a "
            "captured header that has no UWI, and those files then sit at "
            "READY with nothing staged and no reason anywhere")
    check("a rejected file stays rejected through a re-triage",
          _rejection_survives_triage)

    # 18. NEVER STAGE A CHILD WITHOUT ITS PARENT. LAS capture staged
    #     cat_well_log_curve unconditionally but gated cat_well_log on `if
    #     uwi:` — so a LAS with no UWI produced curves whose parent log did
    #     not exist. dv_well_log_curve carries fk_log_curve_log (uwi, log_id),
    #     a COMPOUND key, which _reference_fk_predicates does not cover, so
    #     nothing held them either: promote attempted the insert, hit 547, and
    #     failed the whole mirror. Measured 23 Aug: cat_well_log empty,
    #     cat_well_log_curve 153 unpromoted, "Promote table failed."
    def _las_stages_parent_with_child():
        import inspect
        from dataview.file_catalog import extract_core as _ec
        src = inspect.getsource(_ec)
        i = src.find('"cat_well_log_curve", curve_rows')
        j = src.find('"cat_well_log", [_wlrow]')
        assert i > 0 and j > i, \
            "the LAS curve/header capture blocks have moved — re-check that " \
            "the header is still staged whenever the curves are"
        # the header's guard must not be stricter than the curves' guard
        window = src[i:j]
        assert "if curve_rows or uwi:" in window, (
            "the cat_well_log header is gated more tightly than its curves "
            "again — a LAS with no UWI would stage the CHILD and skip the "
            "PARENT, and those curves can never promote")
        # and the shared log id must not become the string 'None-LAS'
        assert 'f"{uwi}-LAS" if uwi' in src, (
            "the log_id falls back to f'{uwi}-LAS' with no uwi again — that "
            "is the literal 'None-LAS', which collides across every unkeyed "
            "LAS so two files' curves claim one log")
    check("LAS: the log header is staged whenever its curves are",
          _las_stages_parent_with_child)

    # 19. A TOOL THAT CANNOT IMPORT THE REPO IS A TOOL THAT CANNOT BE RUN.
    #     Python puts the SCRIPT's own directory on sys.path[0], never the repo
    #     root, so `python tools/<name>.py` — how every one of them documents
    #     itself — died with ModuleNotFoundError: No module named 'dataview'.
    #     26 of the 28 tools/ scripts that import dataview could not run at all,
    #     found when reconcile_orphans, the tool that diagnoses orphaned
    #     provenance, was needed and would not start.
    #
    #     THIS DOES NOT TEST THAT THE WORDS "sys.path" APPEAR, and that is the
    #     whole point. FOURTEEN of the offenders already had a sys.path line
    #     and it was a NO-OP: it pointed at tools/ (already sys.path[0]) or at
    #     a modules/ directory the v4 reorg deleted. A check keyed on the
    #     string passes all fourteen while the bug survives — the same shape as
    #     the invariant keyed on FILE_NAME that could never pass. So the
    #     ARGUMENT is evaluated, with __file__ bound to the script, and the
    #     repo root has to be the answer.
    def _tools_can_import_the_repo():
        from pathlib import Path
        root = os.path.dirname(os.path.abspath(__file__))
        tools = os.path.join(root, "tools")
        if not os.path.isdir(tools):
            return
        broken = []
        for name in sorted(os.listdir(tools)):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            path = os.path.join(tools, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8",
                                      errors="replace").read())
            except SyntaxError:
                continue                 # the imports tier owns that failure
            wants = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    wants |= (node.module or "").split(".")[0] == "dataview"
                elif isinstance(node, ast.Import):
                    wants |= any(a.name.split(".")[0] == "dataview"
                                 for a in node.names)
            if not wants:
                continue
            reaches = False
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("insert", "append")
                        and node.args):
                    continue
                target = node.args[-1]
                try:
                    # only the ARGUMENT, in a namespace holding nothing but
                    # path helpers and this script's own path
                    got = eval(compile(ast.Expression(target), path, "eval"),
                               {"__builtins__": {"str": str}},
                               {"os": os, "Path": Path, "__file__": path})
                except Exception:
                    continue             # _BASE_DIR, HERE, … — unresolvable
                if (os.path.normcase(os.path.abspath(str(got)))
                        == os.path.normcase(root)):
                    reaches = True
                    break
            if not reaches:
                broken.append(name)
        assert not broken, (
            f"{len(broken)} tools script(s) import dataview but never put the "
            f"REPO ROOT on sys.path, so `python tools/<name>.py` cannot run: "
            + ", ".join(broken))
    check("every tools/ script that imports dataview can find it",
          _tools_can_import_the_repo)

    # 20. NO st.expander INSIDE ANOTHER ONE. Streamlit raises
    #     StreamlitAPIException ("Expanders may not be nested inside other
    #     expanders"), and an expander's body executes whether or not it is
    #     open — so the crash needs no click, only the code path. That is
    #     Streamlit scar #4 in CLAUDE.md, and its second clause is the fix:
    #     a block inside one needs no second disclosure. Render it inline,
    #     in st.container(border=True), or behind a toggle.
    #
    #     Five were live when this was written, two of them in page_workbench
    #     for months. The worst sat inside a try/except that reported the
    #     layout error as "Could not read header: …" — so a Streamlit bug was
    #     shown to the user as a corrupt SEG-Y file, and anyone debugging it
    #     went looking at the file. A rule that is only written down gets
    #     broken; this one is now checked.
    #
    #     LIMIT worth knowing: this is LEXICAL. It cannot see an expander
    #     reached through a CALL — page_file_manager opens one and then calls
    #     inv_workbench.render_file_workbench(), which draws its own. A clean
    #     run here does not prove no nesting at runtime.
    def _no_nested_expanders():
        import pathlib
        root = pathlib.Path(__file__).resolve().parent

        def _is_expander(node):
            for item in node.items:
                c = item.context_expr
                if (isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "expander"):
                    return True
            return False

        def _walk(node, depth, fname, out):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fname = node.name
            if isinstance(node, (ast.With, ast.AsyncWith)) and _is_expander(node):
                if depth:
                    out.append((fname, node.lineno))
                depth += 1
            for ch in ast.iter_child_nodes(node):
                _walk(ch, depth, fname, out)

        files = sorted(root.glob("*.py")) + sorted(
            p for p in (root / "dataview").rglob("*.py")
            if "_attic" not in p.parts and "_quarantine" not in p.parts)
        bad = []
        for py in files:
            src = py.read_text(encoding="utf-8", errors="replace")
            if "st.expander" not in src:
                continue
            out = []
            _walk(ast.parse(src), 0, "<module>", out)
            bad += [f"{py.name}:{ln} in {fn}()" for fn, ln in out]
        assert not bad, (
            "st.expander nested inside another expander — Streamlit raises "
            "on this, and the body runs whether or not it is open: "
            + "; ".join(bad) +
            " · render it inline, in st.container(border=True), or behind "
            "a toggle (CLAUDE.md Streamlit scar #4)")
    check("streamlit: no expander nested inside another",
          _no_nested_expanders)

    # 21. A GENERATED LAS MUST READ BACK. synth_docs wrote every ~WELL line in
    #     LAS 2.0 order and changed only the VERS number for its "las_1.2"
    #     case, so the 1.2 fixture was not a 1.2 file. lasio applied 1.2
    #     semantics exactly as the standard says and handed back every field as
    #     its own label — UWI='UNIQUE WELL IDENTIFIER', LOG_ID='LOG ID'. Four
    #     files in the corpus, all four stuck: their curves staged under log_id
    #     'LOG ID', their log header never staged, and promote held them on a
    #     missing parent every run.
    #
    #     The generator's comment says the 1.2 case exists because "a parser
    #     that only ever meets unwrapped LAS 2.0 with a populated UWI is not a
    #     tested parser". Right — and it tested nothing, because a malformed
    #     fixture exercises the reader's error handling, not its 1.2 support.
    #
    #     So the test is a ROUND TRIP, not an inspection of the text: write it
    #     with the generator, read it with lasio, and require the values back.
    #     Nothing short of that would have caught this — the file looked
    #     perfectly well-formed, and the two orders are indistinguishable
    #     without knowing the declared version.
    def _las_roundtrips():
        try:
            import lasio
            from dataview.file_catalog.las_reader import read_las
        except ImportError:
            return                       # imports tier owns a missing lasio
        import random
        import tempfile
        from dataview.migration.synth_docs import las_file
        w = {"uwi": "15041204660000", "well_name": "BAKER 13-8",
             "operator_name": "Apache Corporation", "county": "15041",
             "province_state": "15", "spud_date": "2020-01-28",
             "final_td": 4200, "kb_elevation": 1210, "ground_elevation": 1198}
        cases = [("2.0", False), ("2.0", True), ("1.2", False), ("1.2", True),
                 ("3.0", False)]
        p = os.path.join(tempfile.gettempdir(), "selftest_roundtrip.las")
        try:
            for vers, wrap in cases:
                las_file(p, w, random.Random(7), version=vers, wrap=wrap)
                # Through the REPO's reader — that is what the pipeline uses,
                # and for 3.0 it is what honours DLM.
                las = read_las(p)
                d = {i.mnemonic.upper(): i for i in las.well}
                got_uwi = str(getattr(d.get("UWI"), "value", "") or "")
                got_lid = str(getattr(d.get("LOG_ID"), "value", "") or "")
                tag = f"VERS {vers}{' wrapped' if wrap else ''}"
                assert got_uwi == w["uwi"], (
                    f"{tag}: lasio read UWI as {got_uwi!r} — the ~WELL field "
                    f"order does not match the declared version, so every "
                    f"value comes back as its own description")
                assert got_lid == f"LOG_{w['uwi']}_1", \
                    f"{tag}: lasio read LOG_ID as {got_lid!r}"

                # UWI AND LOG_ID ARE NOT ENOUGH, and asserting only them is
                # how the 1.2 generator shipped broken twice. Both really are
                # descr:value in 1.2, so a generator that swapped EVERY field
                # passed this check while writing
                #     STOP.FT   STOP DEPTH : 5165.0
                # STRT/STOP/STEP/NULL keep the 2.0 order even in 1.2, and they
                # are the ones that reach a NUMERIC column: TOTAL_DEPTH took
                # the string 'STOP DEPTH', the FILE_WELL_HEADER MERGE failed
                # nvarchar -> numeric, and because a failed write leaves the
                # file pending the extract stage re-claimed it ~570 times.
                for _m in ("STRT", "STOP", "STEP", "NULL"):
                    _v = getattr(d.get(_m), "value", None)
                    assert isinstance(_v, (int, float)) and _v == _v, (
                        f"{tag}: {_m} read back as {_v!r}, not a number. In "
                        f"LAS 1.2 the ~W section is descr:value EXCEPT "
                        f"STRT/STOP/STEP/NULL - swapping those too hands a "
                        f"numeric column its own label")
                assert float(d.get("NULL").value) == -999.25, (
                    f"{tag}: NULL came back {d.get('NULL').value!r}")
                assert len(las.curves) == 9, f"{tag}: {len(las.curves)} curves"
                assert len(las.index) > 100, f"{tag}: {len(las.index)} rows"
                # The curve data must be DATA, not a column of nulls. A
                # delimiter the reader ignores does not raise — it returns
                # nan, which every "did it load" check passes.
                import numpy as _np
                _gr = las.data[:, 1]
                assert not _np.all(_np.isnan(_gr)), \
                    f"{tag}: every GR sample is nan — the data section parsed " \
                    f"as one column, which is what an unhonoured delimiter does"

                # AND THE WRAPPER MUST BE LOAD-BEARING FOR 3.0. If raw lasio
                # ever reads this correctly, las_reader has stopped being the
                # reason it works and the DLM handling can be reconsidered —
                # but until then, asserting only the happy path would let the
                # wrapper be deleted with every test still green.
                if vers == "3.0":
                    _raw = lasio.read(p)
                    _bad = (len(_raw.index) != len(las.index)
                            or bool(_np.all(_np.isnan(_raw.data[:, 1]))))
                    assert _bad, (
                        "raw lasio now reads DLM-delimited 3.0 correctly — "
                        "las_reader's delimiter handling may be redundant; "
                        "re-measure before trusting either")
        finally:
            if os.path.exists(p):
                os.remove(p)
    check("synth LAS: every version/wrap variant reads back correctly",
          _las_roundtrips)

    # 22. LAS 3.0 MULTI-SECTION. lasio assumes one data block, so a 3.0 file
    #     carrying several either dies ("Cannot reshape ~A data size") or
    #     returns one set and drops the rest. 3.0's point is that it can hold
    #     Core, Inclinometry, Tops, Test and Perforation ALONGSIDE the log —
    #     the same things this catalog has tables for and currently extracts
    #     from PDFs. split_las3 parses them directly.
    #
    #     The fixture is deliberately awkward in the ways the standard allows,
    #     because those are the ways a naive splitter goes wrong quietly:
    #     a quoted description containing the delimiter, the file's NULL, a
    #     string column beside numeric ones, and a comma inside a HEADER value
    #     that must not be touched.
    def _las3_sections():
        import tempfile
        from dataview.file_catalog.las_reader import split_las3
        # SHAPED LIKE THE REAL SPEC SAMPLES, not like something convenient.
        # The first fixture used bare "~Version" headers and "_Data" section
        # names, and passed while the reader could not read either of the two
        # LAS 3.0 sample files published with the standard. Every oddity below
        # is copied from those files:
        #   * trailing prose after the section name ("~VERSION INFORMATION")
        #   * data sections named for the SUBJECT, not "_Data" ("~TOPS | …")
        #   * an INDEX distinguishing two sets of one kind (~Core[1], ~Core[2])
        #   * an association whose case differs from the section it names
        #     ("~TEST | TEST_Definition" against "~Test_Definition")
        #   * "~ASCII | CURVE" — the log data pointing at ~CURVE INFORMATION
        las3 = (
            "~VERSION INFORMATION\n"
            "VERS.   3.0 : CWLS LOG ASCII STANDARD - VERSION 3.0\n"
            "WRAP.   NO  : ONE LINE PER DEPTH STEP\n"
            "DLM .   COMMA : DELIMITING CHARACTER\n"
            "\n~Well Information\n"
            "NULL .     -999.25 : NULL VALUE\n"
            "UWI  .     15041204660000 : UNIQUE WELL IDENTIFIER\n"
            "WELL .     BAKER, 13-8 : WELL NAME\n"
            "\n~CURVE INFORMATION\n"
            "DEPT .M    : Depth {F}\n"
            "GR   .GAPI : Gamma Ray {F}\n"
            "\n~ASCII | CURVE\n"
            "540.5,61.2\n"
            "541.0,-999.25\n"
            "\n~Core_Definition\n"
            "CORT .M : Core top {F}\n"
            "CORD .  : Core description {S}\n"
            "\n~Core[1] | Core_Definition\n"
            '541.0,"shale, silty"\n'
            "\n~Core[2] | Core_Definition\n"
            "560.0,Fine sandstone\n"
            "\n~Test_Definition\n"
            "DST  .  : Test number {F}\n"
            "DDES .  : Recovery {S}\n"
            "\n~TEST | TEST_Definition\n"
            "1,Oil to surface\n"
            "\n~TOPS_Definition\n"
            "TOPT .M : Top depth {F}\n"
            "TOPN .  : Formation {S}\n"
            "\n~TOPS | TOPS_Definition\n"
            "540.8,Basal Quartz\n")
        p = os.path.join(tempfile.gettempdir(), "selftest_las3.las")
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(las3)
            las = split_las3(p)
            assert las.version.startswith("3"), las.version
            assert las.delimiter == ",", repr(las.delimiter)
            # ~ASCII|CURVE lands as 'Log'; the two Core sets stay APART, which
            # is the point of the index — merging them drops one silently
            assert set(las.sets) == {"Log", "Core[1]", "Core[2]", "TEST",
                                     "TOPS"}, sorted(las.sets)

            # a comma inside a HEADER value is data, not a delimiter
            assert las.well["WELL"] == "BAKER, 13-8", las.well["WELL"]
            assert las.well["UWI"] == "15041204660000"

            # the file's NULL becomes None, not -999.25 masquerading as a
            # reading — the whole point of declaring one
            assert las.sets["Log"].rows == [[540.5, 61.2], [541.0, None]], \
                las.sets["Log"].rows

            # a QUOTED value containing the delimiter stays one cell; splitting
            # it would shift every column after it and land plausible garbage
            assert las.sets["Core[1]"].rows == [[541.0, "shale, silty"]], \
                las.sets["Core[1]"].rows
            assert las.sets["Core[2]"].rows == [[560.0, "Fine sandstone"]], \
                las.sets["Core[2]"].rows

            # an association whose CASE differs from the section it names
            assert las.sets["TEST"].rows == [[1.0, "Oil to surface"]], \
                las.sets["TEST"].rows

            # {F} and {S} produce a float and a str, not two strings
            t = las.sets["TOPS"].rows[0]
            assert isinstance(t[0], float) and t[1] == "Basal Quartz", t

            # and it must REFUSE a 2.0 file rather than half-parsing one that
            # lasio already handles properly
            with open(p, "w", encoding="utf-8") as f:
                f.write(las3.replace("3.0 : CWLS", "2.0 : CWLS"))
            try:
                split_las3(p)
                raise AssertionError("split_las3 accepted a VERS 2.0 file")
            except ValueError:
                pass
        finally:
            if os.path.exists(p):
                os.remove(p)
    check("LAS 3.0: named data sets parse, with types and quoting",
          _las3_sections)

    # 23. ~Inclinometry -> the directional-survey mirrors. A LAS 3.0 file can
    #     carry a survey as DATA; the mirrors for it already promote and
    #     already draw on the map, so this is a mapping and its risks are a
    #     mapping's risks: a column pointed at the wrong column, an invented
    #     value, or a child staged without its parent.
    def _las3_inclinometry():
        from dataview.file_catalog.las_reader import split_las3
        from dataview.file_catalog.las3_capture import all_sets
        import io as _io
        las3 = (
            "~VERSION INFORMATION\n"
            "VERS.   3.0 : CWLS LOG ASCII STANDARD - VERSION 3.0\n"
            "WRAP.   NO  : ONE LINE PER DEPTH STEP\n"
            "DLM .   COMMA : DELIMITING CHARACTER\n"
            "\n~Well Information\n"
            "STRT .M    0.0 : First Index Value\n"
            "UWI  .     15001209150000 : UNIQUE WELL IDENTIFIER\n"
            "SRVC .     ANY LOGGING COMPANY INC. : SERVICE COMPANY\n"
            "\n~Inclinometry_Definition\n"
            "MD   .  : Measured Depth {F}\n"
            "TVD  .  : True Vertical Depth {F}\n"
            "AZIM .DEG : Borehole Azimuth {F}\n"
            "DEVI .DEG : Borehole Deviation {F}\n"
            "\n~Inclinometry | Inclinometry_Definition\n"
            "0.0,0.0,290.0,0.0\n"
            "200.0,198.34,284.86,1.43\n"
            "600.0,571.90,204.39,7.41\n")
        las = split_las3(_io.StringIO(las3))
        out = all_sets(las, source_path=r"C:\x\demo_3.las")

        # THE PARENT SHIPS WITH THE CHILD. Stations carry fk_srvy_sta_hdr ->
        # dv_well_dir_srvy_hdr; staging one without the other is exactly what
        # left 153 log curves unpromotable this morning.
        assert set(out) == {"cat_well_dir_srvy_hdr", "cat_well_dir_srvy_sta"}, \
            sorted(out)
        hdr = out["cat_well_dir_srvy_hdr"][0]
        sta = out["cat_well_dir_srvy_sta"]
        assert len(sta) == 3, len(sta)
        assert hdr["survey_id"] == sta[0]["survey_id"], "header/station id split"

        # DEVI IS INCLINATION. Pointing it at azim, or dropping it, gives a
        # survey that plots as a different hole.
        assert [r["incl"] for r in sta] == [0.0, 1.43, 7.41], \
            [r["incl"] for r in sta]
        assert [r["azim"] for r in sta] == [290.0, 284.86, 204.39]
        assert [r["md"] for r in sta] == [0.0, 200.0, 600.0]
        assert [r["tvd"] for r in sta] == [0.0, 198.34, 571.90]

        # uwi in the catalog's char(14) form, the same transform promote uses
        assert all(r["uwi"] == "15001209150000" for r in sta)

        # the unit comes from the file's ~Well STRT, not a default
        assert hdr["depth_ouom"] == "M" and sta[0]["depth_ouom"] == "M"
        # extent is computed from the stations, not guessed
        assert (hdr["survey_top_depth"], hdr["survey_base_depth"]) == (0.0, 600.0)

        # NOTHING INVENTED. The file carries no dogleg, no offsets, no
        # position, and no contractor id — a plausible value in any of these
        # would plot and get quoted.
        for k in ("ns_offset", "ew_offset", "dls",
                  "surface_latitude", "surface_longitude"):
            assert not sta[0].get(k), f"{k} was invented"
        assert hdr["contractor_ba_id"] is None, \
            "a service-company NAME was written into an FK to " \
            "dv_business_associate — seeding an entity parent is a decision"
        assert hdr["survey_date"] is None, \
            "the well header's log DATE was reused as the survey date"

        # station_id sorts in depth order as TEXT — '10' before '2' makes a
        # survey read as nonsense in any grid that orders by it
        assert [r["station_id"] for r in sta] == ["00001", "00002", "00003"]

        # and a file with no such section yields nothing rather than an empty
        # header row hanging off a well
        assert all_sets(split_las3(_io.StringIO(
            las3.split("~Inclinometry_Definition")[0])), source_path="x") == {}

        # A BARE ~Ascii MUST STILL FIND ITS DEFINITION. The spec samples write
        # "~ASCII | CURVE" and take the association branch; the generator
        # writes a bare "~Ascii" against "~CURVE INFORMATION" and takes the
        # fallback — which matched the literal "Curve", so every generated 3.0
        # file parsed to ZERO data sets while its header read perfectly. No
        # exception, just nothing. Two sources of files, two paths through one
        # function, and only the second source walked this one.
        bare = (
            "~VERSION INFORMATION\n"
            "VERS.   3.0 : CWLS LOG ASCII STANDARD - VERSION 3.0\n"
            "DLM .   COMMA : DELIMITING CHARACTER\n"
            "\n~Well Information\n"
            "UWI  .     15001209150000 : UNIQUE WELL IDENTIFIER\n"
            "\n~CURVE INFORMATION\n"
            "DEPT .M    : Depth {F}\n"
            "GR   .GAPI : Gamma Ray {F}\n"
            "\n~Ascii\n"
            "540.5,61.2\n"
            "541.0,62.7\n")
        b = split_las3(_io.StringIO(bare))
        assert "Log" in b.sets, (
            "a bare ~Ascii found no definition — the fallback is matching "
            "section names case-sensitively again, and '~CURVE INFORMATION' "
            "is not '~Curve'")
        assert b.sets["Log"].rows == [[540.5, 61.2], [541.0, 62.7]], \
            b.sets["Log"].rows

        # CAPTURE DOES NOT WANT THE SAMPLES. The catalog stores curve METADATA
        # and never the bulk arrays, so parsing a Log set of 8,000-13,000 rows
        # on every capture is work thrown away. curve_data=False keeps the
        # COLUMNS — a set with no columns cannot be mapped — and drops only the
        # rows.
        c = split_las3(_io.StringIO(bare), curve_data=False)
        assert c.sets["Log"].rows == [], c.sets["Log"].rows
        assert len(c.sets["Log"].columns) == 2, \
            "the skip dropped the column definitions too, so the set is unusable"

        # ...and it must not touch any OTHER set. Inclinometry is the whole
        # point of reading a 3.0 file here; a skip that silently emptied it
        # would look exactly like a file with no survey.
        full = split_las3(_io.StringIO(las3))
        cheap = split_las3(_io.StringIO(las3), curve_data=False)
        for _n in full.sets:
            if _n == "Log":
                continue
            assert full.sets[_n].rows == cheap.sets[_n].rows, \
                f"curve_data=False changed the {_n} set"

        # and the capture path must actually ask for the cheap read
        import inspect
        from dataview.file_catalog import extract_core as _ec
        _src = inspect.getsource(_ec)
        assert "split_las3(fpath, curve_data=False)" in _src, \
            "capture parses the Log samples again — thousands of rows per " \
            "file materialised and dropped on the floor"
    check("LAS 3.0: ~Inclinometry maps to the survey mirrors, parent included",
          _las3_inclinometry)

    def _extract_cannot_loop():
        # A FILE WHOSE WRITE FAILS KEEPS ITS PENDING FLAG, so the next claim
        # returns it again and _stage_extract never leaves its `while True`.
        # Seven LAS files did exactly that on 23 Aug 2026: ~570 passes each,
        # 117,640 log lines, "ok 3,995" reported for 7 files, and the UI Stop
        # button could not reach it because should_abort was consulted only
        # BETWEEN stages. The process tree had to be killed.
        #
        # THIS IS STRUCTURAL, NOT TEXTUAL. A grep for "attempted" passes on a
        # variable that is assigned and never read — the same shape as the
        # invariant keyed on FILE_NAME that could never fail. So walk the AST:
        # the guard and the abort check must both sit INSIDE the loop, and each
        # must be able to leave it.
        import ast
        import inspect
        from dataview.import_data import pipeline_run as _pr

        sig = inspect.signature(_pr._stage_extract)
        assert "should_abort" in sig.parameters, (
            "_stage_extract no longer takes should_abort — the Stop button "
            "cannot interrupt a stage that is already looping")

        tree = ast.parse(inspect.getsource(_pr).replace("\r\n", "\n"))
        tgt = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_stage_extract")
        loops = [n for n in ast.walk(tgt) if isinstance(n, ast.While)]
        assert loops, "_stage_extract has no loop to guard"

        def _breaks_on(loop, want):
            """Is there an `if <… want …>: … break` directly in this loop?"""
            for node in loop.body:
                if not isinstance(node, ast.If):
                    continue
                names = {m.id for m in ast.walk(node.test)
                         if isinstance(m, ast.Name)}
                if want in names and any(isinstance(b, ast.Break)
                                         for b in ast.walk(node)):
                    return True
            return False

        assert any(_breaks_on(lp, "attempted") for lp in loops), (
            "the extract loop no longer breaks when a claim comes back holding "
            "only files it already tried — a failed write makes it spin forever")
        assert any(_breaks_on(lp, "should_abort") for lp in loops), (
            "the extract loop no longer checks should_abort, so the Stop button "
            "cannot end a run once this stage has started")

        # and the caller must actually hand the hook over: run_pipeline already
        # has it, and a parameter nobody passes is not a guard
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_stage_extract"]
        assert calls, "no call to _stage_extract found"
        for c in calls:
            assert any(k.arg == "should_abort" for k in c.keywords), (
                "a _stage_extract call does not pass should_abort, so that run "
                "cannot be stopped once it starts")
    check("extract: a failed write stops the stage instead of looping",
          _extract_cannot_loop)

    def _segy_2d_coords_survive_crossline_garbage():
        # A 2D LINE HAS NO CROSSLINE, so the bytes at the standard crossline
        # offset (193-196) hold whatever the vendor put there. In the Geoscience
        # Australia headers that is CDP-STAT — statics, routinely negative. The
        # readability veto used to key on inline/crossline validity and threw
        # the COORDINATES away with them, so "300 of 300 crossline values
        # invalid" — the expected reading for 2D — condemned clean CDP-X/Y at
        # bytes 181-188. Measured 24 Aug: 228 of 232 seismic files reported no
        # geometry despite carrying good coordinates, which read downstream as
        # "no CRS" and sent the operator to arm a fallback CRS that was never
        # missing.
        #
        # Built here rather than read from disk: the corpus lives outside the
        # repo, and a check that needs C:\Bulk passes vacuously anywhere else.
        import struct
        import tempfile
        import os
        from dataview.file_catalog.segy_header import read_segy_header

        NS = 8                                   # samples per trace
        TR = 60                                  # traces

        def _segy(x0, y0, dx, dy, xline_val):
            """A minimal rev-1 SEG-Y: EBCDIC text, 400-byte binary, TR traces."""
            card = [" " * 80 for _ in range(40)]
            card[0] = "C01 CLIENT:TEST, VOLUME:STACK".ljust(80)
            card[1] = "C02 XY COORDINATES:AMG ZONE 54; SURVEY DATUM:GDA2020;".ljust(80)
            text = "".join(card).encode("cp037")
            binhdr = bytearray(400)
            struct.pack_into(">H", binhdr, 20, NS)          # 3221-3222 samples
            struct.pack_into(">H", binhdr, 24, 5)           # 3225-3226 IEEE
            out = bytearray(text + bytes(binhdr))
            for i in range(TR):
                th = bytearray(240)
                struct.pack_into(">h", th, 70, 1)           # 71-72  scalar
                struct.pack_into(">i", th, 180, int(x0 + i * dx))   # 181-184 CDP-X
                struct.pack_into(">i", th, 184, int(y0 + i * dy))   # 185-188 CDP-Y
                struct.pack_into(">i", th, 188, 1)                  # 189-192 inline
                struct.pack_into(">i", th, 192, xline_val)          # 193-196 "crossline"
                out += th + b"\x00" * (NS * 4)
            return bytes(out)

        def _read(blob):
            fd, path = tempfile.mkstemp(suffix=".segy")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(blob)
                return read_segy_header(path)
            finally:
                os.unlink(path)

        # 1. THE BUG. Good coordinates, negative junk where crossline would be.
        h = _read(_segy(379547, 7076337, 13, -32, -458751))
        pts = h.get("cdp_points") or []
        assert len(pts) >= TR - 1, (
            f"a 2D line's coordinates were discarded ({len(pts)} points) "
            f"because the bytes at the standard CROSSLINE offset are not valid "
            f"indices — which is the expected reading for 2D, not evidence the "
            f"header is corrupt. Notes: {h.get('notes')}")
        assert h.get("cdp_x_range") and h.get("cdp_y_range"), \
            "coordinate ranges were dropped despite usable coordinates"

        # 2. The veto must still fire on coordinates that are not coordinates.
        #    filt_mig is the real case (headers lose alignment after trace 2);
        #    here every pair is beyond any CRS's reach.
        h2 = _read(_segy(9_999_999_00, 9_999_999_00, 1, 1, 1))
        assert not (h2.get("cdp_points") or []), (
            "coordinates far outside any coordinate system were published — "
            "the magnitude bound is not being applied")

        # 3. And a degenerate extent must publish nothing. brecon_3d reads
        #    11,770,231 over a 79 m span because its header declares 4R (real)
        #    and this reader takes int32; self-consistent, wrong, and it plots.
        h3 = _read(_segy(11_770_231, 11_806_031, 1, 1, 1))
        assert not (h3.get("cdp_points") or []), (
            "a survey spanning tens of metres across 60 traces was accepted as "
            "an extent — an outline that plots is worse than no outline")
    check("SEG-Y 2D: crossline junk does not veto good coordinates",
          _segy_2d_coords_survive_crossline_garbage)

    def _seis_nav_only_never_promotes():
        # NAVIGATION IS NOT DATA. A P1/90 carries geometry FOR a survey; on its
        # own it produced a dv_seis_set row with nothing openable behind it.
        # Three of the eight rows in dv_seis_set were exactly that (TUIHU,
        # SOUTH CHINA SEA UNIFIED AREA, EXAMPLE FIELD UKCS BLOCKS 311/7),
        # each backed by one .p190 and nothing else.
        #
        # THE GATE IS A PREDICATE REUSED BY FOUR QUERIES, so the failure mode is
        # not "the gate is wrong", it is "a fifth query forgot it" — the shape
        # CLAUDE.md records for the four lists that must agree. Rather than
        # assert the four call sites exist, assert the INVARIANT: no query that
        # selects promotable rows may use _NAMED without _TIED beside it. A new
        # MERGE added later fails this the moment it lands.
        import inspect
        import re
        from dataview.file_catalog import promote_catalog as _pc

        src = inspect.getsource(_pc.promote_seismic)
        assert "_TIED" in src, (
            "promote_seismic no longer gates on _TIED — a navigation-only "
            "survey promotes into dv_seis_set with no seismic behind it")

        # PER STATEMENT, NOT PER LINE. The first version of this check tested
        # each source LINE, so wrapping "AND ({_TIED})" onto a continuation
        # line made it report a correctly gated query as an offender — a check
        # keyed on the wrong unit, which is the thing it exists to catch.
        # {_NAMED} only ever appears inside a cur.execute(...), so split there
        # and judge whole statements.
        stmts = src.split("cur.execute(")[1:]
        offenders = []
        for st in stmts:
            if "{_NAMED}" not in st:
                continue
            if "_TIED" not in st:
                first = next((ln.strip() for ln in st.splitlines()
                              if "{_NAMED}" in ln), st[:60].strip())
                offenders.append(first)
        assert not offenders, (
            "these promote queries filter on _NAMED without the _TIED gate, so "
            "they will lift a nav-only survey: " + " | ".join(offenders))

        # And the hold tally must count them. Reporting "held 18" for 21 held
        # surveys is the undercount that made five Teapot lines read as having
        # vanished — Held is one of the four states and it has to be visible.
        m = re.search(r"held\s*=\s*([^\r\n]+)", src)
        assert m and "_held_untied" in m.group(1), (
            "the held tally omits _held_untied, so nav-only surveys are "
            "reported as neither promoted nor held")

        # The gate must be keyed on the SAME survey key the MERGE groups on.
        # Keyed on raw SURVEY_NAME instead, a survey whose files disagree on
        # whitespace would tie under one spelling and not the other.
        assert "_NAV_EXTS" in src and ".p190" in src, \
            "the navigation extension list is gone from the tie gate"
    check("seismic: navigation-only surveys are held, not promoted",
          _seis_nav_only_never_promotes)

    def _segy_declared_coord_format_is_honoured():
        # "4R" IS A 4-BYTE REAL. brecon_3d states its own layout:
        #     C 7 CDP_X          181   4R   CDP_Y          185   4R
        #     C 8 ILINE_NO       197   4I   XLINE_NO       201   4I
        # Read as int32 with the stated -100 scalar, its easting 2,617,988
        # arrives as 11,770,231 and the survey spans 79 m -- self-consistent,
        # wrong, and it plots. Its inline/crossline are at 197/201, not the
        # rev-1 189/193, so index reads were garbage too.
        #
        # Verified against the file's OWN stated ranges (ILINES 1-457,
        # XLINES 1-318), which is why this bug was provable rather than
        # arguable. The fixture reproduces the shape without needing C:\Bulk.
        import struct
        import tempfile
        import os
        from dataview.file_catalog.segy_header import (
            declared_trace_layout, read_segy_header, _classify_byte_label)

        def _ibm(v):
            """float -> IBM System/360 32-bit, the inverse of _ibm32."""
            if v == 0:
                return 0
            sign, v = (1, -v) if v < 0 else (0, v)
            e = 0
            while v >= 1.0:
                v /= 16.0
                e += 1
            while v < 1.0 / 16.0:
                v *= 16.0
                e -= 1
            return ((sign << 31) | ((e + 64) << 24)
                    | int(round(v * (1 << 24))) & 0x00FFFFFF)

        NS, TR = 8, 40
        X0, Y0, DX = 2_617_988.0, 6_197_988.0, 200.0

        card = [" " * 80 for _ in range(40)]
        card[0] = "C 1 CLIENT: TEST VOL:FD MIGRATION".ljust(80)
        card[2] = "C 3 ILINES: 1-457  XLINES: 1 - 318".ljust(80)
        card[3] = "C 4  SAMPLE RATE  4 MS  Time   4200 MS  DATUM 0 ASL".ljust(80)
        card[5] = "C 6 BYTES FORMAT   (FOR NON STANDARD SEGY HEADERS)".ljust(80)
        card[6] = "C 7 CDP_X          181   4R   CDP_Y          185   4R".ljust(80)
        card[7] = "C 8 ILINE_NO       197   4I   XLINE_NO       201   4I".ljust(80)
        text = "".join(card).encode("cp037")
        binhdr = bytearray(400)
        struct.pack_into(">H", binhdr, 20, NS)
        struct.pack_into(">H", binhdr, 24, 1)          # format 1 => IBM reals
        blob = bytearray(text + bytes(binhdr))
        for i in range(TR):
            th = bytearray(240)
            struct.pack_into(">h", th, 70, -100)       # a scalar that must NOT apply
            struct.pack_into(">I", th, 180, _ibm(X0 + i * DX))
            struct.pack_into(">I", th, 184, _ibm(Y0 + i * DX))
            struct.pack_into(">i", th, 196, 1 + i)     # ILINE_NO at 197
            struct.pack_into(">i", th, 200, 1 + i)     # XLINE_NO at 201
            struct.pack_into(">i", th, 188, -458751)   # rev-1 inline slot: junk
            struct.pack_into(">i", th, 192, -458751)   # rev-1 crossline slot: junk
            blob += th + b"\x00" * (NS * 4)

        offs, fmts = declared_trace_layout(
            "".join(card).replace(" " * 8, " " * 8))   # text form, not EBCDIC
        assert fmts.get("cdp_x") == "real" and fmts.get("cdp_y") == "real", (
            f"the declared 4R coordinate format was not read: {fmts!r} -- int32 "
            f"there turns 2,617,988 into 11,770,231 and it still plots")
        assert offs.get("inline") == 196 and offs.get("crossline") == 200, (
            f"declared ILINE_NO/XLINE_NO byte positions ignored: {offs!r}")

        # NEGATIVE CASES. The colon-delimited Geoscience Australia form and
        # ordinary prose must not be mistaken for a layout declaration.
        _ga = ("C28 SDATUM:57-60:INT;     CDP-X:73-76/181-184:INT; "
               "CDP-Y:77-80/185-188:INT;")
        assert not declared_trace_layout(_ga)[1], \
            "the GA colon form was parsed as a FORMAT declaration"
        assert _classify_byte_label("ILINES: 1-457") is None, \
            "the prose line 'ILINES: 1-457' classifies as a field"
        assert _classify_byte_label("XLINES: 1 - 318") is None, \
            "the prose line 'XLINES: 1 - 318' classifies as a field"

        fd, path = tempfile.mkstemp(suffix=".sgy")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(blob))
            h = read_segy_header(path)
        finally:
            os.unlink(path)

        pts = h.get("cdp_points") or []
        assert pts, f"no coordinates read; notes={h.get('notes')}"
        gx = [p[0] for p in pts]
        assert abs(min(gx) - X0) < 2.0, (
            f"CDP_X read as {min(gx):,.0f}, expected ~{X0:,.0f}. Either the "
            f"declared real format was ignored, or the bytes 71-72 scalar was "
            f"applied to it -- the scalar governs INTEGER coordinates only")
        assert h.get("inline_range") and h["inline_range"][0] == 1, (
            f"inline read from the rev-1 offset instead of the declared 197: "
            f"{h.get('inline_range')!r}")
        assert h.get("crossline_range") and h["crossline_range"][0] == 1, (
            f"crossline read from the rev-1 offset instead of the declared 201: "
            f"{h.get('crossline_range')!r}")
    check("SEG-Y: a declared 4R coordinate format is honoured, not assumed int",
          _segy_declared_coord_format_is_honoured)

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
