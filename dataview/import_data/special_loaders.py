"""special_loaders.py — the Data Assistant's specialised loaders.

WHY THESE ARE NOT ROUTE A OR ROUTE B. The two general routes read a FOLDER and
work out what is in it from extensions: tabular files become tables, well files
become headers and curves. That works because those formats describe
themselves. These do not.

    a slabbed core photograph      the depth interval is in the FILE NAME
    a plug analysis workbook       a four-row stacked lab header read by
                                   position, and "<0.0001" is a detection
                                   limit, not a number
    a MUD.LOG 4.4b binary          a vendor tag/length/value format whose
                                   own viewer ships beside it

Each needed a parser written against the source, not a mapping. That is the
line: a specialised loader exists when the FILE cannot say what it holds and a
human had to read the documents to find out.

ONE WRITER PER TABLE is the rule that makes this safe. Every table below is
owned by exactly one loader, and `tables` is that claim written down —
`check_ownership()` fails if two loaders name the same table, so the registry
cannot quietly grow a second writer the way load_well_detail did before
dv_well_mud_log and dv_well_shows moved out of it.

The loaders themselves stay in tools/ and stay runnable from the command line.
This module does not reimplement them: it imports each one and calls its
main() with the same arguments you would type, capturing what it prints. There
is no second code path to drift, and a dry run here is the dry run there.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

try:
    import streamlit as st
except Exception:                                    # importable without UI
    st = None

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_TOOLS = os.path.join(_ROOT, "tools")


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------
# `tables` is not documentation. check_ownership() reads it, and selftest runs
# that check, so a new loader that claims a table another one owns fails before
# it can write anything.

SPECIAL = [
    {
        "key": "core",
        "module": "load_core_data",
        "title": "Core — runs, plug analyses and slab photographs",
        "reads": "the RMOTC core CD: slabbed-core .jpg photographs and the "
                 "plug analysis workbook",
        "tables": ("dv_well_core", "dv_well_core_sample", "dv_well_core_photo"),
        "why": "The depth interval of a slab photograph is in its FILE NAME, "
               "and the analysis workbook has a four-row stacked lab header "
               "that only reads correctly by position. '<0.0001' in a "
               "permeability column is a detection limit, not a number, and "
               "is stored as a remark rather than a false zero.",
    },
    {
        "key": "mudlog",
        "module": "load_mudlog",
        "title": "Mud log — header and hydrocarbon shows",
        "reads": "a MUD.LOG 4.4b binary (48X28.LOG)",
        "tables": ("dv_well_mud_log", "dv_well_shows"),
        "why": "A vendor tag/length/value binary. The shows are read out of "
               "the geologist's sample descriptions, and the hard part is the "
               "NEGATIVES: mineral fluorescence is not hydrocarbon, 'no vis "
               "flor' is a record of absence, and cavings are not in-situ. 38 "
               "descriptions mention fluorescence, stain or cut; 10 are shows.",
    },
    {
        "key": "welldetail",
        "module": "load_well_detail",
        "title": "Well detail — legal location, aliases, intervals, pressures",
        "reads": "the MUD.LOG header (for the legal location) plus facts "
                 "already in the database",
        "tables": ("dv_well_legal", "dv_well_alias", "dv_well_pressure",
                   "dv_strat_interval"),
        "why": "The legal location is PARSED from the mud log header, and the "
               "quarter-quarter is derived by arithmetic on the FSL/FWL "
               "footages rather than transcribed. The remaining rows are "
               "synthetic but anchored — pressures repeat the DST's own "
               "readings, and each interval hangs off a real formation pick.",
    },
]


def check_ownership(specs=None):
    """Every table is claimed by exactly one loader. Returns a list of
    complaints, empty when the registry is sound.

    THIS IS THE CHECK THAT EARNS THE REGISTRY. A specialised loader is only
    safe because nothing else writes its tables; the moment two of them claim
    one, 'the first one in wins' becomes 'whichever ran last', and neither
    loader's --remove undoes the other's rows."""
    specs = SPECIAL if specs is None else specs
    owner, bad = {}, []
    for s in specs:
        for t in s["tables"]:
            if t in owner:
                bad.append("%s is claimed by both %s and %s"
                           % (t, owner[t], s["key"]))
            else:
                owner[t] = s["key"]
    keys = [s["key"] for s in specs]
    for k in set(keys):
        if keys.count(k) > 1:
            bad.append("duplicate loader key %r" % k)
    return bad


def _import(spec):
    """Import the tools/ script. It stays a script; this just borrows main()."""
    if _TOOLS not in sys.path:
        sys.path.insert(0, _TOOLS)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    return __import__(spec["module"])


def run_loader(spec, apply=False, remove=False, extra=None):
    """Call the loader's own main() and return (ok, printed_output).

    Captures stdout because the loaders report through print() and their dry
    run is the thing worth reading — it lists what WOULD be written, which is
    the whole point of running one from a UI before committing to it."""
    mod = _import(spec)
    argv = []
    if remove:
        argv.append("--remove")
    if apply:
        argv.append("--apply")
    argv.extend(extra or [])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = mod.main(argv)
        return (rc in (0, None)), buf.getvalue()
    except SystemExit as e:
        # SystemExit is how these refuse — an unregistered dv_r_* code, a well
        # that does not resolve. That message is the useful part, not a crash.
        out = buf.getvalue()
        msg = str(e) if str(e) not in ("0", "None") else ""
        return (e.code in (0, None)), (out + ("\n" + msg if msg else ""))
    except Exception as e:
        return False, buf.getvalue() + "\n%s: %s" % (type(e).__name__, e)


def loader_database(spec):
    """The database this loader's buttons will actually write to.

    Read off the loader's OWN --database default, by READING its source rather
    than restating it here, so the two cannot drift: a count taken from a
    different database than the button writes to is a wrong number wearing the
    right label.

    Parsed with ast, not executed. An earlier version called main() with a
    sentinel database to watch argparse take the default -- which runs the
    loader to ask it a question about itself, and a loader is exactly the
    thing you do not run by accident."""
    import ast
    path = os.path.join(_TOOLS, spec["module"] + ".py")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
    except Exception:
        return None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        if not any(isinstance(a, ast.Constant) and a.value == "--database"
                   for a in node.args):
            continue
        for kw in node.keywords:
            if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                return kw.value.value
    return None


def row_counts(spec, engine=None):
    """What each of this loader's tables currently holds.

    NOT the app's connection. The app can be pointed at one database while the
    loader writes to another -- in Demo Mode it has no engine at all
    (connect_demo returns engine=None) -- and a count that describes somewhere
    else is worse than no count, because it looks authoritative."""
    from sqlalchemy import text
    if engine is None:
        db = loader_database(spec)
        if not db:
            return {}
        from dataview.core.dw_utils import make_engine
        engine = make_engine(db)
    out = {}
    try:
        with engine.connect() as c:
            for t in spec["tables"]:
                try:
                    out[t] = c.execute(text(
                        "SELECT COUNT(*) FROM dataview.[%s]" % t)).scalar()
                except Exception:
                    out[t] = None
    except Exception:
        return {}
    return out


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def _demo():
    """tools/demo_teapot, or None when it cannot be imported."""
    try:
        if _TOOLS not in sys.path:
            sys.path.insert(0, _TOOLS)
        if _ROOT not in sys.path:
            sys.path.insert(0, _ROOT)
        return __import__("demo_teapot")
    except Exception:
        return None


def render_demo_panel(engine=None):
    """Load the Teapot demo set, or take it back out.

    TWO CLICKS TO REMOVE, AND THE COUNTS SHOWN FIRST. This deletes data, and
    the history of reset buttons in this project is the reason for the
    ceremony: v3's demo_reset defaulted to full=True, protected none of the
    learned-mapping tables, and pointed at this same database -- one click
    destroyed about 2,604 rows belonging to the app that replaced it.

    So nothing here sweeps a table. The removal is each loader's own --remove,
    scoped to that loader's row_created_by stamp, plus the synthetic seismic
    set scoped by seis_set_id. What it will delete is listed BEFORE the
    confirm, so the claim can be read rather than trusted.
    """
    d = _demo()
    if d is None or engine is None:
        return
    try:
        rows = d.counts(engine)
        total = sum(sum(v.values()) for v in rows.values())
    except Exception as exc:
        st.caption("Demo set unavailable: %s" % exc)
        return

    with st.container(border=True):
        st.markdown("**Teapot demo set**")
        st.caption(
            "Core, mud log, well detail and the synthetic 2D seismic — the "
            "subset to load on camera and take back out between takes.")
        if rows:
            st.caption(" · ".join("`%s` %d" % (t, sum(v.values()))
                                  for t, v in sorted(rows.items())))
            st.caption("**%d row(s)** loaded." % total)
        else:
            st.caption("Nothing loaded.")

        c = st.columns([1, 1, 3])
        if c[0].button("Load demo set", key="demo_load_btn",
                       disabled=bool(rows)):
            with st.spinner("Loading the demo set…"):
                out = d.load(apply=True)
                sok, smsg = d.load_seismic(apply=True)
            st.session_state["demo_msg"] = (
                "\n".join("%s: %s" % (m, (o.strip().splitlines() or ["ok"])[-1])
                           for m, _ok, o in out)
                + "\nseismic: %s" % (smsg or ("ok" if sok else "failed")))
            st.rerun()

        # THE ARM IS THE POINT. A destructive action one click away from a
        # mis-click is how the v3 reset did its damage.
        if not st.session_state.get("demo_reset_armed"):
            if c[1].button("Reset demo set", key="demo_reset_btn",
                           disabled=not rows):
                st.session_state["demo_reset_armed"] = True
                st.rerun()
        else:
            st.warning(
                "This will remove **%d row(s)** — exactly the ones listed "
                "above and nothing else. Wells, reference tables, the rest of "
                "the file catalog and the learned column mappings are not "
                "touched." % total)
            a = st.columns([1, 1, 3])
            if a[0].button("Yes, remove them", key="demo_reset_go_btn",
                           type="primary"):
                with st.spinner("Removing…"):
                    out = d.reset(apply=True)
                    seis = d.reset_seismic(engine, apply=True)
                st.session_state.pop("demo_reset_armed", None)
                st.session_state["demo_msg"] = (
                    "\n".join("%s: %s"
                               % (m, (o.strip().splitlines() or ["ok"])[-1])
                               for m, _ok, o in out)
                    + "\n" + "\n".join("seismic %s: %s" % (k, v)
                                        for k, v in seis.items()))
                st.rerun()
            if a[1].button("Cancel", key="demo_reset_no_btn"):
                st.session_state.pop("demo_reset_armed", None)
                st.rerun()

        msg = st.session_state.pop("demo_msg", None)
        if msg:
            st.code(msg, language="text")


def render(engine=None):
    if st is None:
        return
    st.caption(
        "Specialised loaders — for sources whose files cannot describe "
        "themselves. Each one owns its tables outright and undoes exactly "
        "what it wrote."
    )
    bad = check_ownership()
    if bad:
        # Loud, because the invariant this page rests on has broken.
        st.error("Loader registry: two loaders claim the same table — "
                 + "; ".join(bad))

    render_demo_panel(engine)

    for spec in SPECIAL:
        with st.container(border=True):
            st.markdown("**%s**" % spec["title"])
            st.caption("Reads %s" % spec["reads"])
            st.caption(spec["why"])

            counts = row_counts(spec, engine)
            if counts:
                st.caption("in `%s` — " % (loader_database(spec) or "?")
                           + " · ".join(
                    "`%s` %s" % (t, "—" if n is None else "{:,}".format(n))
                    for t, n in counts.items()))

            cols = st.columns([1, 1, 1, 3])
            k = spec["key"]
            # NOTE the keys end in _btn: _is_action_key() must recognise these
            # or the sub-page persist sweep tries to self-assign a button key
            # and crashes on a LATER page. See CLAUDE.md, Streamlit scar 6.
            plan = cols[0].button("Plan", key="sl_%s_plan_btn" % k)
            go = cols[1].button("Load", key="sl_%s_apply_btn" % k,
                                type="primary")
            undo = cols[2].button("Undo", key="sl_%s_undo_btn" % k)

            # st.rerun() RAISES, so anything rendered before it is destroyed.
            # The outcome is stashed and drawn on the next run instead.
            if plan or go or undo:
                ok, out = run_loader(spec, apply=(go or undo), remove=undo)
                st.session_state["sl_out_%s" % k] = (ok, out)
                if go or undo:
                    # THE ROW COUNTS ABOVE WERE DRAWN BEFORE THIS RAN, so they
                    # describe the database as it was a moment ago. Leaving
                    # them there puts "dv_well_shows 10" directly above "10
                    # removed", which reads as a failed undo. Stash first --
                    # st.rerun() RAISES, and anything rendered before it is
                    # destroyed -- then rerun so the counts are re-read.
                    st.rerun()

            stash = st.session_state.get("sl_out_%s" % k)
            if stash:
                ok, out = stash
                (st.success if ok else st.error)(
                    "Finished." if ok else "Did not complete — read below.")
                st.code(out or "(no output)", language="text")


main = show = app = render
