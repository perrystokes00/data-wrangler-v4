"""Read a captured map workflow, and re-run it against today's code.

    python tools/replay_workflow.py                       # newest capture, read it
    python tools/replay_workflow.py <file.jsonl>
    python tools/replay_workflow.py --check               # re-execute and compare
    python tools/replay_workflow.py --check --db DataView_Demo

TWO JOBS, AND THE SECOND IS THE POINT.

Read forwards it is a workflow: what was on screen, what was asked, what
came back. That alone answers "what did you actually do", which is where
three separate investigations stalled in one evening.

With --check it is a REGRESSION TEST. Every replayable op is re-executed
with its recorded arguments and the answer is compared by digest. A well
set that changed size is obvious; a well set that changed CONTENTS while
keeping its size is the one worth catching, because nothing on screen would
ever say so -- it plots, exports and gets quoted. So the comparison is the
digest, and the count is only how it is described.

AN OP IS ONLY RE-RUN IF IT SAID IT COULD BE. workflow_capture marks each
one: a call whose arguments included the loaded well rows cannot be rebuilt
from the file, and pretending otherwise would make this skip in silence
while reporting a pass. Those are listed as observations and counted
separately, so "12 checked, 4 not checkable" is visible rather than "12
passed".

EXIT CODE IS THE ANSWER: 0 all good, 1 something differs, 2 could not run.
A regression check whose failure has to be read out of prose is one nobody
wires into anything.
"""

import argparse
import glob
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# IMPORTING THE MAP MODULE OUTSIDE `streamlit run` IS LOUD. Every
# @st.cache_data logs "No runtime found", and every session_state touch warns
# about bare mode -- forty lines of it, ahead of a three-line answer. None of
# it is wrong and none of it is about this run.
#
# THREE LEVERS WERE TRIED AND TWO DID NOT WORK, which is worth writing down
# because both look right: logging.getLogger("streamlit").setLevel() is
# overwritten when streamlit configures its own loggers during import, and
# STREAMLIT_LOGGER_LEVEL is only read under `streamlit run`, not on a plain
# import. What holds is streamlit's own set_log_level, called after streamlit
# is imported but BEFORE the map module is -- the map module's decorators are
# what do the logging, and they fire at ITS import.
os.environ.setdefault("DW_MAP_TIMERS", "0")   # no timing wrappers in a replay
try:
    import streamlit as _st_early                       # noqa: F401
    from streamlit.logger import set_log_level as _set_log_level
    _set_log_level("critical")
except Exception:
    logging.getLogger("streamlit").setLevel(logging.CRITICAL)

REPORT_DIR = os.environ.get("DW_REPORT_DIR", r"C:\Bulk\reports")


def _newest():
    _hits = sorted(glob.glob(os.path.join(REPORT_DIR, "workflow_*.jsonl")),
                   key=os.path.getmtime)
    return _hits[-1] if _hits else None


def _load(path):
    _rows = []
    with open(path, encoding="utf-8") as _fh:
        for _i, _ln in enumerate(_fh, 1):
            _ln = _ln.strip()
            if not _ln:
                continue
            try:
                _rows.append(json.loads(_ln))
            except json.JSONDecodeError as _e:
                # NAMED, NOT SKIPPED. A truncated last line is normal if the
                # app was killed; a bad line in the middle is a bug here.
                print("  ! line %d is not JSON (%s)" % (_i, _e))
    return _rows


def _fmt_args(args):
    _bits = []
    for _k, _v in (args or {}).items():
        if isinstance(_v, dict) and "__n" in _v:
            _v = "<%d items>" % _v["__n"]
        _s = str(_v)
        _bits.append("%s=%s" % (_k, _s if len(_s) <= 40 else _s[:37] + "..."))
    return ", ".join(_bits)


def show(rows):
    """The workflow, forwards."""
    for _r in rows:
        _k = _r.get("kind")
        _t = _r.get("t", "")
        if _k == "start":
            print("%s  ── recording started%s" % (
                _t, (" — " + _r["label"]) if _r.get("label") else ""))
        elif _k == "stop":
            print("%s  ── recording stopped" % _t)
        elif _k == "render":
            _st = _r.get("state") or {}
            _on = [k for k, v in _st.items() if v is True]
            print("%s  RENDER %s" % (_t, _r.get("tag", "")))
            for _key in ("map_mode", "wm_sc_state", "wm_ai_question",
                         "wm_ai_scope", "_active_drill_bbox"):
                if _st.get(_key) not in (None, "", []):
                    print("           %-20s %s" % (_key, _st[_key]))
            if _on:
                print("           %-20s %s" % ("on", ", ".join(sorted(_on))))
        elif _k == "op":
            _n = _r.get("n")
            print("%s  op  %-24s %s%s" % (
                _t, _r.get("name", "?"), _fmt_args(_r.get("args")),
                ("  -> %s" % format(_n, ",")) if _n is not None else ""))
            if not _r.get("replayable"):
                print("           (observation only — not re-runnable from "
                      "the file)")
        elif _k == "note":
            print("%s  note  %s" % (_t, _r.get("text", "")))


# ── the replayable operations ──────────────────────────────────────────────
# Each entry re-executes one recorded op from its arguments alone and returns
# the values whose digest is compared. Adding an op here is the ONLY thing
# that makes it checkable; a name that is recorded but not listed is reported
# as unchecked rather than passing quietly.

def _op_wells_near_feature(P, engine, args):
    return P._wells_near_feature(engine, args["feature"], args["name"],
                                 float(args["distance_m"]))


def _op_resolve_near_name(P, engine, args):
    _hit = P._resolve_near_name(engine, args["feature"], args["name"])
    return [] if _hit is None else [_hit]


def _op_ai_spec_to_where(P, engine, args):
    _where, _rej = P._ai_spec_to_where(json.loads(args["spec"]),
                                       P._ai_db_columns(engine))
    # The WHERE and the rejections together ARE the answer -- a spec that
    # starts silently dropping a clause changes the second without changing
    # the first.
    return [_where] + sorted(_rej)


REPLAYERS = {
    "wells_near_feature": _op_wells_near_feature,
    "resolve_near_name": _op_resolve_near_name,
    "ai_spec_to_where": _op_ai_spec_to_where,
}


def check(rows, db):
    """Re-execute every replayable op and compare digests."""
    try:
        from sqlalchemy import create_engine
        import dataview.mapping.page_well_map as P
        from dataview.mapping.workflow_capture import digest
    except Exception as _e:
        print("cannot import the map module: %s" % _e)
        return 2

    engine = create_engine(
        "mssql+pyodbc://@localhost\\SQLEXPRESS/%s"
        "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes" % db)

    _ops = [r for r in rows if r.get("kind") == "op"]
    _same = _diff = _skipped = _errored = 0

    print("re-running %d recorded op(s) against %s\n" % (len(_ops), db))
    for _r in _ops:
        _name = _r.get("name", "?")
        _fn = REPLAYERS.get(_name)
        if not _r.get("replayable") or _fn is None:
            _skipped += 1
            print("  SKIP  %-24s %s" % (_name, "not re-runnable from the file"))
            continue
        try:
            _got = _fn(P, engine, _r.get("args") or {})
        except Exception as _e:
            _errored += 1
            print("  ERROR %-24s %s: %s" % (_name, type(_e).__name__, _e))
            continue
        _gd, _gn = digest(_got), len(_got)
        _wd, _wn = _r.get("digest"), _r.get("n")
        if _wd is None:
            _skipped += 1
            print("  SKIP  %-24s recorded without a digest" % _name)
        elif _gd == _wd:
            _same += 1
            print("  same  %-24s %s" % (_name, format(_gn, ",")))
        else:
            _diff += 1
            print("  DIFF  %-24s was %s (%s), now %s (%s)"
                  % (_name, format(_wn or 0, ","), _wd, format(_gn, ","), _gd))
            print("        args: %s" % _fmt_args(_r.get("args")))

    print("\n%d same, %d DIFFERENT, %d not checkable, %d errored"
          % (_same, _diff, _skipped, _errored))
    if _diff or _errored:
        return 1
    if _same == 0:
        # NOT A PASS. A file with nothing checkable in it must not report
        # success -- that is how a regression suite becomes decoration.
        print("nothing was actually checked.")
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", nargs="?", help="capture to read (default: newest)")
    ap.add_argument("--check", action="store_true",
                    help="re-execute the replayable ops and compare")
    ap.add_argument("--db", default="DataView_Demo")
    a = ap.parse_args()

    _p = a.file or _newest()
    if not _p:
        print("no capture found in %s" % REPORT_DIR)
        return 2
    if not os.path.exists(_p):
        print("no such file: %s" % _p)
        return 2

    print("%s\n" % _p)
    _rows = _load(_p)
    if not _rows:
        print("empty capture")
        return 2
    if a.check:
        return check(_rows, a.db)
    show(_rows)
    _ops = sum(1 for r in _rows if r.get("kind") == "op")
    print("\n%d event(s), %d op(s). Re-run them with --check." % (len(_rows), _ops))
    return 0


if __name__ == "__main__":
    sys.exit(main())
