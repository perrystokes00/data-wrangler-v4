"""
page_well_map.py
================
DataView v3 — Map Window

A live window into what is stored in the DataView database and
registered shapefiles. Every significant spatial dataset is queryable
as a toggleable layer.

Database layers:
  Wells, Well Trajectories, Formation Tops, DST Intervals,
  Production Bubbles, Fields, Basins

Shapefile layers:
  Read from dv_spatial_layer registry (GEOJSON or SHAPEFILE source_type)

Called from app.py:
    from dataview.mapping import page_well_map
    page_well_map.run(engine)
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import h3
import pandas as pd
import streamlit as st

from dataview.import_data import exporters   # multi-format result-set exporters (sits beside this file)


# ── help "?" badge (uses the global .card-help CSS injected by app_v3.py) ──
def _help_badge(text, top="2px", right="6px"):
    """Gold '?' help badge anchored to the top-right of the current column or
    container. Call right before the widget whose hover text it carries.
    Self-anchoring (own relative wrapper), newline-safe. Tune top/right to
    nudge placement."""
    import html as _html
    if not text:
        return
    _safe = _html.escape(str(text), quote=True).replace("\r", "").replace("\n", "&#10;")
    st.markdown(
        f"<div style='position:relative;width:100%;height:0;z-index:7'>"
        f"<span class='card-help' style='top:{top};right:{right}' "
        f"data-help=\"{_safe}\">?</span>"
        f"</div>",
        unsafe_allow_html=True)
# BCP server config for the BCP-bypass loaders (_qry_wells_bcp /
# _qry_gom_wells_bcp). Matches the SQL Server instance / database used
# by the rest of the page. If you change the SQLAlchemy CONN_STR for
# the main connection, update these too — BCP is a separate executable
# that doesn't share the SQLAlchemy connection string.
# ── KEYS THAT MUST NEVER BE WRITTEN BACK TO SESSION STATE ───────────────
#
# Both sub-pages (Documents, Export) keep widget state alive across the page
# switch by self-assigning every key: `st.session_state[k] = st.session_state[k]`.
# That is the standard Streamlit idiom and it works for INPUT widgets —
# text_input, selectbox, checkbox, slider.
#
# It is INVALID for ACTION widgets: button, download_button,
# form_submit_button, file_uploader. Streamlit refuses to let their value be
# set from session_state at all.
#
# 🔑 AND THE ERROR DOES NOT SURFACE AT THE ASSIGNMENT. It is raised when the
# WIDGET IS CREATED, on a later run:
#
#     Values for the widget with key 'wm_shp_add' cannot be set using
#     st.session_state
#
# which is why the try/except wrapped around the assignment never caught it,
# and why the failure appears on a completely different page from the code
# that caused it. Returning from Export poisoned a button on the map.
#
# The skip lists below already named several action keys — they were added
# one at a time, each after a bug like this one. A NAME-BY-NAME DENY LIST
# CANNOT HOLD: every new button is a future crash, and the crash lands
# somewhere else. So this also matches on how action widgets are NAMED here,
# which catches the next one without anybody remembering.
import re as _re

_ACTION_KEY_SUFFIXES = (
    "_add", "_btn", "_go", "_run", "_clear", "_apply", "_save", "_del",
    "_delete", "_refresh", "_reset", "_open", "_close", "_back", "_dl",
    "_download", "_upload", "_submit", "_cancel", "_toggle", "_export",
)
# SETTABLE WIDGETS THAT THE SUFFIX RULES CATCH BY ACCIDENT. The suffixes
# describe what a key usually MEANS, and a checkbox called "near_open" is
# named for what it opens, not for being an action. Excluding it does not
# crash -- it silently drops the value on every page switch, so the box
# keeps coming back unticked and nothing says why. Checked FIRST.
_ACTION_KEY_NEVER = {
    "wm_near_open",
}
_ACTION_KEY_EXACT = {
    "wm_shp_add", "wm_ai_run", "wm_ai_clear", "wm_reset_page",
    "apply_uwi_filter", "wells_clear_viewport", "wells_reset_view",
    "view_summary", "clear_tray", "close_summary_bottom",
    "open_docs_btn", "export_xlsx_btn", "docs_back", "export_back",
    # Found by sweeping every st.button/download_button key in this
    # file against the predicate — the one name the suffix rules
    # missed. Re-run that sweep after adding a button.
    "close_summary",
    # Found by the automated sweep, not by a crash: a BUTTON whose name
    # matches no action suffix, so the persist loop self-assigned it and
    # it would have raised on whatever page drew next, far from here.
    "wm_compute_paths",
}


def _is_action_key(k):
    """True when k belongs to a widget whose value cannot be set."""
    s = str(k)
    if s in _ACTION_KEY_NEVER:
        return False
    return (s in _ACTION_KEY_EXACT
            or s.endswith(_ACTION_KEY_SUFFIXES)
            # dynamic keys built from a file path — the universal viewer's
            # per-file download buttons ("las_dl_C:\...\file.las")
            # A BARE COLON IS NOT ENOUGH: "results_mode:v1" is a
            # SELECTBOX whose key carries a version marker, and skipping
            # it silently reset that control every time someone came back
            # from Export. Match a real separator, or a drive letter
            # followed by one.
            or "\\" in s or "/" in s
            or bool(_re.search(r"[A-Za-z]:[\\/]", s))
            # DATA EDITORS CANNOT BE SET EITHER, and they are not "action"
            # widgets so nothing above catches them. `tray_grid:sel` is the
            # Results tray's st.data_editor: the persist loops self-assigned
            # it, the assignment raised, the try/except swallowed it, and the
            # error surfaced LATER — "Values for the widget with key
            # 'tray_grid:sel' cannot be set using st.session_state" — on
            # whatever run next instantiated the widget.
            #
            # ⚠ A BARE COLON IS NOT THE TEST, for the reason given above:
            # "results_mode:v1" is a SELECTBOX carrying a version marker, and
            # skipping it reset that control on every return from Export.
            # Match the editor SUFFIX instead.
            or s.endswith((":sel", ":edit", ":editor", "_editor"))
            # FORM SUBMIT BUTTONS. Streamlit names them internally as
            # "FormSubmitter:<form key>-<label>" and they cannot be set either.
            # Self-assigning one corrupts the form's state, and the symptom is
            # not an obvious error — the form renders with NO SUBMIT BUTTON at
            # all, plus Streamlit's own warning that user interactions will
            # never be sent. Same root cause as tray_grid:sel, one more widget
            # type nobody had added to this list.
            or s.startswith("FormSubmitter:"))


BCP_SERVER = r"localhost\SQLEXPRESS"
BCP_DATABASE = "DataView"

# Max wells a single drill / "Send results to tray" action auto-adds to the
# object tray. Keeps the tray (and st_folium serialization) usable; larger
# result sets are offered via an explicit "add first N" prompt.
_TRAY_AUTO_ADD_CAP = 500

# Hard cap on how many wells a single load pulls (TOP n in SQL). Guardrail so
# the loader never tries to render the full 500K+ table. No UI control — change
# here if needed.
_WELLS_LOAD_CAP = 30000

# How many individual markers the browser is actually asked to draw. The cap
# above bounds the QUERY; this bounds the RENDER, and they are not the same
# limit. Measured on this machine: 1,373 wells ~2.0s per render, 28,173 wells
# 593s -- ten minutes, with the page greyed out throughout, which reads as a
# frozen app. Clustering is deliberately off, so every marker is serialised
# into the map HTML on every rerun and the cost grows faster than the count.
#
# 5,000 is chosen from that curve, not from taste: it is roughly where a
# render stays inside a few seconds. DW_MAP_WELL_DRAW_CAP overrides it.
try:
    _WELLS_DRAW_CAP = int(os.environ.get("DW_MAP_WELL_DRAW_CAP", "5000"))
except ValueError:
    _WELLS_DRAW_CAP = 5000

# Sentinel "state" for the offshore Gulf of Mexico in the Constrain-to State
# dropdown. Picking it swaps the County sub-list for Protraction Areas and
# constrains wells to the Gulf (whole footprint, or a specific area).
_GULF_STATE = "Gulf of Mexico"


# -----------------------------------------------------------------------------
# User preferences — small JSON file alongside the page, persists user-tunable
# settings (currently the uncluster zoom threshold) across Streamlit restarts
# and browser sessions. session_state alone resets when the browser closes.
# -----------------------------------------------------------------------------
_USER_PREFS_PATH = Path(__file__).parent / "user_prefs.json"


# -----------------------------------------------------------------------------
# EXECUTION TIMING
# -----------------------------------------------------------------------------
# "The map is slow" is not a diagnosis. This records where a render's time
# actually goes.
#
# EACH MARK PRINTS AS IT HAPPENS, not as a summary at the end. st.rerun()
# RAISES, so a render that reruns never reaches a tail report -- and the slow
# renders are exactly the ones that rerun. A discarded diagnostic has cost this
# project whole evenings; `except: return [], 0` is in CLAUDE.md for a reason.
#
# The _qry_/_add_ functions are instrumented by WRAPPING THEM AT IMPORT (see
# _install_timers at the bottom of this file) rather than by editing ~60 call
# sites in a 14,000-line CRLF file: the edit that instruments everything must
# not be the edit that breaks something. _render_* is deliberately NOT wrapped
# -- two of those are @st.fragment, and wrapping one changes the identity
# Streamlit keys it by, which is its own bug.
#
# Off with DW_MAP_TIMERS=0. The floor keeps the log readable; nothing under it
# is printed, though it is still counted in the totals.
_MAP_TIMERS = os.environ.get("DW_MAP_TIMERS", "1").strip().lower() not in (
    "0", "false", "no", "off")
try:
    _MAP_TIMER_FLOOR = float(os.environ.get("DW_MAP_TIMER_FLOOR", "0.05"))
except ValueError:
    _MAP_TIMER_FLOOR = 0.05


def _say(msg):
    """Print a diagnostic that can NEVER be the thing that breaks the page.

    "Well Map error: 'charmap' codec can't encode character '\\U0001f310'" --
    the timing instrumentation took down the page it was measuring.

    The phase labels carry emoji ("Rendering map in browser"), _mark prints
    them, and start.bat now redirects stdout to logs\\dev.out.log. A REDIRECTED
    stdout on Windows gets the ANSI codepage, not the console's, so print()
    raised UnicodeEncodeError where the same line had been fine in a console
    window. Two harmless changes, combining.

    A measurement must not be able to break what it measures. Every print here
    goes through this, and the fallback degrades the characters rather than the
    render. sys is deliberately not used: it is NOT imported in this module,
    and reaching for a bare name that only fails when the line runs is the
    exact trap CLAUDE.md opens with.
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    except Exception:
        pass


def _tstate():
    """session_state, or a throwaway dict when there is no script context.

    Timing must never be the thing that raises. A wrapped _qry_ can be called
    from a worker thread or at import, where touching session_state throws.
    """
    try:
        return st.session_state
    except Exception:
        return {}


class _MsgBelowMap:
    """Collects the map's status messages and renders them UNDER the map.

    THE MESSAGES WERE THE PUSH-DOWN. _msg was an st.empty() created ABOVE
    st_folium, and 39 places write into it -- how many hexes drew, how many
    reference wells and whether they were a sample, what the clip did, why a
    layer is empty. Every one of them pushed the map down by its own height,
    and because they vary run to run the map started somewhere different each
    time. Reported as "it is still getting pushed down" and "sometimes I see a
    statement above the page": the same thing, seen twice.

    A BUFFER RATHER THAN A MOVED st.empty(), because a placeholder renders
    where it is CREATED, not where it is written. Creating it after the map
    would put the messages in the right place and raise NameError in all 39
    writers, which run long before the map is built. This collects instead,
    and flush() replays into a container made after st_folium.

    empty() clears the queue, which is what its three callers mean by it.
    """

    def __init__(self):
        self._q = []

    def _add(self, kind, *a, **k):
        self._q.append((kind, a, k))

    def info(self, *a, **k):
        self._add("info", *a, **k)

    def warning(self, *a, **k):
        self._add("warning", *a, **k)

    def error(self, *a, **k):
        self._add("error", *a, **k)

    def success(self, *a, **k):
        self._add("success", *a, **k)

    def caption(self, *a, **k):
        self._add("caption", *a, **k)

    def markdown(self, *a, **k):
        self._add("markdown", *a, **k)

    def empty(self):
        self._q = []

    def flush(self, target):
        """Replay into `target`. Never raises: a status line must not be the
        thing that breaks the page it is describing."""
        for kind, a, k in self._q:
            try:
                getattr(target, kind)(*a, **k)
            except Exception:
                pass
        self._q = []
def _marks_begin(tag=""):
    """Start a render's timing, keeping the previous render's for display.

    THE HEADER CARRIES THE RENDER COUNT AND THE PREVIOUS RENDER'S TOTAL,
    because the first log this produced showed no slow step anywhere and was
    still describing a slow page: fifty renders at ~2.8s each. The count is
    the number that explains it, so it goes where it cannot be missed.

    A render whose header is followed by NO marks did no work -- it ended in
    an st.rerun() before reaching the first phase. Those are pure waste and
    the run of seven in a row is what sent me looking.
    """
    if not _MAP_TIMERS:
        return
    _s = _tstate()
    _prev_total = 0.0
    if _s.get("_wm_marks"):
        _s["_wm_marks_prev"] = _s.get("_wm_marks")
        _s["_wm_calls_prev"] = _s.get("_wm_calls") or []
        _prev_total = max((_m.get("cumulative", 0)
                           for _m in _s.get("_wm_marks") or []), default=0.0)
    _n = int(_s.get("_wm_render_n") or 0) + 1
    _s["_wm_render_n"] = _n
    _s["_wm_render_total"] = float(_s.get("_wm_render_total") or 0.0) + _prev_total
    _now = time.perf_counter()
    _s["_wm_marks"], _s["_wm_calls"] = [], []
    _s["_wm_mark_t0"] = _s["_wm_render_t0"] = _now
    _say("[map] ===== render #%d start  (previous %.2fs, %.1fs in %d renders)"
         "  %s" % (_n, _prev_total, _s["_wm_render_total"], _n - 1, tag or ""))


def _mark(label):
    """Time since the previous mark. Cheap enough to leave switched on."""
    if not _MAP_TIMERS:
        return
    _s = _tstate()
    _now = time.perf_counter()
    _t0 = _s.get("_wm_mark_t0")
    if _t0 is None:
        _s["_wm_mark_t0"] = _s["_wm_render_t0"] = _now
        return
    _dt = _now - _t0
    _s["_wm_mark_t0"] = _now
    _cum = _now - _s.get("_wm_render_t0", _now)
    _s.setdefault("_wm_marks", []).append(
        {"step": str(label)[:70], "seconds": round(_dt, 3),
         "cumulative": round(_cum, 3)})
    if _dt >= _MAP_TIMER_FLOOR:
        _say("[map] %8.3fs  %-44s (cum %6.2fs)"
              % (_dt, str(label)[:44], _cum))


def _timed(name, fn):
    """Wrap fn so every call is timed, logged and totalled."""
    import functools

    @functools.wraps(fn)
    def _w(*a, **k):
        if not _MAP_TIMERS:
            return fn(*a, **k)
        _t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            # FINALLY, so a call that RAISES is still timed. st.rerun() raises
            # by design and several of these run either side of one; a timer
            # that only records success would hide the expensive failures.
            _dt = time.perf_counter() - _t0
            if _dt >= _MAP_TIMER_FLOOR:
                try:
                    _tstate().setdefault("_wm_calls", []).append(
                        {"call": name, "seconds": round(_dt, 3)})
                except Exception:
                    pass
                _say("[map] %8.3fs  %s()" % (_dt, name))
    return _w


# -----------------------------------------------------------------------------
# WHAT "AN OPTION CHANGED" MEANS, for Hold for Map
# -----------------------------------------------------------------------------
# DERIVED BY PREFIX, NOT WRITTEN DOWN. This was a six-name tuple --
# wm_area_sel, wm_query_sel, wm_basemap, wm_map_db, wm_geo_pills,
# h3_resolution -- and every control added to the page since was missing from
# it. wells_layer_on and h3_layer_on among them, which is to say the two
# switches that decide what the map draws at all. Hold was on, the operator
# changed a setting, and the map redrew anyway: the toggle appeared to do
# nothing, which is worse than not having it.
#
# Same shape as the four-lists-that-must-agree in CLAUDE.md, and the fix is
# the same one: stop maintaining the list. What matters is WHICH WAY IT FAILS.
# Listed, a forgotten setting means Hold silently does nothing. Derived, a
# forgotten setting means a hold you did not want -- visible, and one click of
# Apply to clear. Fail toward the visible one.
#
# The deny-list is the controls that must NOT hold: the hold/freeze toggles
# themselves, and the tools that carry their own Run or Send button, where the
# map should redraw when the action fires rather than while a box is being
# filled in.
_OPT_PREFIXES = ("wm_", "h3_", "wells_", "seis_")
_OPT_DENY = frozenset({
    "wm_hold_map", "wm_freeze_map", "wm_reset_page",
    "wm_ai_question", "wm_ai_scope", "wm_ai_run", "wm_ai_clear",
    "wm_near_dist", "wm_near_feat", "wm_near_run", "wm_near_open",
    "wm_compute_paths",
    # the reference-well loader: picking a county in it is not a map option,
    # and holding the map because someone opened the picker would be absurd
    "wm_seed_county",
    "seis_basket_sel", "seis_basket_all", "seis_basket_clear",
    "seis_open_sel", "seis_pick_clear", "seis_il_no", "seis_xl_no",
    "wells_clear_viewport", "wells_reset_view",
})
# Saved places live in their own @st.fragment and "Go" does its own app-scoped
# rerun, so none of that block is a map option.
_OPT_DENY_PREFIX = ("wm_place_",)
# _ver is the widget-key version counter from Streamlit scar #1 -- internal
# bookkeeping, not a setting. _page is navigation. The message and error slots
# are output, and including output in a signature of the INPUTS is how a
# signature comes to differ from itself.
_OPT_DENY_SUFFIX = ("_btn", "_button", "_msg", "_msgs", "_err", "_ver",
                    "_page", "_editor", ":sel")
_OPT_SCALARS = (str, int, float, bool, type(None))


def _map_option_sig():
    """Signature of every setting whose change should hold the map.

    SCALARS ONLY, and that is not tidiness. A key holding a DataFrame or a
    result set can differ between two renders that changed nothing, and a
    signature that never matches means the map is held forever -- which
    fails in the same invisible direction as the bug this replaces.
    """
    _out = []
    for _k, _v in list(st.session_state.items()):
        if not isinstance(_k, str) or _k.startswith(("_", "FormSubmitter:")):
            continue
        if (_k in _OPT_DENY or _k.endswith(_OPT_DENY_SUFFIX)
                or _k.startswith(_OPT_DENY_PREFIX)):
            continue
        if not _k.startswith(_OPT_PREFIXES):
            continue
        if isinstance(_v, _OPT_SCALARS):
            _out.append((_k, repr(_v)))
        elif isinstance(_v, (list, tuple, set, frozenset)) and all(
                isinstance(_x, _OPT_SCALARS) for _x in _v):
            _out.append((_k, repr(sorted(map(repr, _v)))))
        # anything else -- frames, engines, blobs -- is deliberately skipped
    return repr(sorted(_out))


def _install_timers():
    """Wrap every _qry_/_add_ in this module. Called at the bottom of the file.

    Idempotent: Streamlit re-imports a page module on some reloads, and
    double-wrapping would double-count and double-log.
    """
    if not _MAP_TIMERS:
        return
    import types
    _g = globals()
    _n_wrapped = 0
    for _name, _fn in list(_g.items()):
        if (isinstance(_fn, types.FunctionType)
                and _name.startswith(("_qry_", "_add_"))
                and not getattr(_fn, "_dw_timed", False)):
            _wrapped = _timed(_name, _fn)
            _wrapped._dw_timed = True
            _g[_name] = _wrapped
            _n_wrapped += 1
    if _n_wrapped:
        _say("[map] timing enabled on %d function(s); DW_MAP_TIMERS=0 to "
              "switch off" % _n_wrapped)
def _install_rerun_trace():
    """Make every st.rerun() say which LINE called it.

    TWO UNEXPLAINED RERUN LOOPS IN ONE DAY, and neither could be located from
    the log: a render that ends in st.rerun() before the first mark prints its
    header and nothing else, and there are seventeen st.rerun() calls between
    the header and that first mark. The header tells you the page is spinning
    and not one thing about why.

    A line number turns "618 zero-mark renders" into "618 reruns from line
    N", which is the whole diagnosis. sys._getframe is used rather than
    inspect.stack() because it is O(1) -- this fires on every rerun, and in a
    loop that is thousands of times.

    `sys` is imported INSIDE the wrapper on purpose: it is not a module-level
    import in this file, and _say already documents why reaching for a bare
    name that only fails when the line runs is the trap CLAUDE.md opens with.

    Idempotent, and off with DW_MAP_TIMERS=0 like every other diagnostic here.
    """
    if not _MAP_TIMERS:
        return
    if getattr(st.rerun, "_dw_traced", False):
        return
    _orig = st.rerun

    def _traced(*a, **k):
        import sys as _sys
        try:
            _say("[map] st.rerun() from line %d"
                 % _sys._getframe(1).f_lineno)
        except Exception:
            # A DIAGNOSTIC MUST NOT BE THE THING THAT BREAKS THE PAGE.
            pass
        return _orig(*a, **k)

    _traced._dw_traced = True
    st.rerun = _traced


# ── SAVED PLACES ───────────────────────────────────────────────────────────
# Every demo starts with "show me", and hunting for a county on a world map is
# a poor opening ten seconds. A named extent is one click.
#
# Ships with Teapot because that extent is now established from THREE
# independent sources that agree: the 2005 navigation file, the 1977 3D load
# sheet corners, and the published 2D basemap. A place added by hand from the
# current view is stored the same way.
_BUILTIN_PLACES = {
    "Teapot Dome (NPR-3), WY": [[43.2291, -106.2482], [43.3424, -106.1585]],
}


@st.cache_data(ttl=600, show_spinner=False)
def _region_bounds(_engine, state: str, counties) -> list | None:
    """The extent of a petroleum region's WELLS, measured not declared.

    CACHED BECAUSE IT RUNS ONCE PER REGION, EVERY RENDER. _saved_places calls
    this for every petroleum play to build the "Go to" dropdown -- 13 queries
    on every rerun of every page that draws the map, to produce extents that
    only move when wells are loaded. Profiling put it second behind the
    Teacup panel. The TTL is the backstop for a load happening elsewhere.

    Leading underscore on _engine so Streamlit does not try to hash it; the
    signature already had it, for the same reason.

    PETROLEUM_REGIONS already names the canonical plays — Permian, Eagle Ford,
    Bakken — as (state, [counties]) for the region FILTER. Rather than write a
    second list of hand-typed extents that would drift from it, ask the data
    where those counties' wells actually are. A region with no wells loaded
    returns None and is not offered: a place that goes nowhere is worse than
    an absent one.
    """
    from sqlalchemy import text as _t
    if not state or not counties:
        return None
    _c = [str(c).strip().upper() for c in counties if str(c).strip()]
    if not _c:
        return None
    _marks = ", ".join(f":c{i}" for i in range(len(_c)))
    _p = {f"c{i}": v for i, v in enumerate(_c)}
    _p["st"] = str(state).strip().upper()
    try:
        with _engine.connect() as cx:
            r = cx.execute(_t(
                f"SELECT MIN(surface_latitude), MIN(surface_longitude), "
                f"       MAX(surface_latitude), MAX(surface_longitude), COUNT(*) "
                f"  FROM dataview.dv_well WITH (NOLOCK) "
                f" WHERE UPPER(LTRIM(RTRIM(province_state))) = :st "
                f"   AND UPPER(LTRIM(RTRIM(county))) IN ({_marks}) "
                f"   AND surface_latitude IS NOT NULL"), _p).fetchone()
    except Exception:
        return None
    if not r or not r[4] or r[0] is None:
        return None
    # a hair of padding so edge wells are not on the frame line
    _pad = 0.02
    return [[float(r[0]) - _pad, float(r[1]) - _pad],
            [float(r[2]) + _pad, float(r[3]) + _pad]]


def _saved_places(_engine=None) -> dict:
    """Built-ins, petroleum regions that have wells, then anything saved.

    Order is deliberate — a USER entry wins any name clash, because their view
    of a play is more current than a built-in of mine.
    """
    out = dict(_BUILTIN_PLACES)
    if HAS_PETROLEUM_REGIONS:
        for _nm, _v in (PETROLEUM_REGIONS or {}).items():
            if str(_nm).startswith("—"):
                continue
            # EVERY REGION IS OFFERED, with or without wells.
            #
            # My first cut derived each region's extent from ITS WELLS and
            # dropped any with none — so on a database holding only Teapot the
            # dropdown showed no plays at all. Wrong: a basin is a geographic
            # fact, and it does not stop existing because this database has not
            # been loaded yet. Wanting to LOOK at the Eagle Ford before loading
            # it — or to check afterwards that a load landed in the right
            # place — is the normal case, not an edge one.
            #
            # And the registry already states each region's centre as the third
            # tuple element, with STATE_CENTERS behind it. _region_zoom_target
            # has resolved both since long before I got here; querying wells
            # for a position it already knew was a second mechanism doing a
            # worse job.
            _c = _region_zoom_target(_nm, _v)
            if not _c:
                continue
            _lat, _lon, _z = _c
            # Same zoom-to-span rule the area auto-zoom uses, so a region
            # frames the way every other navigation on this page frames.
            _sp = max(0.5, 30.0 / (2 ** (int(_z) - 4)))
            _b = [[_lat - _sp / 2, _lon - _sp], [_lat + _sp / 2, _lon + _sp]]
            # If wells ARE loaded, their measured extent is tighter and more
            # useful than the registry's nominal centre — prefer it, and say
            # which one the label is showing.
            _wb = _region_bounds(_engine, *_v[:2]) if _engine is not None else None
            out[f"{_nm} (wells)" if _wb else _nm] = _wb or _b
    try:
        out.update(_load_user_prefs().get("places") or {})
    except Exception:
        pass
    return out


def _rename_place(old: str, new: str) -> str:
    """Rename a SAVED place. Returns "" on success, else why not.

    REFUSES A COLLISION rather than overwriting. Two places with one name
    cannot both be reached, and silently replacing the other one destroys an
    extent the user drew -- the same reasoning as promote holding a row
    instead of guessing. Shadowing a BUILT-IN is allowed, because that is
    already the documented rule: a user entry wins a name clash.
    """
    new = (new or "").strip()
    if not new:
        return "Give it a name."
    if new == old:
        return ""
    try:
        _p = _load_user_prefs()
        _pl = _p.get("places") or {}
        if old not in _pl:
            return "That place is built in and cannot be renamed."
        if new in _pl:
            return "You already have a place called %s." % new
        _pl[new] = _pl.pop(old)
        _p["places"] = _pl
        _save_user_prefs(_p)
        return ""
    except Exception as exc:
        return "Could not rename: %s" % exc


def _repoint_place(name: str, bounds) -> str:
    """Point an existing SAVED place at a new extent. "" on success.

    Normalised through _norm_bounds like every other write, so a re-point
    cannot reintroduce the flat shape the save fallback used to store.
    """
    _b = _norm_bounds(bounds)
    if _b is None:
        return "No extent to use. Draw a box first."
    try:
        _p = _load_user_prefs()
        _pl = _p.get("places") or {}
        if name not in _pl:
            return "That place is built in and cannot be changed."
        _pl[name] = _b
        _p["places"] = _pl
        _save_user_prefs(_p)
        return ""
    except Exception as exc:
        return "Could not update: %s" % exc


def _delete_place(name: str) -> bool:
    """Remove a SAVED place. Built-ins and regions cannot be deleted — they
    are code and data respectively, so a delete would appear to work and the
    entry would be back on the next run, which is worse than refusing."""
    try:
        _p = _load_user_prefs()
        if name in (_p.get("places") or {}):
            del _p["places"][name]
            _save_user_prefs(_p)
            return True
    except Exception:
        pass
    return False


def _norm_bounds(v):
    """[[min_lat, min_lon], [max_lat, max_lon]], from either shape, or None.

    TWO SHAPES REACHED THE SAME STORE. The rectangle handler sets
    _drawn_bounds as nested pairs and _active_drill_bbox as a flat
    (min_lat, max_lat, min_lon, max_lon), and "save this place" took
    whichever survived -- so the file holds both, and fit_bounds understands
    only one. Four bare numbers are not rejected by folium; they simply put
    the camera somewhere meaningless, which reads as "save does not keep my
    zoom".

    Normalising on the way IN and on the way OUT means the places already
    written in the flat shape start working without being re-saved. The flat
    reading is lat, lat, lon, lon because that is the order the one producer
    writes; the min/max sort makes a corner-swapped rectangle harmless too.
    """
    try:
        if v is None:
            return None
        # A place may now be {"bounds": [...], "shapes": [...]} -- the extent
        # plus the shapes that were drawn when it was saved. Older places are
        # a bare list and stay that way, so nothing has to be migrated and a
        # place saved before this still opens.
        if isinstance(v, dict):
            v = v.get("bounds")
            if v is None:
                return None
        if (len(v) == 2 and all(hasattr(p, "__len__") and len(p) == 2
                                for p in v)):
            (a_lat, a_lon), (b_lat, b_lon) = v
        elif len(v) == 4 and not any(hasattr(p, "__len__") for p in v):
            a_lat, b_lat, a_lon, b_lon = v
        else:
            return None
        a_lat, b_lat = float(a_lat), float(b_lat)
        a_lon, b_lon = float(a_lon), float(b_lon)
    except (TypeError, ValueError):
        return None
    return [[min(a_lat, b_lat), min(a_lon, b_lon)],
            [max(a_lat, b_lat), max(a_lon, b_lon)]]


def _go_to_place(bounds) -> None:
    """Fit the map to [[min_lat, min_lon], [max_lat, max_lon]].

    Reuses the EXISTING one-shot bounds mechanism rather than adding a second
    way to move the camera — _drawn_bounds is what area changes, circle drills
    and the schema auto-zoom all use, and the consumer pops it after applying
    once so a later rerun does not snap the view back.

    Deliberately does NOT touch the well selection: this moves the CAMERA, not
    the DATA — the same distinction as 🎯 Reset view versus ✗ Clear wells.
    """
    import streamlit as st
    # NORMALISE ON THE WAY OUT TOO, so a place saved in the flat shape before
    # this fix still moves the camera instead of losing it.
    _b = _norm_bounds(bounds)
    if _b is None:
        return
    # Shapes saved with the place, redrawn as an outline when we get there.
    # Set unconditionally -- including to [] -- so going to a place WITHOUT
    # shapes clears the previous place's, rather than leaving someone else's
    # box drawn over a field it has nothing to do with.
    st.session_state["_place_shapes"] = (
        list(bounds.get("shapes") or []) if isinstance(bounds, dict) else [])
    # The layer state travels as a REQUEST, consumed at the top of the next
    # render before the widgets it sets are built.
    #
    # A BARE AREA LEAVES THE LAYERS ALONE. An empty view is falsy, so the
    # consumer skips it -- and that is the right reading of this function's
    # own contract: it moves the CAMERA, not the DATA, the same distinction
    # as Reset view versus Clear wells. Saving "just this area" should not
    # become a way to switch someone's layers off. The SHAPES above do clear,
    # because a leftover annotation draws a selection that is not there.
    st.session_state["_place_pending"] = (
        dict(bounds.get("view") or {}) if isinstance(bounds, dict) else {})
    st.session_state["_drawn_bounds"] = _b
    st.session_state["_drawn_bounds_oneshot"] = True
    # AND THE CLIP BOX, because a named place IS a box somebody drew. The
    # saved view can carry wm_clip_to_box=True, and _clip_bounds_now() reads
    # _clip_box -- deliberately NOT _drawn_bounds, which means "whatever last
    # moved the camera" and is the whole continental US after an area change.
    # Without this line a place saved WITH the clip on came back with the
    # toggle on, no box, and nothing clipped: the feature silently absent in
    # exactly the state that was saved to preserve it.
    st.session_state["_clip_box"] = _b
    # FIT THIS EXACTLY. The fit path pads _drawn_bounds by 15% a side so a
    # data-derived extent is not flush against the edge; on a box the user
    # drew and named, that padding is what drops the recalled view a zoom
    # level below the one it was saved at. The box IS the request.
    st.session_state["_drawn_bounds_exact"] = True
    # AND ALWAYS FIT, even to the extent we last fitted. The fit site skips a
    # target it has already applied, so going to the same place twice -- pan
    # away, press Go again, the ordinary way to use this control -- would
    # otherwise fit nothing and let the view-persist JS put the map back where
    # the user had wandered to. An explicit Go must always move the camera.
    st.session_state.pop("_last_fit_sig", None)
    # 🔑 TELL THE VIEW-PERSIST JS TO DROP ITS SAVED VIEW FIRST.
    # That script restores the previous pan/zoom on every rerun — which is what
    # makes drawing usable, and also what silently undoes a camera move. Set
    # the bounds without this and the region's LAYERS appear while the map
    # stays where it was, which reads as "the zoom does not work".
    #
    # 🎯 Reset view has set this flag since July for exactly this reason. My
    # first cut POPPED it — removing the one thing that makes a camera move
    # stick.
    st.session_state["_reset_saved_view"] = True


BOUNDARY_SOURCE = "MAP_DRAWN"
BOUNDARY_TYPES = ["Pool", "Fault block", "Unit", "AOI", "Prospect", "Other"]


def _save_drawn_boundary(engine, name: str, btype: str, feature: dict) -> str:
    """Store one drawn shape in dv_boundary. "" on success, else why not.

    dv_boundary IS THE RIGHT HOME and no new table is needed: it is
    boundary_name + boundary_type + geog, the 🟪 Boundaries chip already
    draws it, and a pool outline is exactly a named boundary of a type.

    ORIENTATION, as everywhere geography is written here. A clockwise ring is
    the planet minus the polygon -- STArea comes back in the hundreds of
    millions of km2 and it "contains" every well there is. Reoriented on the
    way in, the same guard gen_synthetic_leases and _load_seis apply.

    Stamped source='MAP_DRAWN' so what a person drew stays separable from
    anything a loader wrote.
    """
    # LOCAL, because uuid is not bound at module level in this file -- the
    # only `import uuid` here is inside another function, so a bare uuid.uuid4
    # raises NameError the moment this runs. Exactly the shape that made every
    # enrichment write in extract_core fail while the stage reported success.
    import uuid as _uuid
    name = (name or "").strip()
    if not name:
        return "Give it a name."
    try:
        geom = (feature or {}).get("geometry") or {}
        if str(geom.get("type")) not in ("Polygon", "MultiPolygon"):
            return "That shape is not an area — draw a polygon or a rectangle."
        rings = geom.get("coordinates") or []
        if not rings or not rings[0] or len(rings[0]) < 4:
            return "That polygon has too few points to close."
    except Exception:
        return "Could not read the drawn shape."

    from sqlalchemy import text as _t
    try:
        with engine.begin() as con:
            dup = con.execute(_t(
                "SELECT COUNT(*) FROM dataview.dv_boundary "
                "WHERE boundary_name = :n"), {"n": name}).scalar()
            if dup:
                # Refuse rather than overwrite: two outlines under one name
                # cannot both be found, and replacing the other destroys
                # something someone drew.
                return "There is already a boundary called %s." % name
            con.execute(_t("""
                INSERT INTO dataview.dv_boundary
                    (boundary_id, boundary_name, boundary_type, country,
                     area_km2, geog, active_ind, source,
                     row_created_by, row_created_date)
                -- REORIENT FIRST, THEN MEASURE. Computing area from the raw
                -- ring while storing the reoriented one stored the shape
                -- correctly and the SIZE of its complement: a clockwise
                -- pentagon came out at 510,065,547 km2 with the right wells
                -- inside it. Both columns now read the same geography.
                SELECT :id, :n, :ty, 'USA', g2.STArea()/1000000.0, g2,
                       'Y', :src, :src, GETUTCDATE()
                  FROM (SELECT CASE WHEN g.STArea()/1000000.0 > 100000
                                    THEN g.ReorientObject() ELSE g END AS g2
                          FROM (SELECT geography::STGeomFromText(
                                   geometry::STGeomFromText(:wkt, 4326)
                                       .MakeValid().STAsText(), 4326) AS g
                               ) q1) q
            """), {"id": _uuid.uuid4().hex[:40].upper(), "n": name,
                   "ty": btype or "Other",
                   "wkt": _geojson_to_wkt(geom), "src": BOUNDARY_SOURCE})
        return ""
    except Exception as exc:
        return "Could not save: %s" % str(exc)[:160]


def _geojson_to_wkt(geom: dict) -> str:
    """GeoJSON Polygon -> WKT. Rings only; holes are kept.

    Leaflet gives [lon, lat] and WKT wants the same order, so no swap -- the
    swap is what a reader expects to find here and its absence is deliberate.
    """
    def ring(rs):
        pts = ["%.8f %.8f" % (float(p[0]), float(p[1])) for p in rs]
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        return "(" + ", ".join(pts) + ")"
    coords = geom.get("coordinates") or []
    if str(geom.get("type")) == "MultiPolygon":
        return "MULTIPOLYGON(" + ", ".join(
            "(" + ", ".join(ring(r) for r in poly) + ")"
            for poly in coords) + ")"
    return "POLYGON(" + ", ".join(ring(r) for r in coords) + ")"


# ── WHAT A SAVED PLACE REMEMBERS ───────────────────────────────
# ONE LIST, READ TWICE. _capture_map_view and _apply_map_view are the two
# halves of one contract and were written out separately -- so they drifted
# the first time one gained a key. wm_clip_to_box was added to capture and
# not to apply, and a place saved WITH the clip on came back without it: the
# feature silently absent in exactly the state saved to preserve it, and the
# save looked like it had worked. Same shape as the four lists that must
# agree in CLAUDE.md, and the same fix: stop maintaining two.
_VIEW_KEYS = ("map_mode", "wells_layer_on", "h3_layer_on", "h3_resolution",
              "wm_clip_to_box", "wm_lease_color_by")


def _capture_map_view() -> dict:
    """What is currently ON the map: layers, mode, and the seismic choice.

    Saved with a place so returning to it redraws what was there, not just
    where it was. An area with nothing on it saves nothing extra -- an empty
    dict here means the place is stored as a bare extent, exactly as before.
    """
    out = {}
    _pills = st.session_state.get("wm_geo_pills")
    if _pills:
        out["pills"] = list(_pills)
    for _k in _VIEW_KEYS:
        _v = st.session_state.get(_k)
        if _v not in (None, False, "none"):
            out[_k] = _v
    # The seismic choice lives in the prefs FILE, not session_state -- it is
    # the channel the second screen writes through. Capture the resolved
    # value so a place remembers which lines were drawn.
    try:
        _sc = _map_seis_choice()
        if _sc.get("mode") != "all" or _sc.get("lines") or _sc.get("surveys"):
            out["seis"] = _sc
    except Exception:
        pass
    return out


def _apply_map_view(view: dict) -> None:
    """Restore a saved view. MUST RUN BEFORE THE LAYER WIDGETS DRAW.

    Assigning a widget's own key after its widget exists is scar #6 in this
    codebase: it raises on a LATER run, on whatever page happens to draw
    next, which is as far from the cause as an error can land. Go therefore
    stores a REQUEST and asks for a full rerun; this consumes it at the top
    of the map column, before st.pills and the layer toggles are built.
    """
    if not isinstance(view, dict):
        return
    if "pills" in view:
        st.session_state["wm_geo_pills"] = list(view["pills"] or [])
    for _k in _VIEW_KEYS:
        if _k in view:
            st.session_state[_k] = view[_k]
    _seis = view.get("seis")
    if isinstance(_seis, dict):
        # Written straight to the prefs file rather than through
        # _write_map_seis, which ends in st.rerun() -- rerunning from inside
        # the consumer would drop the rest of this restore on the floor.
        try:
            _p = _load_user_prefs()
            _p[MAP_SEIS_PREF] = {"mode": _seis.get("mode") or "all",
                                 "surveys": list(_seis.get("surveys") or []),
                                 "lines": list(_seis.get("lines") or [])}
            _save_user_prefs(_p)
        except Exception:
            pass


def _wells_on_map() -> bool:
    """Is there anything a Clear would remove?"""
    return bool(st.session_state.get("viewport_uwis")
                or st.session_state.get("viewport_gom_wells")
                or st.session_state.get("processed_drawings")
                or st.session_state.get("clicked_uwis"))


def _clear_wells_state() -> None:
    """Remove every displayed well: drill, base layer, Results and tray.

    LIFTED OUT OF WELLS MODE. This lived inside the `elif _new_mode ==
    "wells"` caption branch, so in H3/grid mode -- and behind a collapsed
    expander -- the only button that clears a selection was not on the page
    at all. It was already described in this file as "present in the code,
    invisible in practice", which is exactly how it was reported again.

    A function rather than a second button: the logic is fiddly (four
    selections, the tray, two suppression flags) and two copies of it would
    drift the first time one of them gained a key.
    """
    st.session_state["viewport_uwis"] = []
    st.session_state["viewport_gom_wells"] = []
    st.session_state["processed_drawings"] = set()
    # The accumulated hexagon selection goes with them. viewport_uwis is
    # REBUILT from _h3_cell_uwis on the next cell click, so clearing the list
    # without clearing its source would put every previously-clicked cell
    # straight back -- and selected_h3_cells would keep drawing outlines
    # around a selection the operator just cleared.
    st.session_state.pop("_h3_cell_uwis", None)
    st.session_state.pop("selected_h3_cells", None)
    st.session_state.pop("_last_h3_click", None)
    # The saved-place outline and the retained geometry go too: "clear the
    # wells" plainly includes the box that selected them, and leaving the
    # outline behind would draw a selection that no longer exists.
    st.session_state.pop("_place_shapes", None)
    st.session_state.pop("_last_drawings", None)
    st.session_state.pop("_drawn_bounds", None)
    st.session_state.pop("_active_drill_bbox", None)
    # The clip box goes with the selection it described.
    st.session_state.pop("_clip_box", None)
    # The camera fits only when its target CHANGES, so the extent just
    # cleared has to stop counting as fitted -- otherwise the next drill of
    # the same area would load its wells and not frame them.
    st.session_state.pop("_last_fit_sig", None)
    # Pending overflow prompt is stale: the user is explicitly resetting.
    st.session_state.pop("_pending_drill_wells", None)
    st.session_state.pop("_pending_drill_label", None)
    # The tray goes too -- one button that wipes everything.
    st.session_state["clicked_uwis"] = []
    st.session_state["scout_uwi"] = None
    st.session_state["show_summary"] = False
    st.session_state["_summary_uwis"] = []
    st.session_state["tray_well_data"] = {}
    st.session_state.pop("_last_grid_click", None)
    # Without this the full wells_df still renders as clusters after a clear
    # -- reported once as "I cleared the viewport but well clusters are
    # still displayed."
    st.session_state["wells_suppressed"] = True
    st.session_state["_wells_already_loaded"] = False


MAP_SEIS_PREF = "map_seis"


def _seis_pref_mtime() -> float:
    """When the shared choice file last changed. 0.0 if it is not there."""
    try:
        return float(_USER_PREFS_PATH.stat().st_mtime)
    except OSError:
        return 0.0
def _scroll_main_to_top():
    """Put the main scroll container back at the top.

    STREAMLIT PRESERVES SCROLL POSITION ACROSS A RERUN, and navigating to a
    page IS a rerun -- so opening the map from a page you had scrolled down
    lands you part-way down a 17,000px page. Reported as "I still have to
    start twice or drag the page up", and it is not layout: the padding and
    the map's own offset measure correctly at 64px the whole time. The
    viewport is simply somewhere else.

    components.html runs inside an iframe, so this reaches for the PARENT
    document's scroller. stMain is the element that actually scrolls here
    (overflow-y:auto, ~17,000px of content); the others are belt and braces
    for other Streamlit versions.

    RETRIED FOR THREE SECONDS, not 200ms. Three tries lost the race and the
    measurement says by how much: the map opened at scrollTop 1759 of a
    3,634px page, and stayed there. The map iframe, the lease GeoJSON and
    the reference-well file all keep loading long after the last retry, and
    the browser restores the old scroll position when the page regains its
    height -- which is necessarily AFTER the content that gives it that
    height. A retry schedule that ends before the page finishes growing can
    only ever win by luck.

    IT STOPS THE MOMENT YOU SCROLL. Retrying for three seconds against a
    user who is deliberately scrolling down would be its own bug -- worse
    than the one it fixes, because it fights back. wheel, touchmove, keydown
    and a mousedown on the scrollbar all cancel the rest of the schedule.
    """
    st.components.v1.html(
        """
        <script>
        (function(){
          var w = window.parent, d = w.document, done = false;
          function toTop(){
            if (done) { return; }
            var els = [
              d.querySelector('section.main'),
              d.querySelector('[data-testid="stMain"]'),
              d.querySelector('[data-testid="stAppViewContainer"]'),
              d.scrollingElement, d.documentElement, d.body
            ];
            for (var i=0;i<els.length;i++){
              var el = els[i];
              if (el){ try{ el.scrollTo(0,0); }catch(e){ el.scrollTop = 0; } }
            }
            try{ w.scrollTo(0,0); }catch(e){}
          }
          function stop(){ done = true; }
          try {
            var o = {passive:true, capture:true};
            d.addEventListener('wheel', stop, o);
            d.addEventListener('touchmove', stop, o);
            d.addEventListener('keydown', stop, o);
            d.addEventListener('mousedown', stop, o);
          } catch(e){}
          var at = [0,50,150,350,700,1200,1800,2500,3200];
          for (var k=0;k<at.length;k++){ setTimeout(toTop, at[k]); }
        })();
        </script>
        """,
        height=0,
    )


@st.fragment(run_every=2)
def _watch_seis_choice():
    """Rebuild the map when the second screen changes what it should draw.

    THE POLL DOES NOT CONTAIN THE MAP, which is the whole reason this is
    affordable. I rejected polling when this was built, on the grounds that
    a fragment re-rendering every two seconds would re-serialise the entire
    map -- true, and irrelevant, because the fragment only has to watch a
    TIMESTAMP. Two seconds of os.stat costs nothing; the expensive rebuild
    happens once, when the file actually changed.

    Without it the map never learned that anything HAD changed. The choice
    was applied on the map's next render and nothing caused a next render,
    so pressing Send on the second screen and switching windows showed the
    previous selection -- and it worked exactly once, on whatever render
    happened to come next for some other reason.

    NO LOOP IS POSSIBLE. The full render records the mtime it drew; this
    compares and reruns only on a difference, and the rerun it triggers
    records the new value. A missing baseline means the map has not drawn
    yet, so there is nothing to refresh.
    """
    _seen = st.session_state.get("_seis_pref_seen")
    if _seen is None:
        return
    _now = _seis_pref_mtime()
    if _now == _seen:
        return
    # ── ONE RERUN PER MTIME, AND THE DOCSTRING ABOVE WAS WRONG ─────────
    # "NO LOOP IS POSSIBLE. The full render records the mtime it drew" --
    # true only if the render REACHES that stamp. This polls every 2s and a
    # map render takes 2-8s, so the fragment aborts the render before the
    # stamp is written, then sees the same difference and fires again. A
    # render slower than the poll interval can never complete.
    #
    # Saving a place writes user_prefs.json, which is the same file this
    # watches -- so "type a name, press Enter" started a loop that ran 246
    # reruns from this line before anything drew.
    #
    # Remembering WHICH mtime was asked for makes a second ask impossible
    # for the same value, while a genuine later change still gets its own
    # rerun. The stamp in the render is still what clears it for good.
    if st.session_state.get("_seis_rerun_for") == _now:
        return
    st.session_state["_seis_rerun_for"] = _now
    st.rerun()


def _map_seis_choice() -> dict:
    """What the second-screen page asked the map to show.

    {"surveys": [names], "lines": ["survey|line"]}, or empty lists.

    THE CHANNEL IS THE PREFS FILE, on purpose. The seismic page is a
    SEPARATE Streamlit session with its own session_state, so nothing in
    Python is shared between the two windows -- the same constraint that
    made the map push travel in the URL. A file both sessions already
    read (it holds the saved places) needs no new store, no polling and
    no JavaScript.

    "mode" is "all", "none" or "pick", and it exists because EMPTY CANNOT
    MEAN BOTH. Empty lists have to mean everything -- a first-run map with no
    file must look exactly as it does today rather than drawing nothing and
    reading as broken -- which left no way to say "draw no seismic at all"
    from the page. Two different intentions were collapsed into one encoding,
    so clearing the map from the second screen was impossible.

    A file written before this key existed has no "mode": lists present means
    it was a deliberate pick, absent means all. So old files keep working and
    nothing has to be migrated.
    """
    _p = (_load_user_prefs().get(MAP_SEIS_PREF) or {})
    _s = [str(x) for x in (_p.get("surveys") or [])]
    _l = [str(x) for x in (_p.get("lines") or [])]
    _mode = str(_p.get("mode") or "").lower()
    if _mode not in ("all", "none", "pick"):
        _mode = "pick" if (_s or _l) else "all"
    return {"mode": _mode, "surveys": _s, "lines": _l}


def _write_map_seis(mode, surveys, lines, msg, msg_key="mapdrive_msg"):
    """Tell the map which seismic to draw. THE ONLY WRITER OF MAP_SEIS_PREF.

    Two doors write this now -- the second screen's grid and the inline
    filters -- and the map reads {mode, surveys, lines} with an exactness
    that would not survive two hand-written copies of the shape.

    Raises, because st.rerun() raises: nothing after a call runs.
    """
    _p = _load_user_prefs()
    _p[MAP_SEIS_PREF] = {"mode": mode, "surveys": surveys, "lines": lines}
    _save_user_prefs(_p)
    st.session_state[msg_key] = msg
    # scope="app" BECAUSE THE CALLER IS A FRAGMENT. _render_seis_pick reruns
    # in isolation so its filters cost nothing, and a fragment-scoped rerun
    # would redraw the chooser while leaving the map showing the OLD
    # selection -- the button would look like it had done nothing. This is
    # the one control in that fragment that has to rebuild the map.
    # Harmless from the second screen's grid, which has no map to rebuild.
    st.rerun(scope="app")


def _seis_map_keys(cands):
    """The (surveys, lines) the map filter wants, from chooser candidates.

    Keyed exactly as the map draw loop and the second screen's grid key
    them: a 2D line is "survey|line", a 3D volume is its survey name alone.
    The "(unnamed survey)" fallback has to match the draw loop's, or a line
    with no survey name is pushed under a key nothing will match.
    """
    keys = []
    for c in (cands or []):
        sv = str(c.get("survey") or "(unnamed survey)")
        keys.append(sv if c.get("dim") == "3D"
                    else "%s|%s" % (sv, c.get("line") or ""))
    return (sorted({k.split("|")[0] for k in keys}),
            sorted(k for k in keys if "|" in k))


def _load_user_prefs() -> dict:
    """Return the user prefs dict, or {} if the file doesn't exist / is bad."""
    try:
        return json.loads(_USER_PREFS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_user_prefs(prefs: dict) -> None:
    """Write user prefs atomically. Silent on failure (not critical)."""
    try:
        # Write to .tmp then rename — atomic on Windows for same-filesystem
        # paths, prevents a half-written file if interrupted.
        tmp = _USER_PREFS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
        tmp.replace(_USER_PREFS_PATH)
    except OSError:
        pass  # Persistence is best-effort; the slider still works in-session.

# BOEM OCS area-code → friendly-name lookup for the GOM Zoom-To dropdown.
# Falls back to a passthrough if the module isn't present, so the page
# still works (just shows bare codes) if boem_area_codes.py is missing.
try:
    from dataview.reference_tables.boem_area_codes import area_name as _boem_area_name
except ImportError:
    def _boem_area_name(code):
        return str(code).strip().upper() if code else ""

# US state/county boundary map (Census counties GeoJSON) powering the
# "Constrain to" spatial selector. Drop the file at
# assets/geo/us_counties.geojson. State/County come from here (not from
# the sparse dv_well.province_state / .county columns), the viewport fits
# the polygon bbox, and the well filter is a lat/lon bbox — valid against
# both dv_well and dataview_gom.well. Falls back gracefully if absent.
try:
    from dataview.mapping import us_geo as _us_geo
    HAS_US_GEO = _us_geo.available()
except Exception:
    _us_geo = None
    HAS_US_GEO = False

# BOEM protraction-area polygons (GeoJSON, WGS84) powering the GOM arm of the
# "Constrain to" selector. Drop the file at assets/geo/gom_protraction.geojson.
# When present, a selected protraction area filters GOM wells SPATIALLY (lat/lon
# bbox on the polygon) instead of by the bottom_area_code attribute — so wells
# with a null/mislabeled code are still caught. Falls back to the attribute
# filter if the file or the matching area is missing.
try:
    from dataview.mapping import boem_geo as _boem_geo
    HAS_BOEM_GEO = _boem_geo.available()
except Exception:
    _boem_geo = None
    HAS_BOEM_GEO = False

# BOEM well status_code → friendly-label lookup for the GOM status
# filter checkboxes. Same passthrough-fallback pattern: if the module
# is missing, checkboxes just show raw codes. status_color gives each
# status a fixed marker color; falls back to neutral slate if missing.
try:
    from dataview.reference_tables.boem_status_codes import (
        status_label as _boem_status_label,
        status_color as _boem_status_color,
    )
except ImportError:
    def _boem_status_label(code):
        return str(code).strip().upper() if code else ""
    def _boem_status_color(code):
        return "#94a3b8"

try:
    import folium
    from streamlit_folium import st_folium
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

try:
    import geopandas as gpd
    HAS_GPD = True
except ImportError:
    HAS_GPD = False

try:
    from sqlalchemy import text
except ImportError:
    pass

try:
    from dataview.mapping.dv_spatial_loader import (
        list_layers, get_layer_geojson,
        LAYER_CATEGORY_DISPLAY,
    )
    HAS_SPATIAL_LOADER = True
except Exception:
    HAS_SPATIAL_LOADER = False
    def list_layers(engine): return []
    def get_layer_geojson(engine, lid): return None
    LAYER_CATEGORY_DISPLAY = {}

# Petroleum-region registry — well-known producing-area definitions
# ported from WranglerView. Used by the Petroleum Region selector in
# the left control panel to filter wells by state + county lists.
# Falls back to a "— none —" stub if the module is missing so the
# page still runs.
try:
    from dataview.region_builder.petroleum_regions import PETROLEUM_REGIONS
    HAS_PETROLEUM_REGIONS = True
except ImportError:
    PETROLEUM_REGIONS = {"— none —": (None, [])}
    HAS_PETROLEUM_REGIONS = False

# State-region registry — user-defined regions built via the Region
# Builder page (page_region_builder.py). Same shape as
# PETROLEUM_REGIONS: each value is (state_code, [county_names]).
# The two are intentionally interchangeable — petroleum_regions is
# canonical plays (Permian, Eagle Ford, ...), state_regions is
# arbitrary user-defined groupings (South Texas, West Texas, ...).
# Both feed the same filter mechanism. If state_regions.py hasn't
# been generated yet, fall back to a stub so the page still runs.
try:
    from dataview.region_builder.state_regions import STATE_REGIONS
    HAS_STATE_REGIONS = True
except ImportError:
    STATE_REGIONS = {"— none —": (None, [])}
    HAS_STATE_REGIONS = False

# =============================================================================
# CONSTANTS
# =============================================================================

STATUS_COLORS = {
    "ACTIVE":    "#1D9E75",
    "COMPLETED": "#378ADD",
    "SHUT_IN":   "#EF9F27",
    "ABANDONED": "#E24B4A",
    "DRILLING":  "#B77FDD",
    "PERMITTED": "#888780",
    "SUSPENDED": "#EF9F27",
    "MONITORING":"#378ADD",
    "UNKNOWN":   "#888780",
}


def _ppdm_symbol_svg(status, color, size=16):
    """
    Standard PPDM / API-style well symbol for a status, as an inline SVG string
    (18×18 viewBox). Shape carries the well's condition; colour carries the
    status palette. Mirrors the JS version in _add_wells so the map markers and
    the legend match exactly.
      producing (active/completed) → filled circle
      shut-in / suspended          → filled circle with a bar
      abandoned / P&A              → open circle with an X (dry/abandoned)
      drilling                     → open triangle (derrick)
      permitted / location         → open circle
      monitoring                   → filled square
      other / unknown              → light open circle
    """
    s = (status or "").upper()
    c = color or "#888780"
    if s in ("ACTIVE", "COMPLETED"):
        inner = f"<circle cx='9' cy='9' r='5' fill='{c}' stroke='#fff' stroke-width='1'/>"
    elif s in ("SHUT_IN", "SUSPENDED"):
        inner = (f"<circle cx='9' cy='9' r='5' fill='{c}' stroke='#fff' stroke-width='1'/>"
                 f"<rect x='8' y='3.5' width='2' height='11' fill='#fff'/>")
    elif s == "ABANDONED":
        inner = (f"<circle cx='9' cy='9' r='5.5' fill='none' stroke='{c}' stroke-width='1.6'/>"
                 f"<line x1='5' y1='5' x2='13' y2='13' stroke='{c}' stroke-width='1.6'/>"
                 f"<line x1='13' y1='5' x2='5' y2='13' stroke='{c}' stroke-width='1.6'/>")
    elif s == "DRILLING":
        inner = f"<polygon points='9,3 15,15 3,15' fill='none' stroke='{c}' stroke-width='1.6'/>"
    elif s == "PERMITTED":
        inner = f"<circle cx='9' cy='9' r='5' fill='none' stroke='{c}' stroke-width='1.6'/>"
    elif s == "MONITORING":
        inner = f"<rect x='4' y='4' width='10' height='10' fill='{c}' stroke='#fff' stroke-width='1'/>"
    else:
        inner = f"<circle cx='9' cy='9' r='5' fill='none' stroke='{c}' stroke-width='1.4'/>"
    return (f"<svg width='{size}' height='{size}' viewBox='0 0 18 18' "
            f"xmlns='http://www.w3.org/2000/svg'>{inner}</svg>")


def _add_status_legend(m, df, ppdm=False):
    """
    Floating legend (bottom-right of the map) for the well colour codes. Built
    from the well_status values actually present in `df`, so it only lists what's
    on screen. Onshore statuses map to STATUS_COLORS; anything else is treated as
    a BOEM status code (GOM) via _boem_status_label/_boem_status_color. When
    ppdm=True the swatch is the PPDM symbol instead of a plain dot.
    """
    if df is None or df.empty or "well_status" not in df.columns:
        return
    _vals = [str(v) for v in df["well_status"].dropna().unique()]
    if not _vals:
        return
    entries, seen = [], set()
    for v in _vals:
        vu = v.upper()
        if vu in STATUS_COLORS:
            col, lbl, skey = STATUS_COLORS[vu], vu.replace("_", " ").title(), vu
        else:
            try:
                lbl = _boem_status_label(v)
            except Exception:
                lbl = v
            try:
                col = _boem_status_color(v)
            except Exception:
                col = "#888780"
            lbl = f"{lbl} ({v})" if lbl and lbl != v else v
            skey = vu
        if (lbl, col) in seen:
            continue
        seen.add((lbl, col))
        entries.append((lbl, col, skey))
    if not entries:
        return
    entries.sort(key=lambda e: e[0])
    entries = entries[:16]
    rows = []
    for lbl, col, skey in entries:
        if ppdm:
            swatch = _ppdm_symbol_svg(skey, col, size=16)
        else:
            swatch = (f"<span style='width:12px;height:12px;border-radius:50%;"
                      f"background:{col};border:1px solid #fff;display:inline-block;"
                      f"flex:0 0 auto'></span>")
        rows.append(
            f"<div style='display:flex;align-items:center;gap:6px;margin:2px 0'>"
            f"{swatch}<span style='font-size:11px;color:#1e293b;white-space:nowrap'>"
            f"{lbl}</span></div>")
    # Inject as a MacroElement with position:fixed. st_folium renders the map
    # in an iframe; position:absolute resolves against whatever positioned
    # ancestor folium emits (often pushing the box off-screen), whereas
    # position:fixed pins to the iframe viewport reliably. The {% macro %}
    # wrapper is the canonical folium floating-legend pattern.
    from branca.element import Template, MacroElement
    # COLLAPSIBLE, AND IT REMEMBERS. A sixteen-entry legend covers a real
    # corner of the map, and the only control was a checkbox that removed it
    # entirely -- so the choice was "in the way" or "gone", with nothing left
    # on screen to say a legend exists. <details> folds to its summary bar,
    # which keeps the affordance visible at about one line high.
    #
    # STATE LIVES IN THE BROWSER, not in session_state. The map is rebuilt on
    # every rerun, so a Python-side toggle would need a rerun to fold a box --
    # greying the map to hide a legend. sessionStorage is what the view-persist
    # JS below already uses for exactly this reason: the browser owns it.
    legend_body = (
        "<details id='wm-status-legend' open style='position:fixed;"
        "bottom:22px;right:12px;z-index:9999;"
        "background:rgba(255,255,255,0.94);padding:6px 11px 8px;"
        "border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,0.35);"
        "max-height:48%;overflow:auto'>"
        "<summary style='font-size:11px;font-weight:700;color:#0f172a;"
        "cursor:pointer;outline:none;user-select:none'>Well status</summary>"
        "<div style='margin-top:4px'>" + "".join(rows) + "</div></details>"
        "<script>(function(){"
        "var d=document.getElementById('wm-status-legend');"
        "if(!d){return;}"
        "try{if(sessionStorage.getItem('dv_legend_open')==='0')"
        "{d.removeAttribute('open');}}catch(e){}"
        "d.addEventListener('toggle',function(){"
        "try{sessionStorage.setItem('dv_legend_open',d.open?'1':'0');}"
        "catch(e){}});"
        "})();</script>")
    macro = MacroElement()
    macro._template = Template(
        "{% macro html(this, kwargs) %}" + legend_body + "{% endmacro %}")
    m.get_root().add_child(macro)

BASEMAPS = {
    "OpenStreetMap":  {
        "tiles": "OpenStreetMap",
        "attr":  "© OpenStreetMap contributors",
    },
    "Esri Satellite": {
        "tiles":    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attr":     "Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community",
        "max_zoom": 19,
    },
    "Esri Topo": {
        "tiles":    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attr":     "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, TomTom, Intermap, iPC, USGS, FAO, NPS, NRCAN, GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community",
        "max_zoom": 19,
    },
    "Esri Street": {
        "tiles":    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        "attr":     "Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS, Intermap, iPC, NRCAN, Esri Japan, METI, Esri China (Hong Kong), Esri (Thailand), TomTom, 2012",
        "max_zoom": 19,
    },
    "CartoDB Light": {
        "tiles":    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "attr":     "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors &copy; <a href='https://carto.com/attributions'>CARTO</a>",
        "max_zoom": 19,
    },
    "CartoDB Dark": {
        "tiles":    "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        "attr":     "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors &copy; <a href='https://carto.com/attributions'>CARTO</a>",
        "max_zoom": 19,
    },
    "USGS Topo": {
        "tiles":    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        "attr":     "Tiles courtesy of the <a href='https://usgs.gov/'>U.S. Geological Survey</a>",
        "max_zoom": 16,
    },
    "Stamen Terrain": {
        "tiles":    "https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}{r}.png",
        "attr":     "&copy; <a href='https://stamen.com'>Stamen Design</a> &copy; <a href='https://stadiamaps.com/'>Stadia Maps</a> &copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a>",
        "max_zoom": 18,
    },
    "Esri Satellite + Labels": {
        "tiles":    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attr":     "Tiles &copy; Esri",
        "max_zoom": 19,
        "overlay":  "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    },
}

# WHICH BACKGROUNDS ARE OFFERED. BASEMAPS keeps all nine definitions — the
# tile URLs and attributions are the awkward part to get right and they cost
# nothing sitting here. This is only what the UI SHOWS: the 🖼 Background
# selector and the map's own layer control both read this list, so the two
# can never drift apart and offer different sets.
#
# Widen it by raising the slice, or name them explicitly if the order of
# BASEMAPS ever changes for another reason.
_BASEMAPS_SHOWN = list(BASEMAPS.keys())[:4]



# ── Region auto-zoom helper ─────────────────────────────────────────
# When a Petroleum or State Region is selected, navigate the map to
# its center. Both registries now store centers inline as the 3rd
# tuple element: (state_code, [counties], (lat, lon, zoom)). Old
# 2-tuple entries fall through to STATE_CENTERS for backward compat.
STATE_CENTERS = {
    "AL": (32.8, -86.8, 7),  "AK": (64.0, -150.0, 4),
    "AZ": (34.3, -111.7, 7), "AR": (34.9, -92.4, 7),
    "CA": (37.0, -119.5, 6), "CO": (39.0, -105.5, 7),
    "CT": (41.6, -72.7, 8),  "DE": (38.9, -75.5, 8),
    "FL": (28.5, -82.5, 6),  "GA": (32.7, -83.4, 7),
    "HI": (20.5, -157.0, 7), "ID": (44.4, -114.5, 6),
    "IL": (40.0, -89.2, 6),  "IN": (39.9, -86.3, 7),
    "IA": (42.1, -93.5, 7),  "KS": (38.5, -98.5, 7),
    "KY": (37.8, -84.7, 7),  "LA": (31.0, -92.0, 7),
    "ME": (45.4, -69.2, 7),  "MD": (39.0, -76.7, 7),
    "MA": (42.3, -71.8, 8),  "MI": (44.3, -85.4, 6),
    "MN": (46.3, -94.3, 6),  "MS": (32.8, -89.7, 7),
    "MO": (38.4, -92.5, 7),  "MT": (47.1, -109.6, 6),
    "NE": (41.5, -99.8, 7),  "NV": (39.5, -116.9, 6),
    "NH": (43.7, -71.6, 7),  "NJ": (40.2, -74.5, 8),
    "NM": (34.5, -106.1, 7), "NY": (42.9, -75.5, 7),
    "NC": (35.6, -79.4, 7),  "ND": (47.5, -100.5, 7),
    "OH": (40.4, -82.7, 7),  "OK": (35.5, -97.5, 7),
    "OR": (44.0, -120.6, 7), "PA": (40.9, -77.8, 7),
    "RI": (41.7, -71.5, 9),  "SC": (33.9, -80.9, 7),
    "SD": (44.4, -100.2, 7), "TN": (35.9, -86.7, 7),
    "TX": (31.0, -100.0, 6), "UT": (39.3, -111.7, 6),
    "VT": (44.1, -72.7, 7),  "VA": (37.5, -78.9, 7),
    "WA": (47.4, -120.4, 6), "WV": (38.6, -80.6, 7),
    "WI": (44.3, -89.6, 7),  "WY": (43.0, -107.6, 6),
}


def _region_zoom_target(region_label: str, region_value: tuple
                          ) -> tuple[float, float, int] | None:
    """Look up (lat, lon, zoom) for auto-zooming to a region.

    region_value is the registry entry — either:
      - 2-tuple: (state, counties) — legacy, no inline center
      - 3-tuple: (state, counties, center) — new format
                  where center is (lat, lon, zoom) or None

    Priority:
      1. Inline center from the 3-tuple if present and non-None
      2. STATE_CENTERS fallback using the region's state_code
      3. None — caller skips the auto-zoom

    Returns None for the "— none —" sentinel or unknown entries."""
    if not region_label or region_label == "— none —":
        return None
    if not region_value:
        return None
    # Length-agnostic unpacking
    state = region_value[0] if len(region_value) >= 1 else None
    center = region_value[2] if len(region_value) >= 3 else None
    # 3-tuple with inline center wins
    if center is not None:
        return center
    # Fall back to state centroid
    if state and state in STATE_CENTERS:
        return STATE_CENTERS[state]
    return None


DB_LAYERS = [
    {"id": "db_trajectories",   "name": "Well Trajectories",  "icon": "📐", "default": False, "order": 2},
    {"id": "db_sticks",          "name": "Surface→TD sticks",  "icon": "➖", "default": False, "order": 2},
    {"id": "db_formation_tops", "name": "Formation Tops",      "icon": "📏", "default": False, "order": 3},
    {"id": "db_dst",            "name": "DST Intervals",       "icon": "🧪", "default": False, "order": 4},
    {"id": "db_production",     "name": "Production Bubbles",  "icon": "📈", "default": False, "order": 5},
    {"id": "db_production_heat","name": "Production Heatmap",   "icon": "🔥", "default": False, "order": 5},
    {"id": "db_fields",         "name": "Fields",              "icon": "🌿", "default": False, "order": 6},
    {"id": "db_basins",         "name": "Basins",              "icon": "🏔", "default": False, "order": 7},
    {"id": "db_seismic_3d",     "name": "Seismic 3D Surveys",  "icon": "🟦", "default": False, "order": 8},
    {"id": "db_wells_gom",      "name": "GOM Wells",           "icon": "🛢", "default": True,  "order": 9},
]


# ── Area registry ────────────────────────────────────────────────────────────
# Defines which producing-area selectors appear in the top-bar Area dropdown.
# Each entry binds a label to:
#   id          — internal area identifier used in render dispatching
#   sources     — which schema(s) feed this area. "main" = dataview.dv_well
#                 (centroid via _qry_well_grid); "gom" = dataview_gom.well;
#                 "all" = both. Density now renders via the H3 federation
#                 views; the old per-source rectangular grids are gone.
#   center      — (lat, lon, zoom) used to auto-fit the map when the user
#                 selects this area. The pan-persistence JS yields to
#                 _drawn_bounds, so we set _drawn_bounds on area change to
#                 force the auto-zoom.
#   enabled     — False for placeholders (regions where data isn't loaded
#                 yet). Disabled entries still appear in the dropdown so
#                 the user sees what's coming, but selecting them does
#                 nothing beyond rendering "All regions" fallback.
#   queries     — which Query-dropdown options are valid for this area's
#                 schema. The keys correspond to QUERIES values. dv_well
#                 (main) supports the full set; dataview_gom.well only has
#                 the columns for a subset, so GOM gets a shorter list.
#                 Keeping a broken option OUT of the dropdown is clearer
#                 than showing it and silently returning nothing.
#
# NOTE: the variable is still named AREAS for internal continuity (44+
# references throughout the page), but the user-facing label is "Schema"
# — this dropdown dispatches by DATABASE SCHEMA, not geography. Pure
# region/geographic filtering lives in the Region selector in the left
# control panel (Petroleum Region + State Region, combined). Renaming
# AREAS → SCHEMAS would touch many call sites for no behavioral benefit.
#
# Future: replace the hardcoded list with a dynamic discovery query that
# enumerates dataview_<region> schemas. For tonight, hardcoded is fine.
AREAS = [
    # Default selection — renders nothing. Page opens with just the basemap;
    # user must explicitly pick a schema to load wells. This prevents the
    # grey-out + spinner on first page open from auto-firing the grid
    # aggregation queries.
    {"label": "— Select schema —",  "id": "none",       "sources": [],
     "center": (39.5, -98.35, 4),  "enabled": True,
     "queries": ["all", "uwi"]},
    {"label": "🗂 dataview",         "id": "main",       "sources": ["main"],
     "center": (32.0, -100.0, 5),  "enabled": True,
     "queries": ["all", "uwi", "operator", "well_type", "source", "area",
                 "td_range", "spud_range", "comp_range",
                 "has_docs",
                 "has_tops", "has_prod", "has_dst", "has_survey",
                 "has_core", "has_core_photos", "has_petro"]},
    {"label": "🌊 dataview_gom",     "id": "gom",        "sources": ["gom"],
     "center": (27.5, -90.0, 6),   "enabled": True,
     # dataview_gom.well has operator (company_name) and well type
     # (type_code) columns. It does NOT have field/county or the
     # aux-table joins (formation tops, production, DST, etc.) that the
     # "has_*" queries depend on — those tables don't exist for GOM yet.
     "queries": ["all", "uwi", "operator", "well_type",
                 "td_range", "spud_range", "comp_range"]},
    {"label": "🌎 All schemas",     "id": "all",        "sources": ["main", "gom"],
     "center": (39.5, -98.35, 4),  "enabled": True,
     "queries": ["all", "uwi", "operator", "well_type", "source", "area",
                 "td_range", "spud_range", "comp_range",
                 "has_docs",
                 "has_tops", "has_prod", "has_dst", "has_survey",
                 "has_core", "has_core_photos", "has_petro"]},
]


# Module-level flag tracking whether run() has been called in this Streamlit
# process. Set to True on first entry, stays True until the process restarts.
# Used to force-reset the Area selector widget to "— Select area —" on every
# fresh Streamlit start, even if browser session state somehow persists the
# previous selection. Streamlit's session_state can survive browser
# close/reopen in some configurations, so we need a marker that ONLY
# survives within a single Python process lifetime — and module-level
# globals are exactly that.
_PROCESS_FIRST_RUN_DONE = False


# =============================================================================
# DATA QUERIES
# =============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def _qry_wells(_engine, _v=4) -> list[dict]:  # bump _v to bust cache
    """
    Returns wells as a list of dicts via FOR JSON PATH.
    SQL Server does the joins and JSON serialization — no pandas, no Python loops.

    This is the dv_well (main / West Texas) variant. For GOM, see
    _qry_gom_wells — the Wells-mode loader dispatches on active_area.
    """
    sql = """
        SELECT w.uwi, w.well_name, w.well_type, w.well_status,
               w.surface_latitude  AS lat,
               w.surface_longitude AS lon,
               w.county, w.province_state, w.country, w.api_num,
               w.source,
               CONVERT(VARCHAR(10), w.spud_date,       120) AS spud_date,
               CONVERT(VARCHAR(10), w.completion_date, 120) AS completion_date,
               w.final_td, w.depth_datum,
               w.operator_ba_id, w.field_id,
               ISNULL(ba.ba_name, 'Unknown') AS operator_name,
               ISNULL(f.field_name,  'Unknown') AS field_name,
               ISNULL(f.basin_name,  'Unknown') AS basin_name,
               w.area,
               w.protraction_area
        FROM dataview.dv_well w
        LEFT JOIN dataview.dv_business_associate ba ON ba.ba_id = w.operator_ba_id
        LEFT JOIN dataview.dv_field f ON f.field_id = w.field_id
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
        ORDER BY w.well_name
        FOR JSON PATH
    """
    try:
        with _engine.connect().execution_options(timeout=30) as con:
            # FOR JSON PATH returns multiple varchar chunks — concatenate them
            rows = con.execute(text(sql)).fetchall()
            if not rows:
                return []
            json_str = "".join(r[0] for r in rows)
            return json.loads(json_str)
    except Exception as exc:
        st.error(f"Wells query failed: {exc}")
        return []


def _qry_gom_wells(_engine) -> list[dict]:
    """
    GOM (dataview_gom.well) variant of _qry_wells.

    Returns the same dict shape as _qry_wells so all downstream code —
    the wells_df DataFrame, the operator/well_type filter dropdowns,
    the map markers — works unchanged. GOM columns are aliased to the
    dv_well names the rest of the page expects:

        well_id          → uwi          (GOM PK, a uniqueidentifier)
        company_name     → operator_name
        type_code        → well_type
        status_code      → well_status
        bh_total_md_ft   → final_td
        api_well_number  → api_num

    GOM has no field/county, so field_name is set to the area code as a
    reasonable stand-in and county/province_state come back blank.
    Field and county filtering live in Zoom-to (place-based filtering),
    not Query — so GOM mode doesn't lose any user-facing capability by
    not exposing those columns through the Query dropdown.
    """
    sql = """
        SELECT CONVERT(VARCHAR(36), w.well_id) AS uwi,
               w.well_name,
               ISNULL(w.type_code,   'Unknown') AS well_type,
               ISNULL(w.status_code, 'Unknown') AS well_status,
               w.surface_latitude  AS lat,
               w.surface_longitude AS lon,
               CAST('' AS NVARCHAR(40))  AS county,
               w.region                 AS province_state,
               w.api_well_number         AS api_num,
               CONVERT(VARCHAR(10), w.spud_date,        120) AS spud_date,
               CONVERT(VARCHAR(10), w.total_depth_date, 120) AS completion_date,
               w.bh_total_md_ft          AS final_td,
               w.rkb_ft                  AS depth_datum,
               CAST(NULL AS INT)         AS operator_ba_id,
               CAST(NULL AS INT)         AS field_id,
               ISNULL(w.company_name, 'Unknown')      AS operator_name,
               ISNULL(w.bottom_area_code, 'Unknown')  AS field_name
        FROM dataview_gom.well w
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
        ORDER BY w.well_name
        FOR JSON PATH
    """
    try:
        with _engine.connect().execution_options(timeout=30) as con:
            rows = con.execute(text(sql)).fetchall()
            if not rows:
                return []
            json_str = "".join(r[0] for r in rows)
            return json.loads(json_str)
    except Exception as exc:
        st.error(f"GOM wells query failed: {exc}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# BCP-bypass wells loaders (Session 4)
# ═══════════════════════════════════════════════════════════════════════════
# Drop-in replacements for _qry_wells / _qry_gom_wells that bypass pyodbc
# transport entirely. Measured 60-100x faster than the FOR JSON PATH pattern
# the originals use (see transport_diagnostic.md, 2026-05-26).
#
#   _qry_wells_bcp     : dataview.dv_well  — 477K wells in ~12s (was 41-min hang)
#   _qry_gom_wells_bcp : dataview_gom.well —  55K wells in ~1s  (was 60+ sec)
#
# Pipeline:
#   1. Issue SELECT to SQL Server, BCP streams to CSV (1-10s for 477K)
#   2. Python parses CSV into typed dicts (no pyodbc anywhere)
#   3. Return list[dict] matching the original function's shape exactly
#
# Newline handling: every text column gets wrapped in REPLACE for CHAR(13)
# and CHAR(10). Without this, a well_name like "ABC \r\nXYZ" creates a
# fake new row in the CSV and the page renders garbage. The transport
# diagnostic caught this — bcp_csv on Main wide returned 523K rows when
# the actual table has 477K, because ~10% of well names had embedded newlines.
# ═══════════════════════════════════════════════════════════════════════════

# Columns returned by _qry_wells (in order). Used to drive the CSV header
# (since BCP -c emits no header) and to coerce CSV strings into typed dicts.
_BCP_MAIN_COLUMNS = [
    ("uwi",             "text"),
    ("well_name",       "text"),
    ("well_type",       "text"),
    ("well_status",     "text"),
    ("lat",             "float"),
    ("lon",             "float"),
    ("county",          "text"),
    ("province_state",  "text"),
    ("country",         "text"),
    ("api_num",         "text"),
    ("source",          "text"),
    ("spud_date",       "text"),     # already 'YYYY-MM-DD' string in source
    ("completion_date", "text"),
    ("final_td",        "float"),
    ("depth_datum",     "float"),
    ("operator_ba_id",  "int"),
    ("field_id",        "int"),
    ("operator_name",   "text"),
    ("field_name",      "text"),
    ("basin_name",      "text"),
    ("area",            "text"),
    ("protraction_area","text"),
]

# Columns for _qry_gom_wells. Note no country/source/basin/area/protraction.
_BCP_GOM_COLUMNS = [
    ("uwi",             "text"),
    ("well_name",       "text"),
    ("well_type",       "text"),
    ("well_status",     "text"),
    ("lat",             "float"),
    ("lon",             "float"),
    ("county",          "text"),     # always blank in GoM
    ("province_state",  "text"),
    ("api_num",         "text"),
    ("spud_date",       "text"),
    ("completion_date", "text"),
    ("final_td",        "float"),
    ("depth_datum",     "float"),
    ("operator_ba_id",  "int"),      # always NULL
    ("field_id",        "int"),      # always NULL
    ("operator_name",   "text"),
    ("field_name",      "text"),
]


def _bcp_safe(col_expr: str) -> str:
    """
    Wrap a text-typed SQL expression so embedded CR/LF/pipes are stripped.

    Why: BCP character mode (-c) is line-oriented. Any embedded newline in
    a text column splits the value across CSV rows. The transport diagnostic
    confirmed ~10% of dv_well names have embedded \\r\\n. The fix is to
    sanitize server-side BEFORE the bytes leave SQL Server.

    Also strips literal pipes since we use | as the field delimiter. Less
    common in well data but cheap to guard against.
    """
    return (
        f"REPLACE(REPLACE(REPLACE("
        f"{col_expr}, "
        f"CHAR(13), ' '), "
        f"CHAR(10), ' '), "
        f"'|', ' ')"
    )


def _bcp_fetch_to_csv(sql: str, out_path: "Path") -> int:
    """
    Run BCP OUT to write the query result to out_path.

    Returns row count from BCP's "N rows copied" message; raises on any
    BCP exit code != 0.

    Uses:
        -c       character mode (text format, line-per-row)
        -t|      field separator: pipe (well-data-safe with _bcp_safe wrap)
        -C 65001 UTF-8 codepage (avoids the encoding fault we hit in diag)
        -T       trusted (Windows) auth — matches SSMS
        -q       quoted identifiers (needed for some join SQL shapes)
    """
    # Collapse multi-line SQL to one line; BCP queryout doesn't accept newlines.
    one_line = " ".join(sql.split())
    cmd = [
        "bcp", one_line, "queryout", str(out_path),
        "-c", "-t|", "-C", "65001",
        "-T", f"-S{BCP_SERVER}",
        f"-d{st.session_state.get('wm_map_db', BCP_DATABASE)}", "-q",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        # SAY ENOUGH TO DIAGNOSE IT. The old message was just bcp's stderr,
        # and bcp can exit non-zero having printed NOTHING at all — which
        # produced "BCP queryout failed:" with nothing after the colon and
        # no way to tell whether the database, the auth, the query or the
        # command line was at fault. An error that cannot be acted on is
        # only slightly better than a silent failure.
        _db = st.session_state.get("wm_map_db", BCP_DATABASE)
        err_msg = (result.stderr or result.stdout or "(no output)").strip()[:300]
        raise RuntimeError(
            f"BCP queryout failed (exit {result.returncode}) · "
            f"server {BCP_SERVER} · database {_db} · "
            f"{len(one_line)} chars of SQL · {err_msg} · "
            f"SQL: {one_line[:200]}")

    # Parse "N rows copied." from BCP stdout
    rows_copied = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.endswith("rows copied.") or line.endswith("row copied."):
            try:
                rows_copied = int(line.split()[0].replace(",", ""))
            except (ValueError, IndexError):
                pass
            break
    return rows_copied


def _parse_bcp_csv(
    csv_path: "Path",
    columns: list[tuple[str, str]],
) -> list[dict]:
    """
    Parse a BCP-produced CSV (no header) into list of typed dicts.

    BCP writes NULL as the literal string "NULL". Map that to None.
    Empty strings stay as ''. Coerce numeric columns per the column type.
    """
    out: list[dict] = []
    n_cols = len(columns)
    with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            # Pad/truncate to expected width — BCP is consistent but defensive
            if len(row) < n_cols:
                row = row + [""] * (n_cols - len(row))
            elif len(row) > n_cols:
                row = row[:n_cols]

            d: dict = {}
            for (name, kind), raw in zip(columns, row):
                # Treat empty, "NULL", and lone NUL bytes as Python None.
                # NUL appears when SQL Server emits an empty NVARCHAR via
                # BCP -c — usually from CAST('' AS NVARCHAR(N)). Stripping
                # it here makes the parser robust regardless of which
                # SELECT shape produced the file.
                if raw in ("", "NULL", "\x00"):
                    d[name] = None
                elif kind == "float":
                    try:
                        d[name] = float(raw)
                    except (TypeError, ValueError):
                        d[name] = None
                elif kind == "int":
                    try:
                        d[name] = int(raw)
                    except (TypeError, ValueError):
                        d[name] = None
                else:  # text
                    d[name] = raw
            out.append(d)
    return out


@st.cache_data(ttl=600, show_spinner=False)
def _qry_wells_bcp(_engine, _v: int = 1, limit: int = 10000,
                   center_lat: float | None = None,
                   center_lon: float | None = None,
                   where_extra: str = "") -> list[dict]:
    """
    BCP-backed drop-in replacement for _qry_wells.

    Returns the same shape (list[dict] with 22 keys) as _qry_wells.
    Bump _v to bust the @st.cache_data cache after a schema change.

    `limit` caps the number of wells returned (default 10,000). This is a
    guardrail so the loader never tries to pull and render the full table
    (500K+ wells); the cap lives in user_prefs.json (no longer a UI slider).

    `center_lat`/`center_lon`: when provided, results are ordered by squared
    distance from that point ascending, so TOP (limit) returns the wells
    NEAREST the map center — i.e. "load what I'm looking at, outward."
    Squared Euclidean (not Haversine) is used because we only need relative
    order, and over a viewport-sized area the lat/lon distortion doesn't
    change the nearest-N set meaningfully. When center is None, falls back
    to ORDER BY uwi (stable but geographically arbitrary).

    limit<=0 means no cap. The limit AND center participate in the cache
    key, so changing the slider or recentering re-queries.

    Timing on 477K wells (uncapped): ~12s wall. With the 10K cap, far
    faster — only 10K rows cross the wire.
    """
    # Operator and field name use a three-tier fallback:
    #   1. w.operator_name / w.field_name (denormalized; populated for KGS
    #      and other sources that don't use ba_id/field_id)
    #   2. ba.ba_name / f.field_name (the FK lookup; populated for MI, WY)
    #   3. 'Unknown' literal
    #
    # _bcp_safe still wraps these because the audit (2026-05-26) found
    # embedded CR/LF in the OLD KGS_GEOJSON data via the ba/f columns. The
    # new KGS data (loaded 2026-05-27) is clean at source, but defense-in-
    # depth is cheap and protects future loads. Every other column tested
    # clean and is passed through raw, saving ~10M SQL Server REPLACE calls
    # and ~20s on cold load.
    # TOP clause for the well-count guardrail. limit<=0 -> no cap.
    _top_clause = f"TOP ({int(limit)}) " if limit and limit > 0 else ""

    # Ordering: nearest-to-center if a center is given, else by uwi.
    # Squared Euclidean distance — no SQRT needed for ordering. Inline the
    # center literals (BCP can't bind params); they're floats we control.
    if center_lat is not None and center_lon is not None:
        _order_clause = (
            f"ORDER BY (POWER(w.surface_latitude  - ({center_lat:.6f}), 2) "
            f"+ POWER(w.surface_longitude - ({center_lon:.6f}), 2))"
        )
    else:
        _order_clause = "ORDER BY w.uwi"

    sql = f"""
        SELECT {_top_clause}
            w.uwi,
            w.well_name,
            w.well_type,
            w.well_status,
            w.surface_latitude  AS lat,
            w.surface_longitude AS lon,
            w.county,
            w.province_state,
            w.country,
            w.api_num,
            w.source,
            CONVERT(VARCHAR(10), w.spud_date,       120) AS spud_date,
            CONVERT(VARCHAR(10), w.completion_date, 120) AS completion_date,
            w.final_td,
            w.depth_datum,
            w.operator_ba_id,
            w.field_id,
            {_bcp_safe("COALESCE(w.operator_name, ba.ba_name, 'Unknown')")} AS operator_name,
            {_bcp_safe("COALESCE(w.field_name,    f.field_name,  'Unknown')")} AS field_name,
            ISNULL(f.basin_name,  'Unknown') AS basin_name,
            w.area,
            w.protraction_area
        FROM dataview.dv_well w
        LEFT JOIN dataview.dv_business_associate ba ON ba.ba_id = w.operator_ba_id
        LEFT JOIN dataview.dv_field f ON f.field_id = w.field_id
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
          {where_extra}
        {_order_clause}
    """

    # Working dir for the CSV. Use Temp so Windows cleans it up automatically
    # if our script crashes.
    work_dir = Path(os.environ.get("LOCALAPPDATA", "/tmp")) / "Temp" / "dw_wells_bcp"
    work_dir.mkdir(parents=True, exist_ok=True)
    csv_path = work_dir / "wells_main.csv"

    try:
        _bcp_fetch_to_csv(sql, csv_path)
        return _parse_bcp_csv(csv_path, _BCP_MAIN_COLUMNS)
    except Exception as exc:
        st.error(f"BCP wells query failed: {exc}")
        return []
    finally:
        try:
            csv_path.unlink()
        except FileNotFoundError:
            pass


@st.cache_data(ttl=600, show_spinner=False)
def _qry_well_count_near(_engine, center_lat: float, center_lon: float,
                         radius_mi: float = 50.0, _v: int = 1) -> int:
    """
    Count wells within `radius_mi` miles of (center_lat, center_lon).

    Used for the "X wells within Y mi of center" message so the user knows
    how many wells exist near where they're looking vs. how many the limit
    actually loaded.

    Uses a bounding-box prefilter (cheap, index-friendly) combined with a
    squared-distance check in degrees. We approximate 1 degree latitude as
    ~69 miles and 1 degree longitude as ~69*cos(lat) miles. This is plenty
    accurate for a "how many wells are near here" readout — we're not
    navigating with it.

    Returns a plain int. Goes through pyodbc (not BCP) because it's a single
    scalar — no transport bottleneck for one number, and BCP overhead would
    dominate. Cached so repeated renders at the same center are free.
    """
    import math
    # Degrees of lat/lon that correspond to radius_mi at this latitude.
    _dlat = radius_mi / 69.0
    _coslat = max(0.01, math.cos(math.radians(center_lat)))  # avoid /0 at poles
    _dlon = radius_mi / (69.0 * _coslat)

    _lat_min = center_lat - _dlat
    _lat_max = center_lat + _dlat
    _lon_min = center_lon - _dlon
    _lon_max = center_lon + _dlon

    # Bounding-box count. We could refine to a true circle with a distance
    # term, but the bbox is a fine approximation for a readout and keeps the
    # query sargable (uses the lat/lon range, index-friendly).
    sql = text("""
        SELECT COUNT(*) AS n
        FROM dataview.dv_well w
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
          AND w.surface_latitude  BETWEEN :lat_min AND :lat_max
          AND w.surface_longitude BETWEEN :lon_min AND :lon_max
    """)
    try:
        with _engine.connect() as con:
            row = con.execute(sql, {
                "lat_min": _lat_min, "lat_max": _lat_max,
                "lon_min": _lon_min, "lon_max": _lon_max,
            }).fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:
        print(f"[well_count_near] failed: {exc}")
        return -1  # sentinel — caller shows "unknown" rather than a wrong 0


@st.cache_data(ttl=600, show_spinner=False)
def _qry_distinct_attr(_engine, expr: str, where_spatial: str = "",
                       _v: int = 1) -> list:
    """DISTINCT values of an attribute, for a Query dropdown.

    WHY NOT just read wells_df: those options were built from the wells that
    survived the CURRENT filter, so picking an operator narrowed the loaded
    wells to that operator, which narrowed the dropdown to that one value —
    and there was no way back to any other. The list has to come from a source
    the attribute filter does not touch.

    Scoped by the SPATIAL clause (area / bbox) but NOT by the attribute one,
    so the options stay relevant to where you are looking while still offering
    every value there. Cached, because it runs on every rerun of the page.
    """
    _w = (where_spatial or "").strip()
    if _w[:4].upper() == "AND ":
        _w = _w[4:].strip()
    _clause = f" AND ({_w})" if _w else ""
    sql = text(
        f"SELECT DISTINCT {expr} AS v FROM dataview.dv_well w "
        "LEFT JOIN dataview.dv_business_associate ba "
        "ON ba.ba_id = w.operator_ba_id "
        "WHERE w.surface_latitude IS NOT NULL "
        "AND w.surface_longitude IS NOT NULL" + _clause + " ORDER BY 1")
    try:
        with _engine.connect() as con:
            return [r[0] for r in con.execute(sql).fetchall() if r[0] is not None]
    except Exception as exc:
        print(f"[distinct_attr] failed: {exc}")
        return []


@st.cache_data(ttl=600, show_spinner=False)
def _qry_wells_scope_count(_engine, where_extra: str = "", _v: int = 1) -> int:
    """Count wells for the current scope (optional WHERE fragment, same one
    _qry_wells uses). Cheap cached scalar COUNT so the 'plot Wells if small'
    check is nearly free. The page's _qry_where starts with 'AND ' — stripped so
    we can wrap it. Returns -1 on error (caller then treats scope as large)."""
    _w = (where_extra or "").strip()
    if _w[:4].upper() == "AND ":
        _w = _w[4:].strip()
    _clause = f" AND ({_w})" if _w else ""
    # The ba join is here ONLY so this query can resolve the same WHERE
    # fragment _qry_wells uses. That fragment may reference ba.ba_name (the
    # operator filter does), and without the join SQL Server rejects the whole
    # statement on an unknown alias — this function then returned its -1
    # sentinel, the caller treated the scope as unloadable, and the map came
    # back empty. A LEFT JOIN on a FK adds nothing to the count.
    sql = text(
        "SELECT COUNT(*) FROM dataview.dv_well w "
        "LEFT JOIN dataview.dv_business_associate ba "
        "ON ba.ba_id = w.operator_ba_id "
        "WHERE w.surface_latitude IS NOT NULL "
        "AND w.surface_longitude IS NOT NULL" + _clause)
    try:
        with _engine.connect() as con:
            row = con.execute(sql).fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:
        print(f"[wells_scope_count] failed: {exc}")
        return -1


@st.cache_data(ttl=600, show_spinner=False)
def _qry_gom_wells_bcp(_engine, _v: int = 1) -> list[dict]:
    """
    BCP-backed drop-in replacement for _qry_gom_wells.

    Returns same shape (list[dict] with 17 keys) as _qry_gom_wells.

    Timing on 55K wells: ~1s wall. Compare to _qry_gom_wells at 60+ sec.
    """
    # Free-text safety wraps only on the two columns that actually need
    # them in BOEM data (well_name, company_name). Codes (type_code/
    # status_code/region/api_well_number/bottom_area_code) pass through raw.
    # county is always NULL for GoM — using CAST(NULL AS NVARCHAR(40))
    # instead of CAST('' AS NVARCHAR(40)) avoids BCP -c emitting a lone
    # NUL byte that would otherwise need scrubbing in the parser.
    sql = f"""
        SELECT
            CONVERT(VARCHAR(36), w.well_id) AS uwi,
            {_bcp_safe('w.well_name')},
            ISNULL(w.type_code,   'Unknown') AS well_type,
            ISNULL(w.status_code, 'Unknown') AS well_status,
            w.surface_latitude  AS lat,
            w.surface_longitude AS lon,
            CAST(NULL AS NVARCHAR(40)) AS county,
            w.region,
            w.api_well_number,
            CONVERT(VARCHAR(10), w.spud_date,        120) AS spud_date,
            CONVERT(VARCHAR(10), w.total_depth_date, 120) AS completion_date,
            w.bh_total_md_ft,
            w.rkb_ft,
            CAST(NULL AS INT) AS operator_ba_id,
            CAST(NULL AS INT) AS field_id,
            {_bcp_safe("ISNULL(w.company_name, 'Unknown')")},
            ISNULL(w.bottom_area_code, 'Unknown') AS field_name
        FROM dataview_gom.well w
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
    """

    work_dir = Path(os.environ.get("LOCALAPPDATA", "/tmp")) / "Temp" / "dw_wells_bcp"
    work_dir.mkdir(parents=True, exist_ok=True)
    csv_path = work_dir / "wells_gom.csv"

    try:
        _bcp_fetch_to_csv(sql, csv_path)
        return _parse_bcp_csv(csv_path, _BCP_GOM_COLUMNS)
    except Exception as exc:
        st.error(f"BCP GoM wells query failed: {exc}")
        return []
    finally:
        try:
            csv_path.unlink()
        except FileNotFoundError:
            pass


@st.cache_data(ttl=3600, show_spinner=False)
def _qry_gom_status_codes(_engine) -> list[str]:
    """
    Distinct status_code values present in dataview_gom.well, ordered by
    well count descending. Cheap — it's a GROUP BY on one indexed column,
    no row payload — so the status sidebar can populate from the real
    schema without paying the cost of loading the full wells list.

    Returns a list of raw BOEM status codes, e.g. ["PA","ST","COM",...].
    Falls back to an empty list on error; the caller handles that.
    """
    try:
        with _engine.connect().execution_options(timeout=8) as con:
            rows = con.execute(text("""
                SELECT status_code, COUNT(*) AS n
                FROM dataview_gom.well
                WHERE status_code IS NOT NULL
                  AND LTRIM(RTRIM(status_code)) <> ''
                GROUP BY status_code
                ORDER BY COUNT(*) DESC
            """)).fetchall()
            return [str(r[0]).strip() for r in rows]
    except Exception:
        return []


    """Sub-data counts per well — cached, with timeout."""
    try:
        with _engine.connect().execution_options(timeout=10) as con:
            return pd.read_sql(text("""
                SELECT w.uwi,
                    ISNULL(t.cnt,  0) top_count,
                    ISNULL(l.cnt,  0) log_count,
                    ISNULL(c.cnt,  0) core_count,
                    ISNULL(d.cnt,  0) dst_count,
                    ISNULL(co.cnt, 0) comp_count,
                    ISNULL(pi.cnt, 0) petro_count,
                    ISNULL(pe.cnt, 0) prod_count
                FROM dataview.dv_well w
                LEFT JOIN (SELECT uwi, COUNT(*) cnt FROM dataview.dv_well_formation_top GROUP BY uwi) t  ON t.uwi  = w.uwi
                LEFT JOIN (SELECT uwi, COUNT(*) cnt FROM dataview.dv_well_log            GROUP BY uwi) l  ON l.uwi  = w.uwi
                LEFT JOIN (SELECT uwi, COUNT(*) cnt FROM dataview.dv_well_core           GROUP BY uwi) c  ON c.uwi  = w.uwi
                LEFT JOIN (SELECT uwi, COUNT(*) cnt FROM dataview.dv_well_dst            GROUP BY uwi) d  ON d.uwi  = w.uwi
                LEFT JOIN (SELECT uwi, COUNT(*) cnt FROM dataview.dv_well_completion     GROUP BY uwi) co ON co.uwi = w.uwi
                LEFT JOIN (SELECT uwi, COUNT(*) cnt FROM dataview.dv_well_petro_interp   GROUP BY uwi) pi ON pi.uwi = w.uwi
                LEFT JOIN (SELECT uwi, COUNT(*) cnt FROM dataview.dv_prod_entity         GROUP BY uwi) pe ON pe.uwi = w.uwi
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def _qry_well_documents(_engine) -> pd.DataFrame:
    """Documented wells with a coordinate, from the materialized table if it
    exists, else the live view. One row per UWI we hold documents for."""
    sql = ("SELECT uwi, lat, lon, well_name, coord_source, doc_count, "
           "pdf_count, log_count, seismic_count, office_count, gis_count, "
           "doc_types FROM {src}")
    for src in ("dataview.well_documents", "dataview.v_well_documents"):
        try:
            with _engine.connect() as con:
                return pd.read_sql(text(sql.format(src=src)), con)
        except Exception:
            continue
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _qry_formation_tops(_engine) -> pd.DataFrame:
    try:
        with _engine.connect() as con:
            return pd.read_sql(text("""
                SELECT t.uwi, t.strat_unit_name formation, t.top_depth, t.base_depth,
                       t.fluid_type, t.net_thickness,
                       w.surface_latitude lat, w.surface_longitude lon, w.well_name
                FROM dataview.dv_well_formation_top t
                JOIN dataview.dv_well w ON w.uwi = t.uwi
                WHERE w.surface_latitude IS NOT NULL
                ORDER BY t.uwi, t.top_depth
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _qry_dst(_engine) -> pd.DataFrame:
    try:
        with _engine.connect() as con:
            return pd.read_sql(text("""
                SELECT d.uwi, d.test_type, d.top_depth, d.base_depth,
                       d.test_result, d.max_oil_rate, d.max_gas_rate,
                       d.api_gravity, d.test_date,
                       w.surface_latitude lat, w.surface_longitude lon, w.well_name
                FROM dataview.dv_well_dst d
                JOIN dataview.dv_well w ON w.uwi = d.uwi
                WHERE w.surface_latitude IS NOT NULL
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
@st.cache_data(ttl=600, show_spinner=False)
def _qry_production(_engine) -> pd.DataFrame:
    try:
        with _engine.connect() as con:
            return pd.read_sql(text("""
                SELECT w.uwi, w.well_name,
                       w.surface_latitude lat, w.surface_longitude lon,
                       SUM(CASE WHEN pv.fluid_type='OIL'   THEN ISNULL(pv.volume,0) ELSE 0 END) cum_oil,
                       SUM(CASE WHEN pv.fluid_type='GAS'   THEN ISNULL(pv.volume,0) ELSE 0 END) cum_gas,
                       SUM(CASE WHEN pv.fluid_type='WATER' THEN ISNULL(pv.volume,0) ELSE 0 END) cum_water,
                       COUNT(DISTINCT pv.period_date) months
                FROM dataview.dv_well w
                JOIN dataview.dv_prod_entity pe ON pe.uwi = w.uwi
                JOIN dataview.dv_prod_volume pv ON pv.prod_entity_id = pe.prod_entity_id
                WHERE w.surface_latitude IS NOT NULL
                GROUP BY w.uwi, w.well_name, w.surface_latitude, w.surface_longitude
            """), con)
    except Exception:
        return pd.DataFrame()


def _chunk(seq, n=900):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


@st.cache_data(ttl=600, show_spinner=False)
def _uwis_with_survey(_engine, uwis: tuple) -> set:
    """
    Subset of the GIVEN uwis that have a directional survey. Bounded by the
    input list (indexed IN-list lookups), so it stays fast no matter how big the
    survey tables are. Onshore (PPDM) uwis hit dv_well_dir_srvy_hdr; UUID-shaped
    ids (GOM well_id) hit directional_survey_point. Cached on the uwi set.
    """
    if not uwis:
        return set()
    out: set = set()
    _onshore = [str(u) for u in uwis
                if not (len(str(u)) == 36 and str(u).count("-") == 4)]
    _gom = [str(u) for u in uwis
            if (len(str(u)) == 36 and str(u).count("-") == 4)]
    try:
        with _engine.connect().execution_options(timeout=20) as con:
            for ch in _chunk(_onshore):
                _p = {f"u{j}": v for j, v in enumerate(ch)}
                _in = ",".join(f":{k}" for k in _p)
                for r in con.execute(text(
                        f"SELECT DISTINCT uwi FROM dataview.dv_well_dir_srvy_hdr "
                        f"WHERE uwi IN ({_in})"), _p):
                    out.add(str(r[0]))
            for ch in _chunk(_gom):
                _p = {f"u{j}": v for j, v in enumerate(ch)}
                _in = ",".join(f":{k}" for k in _p)
                # well_id is a uniqueidentifier; SQL converts the string
                # literals and uses the index (seek, not a 4.9M-row scan).
                for r in con.execute(text(
                        f"SELECT DISTINCT CONVERT(VARCHAR(36), well_id) "
                        f"FROM dataview_gom.directional_survey_point "
                        f"WHERE well_id IN ({_in})"), _p):
                    out.add(str(r[0]))
    except Exception:
        pass
    return out


@st.cache_data(ttl=600, show_spinner=False)
def _uwi_cum_prod(_engine, uwis: tuple) -> dict:
    """
    uwi → (cum_oil_bbl, cum_gas_mcf) from the onshore production tables, for the
    GIVEN wells only. Filtering by w.uwi IN (...) keeps this an indexed lookup
    instead of a full GROUP BY over the entire 77M-row volume table. Wells with
    no production simply aren't in the dict. Cached on the uwi set.
    """
    if not uwis:
        return {}
    out: dict = {}
    _onshore = [str(u) for u in uwis
                if not (len(str(u)) == 36 and str(u).count("-") == 4)]
    if not _onshore:
        return {}
    try:
        with _engine.connect().execution_options(timeout=20) as con:
            for ch in _chunk(_onshore):
                _p = {f"u{j}": v for j, v in enumerate(ch)}
                _in = ",".join(f":{k}" for k in _p)
                for r in con.execute(text(f"""
                    SELECT w.uwi,
                           SUM(CASE WHEN pv.fluid_type='OIL' THEN ISNULL(pv.volume,0) ELSE 0 END) oil,
                           SUM(CASE WHEN pv.fluid_type='GAS' THEN ISNULL(pv.volume,0) ELSE 0 END) gas
                    FROM dataview.dv_well w
                    JOIN dataview.dv_prod_entity pe ON pe.uwi = w.uwi
                    JOIN dataview.dv_prod_volume pv ON pv.prod_entity_id = pe.prod_entity_id
                    WHERE w.uwi IN ({_in})
                    GROUP BY w.uwi
                """), _p):
                    out[str(r[0])] = (float(r[1] or 0), float(r[2] or 0))
    except Exception:
        pass
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def _qry_fields(_engine) -> pd.DataFrame:
    try:
        with _engine.connect() as con:
            return pd.read_sql(text("""
                SELECT field_id, field_name, field_type, country_code,
                       centroid_latitude lat, centroid_longitude lon
                FROM dataview.dv_field WHERE centroid_latitude IS NOT NULL
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _qry_basins(_engine) -> pd.DataFrame:
    try:
        with _engine.connect() as con:
            return pd.read_sql(text("""
                SELECT basin_id, basin_name, basin_type, country_code,
                       centroid_latitude lat, centroid_longitude lon,
                       area_km2, primary_play_type
                FROM dataview.dv_basin WHERE centroid_latitude IS NOT NULL
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def _qry_seismic_3d(_engine) -> pd.DataFrame:
    """3D seismic surveys with valid bbox geometry, joined to file path.

    Returns one row per 3D survey footprint. We pull from FILE_SEIS_HEADER
    where the bbox columns are populated AND the lat values fall in a
    sane range (sometimes segyio misreads CDP scalars and yields huge
    out-of-range numbers; those got filtered to NULL at write time by
    _safe_coord, but old rows from before that fix may still have garbage).

    Joins to GLOBAL_FILE_CATALOG to surface the filename for the popup.
    """
    try:
        with _engine.connect() as con:
            # BAD_FILE is created on first use, so a database that has
            # never rejected a file does not have it. Naming it
            # unconditionally makes the whole statement fail to compile —
            # and this function returns an empty frame on any exception,
            # so every 3D survey would quietly stop drawing. Probe, then
            # compose.
            _has_bad = con.execute(text(
                "SELECT OBJECT_ID('file_catalog.BAD_FILE')")).scalar() is not None
            _bad = ("AND NOT EXISTS (SELECT 1 FROM file_catalog.BAD_FILE bf"
                    " WHERE bf.INVENTORY_ID = sh.INVENTORY_ID)") if _has_bad else ""
            return pd.read_sql(text(f"""
                SELECT
                    sh.SEIS_HEADER_ID                AS id,
                    sh.SURVEY_NAME                   AS survey_name,
                    sh.LINE_NAME                     AS line_name,
                    sh.CONTRACTOR                    AS contractor,
                    sh.SURVEY_DATE                   AS survey_date,
                    sh.TRACE_COUNT                   AS trace_count,
                    sh.SAMPLE_INTERVAL               AS sample_interval,
                    sh.EPSG_CODE                     AS epsg_code,
                    CAST(sh.BBOX_MIN_LAT AS FLOAT)   AS min_lat,
                    CAST(sh.BBOX_MAX_LAT AS FLOAT)   AS max_lat,
                    CAST(sh.BBOX_MIN_LON AS FLOAT)   AS min_lon,
                    CAST(sh.BBOX_MAX_LON AS FLOAT)   AS max_lon,
                    fc.FILE_NAME                     AS file_name,
                    fc.FILE_PATH                     AS file_path
                FROM file_catalog.FILE_SEIS_HEADER sh
                LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG fc
                    ON fc.INVENTORY_ID = sh.INVENTORY_ID
                WHERE sh.SEIS_SET_TYPE = '3D'
                  -- A REJECTED FILE MUST NOT DRAW. This layer is the one
                  -- seismic path that reads the CATALOG directly rather than
                  -- dv_seis_*, so deleting a bad file's promoted rows leaves
                  -- its rectangle on the map regardless. Both tests are here
                  -- because they are set independently: _mark_bad writes
                  -- BAD_FILE and stamps SKIPPED, but a file can be skipped
                  -- without ever being fingerprinted bad.
                  AND ISNULL(fc.CATALOG_READINESS,'') <> 'SKIPPED'
                  {_bad}
                  AND sh.BBOX_MIN_LAT IS NOT NULL
                  AND sh.BBOX_MAX_LAT IS NOT NULL
                  AND sh.BBOX_MIN_LON IS NOT NULL
                  AND sh.BBOX_MAX_LON IS NOT NULL
                  AND TRY_CAST(sh.BBOX_MIN_LAT AS FLOAT) BETWEEN -90 AND 90
                  AND TRY_CAST(sh.BBOX_MAX_LAT AS FLOAT) BETWEEN -90 AND 90
                  AND TRY_CAST(sh.BBOX_MIN_LON AS FLOAT) BETWEEN -180 AND 180
                  AND TRY_CAST(sh.BBOX_MAX_LON AS FLOAT) BETWEEN -180 AND 180
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=None, show_spinner=False)
def _qry_well_grid(_engine, step: float = 0.035) -> pd.DataFrame:
    """
    Server-side spatial aggregation for the hex/grid overview layer.

    Bins wells into square cells of `step` degrees (default 0.1° ~= 7 miles).
    Returns one row per non-empty cell with the well count and the centroid
    of wells inside it.

    This replaces the 50K-marker payload with ~100-500 polygons. Massive
    serialization win on every Streamlit rerun.

    Columns returned:
        lat_bin    — south edge of the cell (degrees)
        lon_bin    — west edge of the cell (degrees)
        well_count — wells inside the cell
        center_lat — centroid of those wells (for tooltip placement)
        center_lon — centroid of those wells

    Cache TTL is None — the grid only changes when wells are loaded/deleted,
    so we hold it for the whole session.

    Transport: tries BCP first (sub-second on the wire), falls back to
    pyodbc if BCP isn't available or fails. Mirrors the fix used by
    _qry_h3_grid — SQL Server execution is fast (~0.5s) but pyodbc transport
    of the result set balloons to 60+ seconds on 500K-row aggregations.
    """
    # ── BCP queryout SQL ─────────────────────────────────────────────
    # BCP can't bind parameters, so we substitute the step value inline.
    # step is a float we control (page-internal), not user input, so
    # direct substitution is safe — format with high precision to keep
    # the FLOOR boundaries consistent with the parameterized version.
    _step_lit = f"{step:.6f}"
    bcp_sql = f"""
        SELECT
            FLOOR(w.surface_latitude  / {_step_lit}) * {_step_lit} AS lat_bin,
            FLOOR(w.surface_longitude / {_step_lit}) * {_step_lit} AS lon_bin,
            COUNT(*) AS well_count,
            AVG(w.surface_latitude)  AS center_lat,
            AVG(w.surface_longitude) AS center_lon
        FROM dataview.dv_well w
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
        GROUP BY FLOOR(w.surface_latitude  / {_step_lit}),
                 FLOOR(w.surface_longitude / {_step_lit})
    """.strip()

    work_dir = Path(os.environ.get("LOCALAPPDATA", "/tmp")) / "Temp" / "dw_grid_bcp"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        csv_path = work_dir / f"well_grid_{step:.4f}.csv"

        try:
            _bcp_fetch_to_csv(bcp_sql, csv_path)
            rows = _parse_bcp_csv(csv_path, [
                ("lat_bin",    "float"),
                ("lon_bin",    "float"),
                ("well_count", "int"),
                ("center_lat", "float"),
                ("center_lon", "float"),
            ])
            try:
                csv_path.unlink()
            except FileNotFoundError:
                pass
            if rows:
                return pd.DataFrame(rows)
            # BCP returned empty — could be legit (no data) or silent
            # failure. Fall through to pyodbc to be sure.
        except Exception as bcp_exc:
            # BCP failed for some reason (binary missing, server unreachable
            # via BCP, etc.). Log and fall through to pyodbc so the page
            # still works (just slowly on this path).
            print(f"[well_grid] BCP path failed: {bcp_exc}; falling back to pyodbc")
            try:
                csv_path.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        # Couldn't even create the working dir. Fall through to pyodbc.
        pass

    # ── Pyodbc fallback (original path) ──────────────────────────────
    sql = """
        DECLARE @step FLOAT = :step;
        SELECT
            FLOOR(w.surface_latitude  / @step) * @step AS lat_bin,
            FLOOR(w.surface_longitude / @step) * @step AS lon_bin,
            COUNT(*) AS well_count,
            AVG(w.surface_latitude)  AS center_lat,
            AVG(w.surface_longitude) AS center_lon
        FROM dataview.dv_well w
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
        GROUP BY FLOOR(w.surface_latitude  / @step),
                 FLOOR(w.surface_longitude / @step)
    """
    try:
        with _engine.connect() as con:
            return pd.read_sql(text(sql), con, params={"step": step})
    except Exception as exc:
        st.error(f"Well grid query failed: {exc}")
        return pd.DataFrame()
@st.cache_data(ttl=600, show_spinner=False)
def _qry_data_extent(_engine) -> list | None:
    """[[min_lat, min_lon], [max_lat, max_lon]] of the loaded wells, padded.

    OPEN WHERE THE DATA IS. The map framed the lower 48 on arrival, which was
    the right call when it was chosen -- the alternative then was a centroid
    zoom onto an EMPTY Teapot, and a tight view of nothing reads as broken.
    With 28,173 wells loaded it is the opposite problem: the whole corpus is a
    speck in Wyoming and the operator has to hunt for it.

    An EXTENT, not a centroid, which is what makes this safe: framing a bbox
    cannot zoom absurdly tight the way centring on a mean can, and if the data
    really is spread across the country the answer is the country anyway.

    Cheap now, and it was not before: MIN/MAX over surface_latitude/longitude
    is a seek on IX_dv_well_lat_lon -- 0.047s measured. Cached for 10 minutes
    on top of that, because the extent only moves when wells are loaded.

    Returns None when there is nothing to frame, so the caller keeps its
    lower-48 fallback rather than fitting a degenerate box.
    """
    try:
        with _engine.connect() as con:
            r = con.execute(text("""
                SELECT MIN(surface_latitude), MAX(surface_latitude),
                       MIN(surface_longitude), MAX(surface_longitude), COUNT(*)
                  FROM dataview.dv_well
                 WHERE surface_latitude IS NOT NULL
                   AND surface_longitude IS NOT NULL""")).one()
    except Exception as exc:
        _say("[map] data extent query failed: %s" % str(exc)[:120])
        return None
    if not r or not r[4] or r[0] is None:
        return None
    mnla, mxla, mnlo, mxlo = (float(r[0]), float(r[1]),
                              float(r[2]), float(r[3]))
    # A single well, or a field, would otherwise be a zero-width box. The
    # floor is what stops fit_bounds picking its maximum zoom on one point.
    pad_la = max(0.05, (mxla - mnla) * 0.08)
    pad_lo = max(0.05, (mxlo - mnlo) * 0.08)
    return [[mnla - pad_la, mnlo - pad_lo], [mxla + pad_la, mxlo + pad_lo]]


# -----------------------------------------------------------------------------
# H3 GRID LOADER (Session 3)
# -----------------------------------------------------------------------------
# Reads from dataview_federation.v_well_density_r{N} — server-side
# aggregated views with pre-computed H3 cells. Returns a small result
# set (hundreds to tens of thousands of rows depending on resolution)
# that's safe to pull through pyodbc.
#
# schema_filter:
#     None             — all schemas (cross-schema, SUM well_count over both)
#     "dataview"       — onshore only
#     "dataview_gom"   — offshore only
#
# The view is a UNION ALL of per-schema aggregations. Filtering by
# dv_schema uses the schema-side index; cross-schema needs an outer
# GROUP BY h3 to combine the two arms (rare h3 cell might appear in
# both, e.g. a coastal hex).
# -----------------------------------------------------------------------------
@st.cache_data(ttl=None, show_spinner=False)
def _qry_h3_grid(_engine, resolution: int = 5,
                 schema_filter: str | None = None) -> pd.DataFrame:
    """
    Fetch H3 density aggregation at given resolution.

    Returns DataFrame with columns:
        h3         — H3 cell ID (NVARCHAR 15)
        well_count — wells in this cell (after any schema filter)

    Resolutions supported: 4, 5, 6, 7. R5 is the typical zoom-default;
    R4 for continent views, R6/R7 for close-in detail.

    Transport: tries BCP first (sub-second on the wire). Falls back to
    pyodbc if BCP isn't available or fails. SQL Server execution plan
    diagnostic (2026-05-27) showed the query itself runs in ~0.5s but
    pyodbc transport balloons to 60+ seconds for the cross-schema arm —
    same family of bug as the wells-loader hang. BCP-bypass mirrors the
    fix used by _qry_wells_bcp.
    """
    if resolution not in (4, 5, 6, 7):
        st.error(f"H3 resolution {resolution} not supported (use 4-7)")
        return pd.DataFrame()

    view = f"dataview_federation.v_well_density_r{resolution}"

    if schema_filter is None:
        # Cross-schema: sum across the UNION ALL arms, MINUS the wells that
        # appear in more than one of them.
        #
        # v_well is dv_well UNION ALL the national reference, and seeding
        # wells FROM that reference INTO dv_well makes the overlap the normal
        # case rather than a curiosity: after one county, 28,150 of dv_well's
        # 28,173 rows existed in both arms and every one was counted twice. A
        # single R5 cell read 4,728 where 2,364 wells stand. The help text on
        # the Density source picker already warned that blending makes "a
        # count nobody can interpret" -- this is that, and it is now
        # subtractable rather than only avoidable.
        #
        # AFFORDABLE BECAUSE dv_well IS THE SMALL SIDE. The duplicate is
        # always a dv_well row that also exists in the reference, so the
        # correction is grouped over ~28k rows, not 3.9M: 53 cells, 5.2s
        # measured, and it is cached like the rest of this query.
        _cell = f"h3_r{resolution}"
        sql = f"""
            SELECT s.h3,
                   s.well_count - ISNULL(d.dupes, 0) AS well_count
              FROM (SELECT h3, SUM(well_count) AS well_count
                      FROM {view} GROUP BY h3) s
              LEFT JOIN (
                    SELECT w.{_cell} AS h3, COUNT(*) AS dupes
                      FROM dataview.dv_well w
                     WHERE w.{_cell} IS NOT NULL
                       AND EXISTS (SELECT 1
                                     FROM dataview_federation.v_well_master_arm m
                                    WHERE m.uwi = w.uwi)
                     GROUP BY w.{_cell}) d
                ON d.h3 = s.h3
        """
        params = {}
    else:
        sql = f"""
            SELECT h3, well_count
            FROM {view}
            WHERE dv_schema = :schema
        """
        params = {"schema": schema_filter}

    # ── Try BCP-bypass first ─────────────────────────────────────────
    # BCP writes the result set to a local CSV, bypassing the pyodbc
    # ODBC pipe entirely. For the cross-schema query this drops the
    # transport time from ~60s to ~1-2s. The query SQL must be inlined
    # (no parameter binding) — BCP queryout doesn't accept :params,
    # so we substitute schema_filter directly. Safe here because the
    # value comes from a fixed enum in the page, not user input.
    if schema_filter is not None:
        # Substitute :schema into the SQL for BCP. Safe — schema_filter
        # is one of a fixed set ('dataview' / 'dataview_gom'), not user
        # input. Quote-escape defensively anyway.
        _safe_schema = schema_filter.replace("'", "''")
        bcp_sql = sql.replace(
            ":schema", f"'{_safe_schema}'"
        ).strip()
    else:
        bcp_sql = sql.strip()

    # Working dir for the CSV. Use Temp so Windows cleans up if we crash.
    work_dir = Path(os.environ.get("LOCALAPPDATA", "/tmp")) / "Temp" / "dw_h3_bcp"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        csv_path = work_dir / f"h3_r{resolution}_{schema_filter or 'all'}.csv"

        try:
            n_rows = _bcp_fetch_to_csv(bcp_sql, csv_path)
            # Two columns: h3 (text), well_count (int).
            rows = _parse_bcp_csv(csv_path, [
                ("h3",         "text"),
                ("well_count", "int"),
            ])
            try:
                csv_path.unlink()
            except FileNotFoundError:
                pass
            if rows:
                return pd.DataFrame(rows)
            # BCP returned an empty result — could be legitimate (no data)
            # or could be a silent failure. Fall through to pyodbc to be
            # sure. The cache will hold whichever path returns first.
        except Exception as bcp_exc:
            # BCP failed (binary not installed, server unreachable on the
            # BCP path, etc.). Log and fall through to pyodbc so the page
            # still works.
            print(f"[h3] BCP path failed: {bcp_exc}; falling back to pyodbc")
            try:
                csv_path.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        # Couldn't even create the working dir. Fall through to pyodbc.
        pass

    # ── Pyodbc fallback (original path) ──────────────────────────────
    try:
        with _engine.connect() as con:
            return pd.read_sql(text(sql), con, params=params)
    except Exception as exc:
        st.error(f"H3 grid query failed: {exc}")
        return pd.DataFrame()


def _h3_resolution_for_zoom(zoom: float | int | None) -> int:
    """
    Pick an H3 resolution based on the current Folium zoom level.

    Hex sizes vs zoom:
       zoom 3-4:   R4 (~370 km hex edge — continent view)
       zoom 5-6:   R5 (~140 km edge — multi-state)
       zoom 7-8:   R6 (~52 km edge — state-to-county)
       zoom >= 9:  R7 (~20 km edge — county-to-play)

    Returns the resolution as int. Defaults to R5 if zoom is None
    or unrecognized — that's the "comfortable middle" for the typical
    initial view of the US.
    """
    if zoom is None:
        return 5
    try:
        z = float(zoom)
    except (TypeError, ValueError):
        return 5
    if z <= 4:
        return 4
    if z <= 6:
        return 5
    if z <= 8:
        return 6
    return 7


@st.cache_data(ttl=3600, show_spinner=False)
def _h3_cell_boundary_geojson(h3_id: str) -> list[list[float]]:
    """
    Compute polygon ring for an H3 cell, formatted for GeoJSON.

    h3.cell_to_boundary returns (lat, lon) tuples; GeoJSON wants
    [lon, lat] (X,Y) order. We close the ring (first == last) so
    Leaflet renders it as a polygon, not a polyline.

    Cached because cell→boundary is deterministic and we re-render
    on every interaction.
    """
    try:
        boundary = h3.cell_to_boundary(h3_id)
    except Exception:
        return []
    coords = [[lon, lat] for lat, lon in boundary]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


@st.cache_data(ttl=3600, show_spinner=False)
def _h3_cell_bbox(h3_id: str) -> tuple[float, float, float, float] | None:
    """
    Return (min_lat, max_lat, min_lon, max_lon) bbox of an H3 cell.

    Used by the cell-click drill: click a hex, drill via _qry_wells_in_bbox
    using this bbox. Returns None if the cell ID is invalid.
    """
    try:
        boundary = h3.cell_to_boundary(h3_id)
    except Exception:
        return None
    if not boundary:
        return None
    lats = [p[0] for p in boundary]
    lons = [p[1] for p in boundary]
    return min(lats), max(lats), min(lons), max(lons)
def _h3_cell_center(cell):
    """Centre of one H3 cell as (lat, lon), or None.

    THE CELL ID IS COERCED AND THE FAILURE IS CONTAINED. h3 parses an id with
    int(cell, 16), so a null, a NaN or an integer column all raise the same
    "int() can't convert non-string with explicit base" -- and raising killed
    the entire density layer for one bad row, reported as "H3 render skipped".

    Module level because two callers need it now: the state/county constraint
    and the clip-to-selection filter. A second copy of this would be the
    parallel-worse-version failure this codebase keeps paying for.
    """
    _s = str(cell or "").strip()
    if not _s or _s.lower() == "nan":
        return None
    try:
        return h3.cell_to_latlng(_s)          # h3 v4
    except AttributeError:
        try:
            return h3.h3_to_geo(_s)           # h3 v3
        except Exception:
            return None
    except Exception:
        return None


def _clip_bounds_now():
    """The DRAWN RECTANGLE to clip to, or None.

    NOT _drawn_bounds. That name means "whatever last moved the camera" --
    an area selection sets it to the whole continental US and, being
    oneshot=False, it then persists. Clipping to that is clipping to
    everything, so the toggle appeared to do nothing at all: reported as
    "clip is on but it did not do anything when I drew a box", with the box
    itself detected correctly in the same log.

    _clip_box is set by the rectangle handler and by nothing else, so it
    means exactly one thing. _active_drill_bbox is the circle equivalent.
    """
    if not st.session_state.get("wm_clip_to_box"):
        return None
    return _norm_bounds(st.session_state.get("_clip_box")
                        or st.session_state.get("_active_drill_bbox"))
def _clip_sql(alias="w"):
    """ AND lat/lon BETWEEN ... for the drawn box, or "" when there is none.

    PUSHING THE BOX INTO THE QUERY, not filtering after it. Clipping the frame
    after the fetch drew the right map while still pulling every well in scope
    and throwing most of it away.

    NO INDEX BACKS THIS, and the comment here used to claim one did.
    dataview.dv_well carries pk_dv_well(uwi), IX_dv_well_h3_r5 and
    IX_dv_well_h3_r6 -- no lat/lon index at all -- so the predicate is a
    clustered scan with a cheap filter. Measured at 28,173 rows: 0.033s
    unconstrained, 0.053s with the box, which is why this is still worth
    doing (less data crosses the wire and less is built into the map) and
    also why it is NOT yet the win it would be on a large table.

    NUMBERS ARE FORMATTED, NOT INTERPOLATED RAW. These are floats this module
    computed from a drawn rectangle, but where_extra is concatenated into SQL,
    and "it cannot be a string here" is exactly the assumption that stops being
    true later. %.8f cannot carry anything but a number.
    """
    b = _clip_bounds_now()
    if not b:
        return ""
    (mnla, mnlo), (mxla, mxlo) = b[0], b[1]
    return (" AND %s.surface_latitude  BETWEEN %.8f AND %.8f"
            " AND %s.surface_longitude BETWEEN %.8f AND %.8f"
            % (alias, float(mnla), float(mxla),
               alias, float(mnlo), float(mxlo)))


def _clip_h3_df(df, bounds):
    """Keep only hexes whose CENTRE lies in bounds.

    Centre-in-box, matching what a drawn box already means for cell SELECTION
    (h3.polygon_to_cells) -- so clipping and selecting agree on which cells a
    box contains. Intersects would disagree with the selection by ~30%.
    """
    if df is None or df.empty or not bounds:
        return df
    (mnla, mnlo), (mxla, mxlo) = bounds[0], bounds[1]
    _ctr = df["h3"].map(_h3_cell_center)
    _nan = float("nan")
    _la = _ctr.map(lambda p: p[0] if p else _nan)
    _lo = _ctr.map(lambda p: p[1] if p else _nan)
    # NaN for a cell that cannot be placed: it fails every comparison and
    # drops out, which is the honest answer -- a cell we cannot locate
    # cannot be shown to be inside.
    return df[(_la >= mnla) & (_la <= mxla)
              & (_lo >= mnlo) & (_lo <= mxlo)].reset_index(drop=True)


def _clip_wells_df(df, bounds):
    """Keep only wells inside bounds. Coordinates coerced, nulls dropped."""
    if df is None or df.empty or not bounds:
        return df
    if "lat" not in df.columns or "lon" not in df.columns:
        return df
    (mnla, mnlo), (mxla, mxlo) = bounds[0], bounds[1]
    _la = pd.to_numeric(df["lat"], errors="coerce")
    _lo = pd.to_numeric(df["lon"], errors="coerce")
    return df[(_la >= mnla) & (_la <= mxla)
              & (_lo >= mnlo) & (_lo <= mxlo)].reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def _qry_wells_in_bbox(
    _engine,
    min_lat: float, max_lat: float,
    min_lon: float, max_lon: float,
    limit: int = 1000,
    where_extra: str = "",
) -> tuple[list[dict], int]:
    """
    Rectangle drill-down query: wells inside the given bounding box.

    Returns a tuple of (wells, total_count):
      wells       — list of well dicts, capped at `limit`
      total_count — true count of wells in the bbox (may exceed len(wells))

    The total_count tells the UI whether to warn about the cap being hit.

    THE INDEX THIS DOCSTRING NAMED DOES NOT EXIST. It claimed
    IX_dv_well_lat_lon made the query "sub-second for any reasonable bbox
    even at 4M scale"; dv_well has only pk_dv_well(uwi) and the h3_r5/h3_r6
    indexes. The query is a clustered scan, which is fine at 28K rows and is
    not what the claim promised at 4M. Checked 28 Aug against sys.indexes.

    Cache TTL is 300s — bbox queries are user-driven by rectangle drawing,
    so we don't need session-long persistence but want to avoid re-firing
    if the same rectangle is drawn twice in quick succession.
    """
    # COUNT first — cheap with the index, tells us whether to return rows
    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM dataview.dv_well w
        WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
          AND w.surface_longitude BETWEEN :min_lon AND :max_lon
          {where_extra}
    """
    rows_sql = f"""
        SELECT TOP (:limit)
               w.uwi, w.well_name, w.well_type, w.well_status,
               w.surface_latitude  AS lat,
               w.surface_longitude AS lon,
               w.county, w.province_state, w.api_num,
               CONVERT(VARCHAR(10), w.spud_date,       120) AS spud_date,
               CONVERT(VARCHAR(10), w.completion_date, 120) AS completion_date,
               w.final_td, w.depth_datum,
               w.operator_ba_id, w.field_id,
               ISNULL(ba.ba_name,   'Unknown') AS operator_name,
               ISNULL(f.field_name, 'Unknown') AS field_name
        FROM dataview.dv_well w
        LEFT JOIN dataview.dv_business_associate ba ON ba.ba_id = w.operator_ba_id
        LEFT JOIN dataview.dv_field f ON f.field_id = w.field_id
        WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
          AND w.surface_longitude BETWEEN :min_lon AND :max_lon
          {where_extra}
        ORDER BY w.well_name
        FOR JSON PATH
    """
    params = {
        "min_lat": float(min_lat), "max_lat": float(max_lat),
        "min_lon": float(min_lon), "max_lon": float(max_lon),
        "limit": int(limit),
    }
    try:
        with _engine.connect().execution_options(timeout=30) as con:
            total = con.execute(text(count_sql), params).scalar() or 0
            if total == 0:
                return [], 0

            # FOR JSON PATH returns multi-row varchar chunks — concat them
            json_rows = con.execute(text(rows_sql), params).fetchall()
            if not json_rows:
                return [], total
            json_str = "".join(r[0] for r in json_rows)
            wells = json.loads(json_str)
            return wells, int(total)
    except Exception as exc:
        st.error(f"Bbox query failed: {exc}")
        return [], 0
@st.cache_data(ttl=300, show_spinner=False)
def _qry_cell_uwis_in_bbox(_engine, min_lat: float, max_lat: float,
                           min_lon: float, max_lon: float,
                           cellcol: str, where_extra: str = "",
                           limit: int = 200000) -> dict:
    """{cell: [uwi]} for every well in a bbox, bucketed by its own H3 cell.

    ONE QUERY FOR THE WHOLE BOX. The box-selects-cells path used to call
    _qry_wells_in_bbox once PER CELL, and each of those runs a COUNT and then
    a SELECT. That was invisible at R4, where a county-sized box is a few
    dozen cells; at R7 the same box is 3,410 cells and therefore ~6,820 round
    trips, which reads as a hung map -- reported as "I drew a box but nothing
    happened", twice.

    Bucketing in Python is right here because the wells are wanted anyway: the
    cell is stored ON the well (h3_r4..h3_r7), so one indexed bbox read gives
    every cell in the box and its members together.

    CELLCOL IS WHITELISTED, not interpolated blind -- it reaches SQL as an
    identifier and the resolutions are a closed set.
    """
    if cellcol not in ("h3_r4", "h3_r5", "h3_r6", "h3_r7"):
        return {}
    sql = f"""
        SELECT TOP (:limit) RTRIM(w.uwi) AS uwi, w.{cellcol} AS cell
        FROM dataview.dv_well w
        WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
          AND w.surface_longitude BETWEEN :min_lon AND :max_lon
          AND w.{cellcol} IS NOT NULL
          {where_extra}
    """
    out: dict = {}
    try:
        with _engine.connect().execution_options(timeout=60) as con:
            for r in con.execute(text(sql), {
                    "min_lat": float(min_lat), "max_lat": float(max_lat),
                    "min_lon": float(min_lon), "max_lon": float(max_lon),
                    "limit": int(limit)}):
                out.setdefault(str(r.cell), []).append(r.uwi)
    except Exception as exc:
        # NOT swallowed silently: a discarded diagnostic makes the next
        # failure undiagnosable (CLAUDE.md).
        _say("[map] cell-bucket query failed: %s" % str(exc)[:160])
        return {}
    return out


@st.cache_data(ttl=300, show_spinner=False)
def _qry_wells_in_circle(
    _engine,
    center_lat: float,
    center_lon: float,
    radius_m: float,
    limit: int = 5000,
    where_extra: str = "",
) -> tuple[list[dict], int]:
    """
    Haversine wells-in-radius query.

    Returns (wells, total_count) — wells capped at `limit`, total_count is
    the true population inside the circle (may exceed len(wells)).

    Two-stage filter:
      1. bbox prefilter using IX_dv_well_lat_lon (cheap index range scan)
      2. Haversine distance check on the prefilter result (Python-side
         dataframe filter, cheap on ~hundreds of candidates)

    Validated against SSMS — same query pattern, sub-second at current scale,
    scales to 4M wells with the index in place.
    """
    import math as _m

    # bbox prefilter expansion in degrees (rough but generous — we filter
    # exactly with Haversine afterward)
    _dlat = radius_m / 111000.0
    _dlon = radius_m / (
        111000.0 * max(_m.cos(_m.radians(center_lat)), 0.01)
    )
    _min_lat = center_lat - _dlat
    _max_lat = center_lat + _dlat
    _min_lon = center_lon - _dlon
    _max_lon = center_lon + _dlon

    # Two queries: COUNT (with Haversine) then TOP rows (with Haversine).
    # We can't combine because the COUNT needs the full result, not TOP.
    # Both queries are fast because of the bbox prefilter on the indexed
    # columns — Haversine runs only on the candidates inside the bbox.
    count_sql = f"""
        WITH InBox AS (
            SELECT surface_latitude AS lat, surface_longitude AS lon
            FROM dataview.dv_well
            WHERE surface_latitude  BETWEEN :min_lat AND :max_lat
              AND surface_longitude BETWEEN :min_lon AND :max_lon
              {where_extra}
        )
        SELECT COUNT(*) AS n
        FROM InBox
        WHERE 6371000 * 2 * ASIN(SQRT(
                POWER(SIN(RADIANS(lat - :center_lat) / 2), 2) +
                COS(RADIANS(:center_lat)) * COS(RADIANS(lat)) *
                POWER(SIN(RADIANS(lon - :center_lon) / 2), 2)
            )) <= :radius_m
    """
    rows_sql = f"""
        WITH InBox AS (
            SELECT w.uwi, w.well_name, w.well_type, w.well_status,
                   w.surface_latitude  AS lat,
                   w.surface_longitude AS lon,
                   w.county, w.province_state, w.api_num,
                   CONVERT(VARCHAR(10), w.spud_date,       120) AS spud_date,
                   CONVERT(VARCHAR(10), w.completion_date, 120) AS completion_date,
                   w.final_td, w.depth_datum,
                   w.operator_ba_id, w.field_id,
                   ISNULL(ba.ba_name,   'Unknown') AS operator_name,
                   ISNULL(f.field_name, 'Unknown') AS field_name
            FROM dataview.dv_well w
            LEFT JOIN dataview.dv_business_associate ba ON ba.ba_id = w.operator_ba_id
            LEFT JOIN dataview.dv_field f               ON f.field_id      = w.field_id
            WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
              AND w.surface_longitude BETWEEN :min_lon AND :max_lon
              {where_extra}
        )
        SELECT TOP (:limit) *,
            6371000 * 2 * ASIN(SQRT(
                POWER(SIN(RADIANS(lat - :center_lat) / 2), 2) +
                COS(RADIANS(:center_lat)) * COS(RADIANS(lat)) *
                POWER(SIN(RADIANS(lon - :center_lon) / 2), 2)
            )) AS distance_m
        FROM InBox
        WHERE 6371000 * 2 * ASIN(SQRT(
                POWER(SIN(RADIANS(lat - :center_lat) / 2), 2) +
                COS(RADIANS(:center_lat)) * COS(RADIANS(lat)) *
                POWER(SIN(RADIANS(lon - :center_lon) / 2), 2)
            )) <= :radius_m
        ORDER BY distance_m
        FOR JSON PATH
    """
    params = {
        "min_lat":    float(_min_lat),
        "max_lat":    float(_max_lat),
        "min_lon":    float(_min_lon),
        "max_lon":    float(_max_lon),
        "center_lat": float(center_lat),
        "center_lon": float(center_lon),
        "radius_m":   float(radius_m),
        "limit":      int(limit),
    }
    try:
        with _engine.connect().execution_options(timeout=30) as con:
            total = con.execute(text(count_sql), params).scalar() or 0
            if total == 0:
                return [], 0

            json_rows = con.execute(text(rows_sql), params).fetchall()
            if not json_rows:
                return [], total
            json_str = "".join(r[0] for r in json_rows)
            wells = json.loads(json_str)
            return wells, int(total)
    except Exception as exc:
        st.error(f"Circle query failed: {exc}")
        return [], 0


# ── GOM well drill queries ──────────────────────────────────────────────────
# REFACTOR: These mirror _qry_wells_in_bbox and _qry_wells_in_circle but
# read from dataview_gom.well and return GOM-shaped rows (no operator FK,
# no field FK, has BOEM-specific fields like bottom_area_code).
# When the second region (Permian, etc.) lands, these should consolidate
# with the dv_well versions into a single generic dispatcher.

@st.cache_data(ttl=300, show_spinner=False)
def _qry_gom_wells_in_bbox(
    _engine,
    min_lat: float, max_lat: float,
    min_lon: float, max_lon: float,
    limit: int = 1000,
    where_extra: str = "",
) -> tuple[list[dict], int]:
    """
    Rectangle drill-down query for GOM wells inside a bounding box.

    Mirrors _qry_wells_in_bbox structure but reads from dataview_gom.well.
    Returns GOM-shaped well dicts with BOEM-native fields (api_well_number,
    company_name, lease, area/block, water depth, etc.).

    The well_id (UUID) is returned alongside the BOEM API so the popup can
    show the user-readable identifier while the system tracks the internal
    one. Indexes ix_dv_well_gom_surface_coords makes this query fast.
    """
    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM dataview_gom.well w
        WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
          AND w.surface_longitude BETWEEN :min_lon AND :max_lon
          {where_extra}
    """
    rows_sql = f"""
        SELECT TOP (:limit)
               CAST(w.well_id AS NVARCHAR(40)) AS well_id,
               w.api_well_number,
               w.well_name,
               w.well_name_suffix,
               w.company_name,
               w.surface_lease_number,
               w.bottom_lease_number,
               w.bottom_area_code,
               w.bottom_block_number,
               w.region,
               CONVERT(VARCHAR(10), w.spud_date,        120) AS spud_date,
               CONVERT(VARCHAR(10), w.total_depth_date, 120) AS total_depth_date,
               CONVERT(VARCHAR(10), w.status_date,      120) AS status_date,
               w.type_code,
               w.status_code,
               CAST(w.surface_latitude  AS FLOAT) AS lat,
               CAST(w.surface_longitude AS FLOAT) AS lon,
               CAST(w.bottom_latitude   AS FLOAT) AS bottom_lat,
               CAST(w.bottom_longitude  AS FLOAT) AS bottom_lon,
               CAST(w.bh_total_md_ft         AS FLOAT) AS bh_total_md_ft,
               CAST(w.true_vertical_depth_ft AS FLOAT) AS tvd_ft,
               CAST(w.water_depth_ft         AS FLOAT) AS water_depth_ft
        FROM dataview_gom.well w
        WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
          AND w.surface_longitude BETWEEN :min_lon AND :max_lon
          {where_extra}
        ORDER BY w.well_name
        FOR JSON PATH
    """
    params = {
        "min_lat": float(min_lat), "max_lat": float(max_lat),
        "min_lon": float(min_lon), "max_lon": float(max_lon),
        "limit": int(limit),
    }
    try:
        with _engine.connect().execution_options(timeout=30) as con:
            total = con.execute(text(count_sql), params).scalar() or 0
            if total == 0:
                return [], 0
            json_rows = con.execute(text(rows_sql), params).fetchall()
            if not json_rows:
                return [], total
            json_str = "".join(r[0] for r in json_rows)
            wells = json.loads(json_str)
            return wells, int(total)
    except Exception as exc:
        st.error(f"GOM bbox query failed: {exc}")
        return [], 0


@st.cache_data(ttl=300, show_spinner=False)
def _qry_gom_wells_in_circle(
    _engine,
    center_lat: float,
    center_lon: float,
    radius_m: float,
    limit: int = 5000,
    where_extra: str = "",
) -> tuple[list[dict], int]:
    """
    Haversine drill-down query for GOM wells inside a radius.

    Mirrors _qry_wells_in_circle structure (bbox prefilter + Haversine
    refinement) but reads from dataview_gom.well. Returns GOM-shaped
    well dicts ordered by distance from circle center.

    The bbox prefilter uses ix_dv_well_gom_surface_coords; the Haversine
    distance check then refines to the exact circle. Two queries (COUNT
    then TOP rows) — same pattern as the dv_well version.
    """
    import math as _m

    _dlat = radius_m / 111000.0
    _dlon = radius_m / (
        111000.0 * max(_m.cos(_m.radians(center_lat)), 0.01)
    )
    _min_lat = center_lat - _dlat
    _max_lat = center_lat + _dlat
    _min_lon = center_lon - _dlon
    _max_lon = center_lon + _dlon

    count_sql = f"""
        WITH InBox AS (
            SELECT CAST(w.surface_latitude  AS FLOAT) AS lat,
                   CAST(w.surface_longitude AS FLOAT) AS lon
            FROM dataview_gom.well w
            WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
              AND w.surface_longitude BETWEEN :min_lon AND :max_lon
              {where_extra}
        )
        SELECT COUNT(*) AS n
        FROM InBox
        WHERE 6371000 * 2 * ASIN(SQRT(
                POWER(SIN(RADIANS(lat - :center_lat) / 2), 2) +
                COS(RADIANS(:center_lat)) * COS(RADIANS(lat)) *
                POWER(SIN(RADIANS(lon - :center_lon) / 2), 2)
            )) <= :radius_m
    """
    rows_sql = f"""
        WITH InBox AS (
            SELECT CAST(w.well_id AS NVARCHAR(40)) AS well_id,
                   w.api_well_number, w.well_name, w.well_name_suffix,
                   w.company_name, w.surface_lease_number, w.bottom_lease_number,
                   w.bottom_area_code, w.bottom_block_number, w.region,
                   CONVERT(VARCHAR(10), w.spud_date,        120) AS spud_date,
                   CONVERT(VARCHAR(10), w.total_depth_date, 120) AS total_depth_date,
                   CONVERT(VARCHAR(10), w.status_date,      120) AS status_date,
                   w.type_code, w.status_code,
                   CAST(w.surface_latitude  AS FLOAT) AS lat,
                   CAST(w.surface_longitude AS FLOAT) AS lon,
                   CAST(w.bottom_latitude   AS FLOAT) AS bottom_lat,
                   CAST(w.bottom_longitude  AS FLOAT) AS bottom_lon,
                   CAST(w.bh_total_md_ft         AS FLOAT) AS bh_total_md_ft,
                   CAST(w.true_vertical_depth_ft AS FLOAT) AS tvd_ft,
                   CAST(w.water_depth_ft         AS FLOAT) AS water_depth_ft
            FROM dataview_gom.well w
            WHERE w.surface_latitude  BETWEEN :min_lat AND :max_lat
              AND w.surface_longitude BETWEEN :min_lon AND :max_lon
              {where_extra}
        )
        SELECT TOP (:limit) *,
            6371000 * 2 * ASIN(SQRT(
                POWER(SIN(RADIANS(lat - :center_lat) / 2), 2) +
                COS(RADIANS(:center_lat)) * COS(RADIANS(lat)) *
                POWER(SIN(RADIANS(lon - :center_lon) / 2), 2)
            )) AS distance_m
        FROM InBox
        WHERE 6371000 * 2 * ASIN(SQRT(
                POWER(SIN(RADIANS(lat - :center_lat) / 2), 2) +
                COS(RADIANS(:center_lat)) * COS(RADIANS(lat)) *
                POWER(SIN(RADIANS(lon - :center_lon) / 2), 2)
            )) <= :radius_m
        ORDER BY distance_m
        FOR JSON PATH
    """
    params = {
        "min_lat":    float(_min_lat),
        "max_lat":    float(_max_lat),
        "min_lon":    float(_min_lon),
        "max_lon":    float(_max_lon),
        "center_lat": float(center_lat),
        "center_lon": float(center_lon),
        "radius_m":   float(radius_m),
        "limit":      int(limit),
    }
    try:
        with _engine.connect().execution_options(timeout=30) as con:
            total = con.execute(text(count_sql), params).scalar() or 0
            if total == 0:
                return [], 0
            json_rows = con.execute(text(rows_sql), params).fetchall()
            if not json_rows:
                return [], total
            json_str = "".join(r[0] for r in json_rows)
            wells = json.loads(json_str)
            return wells, int(total)
    except Exception as exc:
        st.error(f"GOM circle query failed: {exc}")
        return [], 0


@st.cache_data(ttl=3600, show_spinner=False)
def _qry_trajectories(_engine) -> pd.DataFrame:
    try:
        with _engine.connect() as con:
            return pd.read_sql(text("""
                SELECT s.uwi, s.seq_num, s.md, s.tvd,
                       s.ns_offset, s.ew_offset,
                       w.surface_latitude surf_lat, w.surface_longitude surf_lon,
                       w.well_name
                FROM dataview.dv_well_dir_srvy_sta s
                JOIN dataview.dv_well w ON w.uwi = s.uwi
                WHERE w.surface_latitude IS NOT NULL
                  AND s.ns_offset IS NOT NULL AND s.ew_offset IS NOT NULL
                ORDER BY s.uwi, s.seq_num
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def _qry_survey_sticks(_engine) -> pd.DataFrame:
    """
    One row per onshore well that has a directional survey: the surface
    location plus the DEEPEST survey station's NS/EW offset (i.e. the
    bottomhole). Used to draw a straight surface→TD "stick" — no intermediate
    stations, just the two endpoints. ROW_NUMBER picks the max-MD station per
    well in a single indexed pass (no per-row work).
    """
    try:
        with _engine.connect() as con:
            return pd.read_sql(text("""
                SELECT uwi, well_name, surf_lat, surf_lon,
                       ns_offset, ew_offset, md
                FROM (
                    SELECT s.uwi, w.well_name,
                           w.surface_latitude  AS surf_lat,
                           w.surface_longitude AS surf_lon,
                           s.ns_offset, s.ew_offset, s.md,
                           ROW_NUMBER() OVER (PARTITION BY s.uwi
                                              ORDER BY s.md DESC) AS rn
                    FROM dataview.dv_well_dir_srvy_sta s
                    JOIN dataview.dv_well w ON w.uwi = s.uwi
                    WHERE w.surface_latitude IS NOT NULL
                      AND s.ns_offset IS NOT NULL
                      AND s.ew_offset IS NOT NULL
                ) t
                WHERE rn = 1
            """), con)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def _register_spatial_layer(engine, path, name, category, colour, filled):
    """Insert a row into dv_spatial_layer for a shapefile or GeoJSON on disk.

    Registration records WHERE the file is; it does not copy or parse the
    geometry into the database. _add_shapefile_layer reads the file at render
    time, so a moved or deleted file shows up as a blank layer rather than a
    stale one — which is the honest behaviour for a registry of paths.

    Bbox and feature count are best-effort: nice for zooming, not worth
    failing a registration over, so a file we can't pre-read still registers.
    """
    import os as _os
    import uuid as _uuid
    from datetime import datetime as _dt2
    if not _os.path.exists(path):
        return False, f"Not found on disk: {path}"
    _ext = _os.path.splitext(path)[1].lower()
    if _ext not in (".shp", ".geojson", ".json"):
        return False, "Expected a .shp, .geojson or .json file."
    if _ext == ".shp":
        _missing = [e for e in (".shx", ".dbf")
                    if not _os.path.exists(_os.path.splitext(path)[0] + e)]
        if _missing:
            return False, ("A shapefile needs its sidecars — missing "
                           + ", ".join(_missing)
                           + ". Copy the whole set, not just the .shp.")
        if not _os.path.exists(_os.path.splitext(path)[0] + ".prj"):
            # Not fatal, but say so: without a .prj the CRS has to be assumed,
            # and a State Plane file read as degrees lands in the wrong ocean.
            pass

    _n, _bb = None, (None, None, None, None)
    try:
        import shapefile as _pyshp          # pyshp
        _r = _pyshp.Reader(path)
        _n = len(_r)
        _mnlo, _mnla, _mxlo, _mxla = _r.bbox
        _bb = (_mnla, _mxla, _mnlo, _mxlo)
    except Exception:
        pass

    row = {
        "layer_id": _uuid.uuid4().hex[:40].upper(),
        "layer_name": name[:255],
        "layer_type": "POLYGON" if filled else "LINE",
        "layer_category": (category or "")[:40] or None,
        "file_path": path[:2000],
        "source_type": "GEOJSON" if _ext != ".shp" else "SHAPEFILE",
        "feature_count": _n,
        "bbox_min_lat": _bb[0], "bbox_max_lat": _bb[1],
        "bbox_min_lon": _bb[2], "bbox_max_lon": _bb[3],
        "active_ind": "Y",
        "style_color": colour, "style_weight": 2, "style_opacity": 0.9,
        "style_fill_color": colour if filled else None,
        "style_fill_opacity": 0.25 if filled else 0.0,
        "source": "MANUAL",
        "row_created_by": "DataWrangler",
        "row_created_date": _dt2.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cols = ", ".join(f"[{k}]" for k in row)
    vals = ", ".join(f":{k}" for k in row)
    try:
        with engine.begin() as con:
            con.execute(text(
                f"INSERT INTO dataview.dv_spatial_layer ({cols}) VALUES ({vals})"),
                row)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, (f"Registered '{name}'"
                  + (f" — {_n:,} feature(s)" if _n else "")
                  + ". Tick it above to draw it.")


def _load_shp_layers(_engine) -> list[dict]:
    try:
        return list_layers(_engine)
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def _cached_layer_geojson(_engine, layer_id: str) -> str | None:
    """Cache the GeoJSON blob — can be large, only fetch once per TTL."""
    return get_layer_geojson(_engine, layer_id)


@st.cache_data(ttl=300, show_spinner=False)
def _qry_zoom_targets(_engine) -> list[dict]:
    """
    Build a list of named locations for zoom-to selectbox.
    Includes: counties (from dv_well locations), fields, basins.

    This is the dv_well / main-source variant. For GOM, see
    _qry_gom_zoom_targets — the Zoom-To widget dispatches on the
    active Area.

    Each entry includes filter_kind and filter_value so picking the
    target also applies a wells filter (composable / cascading with
    Query / Status / Region). filter_kind="field" / "basin" / "county"
    binds to dv_well columns field_name / basin_name / county.
    """
    targets = [{"label": "— Zoom to location —",
                "lat": None, "lon": None, "zoom": 7,
                "filter_kind": None, "filter_value": None}]
    try:
        with _engine.connect().execution_options(timeout=8) as con:
            # Fields with coordinates
            rows = con.execute(text("""
                SELECT field_name, surface_latitude, surface_longitude
                FROM dataview.dv_field
                WHERE surface_latitude IS NOT NULL
                ORDER BY field_name
            """)).fetchall()
            for r in rows:
                targets.append({
                    "label": f"🌿 {r[0]}",
                    "lat": float(r[1]), "lon": float(r[2]), "zoom": 9,
                    "filter_kind":  "field",
                    "filter_value": str(r[0]),
                })
            # Basins with coordinates
            rows = con.execute(text("""
                SELECT basin_name, centroid_latitude, centroid_longitude
                FROM dataview.dv_basin
                WHERE centroid_latitude IS NOT NULL
                ORDER BY basin_name
            """)).fetchall()
            for r in rows:
                targets.append({
                    "label": f"🏔 {r[0]}",
                    "lat": float(r[1]), "lon": float(r[2]), "zoom": 7,
                    "filter_kind":  "basin",
                    "filter_value": str(r[0]),
                })
            # Counties — use average well location as proxy centre
            rows = con.execute(text("""
                SELECT w.county, w.province_state,
                       AVG(w.surface_latitude)  lat,
                       AVG(w.surface_longitude) lon,
                       COUNT(*) n
                FROM dataview.dv_well w
                WHERE w.surface_latitude IS NOT NULL AND w.county IS NOT NULL
                GROUP BY w.county, w.province_state
                HAVING COUNT(*) >= 1
                ORDER BY w.province_state, w.county
            """)).fetchall()
            for r in rows:
                targets.append({
                    "label": f"📍 {r[0]}, {r[1]}",
                    "lat": float(r[2]), "lon": float(r[3]), "zoom": 10,
                    # Counties need state disambiguation since county
                    # names aren't unique across states. We store both
                    # as a tuple so the mask handler can apply both
                    # constraints (county + province_state).
                    "filter_kind":  "county",
                    "filter_value": (str(r[0]), str(r[1])),
                })
            # Individual wells omitted from dropdown — too many items causes hang
            # Use the scout ticket panel to navigate to individual wells
    except Exception:
        pass
    return targets


def _qry_gom_zoom_targets(_engine) -> list[dict]:
    """
    Build the Zoom-To list for the Gulf of America area.

    GOM has no fields/basins/counties in the dv_well sense. The natural
    navigation unit offshore is the BOEM OCS protraction area
    (Mississippi Canyon, Green Canyon, etc.), identified by
    bottom_area_code in dataview_gom.well. Each entry's centroid is the
    average surface coordinate of all wells in that area code.

    Ordered by well count descending so the most-drilled areas are at
    the top of the dropdown — that's where the user most likely wants
    to go.

    Each entry includes filter_kind and filter_value so picking the
    target also applies a wells filter (composable / cascading with
    Query / Status / Region). filter_kind="protraction" for GOM area
    codes; the placeholder entry has filter_kind=None.
    """
    targets = [{"label": "— Zoom to location —",
                "lat": None, "lon": None, "zoom": 6,
                "filter_kind": None, "filter_value": None}]
    try:
        with _engine.connect().execution_options(timeout=8) as con:
            rows = con.execute(text("""
                SELECT bottom_area_code,
                       AVG(surface_latitude)  AS lat,
                       AVG(surface_longitude) AS lon,
                       COUNT(*)               AS n
                FROM dataview_gom.well
                WHERE surface_latitude IS NOT NULL
                  AND surface_longitude IS NOT NULL
                  AND bottom_area_code IS NOT NULL
                  AND LTRIM(RTRIM(bottom_area_code)) <> ''
                GROUP BY bottom_area_code
                ORDER BY COUNT(*) DESC
            """)).fetchall()
            for r in rows:
                _code = str(r[0]).strip()
                _n    = int(r[3])
                _name = _boem_area_name(_code)
                _disp = f"{_name} ({_code})" if _name != _code else _code
                targets.append({
                    "label": f"🌊 {_disp} · {_n:,} wells",
                    "lat": float(r[1]), "lon": float(r[2]),
                    "zoom": 9,
                    # Filter binding — picking this target narrows wells
                    # to those whose bottom_area_code matches. The raw
                    # code (not the friendly name) is what the column
                    # actually stores.
                    "filter_kind":  "protraction",
                    "filter_value": _code,
                })
    except Exception:
        pass
    return targets


# =============================================================================
# HELPERS
# =============================================================================

def _popup_table(fields: dict) -> str:
    rows = "".join(
        f"<tr><td style='color:#666;padding:2px 6px 2px 0;font-size:11px'>{k}</td>"
        f"<td style='font-size:11px'>{v}</td></tr>"
        for k, v in fields.items()
        if v is not None and str(v).strip() not in ("", "None", "nan")
    )
    return f"<table style='border-collapse:collapse'>{rows}</table>"


def _offset_to_latlon(surf_lat, surf_lon, ns_ft, ew_ft):
    deg_lat = 364000.0
    deg_lon = 364000.0 * math.cos(math.radians(surf_lat))
    return surf_lat + ns_ft / deg_lat, surf_lon + ew_ft / deg_lon


def _trajectory_geojson(df: pd.DataFrame) -> dict:
    features = []
    for uwi, grp in df.groupby("uwi"):
        grp = grp.sort_values("seq_num")
        slat = grp["surf_lat"].iloc[0]
        slon = grp["surf_lon"].iloc[0]
        coords = [list(_offset_to_latlon(slat, slon,
                    float(r["ns_offset"] or 0),
                    float(r["ew_offset"] or 0)))[::-1]
                  for _, r in grp.iterrows()]
        if len(coords) >= 2:
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "well_name": grp["well_name"].iloc[0],
                    "uwi": uwi,
                    "stations": len(coords),
                    "td_ft": float(grp["md"].max()),
                },
            })
    return {"type": "FeatureCollection", "features": features}


# =============================================================================
# LAYER RENDERERS
# =============================================================================

def _add_h3_layer(
    m,
    df: pd.DataFrame,
    selected_set: set | None = None,
    interactive: bool = True,
) -> int:
    """
    Render the H3 density layer.

    Each H3 cell becomes a hex polygon, fill-colored by log-scaled well
    count, with selected/interactive semantics for click-to-drill.

    Cells in `selected_set` (set of h3 cell IDs) get a bold blue outline,
    matching the multi-select Commit pattern used for click-to-drill.

    Args:
        m: folium.Map
        df: result of _qry_h3_grid — columns h3 (cell ID) and well_count
        selected_set: optional set of h3 cell IDs the user has multi-selected
        interactive: when False (Circle selection mode), hexes are inert so
            press-drag for circle drawing can pass through to Leaflet.draw

    Returns:
        number of hexes rendered
    """
    if df is None or df.empty:
        return 0

    # Same palette + log scale as the rectangular grid so the user's
    # visual intuition transfers between modes.
    max_count = max(int(df["well_count"].max()), 1)
    log_max = math.log10(max_count + 1) or 1.0

    palette = [
        "#fff5b1",  # very pale yellow
        "#fed976",
        "#feb24c",
        "#fd8d3c",
        "#fc4e2a",
        "#b10026",  # deep red
    ]

    def _color_for(count: int) -> str:
        if count <= 0:
            return palette[0]
        t = math.log10(count + 1) / log_max
        idx = min(int(t * len(palette)), len(palette) - 1)
        return palette[idx]

    if selected_set is None:
        selected_set = set()

    # ONE GeoJson LAYER, NOT ONE POLYGON PER CELL. The loop this replaces
    # carried its own warning -- "for ~1,800 cells at R5 this is fast enough;
    # if we move to R7 (37K cells) we may want to switch to a single GeoJson
    # layer" -- and the assumption expired: r5 is 14,780 cells now, r6 is
    # 66,477, because the federated reference master brought 3.9M wells.
    #
    # Measured at 14,780 cells: 8.22s and 13.6 MB of HTML per Polygon, against
    # 1.14s and 6.4 MB as one GeoJson -- 7.2x the build, half the payload, and
    # Leaflet manages ONE layer object instead of 14,780. The payload crosses
    # to the browser on every rerun, so it is paid far more often than it is
    # built.
    #
    # CLICK-TO-DRILL IS UNAFFECTED, checked before changing the object type:
    # the handler reads last_object_clicked's lat/lon and recomputes the cell
    # with h3.latlng_to_cell, so it never depended on the polygon's identity.
    # No popup is attached, deliberately -- the click path distinguishes a
    # marker from a cell by "markers have popups, cells don't".
    features = []
    n_rendered = 0
    for row in df.itertuples(index=False):
        try:
            h3_id = str(row.h3)
            count = int(row.well_count)
        except (TypeError, ValueError):
            continue
        coords = _h3_cell_boundary_geojson(h3_id)
        if not coords:
            continue
        _sel = h3_id in selected_set
        features.append({
            "type": "Feature",
            "properties": {
                "h3": h3_id,
                "fill": _color_for(count),
                "line": "#1d4ed8" if _sel else "#7f1d1d",
                "w": 3 if _sel else 0.5,
                "tip": (f"{count:,} wells - "
                        + ("selected, click again to deselect"
                           if _sel else "click to select")
                        + f"  ({h3_id})"),
            },
            # GeoJSON is [lon, lat]; _h3_cell_boundary_geojson already returns
            # that order, so unlike the folium.Polygon path there is no flip.
            "geometry": {"type": "Polygon", "coordinates": [list(coords)]},
        })
        n_rendered += 1

    if features:
        _gj = folium.GeoJson(
            {"type": "FeatureCollection", "features": features},
            name="Wells (H3)",
            style_function=lambda f: {
                "fillColor": f["properties"]["fill"],
                "color": f["properties"]["line"],
                "weight": f["properties"]["w"],
                "fillOpacity": 0.55,
                # Inert in Circle-selection mode so a press-drag can pass
                # through to Leaflet.draw, exactly as the old per-polygon
                # options["interactive"] did.
                "interactive": bool(interactive),
            },
            tooltip=(folium.GeoJsonTooltip(fields=["tip"], labels=False,
                                           sticky=True)
                     if interactive else None),
        )
        _gj.add_to(m)
    return n_rendered



def _add_drill_results_to_tray(wells: list[dict], replace: bool = False) -> int:
    """
    Add drill-result wells to the result set (`clicked_uwis`) and shadow cache
    (`tray_well_data`). Drill results are full well dicts (uwi + well_name,
    operator_name, etc.).

    replace=True ⇒ this draw becomes the *entire* result set: the prior set is
    cleared and the filter auto-route is suppressed so it won't re-merge the
    Query result on the next rerun (a later Query clears suppression and takes
    the set back over). replace=False just appends (used by the filter
    auto-route itself).

    Returns the count actually added.
    """
    if not wells:
        return 0
    if "clicked_uwis" not in st.session_state:
        st.session_state.clicked_uwis = []
    if "tray_well_data" not in st.session_state:
        st.session_state.tray_well_data = {}
    if replace:
        # New spatial selection owns the result set.
        st.session_state.clicked_uwis = []
        st.session_state["_auto_tray_uwis"] = []
        st.session_state["tray_well_data"] = {}
        st.session_state["wells_suppressed"] = True
    _added = 0
    _shadow = st.session_state["tray_well_data"]
    for w in wells:
        uwi = w.get("uwi")
        if not uwi:
            continue
        if uwi not in st.session_state.clicked_uwis:
            st.session_state.clicked_uwis.append(uwi)
            _added += 1
        # Update shadow with the full well dict so the result label, scout
        # ticket, and Excel export all have what they need.
        _shadow[uwi] = w
    st.session_state["tray_well_data"] = _shadow
    return _added

def _add_wells(m, df, exclude_uwis=None, disable_clustering_at_zoom=13,
               ppdm=False):
    """
    Individual (un-clustered) well markers.

    Behavior:
    - Every well in df (minus exclude_uwis) renders as its own circle marker
      at all zoom levels — no cluster bubbles. Density is shown by the H3
      grid layers instead.
    - Individual markers from a drawn rectangle are rendered separately by
      _add_viewport_wells.

    Args:
        m: folium.Map
        df: wells DataFrame (already filtered by area/Query, so the count is
            bounded by the load cap)
        exclude_uwis: iterable of UWI strings to skip (typically the current
            viewport selection, since those render as their own markers)
        disable_clustering_at_zoom: retained for call-site compatibility;
            no longer used (clustering is off entirely).
    """
    if df.empty:
        return

    df = df.reset_index(drop=True)
    if exclude_uwis:
        excl = set(map(str, exclude_uwis))
        df = df[~df["uwi"].astype(str).isin(excl)].reset_index(drop=True)
        if df.empty:
            return

    # ── THE CAP THAT MATTERS IS ON MARKERS DRAWN, NOT ROWS LOADED ──────
    # MEASURED, not guessed: 1,373 wells rendered in ~2.0s; 28,173 wells --
    # one county seeded from the reference master -- took 593 SECONDS for a
    # single render, and Streamlit greys the page for the duration, so it
    # reads as a frozen app rather than a slow one. Twenty times the wells,
    # three hundred times the time: the cost is superlinear, because
    # clustering is off (maxClusterRadius 1) and every marker is serialised
    # into the map HTML on EVERY rerun.
    #
    # _WELLS_LOAD_CAP is 30,000 and never fired, because it caps the QUERY.
    # Nothing capped the drawing. This does.
    #
    # It draws the first N and SAYS SO. Silently truncating would be the
    # "wrong is worse than missing" case -- a map that looks complete and is
    # not. H3 density is the honest way to see them all, and the message says
    # that too.
    if len(df) > _WELLS_DRAW_CAP:
        _shown = len(df)
        df = df.head(_WELLS_DRAW_CAP).reset_index(drop=True)
        try:
            st.warning(
                "📍 **%s wells in scope — drawing the first %s.** Individual "
                "markers are re-serialised on every interaction, and past a "
                "few thousand that costs minutes per render. Narrow the area, "
                "draw a box, or switch to 🔶 **H3 density**, which aggregates "
                "and shows every well."
                % (format(_shown, ","), format(_WELLS_DRAW_CAP, ",")))
        except Exception:
            pass
        _say("[map] well draw cap: %s in scope, drawing %s"
             % (format(_shown, ","), format(_WELLS_DRAW_CAP, ",")))

    sc = df["well_status"].astype(str).str.upper().fillna("UNKNOWN")

    # Vectorized prep — every column to a flat list, no iterrows()
    colors = sc.map(STATUS_COLORS).fillna("#888780").tolist()
    statuses = sc.tolist()
    lats     = pd.to_numeric(df["lat"], errors="coerce").tolist()
    lons     = pd.to_numeric(df["lon"], errors="coerce").tolist()
    uwis     = df["uwi"].astype(str).tolist()
    names    = df["well_name"].fillna("").astype(str).tolist()
    ops      = df["operator_name"].fillna("—").astype(str).tolist()
    fields   = df["field_name"].fillna("—").astype(str).tolist()
    spuds    = df["spud_date"].fillna("—").astype(str).str[:10].tolist()
    tds      = df["final_td"].apply(
        lambda v: f"{float(v):,.0f} ft" if pd.notna(v) else "—").tolist()
    # Enrichment columns (added by the caller). Default safely if absent so
    # _add_wells still works when called without enrichment.
    if "well_path" in df.columns:
        paths = df["well_path"].fillna("").astype(str).tolist()
    else:
        paths = [""] * len(df)
    if "cum_oil" in df.columns:
        oils = pd.to_numeric(df["cum_oil"], errors="coerce").fillna(0).tolist()
    else:
        oils = [0.0] * len(df)
    if "cum_gas" in df.columns:
        gases = pd.to_numeric(df["cum_gas"], errors="coerce").fillna(0).tolist()
    else:
        gases = [0.0] * len(df)

    # Flat per-well data array — passed to JS as a single JSON payload
    data = []
    for i in range(len(df)):
        if lats[i] is None or lons[i] is None:
            continue
        try:
            if pd.isna(lats[i]) or pd.isna(lons[i]):
                continue
        except (TypeError, ValueError):
            continue
        data.append([
            float(lats[i]), float(lons[i]),
            colors[i], uwis[i], names[i], statuses[i],
            ops[i], fields[i], spuds[i], tds[i],
            paths[i], float(oils[i]), float(gases[i])
        ])

    if not data:
        return

    # JS callback — one function per well at marker construction time.
    # Builds the popup HTML on the JS side from the flat data array, embedding
    # data-uwi for the Python click parser. When usePPDM, the marker is a PPDM
    # well symbol (divIcon SVG) keyed on status; otherwise a coloured circle.
    callback = """
        function(row) {
            var usePPDM = __PPDM__;
            var lat=row[0], lon=row[1], color=row[2], uwi=row[3],
                name=row[4], status=row[5], op=row[6],
                field=row[7], spud=row[8], td=row[9],
                path=row[10], oil=row[11], gas=row[12];
            function ppdmSvg(st, c) {
                var s = (st||'').toUpperCase(), inner;
                if (s==='ACTIVE' || s==='COMPLETED') {
                    inner = "<circle cx='9' cy='9' r='5' fill='"+c+"' stroke='#fff' stroke-width='1'/>";
                } else if (s==='SHUT_IN' || s==='SUSPENDED') {
                    inner = "<circle cx='9' cy='9' r='5' fill='"+c+"' stroke='#fff' stroke-width='1'/>"
                          + "<rect x='8' y='3.5' width='2' height='11' fill='#fff'/>";
                } else if (s==='ABANDONED') {
                    inner = "<circle cx='9' cy='9' r='5.5' fill='none' stroke='"+c+"' stroke-width='1.6'/>"
                          + "<line x1='5' y1='5' x2='13' y2='13' stroke='"+c+"' stroke-width='1.6'/>"
                          + "<line x1='13' y1='5' x2='5' y2='13' stroke='"+c+"' stroke-width='1.6'/>";
                } else if (s==='DRILLING') {
                    inner = "<polygon points='9,3 15,15 3,15' fill='none' stroke='"+c+"' stroke-width='1.6'/>";
                } else if (s==='PERMITTED') {
                    inner = "<circle cx='9' cy='9' r='5' fill='none' stroke='"+c+"' stroke-width='1.6'/>";
                } else if (s==='MONITORING') {
                    inner = "<rect x='4' y='4' width='10' height='10' fill='"+c+"' stroke='#fff' stroke-width='1'/>";
                } else {
                    inner = "<circle cx='9' cy='9' r='5' fill='none' stroke='"+c+"' stroke-width='1.4'/>";
                }
                return "<svg width='18' height='18' viewBox='0 0 18 18' "
                     + "xmlns='http://www.w3.org/2000/svg'>"+inner+"</svg>";
            }
            // Year drilled from the spud date (first 4 chars of YYYY-MM-DD).
            var year = (spud && spud.length >= 4 && spud.charAt(0) !== '—')
                       ? spud.substring(0, 4) : '—';
            // Production-to-date string — only when there's something.
            var prod = '';
            if (oil > 0 || gas > 0) {
                var parts = [];
                if (oil > 0) parts.push(Math.round(oil).toLocaleString() + ' bbl oil');
                if (gas > 0) parts.push(Math.round(gas).toLocaleString() + ' Mcf gas');
                prod = parts.join(', ');
            }
            var popup = '<div data-uwi="' + uwi + '" '
                + 'style="font-size:11px;line-height:1.4;padding:0">'
                + '<b style="font-size:12px;color:#0f172a">' + name + '</b><br>'
                + '<span style="font-family:monospace;font-size:10px;color:#888">'
                + uwi + '</span><br>'
                + '<span style="color:#475569;font-size:10px">' + op + '</span><br>'
                + '<span style="color:#475569;font-size:10px">' + field + '</span><br>'
                + '<b style="color:' + color + ';font-size:10px">' + status + '</b><br>'
                + '<span style="font-size:10px;color:#475569">TD ' + td + '</span><br>'
                + '<span style="font-size:10px;color:#475569">Year drilled ' + year + '</span><br>'
                + (path ? '<span style="font-size:10px;color:#475569">' + path + '</span><br>' : '')
                + (prod ? '<span style="font-size:10px;color:#166534">Production: ' + prod + '</span><br>' : '')
                + '<span style="font-size:10px;color:#1a73e8;font-weight:600">'
                + '📋 Open Results for Scout Tickets<br>'
                + 'and to export data'
                + '</span>'
                + '</div>';
            var marker;
            if (usePPDM) {
                var icon = L.divIcon({html: ppdmSvg(status, color),
                                      className: '', iconSize: [18, 18],
                                      iconAnchor: [9, 9]});
                marker = L.marker(new L.LatLng(lat, lon), {icon: icon});
            } else {
                marker = L.circleMarker(
                    new L.LatLng(lat, lon),
                    {radius:5, fillColor:color, color:'#ffffff',
                     weight:1, opacity:1, fillOpacity:0.88}
                );
            }
            marker.bindPopup(popup, {maxWidth:300});
            // Tooltip: UWI, well-name, TD, year drilled, vertical/directional,
            // production-to-date (when present). Empty fields are skipped.
            var ttName = (name && name.trim()) ? name : uwi;
            var ttLines = ['<b>' + ttName + '</b>'];
            ttLines.push('<span style="font-family:monospace;font-size:10px">'
                         + uwi + '</span>');
            ttLines.push('TD ' + td);
            ttLines.push('Year ' + year);
            if (path) ttLines.push(path);
            if (prod) ttLines.push('Prod: ' + prod);
            marker.bindTooltip(
                ttLines.join('<br>'),
                {sticky:true, direction:'top', offset:[0,-5]}
            );
            return marker;
        }
    """
    callback = callback.replace("__PPDM__", "true" if ppdm else "false")

    try:
        from folium.plugins import FastMarkerCluster
        FastMarkerCluster(
            data,
            callback=callback,
            name="🛢 Wells",
            options={
                # Clustering removed — render every well as an individual
                # marker at all zoom levels. We keep FastMarkerCluster only
                # for its efficient JS-callback / chunked rendering of the
                # marker set; the cluster behavior itself is switched off
                # (maxClusterRadius=1 + disableClusteringAtZoom=1 means
                # markers never group). Density now comes from the H3 grid,
                # not cluster bubbles.
                "maxClusterRadius":          1,     # never group markers
                "disableClusteringAtZoom":   1,     # individual at every zoom
                "spiderfyOnMaxZoom":         False,
                "showCoverageOnHover":       False,
                "zoomToBoundsOnClick":       False,
                "chunkedLoading":            True,  # render incrementally
            },
        ).add_to(m)
    except ImportError:
        # Graceful fallback if folium.plugins unavailable — uncluster everything
        st.warning("FastMarkerCluster unavailable; falling back to individual markers.")
        fg = folium.FeatureGroup(name="🛢 Wells", show=True)
        for r in data:
            (lat, lon, color, uwi, name, status, op, field, spud, td,
             path, oil, gas) = r
            year = spud[:4] if (spud and len(spud) >= 4 and spud[0] != "—") else "—"
            prod = ""
            _pp = []
            if oil > 0:
                _pp.append(f"{round(oil):,} bbl oil")
            if gas > 0:
                _pp.append(f"{round(gas):,} Mcf gas")
            prod = ", ".join(_pp)
            popup_html = (
                f'<div data-uwi="{uwi}" '
                f'style="font-size:11px;line-height:1.4;padding:0">'
                f'<b style="font-size:12px;color:#0f172a">{name}</b><br>'
                f'<span style="font-family:monospace;font-size:10px;color:#888">'
                f'{uwi}</span><br>'
                f'<span style="color:#475569;font-size:10px">{op}</span><br>'
                f'<span style="color:#475569;font-size:10px">{field}</span><br>'
                f'<b style="color:{color};font-size:10px">{status}</b><br>'
                f'<span style="font-size:10px;color:#475569">TD {td}</span><br>'
                f'<span style="font-size:10px;color:#475569">Year drilled {year}</span><br>'
                + (f'<span style="font-size:10px;color:#475569">{path}</span><br>' if path else "")
                + (f'<span style="font-size:10px;color:#166534">Production: {prod}</span><br>' if prod else "")
                + f'<span style="font-size:10px;color:#1a73e8;font-weight:600">'
                f'📋 Open Results for Scout Tickets<br>'
                f'and to export data</span>'
                f'</div>'
            )
            _tt = [f"<b>{name or uwi}</b>",
                   f'<span style="font-family:monospace;font-size:10px">{uwi}</span>',
                   f"TD {td}", f"Year {year}"]
            if path:
                _tt.append(path)
            if prod:
                _tt.append(f"Prod: {prod}")
            if ppdm:
                _icon = folium.DivIcon(
                    html=_ppdm_symbol_svg(status, color, size=18),
                    icon_size=(18, 18), icon_anchor=(9, 9))
                folium.Marker(
                    location=[lat, lon], icon=_icon,
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip="<br>".join(_tt),
                ).add_to(fg)
            else:
                folium.CircleMarker(
                    location=[lat, lon], radius=5, color="#ffffff", weight=1,
                    fill=True, fill_color=color, fill_opacity=0.88, opacity=1,
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip="<br>".join(_tt),
                ).add_to(fg)
        fg.add_to(m)


def _add_viewport_wells(m, df, viewport_uwis, ppdm=False):
    """
    Render INDIVIDUAL clickable markers for wells inside a user-drawn viewport,
    on top of the existing cluster layer. Triggered when the user draws a rectangle.

    These markers are interactive (click → popup → scout ticket) — unlike the
    underlying cluster bubbles which are passive density indicators.

    Args:
        m: folium.Map
        df: full wells DataFrame (filtered by current top-level filters)
        viewport_uwis: set/list of UWI strings to render as individual markers
        ppdm: when True, draw PPDM/API status symbols (shape = status) with a
              yellow glow instead of a yellow-ringed dot.
    """
    if df.empty or not viewport_uwis:
        return 0

    vp_set = set(viewport_uwis)
    sub = df[df["uwi"].astype(str).isin(vp_set)].reset_index(drop=True)
    if sub.empty:
        return 0

    sc = sub["well_status"].astype(str).str.upper().fillna("UNKNOWN")
    fg = folium.FeatureGroup(name=f"📍 Viewport selection ({len(sub)})", show=True)

    for _i, row in sub.iterrows():
        try:
            lat = float(row["lat"])
            lon = float(row["lon"])
        except (TypeError, ValueError):
            continue

        uwi    = row.get("uwi", "")
        # Coerce row values through pd.notna — pandas returns NaN floats for
        # empty cells, and `NaN or "default"` returns NaN (NaN is truthy as
        # a float). Using a helper guarantees clean strings everywhere.
        def _s(v, default=""):
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return default
            return str(v).strip() or default
        name   = _s(row.get("well_name"))
        status = sc.iat[_i]
        color  = STATUS_COLORS.get(status, "#888780")
        op     = _s(row.get("operator_name"), "—")
        field  = _s(row.get("field_name"), "—")
        spud   = _s(row.get("spud_date"), "—")[:10]
        ftd    = row.get("final_td")
        td     = f"{float(ftd):,.0f} ft" if pd.notna(ftd) else "—"

        popup_html = (
            f"<div data-uwi=\"{uwi}\" "
            f"style='font-size:11px;line-height:1.4;padding:0'>"
            f"<b style='font-size:12px;color:#0f172a'>{name}</b><br>"
            f"<span style='font-family:monospace;font-size:10px;color:#888'>"
            f"{uwi}</span><br>"
            f"<span style='color:#475569;font-size:10px'>{op}</span><br>"
            f"<span style='color:#475569;font-size:10px'>{field}</span><br>"
            f"<b style='color:{color};font-size:10px'>{status}</b><br>"
            f"<span style='font-size:10px;color:#475569'>Spud {spud}</span><br>"
            f"<span style='font-size:10px;color:#475569'>TD {td}</span><br>"
            f"<span style='font-size:10px;color:#1a73e8;font-weight:600'>"
            f"📋 Open Results for Scout Tickets<br>"
            f"and to export data</span>"
            f"</div>"
        )

        # Slightly larger radius (6 vs 5) and brighter outline so viewport
        # markers stand out from any background cluster bubbles
        # Build a richer tooltip: name + operator + status, joined with line
        # breaks. The _s() helper above already coerced NaN/None to clean
        # strings ("" or "—"), so we just need a truthy check + "—" exclusion.
        _tt_lines = [f"<b>{name or uwi}</b>"]
        if op and op != "—":
            _tt_lines.append(op)
        if status:
            _tt_lines.append(f"<i>{status}</i>")
        _tooltip_html = "<br>".join(_tt_lines)

        if ppdm:
            # PPDM/API status symbol (shape = status) with a yellow glow so the
            # drilled selection still reads as selected.
            _svg = _ppdm_symbol_svg(status, color, size=18)
            _icon_html = (
                f"<div style='filter:drop-shadow(0 0 2px #ffeb3b) "
                f"drop-shadow(0 0 1px #ffeb3b);' data-uwi=\"{uwi}\">{_svg}</div>"
            )
            folium.Marker(
                location=[lat, lon],
                icon=folium.DivIcon(html=_icon_html,
                                    icon_size=(18, 18), icon_anchor=(9, 9)),
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=folium.Tooltip(_tooltip_html, sticky=True),
            ).add_to(fg)
        else:
            folium.CircleMarker(
                location=[lat, lon],
                radius=6,
                color="#ffeb3b",   # yellow ring — distinguishes "in viewport"
                weight=2,
                fill=True,
                fill_color=color,
                fill_opacity=0.95,
                opacity=1,
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=folium.Tooltip(_tooltip_html, sticky=True),
            ).add_to(fg)

    fg.add_to(m)
    return len(sub)


# ── GOM well marker rendering ────────────────────────────────────────────────
# REFACTOR: _build_gom_popup_html mirrors the inline popup HTML in
# _add_viewport_wells; _add_gom_wells_markers mirrors _add_viewport_wells
# itself. Both should consolidate with a generic well-marker renderer once
# we have a second per-region pattern in place.

def _build_gom_popup_html(well: dict) -> str:
    """
    Build the popup HTML for one GOM well.

    Designed to mirror the dv_well popup visual style while showing
    GOM-specific fields. The data-well-id attribute lets the click handler
    extract the GOM well's UUID (parallel to data-uwi for dv_well wells).

    Eight fields visible per the popup spec:
      1. Well name + Suffix
      2. BOEM API number
      3. Operator (company_name)
      4. Lease (surface + bottom area/block)
      5. Spud date
      6. Water depth (ft)
      7. Total depth MD / TVD (ft)
      8. Status code / Type code
    """
    well_id = well.get("well_id", "")
    name    = well.get("well_name") or "—"
    suffix  = well.get("well_name_suffix") or ""
    api     = well.get("api_well_number") or "—"
    op      = well.get("company_name") or "—"
    sl      = well.get("surface_lease_number") or "—"
    bl      = well.get("bottom_lease_number") or ""
    area    = well.get("bottom_area_code") or ""
    block   = (well.get("bottom_block_number") or "").strip()
    spud    = str(well.get("spud_date") or "—")[:10]

    # Numeric formatting with NaN protection
    def _fmt_ft(v):
        try:
            f = float(v)
            return f"{f:,.0f} ft" if f == f else "—"  # f==f filters NaN
        except (TypeError, ValueError):
            return "—"
    wd_ft   = _fmt_ft(well.get("water_depth_ft"))
    md_ft   = _fmt_ft(well.get("bh_total_md_ft"))
    tvd_ft  = _fmt_ft(well.get("tvd_ft"))

    # Compose lease label — show area/block if available
    lease_label = sl
    if area or block:
        lease_label = f"{sl} ({area} {block})".strip()

    status  = well.get("status_code") or "—"
    wtype   = well.get("type_code") or "—"

    # Friendly title — combine name + suffix when present
    title = f"{name} {suffix}".strip() if suffix else name

    # Color the status badge — teal for GOM (matches the layer's palette)
    return (
        f"<div data-well-id=\"{well_id}\" data-source=\"gom\" "
        f"style='font-size:11px;line-height:1.4;padding:0'>"
        f"<b style='font-size:12px;color:#0f172a'>🛢 {title}</b><br>"
        f"<span style='font-family:monospace;font-size:10px;color:#888'>"
        f"API {api}</span><br>"
        f"<span style='color:#475569;font-size:10px'>{op}</span><br>"
        f"<span style='color:#475569;font-size:10px'>Lease {lease_label}</span><br>"
        f"<b style='color:#0f766e;font-size:10px'>{status} · {wtype}</b><br>"
        f"<span style='font-size:10px;color:#475569'>Spud {spud}</span><br>"
        f"<span style='font-size:10px;color:#475569'>"
        f"WD {wd_ft} · MD {md_ft} · TVD {tvd_ft}</span>"
        f"</div>"
    )


def _add_gom_wells_markers(m, wells: list[dict]) -> int:
    """
    Render individual clickable CircleMarkers for drilled GOM wells.

    Called after a cell-Commit or circle-drill against GOM. Each marker
    is a circle filled with its status color (see BOEM_STATUS_COLORS)
    and an amber ring — the ring is the constant "drilled / interactive"
    cue, the fill tells you the well's status at a glance. The sidebar
    status checkboxes carry matching color swatches, so the sidebar
    doubles as the map legend.

    The popup uses _build_gom_popup_html which embeds data-well-id (the
    GOM UUID) so the click handler can identify which well was clicked.

    Args:
        m:     folium.Map
        wells: list of dicts as returned by _qry_gom_wells_in_bbox or
               _qry_gom_wells_in_circle

    Returns:
        number of markers rendered (for status caption)
    """
    if not wells:
        return 0

    fg = folium.FeatureGroup(
        name=f"🛢 GOM Wells Selection ({len(wells):,})",
        show=True,
    )

    rendered = 0
    for w in wells:
        try:
            lat = float(w["lat"])
            lon = float(w["lon"])
        except (TypeError, ValueError, KeyError):
            continue

        popup_html = _build_gom_popup_html(w)
        title      = w.get("well_name") or w.get("api_well_number") or "—"

        # Fill color is driven by the well's BOEM status_code. Unknown
        # or missing codes fall back to neutral slate inside
        # _boem_status_color.
        _fill = _boem_status_color(w.get("status_code", ""))

        folium.CircleMarker(
            location=[lat, lon],
            radius=6,
            color="#fbbf24",       # amber/gold ring — drilled marker
            weight=2,
            fill=True,
            fill_color=_fill,      # status color — see BOEM_STATUS_COLORS
            fill_opacity=0.9,
            opacity=1,
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=title,
        ).add_to(fg)
        rendered += 1

    fg.add_to(m)
    return rendered


# Trajectory simplification tuning. A GOM directional survey can carry
# hundreds to >3,700 stations per wellbore. Feeding every raw station to
# folium means streamlit-folium serializes tens of thousands of
# coordinate pairs into the map JS and the browser parses + renders all
# of them — that's the minute-plus stall on a 209-well draw, NOT the DB
# query (single indexed query, see _qry_gom_trajectories).
#
# Real GOM survey data splits into two populations:
#   - Vertical / near-vertical wells: thousands of stations occupying a
#     map footprint of only a few metres. These genuinely have no shape
#     to preserve at map zoom — collapsing them to ~2 points is correct.
#   - Deviated wells: 2-3 km of lateral reach with real doglegs and build
#     curves that the overlay exists to show.
# A single fixed tolerance can't serve both — it either flattens the
# deviated wells or fails to reduce the vertical ones. So tolerance is
# ADAPTIVE: each wellbore is simplified relative to its own bounding-box
# diagonal (a small %), with an absolute floor so a sub-metre vertical
# well doesn't get a degenerate near-zero tolerance.
#
# Douglas-Peucker keeps the *shape* (kickoff, build, doglegs, lateral)
# and drops redundant collinear vertices. _MAX_TRAJ_VERTICES is a hard
# safety cap so a pathologically noisy survey still can't blow up one
# polyline even if simplification can't reduce it enough.
_TRAJ_SIMPLIFY_FRAC  = 0.002      # tolerance = 0.2% of wellbore diagonal
_TRAJ_SIMPLIFY_FLOOR = 0.5        # absolute tolerance floor, in metres
_MAX_TRAJ_VERTICES   = 250        # hard per-wellbore vertex cap
_DEG_PER_M           = 1.0 / 111_320.0


def _adaptive_tol(points: list) -> float:
    """Per-wellbore Douglas-Peucker tolerance, in degrees.

    Scales to the wellbore's own extent: tolerance is _TRAJ_SIMPLIFY_FRAC
    of the lat/lon bounding-box diagonal, floored at _TRAJ_SIMPLIFY_FLOOR
    metres. A vertical well (tiny diagonal) gets the floor and collapses
    hard; a 2 km deviated well gets tens of metres and keeps its shape.
    """
    if len(points) < 3:
        return _TRAJ_SIMPLIFY_FLOOR * _DEG_PER_M
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    diag_deg = math.hypot(max(lats) - min(lats), max(lons) - min(lons))
    diag_m   = diag_deg / _DEG_PER_M
    tol_m    = max(diag_m * _TRAJ_SIMPLIFY_FRAC, _TRAJ_SIMPLIFY_FLOOR)
    return tol_m * _DEG_PER_M


def _perp_dist(pt, line_start, line_end) -> float:
    """Perpendicular distance from `pt` to the segment line_start→line_end.

    All points are (lat, lon). Treated as planar — fine for the small
    spans a single wellbore covers; the error from ignoring curvature is
    far below the simplification tolerance.
    """
    (y0, x0), (y1, x1), (y2, x2) = pt, line_start, line_end
    dy, dx = (y2 - y1), (x2 - x1)
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0.0:
        # Degenerate segment — start == end. Fall back to point distance.
        return math.hypot(y0 - y1, x0 - x1)
    # Cross-product magnitude / segment length = perpendicular distance.
    return abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / math.sqrt(seg_len_sq)


def _douglas_peucker(points: list, tol: float) -> list:
    """Iterative Douglas-Peucker line simplification.

    Returns a subset of `points` (always keeps the first and last) such
    that no dropped point sat more than `tol` from the simplified line.
    Iterative (explicit stack) rather than recursive so a long survey
    can't hit Python's recursion limit.
    """
    n = len(points)
    if n < 3:
        return list(points)

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        # Find the point farthest from the start→end chord.
        max_d, max_i = -1.0, -1
        a, b = points[start], points[end]
        for i in range(start + 1, end):
            d = _perp_dist(points[i], a, b)
            if d > max_d:
                max_d, max_i = d, i
        if max_d > tol:
            keep[max_i] = True
            stack.append((start, max_i))
            stack.append((max_i, end))

    return [points[i] for i in range(n) if keep[i]]


def _cap_vertices(points: list, cap: int) -> list:
    """Hard safety cap — if a wellbore still has more than `cap` points
    after Douglas-Peucker, evenly decimate down to `cap`. Always keeps
    the first and last station so surface and bottom-hole stay anchored.
    """
    n = len(points)
    if n <= cap:
        return points
    # Even stride sample, then force-include the last point.
    step = n / float(cap)
    idx = sorted(set(int(i * step) for i in range(cap)) | {n - 1})
    return [points[i] for i in idx]


@st.cache_data(ttl=300, show_spinner=False)
def _qry_gom_trajectories(_engine, well_ids: tuple) -> dict:
    """Survey-point trajectories for a set of GOM well_ids.

    Returns {well_id: [(lat, lon), ...]} — one ordered coordinate list
    per wellbore, stations ordered by measured depth. Each wellbore
    (including each sidetrack, which has its own well_id) is its own
    entry, so the renderer draws them as separate polylines.

    Cached on the well_id tuple — re-drilling the same set is free.
    Only points with both coordinates present are included; a survey
    station with a null lat/lon is skipped rather than breaking the line.

    Each wellbore's coordinate list is Douglas-Peucker simplified (and
    hard-capped at _MAX_TRAJ_VERTICES) BEFORE it's returned, so both the
    cache payload and the folium serialization stay small. A summary of
    the reduction is printed so the effect is visible, not silent.
    """
    if not well_ids:
        return {}
    # Parameterize the IN-list. well_ids is a tuple of UUID strings.
    _params = {f"w{i}": str(w) for i, w in enumerate(well_ids)}
    _in = ", ".join(f":{k}" for k in _params)
    sql = f"""
        SELECT CONVERT(VARCHAR(36), well_id) AS well_id,
               CAST(latitude  AS FLOAT) AS lat,
               CAST(longitude AS FLOAT) AS lon
        FROM dataview_gom.directional_survey_point
        WHERE well_id IN ({_in})
          AND latitude  IS NOT NULL
          AND longitude IS NOT NULL
        ORDER BY well_id, survey_point_md
    """
    out: dict = {}
    try:
        with _engine.connect().execution_options(timeout=30) as con:
            for r in con.execute(text(sql), _params):
                out.setdefault(r.well_id, []).append((r.lat, r.lon))
    except Exception as exc:
        st.warning(f"GOM trajectory query failed: {exc}")
        return {}

    # ── Simplify each wellbore before returning ──────────────────────
    # Raw survey stations are mostly collinear; Douglas-Peucker keeps the
    # shape and drops the redundancy. Tolerance is ADAPTIVE per wellbore
    # (see _adaptive_tol) — a vertical well collapses hard, a deviated
    # well keeps its curve. _cap_vertices is the hard backstop.
    # Single-station wellbores (len < 2) are left alone — the renderer
    # already skips anything under 2 points.
    _raw_total = 0
    _simp_total = 0
    for _wid, _pts in out.items():
        _raw_total += len(_pts)
        if len(_pts) < 2:
            _simp_total += len(_pts)
            continue
        _tol = _adaptive_tol(_pts)
        _simplified = _douglas_peucker(_pts, _tol)
        _simplified = _cap_vertices(_simplified, _MAX_TRAJ_VERTICES)
        out[_wid] = _simplified
        _simp_total += len(_simplified)

    # Visible, not silent — so the reduction is verifiable at a glance.
    if _raw_total:
        _pct = 100.0 * (1.0 - _simp_total / _raw_total)
        print(f"[GOM trajectories] {len(out):,} wellbores: "
              f"{_raw_total:,} raw stations → {_simp_total:,} points "
              f"({_pct:.1f}% reduction)")

    return out


def _add_gom_trajectories(m, wells: list[dict], engine) -> int:
    """Draw wellbore trajectory polylines for drilled GOM wells.

    Each wellbore is one polyline following its survey stations from
    surface to bottom hole. Sidetracks have their own well_id and survey
    rows, so they render as their own paths branching near the parent —
    no special branch logic needed.

    Path A design: only the currently-drilled wells get trajectories
    drawn, so the well set is always bounded by the drill. No reliance
    on a precomputed simplified-polyline table.

    Args:
        m:      folium.Map
        wells:  the drilled GOM well dicts (viewport_gom_wells)
        engine: SQLAlchemy engine

    Returns:
        number of trajectories drawn (for the status caption)
    """
    if not wells or engine is None:
        return 0

    # Collect well_ids from the drilled set. The dicts use "well_id"
    # (circle/bbox query shape) — fall back to "uwi" just in case.
    _wids = []
    for w in wells:
        _wid = str(w.get("well_id") or w.get("uwi") or "").strip()
        if _wid:
            _wids.append(_wid)
    if not _wids:
        return 0

    traj = _qry_gom_trajectories(engine, tuple(sorted(set(_wids))))
    if not traj:
        return 0

    fg = folium.FeatureGroup(
        name=f"🌀 GOM Trajectories ({len(traj):,})",
        show=True,
    )
    # well_id → well dict, for the polyline tooltip
    _by_id = {str(w.get("well_id") or w.get("uwi") or "").strip(): w
              for w in wells}

    drawn = 0
    for wid, coords in traj.items():
        # A polyline needs at least two points. A single-station well
        # (rare) has nothing to draw as a line.
        if len(coords) < 2:
            continue
        _w = _by_id.get(wid, {})
        _name = _w.get("well_name") or _w.get("api_well_number") or wid
        _sfx  = _w.get("well_name_suffix") or ""
        _label = f"{_name} {_sfx}".strip() if _sfx else _name
        folium.PolyLine(
            locations=coords,
            color="#06b6d4",     # cyan — distinct from the amber-ring markers
            weight=2,
            opacity=0.85,
            tooltip=f"{_label} — {len(coords):,} stations",
        ).add_to(fg)
        drawn += 1

    fg.add_to(m)
    return drawn


def _add_trajectories(m, df):
    """
    Render onshore well trajectories (directional surveys) as a cyan
    GeoJSON line layer.

    Args:
        m:  folium.Map
        df: trajectories DataFrame (from _qry_trajectories); converted to a
            FeatureCollection via _trajectory_geojson.
    """
    if df.empty:
        return
    gj = _trajectory_geojson(df)
    if not gj["features"]:
        return
    folium.GeoJson(
        gj, name="📐 Well Trajectories",
        style_function=lambda _: {"color":"#00BCD4","weight":2,"opacity":0.8},
        tooltip=folium.GeoJsonTooltip(
            fields=["well_name","stations","td_ft"],
            aliases=["Well","Stations","TD (ft)"], sticky=True),
        popup=folium.GeoJsonPopup(
            fields=["well_name","uwi","stations","td_ft"],
            aliases=["Well","UWI","Stations","TD MD (ft)"], max_width=280),
    ).add_to(m)


def _add_survey_sticks(m, df) -> int:
    """
    Draw a STRAIGHT line from surface to bottomhole (TD) for each onshore well
    that has a directional survey. The bottomhole is the surface offset by the
    deepest station's NS/EW offset. Vertical wells (no horizontal displacement)
    are skipped — a zero-length stick has nothing to show on a 2-D map.

    Args:
        m:  folium.Map
        df: result of _qry_survey_sticks (one row per well).

    Returns:
        number of sticks drawn.
    """
    if df is None or df.empty:
        return 0
    fg = folium.FeatureGroup(name="➖ Surface→TD sticks", show=True)
    n = 0
    for _, r in df.iterrows():
        try:
            slat = float(r["surf_lat"]); slon = float(r["surf_lon"])
        except (TypeError, ValueError):
            continue
        blat, blon = _offset_to_latlon(
            slat, slon, float(r["ns_offset"] or 0), float(r["ew_offset"] or 0))
        if abs(blat - slat) < 1e-9 and abs(blon - slon) < 1e-9:
            continue  # vertical well — surface == bottomhole in plan view
        _td = r.get("md")
        _td_str = (f"{float(_td):,.0f} ft"
                   if _td is not None and not pd.isna(_td) else "—")
        _name = str(r.get("well_name", "") or "")
        folium.PolyLine(
            [[slat, slon], [blat, blon]],
            color="#ff6f00", weight=2, opacity=0.85,
            tooltip=f"{_name} — TD {_td_str}",
        ).add_to(fg)
        # Small dot at the bottomhole end so the TD is locatable.
        folium.CircleMarker(
            [blat, blon], radius=2, color="#ff6f00", weight=1,
            fill=True, fill_color="#ff6f00", fill_opacity=0.9,
            tooltip=f"{_name} — TD {_td_str}",
        ).add_to(fg)
        n += 1
    fg.add_to(m)
    return n


def _add_gom_survey_sticks(m, wells: list[dict], engine) -> int:
    """
    Straight surface→TD sticks for GOM wells that have a directional survey.
    Scoped to the supplied wells (the drilled set), matching how GOM curved
    trajectories work. The bottomhole is the deepest survey point; the surface
    is the well's own surface lat/lon (falls back to the shallowest survey
    point if the well dict has no coordinates).
    """
    if not wells:
        return 0
    _wids = [str(w.get("well_id") or w.get("uwi"))
             for w in wells if (w.get("well_id") or w.get("uwi"))]
    if not _wids:
        return 0
    traj = _qry_gom_trajectories(engine, tuple(sorted(set(_wids))))
    if not traj:
        return 0
    _surf = {}
    for w in wells:
        _wid = str(w.get("well_id") or w.get("uwi") or "")
        _la, _lo = w.get("lat"), w.get("lon")
        if _wid and _la is not None and _lo is not None:
            try:
                _surf[_wid] = (float(_la), float(_lo))
            except (TypeError, ValueError):
                pass
    fg = folium.FeatureGroup(name="➖ GOM Surface→TD sticks", show=True)
    n = 0
    for _wid, _pts in traj.items():
        if not _pts:
            continue
        _bh = _pts[-1]
        _start = _surf.get(_wid, _pts[0])
        if (abs(_start[0] - _bh[0]) < 1e-9
                and abs(_start[1] - _bh[1]) < 1e-9):
            continue
        folium.PolyLine(
            [list(_start), list(_bh)],
            color="#ff6f00", weight=2, opacity=0.85,
        ).add_to(fg)
        folium.CircleMarker(
            list(_bh), radius=2, color="#ff6f00", weight=1,
            fill=True, fill_color="#ff6f00", fill_opacity=0.9,
        ).add_to(fg)
        n += 1
    fg.add_to(m)
    return n


def _add_formation_tops(m, df):
    if df.empty:
        return
    fg = folium.FeatureGroup(name="📏 Formation Tops", show=False)
    for _, row in df.iterrows():
        fluid = str(row.get("fluid_type") or "").upper()
        color = {"OIL":"#4CAF50","GAS":"#FF9800","OIL/GAS":"#CDDC39",
                 "WATER":"#2196F3"}.get(fluid, "#9C27B0")
        depth = row.get("top_depth")
        depth_str = f"{depth:,.0f} ft" if pd.notna(depth) else "—"
        net = row.get("net_thickness")
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=5, color=color, weight=1,
            fill=True, fill_color=color, fill_opacity=0.7,
            popup=folium.Popup(
                f"<b>{row.get('formation','—')}</b><br>"
                + _popup_table({
                    "Well":    row.get("well_name","—"),
                    "Top MD":  depth_str,
                    "Net Pay": f"{net:,.1f} ft" if pd.notna(net) else "—",
                    "Fluid":   fluid or "—",
                }), max_width=220),
            tooltip=f"{row.get('formation','?')} @ {depth_str}",
        ).add_to(fg)
    fg.add_to(m)


def _add_dst(m, df):
    if df.empty:
        return
    fg = folium.FeatureGroup(name="🧪 DST Intervals", show=False)
    for _, row in df.iterrows():
        result = str(row.get("test_result") or "").upper()
        color  = {"PRODUCTIVE":"#4CAF50","NON-PRODUCTIVE":"#E24B4A",
                  "GAS":"#FF9800","SHOWS":"#CDDC39"}.get(result, "#9C27B0")
        oil = row.get("max_oil_rate")
        gas = row.get("max_gas_rate")
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=8, color=color, weight=2,
            fill=True, fill_color=color, fill_opacity=0.6,
            popup=folium.Popup(
                f"<b>DST — {row.get('well_name','—')}</b><br>"
                + _popup_table({
                    "Type":     row.get("test_type","—"),
                    "Interval": f"{row.get('top_depth','?')} – {row.get('base_depth','?')} ft",
                    "Result":   result or "—",
                    "Oil":      f"{oil:,.0f} BOPD" if pd.notna(oil) and oil else "—",
                    "Gas":      f"{gas:,.0f} Mcf/d" if pd.notna(gas) and gas else "—",
                    "Date":     str(row.get("test_date",""))[:10],
                }), max_width=260),
            tooltip=f"DST: {result or '?'}",
        ).add_to(fg)
    fg.add_to(m)


def _add_production_bubbles(m, df):
    if df.empty:
        return
    fg = folium.FeatureGroup(name="📈 Production Bubbles", show=False)
    import math
    max_oil = float(df["cum_oil"].max() or 1)
    min_oil = float(df["cum_oil"].min() or 0)
    for _, row in df.iterrows():
        cum_oil = float(row.get("cum_oil") or 0)
        # Square root scaling gives better visual spread for narrow ranges
        if max_oil > min_oil:
            norm   = (cum_oil - min_oil) / (max_oil - min_oil)
        else:
            norm   = 0.5
        radius = 2 + math.sqrt(norm) * 7  # range: 2-9px
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=radius, color="#1B5E20", weight=1,
            fill=True, fill_color="#4CAF50", fill_opacity=0.55,
            popup=folium.Popup(
                f"<b>{row.get('well_name','—')}</b><br>"
                + _popup_table({
                    "Cum Oil":   f"{cum_oil:,.0f} bbl",
                    "Cum Gas":   f"{float(row.get('cum_gas') or 0):,.0f} Mcf",
                    "Cum Water": f"{float(row.get('cum_water') or 0):,.0f} bbl",
                    "Months":    str(int(row.get("months") or 0)),
                }), max_width=220),
            tooltip=f"Cum oil: {cum_oil:,.0f} bbl",
        ).add_to(fg)
    fg.add_to(m)


def _add_production_heatmap(m, df, weight="boe"):
    """
    Production-weighted heatmap for wells that have production data. Distinct
    from the H3 well-count density grid: here the intensity follows cumulative
    production volume, not how many wells fall in a cell.

    weight: 'oil' (cum bbl), 'gas' (cum Mcf), or 'boe' (oil bbl + gas Mcf/6).
    Returns the number of producing wells plotted (0 if none / plugin missing).
    """
    if df is None or df.empty:
        return 0
    try:
        from folium.plugins import HeatMap
    except Exception:
        st.warning("folium.plugins.HeatMap unavailable; skipping production heatmap.")
        return 0
    import math
    oil = pd.to_numeric(df.get("cum_oil"), errors="coerce").fillna(0.0)
    gas = pd.to_numeric(df.get("cum_gas"), errors="coerce").fillna(0.0)
    w = (weight or "boe").lower()
    if w == "oil":
        vals, label = oil, "Oil (bbl)"
    elif w == "gas":
        vals, label = gas, "Gas (Mcf)"
    else:  # 6 Mcf gas ≈ 1 boe
        vals, label = oil + gas / 6.0, "BOE"
    vals = vals.clip(lower=0)
    vmax = float(vals.max() or 0)
    if vmax <= 0:
        return 0
    lats = pd.to_numeric(df.get("lat"), errors="coerce")
    lons = pd.to_numeric(df.get("lon"), errors="coerce")
    pts = []
    for _lat, _lon, _v in zip(lats, lons, vals):
        if pd.isna(_lat) or pd.isna(_lon) or _v <= 0:
            continue
        # sqrt scaling so a handful of giant wells don't wash out the field
        pts.append([float(_lat), float(_lon), math.sqrt(_v / vmax)])
    if not pts:
        return 0
    fg = folium.FeatureGroup(name=f"🔥 Production Heatmap ({label})", show=True)
    HeatMap(
        pts,
        radius=18, blur=22, min_opacity=0.25, max_zoom=11,
        gradient={0.2: "#2c7fb8", 0.4: "#41b6c4", 0.6: "#a1dab4",
                  0.8: "#fecc5c", 1.0: "#e31a1c"},
    ).add_to(fg)
    fg.add_to(m)
    return len(pts)


def _add_fields(m, df):
    if df.empty:
        return
    fg = folium.FeatureGroup(name="🌿 Fields", show=False)
    for _, row in df.iterrows():
        folium.Marker(
            location=[row["lat"], row["lon"]],
            icon=folium.Icon(color="green", icon="leaf", prefix="fa"),
            popup=folium.Popup(
                f"<b>{row.get('field_name','—')}</b><br>"
                + _popup_table({"Type": row.get("field_type","—"),
                                "Country": row.get("country_code","—")}),
                max_width=200),
            tooltip=row.get("field_name","—"),
        ).add_to(fg)
    fg.add_to(m)


def _add_basins(m, df):
    if df.empty:
        return
    fg = folium.FeatureGroup(name="🏔 Basins", show=False)
    for _, row in df.iterrows():
        area = row.get("area_km2")
        folium.Marker(
            location=[row["lat"], row["lon"]],
            icon=folium.Icon(color="orange", icon="globe", prefix="fa"),
            popup=folium.Popup(
                f"<b>{row.get('basin_name','—')}</b><br>"
                + _popup_table({
                    "Type":    row.get("basin_type","—"),
                    "Country": row.get("country_code","—"),
                    "Area":    f"{area:,.0f} km²" if pd.notna(area) else "—",
                    "Play":    row.get("primary_play_type","—"),
                }), max_width=240),
            tooltip=row.get("basin_name","—"),
        ).add_to(fg)
    fg.add_to(m)


def _geog_linestring_pts(wkt):
    """LINESTRING WKT -> [[lat, lon], ...].

    SQL Server geography WKT is (lon lat) — X is longitude; folium wants
    (lat, lon). Plain string parse: no shapely dependency for a layer that
    must degrade to nothing, not to an ImportError.
    """
    s = str(wkt or "")
    if "(" not in s or not s.lstrip().upper().startswith("LINESTRING"):
        return []
    body = s[s.find("(") + 1:s.rfind(")")]
    pts = []
    for pair in body.split(","):
        bits = pair.split()
        if len(bits) >= 2:
            try:
                pts.append([float(bits[1]), float(bits[0])])
            except ValueError:
                continue
    return pts


def _popup_safe(s):
    r"""Text safe to interpolate into a folium popup.

    FOLIUM PUTS POPUP HTML INSIDE A BACKTICK TEMPLATE LITERAL, so anything
    the JS template parser treats specially detonates the whole script -- and
    a script that fails to parse renders no map at all: no basemap, no
    layers, a white rectangle, with no error in Python and none on the page.

    A WINDOWS PATH IS EXACTLY THAT. "C:\Bulk\Seismic\2D_SEGY" reaches the
    browser raw, and "\2" is an OCTAL escape, which is illegal in a template
    string. Node named it in one line -- "Octal escape sequences are not
    allowed in template strings" -- after every Python-side check had passed:
    the layer built, the counts were right, the HTML was produced.

    GeoJsonPopup does not need this; its values travel as JSON and are
    escaped on the way. Only hand-built popup HTML does.
    """
    return (str(s or "")
            .replace("\\", "\\\\")     # backslash FIRST, or it doubles the rest
            .replace("`", "\\`")
            .replace("${", "\\${"))


def _seis_candidates(lines, df3d=None):
    """Every catalogued seismic file that could be opened, one dict each.

    ONE SOURCE FOR BOTH DOORS. The click resolver and the chooser must agree
    about what exists; building the list twice is how they drift.
    """
    out = []
    for _l in (lines or []):
        _fp = str(_l.get("file") or "")
        if _fp:
            out.append({"path": _fp, "name": _l.get("file_name") or "",
                        "survey": _l.get("survey") or "", "line": _l.get("line") or "",
                        "epsg": _l.get("epsg"), "traces": _l.get("traces"),
                        "kind": "2D line",
                        "dim": "2D",
                        "stage": _seis_stage(_fp), "product": _seis_product(_fp),
                        "base": _seis_base_line(
                            _l.get("line") or _l.get("file_name") or "")})
    try:
        if df3d is not None and not df3d.empty and "file_path" in df3d.columns:
            for _r in df3d.to_dict("records"):
                _fp = str(_r.get("file_path") or "")
                if _fp:
                    out.append({"path": _fp, "name": _r.get("file_name") or "",
                                "survey": _r.get("survey_name") or "",
                                "line": _r.get("line_name") or "",
                                "epsg": _r.get("epsg_code"),
                                "traces": _r.get("trace_count"),
                                "kind": "3D survey",
                                "dim": "3D",
                                "stage": _seis_stage(_fp), "product": _seis_product(_fp),
                                "base": ""})
    except Exception:
        pass
    return out


def _seis_pick_from_popup(clicked, lines, df3d=None):
    """Which seismic FILE a map click landed on, or None.

    THE POPUP IS THE ONLY CHANNEL. streamlit-folium strips HTML attributes and
    returns visible text, so a data-* attribute never survives the round trip
    -- which is why the well markers have to regex a UWI out of prose, with a
    ladder of six patterns and a comment apologising for each.

    Seismic does not have to guess. The popup already SHOWS the full path and
    we hold the authoritative list of paths, so this MATCHES AGAINST A KNOWN
    SET rather than parsing an unknown string. Nothing is inferred: if no
    known path appears in the text, the answer is None and the caller says so.
    """
    if not clicked:
        return None
    txt = str(clicked)
    cands = _seis_candidates(lines, df3d)

    for _hit in sorted(cands, key=lambda h: -len(h["path"])):
        if _hit["path"] in txt:
            return _hit

    # THE 3D POPUP TRUNCATES ITS PATH AT 260 CHARACTERS, so a deep tree leaves
    # only the file NAME to match on -- and one name routinely sits inside
    # another ("x.segy" is a substring of "xx.segy"). Longest name first, or a
    # click on the longer opens the shorter: a real section from the wrong
    # file, with nothing on screen to say so.
    for _hit in sorted(cands, key=lambda h: -len(str(h.get("name") or ""))):
        _nm = str(_hit.get("name") or "")
        if _nm and _nm in txt:
            return _hit
    return None


def _seis_label(h):
    """One line of text identifying a seismic file in the chooser."""
    _bits = [x for x in (h.get("survey"), h.get("line")) if x]
    _lbl = " / ".join(_bits) or (h.get("name") or "(unnamed)")
    _sfx = h.get("name") or ""
    return f"{_lbl}  -  {_sfx}" if _sfx and _sfx not in _lbl else _lbl


# -- Seismic vocabulary, read out of the file name -----------------------
#
# THERE IS NO PROCESSING COLUMN. Not in dv_seis_line, not in dv_seis_set,
# not in FILE_SEIS_HEADER -- all 24 columns checked. Perry: "It's in the file
# name usually," and the corpus bears that out: 230 of 240 catalogued lines
# carry an unambiguous stage in their own name, on a consistent scheme
#
#     85-ZBF_PRESDM_PROCESSED_Stack_10KM.segy
#     tarata-m3d-pr5857-t-pstm-full-ss-agc.sgy
#
# so this READS a convention rather than guessing at one. The ten that carry
# no stage (lineA.sgy, f3.sgy, delft.sgy, the .p190 navigation files) are
# reported as "(not stated)" and never assigned one: a filter that invents a
# processing type puts a confident wrong label on a section, and a wrong label
# is worse than an absent one because it plots, exports and gets quoted.
_SEIS_STAGE = [
    (r"PRE[_\- ]?SDM|PSDM", "PreSDM (pre-stack depth)"),
    (r"PRE[_\- ]?STM|PSTM", "PreSTM (pre-stack time)"),
    (r"POST[_\- ]?M(?![A-Z])", "PostM (post-stack)"),
    (r"PRE[_\- ]?MIG", "Pre-migration"),
    (r"FINAL[_\- ]?MIGRATION|MIGRATION|[_\-]MIG(?![A-Z])", "Migrated"),
]
_SEIS_PRODUCT = [(r"PROCESSED", "Processed"), (r"RAW", "Raw")]
_SEIS_UNSTATED = "(not stated)"


def _seis_stage(name):
    """Imaging stage read out of a file name, or None. Order is significant:
    PRESDM and PRESTM must both be tested before the generic migration
    pattern, or every one of them reads as merely "Migrated"."""
    u = str(name or "").upper()
    for pat, lbl in _SEIS_STAGE:
        if re.search(pat, u):
            return lbl
    return None


def _seis_product(name):
    """RAW vs PROCESSED, or None when the name does not say."""
    u = str(name or "").upper()
    for pat, lbl in _SEIS_PRODUCT:
        if re.search(pat, u):
            return lbl
    return None


def _seis_base_line(line_name):
    """The line code with its processing suffix removed.

    dv_seis_line.line_name carries the processing in it --
    85-ZBF_PRESDM_PROCESSED_Stack_4S -- so a filter offering raw line names
    lists the same physical line eight times and is useless for finding it.
    Cutting at the first stage token collapses those to 85-ZBF: 240 names
    become 38 line codes, which is what someone looking for a line means.
    """
    u = str(line_name or "")
    m = re.search(r"[_\-](PRE[_\- ]?SDM|PRE[_\- ]?STM|POSTM|PRE[_\- ]?MIG|PSTM|PSDM)",
                  u, re.I)
    return (u[:m.start()] if m else u).strip("_- ") or u


def _seis_filter_box(label, key, values, help_text=None):
    """A cascading filter. Returns the chosen value, or None for "All".

    THE OPTIONS ARE DRAWN FROM WHAT SURVIVED THE FILTERS ABOVE, so no
    combination can select an empty set -- pick a survey and the line list is
    that survey's lines only.

    That is also why the stored value has to be released when it disappears
    from the options: Streamlit keeps a keyed widget's value across reruns, and
    a value no longer in the list leaves the box showing a selection the data
    can no longer honour. Popping it BEFORE the widget is drawn is the safe
    half of Streamlit scar #6 -- assigning a widget's own key AFTER it exists
    raises on a later run, on whatever page draws next.
    """
    opts = ["All"] + values
    if st.session_state.get(key) not in opts:
        st.session_state.pop(key, None)
    sel = st.selectbox(label, opts, key=key, help=help_text)
    return None if sel == "All" else sel


SEIS_GALLERY_MAX = 6
SEIS_GALLERY_TRACES = 240


def _seis_basket_add(paths, lines, df3d=None):
    """Put SEG-Y paths into the picks basket. Returns how many were new.

    ONE WRITER FOR THE BASKET. Two doors fill it now -- "the lines inside the
    shape I drew" and "the lines I ticked in the grid" -- and the area-add
    already had the right instinct in its own comment: go through
    _seis_candidates so the basket holds the same shape of dict however a
    line got into it, because two producers of one structure is how the two
    halves drift. That argument only gets stronger with a second producer, so
    the body moved here rather than being copied.

    Silently skips a path with no candidate behind it: the basket feeds a
    renderer that reads traces from a file, so an entry with no file is an
    entry that draws an error instead of a section.
    """
    _have = {str(x.get("path")) for x in
             (st.session_state.get("_seis_multi") or [])}
    _new = [str(p) for p in (paths or []) if p and str(p) not in _have]
    if not _new:
        return 0
    _by_path = {str(c.get("path")): c for c in _seis_candidates(lines, df3d)}
    _multi = list(st.session_state.get("_seis_multi") or [])
    _added = 0
    for _p in _new:
        _c = _by_path.get(_p)
        if _c:
            _multi.append(dict(_c))
            _added += 1
    if _added:
        st.session_state["_seis_multi"] = _multi
        # The last one added becomes the open section, so the panel shows
        # something rather than going quiet after a successful add.
        st.session_state["_seis_pick"] = dict(_multi[-1])
        st.session_state["_seis_basket_last"] = _seis_label(_multi[-1])
    return _added


def _render_seis_area_add(lines, df3d=None):
    """Add every 2D line inside the last drawn shape to the picks.

    THE CIRCLE ALREADY EXISTS and already means something: it drills WELLS.
    Quietly re-pointing it at seismic would break that for anyone using it,
    and a tool whose meaning depends on invisible state is the thing this
    map has repeatedly got wrong. So the shape keeps its job and this OFFERS
    the seismic reading of it -- draw a circle or a box, then take the lines
    in it if that is what you wanted.

    IT REUSES _drawn_bounds rather than re-reading all_drawings. That is
    already the extent the draw handler stored, already deduped against
    reprocessing, and already what the saved-view feature reads -- a second
    parse of the same drawing is a second thing to keep in step.

    BOUNDS, AND IT SAYS SO. A circle is stored as its bounding box, so a
    line clipping a corner counts. Saying "in the drawn area" rather than
    "in the circle" is the difference between a rounded answer and a wrong
    one.
    """
    _b = st.session_state.get("_drawn_bounds")
    if not _b or not lines:
        return
    try:
        _la0, _lo0 = float(_b[0][0]), float(_b[0][1])
        _la1, _lo1 = float(_b[1][0]), float(_b[1][1])
    except (TypeError, ValueError, IndexError):
        return
    _slo, _shi = min(_la0, _la1), max(_la0, _la1)
    _wlo, _whi = min(_lo0, _lo1), max(_lo0, _lo1)

    # ANY VERTEX INSIDE COUNTS. A 2D line is long and the box is small, so
    # requiring the whole line would select almost nothing -- what a person
    # circling part of a field means is "the lines through here".
    _hitpaths = []
    for _sl in lines:
        for _la, _lon in (_sl.get("pts") or []):
            if _slo <= _la <= _shi and _wlo <= _lon <= _whi:
                if _sl.get("file"):
                    _hitpaths.append(str(_sl["file"]))
                break
    if not _hitpaths:
        return

    _have = {str(x.get("path")) for x in
             (st.session_state.get("_seis_multi") or [])}
    _new = [p for p in _hitpaths if p not in _have]
    if not _new:
        st.caption("All %d line(s) in the drawn area are already picked."
                   % len(_hitpaths))
        return
    if st.button("Add the %d line(s) in the drawn area" % len(_new),
                 key="seis_area_add_btn", use_container_width=True):
        # THROUGH _seis_candidates, so the basket holds the same shape of
        # dict however a line got into it. Two producers of one structure
        # is how the two halves drift.
        _seis_basket_add(_new, lines, df3d)
        st.rerun()


def _render_seis_gallery(picks):
    """Every picked line's section, stacked down the page.

    THE SAME RENDERER THE SINGLE VIEW USES. file_viewer._segy_plot already
    draws density + wiggle from an array; a second "compact" plotter would
    be a parallel worse version of it that drifts the first time one of
    them gains a feature. This only reads the traces and hands them over.

    DECIMATED, because the cost here is real: N file reads and N matplotlib
    figures on a page that already takes ~25 s. Every SEIS_GALLERY_TRACES-th
    trace preserves the structure -- what a stacked view is FOR is comparing
    shape between lines, not reading a single trace -- and a line of 2,000
    traces costs the same as one of 240.

    CAPPED, AND IT SAYS SO. Silently drawing the first six of nine reads as
    three lines that failed.
    """
    _shown = [h for h in picks if h.get("path")][:SEIS_GALLERY_MAX]
    if len(picks) > len(_shown):
        st.info("Showing the first %d of %d. Clear some, or step through "
                "them with ◀ ▶." % (len(_shown), len(picks)))
    try:
        import segyio
        import numpy as np
        from dataview.file_catalog.file_viewer import _segy_plot
    except ImportError as _ie:
        st.error("Cannot draw sections: %s" % _ie)
        return
    for _gi, _h in enumerate(_shown):
        _hc, _hb = st.columns([5, 1])
        _hc.markdown("##### " + _seis_label(_h))
        # A PICTURE IS NOT A BUTTON. st.pyplot renders a static image, so
        # "click the section you want" cannot work however natural it
        # looks -- and the alternative on offer was to leave the gallery,
        # find the line again in the dropdown, and come back. One button
        # per section instead.
        #
        # KEY ENDS "_btn" so _is_action_key excludes it: the persist loop
        # self-assigns every settable key, a button cannot be set, and the
        # assignment raises on a LATER run on whatever page draws next.
        if _hb.button("Show only this", key="seis_gal_pick_%d_btn" % _gi,
                      use_container_width=True):
            st.session_state["_seis_pick"] = dict(_h)
            st.session_state["_seis_basket_last"] = _seis_label(_h)
            # A REQUEST FLAG, NOT THE WIDGET KEY. seis_basket_all belongs
            # to a checkbox drawn ABOVE this, so assigning it here is
            # setting a widget key after instantiation -- Streamlit scar
            # #6, and it raises on a later run far from the cause. The
            # flag is consumed next run BEFORE that checkbox is drawn,
            # which is the legal half of the same move.
            st.session_state["_seis_gal_close"] = True
            st.rerun()
        _p = str(_h.get("path") or "")
        if not os.path.exists(_p):
            st.warning("The catalogue points at a file that is not there "
                       "now: `%s`" % _p)
            continue
        # ONE BAD FILE MUST NOT TAKE THE PAGE. A gallery is the one place a
        # single unreadable SEG-Y would cost every other section on screen.
        try:
            with segyio.open(_p, ignore_geometry=True) as _f:
                _n = _f.tracecount
                _stepn = max(1, _n // SEIS_GALLERY_TRACES)
                _idxs = list(range(0, _n, _stepn))
                _data = np.stack([_f.trace[i] for i in _idxs]).T
                _samp = _f.samples
            if _stepn > 1:
                st.caption("every %d%s trace of %s"
                           % (_stepn,
                              {1: "st", 2: "nd", 3: "rd"}.get(_stepn % 10,
                                                              "th"),
                              format(_n, ",")))
            _segy_plot(_data, _samp, len(_idxs), _p)
        except Exception as _ge:
            st.error("Could not draw %s: %s"
                     % (_h.get("name") or _p, _ge))


def _render_seis_basket():
    """The lines collected by clicking the map, and what to do with them.

    ONE SECTION AT A TIME, DELIBERATELY. Four SEG-Y files rendered side by
    side is four file reads and four matplotlib figures on a page that
    already takes ~25 s to draw, and the second screen exists precisely so
    a section can be looked at properly rather than in a quarter of a
    column. So the basket chooses WHICH one is shown, and the whole set can
    be sent to the map or opened one at a time on the other monitor.

    Hidden entirely below two entries: one line is a pick, not a basket,
    and a control that appears for every click is clutter.
    """
    _multi = list(st.session_state.get("_seis_multi") or [])
    if len(_multi) < 2:
        return
    st.caption("%d lines picked from the map." % len(_multi))
    _labels = [_seis_label(h) for h in _multi]
    _cur = (st.session_state.get("_seis_pick") or {}).get("path")
    _idx = next((i for i, h in enumerate(_multi)
                 if str(h.get("path")) == str(_cur)), 0)
    _b1, _bp, _bn, _b2, _b3 = st.columns([3, 0.6, 0.6, 1, 1])
    _sel = _b1.selectbox("Showing", _labels, index=_idx,
                         key="seis_basket_sel",
                         label_visibility="collapsed")
    # STEP THROUGH THEM. Comparing sections means going back and forth, and
    # hunting the right entry in a dropdown each time is the wrong gesture
    # for it. Wraps, so the last one steps round to the first rather than
    # dead-ending on a disabled button.
    _step = 0
    if _bp.button("◀", key="seis_basket_prev_btn",
                  use_container_width=True, help="Previous line"):
        _step = -1
    if _bn.button("▶", key="seis_basket_next_btn",
                  use_container_width=True, help="Next line"):
        _step = 1
    if _step:
        _to = _multi[(_idx + _step) % len(_multi)]
        st.session_state["_seis_pick"] = dict(_to)
        # The dropdown is keyed and would otherwise still read the OLD
        # entry, whose act-on-change test would then fire and undo this
        # on the very next run. Move its remembered value with the pick.
        st.session_state["_seis_basket_last"] = _seis_label(_to)
        st.rerun()
    # ACT ON CHANGE ONLY, or this overrules the map click that just
    # arrived -- the same rule the main chooser follows, and for the same
    # reason: two doors onto one selection, most recent wins.
    if _sel != st.session_state.get("_seis_basket_last"):
        st.session_state["_seis_basket_last"] = _sel
        _hit = _multi[_labels.index(_sel)]
        if str(_hit.get("path")) != str(_cur):
            st.session_state["_seis_pick"] = dict(_hit)
            st.rerun()
    if _b2.button("Send to map", key="seis_basket_send_btn",
                  use_container_width=True,
                  help="Draw only these lines on the map."):
        _lines = sorted("%s|%s" % (h.get("survey"), h.get("line"))
                        for h in _multi if h.get("line"))
        _survs = sorted({str(h.get("survey")) for h in _multi})
        _p = _load_user_prefs()
        _p[MAP_SEIS_PREF] = {"mode": "pick", "surveys": _survs,
                             "lines": _lines}
        _save_user_prefs(_p)
        st.session_state["_seis_basket_msg"] = (
            "Map set to the %d picked line(s)." % len(_lines))
        st.rerun()
    if _b3.button("Clear", key="seis_basket_clear",
                  use_container_width=True):
        st.session_state.pop("_seis_multi", None)
        st.session_state.pop("_seis_basket_last", None)
        st.rerun()
    # Consumed BEFORE the checkbox is instantiated -- see the flag's
    # comment in _render_seis_gallery. Setting a widget key here is legal;
    # setting it after the widget exists is not.
    if st.session_state.pop("_seis_gal_close", False):
        st.session_state["seis_basket_all"] = False
    st.checkbox("▦ Show all %d sections, one after another" % len(_multi),
                key="seis_basket_all",
                help="Every picked line stacked down the page. Each one is "
                     "a file read and a figure, so it is opt-in.")
    # AFTER the rerun -- st.rerun() raises and discards anything already
    # rendered, the scar that hid the colour-grid errors for a session.
    _bm = st.session_state.pop("_seis_basket_msg", None)
    if _bm:
        st.success(_bm)


@st.fragment
def _render_seis_pick(lines=None, df3d=None):
    """Filter down to one seismic line or volume, then look at it.

    THE VIEWER ALREADY EXISTED and the map could not reach it. file_viewer.view
    renders the textual header, the binary header, the trace headers and the
    section (density + wiggle), with a tolerant reader for the files segyio
    refuses. What was missing was a way to GET to a given line: 240 of them in
    one flat list is not a chooser, it is a haystack.

    It is nest-safe by construction -- _vsection is a bordered container, not
    an expander -- so it embeds here without tripping Streamlit scar #4.
    """
    cands = _seis_candidates(lines, df3d)
    if not cands and not st.session_state.get("_seis_pick"):
        return

    st.markdown("#### Seismic")

    # -- A DOOR THAT IS NOT THE MAP -------------------------------------
    # Every map click round-trips through Python: streamlit-folium returns the
    # click, Streamlit reruns the script, and the whole map is rebuilt and
    # re-serialised. That is what greys the page out and snaps you away from
    # where you were -- the framework's model, not a bug we can patch out, and
    # the same reason Python can never learn about a pan or a zoom. So
    # browsing popups and CHOOSING a section are separated: read the map
    # freely, and filter here when you want the data. Nothing below touches
    # the map.
    if cands:
        _c = st.columns(3)
        with _c[0]:
            _dim = _seis_filter_box(
                "Dimension", "seis_f_dim",
                sorted({c["dim"] for c in cands if c.get("dim")}))
        _f = [c for c in cands if not _dim or c.get("dim") == _dim]

        # One control, two meanings: a 2D survey groups lines, a 3D survey IS
        # the volume. Labelling it "Survey" in both cases reads as a mistake
        # to anyone looking for a volume by name.
        _lbl = ("Volume" if _dim == "3D"
                else "Survey" if _dim == "2D" else "Survey / volume")
        with _c[1]:
            _surv = _seis_filter_box(
                _lbl, "seis_f_survey",
                sorted({c["survey"] for c in _f if c.get("survey")}))
        _f = [c for c in _f if not _surv or c.get("survey") == _surv]

        with _c[2]:
            _stage = _seis_filter_box(
                "Processing", "seis_f_stage",
                sorted({c.get("stage") or _SEIS_UNSTATED for c in _f}),
                help_text="Imaging stage, read from the file name -- there is "
                          "no processing column in the catalogue. Names that "
                          "do not state one are listed as " + _SEIS_UNSTATED
                          + " rather than guessed at.")
        _f = [c for c in _f
              if not _stage or (c.get("stage") or _SEIS_UNSTATED) == _stage]

        _c2 = st.columns(3)
        with _c2[0]:
            _base = _seis_filter_box(
                "Line", "seis_f_line",
                sorted({c["base"] for c in _f if c.get("base")}),
                help_text="The line code with its processing suffix removed, "
                          "so one physical line is one entry rather than eight.")
        _f = [c for c in _f if not _base or c.get("base") == _base]

        with _c2[1]:
            _prod = _seis_filter_box(
                "Product", "seis_f_product",
                sorted({c.get("product") or _SEIS_UNSTATED for c in _f}))
        _f = [c for c in _f
              if not _prod or (c.get("product") or _SEIS_UNSTATED) == _prod]

        with _c2[2]:
            _NONE = "-- none --"
            _labels = [_NONE] + [_seis_label(h) for h in _f]
            # Clear asks for the chooser to be released; honour it HERE, before
            # the selectbox exists. Without this the box still shows the
            # cleared line, so picking that same line again matches the last
            # applied value and silently does nothing.
            if st.session_state.pop("_seis_sel_reset", False):
                st.session_state["seis_open_sel"] = _NONE
                st.session_state["_seis_sel_last"] = _NONE
            if st.session_state.get("seis_open_sel") not in _labels:
                st.session_state.pop("seis_open_sel", None)
                st.session_state["_seis_sel_last"] = None
            _sel = st.selectbox(f"Open ({len(_f)} match)", _labels,
                                key="seis_open_sel")
        # ACT ON CHANGE ONLY. The selectbox keeps its value across reruns, so
        # re-applying it every pass would overrule a later map click and make
        # the two doors fight. Comparing against the last APPLIED value lets
        # whichever was used most recently win.
        if _sel != st.session_state.get("_seis_sel_last"):
            st.session_state["_seis_sel_last"] = _sel
            if _sel == _NONE:
                st.session_state.pop("_seis_pick", None)
            else:
                st.session_state["_seis_pick"] = dict(_f[_labels.index(_sel) - 1])

        # ── send the filters to the map ────────────────────────────────
        # THE FILTERS ABOVE ONLY NARROWED THE OPEN CHOOSER. Everything up
        # to here decides which file you can open; the map draws from the
        # prefs file instead, so changing Processing inline moved nothing
        # on the map and read as a control that did not work.
        #
        # NOT AUTOMATIC ON EVERY FILTER CHANGE, deliberately: applying it
        # re-renders the map, the expensive object on this page, so
        # touching a dropdown would cost a full rebuild. Choosing when to
        # spend that is the operator's call, which is what a button is.
        _pm = st.session_state.pop("seis_push_msg", None)
        if _pm:
            st.success(_pm)
        _pc = st.columns([1, 1, 3])
        if _pc[0].button("Show on map", key="seis_push_map_btn",
                         disabled=not _f,
                         help="Draw exactly the %d match(es) above, and "
                              "nothing else." % len(_f)):
            # EVERYTHING SELECTED IS "all", NOT A PICK OF EVERYTHING. A
            # pick listing every line looks identical today but freezes
            # the map against whatever is catalogued next.
            if len(_f) >= len(cands):
                _write_map_seis("all", [], [], "Map showing every survey.",
                                msg_key="seis_push_msg")
            else:
                _ps, _pl = _seis_map_keys(_f)
                _write_map_seis(
                    "pick", _ps, _pl,
                    "Map set to %d line(s) from %d survey(s)."
                    % (len(_pl), len(_ps)), msg_key="seis_push_msg")
        if _pc[1].button("Show all", key="seis_push_all_btn",
                         help="Undo any pick and draw every survey -- the "
                              "way back from a selection made here or on "
                              "the second screen."):
            _write_map_seis("all", [], [], "Map showing every survey.",
                            msg_key="seis_push_msg")
        # SAY WHAT THE MAP IS DOING NOW, which is not what the filters show
        # the moment either changes -- the same distinction the second
        # screen's grid draws, for the same reason.
        _mc = _map_seis_choice()
        st.caption("Map is drawing %s." % (
            "every survey" if _mc["mode"] == "all"
            else "**nothing** - cleared" if _mc["mode"] == "none"
            else "%d picked line(s)" % len(_mc["lines"])))

        # ── the tick-a-set-of-lines grid, INLINE ───────────────────────
        # The filters above narrow to ONE line at a time; picking a SET --
        # "all twelve dip lines, not the strike lines" -- is a column of
        # checkboxes, and that grid already existed. It was only ever
        # rendered on the second screen, so the one control that can choose
        # a set was on the other monitor.
        #
        # It costs nothing to tick: the grid is inside a form, so the boxes
        # batch into ONE write instead of a rerun per box (scar #5), and
        # this whole function is a fragment, so even that write does not
        # touch the map. Send is the single rebuild.
        #
        # BEFORE THE EARLY RETURNS BELOW. _render_seis_pick returns as soon
        # as there is no picked file, and choosing which lines to DRAW has
        # nothing to do with having opened one.
        _render_map_drive(lines, df3d)

    # A DOOR TO THE SECOND SCREEN THAT DOES NOT NEED A PICK FIRST. The link
    # further down re-navigates the named window to the SELECTED file, which is
    # the live push -- but it only exists once something is selected, so there
    # was no way to simply put the seismic page on the other monitor and drive
    # it from its own chooser. Same window name, so this opens the same window
    # the pushes land in, not a second one.
    if str(st.query_params.get("view") or "").lower() != "seis":
        st.markdown(
            '<a href="?view=seis" target="dwseis" rel="noopener" '
            'style="font-size:0.82rem;text-decoration:none">'
            '&#x2197; seismic page on second screen</a>',
            unsafe_allow_html=True)

    _render_seis_area_add(lines, df3d)
    _render_seis_basket()
    if st.session_state.get("seis_basket_all"):
        _render_seis_gallery(st.session_state.get("_seis_multi") or [])
        return

    _pick = st.session_state.get("_seis_pick")
    if not _pick:
        return
    _path = str(_pick.get("path") or "")
    _hdr = " - ".join([x for x in (_pick.get("survey"), _pick.get("line")) if x])

    st.markdown("##### " + (_hdr or _pick.get("name") or "picked file"))
    _c1, _c2c = st.columns([5, 1])
    with _c1:
        _bits = [str(_pick.get("kind") or "seismic")]
        if _pick.get("stage"):
            _bits.append(str(_pick["stage"]))
        if _pick.get("product"):
            _bits.append(str(_pick["product"]))
        _bits.append(("EPSG " + str(_pick.get("epsg"))) if _pick.get("epsg")
                     else "EPSG unknown")
        if _pick.get("traces") not in (None, ""):
            try:
                _bits.append(format(int(_pick["traces"]), ",") + " traces")
            except (TypeError, ValueError):
                pass
        st.caption(" - ".join(_bits))
        # -- Open on the second screen ----------------------------------
        # A NAMED window, not a new tab. target="dwseis" means the first
        # click opens the window (drag it to the other monitor once) and
        # every later pick RE-NAVIGATES that same window. That is what makes
        # this feel live without polling: the map pushes, nothing watches.
        # Hidden in the second window itself, which would otherwise offer to
        # open a second copy of the page it already is.
        if _path and str(st.query_params.get("view") or "").lower() != "seis":
            st.markdown(
                '<a href="%s" target="dwseis" rel="noopener" '
                'style="font-size:0.82rem;text-decoration:none">'
                '&#x2197; open on second screen</a>' % seis_view_url(_path),
                unsafe_allow_html=True)
    with _c2c:
        if st.button("Clear", key="seis_pick_clear", use_container_width=True):
            st.session_state.pop("_seis_pick", None)
            st.session_state["_seis_sel_reset"] = True
            st.rerun()

    if not _path:
        st.warning("This survey has no file path recorded, so there is "
                   "nothing to open. Its geometry is still trustworthy.")
        return
    if not os.path.exists(_path):
        # A CATALOGUED FILE THAT HAS MOVED IS HELD AS 'M', NOT DROPPED, so a
        # path that no longer resolves is an expected state with a known
        # repair. Say which, rather than showing a bare "file not found".
        st.error("The catalogue points at a file that is not there now:")
        st.code(_path, language=None)
        st.caption("Re-scan the folder it moved to. The catalogue keeps the "
                   "row and its reason rather than discarding it, so nothing "
                   "was lost by the file moving.")
        return

    # DOWNLOAD IS SIZE-GATED. st.download_button needs the whole file in
    # memory and a 3D volume is routinely gigabytes, so offering the button
    # unconditionally would hang the app rather than fail.
    try:
        _sz = os.path.getsize(_path)
    except OSError:
        _sz = 0
    _CAP = 250 * 1024 * 1024
    if 0 < _sz <= _CAP:
        with open(_path, "rb") as _fh:
            st.download_button(
                "Download the SEG-Y (" + format(_sz / 1048576, ".1f") + " MB)",
                data=_fh.read(),
                file_name=_pick.get("name") or os.path.basename(_path),
                mime="application/octet-stream",
                key="seis_pick_dl")
    elif _sz:
        st.caption(format(_sz / 1048576, ",.0f") + " MB - too large to serve "
                   "through the browser, so it is read from disk in place:")
        st.code(_path, language=None)

    if _pick.get("dim") == "3D" and _render_seis_slice(_path):
        return

    try:
        from dataview.file_catalog.file_viewer import view as _fview
        _fview(_path, os.path.splitext(_path)[1].lower())
    except Exception as _e:
        st.error("The SEG-Y viewer could not open this file: " + str(_e))
        st.code(_path, language=None)


def seis_view_url(path):
    """The href that opens `path` on the second screen.

    Relative, so it works behind whatever host/port Streamlit was started on.
    """
    from urllib.parse import quote
    return "?view=seis&path=" + quote(str(path or ""), safe="")


def render_seis_view(engine):
    """The seismic panel ALONE, sized for a second monitor.

    A SECOND WINDOW IS A SECOND SESSION. Streamlit keys session_state to the
    browser connection, so this window cannot see the map's _seis_pick and no
    amount of shared Python state would change that. The pick travels in the
    URL instead, and the map re-navigates this window BY NAME (target=dwseis)
    rather than opening a tab per click -- which is why this needs no polling,
    no shared store, and no refresh loop to feel live.

    The URL is applied only when it CHANGES. This window has its own chooser,
    and re-asserting the query param on every rerun would fight the operator
    every time they picked a different line here.
    """
    st.markdown("### Seismic — second screen")
    _want = str(st.query_params.get("path") or "").strip()
    _lines = _seismic_line_paths(engine)
    _df3d = _qry_seismic_3d(engine)

    if _want and st.session_state.get("_seis_url_path") != _want:
        _hit = next((c for c in _seis_candidates(_lines, _df3d)
                     if c.get("path") == _want), None)
        st.session_state["_seis_url_path"] = _want
        if _hit:
            st.session_state["_seis_pick"] = dict(_hit)
            # AND POINT THE CHOOSER AT IT, or the chooser throws it away.
            # _render_seis_pick treats a selectbox reading "-- none --" that
            # differs from _seis_sel_last as a deliberate Clear. On the FIRST
            # run of this window both conditions hold for free -- the box has
            # no stored value so it defaults to "-- none --", and _seis_sel_last
            # is unset -- so the pick the URL had just set was popped before it
            # could draw. The window opened, showed the right title for one
            # frame, and then showed nothing.
            #
            # The filters are released as well, because the box is built only
            # from what survives them: on a RE-navigation this session still
            # holds the previous line's survey and stage, and a new line from
            # a different survey would not be in the list to be selected. The
            # map pushed this line, so the map wins -- which is the whole point
            # of the second screen.
            #
            # All of it BEFORE _render_seis_pick draws anything. Assigning a
            # widget key after its widget exists is scar #6, and it raises on a
            # later run, on whatever page happens to draw next.
            for _fk in ("seis_f_dim", "seis_f_survey", "seis_f_stage",
                        "seis_f_line", "seis_f_product"):
                st.session_state.pop(_fk, None)
            _url_lbl = _seis_label(_hit)
            st.session_state["seis_open_sel"] = _url_lbl
            st.session_state["_seis_sel_last"] = _url_lbl
        else:
            # NAME THE FILE. "Nothing to show" sends the reader back to the map
            # to click again; the path says whether the catalog moved on.
            st.warning("That file is not in the seismic catalog on this "
                       "connection: `%s`. Pick a line below instead."
                       % _want)

    # _render_seis_pick now renders the map-drive grid itself, so this page
    # and the map page offer the same control instead of only this one having
    # it. The note that used to live here is on that call.
    _render_seis_pick(_lines, _df3d)


def _render_map_drive(lines, df3d):
    """Tick what the map draws, on the second screen.

    THE OTHER DIRECTION. The map pushes a picked line to this window through
    a named window and a URL; this sends a CHOICE back. It cannot go back the
    same way -- re-navigating the map window would reload the heaviest page
    in the app every time a box moved -- and it cannot go through
    session_state, because the two windows are separate Streamlit sessions.

    So it goes through the prefs FILE both sessions already read for saved
    places. That makes it deliberately NOT live: the map applies it on its
    next render. Live would mean polling, and a poll that re-renders the map
    re-serialises the whole thing every couple of seconds -- the map is the
    expensive object, so the cheap-sounding option is the ruinous one.

    A GRID, NOT TWO MULTISELECTS. Turning one line off in a multiselect means
    finding its chip and deleting it; the job here is "these on, that one
    off, now this one too", which is a column of checkboxes. Inside a form so
    the ticks batch into ONE write instead of a rerun per box (scar #5).
    """
    # ONE ROW PER DRAWABLE THING, FROM THE SAME TWO SOURCES THE MAP DRAWS.
    # A 2D line is a row; a 3D volume has no lines so it is its own row. The
    # stored shape stays {surveys, lines} -- the map already reads that --
    # and both are DERIVED from the ticks, so a survey is on exactly when
    # something of its own is on.
    rows = []
    for _sl in (lines or []):
        _sv = str(_sl.get("survey") or "(unnamed survey)")
        _ln = str(_sl.get("line") or "")
        if not _ln:
            continue
        # The SEG-Y behind it, carried so the ticks can also go to Results.
        # A line with no file still belongs in the grid -- it is drawable, and
        # this control decides what is DRAWN -- it just cannot be opened.
        rows.append({"key": "%s|%s" % (_sv, _ln), "survey": _sv,
                     "line": _ln, "kind": "2D",
                     "path": str(_sl.get("file") or "")})
    try:
        _iter = df3d.iterrows() if df3d is not None else []
    except AttributeError:
        _iter = []
    _seen3d = set()
    for _i, _r in _iter:
        _sv = str(_r.get("survey_name") or "").strip()
        if not _sv or _sv in _seen3d:
            continue
        _seen3d.add(_sv)
        rows.append({"key": _sv, "survey": _sv,
                     "line": "(whole volume)", "kind": "3D",
                     "path": str(_r.get("file_path") or "")})
    if not rows:
        return
    rows.sort(key=lambda r: (r["survey"], r["line"]))

    _cur = _map_seis_choice()
    _on = set(_cur["lines"]) | set(_cur["surveys"])

    st.divider()
    st.markdown("#### Drive the map")
    # SAY WHAT IS IN FORCE. The ticks show what you are ABOUT to send, not
    # what the map is doing, and the two differ the moment you touch a box --
    # so without this a cleared map looks identical to a full one.
    # Same distinction in the live-state line, for the same reason.
    _on_rows = [r for r in rows if r["key"] in _on]
    _on_l = len([r for r in _on_rows if r["kind"] != "3D"])
    _on_v = len(_on_rows) - _on_l
    _state = {"all": "every survey",
              "none": "**nothing** - cleared",
              "pick": "%d line(s)%s" % (
                  _on_l, (" and %d volume(s)" % _on_v) if _on_v else ""),
              }[_cur["mode"]]
    st.caption("The map is drawing %s. Tick and send - it takes effect on "
               "the map's next render." % _state)

    import zlib as _z
    _sig = _z.crc32("|".join(r["key"] for r in rows).encode("utf-8"))
    with st.form("mapdrive_form"):
        _grid = st.data_editor(
            pd.DataFrame([{
                # ALL TICKED when the map is showing everything, so the
                # first move is to untick rather than to hunt for what
                # was on.
                "Show": (_cur["mode"] == "all") or (r["key"] in _on),
                "Survey": r["survey"], "Line": r["line"], "Kind": r["kind"],
                "key": r["key"],
            } for r in rows]),
            hide_index=True, use_container_width=True,
            # ENDS "_editor" ON PURPOSE: _is_action_key excludes data editors
            # by that suffix, and without it the persist loop self-assigns
            # the key, the assignment raises, and the error surfaces on
            # whatever page draws next.
            key="mapdrive_grid_v%d_editor" % _sig,
            column_config={
                "Show": st.column_config.CheckboxColumn(width="small"),
                "Survey": st.column_config.TextColumn(disabled=True),
                "Line": st.column_config.TextColumn(disabled=True),
                "Kind": st.column_config.TextColumn(disabled=True,
                                                    width="small"),
                "key": None,
            })
        _c1, _c4, _c2, _c3 = st.columns([1.2, 1.4, 1, 1])
        _send = _c1.form_submit_button("Send to map", type="primary",
                                       use_container_width=True)
        # THE OTHER THING A TICKED SET IS FOR. Choosing twelve dip lines to
        # DRAW and choosing twelve to LOOK AT are the same act of picking,
        # and until now only the first had a button -- the sections could be
        # collected only by clicking the map one line at a time, which is the
        # rebuild-per-click this whole afternoon was about.
        _tores = _c4.form_submit_button("Send to Results",
                                        use_container_width=True)
        _none = _c2.form_submit_button("Clear all",
                                       use_container_width=True)
        _all = _c3.form_submit_button("Show everything",
                                      use_container_width=True)

    def _write(mode, surveys, lines, msg):
        _write_map_seis(mode, surveys, lines, msg)

    if _tores:
        # Ticked -> the picks basket, which stacks each one's section down
        # the page. A fragment-scoped rerun is enough and is the point: this
        # changes what is OPEN, not what the map draws, so the map is left
        # exactly as it is.
        _bypath = {str(r["key"]): r.get("path") for r in rows}
        _want = [_bypath.get(str(r["key"]))
                 for _i, r in _grid.iterrows() if r["Show"]]
        _added = _seis_basket_add([p for p in _want if p], lines, df3d)
        _asked = len([p for p in _want])
        if _added:
            st.session_state["mapdrive_msg"] = (
                "Added %d line(s) to Results." % _added)
        elif _asked:
            # SAY WHICH NOTHING HAPPENED. "Already there" and "none of them
            # has a file to open" are different facts with different fixes,
            # and a silent no-op reads as a broken button either way.
            st.session_state["mapdrive_msg"] = (
                "Nothing added — those line(s) are already in Results, or "
                "have no SEG-Y behind them to open.")
        else:
            st.session_state["mapdrive_msg"] = "Nothing ticked."
        st.rerun()

    if _send:
        _keys = {str(r["key"]) for _i, r in _grid.iterrows() if r["Show"]}
        _lines = sorted(k for k in _keys if "|" in k)
        # A SURVEY IS ON WHEN SOMETHING OF ITS OWN IS ON. Derived rather
        # than asked for twice: the footprint layer keys on survey names and
        # the line filter on survey|line, and two lists a person maintains
        # by hand are two lists that drift.
        _survs = sorted({k.split("|")[0] for k in _keys})
        # COUNT LINES AND VOLUMES SEPARATELY. "Map set to 18" counted ROWS,
        # and one of those rows is a 3D VOLUME that draws as a footprint
        # rectangle rather than a line -- so ticking 18 drew 17 lines and
        # the message looked like it had lost one. The number was right and
        # meant something other than what it appeared to mean, which is
        # worse than being wrong: it sends you looking for a missing line.
        _n_vol = len(_keys) - len(_lines)
        _what = "%d line(s)" % len(_lines)
        if _n_vol:
            _what += " and %d volume(s)" % _n_vol
        if len(_keys) == len(rows):
            _write("all", [], [], "Map showing every survey.")
        elif not _keys:
            _write("none", [], [], "Nothing ticked - seismic cleared.")
        else:
            _write("pick", _survs, _lines,
                   "Map set to %s." % _what)
    if _none:
        # KEEP THE TICKS. Clear is a mute, not a reset: the selection stays
        # in the file so one Send puts exactly it back.
        _write("none", _cur["surveys"], _cur["lines"],
               "Seismic cleared from the map.")
    if _all:
        _write("all", [], [], "Map restored to every survey.")

    # AFTER the rerun. st.rerun() raises, so anything rendered above it is
    # discarded -- the scar that hid the colour-grid errors for a session.
    _mm = st.session_state.pop("mapdrive_msg", None)
    if _mm:
        st.success(_mm)


def _render_seis_slice(path):
    """A 3D volume browsed by inline and crossline. True if it drew.

    THE SEQUENTIAL READER IS THE WRONG TOOL FOR A VOLUME. "The first 120
    traces" of a 3D survey is a fragment of one inline and tells you nothing
    about the survey; a NAMED inline is a section that can be interpreted.
    Delft is 211,519 traces across 451 inlines, so without this the volume can
    be catalogued, mapped and downloaded but not looked at.

    Both sections are shown together and tied to each other, because the
    question a volume raises is always "what does it look like the other way".

    Not every volume can offer it, and one that cannot must SAY SO rather than
    show a picker full of noise -- see segy_header.trace_index, which refuses a
    file whose trace stride is not exact. Teapot's filt_mig.sgy is exactly that
    file, and it falls through to the sequential viewer.
    """
    try:
        from dataview.file_catalog.segy_header import (
            slice_values, read_slice_samples)
        from dataview.file_catalog.file_viewer import segy_volume_plot
    except Exception as _e:
        st.caption("Slice reader unavailable: " + str(_e))
        return False

    with st.spinner("Indexing the volume's inline / crossline numbers..."):
        try:
            _ils = slice_values(path, "inline")
            _xls = slice_values(path, "crossline")
        except Exception as _e:
            st.caption("Could not index this volume: " + str(_e))
            return False

    if not _ils and not _xls:
        st.info(
            "**This volume carries no usable inline / crossline index**, so it "
            "is shown as a sequential trace section below. Either those trace "
            "header fields hold something else (Teapot's hold coordinates), or "
            "the trace stride is not exact and a strided read would return "
            "numbers that look like inline numbers and are not.")
        return False

    # A SLIDER, NOT A LIST. Delft has 451 inlines and Brecon 457; a selectbox
    # of those is a scroll, while stepping a slider is how someone walks
    # through a volume. select_slider keeps the REAL numbering rather than an
    # index, so the label is the inline a geologist would quote.
    _c = st.columns(2)
    _il_no = _xl_no = None
    if _ils:
        with _c[0]:
            if st.session_state.get("seis_il_no") not in _ils:
                st.session_state.pop("seis_il_no", None)
            _il_no = st.select_slider(
                f"Inline  ({_ils[0]}-{_ils[-1]}, {len(_ils)})",
                options=_ils, key="seis_il_no")
    if _xls:
        with _c[1 if _ils else 0]:
            if st.session_state.get("seis_xl_no") not in _xls:
                st.session_state.pop("seis_xl_no", None)
            _xl_no = st.select_slider(
                f"Crossline  ({_xls[0]}-{_xls[-1]}, {len(_xls)})",
                options=_xls, key="seis_xl_no")

    def _panel(axis, value, tie, tie_label):
        if value is None:
            return None
        _d, _t = read_slice_samples(path, axis, int(value))
        if _d is None:
            return None
        _s = getattr(read_slice_samples, "last_stats", {}) or {}
        return {"data": _d, "times": _t,
                "x": _s.get("cross_values"),
                "cross_label": _s.get("cross_axis", "trace"),
                "tie": tie, "tie_label": tie_label,
                "title": f"{axis.capitalize()} {value}",
                "_of": _s.get("of"), "_n": _d.shape[1]}

    with st.spinner("Reading sections..."):
        _ilp = _panel("inline", _il_no, _xl_no, f"XL {_xl_no}")
        _xlp = _panel("crossline", _xl_no, _il_no, f"IL {_il_no}")

    if _ilp is None and _xlp is None:
        st.warning("Neither section could be read from this volume.")
        return True

    segy_volume_plot(_ilp, _xlp,
                     title=os.path.basename(path))

    # SAY WHAT WAS DECIMATED. A section quietly reduced to 1,200 traces reads
    # as the whole slice, and the caption is the only thing that distinguishes
    # "this is the section" from "this is a sample of it".
    _notes = []
    for _p, _lbl in ((_ilp, "inline"), (_xlp, "crossline")):
        if not _p:
            continue
        _n, _of = _p["_n"], _p.get("_of") or _p["_n"]
        _notes.append(f"{_lbl} {_n:,} trace(s)"
                      + (f" decimated from {_of:,} across the whole slice"
                         if _of > _n else ""))
    if _notes:
        st.caption(" - ".join(_notes)
                   + ". The dashed red line on each section marks where the "
                     "other one cuts through it.")
    return True


@st.cache_data(ttl=300, show_spinner=False)
def _seismic_line_paths(_engine, _v: int = 2):
    """Real 2D seismic line paths from dataview.dv_seis_line.geog.

    The extract path now writes trace-order LINESTRINGs (WGS84, reprojected
    from the CRS each file's own textual header declares) into
    FILE_SEIS_HEADER.SURVEY_OUTLINE, and promote converts them into
    dv_seis_line.geog — so the DATABASE is the source. The old
    seismic_lines.geojson is a pure export and is no longer read here.

    No rows, no layer, silently: the dv_seis_set.geog footprints on the same
    pill still draw, so a deployment that has never promoted seismic
    geometry is not worse off than before.
    """
    try:
        with _engine.connect() as con:
            df = pd.read_sql(text("""
                SELECT ss.seis_set_name   AS survey,
                       sl.line_name       AS line_name,
                       sl.trace_count     AS trace_count,
                       ss.epsg_code       AS epsg,
                       sl.file_path       AS file_path,
                       sl.geog.STAsText() AS wkt
                  FROM dataview.dv_seis_line sl
                  LEFT JOIN dataview.dv_seis_set ss
                         ON ss.seis_set_id = sl.seis_set_id
                 WHERE sl.geog IS NOT NULL
                   AND sl.geog.STGeometryType() = 'LineString'
                 ORDER BY ss.seis_set_name, sl.line_name
            """), con)
    except Exception as exc:
        print(f"[seismic_lines] dv_seis_line: {exc}")
        return []
    out = []
    for r in df.itertuples():
        pts = _geog_linestring_pts(r.wkt)
        if len(pts) < 2:
            continue
        try:
            _epsg = int(r.epsg) if pd.notna(r.epsg) else None
        except (TypeError, ValueError):
            _epsg = None
        try:
            _tr = int(r.trace_count) if pd.notna(r.trace_count) else None
        except (TypeError, ValueError):
            _tr = None
        # THE FILE IS THE POINT OF PICKING A LINE. dv_seis_line.file_path is
        # populated on every row (240/240), so the map already knew which
        # SEG-Y each line came from and simply never said. A line you can see
        # but cannot trace back to a file is a picture, not a catalogue.
        _fp = str(getattr(r, "file_path", "") or "")
        out.append({"pts": pts,
                    "survey": r.survey or "(unnamed survey)",
                    "line": r.line_name or "",
                    "epsg": _epsg, "traces": _tr,
                    "file": _fp,
                    "file_name": _fp.replace("/", "\\").split("\\")[-1]})
    return out


def _add_seismic_3d(m, df):
    """Render 3D seismic survey footprints as filled rectangles.

    Path A for seismic on the map: bbox-as-rectangle. Each 3D survey shows
    as a translucent blue rectangle bounded by its BBOX_MIN/MAX_LAT/LON.
    This is geometrically correct for 3D surveys (they ARE rectangular
    footprints) — unlike 2D lines, which need actual polyline extraction
    and are deferred to Stage B.

    Click any rectangle for a popup with file name, contractor, trace
    count, sample interval, EPSG, and survey date.
    """
    if df.empty:
        return
    fg = folium.FeatureGroup(
        name=f"🟦 Seismic 3D Surveys ({len(df):,})", show=False
    )

    for _, row in df.iterrows():
        # Defensive bbox sanity. Even after the SQL filter, some rows may
        # come back with min > max (rare segyio quirk). Skip those — a
        # negative-area rectangle would render as a line, confusing.
        try:
            min_lat = float(row["min_lat"])
            max_lat = float(row["max_lat"])
            min_lon = float(row["min_lon"])
            max_lon = float(row["max_lon"])
        except (TypeError, ValueError):
            continue
        if not (min_lat < max_lat and min_lon < max_lon):
            continue

        # Skip ludicrously large bboxes — these indicate a CDP_X/Y scalar
        # misread, where we got raw scaled coordinates instead of lat/lon.
        # Anything bigger than 5 degrees in either dimension is almost
        # certainly garbage for a 3D survey (largest single 3D surveys are
        # ~2 degrees on a side).
        if (max_lat - min_lat) > 5 or (max_lon - min_lon) > 5:
            continue

        # Build a popup with the survey metadata that's worth knowing
        # before someone digs deeper into the file. Trim long values.
        _name = row.get("survey_name") or row.get("line_name") \
                or row.get("file_name") or "Unnamed 3D"
        _name = str(_name)[:80]
        _popup = folium.Popup(
            f"<b>🟦 {_name}</b><br>"
            + _popup_table({
                "File":     str(row.get("file_name") or "—")[:80],
                "Contractor": str(row.get("contractor") or "—")[:60],
                "Date":     str(row.get("survey_date") or "—"),
                "Traces":   f"{int(row['trace_count']):,}"
                            if pd.notna(row.get("trace_count")) else "—",
                "Sample interval": f"{row['sample_interval']:g} μs"
                            if pd.notna(row.get("sample_interval")) else "—",
                "EPSG":     str(int(row["epsg_code"]))
                            if pd.notna(row.get("epsg_code")) else "—",
                "Extent":   f"{max_lat-min_lat:.3f}° × {max_lon-min_lon:.3f}°",
                "Path":     _popup_safe(str(row.get("file_path") or "")[:260])
                            or None,
            }),
            max_width=280,
        )

        folium.Rectangle(
            bounds=[[min_lat, min_lon], [max_lat, max_lon]],
            # TAGGED FOR THE PICKER. class_name becomes Leaflet className
            # on the SVG path, which is how dv_seis_picker tells a 3D
            # survey from a 2D line without guessing at layer names.
            class_name="dv-seis-3d",
            color="#1d4ed8",
            weight=2,
            fill=True,
            fill_color="#3b82f6",
            fill_opacity=0.25,
            popup=_popup,
            tooltip=_name,
        ).add_to(fg)

    fg.add_to(m)


# A NAMED PALETTE, because "#795548" is not a colour to a human. The grid
# stores and the map draws HEX -- that is the real value and it has to stay
# exact -- but nobody picking a colour for a pipeline is thinking in hex, and
# a text field full of them is a field you cannot skim. Names in, hex out.
#
# Chosen to stay apart from each other on a pale basemap and from the well
# symbols already on it: no two greens, nothing so light it vanishes on
# CartoDB positron.
MAP_COLOURS = [
    ("Red",         "#D32F2F"), ("Orange",      "#F57C00"),
    ("Amber",       "#FFA000"), ("Yellow",      "#FBC02D"),
    ("Olive",       "#827717"), ("Green",       "#388E3C"),
    ("Teal",        "#00796B"), ("Cyan",        "#0097A7"),
    ("Blue",        "#1976D2"), ("Navy",        "#283593"),
    ("Purple",      "#7B1FA2"), ("Magenta",     "#C2185B"),
    ("Brown",       "#795548"), ("Slate",       "#455A64"),
    ("Grey",        "#616161"), ("Black",       "#212121"),
    # THE CATEGORY DEFAULTS, BY NAME. Without these, eleven of twelve loaded
    # layers showed a raw hex in the grid -- every layer the loader styled from
    # CATEGORY_DEFAULTS -- and a palette that names only the colours you chose
    # by hand is a palette that names almost nothing.
    ("Field Green", "#4CAF50"), ("Lease Blue",   "#2196F3"),
    ("Well Red",    "#E24B4A"), ("Boundary Slate", "#607D8B"),
    ("Layer Grey",  "#9E9E9E"), ("Seismic Orange", "#FF6B35"),
    ("Seismic Purple", "#7B2D8B"), ("Basin Orange", "#FF9800"),
    ("Pale Green",  "#A5D6A7"), ("Pale Blue",    "#90CAF9"),
    ("Pale Purple", "#C490D1"), ("Pale Orange",  "#FFE0B2"),
    ("Near White",  "#EEEEEE"), ("White",        "#FFFFFF"),
]
_COLOUR_BY_NAME = {n: h for n, h in MAP_COLOURS}
# CASE-FOLDED, because people TYPE into the grid. See _colour_hex.
_COLOUR_BY_FOLD = {n.casefold(): h for n, h in MAP_COLOURS}
_COLOUR_BY_HEX = {h.upper(): n for n, h in MAP_COLOURS}


def _colour_name(hex_colour):
    """A palette name for a hex value, or the hex itself when it is not one.

    Returning the hex UNCHANGED is the point. A layer styled outside this
    grid -- by the loader's category defaults, or by hand -- must still show
    something true. Mapping it to the NEAREST name would rename a colour
    nobody chose, and the grid writes back what it displays.
    """
    s = str(hex_colour or "").strip().upper()
    return _COLOUR_BY_HEX.get(s, s or "#888888")


def _colour_hex(name):
    """The hex for a palette name, passing an unrecognised value through.

    CASE-INSENSITIVE, AND A BARE HEX IS A HEX. The grid column is a
    dropdown, but people type into it, and the lookup that knew only
    "Red" sent a typed "red" straight through to the malformed-hex
    refusal below. That refusal warned -- into an st.rerun() that
    destroys anything rendered before it -- so the cell went back to
    brown and NOTHING on screen said why. Two bugs, one symptom.

    "red" is not a guess about what was meant: it is the same word as
    "Red" and the palette holds exactly one of them. An unrecognised
    WORD still passes through unchanged so the caller can refuse it --
    nearest-matching would restyle a layer nobody named.
    """
    s = str(name or "").strip()
    if s in _COLOUR_BY_NAME:
        return _COLOUR_BY_NAME[s]
    hit = _COLOUR_BY_FOLD.get(s.casefold())
    if hit:
        return hit
    bare = s.lstrip("#")
    if len(bare) == 6 and all(c in "0123456789abcdefABCDEF" for c in bare):
        return "#" + bare.upper()
    return s

# ONE COLOUR PER 2D SURVEY. Every line used to be the same orange-brown,
# so a map with several surveys on it showed one indistinguishable mesh --
# which is exactly when you most need to know whose line you are looking at.
#
# Not the full MAP_COLOURS: these have to stay apart from each other AND
# from what is already on the map -- wells are red, fields green, leases
# blue, boundaries slate. These six are chosen against that.
_SEIS_SURVEY_COLOURS = [
    "#B36A00",   # the original orange-brown, so a single-survey map is
                 # unchanged and nobody has to relearn it
    "#7B2D8B",   # purple
    "#00796B",   # teal
    "#C2185B",   # magenta
    "#827717",   # olive
    "#283593",   # navy
]


def _survey_colour(name):
    """A stable colour for a survey name.

    crc32, NOT hash(). Python salts hash() per process, so the colours
    would be reshuffled on every restart -- a map that recolours itself
    overnight teaches nothing and looks like a bug. This is the same trap
    the layer grid hit and the same fix.
    """
    import zlib
    _k = str(name or "")
    return _SEIS_SURVEY_COLOURS[
        zlib.crc32(_k.encode("utf-8")) % len(_SEIS_SURVEY_COLOURS)]


def _darken(hex_colour, factor=0.5):
    """A darker shade of a #rrggbb colour, for a pipeline's casing.

    Returns the input unchanged if it is not a hex triple -- a style value can
    be any string a person typed, and a casing that raises would take the whole
    map down to make a line prettier.
    """
    s = str(hex_colour or "").strip()
    if not _re.fullmatch(r"#[0-9A-Fa-f]{6}", s):
        return s or "#333333"
    r, g, b = (int(s[i:i + 2], 16) for i in (1, 3, 5))
    f = max(0.0, min(1.0, factor))
    return "#%02X%02X%02X" % (int(r * f), int(g * f), int(b * f))

def _add_shapefile_layer(m, engine, layer):
    source_type  = layer.get("source_type","GEOJSON")
    layer_name   = layer.get("layer_name","Layer")
    color        = layer.get("style_color")        or "#888888"
    weight       = float(layer.get("style_weight")      or 1.5)
    opacity      = float(layer.get("style_opacity")     or 0.8)
    fill_color   = layer.get("style_fill_color")   or color
    fill_opacity = float(layer.get("style_fill_opacity") or 0.0)
    dash         = layer.get("style_dash")
    tt_fields    = [f.strip() for f in
                    (layer.get("tooltip_fields") or "").split(",") if f.strip()]

    gj = None
    if source_type == "SHAPEFILE":
        fpath = layer.get("file_path","")
        if fpath and os.path.exists(fpath) and HAS_GPD:
            try:
                gdf = gpd.read_file(fpath).to_crs("EPSG:4326")
                gj  = json.loads(gdf.to_json())
            except Exception:
                return
        else:
            return
    else:
        gj_str = _cached_layer_geojson(engine, layer["layer_id"])
        if not gj_str:
            return
        try:
            gj = json.loads(gj_str)
        except Exception:
            return

    if not gj:
        return

    icon_ch = LAYER_CATEGORY_DISPLAY.get(layer.get("layer_category",""), "📁").split()[0]

    def _style(_, c=color, w=weight, o=opacity,
               fc=fill_color, fo=fill_opacity, d=dash):
        s = {"color":c,"weight":w,"opacity":o,"fillColor":fc,"fillOpacity":fo}
        if d:
            s["dashArray"] = d
        return s

    # ── pipelines look like pipelines ─────────────────────────────
    # A pipeline drawn as a plain coloured line is indistinguishable from a
    # road, a fault, a contour or a lease edge -- and this map carries all
    # four. The cartographic convention is a CASING: a dark line laid down
    # first, a lighter body over it, and a dashed centreline on top. The
    # casing gives the pipe an edge, the dashes read as segment ticks, and
    # together they say "pipe" at any zoom without a legend.
    #
    # Three passes into ONE FeatureGroup, not three layers: the layer control
    # must toggle a pipeline once, and three entries for one dataset is the
    # kind of clutter that makes people stop using the control.
    #
    # Leaflet's PolylineDecorator would draw true perpendicular ticks and is
    # deliberately not used: it is an external plugin, and the map has to work
    # offline behind whatever CSP the host applies.
    if str(layer.get("layer_category") or "").upper() == "PIPELINE":
        _grp = folium.FeatureGroup(name=f"{icon_ch} {layer_name}", show=True)

        # PROPORTIONS, LEARNED THE HARD WAY. The first cut was casing
        # weight+3.0 darkened to 0.45: a 5.5px near-black stroke around a
        # 2.5px body. It read as one thick dark line, and CHANGING THE COLOUR
        # DID NOTHING VISIBLE because the casing swallowed the hue -- two
        # complaints, one cause. The casing is an EDGE, not the pipe: barely
        # wider than the body, and dark enough to separate it from the basemap
        # without dominating it.
        _bw = max(1.0, min(float(weight or 1.5), 2.0))

        def _casing(_, c=_darken(color, 0.62), w=_bw + 1.2, o=opacity):
            return {"color": c, "weight": w, "opacity": o, "fillOpacity": 0}

        def _body(_, c=color, w=_bw, o=opacity):
            return {"color": c, "weight": w, "opacity": o, "fillOpacity": 0}

        def _ticks(_, w=max(0.6, _bw * 0.35)):
            # Short dash, long gap: ticks along the pipe rather than a dashed
            # line, which would read as "proposed" or "buried". Softened to
            # 0.55 so they mark the pipe instead of speckling it.
            return {"color": "#FFFFFF", "weight": w, "opacity": 0.55,
                    "dashArray": "1,10", "fillOpacity": 0}

        folium.GeoJson(gj, style_function=_casing).add_to(_grp)
        folium.GeoJson(gj, style_function=_body).add_to(_grp)
        _tk = {"style_function": _ticks}
        if tt_fields:
            _sample = (gj.get("features") or [{}])[0].get("properties", {})
            _valid = [f for f in tt_fields if f in _sample]
            if _valid:
                # THE TOP PASS CARRIES THE TOOLTIP, once. On all three the
                # hover fires whichever the cursor happens to hit and the
                # popup can open three deep.
                _tk["tooltip"] = folium.GeoJsonTooltip(fields=_valid, sticky=True)
                _tk["popup"] = folium.GeoJsonPopup(fields=_valid, max_width=300)
        folium.GeoJson(gj, **_tk).add_to(_grp)
        _grp.add_to(m)
        return

    kw = {"name": f"{icon_ch} {layer_name}", "style_function": _style}
    if tt_fields:
        sample = (gj.get("features") or [{}])[0].get("properties",{})
        valid  = [f for f in tt_fields if f in sample]
        if valid:
            kw["tooltip"] = folium.GeoJsonTooltip(fields=valid, sticky=True)
            kw["popup"]   = folium.GeoJsonPopup(fields=valid, max_width=300)
    folium.GeoJson(gj, **kw).add_to(m)


# =============================================================================
# WELL DETAIL PANEL
# =============================================================================

def _fluid_color(fluid):
    f = str(fluid or "").upper()
    c = {"OIL":"#1b5e20","GAS":"#e65100","WATER":"#0d47a1"}.get(f,"#555")
    return f"<span style='color:{c};font-weight:600'>{fluid or chr(8212)}</span>"


def _fmt(v, fmt=",", suffix=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return chr(8212)
    try:
        if fmt == ",":   return f"{float(v):,.0f}{suffix}"
        if fmt == ",.1": return f"{float(v):,.1f}{suffix}"
        if fmt == ",.2": return f"{float(v):,.2f}{suffix}"
        return str(v)
    except Exception:
        return str(v)


def _th(cells, bg="#475569"):
    """Column header row — slate-600 by default."""
    tds = "".join(
        f"<th style='background:{bg};color:#ffffff;padding:8px 12px;"
        f"font-size:12px;font-weight:700;text-align:left;letter-spacing:0.3px;"
        f"border-right:1px solid #64748b;border-bottom:1px solid #1e293b'>{c}</th>"
        for c in cells
    )
    return f"<tr>{tds}</tr>"


def _td(cells, alt=False):
    """Body cell row — white or slate-100 alt, near-black text."""
    bg = "#f1f5f9" if alt else "#ffffff"
    tds = "".join(
        f"<td style='background:{bg};color:#1e293b;padding:7px 12px;"
        f"font-size:13px;border-bottom:1px solid #cbd5e1;"
        f"border-right:1px solid #cbd5e1;white-space:nowrap'>{c}</td>"
        for c in cells
    )
    return f"<tr>{tds}</tr>"


def _section(title):
    """Section divider bar — slate-700, white text."""
    return (f"<div style='background:#334155;color:#ffffff;padding:8px 14px;"
            f"font-size:13px;font-weight:700;margin-top:10px;letter-spacing:0.3px;"
            f"border-radius:3px 3px 0 0'>{title}</div>")


def _tbl(rows):
    """Table wrapper — soft slate border."""
    return (f"<table style='width:100%;border-collapse:collapse;"
            f"border:1px solid #cbd5e1;margin-bottom:0;background:#ffffff'>{rows}</table>")


def _full_html_doc(html_body: str, title: str = "Scout Tickets") -> str:
    """Wrap scout ticket HTML in a full printable document with print/save buttons."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="color-scheme" content="light">
<title>{title}</title>
<style>
@page {{size:A4;margin:15mm 12mm;}}
html, body {{background:#ffffff;color:#1e293b;color-scheme:light;}}
body {{font-family:Arial,Helvetica,sans-serif;font-size:10px;margin:0;padding:0;}}
table {{width:100%;border-collapse:collapse;background:#ffffff;}}
th {{background:#475569;color:#ffffff;padding:8px 12px;font-size:11px;font-weight:700;
     text-align:left;letter-spacing:0.3px;border-right:1px solid #64748b;}}
td {{padding:6px 12px;font-size:11px;background:#ffffff;color:#1e293b;
     border-bottom:1px solid #cbd5e1;border-right:1px solid #cbd5e1;}}
tr:nth-child(even) td {{background:#f1f5f9;}}
.sh {{background:#334155;color:#ffffff;padding:8px 14px;font-size:12px;
      font-weight:700;margin-top:10px;letter-spacing:0.3px;}}
.no-print {{
    position:fixed;top:12px;right:16px;z-index:9999;
    display:flex;gap:8px;
}}
.no-print button {{
    background:#334155;color:#fff;border:none;border-radius:6px;
    padding:8px 18px;font-size:13px;font-weight:600;cursor:pointer;
    box-shadow:0 2px 4px rgba(0,0,0,0.15);
}}
.no-print button:hover {{background:#1e293b;}}
@media print {{.no-print{{display:none;}}}}
</style>
</head>
<body>
<div class="no-print">
  <button onclick="window.print()">🖨 Print</button>
  <button onclick="window.close()">✕ Close</button>
</div>
{html_body}
</body></html>"""


def _scout_ticket_pdf(html_body, well_name, return_error=False):
    """Render scout-ticket HTML to a real PDF via WeasyPrint.

    WeasyPrint output carries a NATIVE TEXT LAYER. That matters beyond looking
    the same: printing the HTML through a browser (or the Windows "Microsoft
    Print to PDF" driver) flattens every glyph to a vector outline, and the
    resulting file has ZERO extractable characters — the File Catalog can't
    read a word of it. Same ticket, same pixels, unusable downstream.

    The old version swallowed the exception and returned None, which the UI
    reported as "pip install weasyprint". On Windows that's usually wrong:
    WeasyPrint imports fine but fails at render time without the GTK/Pango
    runtime, so the real message is the one worth showing.
    """
    try:
        from weasyprint import HTML
        pdf = HTML(string=_full_html_doc(html_body, well_name)).write_pdf()
        return (pdf, None) if return_error else pdf
    except ImportError as e:
        err = f"WeasyPrint is not installed — pip install weasyprint ({e})"
    except Exception as e:
        err = (f"{type(e).__name__}: {e}\n\n"
               "On Windows WeasyPrint also needs the GTK3 runtime "
               "(Pango/Cairo) on PATH.")
    return (None, err) if return_error else None


def _show_detail(uwi, well_row, counts_df, engine=None):
    st.markdown('<hr style="margin:4px 0 8px 0;border-top:1px solid #ccc">',
                unsafe_allow_html=True)
    html = _build_scout_ticket_html(uwi, well_row, engine)
    st.markdown(html, unsafe_allow_html=True)
    # PDF download
    dl_col, _ = st.columns([1, 4])
    with dl_col:
        if st.button("⬇ Download PDF", key=f"pdf_btn_{uwi}",
                     type="primary", use_container_width=True):
            with st.spinner("Generating PDF..."):
                pdf = _scout_ticket_pdf(html, well_row.get("well_name", uwi))
            if pdf:
                st.download_button("📄 Save PDF", data=pdf,
                    file_name=f"Scout_{well_row.get('well_name',uwi).replace(' ','_')}.pdf",
                    mime="application/pdf",
                    key=f"pdf_dl_{uwi}", use_container_width=True)
            else:
                st.error("PDF generation failed — pip install weasyprint")

def _dst_result_color(result):
    r = str(result or "").upper()
    c = {"SHOW":"#1b5e20","MISS":"#c62828","INC":"#e65100","TRACE":"#e65100"}.get(r,"#555")
    return f"<b style='color:{c}'>{result or chr(8212)}</b>"


SCOUT_PHOTO_PX = (320, 160)      # what the card actually displays
SCOUT_PHOTO_MAX = 400            # cards per ticket, before it says so


@st.cache_data(ttl=1800, show_spinner=False)
def _photo_to_b64(file_path: str, _mtime: float = 0.0) -> str:
    """A base64 THUMBNAIL of a core photo, sized for the card it goes in.

    THIS USED TO EMBED THE WHOLE FILE. It was written against an empty
    table, so nothing showed what that cost until the photos landed:
    well 48-X-28 has 184 of them, 75 MB on disk, which is 100 MB of base64
    in ONE HTML page -- past what a browser will hold and far past what
    Streamlit will ship. The card displays 320x160 the whole time.

    Measured on those 184: 4 KB each at 320x160 q72, 0.9 MB for the lot,
    about 1.5 s to build. 110x smaller than the file it replaces.

    _mtime is in the signature ONLY to key the cache: a re-photographed
    file at the same path must not serve the old thumbnail, and the
    leading underscore keeps Streamlit from hashing it as data.
    """
    import base64
    import io as _io
    from pathlib import Path
    try:
        p = Path(file_path)
        if not p.exists():
            return ""
        from PIL import Image
        im = Image.open(p)
        im.thumbnail(SCOUT_PHOTO_PX)
        buf = _io.BytesIO()
        # RGB, because a CMYK or paletted source cannot be written as JPEG
        # and a core photo is not the place to lose one to an exception.
        im.convert("RGB").save(buf, "JPEG", quality=72)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        # NO FALLBACK TO THE RAW BYTES. That is the 100 MB path, and a
        # missing thumbnail costs one card while that costs the page.
        return ""


def _has_mud_log(engine, uwi):
    """True when this well has a mud log row. Cheap enough to ask per well."""
    if engine is None or not uwi:
        return False
    try:
        from sqlalchemy import text as _text
        with engine.connect() as _c:
            if not _c.execute(_text(
                    "SELECT OBJECT_ID('dataview.dv_well_mud_log')")).scalar():
                return False
            return bool(_c.execute(_text(
                "SELECT COUNT(*) FROM dataview.dv_well_mud_log "
                "WHERE uwi = :u"), {"u": uwi}).scalar())
    except Exception:
        return False


def _render_mud_log(engine, uwi):
    """The mud log strip for one well, on DRILLER'S depth.

    SEPARATE FROM THE WIRELINE STRIP, DELIBERATELY. A mud log is on driller's
    depth with lagged returns; the LAS files for this same well are on
    logger's depth, and the two disagree by nine feet at the Tensleep A. One
    button each keeps that honest -- overlaying them would file a sandstone
    show under the Opeche and look entirely reasonable doing it.

    Only offered where there IS one: 48-X-28 is the single well in this
    database with a mud log, so every other well shows nothing rather than an
    empty panel.
    """
    if not _has_mud_log(engine, uwi):
        return
    st.markdown("#### Mud log")
    if not st.session_state.get("ml_show"):
        if st.button("📜 Show mud log", key="ml_show_btn"):
            st.session_state["ml_show"] = True
            st.rerun()
        st.caption("Driller's depth — not the same axis as the wireline logs.")
        return
    if st.button("✕ Hide mud log", key="ml_hide_btn"):
        st.session_state.pop("ml_show", None)
        st.rerun()

    try:
        # LOCAL, because this module does not import sys at all -- the other
        # "sys." matches in this file are SQL (sys.columns). A bare name that
        # is missing fails only when the line runs, and this line runs only
        # when someone clicks the button.
        import importlib
        import sys as _sys
        _root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        _tools = os.path.join(_root, "tools")
        if _tools not in _sys.path:
            _sys.path.insert(0, _tools)
        _pm = importlib.import_module("plot_mudlog")   # puts its helpers on
        _mx = importlib.import_module("mudlog_export")   # the path for _mh
        _mh = importlib.import_module("mudlog_html")
    except Exception as _impexc:
        st.caption("Mud log plotter unavailable: %s" % _impexc)
        return

    _dat = getattr(_pm, "DEFAULT_DAT", "")
    if not _dat or not os.path.exists(_dat):
        st.info(
            "The mud log is catalogued, but its exported data has not been "
            "written yet. Open the .LOG in the WellSight viewer and use "
            "File → Export → “Write all data to an ASCII file”, saving to "
            "%s." % (_dat or "C:\\Bulk\\mudlog_test\\export.dat"))
        return

    try:
        _ex = _mx.parse(_dat)
        _lo, _hi = _ex.depth_range
    except Exception as _pexc:
        st.caption("Could not read the mud log export: %s" % _pexc)
        return

    # FULL SCREEN IS A FILE, NOT A CONTROL. Streamlit has no full-screen for
    # an image it did not draw, and the overlay it does have loses to the
    # two-second map fragment anyway. A self-contained page sidesteps both:
    # save it, double-click it, F11. It carries the DATA and draws in the
    # browser, so it stays legible at any scale instead of being a picture of
    # a log at one size -- 0.17 MB against 260 inches of PNG.
    try:
        _html = _mh.build_html(_ex)
        st.download_button(
            "⤢  Save as a full-screen page (HTML)",
            data=_html.encode("utf-8"),
            file_name="mudlog_%s.html"
                      % (_ex.header.get("Well Name", "well").replace(" ", "")),
            mime="text/html", key="ml_html_dl",
            help="Opens in any browser with no server. Scroll the log, drag "
                 "the column dividers, zoom, and press F11 for full screen.")
    except Exception as _hexc:
        st.caption("Full-screen page unavailable: %s" % _hexc)

    # THE SAME RENDERER AS THE SAVED PAGE. This drew a static PNG through
    # plot_mudlog first, which meant the mud log in the app and the mud log in
    # the downloaded file were two different things -- the app's had no
    # formation-top strip down the side, did not scroll and could not be
    # zoomed, and there is no reason for a reader to meet two versions of one
    # log. One renderer, one behaviour, and the download is the same page
    # saved rather than a second implementation of it.
    try:
        _frag = _mh.build_html(_ex)          # wrapped: see build_html's note
        st.components.v1.html(_frag, height=820, scrolling=False)
        st.caption(
            "%s · %.0f–%.0f ft · driller's depth · KB %s — scroll the log, "
            "drag the column dividers, zoom with the slider or Ctrl-scroll. "
            "Save it above to open full screen."
            % (_ex.header.get("Well Name", ""), _lo, _hi,
               _ex.header.get("K.B. Elevation", "?")))
    except Exception as _dexc:
        st.caption("Mud log strip failed: %s" % _dexc)


def _unplaced_core_photos(engine, uwi):
    """Photographs catalogued on disk that no dv_well_core_photo row claims.

    HELD IS NOT LOST, BUT IT MUST NOT BE INVISIBLE EITHER. The core loader
    places a photograph by matching its EXIF capture date to a core run's cut
    date, or by reading a depth interval out of the file name. Twenty-four of
    48-X-28's were shot on 4 June 2004 -- seventeen days after the last core
    came up -- and carry a sequence number rather than a depth, so neither
    link fires and the loader holds them with a reason.

    They cannot simply be loaded: dv_well_core_photo requires core_id (an FK
    to a real run), top_depth and base_depth, all NOT NULL, and none of the
    three is known for these. Assigning them by running order would assume the
    photographer worked top down -- likely, unverifiable, and a wrong depth on
    a core photograph is precisely the confident-wrong value that plots and
    gets quoted.

    So they are shown, unplaced and labelled as such, where a geologist can
    look at them and say what they are. That is the fact nobody has, and it is
    not one this code can derive.
    """
    if engine is None or not uwi:
        return []
    try:
        from sqlalchemy import text as _text
        with engine.connect() as _c:
            if not _c.execute(_text(
                    "SELECT OBJECT_ID('file_catalog.GLOBAL_FILE_CATALOG')"
            )).scalar():
                return []
            rows = _c.execute(_text("""
                SELECT g.FILE_PATH, g.FILE_NAME
                FROM file_catalog.GLOBAL_FILE_CATALOG g
                WHERE g.FILE_PATH LIKE '%Core%CD Files%'
                  AND (g.FILE_NAME LIKE '%.jpg' OR g.FILE_NAME LIKE '%.jpeg')
                  AND NOT EXISTS (SELECT 1 FROM dataview.dv_well_core_photo p
                                  WHERE p.file_path = g.FILE_PATH)
                ORDER BY g.FILE_NAME
            """)).fetchall()
        seen, out = set(), []
        for fp, fn in rows:
            key = (fn or "").upper()
            if key in seen:
                continue
            seen.add(key)
            if fp and os.path.exists(fp):
                out.append((fp, fn))
        return out
    except Exception:
        return []


def _render_photo_gallery(engine, uwis):
    """Core photographs at full size, on demand.

    WHY THIS IS NOT IN THE TICKET. The ticket embeds base64 THUMBNAILS at
    320x160 -- it has to, because 48-X-28's 184 photographs are 75 MB on disk
    and 100 MB as base64 in one page, past what a browser will hold. There is
    therefore nothing in the ticket to enlarge: the big pixels were never sent.

    st.image reads the file from disk at request time instead, so nothing is
    embedded and nothing is capped. Streamlit puts its own fullscreen control
    on every image, which is the enlarge: click the icon and the slab fills the
    screen at native resolution.

    ONE CORE RUN AT A TIME. 184 images in one page is slow whether they are
    embedded or served, and a core is read run by run anyway.
    """
    if engine is None or not uwis:
        return
    try:
        import pandas as _pd
        from sqlalchemy import text as _text
    except Exception:
        return

    frames = []
    for _u in uwis:
        try:
            with engine.connect() as _con:
                _df = _pd.read_sql(_text("""
                    SELECT p.uwi, p.core_id, p.photo_type, p.lighting,
                           p.top_depth, p.base_depth, p.tray_num,
                           p.file_path, p.file_name,
                           c.core_num
                    FROM dataview.dv_well_core_photo p
                    LEFT JOIN dataview.dv_well_core c
                           ON c.uwi = p.uwi AND c.core_id = p.core_id
                    WHERE p.uwi = :u AND p.active_ind = 'Y'
                    ORDER BY p.top_depth, p.base_depth, p.lighting, p.tray_num
                """), _con, params={"u": _u})
            if not _df.empty:
                frames.append(_df)
        except Exception:
            continue
    if not frames:
        return
    photos = _pd.concat(frames, ignore_index=True)

    st.markdown("#### Core photographs")
    st.caption(
        "%d photograph(s). Pick a core run, then use the fullscreen control "
        "on any image to enlarge it." % len(photos))

    def _run_label(r):
        n = r.get("core_num")
        cid = r.get("core_id") or "?"
        return ("Core %d" % int(n)) if _pd.notna(n) and n else str(cid)

    photos["_run"] = photos.apply(_run_label, axis=1)
    runs = list(dict.fromkeys(photos["_run"].tolist()))

    cols = st.columns([2, 1, 1])
    # KEY DELIBERATELY NOT "cp_run". _is_action_key() treats anything ending
    # "_run" as a button -- something whose value cannot be set -- so the
    # persist loop skips it and the selectbox silently resets on every page
    # switch. selftest's both-directions sweep caught it; the fix is a name
    # that does not collide, not an exception list that grows.
    run = cols[0].selectbox("Core run", runs, key="cp_corerun")
    sub = photos[photos["_run"] == run]

    kinds = ["All"] + sorted({str(x) for x in sub["photo_type"].dropna()})
    kind = cols[1].selectbox("Type", kinds, key="cp_kind")
    if kind != "All":
        sub = sub[sub["photo_type"].astype(str) == kind]

    lights = ["All"] + sorted({str(x) for x in sub["lighting"].dropna()})
    light = cols[2].selectbox("Lighting", lights, key="cp_light")
    if light != "All":
        sub = sub[sub["lighting"].astype(str) == light]

    # WHITE AND UV ARE THE SAME ROCK. Sorting by depth puts each pair
    # side by side, which is how they are meant to be compared -- the cut
    # shows under ultraviolet and not under white light.
    sub = sub.sort_values(["top_depth", "base_depth", "lighting"],
                          na_position="last")
    if sub.empty:
        st.caption("No photographs match that combination.")
        return

    # THE BIG ONE IS RENDERED IN THE PAGE, NOT IN A FULLSCREEN OVERLAY.
    # Streamlit's own fullscreen control opens and closes again on this page:
    # _watch_seis_choice is an @st.fragment(run_every=2) that keeps the map in
    # step with the second screen, and a rerun tears the overlay down -- which
    # is seen as a flicker. Nothing here depends on that overlay surviving, so
    # the enlarged photograph is simply an image at full container width, and
    # a rerun redraws it exactly where it was.
    _big = st.session_state.get("cp_big")
    if _big and os.path.exists(_big):
        _brow = sub[sub["file_path"] == _big]
        _cap = os.path.basename(_big)
        if not _brow.empty:
            _b = _brow.iloc[0]
            _td, _bd = _b.get("top_depth"), _b.get("base_depth")
            _dep = ("%.0f-%.0f ft" % (_td, _bd)
                    if _pd.notna(_td) and _pd.notna(_bd) and _bd
                    else ("%.0f ft" % _td if _pd.notna(_td) else ""))
            _cap = " · ".join(x for x in (_dep, str(_b.get("photo_type") or ""),
                                          str(_b.get("lighting") or ""),
                                          os.path.basename(_big)) if x)
        st.image(_big, caption=_cap, use_container_width=True)
        if st.button("✕ Close enlarged photo", key="cp_close_btn"):
            st.session_state.pop("cp_big", None)
            st.rerun()
        st.markdown("---")

    missing = 0
    grid = st.columns(3)
    shown = 0
    for _i, r in sub.reset_index(drop=True).iterrows():
        fp = r.get("file_path") or ""
        if not fp or not os.path.exists(fp):
            missing += 1
            continue
        td, bd = r.get("top_depth"), r.get("base_depth")
        if _pd.notna(td) and _pd.notna(bd) and bd:
            dep = "%.0f-%.0f ft" % (td, bd)
        elif _pd.notna(td):
            dep = "%.0f ft" % td
        else:
            dep = ""
        cap = " · ".join(x for x in (dep, str(r.get("photo_type") or ""),
                                     str(r.get("lighting") or "")) if x)
        with grid[shown % 3]:
            try:
                st.image(fp, caption=cap, use_container_width=True)
            except Exception as _imgexc:
                # SAY WHICH FILE. A silently skipped photograph reads as a
                # core that was never photographed.
                st.caption("%s: %s" % (r.get("file_name") or fp, _imgexc))
            # KEY ENDS "_btn" so _is_action_key() excludes it from the persist
            # sweep -- a button's value cannot be assigned, and the crash for
            # trying surfaces on a LATER page.
            if st.button("⤢ Enlarge", key="cp_big_%d_btn" % _i,
                         use_container_width=True):
                st.session_state["cp_big"] = fp
                st.rerun()
        shown += 1
    if missing:
        st.caption("%d photograph(s) registered but not found on disk."
                   % missing)

    # ── the ones no core run claims ───────────────────────────────────────
    _un = _unplaced_core_photos(engine, uwis[0] if uwis else None)
    if _un:
        st.markdown("---")
        st.markdown("##### Unplaced photographs (%d)" % len(_un))
        st.caption(
            "On the CD and catalogued, but not attached to a core run. The "
            "loader places a photograph by its EXIF capture date matching a "
            "run's cut date, or by a depth interval in the file name; these "
            "have neither. Most were shot 4 June 2004, seventeen days after "
            "the last core came up. They are shown so they can be identified "
            "— a depth guessed from running order would be a wrong depth, and "
            "a wrong depth on a core photograph gets quoted.")
        if not st.session_state.get("cp_unplaced"):
            if st.button("Show unplaced photographs", key="cp_unplaced_btn"):
                st.session_state["cp_unplaced"] = True
                st.rerun()
        else:
            if st.button("Hide unplaced photographs",
                         key="cp_unplaced_hide_btn"):
                st.session_state.pop("cp_unplaced", None)
                st.rerun()
            _ug = st.columns(4)
            for _j, (_fp, _fn) in enumerate(_un):
                with _ug[_j % 4]:
                    try:
                        st.image(_fp, caption=_fn, use_container_width=True)
                    except Exception as _uexc:
                        st.caption("%s: %s" % (_fn, _uexc))
                    if st.button("⤢ Enlarge", key="cp_un_%d_btn" % _j,
                                 use_container_width=True):
                        st.session_state["cp_big"] = _fp
                        st.rerun()


def _photos_html(photos_df) -> str:
    if photos_df.empty:
        return "<div style='padding:6px 12px;font-size:12px;color:#999;background:#fff'>No photos registered</div>"
    cards = []
    _shown = photos_df.head(SCOUT_PHOTO_MAX)
    for _, r in _shown.iterrows():
        _fp = r.get("file_path", "")
        try:
            _mt = os.path.getmtime(_fp)
        except OSError:
            _mt = 0.0
        b64 = _photo_to_b64(_fp, _mt)
        if not b64:
            continue
        # ALWAYS JPEG NOW. The thumbnail is re-encoded, so the source
        # extension no longer describes the bytes in the tag -- a .tif
        # labelled image/tiff here would simply not render.
        mime = "image/jpeg"
        td   = r.get("top_depth"); bd = r.get("base_depth")
        dep  = (f"{td:.0f}–{bd:.0f} ft" if pd.notna(td) and td and pd.notna(bd) and bd
                else f"{td:.0f} ft" if pd.notna(td) and td else "")
        lbl  = f"{r.get('photo_type','')} · {r.get('lighting','')} · {dep}"
        cards.append(
            f"<div style='display:inline-block;margin:4px;vertical-align:top;text-align:center'>"
            f"<img src='data:{mime};base64,{b64}' "
            f"style='max-width:320px;max-height:160px;border:1px solid #dde;"
            f"border-radius:4px;display:block'/>"
            f"<div style='font-size:10px;color:#666;margin-top:2px'>{lbl}</div>"
            f"</div>"
        )
    if not cards:
        return "<div style='padding:6px 12px;font-size:12px;color:#999;background:#fff'>Photos on file but not found on disk</div>"
    # SAY WHEN IT IS A SUBSET. A ticket quietly showing 400 of 900 reads as
    # a complete record of the core, which is the one thing a scout ticket
    # must not be wrong about.
    _note = ""
    if len(photos_df) > len(_shown):
        _note = ("<div style='font-size:11px;color:#666;padding:2px 4px'>"
                 "showing the first %d of %d photos, shallowest first"
                 "</div>" % (len(_shown), len(photos_df)))
    return ("<div style='background:#fff;padding:8px'>" + _note
            + "<div style='overflow-x:auto;white-space:nowrap'>"
            + "".join(cards) + "</div></div>")


def _provenance_html(uwi, engine):
    """The PROVENANCE FOOTER for a scout ticket: where this well's data
    came from, and which documents said so.

    Added Aug 5. The ticket above it is assembled from hand-written
    per-section queries, which is right — they are tuned, they alias
    columns readably, and rewriting them to be generic would lose that.
    So this does not touch them. It answers the one question they cannot:
    IS ANY OF THIS TRACEABLE, and to what.

    Counts are gathered by INTROSPECTION rather than a table list, for the
    same reason as everywhere else: a section added to the model next month
    should appear here without an edit, and a report that silently shows
    less is worse than one that errors.

    Never raises. A ticket that fails to render because its footer could
    not count something would be a poor trade.
    """
    try:
        with engine.connect() as cx:
            tabs = [r[0] for r in cx.execute(text(
                "SELECT c.TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS c "
                "JOIN INFORMATION_SCHEMA.TABLES t "
                "  ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME "
                " AND t.TABLE_TYPE='BASE TABLE' "
                "WHERE c.TABLE_SCHEMA='dataview' AND LOWER(c.COLUMN_NAME)='uwi' "
                "  AND c.TABLE_NAME LIKE 'dv[_]%' "
                "  AND c.TABLE_NAME NOT LIKE 'dv[_]r[_]%' "
                "  AND c.TABLE_NAME <> 'dv_global_file_catalog' "
                "  AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS i "
                "              WHERE i.TABLE_SCHEMA=c.TABLE_SCHEMA "
                "                AND i.TABLE_NAME=c.TABLE_NAME "
                "                AND LOWER(i.COLUMN_NAME)='inventory_id')"))]
            if not tabs:
                return ""
            # READ FROM A DOCUMENT is decided by the KIND OF FILE behind
            # the row, not by "has an inventory_id". The bulk loader
            # stamps ids too and registers its CSVs in the catalog, so
            # the older test called a spreadsheet load "catalogued".
            try:
                from dataview.tools.well_report import file_class as _fclass
            except Exception:
                _DOCX = {".pdf", ".docx", ".doc", ".html", ".htm", ".rtf"}

                def _fclass(name):
                    return ("document"
                            if os.path.splitext(str(name or ""))[1].lower()
                            in _DOCX else "data file")

            n_rows = n_doc = n_orphan = 0
            ids = set()
            for t in tabs:
                try:
                    # group by the source FILE, then classify: one query
                    # per table either way, and the extension list stays
                    # in one place.
                    res = cx.execute(text(
                        f"SELECT g.INVENTORY_ID, g.FILE_NAME, COUNT_BIG(*) "
                        f"FROM dataview.[{t}] d "
                        f"LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g "
                        f"       ON g.INVENTORY_ID = d.inventory_id "
                        f"WHERE d.uwi=:u "
                        f"GROUP BY g.INVENTORY_ID, g.FILE_NAME"),
                        {"u": uwi}).fetchall()
                    for iid, fname, n in res:
                        n = int(n or 0)
                        n_rows += n
                        if not fname:
                            continue          # bulk, or an unresolved id
                        if _fclass(fname) == "document":
                            n_doc += n
                            ids.add(iid)
                except Exception:
                    continue
            docs = []
            if ids:
                idl = list(ids)
                for i in range(0, len(idl), 200):
                    chunk = idl[i:i + 200]
                    marks = ", ".join(f":p{j}" for j in range(len(chunk)))
                    prm = {f"p{j}": v for j, v in enumerate(chunk)}
                    try:
                        docs += cx.execute(text(
                            "SELECT INVENTORY_ID, FILE_NAME, FILE_PATH, MODIFIED_DATE "
                            "FROM file_catalog.GLOBAL_FILE_CATALOG "
                            f"WHERE INVENTORY_ID IN ({marks})"), prm).fetchall()
                    except Exception:
                        break
            found = {d[0] for d in docs}
            n_orphan = len(ids - found)          # document ids the catalog lost
    except Exception:
        return ""

    if not n_rows:
        return ""
    pct = (100.0 * n_doc / n_rows) if n_rows else 0.0
    rows_html = ""
    for _iid, name, path, mod in sorted(docs, key=lambda d: str(d[1] or "")):
        href = str(path or "").replace("\\", "/")
        link = (f"<a href='file:///{href}' style='color:#1d4ed8;"
                f"text-decoration:none'>{name}</a>" if href else (name or ""))
        when = f" &middot; {str(mod)[:10]}" if mod else ""
        rows_html += (f"<div style='padding:2px 0'>&#128196; {link}"
                      f"<span style='color:#64748b'>{when}</span></div>")
    if n_orphan:
        rows_html += (f"<div style='padding:2px 0;color:#b45309'>&#9888; "
                      f"{n_orphan} source id(s) with no catalog entry</div>")
    if not rows_html:
        rows_html = ("<div style='color:#64748b'>No source documents &mdash; "
                     "every row here was parsed from a data file, not read from a document.</div>")

    return f"""
      <div style='padding:10px 18px;border-top:2px solid #e2e8f0;
                  background:#f8fafc;font-size:11px;color:#334155'>
        <div style='font-weight:700;letter-spacing:0.6px;color:#334155;
                    margin-bottom:5px'>PROVENANCE</div>
        <div style='margin-bottom:6px'>
          <b>{n_rows:,}</b> row(s) across <b>{len(tabs)}</b> table(s) &middot;
          <b>{n_doc:,}</b> ({pct:.0f}%) read from a document &middot;
          <b>{len(docs)}</b> source document(s)
        </div>
        {rows_html}
      </div>"""


def _build_scout_ticket_html(uwi, well_row, engine=None):
    """Build scout ticket HTML for one well."""
    tops_df = srvy_df = comp_df = prod_df = pd.DataFrame()
    dst_df  = core_df = core_sample_df = photos_df = petro_df = frac_df = pd.DataFrame()

    if engine is not None:
        def _q(sql, params):
            try:
                with engine.connect() as con:
                    return pd.read_sql(text(sql), con, params=params)
            except Exception:
                return pd.DataFrame()

        tops_df = _q("SELECT strat_unit_name, top_depth, base_depth, gross_thickness, lithology FROM dataview.dv_well_formation_top WHERE uwi=:u ORDER BY top_depth", {"u": uwi})
        srvy_df = _q("SELECT TOP 15 s.md, s.incl, s.azim, s.tvd, s.ns_offset, s.ew_offset, s.dls FROM dataview.dv_well_dir_srvy_sta s WHERE s.uwi=:u ORDER BY s.md", {"u": uwi})
        comp_df = _q("""SELECT completion_type, completion_design, well_orientation,
                   completion_date, strat_unit_name, completion_status, primary_fluid,
                   lateral_length_ft, stage_count, avg_cluster_spacing_ft,
                   frac_fluid_system, proppant_type, total_fluid_bbl,
                   total_proppant_lbs, proppant_intensity_lbs_ft
            FROM dataview.dv_well_completion
            WHERE uwi=:u ORDER BY completion_date DESC""", {"u": uwi})
        frac_df = _q("""SELECT TOP 15 stage_num, stage_top_depth, stage_base_depth,
                   num_clusters, cluster_spacing_ft, fluid_volume_bbl, proppant_mass_lbs,
                   isip_psi, avg_treating_pressure_psi, max_rate_bpm
            FROM dataview.dv_well_stimulation WHERE uwi=:u ORDER BY stage_num""", {"u": uwi})
        dst_df  = _q("""SELECT test_date, test_type, top_depth, base_depth,
                   test_result, max_oil_rate, max_gas_rate, api_gravity
            FROM dataview.dv_well_dst WHERE uwi=:u ORDER BY test_date""", {"u": uwi})
        core_df = _q("""SELECT core_num, core_type, core_show, strat_unit_name,
                   top_depth, base_depth, core_length, recovery_length,
                   recovery_pct, core_date, photo_count
            FROM dataview.dv_well_core WHERE uwi=:u ORDER BY top_depth""", {"u": uwi})
        core_sample_df = _q("""SELECT sample_id, sample_type, sample_depth,
                   lithology, hydrocarbon_show,
                   porosity_frac  * 100.0        porosity_pct,
                   permeability_air_md            permeability_md,
                   bulk_density_g_cc              bulk_density,
                   water_saturation_frac * 100.0  water_saturation,
                   oil_saturation_frac  * 100.0   oil_saturation
            FROM dataview.dv_well_core_sample
            WHERE uwi=:u ORDER BY sample_depth""", {"u": uwi})
        photos_df = _q("""SELECT photo_type, lighting, file_path, file_name,
                   top_depth, base_depth, tray_num
            FROM dataview.dv_well_core_photo
            WHERE uwi=:u AND active_ind='Y'
            -- BY DEPTH. tray_num led, and a tray number is an artefact of
            -- how the boxes were stacked; core is read top down, and the
            -- slab frames carry 0 because no tray was recorded, so they
            -- all sorted ahead of everything regardless of depth.
            ORDER BY top_depth, base_depth, lighting, tray_num""",
                       {"u": uwi})
        petro_df = _q("""SELECT z.zone_name, z.strat_unit_name, z.top_depth, z.base_depth,
                   z.net_thickness, z.net_to_gross, z.vsh_avg, z.phi_effective_avg,
                   z.sw_avg, z.perm_avg_md, z.fluid_type, z.pay_flag, z.hcpv,
                   i.interp_name
            FROM dataview.dv_well_petro_zone z
            LEFT JOIN dataview.dv_well_petro_interp i
              ON i.uwi=z.uwi AND i.interp_id=z.interp_id
            WHERE z.uwi=:u ORDER BY z.top_depth""", {"u": uwi})
        prod_df = _q("""
            SELECT pv.period_date prod_date,
                   SUM(CASE WHEN pv.fluid_type='OIL'   THEN ISNULL(pv.volume,0) ELSE 0 END) oil_vol,
                   SUM(CASE WHEN pv.fluid_type='GAS'   THEN ISNULL(pv.volume,0) ELSE 0 END) gas_vol,
                   SUM(CASE WHEN pv.fluid_type='WATER' THEN ISNULL(pv.volume,0) ELSE 0 END) water_vol,
                   MAX(pv.avg_daily_rate) avg_rate
            FROM dataview.dv_prod_volume pv
            JOIN dataview.dv_prod_entity pe ON pe.prod_entity_id=pv.prod_entity_id
            WHERE pe.uwi=:u
            GROUP BY pv.period_date ORDER BY pv.period_date""", {"u": uwi})

    status       = well_row.get("well_status","")
    status_color = STATUS_COLORS.get(str(status).upper(), "#888")
    td  = well_row.get("final_td")
    lat = well_row.get("lat"); lon = well_row.get("lon")
    loc = (f"{lat:.6f}N  {abs(lon):.6f}W"
           if pd.notna(lat) and lat else chr(8212))

    html = f"""
    <div style='font-family:Arial,Helvetica,sans-serif;border:2px solid #334155;
                border-radius:8px;overflow:hidden;margin-bottom:8px;
                background:#ffffff;color:#1e293b'>
      <div style='background:#334155;color:#ffffff;padding:14px 18px;
                  border-bottom:4px solid {status_color};
                  display:flex;justify-content:space-between;align-items:center'>
        <div>
          <div style='font-size:17px;font-weight:700;letter-spacing:1.5px;color:#ffffff'>WELL SCOUT TICKET</div>
          <div style='font-size:12px;color:#cbd5e1;margin-top:2px'>DataView &nbsp;·&nbsp; {well_row.get("operator_name","")}</div>
        </div>
        <span style='background:{status_color};color:#ffffff;padding:4px 14px;
              border-radius:12px;font-size:12px;font-weight:700;letter-spacing:0.5px;
              border:1px solid rgba(255,255,255,0.25)'>{status}</span>
      </div>

      {_section("Well Header")}
      {_tbl(
        _th(["API","Well Name","Well Type","Status"]) +
        _td([well_row.get("api_num",chr(8212)),
             f"<b>{well_row.get('well_name', uwi)}</b>",
             well_row.get("well_type",chr(8212)),
             f"<span style='color:{status_color};font-weight:600'>{status}</span>"]) +
        _th(["Operator","Field","County","State"]) +
        _td([well_row.get("operator_name",chr(8212)), well_row.get("field_name",chr(8212)),
             well_row.get("county",chr(8212)), well_row.get("province_state",chr(8212))], alt=True) +
        _th(["Spud Date","Completion Date","Total Depth MD","Surface Location"]) +
        _td([str(well_row.get("spud_date",""))[:10] or chr(8212),
             str(well_row.get("completion_date",""))[:10] or chr(8212),
             f"{_fmt(td)} ft", loc]) +
        _th(["UWI","KB Elevation","Depth Datum",""]) +
        _td([f"<span style='font-family:monospace'>{uwi}</span>",
             f"{_fmt(well_row.get('kb_elevation'))} ft",
             well_row.get("depth_datum","KB"), ""], alt=True)
      )}

      {_section("Stratigraphy — Formation Tops")}
      {_tbl(_th(["Formation","Top MD (ft)","Base MD (ft)","Net Pay (ft)","Fluid"]) +
        ("".join(_td([f"<b>{r.get('strat_unit_name',chr(8212))}</b>",
                      _fmt(r.get("top_depth")), _fmt(r.get("base_depth")),
                      _fmt(r.get("net_thickness")), _fluid_color(r.get("fluid_type"))],
                     alt=i%2==0) for i, r in tops_df.iterrows())
         if not tops_df.empty else _td(["No formation tops loaded","","","",""]))
      )}

      {_section("Petrophysics — Log Analysis Zones")}
      {_tbl(_th(["Zone","Top MD (ft)","Base MD (ft)","Net (ft)","N/G",
                  "Vsh","Øe","Sw","Perm (mD)","Fluid","Pay"]) +
        ("".join(_td([r.get("zone_name","—"),
                      _fmt(r.get("top_depth")), _fmt(r.get("base_depth")),
                      _fmt(r.get("net_thickness"),",.1"),
                      _fmt(r.get("net_to_gross"),",.2"),
                      _fmt(r.get("vsh_avg"),",.2"),
                      _fmt(r.get("phi_effective_avg"),",.3"),
                      _fmt(r.get("sw_avg"),",.2"),
                      _fmt(r.get("perm_avg_md"),",.3"),
                      r.get("fluid_type","—"),
                      r.get("pay_flag","—")], alt=i%2==0)
                 for i, r in petro_df.iterrows())
         if not petro_df.empty else _td(["No petrophysics","","","","","","","","","",""]))
      )}

      {_section("Directional Survey" + (f" — first {len(srvy_df)} stations" if not srvy_df.empty else ""))}
      {_tbl(_th(["MD (ft)","Inc","Azi","TVD (ft)","N/S (ft)","E/W (ft)","DLS"]) +
        ("".join(_td([_fmt(r.get("md")), _fmt(r.get("incl"),",.2"),
                      _fmt(r.get("azim"),",.2"), _fmt(r.get("tvd")),
                      _fmt(r.get("ns_offset")), _fmt(r.get("ew_offset")),
                      _fmt(r.get("dls"),",.2")], alt=i%2==0)
                 for i, r in srvy_df.iterrows())
         if not srvy_df.empty else _td(["No survey data","","","","","",""]))
      )}

      {_section("DST — Drill Stem Tests")}
      {_tbl(_th(["Test Date","Type","Top MD (ft)","Base MD (ft)",
                  "Result","Max Oil (bbl/d)","Max Gas (Mcf/d)","API Gravity"]) +
        ("".join(_td([str(r.get("test_date",""))[:10], r.get("test_type","—"),
                      _fmt(r.get("top_depth")), _fmt(r.get("base_depth")),
                      _dst_result_color(r.get("test_result")),
                      _fmt(r.get("max_oil_rate")), _fmt(r.get("max_gas_rate")),
                      _fmt(r.get("api_gravity"),",.1")], alt=i%2==0)
                 for i, r in dst_df.iterrows())
         if not dst_df.empty else _td(["No DST data","","","","","","",""]))
      )}

      {_section("Core Runs")}
      {_tbl(_th(["#","Type","Formation","Show","Top MD (ft)","Base MD (ft)",
                  "Length (ft)","Recovery (%)","Date","Photos"]) +
        ("".join(_td([str(r.get("core_num","—")), r.get("core_type","—"),
                      r.get("strat_unit_name","—"), r.get("core_show","—"),
                      _fmt(r.get("top_depth")), _fmt(r.get("base_depth")),
                      _fmt(r.get("core_length"),",.1"),
                      _fmt(r.get("recovery_pct"),",.1"),
                      str(r.get("core_date",""))[:10],
                      str(int(r.get("photo_count") or 0))], alt=i%2==0)
                 for i, r in core_df.iterrows())
         if not core_df.empty else _td(["No core data","","","","","","","","",""]))
      )}

      {_section("Core Sample Analysis")}
      {_tbl(_th(["Sample","Depth (ft)","Type","Por (%)","Perm (mD)",
                  "Bulk Den.","Sw (%)","So (%)","Lithology","Show"]) +
        ("".join(_td([str(r.get("sample_id","—")),
                      _fmt(r.get("sample_depth"),",.1"),
                      r.get("sample_type","—"),
                      _fmt(r.get("porosity_pct"),",.2"),
                      _fmt(r.get("permeability_md"),",.4"),
                      _fmt(r.get("bulk_density"),",.3"),
                      _fmt(r.get("water_saturation"),",.1"),
                      _fmt(r.get("oil_saturation"),",.1"),
                      r.get("lithology","—"),
                      r.get("hydrocarbon_show","—")], alt=i%2==0)
                 for i, r in core_sample_df.iterrows())
         if not core_sample_df.empty else _td(["No sample data","","","","","","","","",""]))
      )}

      {_section("Core Photographs")}
      {_photos_html(photos_df)}

      {_section("Completion Summary")}
      {_tbl(_th(["Completion Date","Type","Orientation","Formation","Lateral (ft)",
                  "Stages","Fluid (bbl)","Proppant (lbs)","Prop Intensity (lbs/ft)","Fluid System"]) +
        (_td([str(comp_df.iloc[0].get("completion_date",""))[:10],
              comp_df.iloc[0].get("completion_type","—"),
              comp_df.iloc[0].get("well_orientation","—"),
              comp_df.iloc[0].get("strat_unit_name","—"),
              _fmt(comp_df.iloc[0].get("lateral_length_ft")),
              _fmt(comp_df.iloc[0].get("stage_count")),
              _fmt(comp_df.iloc[0].get("total_fluid_bbl")),
              _fmt(comp_df.iloc[0].get("total_proppant_lbs")),
              _fmt(comp_df.iloc[0].get("proppant_intensity_lbs_ft")),
              comp_df.iloc[0].get("frac_fluid_system","—")])
         if not comp_df.empty else _td(["No completion data","","","","","","","","",""]))
      )}

      {_section("Frac Stages" + (f" — first {len(frac_df)}" if not frac_df.empty else ""))}
      {_tbl(_th(["Stage","Top MD (ft)","Base MD (ft)","Clusters","Cluster Sp (ft)",
                  "Fluid (bbl)","Proppant (lbs)","ISIP (psi)","Avg Treat (psi)","Max Rate (bpm)"]) +
        ("".join(_td([str(r.get("stage_num","—")),
                      _fmt(r.get("stage_top_depth")), _fmt(r.get("stage_base_depth")),
                      str(r.get("num_clusters","—")),
                      _fmt(r.get("cluster_spacing_ft"),",.1"),
                      _fmt(r.get("fluid_volume_bbl")), _fmt(r.get("proppant_mass_lbs")),
                      _fmt(r.get("isip_psi")), _fmt(r.get("avg_treating_pressure_psi")),
                      _fmt(r.get("max_rate_bpm"),",.1")], alt=i%2==0)
                 for i, r in frac_df.iterrows())
         if not frac_df.empty else _td(["No frac stages","","","","","","","","",""]))
      )}

      {_section("Production Summary")}
      {_tbl(_th(["Date","Oil (bbl)","Gas (Mcf)","Water (bbl)","Avg Rate"]) +
        ("".join(_td([str(r.get("prod_date",""))[:10],
                      _fmt(r.get("oil_vol")), _fmt(r.get("gas_vol")),
                      _fmt(r.get("water_vol")), _fmt(r.get("avg_rate"))],
                     alt=i%2==0) for i, r in prod_df.iterrows()) +
         _td([f"<b>CUMULATIVE ({len(prod_df)} months)</b>",
              f"<b>{_fmt(prod_df['oil_vol'].sum())}</b>",
              f"<b>{_fmt(prod_df['gas_vol'].sum())}</b>",
              f"<b>{_fmt(prod_df['water_vol'].sum())}</b>",""])
         if not prod_df.empty else _td(["No production data","","","",""]))
      )}

      <div style='background:#334155;color:#cbd5e1;font-size:10px;
                  padding:6px 14px;text-align:center;letter-spacing:0.5px'>
        CONFIDENTIAL &nbsp;|&nbsp; {well_row.get("operator_name","")}
        &nbsp;|&nbsp; {well_row.get("well_name", uwi)}
        &nbsp;|&nbsp; DataView Scout Ticket
      </div>
    </div>"""
    # Provenance goes INSIDE the ticket's border, above the confidential
    # strip, so it travels with every export — print, PDF and multi-well.
    if engine is not None:
        prov = _provenance_html(uwi, engine)
        if prov:
            # rfind, not replace: the confidential strip is the LAST block
            # before the ticket's closing div, and matching on a style
            # prefix that also occurs earlier would put the footer in the
            # middle of the ticket. Anchoring on the last occurrence is
            # unambiguous however the styling above it changes.
            _k = html.rfind("<div style='background:#334155;color:#cbd5e1")
            html = (html[:_k] + prov + "\n      " + html[_k:]) if _k > 0 \
                else html + prov
    return html


def _build_gom_scout_ticket_html(well_id, well_row, engine=None):
    """
    Build a scout ticket for one GOM well.

    GOM wells live in dataview_gom.well, which is a header table — there
    are no GOM equivalents of dv_well's aux tables (formation tops,
    surveys, completions, production, cores) yet. So this ticket renders
    the sections the GOM schema can actually fill — Well Header,
    Location & Lease, Depths, Dates — and shows labelled PLACEHOLDER
    panels for the aux sections so the layout matches the dv_well ticket
    and it's obvious what will populate once those tables exist.

    well_row is a GOM well dict (the shape _qry_gom_wells_in_circle /
    _qry_gom_wells_in_bbox return, shadow-cached in tray_well_data). If
    `engine` is provided we refresh from dataview_gom.well by well_id so
    the ticket reflects current data even if the cached dict is stale.
    """
    # Refresh from the table when we can — the cached tray dict may be
    # from an earlier drill. Fall back to the cached row on any failure.
    if engine is not None and well_id:
        try:
            with engine.connect().execution_options(timeout=10) as con:
                _r = con.execute(text("""
                    SELECT CONVERT(VARCHAR(36), well_id) AS well_id,
                           well_name, well_name_suffix, api_well_number,
                           company_name, region,
                           surface_lease_number, bottom_lease_number,
                           bottom_area_code, bottom_block_number,
                           type_code, status_code, casing_cut_code,
                           CONVERT(VARCHAR(10), spud_date,        120) AS spud_date,
                           CONVERT(VARCHAR(10), total_depth_date, 120) AS total_depth_date,
                           CONVERT(VARCHAR(10), status_date,      120) AS status_date,
                           CAST(bh_total_md_ft         AS FLOAT) AS bh_total_md_ft,
                           CAST(true_vertical_depth_ft AS FLOAT) AS true_vertical_depth_ft,
                           CAST(tvd_subsea_ft          AS FLOAT) AS tvd_subsea_ft,
                           CAST(rkb_ft                 AS FLOAT) AS rkb_ft,
                           CAST(kop_ft                 AS FLOAT) AS kop_ft,
                           CAST(water_depth_ft         AS FLOAT) AS water_depth_ft,
                           CAST(surface_latitude  AS FLOAT) AS surface_latitude,
                           CAST(surface_longitude AS FLOAT) AS surface_longitude,
                           CAST(bottom_latitude   AS FLOAT) AS bottom_latitude,
                           CAST(bottom_longitude  AS FLOAT) AS bottom_longitude,
                           source_file
                    FROM dataview_gom.well
                    WHERE well_id = :wid
                """), {"wid": str(well_id)}).fetchone()
                if _r is not None:
                    well_row = dict(_r._mapping)
        except Exception:
            pass  # keep the cached well_row

    # Directional survey — pull this well's stations from
    # dataview_gom.directional_survey_point. A well can have hundreds or
    # thousands of stations, so the ticket shows a summary line plus the
    # first N stations rather than the whole trajectory. We query by
    # well_id (resolved by the well_id-resolution pass); falls back to
    # an empty result on any failure so the section just shows "none".
    _SRVY_PREVIEW_N = 15
    srvy_rows: list = []
    srvy_summary: dict = {}
    if engine is not None and well_id:
        try:
            with engine.connect().execution_options(timeout=10) as con:
                # Summary first — count, max MD, max inclination — cheap
                # aggregate over the indexed well_id.
                _s = con.execute(text("""
                    SELECT COUNT(*)            AS n_stations,
                           MAX(survey_point_md)  AS max_md,
                           MAX(survey_point_tvd) AS max_tvd,
                           MAX(incl_ang)         AS max_incl
                    FROM dataview_gom.directional_survey_point
                    WHERE well_id = :wid
                """), {"wid": str(well_id)}).fetchone()
                if _s is not None:
                    srvy_summary = dict(_s._mapping)
                # Preview rows — first N stations by measured depth.
                _sr = con.execute(text(f"""
                    SELECT TOP ({_SRVY_PREVIEW_N})
                           CAST(survey_point_md  AS FLOAT) AS md,
                           CAST(incl_ang         AS FLOAT) AS incl,
                           CAST(azimuth          AS FLOAT) AS azim,
                           CAST(survey_point_tvd AS FLOAT) AS tvd,
                           CAST(latitude         AS FLOAT) AS lat,
                           CAST(longitude        AS FLOAT) AS lon
                    FROM dataview_gom.directional_survey_point
                    WHERE well_id = :wid
                    ORDER BY survey_point_md
                """), {"wid": str(well_id)}).fetchall()
                srvy_rows = [dict(r._mapping) for r in _sr]
        except Exception:
            srvy_rows = []
            srvy_summary = {}

    def _g(*keys):
        """First non-empty value across possible key names, else em-dash."""
        for k in keys:
            v = well_row.get(k)
            if v is not None and str(v).strip() not in ("", "None", "nan"):
                return v
        return chr(8212)

    # Header fields — tolerate both the refreshed-row names and the
    # circle/bbox dict names (they mostly overlap; tvd differs).
    name   = _g("well_name")
    suffix = well_row.get("well_name_suffix") or ""
    title  = f"{name} {suffix}".strip() if suffix and name != chr(8212) else name
    api    = _g("api_well_number", "api_num")
    op     = _g("company_name", "operator_name")
    status = str(_g("status_code")).strip()
    wtype  = str(_g("type_code")).strip()
    status_disp = _boem_status_label(status) if status != chr(8212) else chr(8212)
    status_col  = _boem_status_color(status) if status != chr(8212) else "#888"

    # Lease / location
    sl     = _g("surface_lease_number")
    bl     = _g("bottom_lease_number")
    area   = well_row.get("bottom_area_code") or ""
    block  = (str(well_row.get("bottom_block_number") or "")).strip()
    area_disp = _boem_area_name(area) if area else chr(8212)
    area_block = f"{area_disp} ({area} {block})".strip() if area else chr(8212)
    region = _g("region")

    def _coords(latk, lonk):
        lat = well_row.get(latk); lon = well_row.get(lonk)
        try:
            if lat is None or lon is None:
                return chr(8212)
            latf = float(lat); lonf = float(lon)
            if latf != latf or lonf != lonf:   # NaN guard
                return chr(8212)
            ns = "N" if latf >= 0 else "S"
            ew = "E" if lonf >= 0 else "W"
            return f"{abs(latf):.6f}{ns}  {abs(lonf):.6f}{ew}"
        except (TypeError, ValueError):
            return chr(8212)
    surf_loc = _coords("surface_latitude", "surface_longitude")
    bott_loc = _coords("bottom_latitude", "bottom_longitude")

    # Depths — tvd column name differs between the refreshed row
    # (true_vertical_depth_ft) and the circle/bbox dict (tvd_ft).
    md   = well_row.get("bh_total_md_ft")
    tvd  = (well_row.get("true_vertical_depth_ft")
            if well_row.get("true_vertical_depth_ft") is not None
            else well_row.get("tvd_ft"))
    tvdss = well_row.get("tvd_subsea_ft")
    rkb  = well_row.get("rkb_ft")
    kop  = well_row.get("kop_ft")
    wd   = well_row.get("water_depth_ft")

    spud = str(_g("spud_date"))[:10]
    tdd  = str(_g("total_depth_date"))[:10]
    std  = str(_g("status_date"))[:10]
    src  = _g("source_file")

    _ph = ("<div style='padding:10px 14px;color:#94a3b8;font-size:12px;"
           "font-style:italic;background:#f8fafc;border:1px solid #e2e8f0;"
           "border-top:none'>Not yet loaded for Gulf of America wells — "
           "this section will populate when the data is available.</div>")

    # ── Directional Survey section ───────────────────────────────────────
    # Real section now that dataview_gom.directional_survey_point is
    # loaded. Shows a summary line (station count, max MD/TVD, max
    # inclination) plus the first N stations. If the well has no survey
    # rows, falls back to a "no survey data" note rather than the
    # generic placeholder — the data path exists, this well just lacks it.
    _n_srvy = srvy_summary.get("n_stations") or 0
    if _n_srvy and srvy_rows:
        _more = _n_srvy - len(srvy_rows)
        _srvy_caption = (
            f"Directional Survey — {_n_srvy:,} station"
            f"{'s' if _n_srvy != 1 else ''}"
            + (f", showing first {len(srvy_rows)}" if _more > 0 else "")
        )
        # Summary strip above the station table
        _srvy_summary_html = (
            "<div style='padding:7px 14px;font-size:12px;color:#475569;"
            "background:#f1f5f9;border:1px solid #cbd5e1;border-top:none'>"
            f"Max MD <b>{_fmt(srvy_summary.get('max_md'), suffix=' ft')}</b>"
            f" &nbsp;·&nbsp; Max TVD "
            f"<b>{_fmt(srvy_summary.get('max_tvd'), suffix=' ft')}</b>"
            f" &nbsp;·&nbsp; Max Inclination "
            f"<b>{_fmt(srvy_summary.get('max_incl'), fmt=',.1', suffix='°')}</b>"
            "</div>"
        )
        # Station rows — MD / Inclination / Azimuth / TVD / Lat / Lon
        _srvy_body = "".join(
            _td([
                _fmt(r.get("md"),   suffix=" ft"),
                _fmt(r.get("incl"), fmt=",.2", suffix="°"),
                _fmt(r.get("azim"), fmt=",.2", suffix="°"),
                _fmt(r.get("tvd"),  suffix=" ft"),
                (f"{r['lat']:.6f}" if r.get("lat") is not None else chr(8212)),
                (f"{r['lon']:.6f}" if r.get("lon") is not None else chr(8212)),
            ], alt=(i % 2 == 1))
            for i, r in enumerate(srvy_rows)
        )
        _srvy_section = (
            _section(_srvy_caption)
            + _srvy_summary_html
            + _tbl(
                _th(["MD", "Inclination", "Azimuth", "TVD",
                     "Latitude", "Longitude"])
                + _srvy_body
            )
        )
    else:
        # Data path exists, this well just has no survey stations.
        _srvy_section = (
            _section("Directional Survey")
            + "<div style='padding:10px 14px;color:#94a3b8;font-size:12px;"
              "font-style:italic;background:#f8fafc;border:1px solid #e2e8f0;"
              "border-top:none'>No directional survey stations found for "
              "this well.</div>"
        )

    html = f"""
    <div style='font-family:Arial,Helvetica,sans-serif;border:2px solid #334155;
                border-radius:8px;overflow:hidden;margin-bottom:8px;
                background:#ffffff;color:#1e293b'>
      <div style='background:#334155;color:#ffffff;padding:14px 18px;
                  border-bottom:4px solid {status_col};
                  display:flex;justify-content:space-between;align-items:center'>
        <div>
          <div style='font-size:17px;font-weight:700;letter-spacing:1.5px;color:#ffffff'>WELL SCOUT TICKET</div>
          <div style='font-size:12px;color:#cbd5e1;margin-top:2px'>DataView &nbsp;·&nbsp; Gulf of America &nbsp;·&nbsp; {op}</div>
        </div>
        <span style='background:{status_col};color:#ffffff;padding:4px 14px;
              border-radius:12px;font-size:12px;font-weight:700;letter-spacing:0.5px;
              border:1px solid rgba(255,255,255,0.25)'>{status_disp}</span>
      </div>

      {_section("Well Header")}
      {_tbl(
        _th(["API","Well Name","Well Type","Status"]) +
        _td([api, f"<b>{title}</b>", wtype, status_disp]) +
        _th(["Operator","Region","Source File",""]) +
        _td([op, region, f"<span style='font-size:11px'>{src}</span>", ""], alt=True) +
        _th(["Well ID (UUID)","","",""]) +
        _td([f"<span style='font-family:monospace;font-size:11px'>{well_id}</span>",
             "", "", ""])
      )}

      {_section("Location & Lease")}
      {_tbl(
        _th(["Surface Lease","Bottom Lease","Area / Block",""]) +
        _td([sl, bl, area_block, ""]) +
        _th(["Surface Location","Bottom Location","",""]) +
        _td([surf_loc, bott_loc, "", ""], alt=True)
      )}

      {_section("Depths")}
      {_tbl(
        _th(["Total Depth MD","True Vertical Depth","TVD Subsea","Water Depth"]) +
        _td([_fmt(md, suffix=" ft"), _fmt(tvd, suffix=" ft"),
             _fmt(tvdss, suffix=" ft"), _fmt(wd, suffix=" ft")]) +
        _th(["RKB Elevation","Kickoff Point (KOP)","",""]) +
        _td([_fmt(rkb, suffix=" ft"), _fmt(kop, suffix=" ft"), "", ""], alt=True)
      )}

      {_section("Dates")}
      {_tbl(
        _th(["Spud Date","Total Depth Date","Status Date",""]) +
        _td([spud or chr(8212), tdd or chr(8212), std or chr(8212), ""])
      )}

      {_section("Stratigraphy — Formation Tops")}
      {_ph}

      {_section("Petrophysics — Log Analysis Zones")}
      {_ph}

      {_srvy_section}

      {_section("Completions & Stimulation")}
      {_ph}

      {_section("Frac Stages")}
      {_ph}

      {_section("Production")}
      {_ph}

      <div style='background:#334155;color:#cbd5e1;font-size:10px;
                  padding:6px 14px;text-align:center;letter-spacing:0.5px'>
        CONFIDENTIAL &nbsp;|&nbsp; {op}
        &nbsp;|&nbsp; {title}
        &nbsp;|&nbsp; DataView Scout Ticket &nbsp;·&nbsp; Gulf of America
      </div>
    </div>"""
    return html


def _build_batch_pdf(selected_uwis, wells_df, engine):
    """Generate a multi-well PDF with one scout ticket per well."""
    all_html = ""
    for uwi in selected_uwis:
        rows = wells_df[wells_df["uwi"] == uwi]
        if rows.empty:
            continue
        all_html += _build_scout_ticket_html(uwi, rows.iloc[0], engine)
    return _scout_ticket_pdf(all_html, f"{len(selected_uwis)} wells")


def _build_export_excel(selected_uwis, wells_df, engine):
    """Build a multi-sheet Excel workbook matching the scout ticket sections."""
    import io
    buf  = io.BytesIO()
    w_df = wells_df[wells_df["uwi"].isin(selected_uwis)].copy()
    w_df = w_df.drop(columns=["lat","lon"], errors="ignore")
    tops_df = srvy_df = comp_df = stim_df = prod_df = pd.DataFrame()
    if engine is not None and selected_uwis:
        ph     = ",".join([f":u{i}" for i in range(len(selected_uwis))])
        params = {f"u{i}": u for i, u in enumerate(selected_uwis)}
        def _q(sql):
            try:
                with engine.connect() as con:
                    return pd.read_sql(text(sql), con, params=params)
            except Exception:
                return pd.DataFrame()
        tops_df = _q(f"SELECT uwi, strat_unit_name formation, top_depth, base_depth, net_thickness, fluid_type FROM dataview.dv_well_formation_top WHERE uwi IN ({ph}) ORDER BY uwi, top_depth")
        srvy_df = _q(f"SELECT s.uwi, s.md, s.incl, s.azim, s.tvd, s.ns_offset, s.ew_offset, s.dls FROM dataview.dv_well_dir_srvy_sta s WHERE s.uwi IN ({ph}) ORDER BY s.uwi, s.md")
        comp_df = _q(f"""SELECT c.uwi, c.completion_date, c.lateral_length,
                   s.num_stages, s.total_fluid_bbl, s.total_proppant_lbs,
                   s.cluster_spacing_ft, s.max_treatment_pressure_psi
            FROM dataview.dv_well_completion c
            LEFT JOIN dataview.dv_well_stimulation s ON s.uwi=c.uwi AND s.completion_id=c.completion_id
            WHERE c.uwi IN ({ph}) ORDER BY c.uwi""")
        prod_df = _q(f"""SELECT pe.uwi, pv.period_date prod_date,
                   SUM(CASE WHEN pv.fluid_type='OIL'   THEN ISNULL(pv.volume,0) ELSE 0 END) oil_vol,
                   SUM(CASE WHEN pv.fluid_type='GAS'   THEN ISNULL(pv.volume,0) ELSE 0 END) gas_vol,
                   SUM(CASE WHEN pv.fluid_type='WATER' THEN ISNULL(pv.volume,0) ELSE 0 END) water_vol
            FROM dataview.dv_prod_volume pv
            JOIN dataview.dv_prod_entity pe ON pe.prod_entity_id=pv.prod_entity_id
            WHERE pe.uwi IN ({ph})
            GROUP BY pe.uwi, pv.period_date ORDER BY pe.uwi, pv.period_date""")
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            def _write(df, sheet):
                if df.empty:
                    pd.DataFrame({"Note":["No data"]}).to_excel(writer, sheet_name=sheet, index=False)
                else:
                    df.to_excel(writer, sheet_name=sheet, index=False)
                    ws = writer.sheets[sheet]
                    ws.freeze_panes = "A2"
                    for col in ws.columns:
                        w = max((len(str(c.value)) for c in col if c.value), default=8)
                        ws.column_dimensions[col[0].column_letter].width = min(w+2, 40)
            _write(w_df,    "Well Header")
            _write(tops_df, "Formation Tops")
            _write(srvy_df, "Directional Survey")
            _write(comp_df, "Completion Summary")
            _write(prod_df, "Production Summary")
    except Exception:
        return b""
    buf.seek(0)
    return buf.read()




# =============================================================================
# AI NATURAL LANGUAGE FILTER
# =============================================================================

def _ai_spec_to_sql(spec) -> str:
    """Render an AI filter spec as the equivalent SQL WHERE clause.

    FOR READING, NOT FOR RUNNING. The AI returns a JSON spec applied with
    pandas over the wells already loaded — no SQL is generated or sent
    anywhere. But "which wells did it actually pick?" is a SQL-shaped
    question, and a list of JSON operators answers it poorly, so the spec is
    shown in the form people read fastest.

    Where the two differ the SQL follows the SEMANTICS rather than the syntax:
    `contains` is a case-insensitive substring match in pandas, so it renders
    as UPPER(col) LIKE '%…%' rather than a plain LIKE that would imply
    case-sensitivity the filter does not have.
    """
    _OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

    def _lit(v):
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    filters = (spec or {}).get("filters") or []
    if not filters:
        return "-- no filters returned - every loaded well matches"
    parts = []
    for f in filters:
        col = str(f.get("field", "?"))
        op = str(f.get("op", "eq")).lower()
        val = f.get("value")
        if op == "in" and isinstance(val, (list, tuple)):
            parts.append(col + " IN (" + ", ".join(_lit(v) for v in val) + ")")
        elif op == "contains":
            parts.append("UPPER(" + col + ") LIKE '%" + str(val).upper() + "%'")
        elif op in _OPS:
            parts.append(col + " " + _OPS[op] + " " + _lit(val))
        else:
            parts.append("/* unsupported op " + repr(op) + " on " + col + " */")
    return "SELECT *\nFROM   wells\nWHERE  " + "\n  AND  ".join(parts)


# ── AI filter, database mode ────────────────────────────────────────────────
# The model NEVER writes SQL. It returns the same JSON spec it always has, and
# this translates that spec against a whitelist. A model that emitted SQL would
# be one prompt away from writing anything at all against a production
# database; a model that emits {field, op, value} can only name a column the
# schema actually has.
#
# The whitelist is REFLECTED from dv_well rather than typed out here — "any
# attribute in the well header" is the requirement, a hand-list is always a
# subset of it, and a hand-list silently rots when a column is added.
_AI_KIND = {
    "int": "num", "bigint": "num", "smallint": "num", "tinyint": "num",
    "decimal": "num", "numeric": "num", "float": "num", "real": "num",
    "money": "num", "smallmoney": "num",
    "date": "date", "datetime": "date", "datetime2": "date",
    "smalldatetime": "date", "datetimeoffset": "date",
    "bit": "num",
}

# Child tables a well can be asked about. These are not columns — "has core
# data" is an EXISTS, and no amount of column whitelisting expresses it.
AI_HAS_TABLES = {
    "has_core":        ("dataview.dv_well_core", "core analysis"),
    "has_core_photos": ("dataview.dv_well_core_photo", "core photographs"),
    "has_tops":        ("dataview.dv_well_formation_top", "formation tops"),
    "has_dst":         ("dataview.dv_well_dst", "drill stem tests"),
    "has_survey":      ("dataview.dv_well_dir_srvy_hdr", "directional surveys"),
    "has_production":  ("dataview.dv_prod_entity", "production"),
    "has_petro":       ("dataview.dv_well_petro_interp", "petrophysical interpretation"),
    "has_stimulation": ("dataview.dv_well_stimulation", "frac / stimulation"),
    "has_casing":      ("dataview.dv_well_casing", "casing"),
    "has_perforations": ("dataview.dv_well_perforation", "perforations"),
    "has_logs":        ("dataview.dv_well_log", "well logs"),
    "has_completion":  ("dataview.dv_well_completion", "completion"),
}


@st.cache_data(ttl=3600, show_spinner=False)
def _ai_db_columns(_engine, _v: int = 1) -> dict:
    """{field: (sql_expression, kind)} for every column on dv_well.

    Reflected, so a schema change is picked up without an edit here. The three
    joined values are added by hand because they are expressions rather than
    columns, and they mirror exactly what _qry_wells_bcp SELECTs so a filter
    matches the value the user can see.
    """
    out = {}
    try:
        with _engine.connect() as con:
            rows = con.execute(text(
                "SELECT c.name, t.name AS ty FROM sys.columns c "
                "JOIN sys.types t ON t.user_type_id = c.user_type_id "
                "WHERE c.object_id = OBJECT_ID('dataview.dv_well') "
                "ORDER BY c.column_id")).fetchall()
        for _name, _ty in rows:
            # geography / varbinary can't be compared from a text spec
            if str(_ty).lower() in ("geography", "geometry", "varbinary",
                                    "binary", "image", "xml"):
                continue
            out[_name] = ("w.[" + _name + "]",
                          _AI_KIND.get(str(_ty).lower(), "text"))
    except Exception as exc:
        print(f"[ai_db_columns] reflection failed: {exc}")
    out["operator_name"] = ("COALESCE(w.operator_name, ba.ba_name, 'Unknown')", "text")
    out["field_name"] = ("COALESCE(w.field_name, f.field_name, 'Unknown')", "text")
    out["basin_name"] = ("ISNULL(f.basin_name, 'Unknown')", "text")

    # ── lease, resolved WHERE THE WELL IS ────────────────────────────────
    # dv_well has no lease column and never will: a well is not stamped with
    # a tract, it simply falls inside one. So these are correlated spatial
    # lookups, which makes lease an ordinary FILTERABLE FIELD -- "in lease 5"
    # composes with depth and spud date through the same machinery as every
    # other column, instead of needing an operator of its own.
    #
    # TOP 1 is honest here rather than lossy: the tracts tile without gaps or
    # overlaps (gen_synthetic_leases verifies both), so a located well is in
    # exactly one. If that ever stops being true this returns one of them
    # rather than multiplying the row, which is the safer failure.
    #
    # Only offered when there ARE tracts. A field the model is told about but
    # that can never match is worse than an absent one: it produces a filter
    # that silently returns nothing and looks like a data problem.
    try:
        with _engine.connect() as con:
            _has_tracts = con.execute(text(
                "SELECT COUNT(*) FROM dataview.dv_land_tract "
                "WHERE geog IS NOT NULL")).scalar()
    except Exception:
        _has_tracts = 0
    if _has_tracts:
        _pt = ("geography::Point(w.surface_latitude, w.surface_longitude, "
               "4326)")
        for _fld, _col in (("lease_name", "tract_name"),
                           ("lease_number", "lease_number"),
                           ("lease_operator", "operator_name")):
            out[_fld] = (
                "(SELECT TOP 1 lt.[%s] FROM dataview.dv_land_tract lt "
                " WHERE w.surface_latitude IS NOT NULL "
                "   AND lt.geog.STIntersects(%s) = 1)" % (_col, _pt),
                "text")
    return out


@st.cache_data(ttl=600, show_spinner=False)
def _well_lease_map(_engine, _v: int = 1) -> dict:
    """{uwi: (tract_name, lease_number, lease_operator)} for located wells.

    ONE QUERY, NOT ONE PER WELL. The spatial join runs once for the whole
    database and is cached; the alternative -- resolving each well's tract as
    the map draws -- is the shape that put 157 round trips in a single render.

    Exists because the AI filter has TWO modes and they read leases
    differently. In database mode the correlated subquery in _ai_db_columns
    answers a lease clause. In loaded mode the filter is pandas over well
    DICTS and reads w.get(field), so without these keys every lease clause is
    unevaluable -- and _apply_ai_filter counts unevaluable as NOT matching.
    The filter would return nothing and read as a data problem rather than a
    missing column.
    """
    out = {}
    try:
        with _engine.connect() as con:
            for r in con.execute(text("""
                SELECT RTRIM(w.uwi), lt.tract_name, lt.lease_number,
                       lt.operator_name
                  FROM dataview.dv_well w
                  JOIN dataview.dv_land_tract lt
                    ON lt.geog.STIntersects(
                         geography::Point(w.surface_latitude,
                                          w.surface_longitude, 4326)) = 1
                 WHERE w.surface_latitude IS NOT NULL
                   AND lt.geog IS NOT NULL
                   -- CURRENT LEASES ONLY. "First tract wins" below is safe
                   -- while tracts do not overlap, which was true of the 34
                   -- synthetic ones and is emphatically false of real BLM
                   -- data: Natrona County's leases sum to 9.2M acres in a
                   -- 3.4M-acre county because a century of expired ones
                   -- stacks on the same ground. Without this clause a well
                   -- reports whichever closed 1962 lease sorts first.
                   -- lease_status IS NULL keeps the synthetic tracts, which
                   -- predate the column.
                   AND (lt.lease_status IS NULL
                        OR lt.lease_status = 'Authorized')
                   AND ISNULL(lt.active_ind, 'Y') = 'Y'""")).fetchall():
                # First tract wins if the tiling ever overlaps -- the same
                # choice the TOP 1 subquery makes, so both modes agree.
                out.setdefault(str(r[0]), (r[1], r[2], r[3]))
    except Exception as exc:
        print(f"[well_lease_map] {exc}")
    return out


# ── layer-toggle defaults, in ONE place ────────────────────────────────────
# These are read twice: to seed map_mode when the page is first entered, and
# as the `value=` for the toggles themselves. Written out twice they disagreed
# -- map_mode was seeded "none" while the toggles derived "h3" -- and the
# derivation below reacts to a mismatch by calling st.rerun(). So every single
# entry to the map page paid a WHOLE EXTRA RENDER, before anything was on
# screen, to correct a disagreement between two constants.
# BOTH OFF ON ARRIVAL. H3 defaulting on meant opening the map immediately
# queried and drew a continental density layer -- the "Rendering map…" wait
# before the operator had asked for anything. An empty basemap opens fast and
# says plainly that the next move is theirs; either layer is one click away.
_H3_DEFAULT_ON = False
_WELLS_DEFAULT_ON = False


def _default_map_mode() -> str:
    """The mode the layer toggles will derive on a fresh session.

    Wells wins when both are on -- the same rule the derivation uses, and the
    reason this is a function rather than a third literal.
    """
    return ("wells" if _WELLS_DEFAULT_ON
            else ("h3" if _H3_DEFAULT_ON else "none"))


_AI_SQL_OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _ai_literal(val, kind):
    """Type-checked SQL literal, or None if the value doesn't fit the column.

    Values reach here from a language model, so each is validated against the
    column's TYPE before it goes near the statement: a number must parse as a
    number, a date must look like a date, and text has its quotes doubled and
    its length capped. A value that fails returns None and the clause is
    dropped and reported rather than guessed at.
    """
    if val is None:
        return None
    if kind == "num":
        try:
            return str(float(val))
        except (TypeError, ValueError):
            return None
    if kind == "date":
        t = str(val).strip()[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            return None
        return "'" + t + "'"
    t = str(val).strip()[:200]
    return "'" + t.replace("'", "''") + "'"


def _ai_spec_to_where(spec, columns=None):
    """(where_fragment, [rejected notes]). The fragment starts with AND."""
    columns = columns or {}
    filters = (spec or {}).get("filters") or []
    parts, rejected = [], []
    for f in filters:
        field = str(f.get("field", ""))
        op = str(f.get("op", "eq")).lower()
        val = f.get("value")

        # EXISTS against a child table — "wells that have core data"
        if field in AI_HAS_TABLES:
            _tbl, _label = AI_HAS_TABLES[field]
            _want = val not in (False, 0, "false", "False", "no", "No", None)
            parts.append(
                ("EXISTS" if _want else "NOT EXISTS")
                + f" (SELECT 1 FROM {_tbl} _x WHERE _x.uwi = w.uwi)")
            continue

        col = columns.get(field)
        if not col:
            rejected.append(f"{field or '(blank)'} — not a column on dv_well")
            continue
        expr, kind = col
        if op == "in":
            vals = [_ai_literal(v, kind) for v in (val or [])]
            vals = [v for v in vals if v is not None]
            if not vals:
                rejected.append(f"{field} IN — no usable values")
                continue
            parts.append(f"{expr} IN ({', '.join(vals)})")
        elif op == "contains":
            lit = _ai_literal(val, "text")
            if lit is None:
                rejected.append(f"{field} contains — unusable value")
                continue
            parts.append(f"UPPER({expr}) LIKE UPPER('%' + {lit} + '%')")
        elif op in _AI_SQL_OPS:
            lit = _ai_literal(val, kind)
            if lit is None:
                rejected.append(f"{field} {op} {val!r} — not a valid {kind}")
                continue
            parts.append(f"{expr} {_AI_SQL_OPS[op]} {lit}")
        else:
            rejected.append(f"{field} — operator {op!r} not supported")
    if not parts:
        return "", rejected
    return " AND (" + " AND ".join(parts) + ")", rejected


@st.cache_data(ttl=300, show_spinner=False)
def _qry_child_rows(_engine, table: str, uwis: tuple, limit: int = 2000):
    """Rows from a child table for a set of wells, as a DataFrame.

    Separate from the FILTER path on purpose. "Wells that have core data" is a
    question about which wells to show; "core data for well X" is a question
    about what to put in a grid. The first narrows the map, the second fills a
    table, and conflating them gives you a map of one dot and no numbers.

    `table` is looked up in AI_HAS_TABLES rather than taken from the model, so
    the only tables reachable are ones already approved for the presence flags.
    """
    import pandas as _pd
    if not uwis or table not in {t for t, _l in AI_HAS_TABLES.values()}:
        return _pd.DataFrame()
    _in = ", ".join("'" + str(u).replace("'", "''") + "'" for u in uwis[:500])
    sql = text(f"SELECT TOP {int(limit)} * FROM {table} WHERE uwi IN ({_in})")
    try:
        with _engine.connect() as con:
            rows = con.execute(sql).fetchall()
            cols = list(con.execute(sql).keys()) if rows else []
        df = _pd.DataFrame([dict(zip(cols, r)) for r in rows]) if rows \
            else _pd.DataFrame()
        # Drop the audit furniture — it is the same on every row and pushes
        # the actual measurements off the right of the grid.
        _noise = {"row_created_by", "row_created_date", "row_changed_by",
                  "row_changed_date", "active_ind", "INVENTORY_ID"}
        return df[[c for c in df.columns if c not in _noise]] if len(df) else df
    except Exception as exc:
        print(f"[child_rows] {table}: {exc}")
        import pandas as _p
        return _p.DataFrame()


# The dozen attributes a well list is actually read for: who, what, where,
# when, how deep. Everything else on dv_well is available behind the "all
# columns" toggle — a fifty-column grid is a wall of text, and thirty of them
# are audit fields or identifiers nobody scans by eye.
WELL_HEADER_CORE = [
    "uwi", "well_name", "operator", "field", "well_type", "well_status",
    "county", "province_state", "spud_date", "completion_date", "final_td",
    "surface_latitude", "surface_longitude",
]


@st.cache_data(ttl=300, show_spinner=False)
def _qry_well_header_rows(_engine, uwis: tuple, limit: int = 2000):
    """The FULL dv_well row for a set of wells, as a DataFrame.

    Distinct from the child-table fetch: "well header data" is not a child
    dataset, it is the well record itself — and distinct from what the map
    already holds, because _qry_wells_bcp selects ~22 columns for drawing and
    the header has fifty. Asking to SEE the header should return the header,
    not the subset that happened to be needed for markers.
    """
    import pandas as _pd
    if not uwis:
        return _pd.DataFrame()
    _in = ", ".join("'" + str(u).replace("'", "''") + "'" for u in uwis[:500])
    # NOT "SELECT w.*". dv_well carries a geography column (geog), and the
    # driver raises reading it — the whole query failed and surfaced as
    # "could not be read back", which sounds like missing wells rather than an
    # unreadable data type. _ai_db_columns already excludes geography,
    # varbinary and friends, so reuse it and name the columns explicitly.
    _cols = [c for c in _ai_db_columns(_engine)
             if c not in ("operator_name", "field_name", "basin_name")]
    if not _cols:
        return _pd.DataFrame()
    _sel = ", ".join("w.[" + c + "]" for c in _cols)
    sql = text(
        f"SELECT TOP {int(limit)} {_sel}, "
        "COALESCE(w.operator_name, ba.ba_name, 'Unknown') AS operator, "
        "ISNULL(f.field_name, 'Unknown') AS field "
        "FROM dataview.dv_well w "
        "LEFT JOIN dataview.dv_business_associate ba ON ba.ba_id = w.operator_ba_id "
        "LEFT JOIN dataview.dv_field f ON f.field_id = w.field_id "
        f"WHERE w.uwi IN ({_in})")
    try:
        with _engine.connect() as con:
            res = con.execute(sql)
            cols = list(res.keys())
            rows = res.fetchall()
        if not rows:
            return _pd.DataFrame()
        df = _pd.DataFrame([dict(zip(cols, r)) for r in rows])
        _noise = {"row_created_by", "row_created_date", "row_changed_by",
                  "row_changed_date", "geog", "h3_r4", "h3_r5", "h3_r6",
                  "h3_r7", "h3_coord_hash"}
        _keep = [c for c in df.columns if c not in _noise]
        # Drop columns that are empty for every matched well — a fifty-column
        # grid where thirty are blank is harder to read than one that isn't.
        _keep = [c for c in _keep if df[c].notna().any()]
        return df[_keep]
    except Exception as exc:
        print(f"[well_header_rows] {exc}")
        import pandas as _p
        _df = _p.DataFrame()
        _df.attrs["error"] = f"{type(exc).__name__}: {exc}"
        return _df


# Model for the AI Well Filter. A hardcoded dated string ("claude-sonnet-4-
# 20250514") eventually 404s when that snapshot is retired, and the failure
# surfaces as an opaque error in the UI. Named alias + an env override, so a
# model change is a .env edit rather than a code edit.
AI_MODEL = os.environ.get("DATAVIEW_AI_MODEL", "claude-sonnet-5")

# Ceiling for a database-mode AI search. A vague question against a real table
# would otherwise try to draw the whole thing.
AI_DB_LIMIT = int(os.environ.get("DATAVIEW_AI_DB_LIMIT", "5000"))


def _ai_filter_wells(question: str, sample_wells: list[dict],
                     _engine=None) -> tuple[dict | None, str]:
    """
    Send a natural language question to Claude via anthropic SDK.
    Returns (filter_spec, error_message).
    """
    try:
        import anthropic

        # Build column summary from sample
        cols = {}
        for w in sample_wells[:20]:
            for k, v in w.items():
                if k not in cols:
                    cols[k] = type(v).__name__
        col_summary = ", ".join(f"{k} ({t})" for k, t in cols.items())

        statuses  = sorted({w.get("well_status","") for w in sample_wells if w.get("well_status")})
        wtypes    = sorted({w.get("well_type","")   for w in sample_wells if w.get("well_type")})
        operators = sorted({w.get("operator_name","") for w in sample_wells if w.get("operator_name")})[:10]
        counties  = sorted({w.get("county","")      for w in sample_wells if w.get("county")})[:15]

        # The model can only name what it is told about. Previously the column
        # list came from the SAMPLE WELLS — i.e. the 22 columns the map query
        # happens to return — so two thirds of the well header was invisible to
        # it and "wells with core data" had no vocabulary at all.
        _db_cols = _ai_db_columns(_engine) if _engine is not None else {}
        # What lease numbers actually EXIST, so the model asks for one that
        # does. Empty when no tracts are loaded, which drops the whole lease
        # paragraph from the prompt rather than advertising a dead field.
        _lease_hint = ""
        if "lease_number" in _db_cols and _engine is not None:
            try:
                with _engine.connect() as _lc:
                    _lns = [str(r[0]) for r in _lc.execute(text(
                        "SELECT lease_number FROM dataview.dv_land_tract "
                        "WHERE lease_number IS NOT NULL "
                        "ORDER BY TRY_CAST(lease_number AS int)")).fetchall()]
                if _lns:
                    _lease_hint = "in use: " + ", ".join(_lns[:40])
            except Exception:
                _lease_hint = ""
        _col_list = ", ".join(sorted(_db_cols)) if _db_cols else col_summary
        _has_list = "\n".join(
            f"  {k} — true if the well has {lbl}"
            for k, (_t, lbl) in sorted(AI_HAS_TABLES.items()))

        system = (
            "You are a petroleum data filter assistant.\n"
            "Convert natural language questions into a JSON filter spec for well data.\n"
            "Return ONLY valid JSON — no explanation, no markdown, no backticks.\n\n"
            f"Available columns: {_col_list}\n\n"
            "Presence flags — use these for questions about what DATA a well\n"
            "has. Give them a boolean value:\n"
            f"{_has_list}\n\n"
            "If the question asks to SEE or LIST a dataset rather than to find\n"
            "wells (e.g. 'show me core data for well 42001205750000'), add a\n"
            '"show" key naming the dataset — one of: well_header, '
            + ", ".join(sorted(AI_HAS_TABLES)) + "\n"
            "  well_header = the wells themselves as a table of their own\n"
            "  attributes (use it for 'show me the well header data',\n"
            "  'list those wells', 'give me a table of wells')\n"
            "and put any well identifier in filters as a uwi condition.\n\n"
            f"Sample values:\n"
            f"  well_status: {statuses}\n"
            f"  well_type: {wtypes}\n"
            f"  operator_name (sample): {operators}\n"
            f"  county (sample): {counties}\n"
            # NAME THE VALUES, or "lease 5" gets guessed at. lease_number is
            # a plain number held as TEXT, so the model must be told to send
            # "5" and not 5 -- _ai_literal type-checks against the column and
            # would otherwise drop the clause and report it as unusable.
            + (f"  lease_number: text, a plain number like \"5\" — "
               f"{_lease_hint}\n"
               "  lease_name: the tract name, e.g. \"Sundance Tract\"\n"
               "  lease_operator: who holds the lease, which is NOT\n"
               "    necessarily the well's operator_name\n"
               if _lease_hint else "")
            + "\n"
            'Return this exact JSON structure:\n'
            '{\n'
            '  "filters": [\n'
            '    {"field": "<column_name>", "op": "<eq|ne|gt|gte|lt|lte|contains|in>", "value": <value>}\n'
            '  ],\n'
            '  "description": "<short human description of the filter>"\n'
            '}\n\n'
            "Rules:\n"
            "- Use exact column names from the available columns list\n"
            "- For text comparisons use uppercase values to match the data\n"
            "- contains is case-insensitive substring match\n"
            "- in value must be a list\n"
            "- combine conditions freely; they are ANDed together\n"
            "- Example: 'wells deeper than 10000 ft operated by Anadarko with\n"
            "  core data' ->\n"
            '  {"filters": [{"field": "final_td", "op": "gt", "value": 10000},\n'
            '               {"field": "operator_name", "op": "contains", "value": "Anadarko"},\n'
            '               {"field": "has_core", "op": "eq", "value": true}],\n'
            '   "description": "Wells over 10,000 ft by Anadarko with core data"}\n'
            "- final_td, lat, lon are numeric (float)\n"
            '- If the question cannot be answered, return {"filters": [], "description": "Could not interpret query"}'
        )

        import os
        from pathlib import Path
        # Try dotenv first
        try:
            from dotenv import load_dotenv
            # Explicitly find .env relative to this file
            _env = Path(__file__).parent / ".env"
            load_dotenv(_env if _env.exists() else None)
        except ImportError:
            pass
        # Manual fallback — read .env directly
        _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not _api_key:
            _env_path = Path(__file__).parent / ".env"
            if _env_path.exists():
                for line in _env_path.read_text().splitlines():
                    if line.startswith("ANTHROPIC_API_KEY"):
                        _api_key = line.split("=", 1)[-1].strip().strip('"').strip("'")
                        break
        if not _api_key:
            return None, "ANTHROPIC_API_KEY not found in .env file"
        client = anthropic.Anthropic(api_key=_api_key)
        msg = client.messages.create(
            model=AI_MODEL,
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        # Take the first TEXT block, not the first block. A model that returns
        # extended thinking puts a ThinkingBlock at index 0, and content[0].text
        # then raises "'ThinkingBlock' object has no attribute 'text'" — which
        # reads like a broken filter rather than a response shape we didn't
        # expect. Also joins multiple text blocks, since a long answer can be
        # split across several.
        _parts = [getattr(_b, "text", "") for _b in (msg.content or [])
                  if getattr(_b, "type", "") == "text"
                  or (not hasattr(_b, "type") and hasattr(_b, "text"))]
        text = "".join(_parts).strip()
        if not text:
            _kinds = ", ".join(sorted({getattr(_b, "type", type(_b).__name__)
                                       for _b in (msg.content or [])})) or "none"
            return None, (f"The model returned no text to parse "
                          f"(blocks: {_kinds}).")
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        # A model may narrate around the JSON despite being told not to; take
        # the outermost object rather than failing on the prose.
        try:
            return json.loads(text), ""
        except json.JSONDecodeError:
            _m = re.search(r"\{.*\}", text, re.S)
            if _m:
                return json.loads(_m.group(0)), ""
            raise
    except Exception as e:
        _msg = str(e)
        # A retired or mistyped model name comes back as a bare 404 with a
        # request id — accurate and useless to whoever is looking at the map.
        # Say what to change and where.
        if "not_found_error" in _msg or "404" in _msg:
            _msg = (f"Model '{AI_MODEL}' was not found. Set DATAVIEW_AI_MODEL "
                    f"in your .env to a current model name, or update "
                    f"AI_MODEL in page_well_map.py.")
        return None, _msg


def _cmp_values(wval, val, op):
    """Compare one well value against a filter value. None = not comparable.

    ORDER MATTERS. Numeric first, then string. The previous version tried only
    float() and swallowed the failure with a bare except, so a date filter —
    float("2020-01-01") raises — left `match` untouched at True and matched
    EVERY well. "Wells drilled since 2020" silently returned everything, which
    is worse than returning nothing: nothing looks broken, everything looks
    correct.

    ISO dates (YYYY-MM-DD) order correctly as strings, so the text fallback
    handles them without a date parser. Mixed formats will not compare sanely,
    which is why an incomparable pair returns None rather than guessing — the
    caller reports it instead of hiding it.
    """
    if wval is None or str(wval).strip().upper() in ("", "NULL", "NONE", "NAN"):
        # bcp writes an unset value as the literal text "NULL", and "NULL"
        # sorts AFTER "2020-01-01", so a missing date would satisfy a
        # "since 2020" filter. Treat every spelling of absent as absent.
        return None
    try:
        a, b = float(wval), float(val)
        return (a > b if op == "gt" else a >= b if op == "gte"
                else a < b if op == "lt" else a <= b)
    except (TypeError, ValueError):
        pass
    a, b = str(wval).strip().upper(), str(val).strip().upper()
    if not b:
        return None
    return (a > b if op == "gt" else a >= b if op == "gte"
            else a < b if op == "lt" else a <= b)


# ── SPATIAL SEARCH AGAINST STORED GEOMETRY ─────────────────────────────────
# The whole point of keeping seismic as `geography` rather than as a drawing
# is that it can be asked questions. "Which wells lie within 500 m of line C"
# is one STDistance away — but nothing on this page could ask it, and the AI
# filter cannot either: it applies a spec in PANDAS over already-loaded wells,
# so it has no access to geometry and no business gaining any.
#
# THIS IS THE DETERMINISTIC HALF, and it is built first on purpose. A picker
# and a distance box answer the question with no model involved. Once this is
# proven, a `near` clause in the AI spec becomes a NAME for this operation —
# the model chooses and parameterises it, exactly as it does with the Data
# Assistant's transform catalogue, and never writes SQL.
#
# Building it the other way round leaves two failure modes indistinguishable:
# a wrong answer could be a bad query or a misread sentence, and with no
# direct control there is no way to tell which.

_NEAR_FEATURES = {
    # feature key      (table,                     name column,      geom)
    "seismic_line":    ("dataview.dv_seis_line",   "line_name",      "geog"),
    "seismic_survey":  ("dataview.dv_seis_set",    "seis_set_name",  "geog"),
    "field":           ("dataview.dv_field",       "field_name",     "geog"),
    "lease":           ("dataview.dv_land_tract",  "tract_name",     "geog"),
    "pipeline":        ("dataview.dv_pipeline",    "pipeline_name",  "geog"),
}
_NEAR_MAX_M = 50_000        # 50 km: past this "near" stops meaning anything


def _near_feature_names(_engine, feature: str) -> list[str]:
    """The named features of this kind that actually HAVE geometry.

    Only geometry-bearing rows are offered — a name in the list that cannot be
    searched is a control that fails after you use it.
    """
    from sqlalchemy import text as _t
    spec = _NEAR_FEATURES.get(feature)
    if not spec:
        return []
    tbl, namecol, geom = spec
    try:
        with _engine.connect() as cx:
            rows = cx.execute(_t(
                f"SELECT DISTINCT {namecol} FROM {tbl} "
                f"WHERE {geom} IS NOT NULL AND {namecol} IS NOT NULL "
                f"ORDER BY {namecol}")).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _wells_near_feature(_engine, feature: str, name: str,
                        distance_m: float) -> list[str]:
    """UWIs within distance_m of a named feature. Server-side, parameterised.

    Returns a LIST OF UWIs rather than well rows, deliberately: the caller
    turns it into an ordinary `uwi in (...)` clause, so every existing
    surface — the per-clause diagnostics, the drill shadow, the results grid —
    keeps working unchanged. By the time it reaches them it is just a well
    list.

    STDistance on geography returns METRES regardless of how the data was
    projected on the way in, which is why this takes a distance in metres and
    not in whatever unit the source survey used.
    """
    from sqlalchemy import text as _t
    spec = _NEAR_FEATURES.get(feature)
    if not spec:
        return []
    tbl, namecol, geom = spec
    try:
        d = float(distance_m)
    except (TypeError, ValueError):
        return []
    if not (0 < d <= _NEAR_MAX_M):
        return []
    # The table and column come from _NEAR_FEATURES — a fixed dict, never from
    # the caller — so they can be interpolated. The NAME and the DISTANCE are
    # user (or model) input and are bound.
    sql = _t(f"""
        SELECT DISTINCT w.uwi
          FROM dataview.dv_well w
          JOIN {tbl} f ON f.{namecol} = :nm
         WHERE w.geog IS NOT NULL
           AND f.{geom} IS NOT NULL
           AND w.geog.STDistance(f.{geom}) <= :d""")
    try:
        with _engine.connect() as cx:
            return [r[0] for r in cx.execute(sql, {"nm": name, "d": d}).fetchall()]
    except Exception:
        return []


def _apply_ai_filter(wells: list[dict], filter_spec: dict) -> list[dict]:
    """Apply a filter spec returned by the model to the loaded wells.

    A clause that cannot be evaluated counts as NOT matching, never as a pass.
    Silently passing an unevaluable clause is how a broken filter looks like a
    working one.
    """
    filters = filter_spec.get("filters", [])
    if not filters:
        return wells

    result = []
    for w in wells:
        match = True
        for f in filters:
            field = f.get("field", "")
            op = f.get("op", "eq")
            val = f.get("value")
            wval = w.get(field)

            if op in ("gt", "gte", "lt", "lte"):
                got = _cmp_values(wval, val, op)
                match = bool(got)
            elif op == "eq":
                match = str(wval or "").strip().upper() == str(val).strip().upper()
            elif op == "ne":
                match = str(wval or "").strip().upper() != str(val).strip().upper()
            elif op == "contains":
                match = str(val).upper() in str(wval or "").upper()
            elif op == "in":
                match = str(wval or "").strip().upper() in [
                    str(v).strip().upper() for v in (val or [])]
            else:
                # Unknown operator: exclude rather than pass, so it shows up as
                # a 0-match clause instead of quietly doing nothing.
                match = False
            if not match:
                break
        if match:
            result.append(w)
    return result

def _lift_well_suppression():
    """A new attribute selection (State / County / Query) normally supersedes a
    drawn selection: a draw sets wells_suppressed=True so it owns the view, and
    clearing it here lets the broad well loader run again.

    Exception (Scenario 2): if a drawn box is still active, an attribute change
    should RE-FILTER that drawn selection in place — keep the spatial box, apply
    the new filter on top — rather than abandoning it. We flag the main body to
    re-drill the stored bbox and keep the box owning the view."""
    import streamlit as st
    if st.session_state.get("_active_drill_bbox"):
        st.session_state["_refilter_drawn_box"] = True
    else:
        st.session_state["wells_suppressed"] = False


def _engage_wells_on_query():
    """on_change for the Query controls. Same suppression handling as
    _lift_well_suppression, but ALSO turns the Wells layer on — running a query
    is an explicit "show me these wells" action. (State/County selection keeps
    using _lift_well_suppression, so picking an area only recenters + shows H3;
    the wells come on when you run the query.) One-shot: the user can still
    toggle Wells off afterward. The broad-scope guard turns it back off if no
    state is constrained."""
    import streamlit as st
    _lift_well_suppression()
    st.session_state["wells_layer_on"] = True


def _add_documents_layer(m, docs_df, show=True):
    """Plot wells we hold catalogued documents for. Marker colour/size scale
    with document count; the popup shows a summary (count + types). The full
    file list is intentionally NOT in the popup — it can run long — and lives
    on the click-through results page instead."""
    import folium
    if docs_df is None or docs_df.empty:
        return 0

    def _color(n):
        return ("#0e7c4a" if n >= 10 else "#2bb3a3" if n >= 4
                else "#e8a23d" if n >= 2 else "#7c8aa0")

    def _s(v, d=""):
        # NaN-safe: pandas turns SQL NULLs into NaN, which is truthy, so a
        # plain `x or default` would leak "nan" into popups.
        return d if v is None or (isinstance(v, float) and v != v) else v

    fg = folium.FeatureGroup(name="📄 Documented wells", show=show)
    for r in docs_df.itertuples():
        try:
            n = int(r.doc_count)
        except Exception:
            n = 0
        name = _s(getattr(r, "well_name", None)) or r.uwi
        popup_html = "<br>".join([
            f"<b>{name}</b>",
            f"<span style='color:#64748b'>UWI {r.uwi}</span>",
            f"<b>{n}</b> document(s) — {_s(getattr(r, 'doc_types', ''))}",
            "<span style='color:#94a3b8;font-size:.85em'>Open Results to view "
            "the files</span>",
        ])
        folium.CircleMarker(
            location=(r.lat, r.lon),
            radius=min(14, 4 + (n ** 0.5) * 1.5),
            color=_color(n), weight=1, fill=True,
            fill_color=_color(n), fill_opacity=0.85,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"{name} · {n} doc(s)",
        ).add_to(fg)
    fg.add_to(m)
    return len(docs_df)


def _render_results_documents(engine, uwis):
    """Scannable list of ALL documents for the tray wells; a button jumps to the
    full Documents page to view them."""
    if not uwis:
        st.info("No wells in the tray.")
        return
    try:
        from dataview.file_catalog import page_selected_documents as _psd
        docs = _psd._docs_for_wells(engine, list(uwis))
    except Exception as e:
        st.error(f"Could not resolve documents: {e}")
        return
    if docs is None or docs.empty:
        st.info("No catalogued documents for these wells.")
        return
    _disp = pd.DataFrame({
        "File": [r.get("file_name") for _, r in docs.iterrows()],
        "Well": [(r.get("well_name") or r.get("uwi") or "") for _, r in docs.iterrows()],
        "Type": [r.get("doc_type") for _, r in docs.iterrows()],
        "Ext":  [str(r.get("file_ext") or "").lower() for _, r in docs.iterrows()],
    })
    st.caption(f"{len(_disp):,} document(s) across {len(uwis):,} well(s) \u2014 scan below.")
    st.dataframe(_disp, hide_index=True, use_container_width=True,
                 height=min(400, 40 + 35 * max(1, len(_disp))))
    if st.button("\U0001F4C4 Open in Documents page \u2192", key="results_docs_open",
                 use_container_width=True, type="primary"):
        st.session_state["selected_entities"] = [
            {"type": "well", "id": _u, "name": _u} for _u in uwis]
        st.session_state["wm_docs_page"] = True
        st.session_state["_export_scroll_pending"] = True
        st.rerun()


def _render_seed_reference(engine):
    """Copy reference wells from the gold master into dv_well, a county at a time.

    THE MAP SHOWS TWO POPULATIONS THAT ARE NOT THE SAME SET. The Wells layer
    reads dataview.dv_well; the H3 density layer reads
    dataview_federation.v_well_density_r*, which aggregates v_well -- dv_well
    UNION the 3.9M-row reference master. So a hot hexagon can hold thousands
    of wells and no clickable spot. This is the control that closes the gap
    for an area you care about.

    COUNTY AT A TIME, ON PURPOSE. The whole master is 3.9M rows and the map's
    own draw cap is 30,000; loading "everything" would exceed it and show a
    truncated subset, which reads as data. A county is a unit an operator
    means, and the count is on screen before anything is written.

    NOT A ONE-BUTTON LOAD. Counting is separate from writing and the write
    says what it dropped -- seeding a parent is a decision (Perry's law), and
    this is 8,960 of them in one click.
    """
    from dataview.import_data import seed_from_master as _sfm

    with st.expander("⬇ Load reference wells into the database", expanded=False):
        st.caption(
            "Copies wells from the **gold master** into `dv_well`, so the "
            "🔶 density hexagons and the 📍 Wells layer describe the same "
            "wells. Insert-only — a well another load already owns is left "
            "alone.")
        _st_code = (st.session_state.get("wm_sc_state") or "").strip()
        if not _st_code or _st_code.startswith("—"):
            st.info("Pick a **state** in *Constrain to* first — this loads a "
                    "county at a time, and the county list comes from it.")
            return
        try:
            # A CONNECTION, not the Engine. SQLAlchemy 2.0 removed
            # Engine.execute, so passing the engine here raised "'Engine'
            # object has no attribute 'execute'" -- and every other function
            # in seed_from_master takes a connection too.
            _cts = _seed_counties_cached(_engine_for_seed(engine), _st_code)
        except Exception as _ce:
            st.warning("Could not read the master: %s" % str(_ce)[:160])
            import traceback as _tb
            _say("[map] seed county list failed:\n%s" % _tb.format_exc())
            return
        if not _cts:
            st.info("The reference master holds no located wells for %s."
                    % _st_code)
            return

        # Default to the county the map is already constrained to, so the
        # control agrees with what is on screen rather than offering a
        # choice the operator has already made.
        _map_co = (st.session_state.get("wm_sc_county") or "").strip()
        _labels = ["%s  (%s wells)" % (c, format(n, ",")) for c, n in _cts]
        _idx = 0
        for _i, (_c, _n) in enumerate(_cts):
            if _map_co and _c.upper().startswith(_map_co.upper().replace(" COUNTY", "").strip()):
                _idx = _i
                break
        _pick = st.selectbox("County", _labels, index=_idx, key="wm_seed_county")
        _county = _cts[_labels.index(_pick)][0]

        # ── the drawn box narrows it further ───────────────────────────
        # A county is the unit the master is organised by, but it is not
        # always the unit you want: Campbell is 60,398 wells. If a box or
        # circle is on the map, offer it -- the same shape the well drill
        # already uses, so what gets loaded is what is on screen.
        _bb = st.session_state.get("_active_drill_bbox")
        _use_bb = False
        if _bb:
            _use_bb = st.checkbox(
                "Only inside the drawn box  (%.3f–%.3f lat, %.3f–%.3f lon)"
                % (_bb[0], _bb[1], _bb[2], _bb[3]),
                key="wm_seed_use_bbox",
                help="Loads only the reference wells inside the shape you "
                     "drew, instead of the whole county.")
        else:
            st.caption("Draw a box or circle on the map to load a smaller "
                       "area than a whole county.")
        # THE ORDER IS (min_lat, max_lat, min_lon, max_lon) at both ends.
        # _active_drill_bbox stores it that way and scope_where takes it that
        # way; swapped, it would return nothing rather than raise.
        _scope = dict(state=_st_code, county=None if _use_bb else _county,
                      bbox=_bb if _use_bb else None)
        _sig = repr(_scope)

        _c1, _c2 = st.columns([1, 1])
        if _c1.button("🔢 Count", key="wm_seed_count_btn",
                      use_container_width=True):
            try:
                with _engine_for_seed(engine).connect() as _cx:
                    _tot, _new = _sfm.scope_count(_cx, **_scope)
                st.session_state["_seed_counts"] = (_sig, _tot, _new)
            except Exception as _e:
                st.error("Count failed: %s" % str(_e)[:200])

        _counts = st.session_state.get("_seed_counts")
        # Keyed on the WHOLE scope, so changing the county -- or ticking the
        # box -- invalidates the count instead of leaving last scope's number
        # sitting above this scope's Load button.
        if not _counts or _counts[0] != _sig:
            st.caption("Press **Count** to see how many wells this would add.")
            return
        _, _tot, _new = _counts
        _m1, _m2 = st.columns(2)
        _m1.metric("In the master", format(_tot, ","))
        _m2.metric("Not yet in dv_well", format(_new, ","),
                   help="What this would insert. The rest are already here "
                        "and are left untouched.")
        _what = "the drawn box" if _use_bb else _county
        if not _new:
            st.success("Every located well in %s is already in the database."
                       % _what)
            return
        # ONE LOAD IS CAPPED AT THE DRAW CAP. Loading more than the map can
        # draw puts rows in the database that the Wells layer then silently
        # truncates -- data you cannot see and did not ask for. Ordered by
        # uwi and insert-only, so pressing Load again takes the NEXT block
        # rather than the same one: the county pages in, visibly, a screenful
        # at a time.
        _batch = min(_new, _WELLS_LOAD_CAP)
        if _new > _WELLS_LOAD_CAP:
            st.warning(
                "⚠ %s wells is more than the map's %s draw cap, so this loads "
                "the first **%s** (ordered by UWI). Press Load again for the "
                "next block, or **draw a box and tick the option above** to "
                "take a smaller area instead."
                % (format(_new, ","), format(_WELLS_LOAD_CAP, ","),
                   format(_batch, ",")))

        if _c2.button("⬇ Load %s wells" % format(_batch, ","),
                      key="wm_seed_apply_btn", type="primary",
                      use_container_width=True):
            try:
                _eng = _engine_for_seed(engine)
                with st.spinner("Reading the master…"):
                    with _eng.connect() as _cx:
                        # REFUSE BEFORE READING 9,000 ROWS, not after. An
                        # unregistered code fails on the FK at insert time,
                        # by which point the work is done and the message is
                        # a constraint violation instead of a sentence.
                        _ok, _reg = _sfm.validate_source(_cx, _sfm.SEED_SOURCE)
                        if not _ok:
                            st.error(
                                "`%s` is not registered in `dv_r_source`, so "
                                "these wells cannot be stamped with it. "
                                "Register it in the Reference Tables app "
                                "first — a loader never seeds a domain value. "
                                "Registered: %s"
                                % (_sfm.SEED_SOURCE, ", ".join(_reg)))
                            return
                        _rows = _sfm.scope_rows(_cx, limit=_WELLS_LOAD_CAP,
                                                **_scope)
                        _rep = _sfm.sanitise_fk(_cx, _rows)
                with st.spinner("Inserting %s wells…" % format(len(_rows), ",")):
                    # SOURCE STAMPED. Seeded with source NULL, these wells were
                    # invisible to every query filter keyed on source -- present,
                    # correctly keyed, in scope, and matching nothing.
                    _n, _present = _sfm.seed(_eng, _rows,
                                             source=_sfm.SEED_SOURCE)
                st.success(
                    "Inserted **%s** wells from %s, stamped `source = %s`. "
                    "%s were already here and were left alone."
                    % (format(_n, ","), _what, _sfm.SEED_SOURCE,
                       format(_present, ",")))
                # SAY WHAT WAS DROPPED. A code the reference table does not
                # hold is set to NULL rather than failing the row -- silently
                # blanking a column the operator can see in the source is the
                # same dishonesty as inventing one.
                if _rep:
                    st.caption("Unregistered coded values were set to NULL "
                               "rather than rejected:")
                    for _col, _d in _rep.items():
                        st.caption("• `%s` — %d row(s): %s"
                                   % (_col, _d["nulled"],
                                      ", ".join("%s×%d" % (_v, _c)
                                                for _v, _c in
                                                list(_d["values"].items())[:8])))
                st.caption("H3 cells came from the master alongside the "
                           "coordinates, so they agree with the point and "
                           "`h3_refresh` is not needed for these rows.")
                st.session_state.pop("_seed_counts", None)
                # EVERY well cache, because 27 of this page's 43 cached
                # functions read dv_well and a stale one would report 1,373
                # wells after inserting 8,960. The county list survives it --
                # see _seed_counties_cached -- because it comes from the
                # master, which this insert did not touch, and re-reading it
                # cost 4.2s on the render right after a load.
                #
                # The NEXT render is cold and will be slow; that is the price
                # of not showing a stale count, and it is paid once.
                st.cache_data.clear()
                st.info("Caches cleared — the next map render rebuilds from "
                        "the database and will take a few seconds.")
            except Exception as _e:
                st.error("Load failed: %s" % str(_e)[:300])
                import traceback as _tb
                _say("[map] seed reference wells failed:\n%s"
                      % _tb.format_exc())


def _seed_counties_cached(_engine, state):
    """[(county, n)] for a state, held in session_state.

    CACHED BECAUSE AN EXPANDER'S BODY ALWAYS RUNS. Streamlit executes what is
    inside st.expander whether or not it is open, so an uncached call here is
    a GROUP BY over 3.9M master rows on EVERY render of the map -- 4.2s
    measured, on a page that renders about fifty times a session, paid by
    everyone who never opens the panel.

    SESSION STATE, NOT @st.cache_data, AND THAT IS THE POINT. Seeding wells
    ends with st.cache_data.clear(), which is the honest thing to do -- 27 of
    this page's 43 cached functions read dv_well, and a stale one would report
    1,373 wells after inserting 8,960, which is the "wrong is worse than
    missing" case. But this list comes from the MASTER, which inserting into
    dv_well cannot change, and it is the single most expensive entry in the
    cache. Clearing it made the render after a load pay 4.2s for nothing.
    Kept out of that blast radius instead of narrowing the clear, because a
    list of "caches a well insert invalidates" is one more list that has to
    agree with every future @st.cache_data on this page.
    """
    from dataview.import_data import seed_from_master as _sfm
    _key = "_seed_counties_%s" % state
    _hit = st.session_state.get(_key)
    if _hit is not None:
        return _hit
    with _engine.connect() as _cx:
        _out = _sfm.counties(_cx, state)
    st.session_state[_key] = _out
    return _out


def _engine_for_seed(engine):
    """The engine the seeder should use.

    seed_from_master reaches WELL_REF by three-part name, so it needs a
    connection to the DataView database, which is what the map already holds.
    A separate helper because the map's `engine` argument has been a
    connection in one code path before now.
    """
    return getattr(engine, "engine", engine)


@st.fragment
def _render_saved_places(engine):
    """Go to / save / rename / re-point / delete a saved place.

    A FRAGMENT BECAUSE NONE OF THIS DRAWS THE MAP. Streamlit reruns the whole
    script on any widget change, and this page costs ~13 s to build, so
    picking a name in the Go-to box used to pay a full map rebuild for a
    control that does not move the camera. A fragment reruns in isolation,
    and the map is drawn above this, so it is left untouched.

    Only "Go" moves the camera, and only it calls st.rerun(scope="app").
    Every other action here -- save, rename, re-point, delete, cancel --
    changes this block and nothing else, so a fragment-scoped rerun is both
    correct and free.
    """
    # SAVED PLACES sit beside Reset view because they are the same kind of
    # control: both move the CAMERA and neither touches the wells. One
    # click to a known field beats hunting for a county on a world map,
    # which is how every demo currently opens.
    _pl1, _pl2, _pl5, _pl3, _pl4 = st.columns(
        [2.2, 0.7, 0.7, 0.7, 1.1])
    _places = _saved_places(engine)
    _pick = _pl1.selectbox(
        "Go to", ["— pick a place —"] + sorted(_places),
        key="wm_place_pick", label_visibility="collapsed")
    if _pl2.button("📍 Go", key="wm_place_go", use_container_width=True,
                   disabled=_pick.startswith("—")):
        _go_to_place(_places[_pick])
        # THE ONE ACTION HERE THAT MOVES THE CAMERA. _go_to_place sets
        # _drawn_bounds and _reset_saved_view, and nothing consumes either
        # until the map renders -- so this is the only control in the
        # fragment that has to cost a full rebuild.
        st.rerun(scope="app")
    # Editing and deleting are offered only for entries that CAN be
    # changed. A built-in or a region would come straight back on the next
    # run, and a control that appears to work and does not is worse than
    # one that is absent.
    _own = _pick in ((_load_user_prefs().get("places") or {}))
    if _pl5.button("\u270e", key="wm_place_edit_btn", use_container_width=True,
                   disabled=not _own,
                   help=("Rename this place, or point it at the box you "
                         "have drawn" if _own else
                         "Built-in places and petroleum regions cannot be "
                         "edited \u2014 they come from code and from your "
                         "data")):
        # A REQUEST FLAG CONSUMED BEFORE THE WIDGETS DRAW. The rename box
        # is pre-filled with the current name, so it has to exist only
        # once a place is chosen -- setting its value after it exists is
        # scar #6 and raises on a later run, on whatever page draws next.
        st.session_state["_place_edit"] = _pick
        st.session_state["wm_place_ren_ver"] = (
            st.session_state.get("wm_place_ren_ver", 0) + 1)
        st.rerun()
    if _pl3.button("🗑", key="wm_place_del", use_container_width=True,
                   disabled=not _own,
                   help=("Remove this saved place" if _own else
                         "Built-in places and petroleum regions cannot be "
                         "removed — they come from code and from your data")):
        if _delete_place(_pick):
            st.rerun()
    # NOT "the current view" -- Python is never told where you panned or
    # zoomed to, and three designs that tried to follow the viewport all
    # failed. What gets saved is the extent the app ITSELF set: the box
    # you drew, or the bbox a drill produced. The old comment here claimed
    # otherwise and the code never matched it.
    #
    # AND IT IS A TEXT BOX AMONG BUTTONS. With its label collapsed to line
    # up with Go and the bin, a grey placeholder in a bordered box reads as
    # a DISABLED BUTTON -- which is exactly how it was reported. Worse, the
    # "draw a box first" hint only appeared AFTER you typed a name, so the
    # thing that looked dead stayed dead until you argued with it. Now the
    # state is honest before you touch it: genuinely disabled when there is
    # nothing to save, and the placeholder says which.
    # _clip_box FIRST, AND IT IS WHY THIS WORKS AT ALL. A drawn box sets
    # _drawn_bounds with oneshot=True, and the very next render fits the
    # camera and POPS it -- so testing _drawn_bounds asked whether a box had
    # been drawn using a value the camera had already eaten. Reported as "I
    # redrew the box but save a place did not light up", and it never could.
    #
    # ORDER MATTERS BEYOND THAT. _drawn_bounds means "whatever last moved the
    # camera", which after an area change is the whole continental US -- so
    # it is the last resort here, not the first. Saving that under a name
    # would store the country as a place and look like a working save.
    _save_src = (st.session_state.get("_clip_box")
                 or st.session_state.get("_active_drill_bbox")
                 or st.session_state.get("_drawn_bounds"))
    _can_save = bool(_save_src)
    _pv = st.session_state.get("wm_place_ver", 0)
    # KEY IS VERSIONED, NOT REASSIGNED. Clearing the box after a save by
    # writing st.session_state["wm_place_new"] = "" is illegal -- Streamlit
    # refuses to let a widget own key be set once the widget exists, and it
    # raises on the NEXT run, on whatever page happens to draw first. That
    # is scar #6 in this codebase. Bumping a counter gives a fresh widget,
    # which starts empty by itself.
    _newname = _pl4.text_input(
        "Save current view as",
        key="wm_place_new_%d" % _pv,
        placeholder=("name this area…" if _can_save
                     else "draw a box to save"),
        disabled=not _can_save,
        help=("Names the extent you drew or drilled and adds it to Go to."
              if _can_save else
              "Nothing to save yet. The map cannot tell Python where you "
              "panned or zoomed to — draw a box with the rectangle tool, "
              "or run a search, and THAT extent is what a name saves."),
        label_visibility="collapsed")
    if _newname.strip() and _can_save:
        # NORMALISE BEFORE STORING. The fallback is a different shape
        # from the first choice -- see _norm_bounds -- and storing it raw
        # is what put four bare numbers in the file.
        # THE SAME SOURCE THE BUTTON WAS ENABLED FROM. Two different
        # expressions for "the extent to save" is how one of them ends
        # up storing the continental US.
        _b = _norm_bounds(_save_src)
        if _b is None:
            st.session_state["wm_place_err"] = _newname.strip()
            st.rerun()
        # SAVE THE SHAPES WITH THE EXTENT, when there are any. A box drawn
        # round part of a field is usually WHY that extent is worth keeping,
        # and returning to the extent without it loses the annotation.
        #
        # Bare list when there is nothing to add, so a place saved with no
        # drawings is byte-identical to what this wrote before and the file
        # does not gain a key it has no use for.
        _shapes = st.session_state.get("_last_drawings") or []
        _view = _capture_map_view()
        _rec = {"bounds": _b}
        if _shapes:
            _rec["shapes"] = _shapes
        if _view:
            _rec["view"] = _view
        _p = _load_user_prefs()
        # An AREA with nothing on it stays a bare list -- byte-identical to
        # what this always wrote, and nothing to migrate. A place that had
        # something on the map keeps it.
        _p.setdefault("places", {})[_newname.strip()] = (
            _rec if len(_rec) > 1 else _b)
        _save_user_prefs(_p)
        st.session_state["wm_place_ver"] = _pv + 1   # fresh, empty box
        st.session_state["wm_place_msg"] = _newname.strip()
        st.rerun()
    # AFTER the rerun, not before it. st.rerun() RAISES, so an st.success
    # written above is destroyed before it reaches the screen -- the same
    # scar that hid the colour-grid errors for a whole session.
    _pmsg = st.session_state.pop("wm_place_msg", None)
    if _pmsg:
        st.success("Saved “%s” — it is in the Go to list now." % _pmsg)
    # SAY SO RATHER THAN STORING SOMETHING UNUSABLE. A place that
    # cannot be read back is worse than one never saved: it sits in the
    # Go to list looking fine until it moves the camera nowhere.
    _perr = st.session_state.pop("wm_place_err", None)
    if _perr:
        st.error("Could not read an extent to save for that name. "
                 "Draw the box again and re-save.")

    # ── name the shape you drew ────────────────────────────────────
    # A DRAWN SHAPE ALREADY MEANS SOMETHING; this lets it be KEPT. The box
    # and circle drill wells and are then thrown away, so an outline traced
    # round a pool had nowhere to go. Saved here it becomes an ordinary named
    # boundary the 🟪 Boundaries chip draws like any other, rather than a
    # second kind of shape with machinery of its own.
    #
    # Offered for any drawn AREA, not only for the polygon tool: someone who
    # squares a pool off with the rectangle meant that too, and telling the
    # two apart from GeoJSON is guesswork -- a rectangle IS a Polygon once it
    # has been drawn.
    _bshapes = [f for f in (st.session_state.get("_last_drawings") or [])
                if str(((f or {}).get("geometry") or {}).get("type"))
                in ("Polygon", "MultiPolygon")]
    if _bshapes:
        with st.container(border=True):
            st.caption("✏️ **Keep the shape you drew** — name it and it joins "
                       "the Boundaries layer.")
            _bv = st.session_state.get("wm_bnd_ver", 0)
            _bc = st.columns([2.2, 1.4, 1])
            _bname = _bc[0].text_input(
                "Name", key="wm_bnd_name_%d" % _bv,
                placeholder="e.g. Pool A", label_visibility="collapsed")
            _btype = _bc[1].selectbox(
                "Type", BOUNDARY_TYPES, key="wm_bnd_type",
                label_visibility="collapsed")
            if _bc[2].button("Save", key="wm_bnd_save_btn",
                             use_container_width=True,
                             disabled=not _bname.strip(),
                             help="Store the LAST shape you drew as a named "
                                  "boundary."):
                _berr = _save_drawn_boundary(
                    engine, _bname, _btype, _bshapes[-1])
                st.session_state["wm_bnd_msg"] = (
                    _berr or "Saved “%s” — switch on 🟪 Boundaries "
                    "to see it." % _bname.strip())
                if not _berr:
                    # Versioned key, never assigned: clearing a text box by
                    # writing its own key is scar #6.
                    st.session_state["wm_bnd_ver"] = _bv + 1
                # scope="app": a new boundary changes what the MAP draws.
                st.rerun(scope="app")
    _bmsg = st.session_state.pop("wm_bnd_msg", None)
    if _bmsg:
        (st.success if _bmsg.startswith("Saved") else st.warning)(_bmsg)

    # ── edit the selected place ────────────────────────────────────
    # NOT AN EXPANDER: this block already sits inside one, and expanders
    # cannot nest (scar #4). It is shown only while a place is being
    # edited, so it costs nothing when it is not.
    _ed = st.session_state.get("_place_edit")
    if _ed and _ed not in (_load_user_prefs().get("places") or {}):
        # Deleted or renamed underneath us -- drop the request rather than
        # drawing a panel for something that is gone.
        st.session_state.pop("_place_edit", None)
        _ed = None
    if _ed:
        with st.container(border=True):
            _cur = _norm_bounds((_load_user_prefs().get("places")
                                 or {}).get(_ed))
            st.caption("Editing **%s**%s" % (_ed, (
                " \u2014 currently %.3f\u00b0 lat \u00d7 %.3f\u00b0 lon"
                % (_cur[1][0] - _cur[0][0], _cur[1][1] - _cur[0][1]))
                if _cur else ""))
            _rv = st.session_state.get("wm_place_ren_ver", 0)
            _newnm = st.text_input(
                "Rename to", value=_ed,
                key="wm_place_ren_%d" % _rv)
            _e1, _e2, _e3 = st.columns([1, 1.4, 1])
            if _e1.button("Rename", key="wm_place_ren_go",
                          use_container_width=True):
                _err = _rename_place(_ed, _newnm)
                st.session_state["wm_place_edit_msg"] = (
                    _err or "Renamed to \u201c%s\u201d."
                    % (_newnm or "").strip())
                if not _err:
                    # POINT THE PICKER AT THE NEW NAME, or the selectbox
                    # holds a value that no longer exists and the release
                    # in the next run silently drops the selection.
                    st.session_state["wm_place_pick"] = (
                        _newnm or "").strip()
                    st.session_state.pop("_place_edit", None)
                st.rerun()
            # RE-POINT USES THE SAME EXTENT THE SAVE BOX WOULD, so what
            # this writes is exactly what saving a new place would write.
            _rb = (st.session_state.get("_drawn_bounds")
                   or st.session_state.get("_active_drill_bbox"))
            if _e2.button("Use the box I drew", key="wm_place_repoint_btn",
                          use_container_width=True,
                          disabled=not _rb,
                          help=("Point %s at the extent currently drawn"
                                % _ed) if _rb else
                          "Draw a box first, then this will point the "
                          "place at it"):
                _err = _repoint_place(_ed, _rb)
                st.session_state["wm_place_edit_msg"] = (
                    _err or "\u201c%s\u201d now points at the box you "
                    "drew." % _ed)
                if not _err:
                    st.session_state.pop("_place_edit", None)
                st.rerun()
            if _e3.button("Cancel", key="wm_place_ren_cancel",
                          use_container_width=True):
                st.session_state.pop("_place_edit", None)
                st.rerun()
    # AFTER the rerun, for the same reason as every other message here.
    _emsg = st.session_state.pop("wm_place_edit_msg", None)
    if _emsg:
        if _emsg.startswith(("Renamed", "\u201c")):
            st.success(_emsg)
        else:
            st.warning(_emsg)


def run(engine=None):
    if not HAS_FOLIUM:
        st.error("pip install folium streamlit-folium")
        return
    if engine is None:
        st.info("Connect to the DataView database first.")
        return

    # ── widget defaults, SEEDED HERE rather than passed to the widget ──
    # "The widget with key X was created with a default value but also had
    # its value set via the Session State API."
    #
    # Both halves are ours. The sub-page persist loops self-assign every
    # settable key (st.session_state[k] = st.session_state[k]) so a control
    # survives a trip to Documents or Export -- Streamlit drops widget state
    # for anything a run does not render, and without that loop the map came
    # back with its filters reset. The self-assignment tags the key as
    # user-set; the widget then ALSO passes value=/index=, and Streamlit says
    # so, on every render, for the rest of the session.
    #
    # setdefault before instantiation is the supported way to say "this is
    # the default" once. It is not scar #6 -- that is assigning a widget's key
    # AFTER it has been drawn, which still must not happen.
    #
    # seis_basket_sel is deliberately NOT here. Its index= is recomputed each
    # render to follow the current pick, so it is not a static default at all;
    # seeding it would freeze the seismic basket on whatever was showing first.
    for _wk, _wv in (("wm_near_dist", 500), ("wm_show_legend", True),
                     ("wm_ppdm_symbols", False), ("wm_shp_fill", True),
                     ("wm_basemap", _BASEMAPS_SHOWN[0])):
        st.session_state.setdefault(_wk, _wv)

    # Clock starts here, after the two guards that return without drawing.
    _marks_begin("mode=%s wells=%s h3=%s freeze=%s hold=%s" % (
        st.session_state.get("map_mode"),
        st.session_state.get("wells_layer_on"),
        st.session_state.get("h3_layer_on"),
        st.session_state.get("wm_freeze_map"),
        st.session_state.get("wm_hold_map")))

    # ── the second-screen watcher, registered FIRST ────────────────────
    # "The fragment with id ... does not exist anymore - it might have been
    # removed during a preceding full-app rerun", arriving every two seconds
    # -- which is exactly this fragment's run_every.
    #
    # It used to be called at the very END of the map build. Between there
    # and here sit 20 early `return`s and 16 st.rerun() calls, and st.rerun()
    # RAISES: any one of them ends the render before the fragment is
    # re-registered, while the browser keeps its two-second timer pointed at
    # the id from the previous run. Opening Documents or Export returns
    # earlier still.
    #
    # A fragment has to be re-created on EVERY run that could follow it, so
    # it belongs before anything that can end the run -- not after everything
    # that can. It draws nothing and only stats a file, so the position costs
    # nothing. Its own guard still applies: with no recorded mtime it returns
    # immediately, which is the first render's case.
    _watch_seis_choice()

    # The top padding and the CSS-only-element collapse now live in
    # app_v4.py's stylesheet, which is injected before any page dispatch.
    # Setting them here meant they only took effect ~90 lines into this
    # function, so the page rendered at Streamlit's 96px until the script
    # got that far -- visible as a push-down on a slow first launch and
    # invisible on a warm one.

    # ── Active database resolution ─────────────────────────────────────
    # The map reads from ONE database, used by both the SQLAlchemy engine
    # AND the bcp.exe well fetch. Default to the database the app connected
    # to (DB_NAME()); the Database dropdown in the map controls can override
    # it. Resolving this here — before any data loads — keeps every path
    # (engine queries + bcp) pointed at the same database, so picking
    # DataView_Demo actually shows DataView_Demo wells.
    if "wm_conn_db" not in st.session_state:
        try:
            from sqlalchemy import text as _dbt
            with engine.connect() as _dbc:
                st.session_state["wm_conn_db"] = _dbc.execute(
                    _dbt("SELECT DB_NAME()")).scalar()
        except Exception:
            st.session_state["wm_conn_db"] = BCP_DATABASE
    st.session_state.setdefault(
        "wm_map_db", st.session_state.get("wm_conn_db", BCP_DATABASE))
    # If the dropdown selected a database other than the connected one,
    # re-point the engine at it (bcp follows via st.session_state["wm_map_db"]).
    _sel_db = st.session_state.get("wm_map_db")
    if _sel_db and _sel_db != st.session_state.get("wm_conn_db"):
        try:
            from dataview.core.dw_utils import make_engine
            engine = make_engine(_sel_db)
        except Exception:
            st.error(f"Cannot connect to {_sel_db}")

    # ── Documents page ──────────────────────────────────────────────────
    # Reached from the Object Tray's "📄 Documents" button. Shows the catalogued
    # documents for the selected wells (search + type filter + inline viewer),
    # in place of the map, with a Back button. Mirrors the export page.
    if st.session_state.get("wm_docs_page"):
        # Persist map state across the documents round-trip. Streamlit garbage-
        # collects widget-backed session keys when their widget isn't rendered,
        # and the docs page renders in place of the map (early return below), so
        # without this the map comes back reset. Re-assign every non-widget key
        # to mark it user-set and keep it alive — same approach as the export
        # page. Skip widget/button keys (they can't be set via session_state,
        # and re-tagging them corrupts state on their next render).
        _skip_prefixes = (
            "export_", "exp_", "sec_", "build_", "dl_", "osdu_", "db_",
            "sf_", "pdf_btn_", "pdf_dl_", "docs_", "seldoc_",
        )
        _skip_keys = {
            "wm_reset_page", "apply_uwi_filter", "wm_ai_run", "wm_ai_clear",
            "wells_clear_viewport", "wells_reset_view", "view_summary",
            "clear_tray",
            "close_summary_bottom", "open_docs_btn", "export_xlsx_btn",
        }
        for _pk in list(st.session_state.keys()):
            if (_pk.startswith(_skip_prefixes)
                    or _pk in _skip_keys
                    or _is_action_key(_pk)):
                continue
            try:
                st.session_state[_pk] = st.session_state[_pk]
            except Exception:
                pass

        if st.button("← Back to map", key="docs_back"):
            # RESET THE RADIO ON THE WAY OUT, or returning lands on a
            # Results panel whose radio still says "Documents", which
            # navigates straight back and traps the user in a loop with no
            # way out but the browser back button.
            #
            # Safe to set here specifically: this is the Documents page, it
            # returns before the Results panel is drawn, so the radio widget
            # has NOT been instantiated in this run. Setting a widget key
            # after its widget exists is what Streamlit refuses.
            st.session_state["results_mode:v1"] = "🛢 Wells"
            st.session_state["wm_docs_page"] = False
            st.rerun()
        try:
            from dataview.file_catalog import page_selected_documents
            page_selected_documents.run(engine)
        except Exception as _de:
            st.error(f"Documents page failed: {_de}")
        return

    # ── Export page ────────────────────────────────────────────────────
    # Reached from the Object Tray's Export button. A full-screen catalog of
    # export formats, each documented, fed the same scout-ticket data
    # sections (header, tops, survey, completions, production). Renders in
    # place of the map, with a Back button.
    if st.session_state.get("wm_export_page"):
        # Scroll back to the top on entry. The Export button is at the bottom
        # of the map page and Streamlit preserves scroll position across the
        # rerun, so without this the export page opens scrolled to the bottom.
        # Gated on a one-shot flag so it only fires on entry, not on every
        # in-page rerun (clicking a "?" popover, Build, etc.). components.html
        # runs in an iframe, so we scroll the PARENT document's scroll element.
        if st.session_state.pop("_export_scroll_pending", False):
            _scroll_main_to_top()
        # Persist map state across the export round-trip. Streamlit garbage-
        # collects widget-backed session keys when their widget isn't rendered
        # in a run, and the export page renders in place of the map (early
        # return below), so without this the map comes back reset to defaults.
        # Re-assigning every key marks them user-set and keeps them alive.
        #
        # BUT skip the export page's OWN widgets — they're being rendered this
        # run, so they aren't getting collected and don't need persisting, and
        # re-assigning them errors. Button keys especially (export_back, Build,
        # download, Select all/Clear) can't be set via st.session_state at all.
        # The try/except additionally skips any sidebar widget already
        # instantiated this run (e.g. sb_dialect).
        _skip_persist_prefixes = (
            "export_", "exp_", "sec_", "build_", "dl_",
            "osdu_", "db_", "sf_", "pdf_btn_", "pdf_dl_",
            "docs_",
        )
        # Buttons / download buttons can't be written through st.session_state
        # at all. Re-assigning one (even key = key) on a run where it isn't
        # rendered SUCCEEDS and silently re-tags the key as "user-set"; the
        # next time that widget instantiates, Streamlit throws
        # "cannot be set using st.session_state". So skip them explicitly —
        # button state never needs persisting (a button is only True on the
        # run it's clicked).
        _skip_persist_keys = {
            "wm_reset_page", "apply_uwi_filter", "wm_ai_run", "wm_ai_clear",
            "wells_clear_viewport", "wells_reset_view", "view_summary",
            "clear_tray",
            "close_summary_bottom",
        }
        for _persist_k in list(st.session_state.keys()):
            if (_persist_k.startswith(_skip_persist_prefixes)
                    or _persist_k in _skip_persist_keys
                    or _is_action_key(_persist_k)):
                # The third test skips dynamic widget keys built from a file
                # path (e.g. the universal viewer's "las_dl_C:\...\file.las"
                # download button). Those are widgets and re-assigning them
                # corrupts their state the next time they render.
                continue
            try:
                st.session_state[_persist_k] = st.session_state[_persist_k]
            except Exception:
                pass
        if st.button("← Back to map", key="export_back"):
            st.session_state["wm_export_page"] = False
            st.rerun()
        _tray_uwis = list(st.session_state.get("clicked_uwis", []))
        _shadow    = st.session_state.get("tray_well_data", {})
        _header_df = pd.DataFrame([_shadow[u] for u in _tray_uwis if u in _shadow])
        _area_lbl  = st.session_state.get("wm_area_sel", "")
        _source    = "gom" if "gom" in _area_lbl.lower() else "onshore"
        exporters.render_export_page(_header_df, engine, _tray_uwis, source=_source)
        return

    # Module-level first-run flag must be declared global up front so the
    # reset block below can read and write it.
    global _PROCESS_FIRST_RUN_DONE

    # ── Cold-start reset ───────────────────────────────────────────────
    # When the Streamlit process FIRST runs this module (process startup,
    # not just a re-render), force a clean slate for:
    #   - wm_area_sel (the Area dropdown) — defaults to "🌎 All schemas"
    #   - _wm_prev_area_id (the "what was last selected" tracker)
    #   - _drawn_bounds (any prior auto-zoom bounds that would re-fit map)
    # We detect first-run via a module-level global, NOT session_state,
    # because session_state can survive Streamlit restarts in some
    # configurations and that's exactly what we're guarding against.
    if not _PROCESS_FIRST_RUN_DONE:
        _PROCESS_FIRST_RUN_DONE = True
        # Default the Schema selector to All schemas (reads both dv_well
        # and dataview_gom.well). prev_area_id stays "none" so the auto-zoom
        # fires once to the all-schemas (US) center on first render.
        st.session_state["wm_area_sel"] = "🌎 All schemas"
        # Clear the area-change tracker so the auto-zoom logic doesn't
        # see "selection changed from None to placeholder" and fire
        st.session_state["_wm_prev_area_id"] = "none"
        # Drop any prior auto-zoom bounds so the map opens at its
        # default basemap centroid instead of yesterday's Permian view
        st.session_state.pop("_drawn_bounds", None)

    # Reset the sticky "preload all wells" flag on every page entry. The flag
    # was originally meant to remember "user already triggered the wells
    # preload in THIS render cycle, don't re-fire it" — but Streamlit's
    # session_state persists across cold starts of Streamlit and across
    # browser refreshes, so it became a permanent trap: once set in any
    # session, every subsequent cold start would re-fire the 30s _qry_wells
    # preload. The Area-selector partitioning makes that preload unnecessary
    # on first paint — the grid layers query their own tables directly. So:
    # reset to False on every entry. The flag will be re-set later in the
    # same render cycle if a Query type that needs wells gets selected.
    if "_wm_page_entered" not in st.session_state:
        st.session_state["_wells_already_loaded"] = False
        # SEEDED TO WHAT THE TOGGLES WILL DERIVE, not to "none". Seeding
        # "none" guaranteed a mismatch further down, and that derivation
        # answers a mismatch with st.rerun() -- so entering the map always
        # cost one whole extra render to reconcile two constants that simply
        # disagreed. Both now come from _default_map_mode.
        st.session_state["map_mode"] = _default_map_mode()
        st.session_state.pop("wm_adv_td_op", None)
        st.session_state.pop("wm_adv_spud_op", None)
        st.session_state.pop("wm_adv_comp_op", None)
        st.session_state.pop("_zoom_target_label", None)
        st.session_state.pop("ai_filter_spec", None)
        # ENTERING THE MAP MEANS STARTING AT THE TOP OF IT. See
        # _scroll_main_to_top: a page switch is a rerun and Streamlit keeps
        # the old scroll position, so arriving from a scrolled page opens
        # the map part-way down. One-shot, consumed on the next line.
        st.session_state["_wm_scroll_top_pending"] = True
        st.session_state["_wm_page_entered"] = True

    # ── CONSUME THE CLIP REQUEST BEFORE ITS WIDGET DRAWS ──────────
    # Drawing a box asks for the constraint; it cannot set wm_clip_to_box
    # itself because the checkbox has already been built by then, and
    # assigning a widget its own key after it exists raises on a later run,
    # on whatever page draws next. Consumed here, at the top, the assignment
    # happens before the widget is created and is therefore legal.
    if st.session_state.pop("_clip_request", False):
        st.session_state["wm_clip_to_box"] = True
    if st.session_state.pop("_clip_off_request", False):
        st.session_state["wm_clip_to_box"] = False


    # ── Phased progress indicator at the top of the page ───────────────
    # A single shared progress bar + message that any slow operation can
    # drive through phases (query → process → render). Hidden when idle.
    # Operations call _phase(pct, "Status text") to update; _phase(100, "")
    # clears both widgets. The widgets are placed at the top so the user
    # always sees them during long loads — they don't have to look down
    # at the map area to know something is happening.


    def _phase(pct: int, text: str = ""):
        """Record a render phase. TIMING ONLY -- it draws nothing.

        It used to own a progress bar and a status line above the map. Both
        are gone: they rendered where they were CREATED, so the one thing
        reporting on a long render sat at the top of the page away from the
        work, and they cost gap slots in the flex column on every render --
        including the ones fast enough that nobody wanted a progress bar.

        NOTHING DIAGNOSTIC IS LOST, which is the only reason this is safe to
        delete. _mark() below is what produces the per-phase timings in
        dev.out.log, and that is where every performance answer today came
        from -- the 644s gap, the 87s density query, the 6,820 round trips.
        The UI never told us any of that. Streamlit's own Running indicator
        still shows that a slow render is in progress.

        LABELLED "-> x", because a mark reports the time BEFORE the thing it
        names. The first log read "2.070s  Rendering map in browser", which
        anyone would take for st_folium's cost; it was the two seconds of
        building spent getting there, and st_folium was the 0.5s on the NEXT
        line. A timing log that invites the wrong conclusion is worse than
        none.
        """
        _mark("-> " + (text or ("phase %d" % pct)))

    # ── ⛔ Abort: a brake on the heavy layers, not a reset ─────────
    # WHY A BUTTON CAN WORK HERE AT ALL. Streamlit checks for a pending
    # RERUN/STOP request every time the script enqueues a ForwardMsg, so
    # every st.* call is an implicit yield point -- see
    # ScriptRunner._maybe_handle_execution_control_request. Clicking a widget
    # WHILE a render is grinding therefore raises RerunException at the next
    # st.* call and tears the slow render down. A custom button is not a
    # placebo here; it uses the very mechanism the toolbar's Stop uses.
    #
    # WHY NOT JUST USE THAT Stop. Stop interrupts and changes nothing, so the
    # next click rebuilds the identical slow map. This draws FIRST on every
    # render, so the browser has it while everything below is still being
    # built, and it sets a hold that makes the rerun cheap.
    #
    # WHY A HOLD AND NOT _clear_wells_state(). Escaping a slow render must
    # not cost the operator the selection they were waiting on -- a hexagon
    # set can take a dozen clicks to build. The hold suppresses the two
    # expensive layers AND the wells query; every selection, the tray and the
    # camera survive it, and ▶ Resume puts them back. Destroying state to
    # escape a render is the wrong-is-worse-than-missing trade in miniature.
    #
    # WHAT IT CANNOT DO. It lands at the next st.* call, so a single blocking
    # call is not interruptible: st_folium handing 28K markers to Leaflet, or
    # one long query, runs to completion and the abort takes hold on the run
    # after. Measured: the 593s render was browser-side Leaflet, not Python.
    # The draw cap (_WELLS_DRAW_CAP) is what bounds that; this bounds the
    # repeat.
    # ── ⛔ Render hold: the flag, read all the way down this function ─
    # The BUTTON that sets it lives in ⚙ Page controls beside Reset page --
    # tucked away for the same reason Reset is: a control that blanks the map
    # must not be one you can hit by accident. The BANNER stays out here at
    # top level, though, and carries its own ▶ Resume. Hiding the
    # explanation inside a collapsed expander is what would make a held map
    # read as a broken one.
    _render_held = bool(st.session_state.get("_wm_render_hold"))

    # ── Reset page button (escape hatch) ───────────────────────────────
    # Tucked into a thin expander at the top so it's findable but won't
    # be clicked accidentally. Click it when the page is in a weird state
    # and you want a fresh start without restarting Streamlit. Clears
    # @st.cache_data results AND all session state, then reruns. The
    # cold-start reset block above will then re-initialize defaults
    # (area selector back to placeholder, etc.) on the new run.
    with st.expander("⚙ Page controls", expanded=False):
        # ── where the LAST render's time went ──────────────────────────
        # The PREVIOUS render, not this one: this expander draws near the
        # top of the script, so the current render has barely started and
        # its own numbers do not exist yet. Showing them would report a
        # near-zero total for a render that took twenty seconds.
        if _MAP_TIMERS and _tstate().get("_wm_marks_prev"):
            _prev = _tstate().get("_wm_marks_prev") or []
            _pcalls = _tstate().get("_wm_calls_prev") or []
            _tot = max((m.get("cumulative", 0) for m in _prev), default=0)
            st.caption("⏱ Previous render: **%.2fs**. Same numbers print to "
                       "the terminal as they happen — a render that ends in "
                       "st.rerun() never reaches this box."
                       % _tot)
            _tc1, _tc2 = st.columns(2)
            _tc1.dataframe(
                pd.DataFrame(sorted(_prev, key=lambda m: -m["seconds"])[:12]),
                hide_index=True, use_container_width=True,
                column_config={"seconds": st.column_config.NumberColumn(
                    "sec", format="%.2f")})
            if _pcalls:
                _agg = {}
                for _c in _pcalls:
                    _a = _agg.setdefault(_c["call"], {"call": _c["call"],
                                                      "calls": 0, "seconds": 0.0})
                    _a["calls"] += 1
                    _a["seconds"] += _c["seconds"]
                _tc2.dataframe(
                    pd.DataFrame(sorted(_agg.values(),
                                        key=lambda a: -a["seconds"])[:12]),
                    hide_index=True, use_container_width=True,
                    column_config={"seconds": st.column_config.NumberColumn(
                        "sec", format="%.2f")})
            else:
                _tc2.caption("No single query or layer crossed %.2fs."
                             % _MAP_TIMER_FLOOR)

        _rc1, _rcA, _rc2 = st.columns([1, 1, 3])
        with _rcA:
            # KEYS END "_btn" so _is_action_key() excludes them from the
            # sub-page persist loops. A button's value cannot be set, and
            # self-assigning one raises on a LATER run, on whatever page
            # draws next -- the crash lands nowhere near its cause.
            if _render_held:
                if st.button("▶ Resume", key="wm_render_resume_btn",
                             use_container_width=True,
                             help="Draw the well and hexagon layers again."):
                    st.session_state.pop("_wm_render_hold", None)
                    st.rerun()
            elif st.button("⛔ Abort", key="wm_render_abort_btn",
                           use_container_width=True,
                           help=("Stop a render that is taking too long. It "
                                 "lands at the next drawing step, then holds "
                                 "the well and hexagon layers off so the page "
                                 "comes back. Nothing is lost -- Resume "
                                 "draws them again.")):
                # No st.rerun(): this expander draws near the TOP of the
                # render, so continuing costs one pass instead of two and
                # everything below reads _render_held on this very run.
                st.session_state["_wm_render_hold"] = True
                _render_held = True
                _say("abort: holding the wells and hexagon layers")
        with _rc1:
            if st.button("🔄 Reset page",
                         key="wm_reset_page",
                         use_container_width=True,
                         help=("Clear caches AND session state, then "
                               "reload. Use when the page is in a weird "
                               "state and you want a fresh start without "
                               "restarting Streamlit.")):
                # Clear @st.cache_data — drops cached query results
                # (BCP wells/grid/H3 outputs). Next call re-queries.
                try:
                    st.cache_data.clear()
                except Exception:
                    pass
                # Drop only the keys THIS PAGE owns. Deleting every key
                # took the app shell's database connection with it and
                # dumped the user back at the login screen — a page reset
                # should not log you out. Iterate over a list copy because
                # deleting from a dict you're iterating raises RuntimeError.
                _PAGE_PREFIXES = (
                    "wm_", "_wm_", "wells_", "_wells_", "map_", "_map_",
                    "_drawn", "_active_", "_last_", "_zoom_", "_refilter_",
                    "_pending_", "viewport_", "tray_", "_sel_", "_summary_",
                    "_reset_saved_view", "selected_cells", "processed_drawings",
                    "clicked_uwis", "scout_uwi", "show_summary", "h3_", "_h3_",
                    "_filter_", "_last_filter_sig", "_broad_", "_auto_",
                    # Found by listing every st.session_state key this file
                    # touches and subtracting the prefixes above — guessing at
                    # the list left the AI filter surviving a page reset, which
                    # is exactly the state someone hits reset to escape.
                    "ai_filter", "gom_sel_mode", "grid_visible",
                    "selected_entities", "selected_h3_cells",
                    "_export_scroll_pending", "_sc_",
                )
                for _k in list(st.session_state.keys()):
                    if not str(_k).startswith(_PAGE_PREFIXES):
                        continue
                    try:
                        del st.session_state[_k]
                    except KeyError:
                        pass
                # Force the cold-start init block to re-run by resetting
                # the module-level first-run flag. The outer `global`
                # declaration at the top of run() already covers writes
                # to this name from anywhere inside run(), so no extra
                # `global` statement is needed here.
                _PROCESS_FIRST_RUN_DONE = False
                st.rerun()
        with _rc2:
            st.caption(
                "Resets caches and session state. Doesn't touch the "
                "database. For a full restart, stop and re-run Streamlit "
                "from the terminal."
            )

    if _render_held:
        _hb1, _hb2 = st.columns([5, 1])
        _hb1.warning(
            "⛔ Render held — wells and hexagons are not being drawn. "
            "Your selection, tray and view are all kept.")
        if _hb2.button("▶ Resume", key="wm_render_resume2_btn",
                       use_container_width=True,
                       help="Draw the well and hexagon layers again."):
            st.session_state.pop("_wm_render_hold", None)
            st.rerun()

    # Lazy-load strategy: skip the expensive _qry_wells call on first load.
    # Grid mode is the default and doesn't need the full wells list — the
    # density polygons come from a separate aggregation query (sub-second).
    # We only pay the cost of pulling 50K+ wells when the user actually
    # needs them: switches to Wells mode, opens search dropdowns, or uses
    # the AI filter. Picking a Petroleum or State Region does NOT
    # auto-load wells anymore — it just navigates the map. The user
    # picks a Query (or switches to Wells mode) to load wells, at which
    # point the active region filter applies.
    #
    # Picking a Zoom-To target that carries a filter (protraction area,
    # field, basin, county) DOES trigger wells-load — the user expects
    # "I picked Mississippi Canyon, show me those wells." The Zoom-To
    # filter then cascades with Query/Status/Region in the mask chain
    # later.
    #
    # ── EVERY MAP MESSAGE RENDERS BELOW THE MAP ──────────────────
    # A plain buffer, not a placeholder, so it has no position of its own:
    # every caller above the map can reach it and flush() decides where the
    # text lands, which is under st_folium.
    #
    # NAMED _mapmsg, NOT _msg. run() already binds _msg -- the message from
    # _register_spatial_layer, inside a button branch that normally never
    # fires. Reusing the name meant the buffer was only defined when someone
    # registered a shapefile, and every other render raised "cannot access
    # local variable '_msg' where it is not associated with a value".
    # CONSUMED HERE, RUN AT THE BOTTOM. st.components.v1.html is an IFRAME,
    # not a script tag -- Streamlit gives it a real element container, so
    # emitting it at the top of the page put a box ABOVE the map on exactly
    # the render that scrolls, which is page entry. That is "it starts to
    # draw then gets pushed down", and only on the first launch, because the
    # flag is one-shot.
    #
    # The script does not care where it lives: it reaches the parent
    # document and scrolls it. Rendered after everything else, it cannot
    # move anything.
    _want_scroll_top = st.session_state.pop("_wm_scroll_top_pending", False)

    _mapmsg = _MsgBelowMap()

    _zoom_target_label = st.session_state.get("wm_zoom_target", "")
    _zoom_target_has_filter = (
        _zoom_target_label
        and not _zoom_target_label.startswith("— ")
    )
    # Advanced Filters (TD / spud / completion) were removed from the sidebar;
    # retained as a constant False so the wells-load trigger below still reads.
    _adv_filters_active = False

    # ── Cold-start gate: load NOTHING until a real area is selected ──────
    # active_area isn't computed until ~line 4691, but we need the selected
    # area's sources here to decide whether any data load should fire at all.
    # Resolve it inline from the selector's session_state value.
    #
    # The placeholder areas ("— Select schema —" and the "🌎 All schemas"
    # entry — wait, All schemas DOES have sources) have specific sources.
    # The rule: if the resolved sources list is EMPTY, the user hasn't picked
    # anything real yet, so we show only the basemap and skip wells, grid,
    # and H3 entirely. This makes cold start instant and quiet — no 514K
    # aggregation, no wells pull — until the user makes their first pick.
    _active_sources_early = []
    try:
        _sel_lbl_early = st.session_state.get("wm_area_sel")
        _area_early = next(
            (a for a in AREAS if a["label"] == _sel_lbl_early), None
        )
        if _area_early:
            _active_sources_early = _area_early.get("sources", [])
    except Exception:
        _active_sources_early = []
    _has_real_selection = bool(_active_sources_early)

    # Un-suppress the wells layer when the user changes the area selection.
    # "✗ Clear wells" sets wells_suppressed=True to hide the base clusters;
    # picking a (different) area is a deliberate "show me this" action, so
    # we lift the suppression. Tracked via _last_area_for_suppress.
    _cur_area_lbl = st.session_state.get("wm_area_sel")
    if st.session_state.get("_last_area_for_suppress") != _cur_area_lbl:
        st.session_state["wells_suppressed"] = False
        st.session_state["_last_area_for_suppress"] = _cur_area_lbl
        # A different schema invalidates any drawn box (its bbox may not be
        # valid for the new sources) — drop it so it can't re-filter.
        st.session_state.pop("_active_drill_bbox", None)
        st.session_state.pop("_refilter_drawn_box", None)

    # ── the SAME reasoning, for every other filter ───────────────────────────
    # Changing the Area lifted the suppression above; changing an operator, a
    # county, a depth range or the Query type did not. So after a "✗ Clear
    # wells" or a draw (both of which set wells_suppressed), adjusting any of
    # those left the flag set and the map came back EMPTY — the user changed a
    # filter and got no wells, with nothing on screen explaining why.
    #
    # Every one of these is a deliberate "show me this" action, so all of them
    # lift the suppression. Compared as a signature rather than one tracker per
    # widget: a new filter added to the tuple is then covered automatically,
    # and repr() copes with the list values multiselects return.
    #
    # DISPLAY toggles are deliberately absent — basemap, legend, the wm_db_*
    # overlay switches. Those change how the map looks, not which wells were
    # asked for, and un-suppressing on them would undo a Clear the moment
    # someone flicked a layer on.
    _FILTER_KEYS = (
        "wm_query_sel", "wm_q_area", "wm_q_op", "wm_q_source", "wm_q_wtype",
        "wm_q_uwi_text", "wm_q_td_lo", "wm_q_td_hi", "wm_q_spud_lo",
        "wm_q_spud_hi", "wm_q_comp_lo", "wm_q_comp_hi",
        "wm_sc_state", "wm_sc_county", "wm_sc_protraction",
    )
    _filter_sig = repr([st.session_state.get(_k) for _k in _FILTER_KEYS])
    if st.session_state.get("_last_filter_sig") != _filter_sig:
        # First render seeds the signature without acting — otherwise every
        # cold start would clear a suppression the user set deliberately.
        if "_last_filter_sig" in st.session_state:
            st.session_state["wells_suppressed"] = False
        st.session_state["_last_filter_sig"] = _filter_sig
# ── Resolve query filter for SQL push-down ─────────────────────
    _qry_where = ""
    _early_qsel = st.session_state.get("wm_query_sel")
    _QUERY_MAP = {"By UWI":"uwi", "By source":"source", "By operator":"operator",
                  "By well type":"well_type", "By area":"area",
                  "Has documents":"has_docs",
                  "Has formation tops":"has_tops", "Has production data":"has_prod",
                  "Has DST":"has_dst", "Has directional survey":"has_survey",
                  "Has core data":"has_core", "Has core photos":"has_core_photos",
                  "Has petro interpretation":"has_petro"}
    _early_qtype = _QUERY_MAP.get(_early_qsel)
    _early_qvalue = None
    if _early_qtype == "source":
        _early_qvalue = st.session_state.get("wm_q_source")
        if _early_qvalue:
            _qry_where = f"AND w.source = '{_early_qvalue.replace(chr(39), chr(39)+chr(39))}'"
    elif _early_qtype == "operator":
        _early_qvalue = st.session_state.get("wm_q_op")
        if _early_qvalue:
            _safe = _early_qvalue.replace("'", "''")
            # Mirror the SELECT expression exactly — _qry_wells displays
            # COALESCE(w.operator_name, ba.ba_name, 'Unknown'), so a two-arg
            # COALESCE here cannot match the wells shown as "Unknown" (both
            # sides NULL). The dropdown offers that value, so it has to work.
            _qry_where = (
                "AND COALESCE(w.operator_name, ba.ba_name, 'Unknown') = "
                f"'{_safe}'")
    elif _early_qtype == "well_type":
        _early_qvalue = st.session_state.get("wm_q_wtype")
        if _early_qvalue:
            _qry_where = f"AND w.well_type = '{_early_qvalue.replace(chr(39), chr(39)+chr(39))}'"
    elif _early_qtype == "has_docs":
        # wells that have catalogued documents (tagged by UWI14 in the file catalog)
        _qry_where = ("AND EXISTS (SELECT 1 FROM file_catalog.GLOBAL_FILE_CATALOG g "
                      "WHERE g.UWI14 = w.uwi AND ISNULL(g.FLAG_DELETE,'N') <> 'Y')")
    elif _early_qtype == "has_docs":
        _qry_where = ("AND EXISTS (SELECT 1 FROM file_catalog.GLOBAL_FILE_CATALOG g "
                      "WHERE g.UWI14 = w.uwi AND ISNULL(g.FLAG_DELETE,'N') <> 'Y')")
    elif _early_qtype == "has_tops":
        _qry_where = "AND EXISTS (SELECT 1 FROM dataview.dv_well_formation_top t WHERE t.uwi = w.uwi)"
    elif _early_qtype == "has_prod":
        _qry_where = "AND EXISTS (SELECT 1 FROM dataview.dv_prod_entity pe WHERE pe.uwi = w.uwi)"
    elif _early_qtype == "has_dst":
        _qry_where = "AND EXISTS (SELECT 1 FROM dataview.dv_well_dst d WHERE d.uwi = w.uwi)"
    elif _early_qtype == "has_survey":
        _qry_where = "AND EXISTS (SELECT 1 FROM dataview.dv_well_dir_srvy_hdr h WHERE h.uwi = w.uwi)"
    elif _early_qtype == "has_core":
        _qry_where = "AND EXISTS (SELECT 1 FROM dataview.dv_well_core c WHERE c.uwi = w.uwi)"
    elif _early_qtype == "has_core_photos":
        _qry_where = ("AND EXISTS (SELECT 1 FROM dataview.dv_well_core_photo cp "
                      "WHERE cp.uwi = w.uwi AND cp.active_ind = 'Y')")
    elif _early_qtype == "has_petro":
        _qry_where = "AND EXISTS (SELECT 1 FROM dataview.dv_well_petro_interp p WHERE p.uwi = w.uwi)"
    elif _early_qtype == "uwi":
        _uwi_raw = st.session_state.get("wm_q_uwi_text", "")
        if _uwi_raw.strip():
            # Parse UWI list: split on commas, newlines, semicolons, or spaces
            _uwi_list = [u.strip().replace("'", "''") for u in re.split(r'[,;\n\r\t]+', _uwi_raw) if u.strip()]
            if _uwi_list:
                _in_clause = ",".join(f"'{u}'" for u in _uwi_list)
                _qry_where = f"AND w.uwi IN ({_in_clause})"

    # ── Spatial constraint (standing "Constrain to" control) ────────────
    # Composes with Query: resolved up-front from wm_sc_* session keys.
    # Onshore / All-schemas use the US county boundary map (us_geo) and
    # filter by a lat/lon BBOX — valid against BOTH dv_well and
    # dataview_gom.well, so it composes everywhere (no cross-schema column
    # problem). GOM uses protraction (bottom_area_code), which only exists
    # on the GOM table, so that one stays scoped to the gom schema.
    _sc_area_early = next(
        (a for a in AREAS if a["label"] == st.session_state.get("wm_area_sel")),
        None)
    _sc_id_early = _sc_area_early.get("id") if _sc_area_early else None
    _spatial_where = ""
    _sc_st = st.session_state.get("wm_sc_state")
    _sc_pa = st.session_state.get("wm_sc_protraction")
    _sc_pa = _sc_pa if (_sc_pa and _sc_pa != "— all areas —") else None
    if _sc_st == _GULF_STATE:
        # Gulf of Mexico (offshore): constrain to one protraction area, or the
        # whole Gulf footprint if none is chosen. Uses the protraction GeoJSON
        # polygons (boem_geo); the bbox naturally excludes onshore wells, so
        # it's valid for GOM and All-schemas alike. Attribute fallback only on
        # the GOM-only table.
        _bb = None
        if _boem_geo is not None and HAS_BOEM_GEO:
            _bb = (_boem_geo.bbox(_sc_pa) if _sc_pa
                   else _boem_geo.overall_bbox())
        if _bb:
            _mnla, _mnlo, _mxla, _mxlo = _bb
            _spatial_where = (
                f" AND w.surface_latitude BETWEEN {_mnla} AND {_mxla}"
                f" AND w.surface_longitude BETWEEN {_mnlo} AND {_mxlo}")
        elif _sc_pa and _sc_id_early == "gom":
            _spatial_where = (" AND w.bottom_area_code = '"
                              + _sc_pa.replace(chr(39), chr(39)+chr(39)) + "'")
    elif (_sc_st and _sc_st != "— all states —"
            and _us_geo is not None and HAS_US_GEO):
        _sc_co = st.session_state.get("wm_sc_county")
        _sc_county = (_sc_co if (_sc_co and _sc_co != "— all counties —")
                      else None)
        _bb = _us_geo.bbox(_sc_st, _sc_county)
        if _bb:
            _mnla, _mnlo, _mxla, _mxlo = _bb
            _spatial_where = (
                f" AND w.surface_latitude BETWEEN {_mnla} AND {_mxla}"
                f" AND w.surface_longitude BETWEEN {_mnlo} AND {_mxlo}")
    # Attribute-only filter for geometry drills (rectangle / circle / hex
    # cell / remembered-box re-filter). A drawn shape supplies its OWN spatial
    # bounds, so the drill must NOT also be clipped to the selected
    # State/County bbox (_spatial_where) — doing so silently dropped wells
    # that were inside the drawn box but outside the county's bounding box.
    # The Query-driven base load below still uses the full _qry_where (with
    # _spatial_where) so a plain Query stays county-constrained.
    _qry_where_attr = _qry_where.strip()
    _qry_where = (_qry_where + _spatial_where).strip()

    st.session_state["_active_where_extra"] = _qry_where_attr

    # GOM-shaped attribute clause. The onshore _qry_where references columns
    # (operator_name, uwi, well_type) and dataview.dv_* child tables that don't
    # exist for GOM, so translate the predicates that map cleanly onto
    # dataview_gom.well (operator→company_name, well_type→type_code,
    # uwi→api_well_number) and skip the rest (source, has-* EXISTS). The
    # spatial clause is shared — GOM has surface_latitude/longitude.
    _qry_where_gom = ""
    if _early_qtype == "operator" and _early_qvalue:
        _qry_where_gom = ("AND w.company_name = '"
                          + _early_qvalue.replace("'", "''") + "'")
    elif _early_qtype == "well_type" and _early_qvalue:
        _qry_where_gom = ("AND w.type_code = '"
                          + _early_qvalue.replace("'", "''") + "'")
    elif _early_qtype == "uwi":
        _uwi_raw_g = st.session_state.get("wm_q_uwi_text", "")
        _uwi_list_g = [u.strip().replace("'", "''")
                       for u in re.split(r'[,;\n\r\t]+', _uwi_raw_g) if u.strip()]
        if _uwi_list_g:
            _qry_where_gom = ("AND w.api_well_number IN ("
                              + ",".join(f"'{u}'" for u in _uwi_list_g) + ")")
    # Attribute-only GOM filter for geometry drills — same rationale as the
    # onshore _qry_where_attr above (the drawn shape owns the spatial bounds).
    _qry_where_gom_attr = _qry_where_gom.strip()
    _qry_where_gom = (_qry_where_gom + _spatial_where).strip()
    st.session_state["_active_where_extra_gom"] = _qry_where_gom_attr

    # ── Scenario 2: re-filter a drawn box in place ───────────────────────
    # When an attribute filter (State / County / Query) changes while a drawn
    # box is active, re-run that stored bbox with the freshly-composed filters
    # so the new filter narrows the wells you drew — instead of abandoning the
    # box and reloading the whole area. Onshore uses the full query; GOM uses
    # the translated clause (operator/type/uwi + spatial).
    if st.session_state.pop("_refilter_drawn_box", False):
        _rbb = st.session_state.get("_active_drill_bbox")
        if _rbb:
            _r_mnla, _r_mxla, _r_mnlo, _r_mxlo = _rbb
            if "main" in _active_sources_early:
                try:
                    _rb_wells, _rb_total = _qry_wells_in_bbox(
                        engine, _r_mnla, _r_mxla, _r_mnlo, _r_mxlo,
                        limit=5000,
                        where_extra=st.session_state.get("_active_where_extra", ""),
                    )
                    if _rb_wells:
                        st.session_state["viewport_uwis"] = [
                            w["uwi"] for w in _rb_wells
                        ]
                        _rb_shadow = st.session_state.get("tray_well_data", {})
                        for w in _rb_wells:
                            _rb_shadow[w["uwi"]] = w
                        st.session_state["tray_well_data"] = _rb_shadow
                    else:
                        st.session_state["viewport_uwis"] = []
                except Exception as _rbe:
                    print(f"[refilter-box] main re-drill failed: {_rbe}")
            if "gom" in _active_sources_early:
                try:
                    _rb_gom, _rb_gom_total = _qry_gom_wells_in_bbox(
                        engine, _r_mnla, _r_mxla, _r_mnlo, _r_mxlo,
                        limit=5000,
                        where_extra=st.session_state.get("_active_where_extra_gom", ""),
                    )
                    st.session_state["viewport_gom_wells"] = _rb_gom or []
                except Exception as _rbge:
                    print(f"[refilter-box] gom re-drill failed: {_rbge}")
            # The box still owns the view: keep the broad loader suppressed
            # and the Wells layer on so only the re-filtered box shows.
            st.session_state["wells_suppressed"] = True
            st.session_state["wells_layer_on"] = True

    _uwi_filter_active = (_early_qtype == "uwi" and st.session_state.get("wm_q_uwi_text", "").strip() != "")

    # A has_* QUERY IS AS NARROW AS A UWI LOOKUP AND MUST BE TREATED LIKE ONE.
    # "Has core data" matches one well in this database. A UWI lookup bypasses
    # the scope and mode gates below because it is a deliberate, small request;
    # so is this, and without the same bypass the wells never load. What the
    # reader then sees is the DENSITY layer, which cannot honour the filter at
    # all -- v_well_density_r* is pre-aggregated to a count per hexagon, and an
    # EXISTS against a child table cannot be applied to a number computed
    # before the question was asked. The hexagons therefore answer a different
    # question, silently, and the filter looks broken when it is working.
    #
    # BOUNDED, BECAUSE A has_* QUERY IS NOT A UWI LOOKUP. A typed UWI list is
    # small by construction; "Has formation tops" matches 1,079 wells here and
    # would trigger the 30-second load at continental scope. So the count is
    # taken FIRST and the bypass only applies when the answer is small enough
    # to draw. Above that the existing scope rules stand and the caption says
    # why -- an unbounded bypass would have traded a filter that looked broken
    # for a map that hangs.
    _HAS_QUERIES = ("has_docs", "has_tops", "has_prod", "has_dst",
                    "has_survey", "has_core", "has_core_photos", "has_petro")
    _HAS_PLOT_CAP = 2500
    _has_hits = None
    if _early_qtype in _HAS_QUERIES and _qry_where_attr:
        try:
            _has_hits = _qry_wells_scope_count(engine, where_extra=_qry_where)
        except Exception:
            _has_hits = None
    _data_filter_active = (_has_hits is not None
                           and 0 < _has_hits <= _HAS_PLOT_CAP)

    # All-states (continental) scope guard: when an onshore/all schema is
    # active but NO state (or Gulf) is constrained, the individual-well list is
    # meaningless to load (millions of wells) — only H3 density is allowed.
    # Selecting any state OR Gulf of Mexico lifts the guard. An explicit UWI
    # lookup is exempt. If Wells is selected at broad scope, we bounce it to H3
    # (set map_mode and the radio key, before the radio renders) and flag it.
    # ── map_mode derived HERE, before anything reads it ────────────────
    # This used to be derived ~2,000 lines further down, and the derivation
    # answered a mismatch with st.rerun(). So flipping a layer toggle cost
    # TWO renders: one that got as far as the derivation and threw itself
    # away, and one that did the work. The timing log shows the pair --
    #   render start  mode=h3     wells=True  h3=True    (no marks)
    #   render start  mode=wells  wells=True  h3=False   6.186s
    # -- at ~2.8s a render, on every toggle.
    #
    # The toggles are authoritative and they are already current: Streamlit
    # applies widget state before the script runs, so wells_layer_on holds
    # the NEW value on the very render the click triggers. map_mode is the
    # only thing that lagged, and _need_wells below reads it.
    #
    # WHY THIS ONLY MATTERS FOR TOGGLING OFF: _need_wells asks for
    # `map_mode == "wells" OR wells_layer_on`, so switching Wells ON was
    # already satisfied by the toggle alone. Switching it OFF left a stale
    # map_mode == "wells" holding the OR true, wells loaded anyway, and the
    # rerun was what corrected it. Deriving first fixes the direction that
    # was actually broken.
    #
    # The late derivation stays: a drill handoff or a broad-scope bounce can
    # still change the toggles mid-render, and a rerun is legitimate then.
    _wl_now = st.session_state.get("wells_layer_on")
    _hl_now = st.session_state.get("h3_layer_on")
    if _wl_now is not None or _hl_now is not None:
        st.session_state["map_mode"] = (
            "wells" if _wl_now else ("h3" if _hl_now else "none"))

    _sc_st_now = st.session_state.get("wm_sc_state")
    _sc_pa_now = _sc_pa   # normalized: a real protraction code, else None
    _broad_scope = (
        _sc_id_early in ("main", "all")
        and (not _sc_st_now or _sc_st_now == "— all states —")
    )
    WELLS_TOGGLE_CAP = 10000
    _broad_over_cap = False
    if _broad_scope and not _uwi_filter_active:
        if (st.session_state.get("map_mode") == "wells"
                or st.session_state.get("map_mode_radio") == "wells"
                or st.session_state.get("wells_layer_on")):
            # Only force H3 (and Wells off) if the dataset is genuinely large.
            # For a small database (e.g. the demo's ~200 wells) plotting every
            # well is cheap, so honor the Wells toggle — H3 stays on too, both
            # layers show, and the page doesn't shift (H3 remains the default).
            try:
                _broad_n = _qry_wells_scope_count(engine, where_extra=_qry_where)
            except Exception:
                _broad_n = -1
            if _broad_n == -1 or _broad_n >= WELLS_TOGGLE_CAP:
                # ── WARN, DO NOT OVERRIDE ──────────────────────────────
                # This used to switch Wells OFF and H3 ON. Reported as "when
                # I turn on wells it starts to draw but then turns on
                # hexagons" -- a control arguing with the person using it,
                # which is worse than a slow map.
                #
                # It was written when nothing stood between the browser and
                # 28,000 markers. _WELLS_DRAW_CAP is that something now: the
                # layer draws at most 5,000 and says how many it did not.
                # So the volume is already handled, and taking the toggle
                # away solves a problem twice while making the page
                # unpredictable.
                #
                # The count still matters, so it is still SAID -- see the
                # caption below, which now reports rather than announces a
                # decision already taken.
                _broad_over_cap = True
                if _broad_n == -1:
                    # The count FAILED, which is not the same as being large.
                    # Nothing is known about the scope, so fall back to the
                    # aggregate: it is the right answer to a question we could
                    # not ask.
                    st.session_state["map_mode"] = "h3"
                    st.session_state["map_mode_radio"] = "h3"
                    st.session_state["wells_layer_on"] = False
                    st.session_state["h3_layer_on"] = True
                elif not st.session_state.get("wells_layer_on"):
                    # ── DEFAULT TO HEXAGONS, DO NOT OVERRIDE ───────────────
                    # A broad scope with tens of thousands of wells wants the
                    # aggregate, and H3 earns that default because CLICKING A
                    # CELL IS EXACT: the drill filters on the well's own
                    # h3_r<res>, not on the cell's bounding box, which holds
                    # ~30% more wells (measured: 3,062 in the box of R5 cell
                    # 8526a01bfffffff against 2,364 in the hexagon).
                    #
                    # The distinction from what this code used to do is the
                    # whole point: it fills in a layer nobody chose, and does
                    # not take away one somebody did. Turn Wells on and it
                    # stays on, capped and reported.
                    st.session_state["h3_layer_on"] = True

    _need_wells = (
        _has_real_selection
        # ⛔ Abort holds the QUERY too, not just the draw. Holding only
        # the layers would leave the slowest part of a broad render --
        # pulling tens of thousands of wells -- running on every rerun
        # while the map showed nothing: the worst of both.
        and not _render_held
        and not st.session_state.get("wells_suppressed", False)
        and (
            _uwi_filter_active   # explicit UWI lookup — allowed at any scope
            or _data_filter_active  # has_* is just as narrow: same bypass
            or (
                # BROAD SCOPE NO LONGER SUPPRESSES THE LOAD. This read
                # `not _broad_over_cap`, which paired with the old
                # forced switch. With the switch gone the flag is only
                # advisory, and leaving it here would make the Wells
                # toggle look like it worked while loading nothing --
                # a control that appears to work and does not, which is
                # worse than one that is absent. The wells_layer_on test
                # below is the real gate, and the count-failed fallback
                # clears it explicitly.
                _broad_scope
                and (st.session_state.get("map_mode", "none") == "wells"
                     or st.session_state.get("wells_layer_on"))
            )
            or (
                not _broad_scope   # narrowed scope: the normal triggers
                and (
                    st.session_state.get("map_mode", "none") == "wells"
                    or st.session_state.get("wells_layer_on")
                    or st.session_state.get("ai_filter_spec") is not None
                    or _zoom_target_has_filter
                    or _adv_filters_active
                )
            )
        )
    )
    # NOTE: _wells_already_loaded was previously an OR clause here. It
    # acted as a sticky bit — once the wells query fired once in any
    # mode, every subsequent rerun re-fired it because the flag stayed
    # True. That caused mode changes (Grid → H3 → GeoJSON) to trigger
    # a 60+ second pyodbc pull every time. Removed because the data
    # itself is @st.cache_data-decorated, so the "skip reload because
    # already loaded" optimization the flag was supposed to enable is
    # already free at the cache layer.

    if _has_hits is not None:
        # THE COUNT IS THE FEEDBACK. One well out of 1,373 is visually
        # indistinguishable from a filter that did nothing -- and if the map
        # has been panned away from it, from a filter that found nothing. The
        # number is said out loud whether it is 0, 1 or 1,079.
        _hlbl = _early_qsel or _early_qtype
        if _has_hits == 0:
            st.warning("%s — no wells match." % _hlbl)
        else:
            st.caption(
                "%s — %s well(s) match.%s"
                % (_hlbl, format(_has_hits, ","),
                   "" if _has_hits <= _HAS_PLOT_CAP else
                   "  Too many to plot individually at this scope — narrow "
                   "the area or switch to Wells mode."))
            if st.session_state.get("h3_layer_on"):
                st.caption(
                    "The density hexagons count every well and cannot apply "
                    "this filter — they are pre-aggregated. Read the well "
                    "markers, not the shading.")

    if _need_wells:
        # Wells mode (or AI filter / dropdown active) — load the full well list.
        # This is the 30-second hit. Show a real progress bar at the top so the
        # user knows the system is working and roughly when it'll finish.
        #
        # We can't get true progress from a single SQL query, so we use a
        # three-stage indicator with sleeps that match typical execution time:
        #   0–40%  during DB query (the slowest part)
        #   40–80% during JSON parse
        #   80–100% during pandas conversion + cache write
        # If the query takes longer than expected, we hold at 80% until done.
        #
        # Schema dispatch: the Area selector (top4) renders AFTER this
        # loader, so active_area isn't set yet. Read the user's last area
        # choice from session state — same lookahead pattern Zoom-To and
        # the Query whitelist use. A GOM-only area loads from
        # dataview_gom.well; everything else uses the dv_well path.
        _wells_area_label = st.session_state.get("wm_area_sel")
        _wells_area = next(
            (a for a in AREAS if a["label"] == _wells_area_label),
            AREAS[0],
        )
        _wells_srcs = _wells_area.get("sources", [])
        _use_gom_wells = ("gom" in _wells_srcs and "main" not in _wells_srcs)

        # ITS OWN PLACEHOLDER, created HERE. _status existed only to host
        # this one transient line, and lived at the top of the page purely
        # because that is where it was declared -- so the message appeared
        # far above where anyone was looking, and cost a permanent gap slot
        # up there for the whole render. _prog_bar below was already inline;
        # this just matches it.
        _prog_msg = st.empty()
        _prog_bar = st.progress(0)
        _prog_msg.info("⏳ Loading wells from DataView via BCP — typically ~30 sec for full main, ~2 sec for GoM…")
        try:
            _prog_bar.progress(10)
            # Session 4: BCP-bypass loaders replace pyodbc transport.
            # The BCP path is 50-100x faster than the original _qry_wells /
            # _qry_gom_wells (which hang on 477K rows via pyodbc). See
            # transport_diagnostic.md for the timing breakdown.
            # Read the well-count guardrail once, before the branch, so it's
            # always in scope for the cap-hit check below (even on the GOM
            # path where it isn't used for the query).
            _well_limit = _WELLS_LOAD_CAP
            # Derive a center for nearest-N ordering from the currently
            # selected area. active_area isn't computed until later in the
            # page, so resolve the selection inline here. Falls back to None
            # (→ order by uwi) if anything is unresolved. This makes the
            # capped well set "the N nearest the area you picked" instead of
            # a geographically arbitrary slice.
            _load_center_lat = None
            _load_center_lon = None
            try:
                _sel_lbl = st.session_state.get("wm_area_sel")
                _area_match = next(
                    (a for a in AREAS if a["label"] == _sel_lbl), None
                )
                if _area_match and _area_match.get("center"):
                    _load_center_lat = float(_area_match["center"][0])
                    _load_center_lon = float(_area_match["center"][1])
            except Exception:
                _load_center_lat = _load_center_lon = None

            if _use_gom_wells:
                _wells_raw = _qry_gom_wells_bcp(engine)
            else:
                # Well-count guardrail: never pull more than the slider's
                # value (default 10,000). Prevents accidentally loading and
                # rendering the full 500K+ table. The slider lives in the
                # Wells-mode caption; its value persists in session_state.
              # THE BOX GOES INTO THE QUERY. Clipping only the frame
              # still pulled every well in scope and threw most away.
              _clip_pred = _clip_sql("w")
              if _clip_pred:
                  _say("[map] wells query constrained to the drawn box")
              _wells_raw = _qry_wells_bcp(
                    engine, limit=_well_limit,
                    center_lat=_load_center_lat,
                    center_lon=_load_center_lon,
                    where_extra=_qry_where + _clip_pred,
                )
            _prog_bar.progress(80)
            _prog_msg.info(f"⏳ Processing {len(_wells_raw):,} wells…")
            st.session_state["_wells_already_loaded"] = True
            # A deliberate load un-suppresses the base cluster layer that
            # "✗ Clear wells" may have hidden. Loading wells means the user
            # wants to see them again.
            st.session_state["wells_suppressed"] = False
            _prog_bar.progress(100)
            # Compute "wells near center" so the user knows how many exist
            # in the area vs how many we loaded. Only meaningful when we have
            # a center (area selected) and are on the main path.
            _near_count = -1
            _near_radius = 50.0  # miles
            if (not _use_gom_wells
                    and _load_center_lat is not None
                    and _load_center_lon is not None):
                _near_count = _qry_well_count_near(
                    engine, _load_center_lat, _load_center_lon,
                    radius_mi=_near_radius,
                )

            # Build the count phrase if we have a valid near-count.
            _near_phrase = ""
            if _near_count >= 0:
                _near_phrase = (
                    f" · ~{_near_count:,} wells within "
                    f"{int(_near_radius)} mi of center"
                )

            # If we got back exactly the cap, the result was almost
            # certainly truncated — tell the user so they know there are
            # more wells than shown and how to see them.
            if (not _use_gom_wells
                    and _well_limit > 0
                    and len(_wells_raw) >= _well_limit):
                _prog_msg.warning(
                    f"✅ Showing nearest {len(_wells_raw):,} wells to center"
                    f"{_near_phrase}. More exist — narrow with a Query or "
                    f"use 🔶 H3 mode for a full density overview."
                )
            else:
                _prog_msg.success(
                    f"✅ Loaded {len(_wells_raw):,} wells{_near_phrase}"
                )
        except Exception as _wq_err:
            _prog_msg.error(f"Wells load failed: {_wq_err}")
            _wells_raw = []
        finally:
            # Clear the progress widgets shortly after completion. They've
            # served their purpose; leaving them clutters the UI.
            import time
            time.sleep(0.5)
            _prog_bar.empty()
            _prog_msg.empty()
    else:
        # Grid mode default — show grid immediately, skip the heavy query
        _wells_raw = []

    # NO TRANSIENT BANNER HERE. This wrote "Loading spatial layers", then
    # cleared it two lines later in the same run -- so it either never
    # appeared or appeared as a flash, while costing a permanent gap slot in
    # the flex column either way. _phase() already reports progress.
    shp_layers = _load_shp_layers(engine)
    # counts_df loaded lazily below — only when a "Has X" filter is active

    # LEASE ONTO THE LOADED WELLS, before any filter reads them. See
    # _well_lease_map for why the loaded mode needs the keys present and the
    # database mode does not. Cheap and cached; skipped entirely when no
    # tracts are loaded, which is the state this database was in until today.
    if _wells_raw:
        try:
            _lz = _well_lease_map(engine)
            if _lz:
                for _w in _wells_raw:
                    _lt = _lz.get(str(_w.get("uwi") or "").strip())
                    if _lt:
                        (_w["lease_name"], _w["lease_number"],
                         _w["lease_operator"]) = _lt
        except Exception as _lze:
            print(f"[lease_enrich] {_lze}")

    # Apply AI filter if active (only meaningful when we have wells)
    _ai_spec = st.session_state.get("ai_filter_spec")
    # The filter runs over _wells_raw — the main wells query. Wells can also
    # reach the map through the drill shadow (tray_well_data) without
    # _wells_raw ever being populated, and in that case the filter was
    # skipped ENTIRELY and silently: a spec on screen, a full map, and no
    # filtering. Record it so the panel can say so.
    # Two different reasons the filter can have nothing to work on, and they
    # need different advice. Either the map is empty (wells cleared, or never
    # loaded), or the map has wells that arrived through the drill shadow
    # without _wells_raw being populated. Telling someone who just pressed
    # Clear that "these wells did not come from the main query" is nonsense —
    # there are no wells at all.
    st.session_state["ai_filter_no_source"] = bool(_ai_spec) and not _wells_raw
    st.session_state["ai_filter_map_empty"] = (
        bool(_ai_spec) and not _wells_raw
        and not st.session_state.get("tray_well_data")
        and not st.session_state.get("viewport_uwis"))

    # ── DATABASE MODE ───────────────────────────────────────────────────────
    # Translate the spec to a WHERE fragment and let the wells query answer it,
    # instead of sieving what happens to be loaded. This is the difference
    # between "which of these 350 wells spudded since 2020" and "which wells
    # spudded since 2020" — the second is usually the question being asked, and
    # the loaded set is an accident of how the map was navigated.
    #
    # It REPLACES the loaded set rather than narrowing it, so the area/query
    # scope no longer applies. That is the point of the mode; the caption says
    # so, and AI_DB_LIMIT caps what a broad question can pull back.
    _ai_db_mode = str(st.session_state.get("wm_ai_scope", "")).startswith("Whole")
    st.session_state.pop("ai_filter_rejected", None)
    if _ai_spec and _ai_db_mode:
        _ai_where, _ai_rejected = _ai_spec_to_where(
            _ai_spec, _ai_db_columns(engine))
        st.session_state["ai_filter_rejected"] = _ai_rejected
        st.session_state["ai_filter_sql_where"] = _ai_where
        if _ai_where:
            try:
                _display_wells = _qry_wells_bcp(
                    engine, limit=AI_DB_LIMIT, where_extra=_ai_where) or []
            except Exception as _e:
                _display_wells = []
                st.session_state["ai_filter_rejected"] = (
                    _ai_rejected + [f"query failed: {type(_e).__name__}: {_e}"])
            st.session_state["ai_filter_match"] = (len(_display_wells), None)
            st.session_state["ai_filter_empty_fields"] = []
            st.session_state["ai_filter_clauses"] = []
            st.session_state["ai_filter_no_source"] = False
            st.session_state["ai_filter_map_empty"] = False
    elif _ai_spec and _wells_raw:
        _display_wells = _apply_ai_filter(_wells_raw, _ai_spec)
        # Diagnostics so a 0-match isn't a silent blank map. Record how many
        # of the loaded wells matched, and flag any filtered field that's
        # empty for EVERY loaded well — a NULL field (e.g. final_td not
        # populated for KGS/synthetic wells) drops every row and looks like a
        # broken filter when it's really missing data.
        def _populated(_v):
            if _v is None:
                return False
            return str(_v).strip().lower() not in (
                "", "0", "0.0", "none", "nan", "null")
        _ai_fields = [f.get("field") for f in _ai_spec.get("filters", [])
                      if f.get("field")]
        _empty_fields = [
            _fld for _fld in dict.fromkeys(_ai_fields)
            if not any(_populated(w.get(_fld)) for w in _wells_raw)
        ]
        st.session_state["ai_filter_match"] = (len(_display_wells), len(_wells_raw))
        st.session_state["ai_filter_empty_fields"] = _empty_fields

        # PER-CLAUSE counts. A combined 0 tells you nothing actionable: the
        # model may have written a sensible filter that legitimately matches
        # nothing, or a nonsense one. Applying each clause ALONE separates
        # those — "spud_date >= 2020 matched 0 by itself, and the sample
        # values are 2016-04-11" is a diagnosis; "0 of 50 matched" is not.
        # Sample values come along because most surprises here are a type
        # mismatch (a date compared as text) rather than a wrong predicate.
        _clauses = []
        for _f in (_ai_spec.get("filters") or []):
            _fld = _f.get("field")
            try:
                _alone = len(_apply_ai_filter(_wells_raw, {"filters": [_f]}))
            except Exception:
                _alone = None
            _samples = [str(_w.get(_fld)) for _w in _wells_raw[:300]
                        if _populated(_w.get(_fld))][:3]
            _clauses.append({
                "clause": f"{_fld} {_f.get('op')} {_f.get('value')!r}",
                "matches alone": "error" if _alone is None else _alone,
                "example values in the data": ", ".join(_samples) or "(all empty)",
            })
        st.session_state["ai_filter_clauses"] = _clauses
    else:
        _display_wells = _wells_raw
        st.session_state.pop("ai_filter_match", None)
        st.session_state.pop("ai_filter_empty_fields", None)
        st.session_state.pop("ai_filter_clauses", None)

    # Merge drilled wells from the shadow cache into _display_wells.
    # The shadow is populated by the rectangle/circle drill paths
    # (tray_well_data) and contains full well dicts. Without this merge,
    # the well picker in the sidebar shows only the wells loaded into
    # _wells_raw — which is empty in Grid mode and limited to the
    # current filter in Wells mode. Merging means: after a user draws a
    # circle or bbox to drill an area, those wells become searchable
    # in the picker AND can be added to the tray from there.
    # 🔑 THE MERGE MUST NOT UNDO THE FILTER.
    #
    # This ran unconditionally and appended every drilled well back into
    # _display_wells — which is what wells_df, and therefore the MAP, is
    # built from. So an AI filter narrowed the set and the very next block
    # put the removed wells straight back. That is the whole of the
    # "intermittent AI filter" bug: it worked in a fresh session with an
    # empty shadow, and silently stopped the moment anyone drew a rectangle
    # or a circle. Spec on screen, full map, no explanation.
    #
    # The merge is still right for what it was FOR — the sidebar picker, so
    # a well you drilled can be found and added to the tray. So the merged
    # set now lives in its own name, and the map keeps the filtered one.
    # 🔑 THE MERGE MUST NOT UNDO THE FILTER.
    #
    # This appended every drilled well back into _display_wells, which is
    # what wells_df — and therefore the MAP — is built from. So an AI filter
    # narrowed the set and the very next block put the removed wells back.
    # That is the whole of the "intermittent AI filter" bug: it worked in a
    # fresh session with an empty shadow and silently stopped the moment
    # anyone drew a rectangle or a circle. Spec on screen, full map, no
    # explanation.
    #
    # The merge itself is still right — a well you drilled should stay
    # findable. So the SHADOW WELLS GO THROUGH THE SAME FILTER before they
    # are merged, rather than bypassing it. With no spec running,
    # _apply_ai_filter returns them untouched, so every other case behaves
    # exactly as it did.
    _shadow_for_display = st.session_state.get("tray_well_data", {})
    if _shadow_for_display:
        _existing_uwis = {w.get("uwi") for w in _display_wells}
        _shadow_new = [_w for _u, _w in _shadow_for_display.items()
                       if _u not in _existing_uwis]
        if _ai_spec and _shadow_new:
            try:
                _shadow_new = _apply_ai_filter(_shadow_new, _ai_spec)
            except Exception:
                # A filter that cannot be evaluated must not silently
                # readmit everything it was meant to exclude.
                _shadow_new = []
        _display_wells = list(_display_wells)   # copy — never mutate _wells_raw
        _display_wells.extend(_shadow_new)

    # Convert list-of-dicts to DataFrame once — all downstream code unchanged.
    # In lazy-load case this is an empty DataFrame, which is fine: the grid
    # render path doesn't use it, and the dropdowns/filters gracefully show
    # empty lists.
    wells_df  = pd.DataFrame(_display_wells) if _display_wells else pd.DataFrame()
    # Build the UWI index used for scout-ticket lookups.
    # Two sources:
    #   1. _wells_raw — the full wells list (populated only in Wells mode or
    #      when filters force a wells load)
    #   2. tray_well_data — the shadow cache populated by grid-cell drills
    #      and rectangle drills (always available regardless of mode)
    # We combine them so scout tickets work whether the user is in grid mode
    # with drilled wells OR in wells mode with the full list.
    uwi_index = {w["uwi"]: w for w in _wells_raw}
    _shadow = st.session_state.get("tray_well_data", {})
    for _u, _w in _shadow.items():
        # Tray-shadow wells fill in only where _wells_raw didn't already cover
        if _u not in uwi_index:
            uwi_index[_u] = _w

    # ── Resolve the active Area BEFORE rendering the top bar ──────────
    # The Area selector widget lives in top4 (rightmost column), but the
    # Zoom-To (top2) and Query (top3) dropdowns need to know the active
    # area to build their options. Streamlit renders columns in code
    # order, so top2/top3 would otherwise see a stale wm_area_sel from
    # the previous run — that's the "Query only shows All wells" bug.
    #
    # Fix: resolve active_area here, up front, from the session-state
    # value the Area widget wrote on the LAST run. On the run where the
    # user changes the area, the widget in top4 updates wm_area_sel, and
    # because the widget key is bound, st.session_state["wm_area_sel"]
    # already reflects the NEW selection by the time this code runs on
    # the NEXT script execution. The Area widget in top4 still renders
    # and drives the selection; this block just reads the current value
    # early so every dropdown in the top bar agrees within one run.
    _area_labels_display = [
        (a["label"] if a["enabled"] else f"{a['label']} (no data)")
        for a in AREAS
    ]
    # EVERY SESSION GETS ALL SCHEMAS, not just the first one in the process.
    # The cold-start block above already writes this default, but it is gated
    # on _PROCESS_FIRST_RUN_DONE -- a MODULE-LEVEL global, so it fires once
    # per Streamlit PROCESS and never again. The first browser session after a
    # restart got All schemas; every session after it fell through to the
    # placeholder "— Select schema —", and the placeholder's id is not one of
    # ("main", "all", "gom"), which is what makes the State and County pickers
    # collapse to "Select a schema to constrain by geography". Reported as
    # "my state and county selector has disappeared" after a relaunch.
    #
    # THE DISPLAY LABEL, NOT THE LITERAL. _area_labels_display appends
    # " (no data)" to a disabled area, so the bare "🌎 All schemas" is not
    # always one of the options -- and a session value that matches no option
    # is how a selectbox raises rather than defaults.
    if "wm_area_sel" not in st.session_state:
        st.session_state["wm_area_sel"] = next(
            (_lbl for _a, _lbl in zip(AREAS, _area_labels_display)
             if _a.get("id") == "all"), _area_labels_display[0])
    _area_sel_current = st.session_state.get("wm_area_sel",
                                             _area_labels_display[0])
    try:
        _active_idx = _area_labels_display.index(_area_sel_current)
    except ValueError:
        _active_idx = 0
    active_area = AREAS[_active_idx]

    # ── Top bar above map (two rows so labels don't truncate) ─────────
    # Five selectboxes on one row squeezed each to ~1/6 width and clipped
    # ("Da…", "—…"). Split into two rows by function:
    #   Row A — context/source : Background | Schema | Database
    #   Row B — navigate/filter: Zoom to | Query (Query 2x for long
    #           labels + its value-picker that renders underneath).
    # Names top1..top5 are preserved so every `with topN:` block below is
    # untouched — we only repoint each name at its new row/cell. The
    # `with` blocks still execute in the same order; only visual
    # placement changes (a column container holds its position from where
    # it was created, regardless of when it's populated).
    # Row B (Zoom-to/Query) is gone: Go-to was removed and Query now renders
    # below the "Constrain to" selector. top3 (vestigial Go-to no-op) runs
    # as a plain statement just below; top1/top2/top5 stay in Row A.
    _row_a = st.columns([1, 1, 1])   # Background, Schema, Database
    top1 = _row_a[0]   # 🖼 Background
    top2 = _row_a[1]   # 🗂 Schema
    top5 = _row_a[2]   # 📊 Database
    with top1:
        basemap = st.selectbox("🖼 Background", _BASEMAPS_SHOWN,
                               key="wm_basemap")
    with top2:
        # Area selector — partitions which region's well data renders on
        # the map. Moved BEFORE Zoom to / Query because Zoom-To options
        # depend on Area, and Query options are constrained by Area's
        # "queries" whitelist. active_area was resolved up-front so the
        # downstream columns can use it.
        # NB: the page-entry block writes wm_area_sel = "🌎 All schemas", so
        # an index= here would never apply -- a stored value always wins. The
        # opening VIEW is settled where the camera is chosen instead: with no
        # layer switched on, that frames the lower 48 whatever the schema is.
        area_sel = st.selectbox(
            "🗂 Schema", _area_labels_display,
            key="wm_area_sel",
            help="Which database schema to query. 'All schemas' reads "
                 "both dataview (onshore) and dataview_gom (offshore). "
                 "Gulf of America reads only dataview_gom. Geographic "
                 "filtering (plays, state regions) lives in the Region "
                 "selector in the left control panel.",
        )

        # Auto-zoom to the area's centroid when the selection changes.
        # We compare the chosen id to the last-seen id and trigger a one-shot
        # _drawn_bounds set so the existing fit_bounds machinery snaps the
        # map to the right region.
        # The placeholder area (id="none") doesn't trigger auto-zoom — it
        # has no meaningful destination, just stays wherever the user is.
        _prev_area_id = st.session_state.get("_wm_prev_area_id")
        if (active_area["enabled"]
                and active_area["id"] != "none"
                and active_area["id"] != _prev_area_id):
            st.session_state["_wm_prev_area_id"] = active_area["id"]
            # Compute a small bounding box from the centroid + zoom-derived
            # span. Lower zoom = bigger span. The fit_bounds receiver expects
            # [[min_lat, min_lon], [max_lat, max_lon]].
            _clat, _clon, _czoom = active_area["center"]
            # Rough span based on zoom: each zoom level halves the span
            _span = max(0.5, 30.0 / (2 ** (_czoom - 4)))
            st.session_state["_drawn_bounds"] = [
                [_clat - _span/2, _clon - _span],
                [_clat + _span/2, _clon + _span],
            ]
            # Mark this as a ONE-SHOT fit. The map consumer will pop the
            # bounds after applying them once, so subsequent reruns (e.g.
            # cell clicks) don't keep snapping the view back to the area
            # overview. Cell-Commit drills and circle drills set
            # _drawn_bounds WITHOUT this flag — those persist correctly.
            st.session_state["_drawn_bounds_oneshot"] = True
            # Also reset any cell selection from the prior area — different
            # region, different cells, no carry-over makes sense
            st.session_state["selected_cells"] = []
            st.session_state.pop("_last_grid_click", None)
    # Go-to (Zoom-To) was removed — its navigate+filter-by-place role
    # is now handled by the standing "Constrain to" spatial selector
    # (State/County onshore, Protraction GOM), which moves the
    # viewport and constrains wells in one control. These two names
    # are kept defined as no-ops so the downstream center fallback
    # (zoom_target) and the zoom-to mask branch (zoom_targets) don't
    # break. wm_zoom_target is never set now, so the mask short-circuits.
    zoom_targets = []
    zoom_target = None
    st.session_state.pop("wm_zoom_target", None)
    with top5:
        # Database selector. Lists the user databases on the server and
        # defaults to the one the app connected to. Changing it re-points the
        # whole map (engine + bcp) on the next rerun via the resolution block
        # at the top of run().
        _conn_db = st.session_state.get("wm_conn_db", BCP_DATABASE)
        _db_all = []          # every user database; set inside the try below
        try:
            from sqlalchemy import text as _dbt2
            with engine.connect() as _dbc2:
                # ONLY DATABASES THE MAP CAN ACTUALLY READ.
                #
                # This listed every user database on the instance, which is a
                # promise the page cannot keep: every query here names
                # dataview.dv_well, dataview_federation.* and file_catalog.*,
                # so picking a database shaped differently — WELL_REF, say —
                # produced "Invalid object name
                # 'dataview_federation.v_well_density_r4'" and an empty map.
                # Two people spent an hour discovering that, one of whom wrote
                # the notes on this page.
                #
                # A control that only offers valid choices needs no
                # explanation. The test is the table every path depends on:
                # dataview.dv_well. OBJECT_ID with a three-part name returns
                # NULL rather than raising when the database is unreachable or
                # the object is absent, so one query settles the whole list.
                #
                # Other sources reach the map by being FEDERATED INTO
                # dataview_federation.v_well — that is the supported route,
                # and it is how the 4M-well reference draws today.
                _db_all = [r[0] for r in _dbc2.execute(_dbt2(
                    "SELECT name FROM sys.databases "
                    "WHERE database_id > 4 AND state = 0 "
                    "ORDER BY name")).fetchall()]
                _db_options = []
                for _dbn in _db_all:
                    try:
                        _ok = _dbc2.execute(_dbt2(
                            "SELECT OBJECT_ID(:o)"),
                            {"o": f"[{_dbn}].dataview.dv_well"}).scalar()
                    except Exception:
                        _ok = None
                    if _ok is not None:
                        _db_options.append(_dbn)
        except Exception:
            _db_options = []
        if _conn_db and _conn_db not in _db_options:
            _db_options = [_conn_db] + _db_options
        if not _db_options:
            _db_options = [_conn_db or BCP_DATABASE]
        # Keep the live selection valid even if it isn't in the list.
        _cur = st.session_state.get("wm_map_db", _conn_db)
        if _cur and _cur not in _db_options:
            _db_options = [_cur] + _db_options
        _map_db = st.selectbox("📊 Database", _db_options, key="wm_map_db",
                                help="Which database the map reads from "
                                     "(engine + bcp). Only databases carrying "
                                     "a dataview.dv_well table are listed — "
                                     "the map cannot read an arbitrary "
                                     "database. Other sources reach the map by "
                                     "being federated into "
                                     "dataview_federation.v_well, which is how "
                                     "the national reference draws.")

    # ── Spatial constraint ("Constrain to") — standing, composes w/ Query ─
    # Geography filter applied after Query and before the map renders. Its
    # selections (wm_sc_* keys) are read up-front and folded into
    # where_extra, so it narrows whatever Query produced. Schema-aware:
    #   dataview / All → State + County from the US boundary map (us_geo);
    #                    filter is a lat/lon bbox, valid on both tables.
    #   dataview_gom   → Protraction area (friendly names)
    _sc_id = active_area.get("id")

    def _protraction_codes():
        """Distinct bottom_area_code values in the GOM table (cached)."""
        from sqlalchemy import text as _pa_t
        if "_sc_protraction_opts" not in st.session_state:
            try:
                with engine.connect() as _c:
                    _r = _c.execute(_pa_t(
                        "SELECT DISTINCT bottom_area_code FROM dataview_gom.well "
                        "WHERE bottom_area_code IS NOT NULL "
                        "AND LTRIM(RTRIM(bottom_area_code)) <> '' "
                        "ORDER BY bottom_area_code")).fetchall()
                st.session_state["_sc_protraction_opts"] = [x[0] for x in _r]
            except Exception:
                st.session_state["_sc_protraction_opts"] = []
        return st.session_state.get("_sc_protraction_opts", [])

    _sc_l, _sc_a, _sc_b = st.columns([1, 2, 2])
    _sc_l.markdown("🧭 **Constrain to**")

    if _sc_id not in ("main", "all", "gom"):
        _sc_a.caption("Select a schema to constrain by geography.")
    elif _us_geo is None or not HAS_US_GEO:
        _sc_a.caption("Add assets/geo/us_counties.geojson to enable the "
                      "geography constraint.")
    else:
        # State dropdown = US states (us_geo) + Gulf of Mexico (offshore).
        _state_codes = ["— all states —"] + _us_geo.states() + [_GULF_STATE]
        if st.session_state.get("wm_sc_state") not in _state_codes:
            st.session_state.pop("wm_sc_state", None)
        _sc_a.selectbox(
            "State", _state_codes, key="wm_sc_state",
            on_change=_lift_well_suppression,
            format_func=lambda c: ("🌊 Gulf of Mexico" if c == _GULF_STATE
                                   else c if c == "— all states —"
                                   else _us_geo.state_name(c)),
            label_visibility="collapsed")
        _cur_state = st.session_state.get("wm_sc_state")

        if _cur_state == _GULF_STATE:
            # Sub-list = Protraction Areas. The selectbox key IS
            # wm_sc_protraction (so Streamlit sets it before the script runs —
            # no one-rerun lag). "— all areas —" means the whole Gulf.
            _codes = _protraction_codes()
            _pa_opts = ["— all areas —"] + list(_codes)
            if st.session_state.get("wm_sc_protraction") not in _pa_opts:
                st.session_state.pop("wm_sc_protraction", None)

            def _pa_fmt(cd):
                if cd == "— all areas —":
                    return cd
                try:
                    nm = _boem_area_name(cd)
                except Exception:
                    nm = cd
                return f"{nm} ({cd})" if nm and nm != cd else str(cd)

            if _codes:
                _sc_b.selectbox("Protraction Area", _pa_opts,
                                key="wm_sc_protraction", format_func=_pa_fmt,
                                label_visibility="collapsed")
            else:
                _sc_b.caption("Whole Gulf (no protraction areas found)")
                st.session_state["wm_sc_protraction"] = "— all areas —"
            st.session_state.pop("wm_sc_county", None)

        elif _cur_state and _cur_state != "— all states —":
            # Sub-list = Counties. Clear any protraction selection.
            st.session_state["wm_sc_protraction"] = "— all areas —"
            _sc_counties = ["— all counties —"] + _us_geo.counties(_cur_state)
            if st.session_state.get("wm_sc_county") not in _sc_counties:
                st.session_state.pop("wm_sc_county", None)
            _sc_b.selectbox("County", _sc_counties, key="wm_sc_county",
                            on_change=_lift_well_suppression,
                            label_visibility="collapsed")
        else:
            st.session_state.pop("wm_sc_county", None)
            st.session_state["wm_sc_protraction"] = "— all areas —"
            _sc_b.caption("All counties / areas")

    # ── Spatial selector → viewport (absorbs Go-to's navigate role) ─────
    # On selection change, fit the map to the selected boundary's exact bbox:
    # a US state/county from us_geo, or — when Gulf of Mexico is the chosen
    # "state" — a protraction-area polygon (or the whole-Gulf footprint) from
    # boem_geo. Fires only on change (tracked via _sc_vp_prev) so it doesn't
    # yank the view after a pan.
    _sc_vp_key = None
    _sc_vp_bounds = None        # [[min_lat,min_lon],[max_lat,max_lon]]
    if _sc_id in ("main", "all", "gom") and _us_geo is not None and HAS_US_GEO:
        _vp_st = st.session_state.get("wm_sc_state")
        if _vp_st == _GULF_STATE:
            _vp_pa = st.session_state.get("wm_sc_protraction")
            _vp_pa = _vp_pa if (_vp_pa and _vp_pa != "— all areas —") else None
            _bb = None
            if _boem_geo is not None and HAS_BOEM_GEO:
                _bb = (_boem_geo.bbox(_vp_pa) if _vp_pa
                       else _boem_geo.overall_bbox())
            if _bb:
                _sc_vp_key = f"gulf::{_vp_pa or '*'}"
                _sc_vp_bounds = [[_bb[0], _bb[1]], [_bb[2], _bb[3]]]
        elif _vp_st and _vp_st != "— all states —":
            _vp_co = st.session_state.get("wm_sc_county")
            _vp_county = (_vp_co if (_vp_co and _vp_co != "— all counties —")
                          else None)
            _bb = _us_geo.bbox(_vp_st, _vp_county)
            if _bb:
                _sc_vp_key = f"geo::{_vp_st}::{_vp_county or '*'}"
                _sc_vp_bounds = [[_bb[0], _bb[1]], [_bb[2], _bb[3]]]

    if _sc_vp_key and _sc_vp_key != st.session_state.get("_sc_vp_prev"):
        if _sc_vp_bounds:
            st.session_state["_drawn_bounds"] = _sc_vp_bounds
            st.session_state["_drawn_bounds_oneshot"] = True
        st.session_state["_sc_vp_prev"] = _sc_vp_key

    # ── Left panel + map ─────────────────────────────────────────────

    # ── Query — filter within the area of interest ───────────────────
    # Renders directly below the "Constrain to" selector: define the
    # area first (State/County or Protraction), then filter the wells
    # inside it. Stays before the map so qtype/qvalue feed the mask with
    # no extra render lag.
    # Master list of every query type the page knows how to run.
    # label → qtype-key. Each AREAS entry's "queries" list says which
    # of these keys are valid for that area's schema.
    #
    # PHILOSOPHY: Query is for filtering by ATTRIBUTE (operator,
    # well_type, source, presence of data). Filtering by PLACE
    # (field, county, basin, protraction_area) lives in Zoom-to —
    # picking a place there navigates AND filters. This avoids
    # having two paths to the same outcome.
    QUERIES = {
        "— no filter —":None,"By UWI":"uwi","By operator":"operator",
        "By well type":"well_type",
        "By source":"source",
        "By area":"area",
        "By total depth":"td_range",
        "By spud date":"spud_range",
        "By completion date":"comp_range",
        "Has documents":"has_docs",
        "Has formation tops":"has_tops","Has production data":"has_prod",
        "Has DST":"has_dst","Has directional survey":"has_survey",
        "Has core data":"has_core","Has core photos":"has_core_photos",
        "Has petro interpretation":"has_petro",
    }
    # Map qtype-key → label so we can go from a whitelist entry back
    # to its display label. "all" is the key for the None ("All
    # wells") option.
    _qkey_to_label = {("all" if v is None else v): k
                      for k, v in QUERIES.items()}

    # The active area was resolved up front (before the top columns).
    # Use it directly — no stale-lookahead needed. active_area's
    # "queries" list is the whitelist of valid query-type keys for
    # that area's schema.
    _allowed_qkeys = active_area.get("queries", ["all"])
    # Build the visible options in QUERIES order, keeping only the
    # ones whitelisted for this area.
    _query_labels = [
        _qkey_to_label[k] for k in
        ["all","uwi","operator","well_type","source","area",
         "td_range","spud_range","comp_range",
         "has_docs",
         "has_tops","has_prod","has_dst","has_survey",
         "has_core","has_core_photos","has_petro"]
        if k in _allowed_qkeys and k in _qkey_to_label
    ]

    # If the previously-selected query isn't valid for the new schema
    # (e.g. user had "By field" selected in an older session before
    # the place-based queries were moved to Zoom-to, OR if some
    # schema's whitelist excludes the option), drop the stale
    # selection so the selectbox falls back to the first option.
    # Pop rather than assign — you can't set a widget's state key
    # after it's instantiated, but popping it before instantiation
    # is fine.
    _prev_qsel = st.session_state.get("wm_query_sel")
    if _prev_qsel is not None and _prev_qsel not in _query_labels:
        st.session_state.pop("wm_query_sel", None)

    qsel   = st.selectbox("📋 Query", _query_labels,
                          key="wm_query_sel",
                          on_change=_engage_wells_on_query)
    qtype  = QUERIES[qsel]
    qvalue = None
    # If user picks a query type that needs wells data, trigger a load
    # on the next rerun (no-op if already loaded).
    if qtype in ("uwi", "operator", "well_type", "source", "area",
                 "td_range", "spud_range", "comp_range", "has_docs",
                 "has_tops", "has_prod", "has_dst",
                 "has_survey", "has_core", "has_core_photos", "has_petro"):
        if not st.session_state.get("_wells_already_loaded", False):
            st.session_state["_wells_already_loaded"] = True
            st.rerun()
    # Value-pickers for the currently-selected Query type. When the
    # user picks "By operator" / "By well type" / "By area" / etc.
    # in the Query dropdown (top4 above), the corresponding value
    # selectbox appears below. Each picker pulls its options
    # dynamically from wells_df so users only see values that
    # actually exist in the loaded wells.
    if qtype == "uwi":
        st.text_area("Enter UWI(s)",
            placeholder="Paste UWIs separated by commas or newlines\ne.g.\n4200320001\n4200320002\n4200320003",
            height=120,
            key="wm_q_uwi_text")
        _uwi_val = st.session_state.get("wm_q_uwi_text", "")
        if _uwi_val.strip():
            _uwi_count = len([u for u in re.split(r'[,;\n\r\t]+', _uwi_val) if u.strip()])
            st.caption(f"{_uwi_count} UWI(s) entered")
            if st.button("🔍 Apply UWI Filter", type="primary", use_container_width=True, key="apply_uwi_filter"):
                st.session_state["_wells_already_loaded"] = True
                st.session_state["map_mode"] = "wells"
                st.session_state["map_mode_radio"] = "wells"
                st.session_state["wells_layer_on"] = True
                st.session_state["wells_suppressed"] = False
                st.rerun()
    elif qtype == "operator":
        # Options come from the database, scoped by area only — see
        # _qry_distinct_attr. Reading them from wells_df collapsed the list to
        # whatever was already selected. Falls back to the loaded wells if the
        # query fails, so the control still works rather than vanishing (a
        # missing selectbox loses its session value and resets to the first
        # option on the next run, which is what made this look like a filter
        # that "went back to Anadarko").
        _ops = _qry_distinct_attr(
            engine, "COALESCE(w.operator_name, ba.ba_name, 'Unknown')",
            _spatial_where)
        if not _ops and not wells_df.empty:
            _ops = sorted(wells_df["operator_name"].dropna().unique())
        if _ops:
            qvalue = st.selectbox("Operator", _ops,
                key="wm_q_op", label_visibility="collapsed")
        else:
            st.caption("No operators found for this area.")
    elif qtype == "well_type":
        _wts = _qry_distinct_attr(engine, "w.well_type", _spatial_where)
        if not _wts and not wells_df.empty:
            _wts = sorted(wells_df["well_type"].dropna().unique())
        if _wts:
            qvalue = st.selectbox("Well Type", _wts,
                key="wm_q_wtype", label_visibility="collapsed")
        else:
            st.caption("No well types found for this area.")
    elif qtype == "source":
        # Query distinct sources directly from DB — no need to load all wells
        _src_opts = []
        try:
            from sqlalchemy import text as _src_t
            with engine.connect() as _src_c:
                _src_rows = _src_c.execute(_src_t(
                    "SELECT DISTINCT source FROM [dataview].[dv_well] "
                    "WHERE source IS NOT NULL ORDER BY source"
                )).fetchall()
            _src_opts = [r[0] for r in _src_rows]
        except Exception:
            if not wells_df.empty and "source" in wells_df.columns:
                _src_opts = sorted(wells_df["source"].dropna().unique())
        qvalue = st.selectbox("Source", _src_opts,
            key="wm_q_source", label_visibility="collapsed") if _src_opts else None
    elif qtype == "area" and not wells_df.empty:
        _area_opts = sorted(wells_df["area"].dropna().unique()) if "area" in wells_df.columns else []
        qvalue = st.selectbox("Area", _area_opts,
            key="wm_q_area", label_visibility="collapsed") if _area_opts else None
    elif qtype == "td_range":
        # Total depth (final_td, ft). 0 on a bound = unbounded that side.
        _tc1, _tc2 = st.columns(2)
        _td_lo = _tc1.number_input("Min TD (ft)", min_value=0, max_value=60000,
                                   step=500, key="wm_q_td_lo",
                                   help="0 = no minimum")
        _td_hi = _tc2.number_input("Max TD (ft)", min_value=0, max_value=60000,
                                   step=500, key="wm_q_td_hi",
                                   help="0 = no maximum")
        qvalue = (int(_td_lo), int(_td_hi)) if (_td_lo or _td_hi) else None
        if qvalue:
            st.caption(f"TD {qvalue[0] or '0'}–{qvalue[1] or '∞'} ft")
    elif qtype == "spud_range":
        # Spud date range. ISO strings sort lexicographically; blank = open.
        _sc1, _sc2 = st.columns(2)
        _sp_lo = _sc1.text_input("Spud from", placeholder="YYYY-MM-DD",
                                 key="wm_q_spud_lo")
        _sp_hi = _sc2.text_input("Spud to", placeholder="YYYY-MM-DD",
                                 key="wm_q_spud_hi")
        _sp_lo, _sp_hi = _sp_lo.strip(), _sp_hi.strip()
        qvalue = (_sp_lo, _sp_hi) if (_sp_lo or _sp_hi) else None
    elif qtype == "comp_range":
        # Completion date range. Blank = open on that side.
        _cc1, _cc2 = st.columns(2)
        _cp_lo = _cc1.text_input("Completion from", placeholder="YYYY-MM-DD",
                                 key="wm_q_comp_lo")
        _cp_hi = _cc2.text_input("Completion to", placeholder="YYYY-MM-DD",
                                 key="wm_q_comp_hi")
        _cp_lo, _cp_hi = _cp_lo.strip(), _cp_hi.strip()
        qvalue = (_cp_lo, _cp_hi) if (_cp_lo or _cp_hi) else None

    # Mode is manual: a Query filters the result set but does NOT auto-switch
    # the map to Wells. Flow: pick State + County, add a filter, then toggle
    # 📍 Wells to load and see the filtered result. (The broad-scope guard
    # above keeps all-states on H3 until a state is chosen.)

    # Auto-engage for the Protraction-area constraint (GOM / All): picking
    # an area should show its wells. Fires on change of the selected area.
    _pa_sig = str(_sc_pa_now or "")
    if (_sc_pa_now and _sc_id_early in ("gom", "all")
            and st.session_state.get("_last_pa_sig") != _pa_sig):
        st.session_state["_last_pa_sig"] = _pa_sig
        st.session_state["map_mode"] = "wells"
        st.session_state["map_mode_radio"] = "wells"
        st.session_state["wells_layer_on"] = True
        st.session_state["wells_suppressed"] = False

    # ── Match the theme's framed look on the panels added above the map ─────
    # The Query selectbox and the geography pills get a gold frame from the app
    # theme; expanders do not, so the new panels read as a different class of
    # thing from the controls beside them.
    #
    # SEVERAL SELECTORS ON PURPOSE. Streamlit has moved the expander DOM more
    # than once — data-testid has sat on the wrapper div, on the <details>
    # itself, and before that there were streamlit-expander* class names. A
    # single selector is a guess about which version is installed; the unmatched
    # ones cost nothing.
    #
    # var(--primary-color) rather than a hex, so this follows Midnight Gold to
    # whatever theme is picked next instead of pinning one palette into the page.
    st.markdown(
        "<style>"
        'div[data-testid="stExpander"],'
        'div[data-testid="stExpander"] > details,'
        'div[data-testid="stExpander"] details,'
        'details[data-testid="stExpander"],'
        'section[data-testid="stExpander"],'
        "div.streamlit-expander,"
        "div.stExpander{"
        "border:1px solid var(--primary-color,#E8B84B) !important;"
        "border-radius:8px !important;"
        "background:transparent !important;"
        "box-shadow:none !important;}"
        # The inner <details> would otherwise draw a second frame inside the
        # wrapper's — one box, not two.
        'div[data-testid="stExpander"] details{'
        "border:0 !important;}"
        "</style>", unsafe_allow_html=True)

    # ── AI Well Filter — FULL WIDTH, above the control/map split ─────────────
    # It used to sit inside `ctrl`, the 1-of-4 column, which left a
    # natural-language question box about 200px wide — you could not read your
    # own question back. A question box wants width more than anything else on
    # this page, and it applies to the whole map rather than to one panel, so
    # it belongs above the split rather than inside either side of it.
    # ── AI Query ──────────────────────────────────────────────────
    _ai_open = bool(
        st.session_state.get("ai_filter_spec") or
        st.session_state.get("ai_filter_error") or
        st.session_state.get("ai_filter_desc")
    )
    with st.expander("🤖 AI Well Filter", expanded=_ai_open):
        st.caption("Ask anything about the wells — natural language.")
        # Scope FIRST. It changes what the question means — "mapped wells"
        # sieves what is on screen, "whole database" goes and finds more — so
        # it belongs before the box, where it is read before typing rather
        # than discovered after a surprising result.
        st.radio(
            "Search", ["Mapped wells", "Whole database"],
            horizontal=True, key="wm_ai_scope",
            help="Mapped wells filters the wells already on the map. Whole "
                 "database queries dv_well directly and REPLACES what is "
                 "displayed, ignoring the current area — use it when the "
                 "answer may lie outside what is loaded.")
        # ── NEAR A FEATURE — the deterministic spatial search ──────────────
        # Sits under the AI question deliberately. The geometry is IN THE
        # DATABASE, so "which wells are near line C" is one STDistance away —
        # but the AI filter applies its spec in pandas over already-loaded
        # wells and can neither reach geometry nor should it. This control
        # answers the question with no model involved, and it is what a `near`
        # clause in the spec will eventually CALL rather than reimplement.
        # A TOGGLE, NOT AN EXPANDER — this whole AI panel is already inside one,
        # and expanders cannot nest. Perry predicted this exact trap in July and
        # I walked into it anyway; the error surfaces as "Expanders may not be
        # nested inside other expanders" and takes the page down, not just the
        # control.
        if st.checkbox("📍 Wells near a feature (seismic line, field, lease…)",
                       key="wm_near_open"):
            _nf1, _nf2, _nf3, _nf4 = st.columns([1.4, 2.2, 1.1, 1])
            _feat = _nf1.selectbox(
                "Feature type",
                ["seismic_line", "seismic_survey", "field", "lease", "pipeline"],
                key="wm_near_feat",
                format_func=lambda k: k.replace("_", " ").title())
            # Only features that HAVE geometry are offered — a name in the list
            # that cannot be searched is a control that fails after you use it.
            _names = _near_feature_names(engine, _feat)
            if not _names:
                _nf2.selectbox("Name", ["(none with geometry)"], disabled=True,
                               key=f"wm_near_name_{_feat}")
                st.caption(f"No {_feat.replace('_', ' ')} in this database "
                           f"carries geometry yet.")
            else:
                _nm = _nf2.selectbox("Name", _names, key=f"wm_near_name_{_feat}")
                _dist = _nf3.number_input(
                    "Within (m)", min_value=10, max_value=int(_NEAR_MAX_M),
                    step=100, key="wm_near_dist",
                    help="Straight-line distance on the ellipsoid. "
                         "STDistance on a geography column returns METRES "
                         "whatever CRS the source data arrived in.")
                if _nf4.button("📍 Find", key="wm_near_run",
                               use_container_width=True):
                    _uwis = _wells_near_feature(engine, _feat, _nm, _dist)
                    if not _uwis:
                        st.warning(
                            f"No wells within {int(_dist):,} m of {_nm}. "
                            f"(A well needs a surface coordinate to be found — "
                            f"wells held without one are not in dv_well.)")
                    else:
                        # Becomes an ordinary uwi-in-list filter, so every
                        # existing surface — diagnostics, drill shadow, results
                        # grid — keeps working unchanged. By this point it is
                        # just a well list.
                        st.session_state["ai_filter_spec"] = {
                            "filters": [{"field": "uwi", "op": "in",
                                         "value": _uwis}],
                            "description": (f"Wells within {int(_dist):,} m of "
                                            f"{_feat.replace('_', ' ')} {_nm}")}
                        st.session_state["ai_filter_desc"] = \
                            st.session_state["ai_filter_spec"]["description"]
                        st.session_state["map_mode"] = "wells"
                        st.session_state["map_mode_radio"] = "wells"
                        st.session_state["wells_layer_on"] = True
                        st.session_state["wells_suppressed"] = False
                        st.session_state["_wells_already_loaded"] = False
                        st.rerun()

        # Question on the left, buttons stacked narrow on the right. At full
        # width a 50/50 button split would give two enormous buttons under a
        # wide box; the question is what deserves the space.
        _q_col, _b_col = st.columns([5, 1], gap="small")
        _ai_q = _q_col.text_area(
            "Question",
            key="wm_ai_question",
            label_visibility="collapsed",
            placeholder='e.g. "horizontal wells deeper than 10,000 ft in Loving '
                        'County" or "wells spudded after 2020 with production"',
            height=80,
        )
        _ai_col1 = _b_col
        _ai_col2 = _b_col
        if _ai_col1.button("🔍 Filter", key="wm_ai_run",
                           use_container_width=True, type="primary",
                           disabled=not _ai_q.strip()):
            st.session_state.pop("ai_filter_error", None)
            # Asking a question is asking to SEE the answer, and three separate
            # pieces of state can keep the result invisible: the mode radio,
            # the wells layer switch, and the suppression flag. Setting only
            # the last one left the map blank until the radio was moved by
            # hand. Same set "🔍 Apply UWI Filter" uses — that button is the
            # working precedent for "show me what this query found".
            st.session_state["map_mode"] = "wells"
            st.session_state["map_mode_radio"] = "wells"
            st.session_state["wells_layer_on"] = True
            st.session_state["wells_suppressed"] = False
            st.session_state["_wells_already_loaded"] = False
            with st.spinner("Asking Claude…"):
                _spec, _err = _ai_filter_wells(_ai_q.strip(), _wells_raw,
                                           _engine=engine)
            if _spec is not None:
                st.session_state["ai_filter_spec"] = _spec
                st.session_state["ai_filter_desc"] = _spec.get("description", "")
                st.session_state.pop("ai_filter_error", None)
                st.rerun()
            else:
                # Store error — don't rerun so user can read it
                st.session_state["ai_filter_error"] = _err
        if _ai_col2.button("✕ Clear", key="wm_ai_clear",
                           use_container_width=True,
                           disabled="ai_filter_spec" not in st.session_state):
            for _k in ("ai_filter_spec", "ai_filter_desc", "ai_filter_error",
                       "ai_filter_match", "ai_filter_clauses",
                       "ai_filter_empty_fields", "ai_filter_rejected",
                       "ai_filter_sql_where", "ai_filter_no_source"):
                st.session_state.pop(_k, None)
            # Clearing has to RESTORE, not just forget. In database mode the
            # spec REPLACED the well set, so dropping it leaves the
            # replacement on screen until something reloads — which is why
            # toggling the wells layer off and on "fixed" it by hand. Ask for
            # a reload, the same way the Filter button does.
            st.session_state["_wells_already_loaded"] = False
            st.session_state["wells_suppressed"] = False
            st.session_state["wells_layer_on"] = True
            st.rerun()
        if st.session_state.get("ai_filter_error"):
            st.error(f"❌ {st.session_state['ai_filter_error']}")
        elif (st.session_state.get("ai_filter_desc")
                and st.session_state.get("ai_filter_spec")):
            st.success(f"✅ {st.session_state['ai_filter_desc']}")
            _spec_now = st.session_state.get("ai_filter_spec")
            if _spec_now:
                # NOT an expander — this whole block already lives inside the
                # "🤖 AI Well Filter" expander, and Streamlit forbids nesting
                # them. It would also be redundant disclosure: the parent is
                # already the thing you open to get here.
                st.markdown("**What the filter actually asked for**")
                st.code(_ai_spec_to_sql(_spec_now), language="sql")
                _cl = st.session_state.get("ai_filter_clauses")
                if _cl:
                    st.caption("Each condition on its own, against the loaded "
                               "wells — the one matching 0 is the one to look "
                               "at:")
                    st.dataframe(pd.DataFrame(_cl), hide_index=True,
                                 use_container_width=True)
                if st.session_state.get("ai_filter_map_empty"):
                    st.warning(
                        "There are no wells on the map to filter — Mapped "
                        "wells mode has nothing to work on. Load wells with an "
                        "Area or Query selection, or set Search to **Whole "
                        "database** and the question will go to dv_well "
                        "directly.")
                elif st.session_state.get("ai_filter_no_source"):
                    st.warning(
                        "The wells on the map arrived from a drawn selection "
                        "rather than the main wells query, so Mapped wells "
                        "mode cannot filter them. Load via an Area or Query "
                        "selection, or use Whole database.")
                # ── the dataset, when one was asked for ──────────────────
                _show = (_spec_now or {}).get("show")
                if _show in ("well_header", "wells", "well", "header"):
                    _uwis = tuple(
                        str(w.get("uwi")) for w in (_display_wells or [])
                        if w.get("uwi"))[:500]
                    if not _uwis:
                        st.info("No wells matched, so there is no header data "
                                "to show.")
                    else:
                        _hdf = _qry_well_header_rows(engine, _uwis)
                        if _hdf.empty:
                            _why = _hdf.attrs.get("error")
                            st.warning(
                                "Matched wells could not be read back from "
                                "dv_well." + (f"\n\n{_why}" if _why else ""))
                        else:
                            _all_cols = st.checkbox(
                                "All columns", key="ai_hdr_all",
                                help=f"Show every populated column "
                                     f"({len(_hdf.columns)}) instead of the "
                                     f"common ones.")
                            _view = _hdf
                            if not _all_cols:
                                _pick = [c for c in WELL_HEADER_CORE
                                         if c in _hdf.columns]
                                if _pick:
                                    _view = _hdf[_pick]
                            st.markdown(
                                f"**Well header** — {len(_view):,} well(s), "
                                f"{len(_view.columns)} of {len(_hdf.columns)} "
                                f"column(s)")
                            st.dataframe(_view, hide_index=True,
                                         use_container_width=True)
                            st.download_button(
                                "⬇ Download CSV",
                                _view.to_csv(index=False).encode(),
                                file_name="well_header.csv", mime="text/csv",
                                key="ai_hdr_dl")
                elif _show and _show in AI_HAS_TABLES:
                    _tbl, _lbl = AI_HAS_TABLES[_show]
                    _uwis = tuple(
                        str(w.get("uwi")) for w in (_display_wells or [])
                        if w.get("uwi"))[:500]
                    if not _uwis:
                        st.info(f"No wells matched, so there is no {_lbl} to "
                                f"show. Widen the filter or switch Search to "
                                f"Whole database.")
                    else:
                        _cdf = _qry_child_rows(engine, _tbl, _uwis)
                        if _cdf.empty:
                            st.warning(
                                f"{len(_uwis)} well(s) matched, but none of "
                                f"them have {_lbl} rows in {_tbl}.")
                        else:
                            st.markdown(
                                f"**{_lbl.title()}** — {len(_cdf):,} row(s) "
                                f"for {_cdf['uwi'].nunique()} well(s)")
                            st.dataframe(_cdf, hide_index=True,
                                         use_container_width=True)
                            st.download_button(
                                "⬇ Download CSV",
                                _cdf.to_csv(index=False).encode(),
                                file_name=f"{_show}.csv", mime="text/csv",
                                key=f"ai_child_dl_{_show}")
                elif _show:
                    st.warning(f"'{_show}' is not a dataset this page can "
                               f"show. Available: well_header, "
                               + ", ".join(sorted(AI_HAS_TABLES)))

                _rej = st.session_state.get("ai_filter_rejected") or []
                if _rej:
                    st.warning("Not searchable in database mode:\n\n"
                               + "\n".join("• " + r for r in _rej))
                if str(st.session_state.get("wm_ai_scope", "")).startswith("Whole"):
                    st.caption(
                        "Database mode: this ran against dv_well and replaced "
                        f"what was displayed (max {AI_DB_LIMIT:,} wells). The "
                        "current area filter does not apply.")
                else:
                    st.caption(
                        "Mapped-wells mode: applied in pandas over the wells "
                        "already on the map. No query was sent to the "
                        "database, and wells outside the loaded set cannot "
                        "match — switch Search to Whole database for those.")
                st.json(_spec_now, expanded=False)
            # Match count + empty-field diagnosis so a 0-match reads as
            # "no wells matched / no data" rather than a silent blank map.
            _match = st.session_state.get("ai_filter_match")
            if _match:
                _n, _tot = _match
                _empty = st.session_state.get("ai_filter_empty_fields") or []
                # _tot is None in DATABASE mode — there is no "of N loaded" to
                # report against, because the query replaced the loaded set
                # rather than narrowing it. Formatting None with :, is what
                # raised "unsupported format string passed to NoneType".
                _of = f" of {_tot:,}" if isinstance(_tot, int) else ""
                _where = "loaded wells" if isinstance(_tot, int) else "in the database"
                if _n == 0:
                    st.warning(f"0{_of} {_where} matched.")
                    if _empty:
                        st.caption(
                            "⚠ " + ", ".join(_empty) + " is empty for every "
                            "loaded well — there's no data to filter on. The "
                            "filter is working; the column just isn't "
                            "populated for these wells.")
                else:
                    st.caption(f"📊 {_n:,}{_of} wells match"
                               + ("" if isinstance(_tot, int)
                                  else " (database search)"))


    # ── Registered layers — FULL WIDTH, above the control/map split ─────────
    # Was inside `ctrl`, the 1-of-4 column: a file path and a five-field form
    # squeezed into ~200px. Same misjudgement as the AI filter, and the same
    # fix — a form whose main input is a Windows path wants the width, and it
    # applies to the whole map rather than to one panel.
    # ── Registered layers ───────────────────────────────────────────────
        # The picker was removed at some point and left active_shp = [], which
        # made the whole shapefile path dead: dv_spatial_layer still held the
        # registry, _load_shp_layers still read it, _add_shapefile_layer still
        # knew how to draw one, and nothing chose any. Restored here, plus the
        # register-by-path form that went with it.
    active_shp = []
    with st.expander("🗺 Registered layers", expanded=False):
        _all_layers = shp_layers or []
        if _all_layers:
            # A GRID IN A FORM, NOT A MULTISELECT. Every change to a multiselect
            # reruns the script, and a rerun REBUILDS AND RE-SERIALISES THE WHOLE
            # MAP -- so ticking four layers redrew it four times, greying the page
            # each time. A data_editor inside a form holds its edits until the
            # submit (Streamlit scar #5 in reverse: outside a form it would be
            # worse than the multiselect), so the map is rebuilt ONCE however
            # many boxes are ticked.
            _by_id = {str(_l.get("layer_id")): _l for _l in _all_layers}
            _order = sorted(_by_id, key=lambda k: (
                (_by_id[k].get("layer_category") or ""),
                (_by_id[k].get("layer_name") or "")))
            _on = set(st.session_state.get("wm_shp_on") or [])
            # KEYED TO THE FRAME'S SIGNATURE (scar #3): load or delete a layer
            # and the editor must rebuild rather than carry stale rows. crc32,
            # not hash() -- hash() is SALTED PER PROCESS, so the key would
            # differ every restart and the grid would reset for no reason.
            import zlib as _zlib
            # "v2|" IS PART OF THE SIGNATURE ON PURPOSE. Scar #3: a
            # data_editor keyed only to its ROWS carries stale widget
            # state when the COLUMNS change, so adding Fill would have
            # left a live session editing a frame that no longer exists.
            _sig = _zlib.crc32(("v2|" + "|".join(_order)).encode("utf-8"))
            with st.form("wm_shp_form"):
                _grid = st.data_editor(
                    pd.DataFrame([{
                        "Show": k in _on,
                        "Layer": _by_id[k].get("layer_name") or k,
                        "Type": _by_id[k].get("layer_type") or "",
                        "Category": _by_id[k].get("layer_category") or "",
                        "Features": _by_id[k].get("feature_count"),
                        # THE NAME, with the hex as its fallback. This is a
                        # TextColumn, NOT the SelectboxColumn it started as --
                        # that one ignored typing outright and dropped the
                        # edits it did take. Names are only readable here
                        # because _colour_hex parses them back, case and hash
                        # insensitively, and the legend below pairs each name
                        # with the hex for anyone who would rather paste one.
                        "Colour": _colour_name(
                            _by_id[k].get("style_color")),
                        # FILL FALLS BACK TO THE LINE COLOUR, which is
                        # exactly what _add_shapefile_layer does when
                        # style_fill_color is null. Showing a blank here
                        # would claim there is no fill when the map draws
                        # one.
                        "Fill": _colour_name(
                            _by_id[k].get("style_fill_color")
                            or _by_id[k].get("style_color")),
                        # A PERCENT, because 0.15 in a grid cell reads as
                        # a typo. Converted back on the way in.
                        "Fill %": int(round(float(
                            _by_id[k].get("style_fill_opacity") or 0)
                            * 100)),
                        "id": k,
                    } for k in _order]),
                    hide_index=True, use_container_width=True,
                    # ENDS "_editor" ON PURPOSE. _is_action_key excludes
                    # data editors by that suffix; without it the sub-page
                    # persist loop self-assigns the key, the assignment
                    # raises, the try/except swallows it, and the error
                    # surfaces on whatever page draws next.
                    key="wm_shp_grid_v%d_editor" % _sig,
                    column_config={
                        "Show": st.column_config.CheckboxColumn(width="small"),
                        "Layer": st.column_config.TextColumn(disabled=True),
                        "Type": st.column_config.TextColumn(disabled=True,
                                                            width="small"),
                        "Category": st.column_config.TextColumn(disabled=True,
                                                                width="small"),
                        "Features": st.column_config.NumberColumn(
                            disabled=True, format="%d", width="small"),
                        # A TEXT COLUMN, AND THIS WAS MEASURED. It was a
                        # SelectboxColumn, which loses the edit: in one submit,
                        # with a Selectbox and a Text column edited in the same
                        # batch, the grid DISPLAYED both changes and Python
                        # received only the text one -- SEL=[Brown, Brown]
                        # TXT=[Red, Brown]. The pick renders to the canvas and
                        # reaches the server late, so a form submit captures the
                        # value the cell used to have. The layer reverted to its
                        # stored colour and nothing anywhere reported a failure,
                        # because as far as Python was concerned nothing changed.
                        #
                        # Worse for the person using it: a single click on a
                        # Selectbox cell followed by typing does NOTHING AT ALL.
                        # No character is accepted, the cell keeps its value, and
                        # the only way in is double-click and pick from a
                        # thirty-item list. Typing "red" is the obvious gesture
                        # and it silently did nothing.
                        #
                        # A text cell takes what is typed, commits it on Enter,
                        # and _colour_hex resolves the name -- case-insensitively,
                        # and a bare or hashed hex too. Anything it cannot resolve
                        # is refused BY NAME below, where a refusal is visible.
                        "Colour": st.column_config.TextColumn(
                            width="medium",
                            help="Double-click to edit. Type a name from "
                                 "the list below, or paste a #rrggbb "
                                 "value — both work."),
                        "Fill": st.column_config.TextColumn(
                            width="medium",
                            help="Polygon fill. Same names and hex "
                                 "values as Colour."),
                        "Fill %": st.column_config.NumberColumn(
                            width="small", min_value=0, max_value=100,
                            step=5, format="%d",
                            help="0 draws no fill. A structure or "
                                 "facility polygon reads best around "
                                 "15-40."),
                        # The id rides along so a row maps back to its layer
                        # without trusting row ORDER, which a sort would break.
                        "id": None,
                    })
                if st.form_submit_button("✓ Apply to map", type="primary",
                                         use_container_width=True):
                    st.session_state["wm_shp_on"] = [
                        str(r["id"]) for _n, r in _grid.iterrows() if r["Show"]]
                    # COLOURS TOO, and only where they CHANGED. Writing every
                    # row on every Apply would be six UPDATEs per layer for no
                    # reason, and would stamp row_changed on layers nobody
                    # touched.
                    # EVERY OUTCOME IS STASHED, NEVER RENDERED HERE. st.rerun()
                    # two lines down RAISES, so an st.warning written in this
                    # block is destroyed before it reaches the screen. That is
                    # how a typed "red" came to revert to brown in silence: the
                    # refusal fired, explained itself, and the explanation was
                    # thrown away. Same scar as bulk_dir_loader's UWI gate.
                    _msgs = []
                    try:
                        from dataview.mapping.dv_spatial_loader import set_style
                        _re_n = 0
                        for _n, r in _grid.iterrows():
                            _k = str(r["id"])
                            _lay = _by_id.get(_k, {})
                            # ONE set_style PER LAYER, not one per field.
                            # set_style already takes None for "leave alone",
                            # and two UPDATEs to change a line and its fill
                            # would stamp row_changed twice for one edit.
                            _chg = {}
                            for _fld, _col in (("color", "Colour"),
                                               ("fill_color", "Fill")):
                                _raw = str(r.get(_col) or "").strip()
                                if not _raw:
                                    continue
                                _c = _colour_hex(_raw)
                                # FILL COMPARES AGAINST THE LINE COLOUR when
                                # style_fill_color is null, because that is
                                # what the frame showed and what the map draws
                                # (_add_shapefile_layer: fill_color or color).
                                # Comparing against "" would rewrite every
                                # unfilled layer on the very first Apply.
                                _cur = _lay.get("style_" + _fld)
                                if _fld == "fill_color" and not _cur:
                                    _cur = _lay.get("style_color")
                                if _c.upper() == str(_cur or "").upper():
                                    continue
                                # REFUSE A MALFORMED HEX rather than writing
                                # it: folium takes any string and draws
                                # nothing, which reads as "layer is broken".
                                if not _re.fullmatch(r"#[0-9A-Fa-f]{6}", _c):
                                    _msgs.append(("warning",
                                        "**%s** / %s: %r is not a colour name "
                                        "or a #rrggbb value - left unchanged."
                                        % (r["Layer"], _col, _raw)))
                                    continue
                                _chg[_fld] = _c
                            # PERCENT IN THE GRID, FRACTION IN THE TABLE.
                            # style_fill_opacity is 0-1 and Leaflet clamps
                            # above 1, so storing a typed 35 would draw every
                            # polygon SOLID -- a plausible number meaning
                            # something completely different.
                            try:
                                _fp = r.get("Fill %")
                                if _fp is not None and str(_fp).strip() != "":
                                    _fp = max(0.0, min(float(_fp), 100.0)) / 100
                                    _wasfp = float(_lay.get(
                                        "style_fill_opacity") or 0.0)
                                    if abs(_fp - _wasfp) > 0.004:
                                        _chg["fill_opacity"] = _fp
                            except (TypeError, ValueError):
                                _msgs.append(("warning",
                                    "**%s**: Fill %% must be a number from 0 "
                                    "to 100 - left unchanged." % r["Layer"]))
                            if not _chg:
                                continue
                            try:
                                _ok = set_style(engine, _k, strict=True, **_chg)
                            except Exception as _we:
                                _ok = False
                                _msgs.append(("error",
                                    "**%s**: %s" % (r["Layer"], _we)))
                            if _ok:
                                _re_n += 1
                            elif not _msgs or _msgs[-1][0] != "error":
                                _msgs.append(("error",
                                    "**%s**: no layer row matched - nothing "
                                    "was written." % r["Layer"]))
                        if _re_n:
                            # NO st.cache_data.clear() HERE. The style is
                            # read by _load_shp_layers, which is NOT cached.
                            # What the cache holds is _cached_layer_geojson --
                            # the GEOMETRY, which a colour change does not
                            # touch. Clearing it re-fetched every layer
                            # (Topography alone is 3,768 features) to refresh
                            # a hex string the next run reads anyway.
                            _msgs.append(("success",
                                "Restyled %d layer(s)." % _re_n))
                    except Exception as _se:
                        _msgs.append(("error",
                                      "Colour changes not saved: %s" % _se))
                    st.session_state["wm_shp_msgs"] = _msgs
                    st.rerun()
            # NAME = HEX, so the legend answers the only question the grid
            # raises: which hex do I paste for red. Both halves are typed:
            # _colour_hex takes the name or the hex, either case.
            st.caption(" · ".join("%s = %s" % (_cn, _ch)
                                   for _cn, _ch in MAP_COLOURS[:16]))
            # READ, DO NOT POP. pop showed each message for exactly ONE
            # render, and this page reruns on any widget -- so an error
            # explaining a refused colour flashed and was gone before it
            # could be read. The next Apply overwrites the list; until then
            # the explanation stays next to the grid it is about.
            for _lvl, _txt in (st.session_state.get("wm_shp_msgs") or []):
                getattr(st, _lvl, st.info)(_txt)
            # READ THE APPLIED SET, not the editor. The grid holds unsubmitted
            # edits; the map must draw what was last APPLIED or a half-ticked
            # grid would redraw on some other widget's rerun.
            active_shp = [_by_id[k] for k in
                          (st.session_state.get("wm_shp_on") or []) if k in _by_id]
            if active_shp:
                st.caption("Showing %d of %d layer(s)."
                           % (len(active_shp), len(_by_id)))
        else:
            st.caption("No layers registered yet.")

        st.divider()
        st.markdown("**Register a shapefile or GeoJSON**")
        # One row at full width instead of two stacked rows of thirds — the
        # path is the field that needs room and everything else is short.
        _r1, _r2, _r3, _r4, _r5 = st.columns([4, 1.6, 1.2, 0.9, 0.9])
        _new_path = _r1.text_input(
            "Path", key="wm_shp_path",
            placeholder=r"C:\GIS\Oil_Fields_USA.shp")
        _new_name = _r2.text_input("Name", key="wm_shp_name",
                                   placeholder="Oil Fields")
        _new_cat = _r3.text_input("Category", key="wm_shp_cat",
                                  placeholder="FIELD")
        _new_col = _r4.color_picker("Colour", "#4CAF50", key="wm_shp_col")
        _new_fill = _r5.checkbox("Filled", key="wm_shp_fill")
        if st.button("➕ Register layer", key="wm_shp_add",
                     disabled=not (_new_path and _new_name)):
            _ok, _msg = _register_spatial_layer(
                engine, _new_path.strip(), _new_name.strip(),
                _new_cat.strip() or None, _new_col,
                bool(_new_fill))
            if _ok:
                st.cache_data.clear()
                st.success(_msg)
                st.rerun()
            else:
                st.error(_msg)

    # The map gets the FULL page width and the controls sit under it.
    #
    # Containers reserve their position at CREATION, not at use, so creating
    # the map container first and the control container second puts the map on
    # top — while `with ctrl:` and `with mapcol:` stay exactly where they are
    # further down. That matters on a file this size: reordering ~100 lines of
    # control UI by hand risks far more than swapping two constructors, and a
    # revert is a two-line change.
    #
    # Was st.columns([1, 3]) — a quarter-width control rail beside the map.
    # Everything that needed room (AI filter, layer registration) has already
    # moved above the split, so the rail was holding two items and costing the
    # map 25% of the page.
    mapcol = st.container()
    ctrl = st.container()

    with ctrl:
        # ── Petroleum Region (optional state+county shortcut) ──────────
        # Picking a region applies a WHERE filter to wells: only those
        # whose province_state AND county fall inside the region's
        # county list pass through. Same dict used by WranglerView, so
        # the two products stay in sync on which counties count as
        # "Permian" / "Eagle Ford" / etc.
        #
        # ── Combined Region selector ────────────────────────────────
        # Petroleum plays (industry-canonical, hand-maintained) and
        # State Regions (user-defined via Region Builder) used to be
        # separate dropdowns. Same data shape, same mask logic, same
        # auto-zoom — keeping them as separate widgets duplicated UI
        # for what is conceptually one operation: "filter the wells
        # to a named geographic group." Combined into one dropdown
        # here, with 🏔 prefix for plays and 📍 prefix for state
        # regions so the source is visible at a glance.
        #
        # The combined options dict maps label → registry entry. The
        # entry is the (state, counties, center) tuple — same shape
        # from both registries — so downstream code doesn't care which
        # registry it came from.
        _combined_regions = {"— none —": (None, [], None)}
        # Petroleum plays first (industry-canonical, stable)
        for _label, _val in PETROLEUM_REGIONS.items():
            if _label == "— none —":
                continue
            _combined_regions[f"🏔 {_label}"] = _val
        # User-defined state regions second
        for _label, _val in STATE_REGIONS.items():
            if _label == "— none —":
                continue
            _combined_regions[f"📍 {_label}"] = _val

        # Region selector folded into the "Constrain to" spatial selector.
        # The widget is no longer rendered; _combined_regions stays defined
        # so the dormant region auto-zoom / mask code below remains valid,
        # and region_sel pins to the sentinel so neither path fires.
        st.session_state.pop("wm_region", None)
        region_sel = "— none —"

        # ── Region auto-zoom ────────────────────────────────────────
        # When the Region selector changes to a non-sentinel value,
        # navigate the map to that region without loading wells. Same
        # one-shot fit_bounds mechanism as the schema auto-zoom up in
        # top2. Tracks the last selection so re-selecting the same
        # region doesn't keep snapping the view back if the user has
        # panned away to look at something specific.
        _cur_region = st.session_state.get("wm_region")
        _prev_region = st.session_state.get("_wm_prev_region")
        if (_cur_region and _cur_region != "— none —"
                and _cur_region != _prev_region):
            st.session_state["_wm_prev_region"] = _cur_region
            _rentry = _combined_regions.get(_cur_region)
            # _region_zoom_target handles 2-tuple (legacy) and 3-tuple
            # (new with center) registry entries.
            _zoom_target = _region_zoom_target(_cur_region, _rentry)
            if _zoom_target:
                _clat, _clon, _czoom = _zoom_target
                # Same span-from-zoom formula as the schema auto-zoom.
                _span = max(0.5, 30.0 / (2 ** (_czoom - 4)))
                st.session_state["_drawn_bounds"] = [
                    [_clat - _span/2, _clon - _span],
                    [_clat + _span/2, _clon + _span],
                ]
                st.session_state["_drawn_bounds_oneshot"] = True

        # The dedicated Status-checkbox panel and Advanced-Filters expander
        # were removed — that filtering now lives in the Query block. Status
        # still drives marker COLOUR in the renderers. Keep _area_is_gom: the
        # drilled-GOM marker path below still keys off it.
        _area_is_gom = ("gom" in active_area.get("sources", [])
                        and "main" not in active_area.get("sources", []))

        # Map display
        # The seven data-layer checkboxes that used to live here — trajectories,
        # sticks, prod bubbles, prod heatmap, DST, formation tops, documented
        # wells — were removed at Perry's request. Only the display toggles
        # remain, so the panel is named for what it now does.
        #
        # active_db stays as an empty set rather than being deleted: the layer
        # rendering downstream reads it, and an empty set means "draw none of
        # them" without touching that code. The render blocks are now dead but
        # harmless, and can be stripped separately once the removal has proved
        # itself — deleting UI and its renderer in one pass makes a revert
        # much more work than it needs to be.
        active_db = set()
    with mapcol:
        # ── restore a saved view, BEFORE the widgets below exist ─────────
        # Go stores the request and asks for a full rerun; this is the top of
        # that rerun and the last safe moment to set these keys. See
        # _apply_map_view for why the timing is the whole point.
        _pv_req = st.session_state.pop("_place_pending", None)
        if _pv_req:
            _apply_map_view(_pv_req)

        # ── Spatial geography layer toggles ─────────────────────────────────
        # Chips above the map for the native-geography layers (dv_*.geog).
        # Each selection adds a geo_* flag to active_db, drawn by the render
        # section below via the geography_layers module. st.pills when available
        # (wraps naturally), else a 3-col checkbox fallback.
        _geo_defs = [
            ("geo_fields",     "🟩 Fields"),
            ("geo_leases",     "🟦 Leases"),
            ("geo_boundaries", "🟪 Boundaries"),
            ("geo_pipelines",  "➖ Pipelines"),
            ("geo_seismic",    "🟪 Seismic"),
            ("geo_horizons",   "〰️ Horizons"),
            ("geo_wellsym",    "● Well symbols"),
            ("geo_wellpts",    "⚫ Well points"),
            ("geo_wellpath",   "🌀 Well paths"),
            ("geo_refwells",   "🔵 Reference wells"),
            # THE RENDERERS WERE ALREADY HERE. _add_production_bubbles and
            # _add_production_heatmap survived the July cull of the seven
            # data-layer checkboxes -- the comment above active_db = set()
            # says so: "the render blocks are now dead but harmless, and can
            # be stripped separately". They were never stripped, so this is a
            # switch being reconnected, not a feature being written. Both read
            # db_* flags, so they come back the moment the chips set them.
            ("db_production",      "📈 Production bubbles"),
            ("db_production_heat", "🔥 Production heat"),
        ]
        _label_to_flag = {lbl: flag for flag, lbl in _geo_defs}
        if hasattr(st, "pills"):
            _picked = st.pills(
                "🌍 Geography layers",
                options=[lbl for _f, lbl in _geo_defs],
                selection_mode="multi",
                key="wm_geo_pills",
                label_visibility="collapsed",
            ) or []
            for _lbl in _picked:
                active_db.add(_label_to_flag[_lbl])
        else:
            _gc = st.columns(3)
            for _i, (_flag, _lbl) in enumerate(_geo_defs):
                if _gc[_i % 3].checkbox(_lbl, key=f"wm_{_flag}"):
                    active_db.add(_flag)

        # THE HEATMAP'S WEIGHT LOST ITS CONTROL, NOT ITS MEANING. The read at
        # the render site survived the July cull -- st.session_state.get(
        # "wm_db_prod_heat_wt", "BOE") -- but the widget that set it did not,
        # so the layer had one permanent setting and no way to say so. Oil
        # against gas is a real question about a field, not a preference.
        if "db_production_heat" in active_db:
            st.radio("Heatmap weight", ["BOE", "Oil", "Gas"],
                     key="wm_db_prod_heat_wt", horizontal=True,
                     help="BOE counts 6 Mcf of gas as one barrel. Intensity "
                          "is sqrt-scaled so a few giant wells do not wash "
                          "out the rest of the field.")

        # 🌀 WELL PATHS ARE COMPUTED, NOT LOADED. Nothing in any load
        # builds them: the survey stations carry md/incl/azim, and the
        # minimum-curvature geometry has to be derived from those plus the
        # surface location. The layer above only DRAWS what is stored, so
        # without this the map said "run well_path_sql apply" and left the
        # operator to find a command line.
        #
        # Offered here, beside the layer that needs it, rather than in the
        # message slot below — that is an st.empty() and the next status
        # message would overwrite the button mid-render.
        if "geo_wellpath" in active_db:
            try:
                from dataview.mapping import well_path as _wp
                from sqlalchemy import text as _t
                with engine.connect() as _pcx:
                    _stored = _pcx.execute(_t(
                        "SELECT COUNT(*) FROM dataview.dv_well_dir_srvy_hdr "
                        "WHERE PATH_GEOG IS NOT NULL")).scalar() or 0
            except Exception:
                _stored = None
            _pc1, _pc2 = st.columns([3, 1])
            if _stored == 0:
                _pc1.caption(
                    "🌀 No paths stored yet — the survey stations are loaded "
                    "but the geometry has not been computed.")
            elif _stored:
                _pc1.caption(f"🌀 {_stored:,} stored path(s). Recompute after "
                             f"loading more surveys.")
            if _pc2.button("Compute paths", key="wm_compute_paths",
                           use_container_width=True,
                           help="Minimum curvature from md/incl/azim, projected to "
                                "WGS84, generalised, and stored. Only wells whose "
                                "closure would be visible at map scale are kept — "
                                "a vertical hole is a dot and stays a marker."):
                try:
                    with st.spinner("Computing well paths…"):
                        _res, _probs = _wp.compute_paths(
                            engine, log=lambda *_a: None)
                        _drawable = [r for r in _res if r.get("drawable")]
                        _wrote = _wp.write_paths(engine, _res,
                                                 log=lambda *_a: None)
                    _mapmsg.success(
                        f"Computed {len(_res):,} survey(s): stored {_wrote:,}, "
                        f"skipped {len(_res) - len(_drawable):,} too vertical to "
                        f"show (closure under {_wp.MIN_CLOSURE_M:.0f} m)"
                        + (f", {len(_probs):,} could not be computed"
                           if _probs else "") + ".")
                    st.rerun()
                except Exception as _ce:
                    _mapmsg.error(f"Compute failed: {type(_ce).__name__}: {_ce}")

        # Safe filter — if nothing selected fall back to show all.
        # When wells_df is empty (lazy-load not yet fired), dff is also empty
        # — the grid render path handles that gracefully (it has its own data).
        counts_df = pd.DataFrame()  # populated lazily if a has_X filter is used
        if wells_df.empty:
            dff = wells_df  # empty
        else:
            # Status filtering moved to the Query block; start from all wells.
            mask = pd.Series(True, index=wells_df.index)
            # Single-value query filters (Query dropdown — attribute filters
            # only; place-based filters live in Zoom-to and apply via the
            # zoom-target mask handler further down).
            if qtype == "operator" and qvalue:
                mask &= wells_df["operator_name"] == qvalue
            elif qtype == "well_type" and qvalue:
                mask &= wells_df["well_type"] == qvalue
            elif qtype == "source" and qvalue:
                if "source" in wells_df.columns:
                    mask &= wells_df["source"] == qvalue
            elif qtype == "area" and qvalue:
                if "area" in wells_df.columns:
                    mask &= wells_df["area"] == qvalue
            elif qtype == "td_range" and qvalue and "final_td" in wells_df.columns:
                _lo, _hi = qvalue
                _tdcol = pd.to_numeric(wells_df["final_td"], errors="coerce")
                if _lo:
                    mask &= _tdcol >= _lo
                if _hi:
                    mask &= _tdcol <= _hi
            elif qtype == "spud_range" and qvalue and "spud_date" in wells_df.columns:
                _lo, _hi = qvalue
                _col = wells_df["spud_date"].fillna("").astype(str)
                if _lo:
                    mask &= (_col >= _lo) & (_col != "")
                if _hi:
                    mask &= (_col <= _hi) & (_col != "")
            elif qtype == "comp_range" and qvalue and "completion_date" in wells_df.columns:
                _lo, _hi = qvalue
                _col = wells_df["completion_date"].fillna("").astype(str)
                if _lo:
                    mask &= (_col >= _lo) & (_col != "")
                if _hi:
                    mask &= (_col <= _hi) & (_col != "")
            elif qtype in ("has_docs","has_tops","has_prod","has_dst",
                           "has_survey","has_core","has_core_photos","has_petro"):
                # SQL push-down already filtered at DB level via where_extra.
                # No in-memory filter needed.
                pass

            # ── Zoom-To filter (cascading) ──────────────────────────
            # Picking a Zoom-To target also filters wells to that target
            # (composable with Query/Status/Region). The target's
            # filter_kind tells us which column to constrain.
            #
            # The Zoom-To selectbox stores its label in session state
            # (wm_zoom_target). We look up the full target dict by label
            # to recover filter_kind and filter_value — those don't fit
            # in a selectbox value, only the label does.
            _zt_label = st.session_state.get("wm_zoom_target", "")
            if _zt_label and not _zt_label.startswith("— "):
                _zt_match = next(
                    (t for t in zoom_targets if t["label"] == _zt_label),
                    None,
                )
                if _zt_match:
                    _zt_kind  = _zt_match.get("filter_kind")
                    _zt_value = _zt_match.get("filter_value")
                    if _zt_kind == "protraction" and _zt_value:
                        # GOM: bottom_area_code lives in dataview_gom.well.
                        # When wells_df comes from the GOM loader, the
                        # column is named bottom_area_code. When wells_df
                        # comes from dv_well (after the protraction
                        # populate script), the column is protraction_area.
                        # Try both.
                        if "bottom_area_code" in wells_df.columns:
                            mask &= wells_df["bottom_area_code"] == _zt_value
                        elif "protraction_area" in wells_df.columns:
                            # Map BOEM code → friendly name via the same
                            # lookup the Zoom-To label uses. dv_well's
                            # protraction_area column stores the friendly
                            # name ("Mississippi Canyon"), not the code.
                            _zt_friendly = _boem_area_name(_zt_value)
                            mask &= wells_df["protraction_area"] == _zt_friendly
                    elif _zt_kind == "field" and _zt_value:
                        if "field_name" in wells_df.columns:
                            mask &= wells_df["field_name"] == _zt_value
                    elif _zt_kind == "basin" and _zt_value:
                        if "basin_name" in wells_df.columns:
                            mask &= wells_df["basin_name"] == _zt_value
                    elif _zt_kind == "county" and _zt_value:
                        # County is a (name, state) tuple — needs both
                        # to disambiguate (Smith County exists in TX,
                        # MS, TN, etc.).
                        _co_name, _co_state = _zt_value
                        if ("county" in wells_df.columns
                                and "province_state" in wells_df.columns):
                            mask &= (
                                (wells_df["county"] == _co_name)
                                & (wells_df["province_state"] == _co_state)
                            )

            # ── Region filter (combined Petroleum + State) ──────────
            # Reads the wm_region session state set by the selector in
            # the left panel. The dropdown options are namespaced with
            # 🏔 (petroleum play) or 📍 (state region) prefixes, but
            # the underlying registry entries are uniform — both are
            # (state, counties, center) tuples.
            #
            # If the user has picked a real region (not "— none —"),
            # narrow the mask to wells whose province_state matches
            # AND whose county is in the region's county list. NULL
            # province_state or county values fail the filter (strict
            # — wells without enough info to place in the region
            # shouldn't pass through).
            #
            # Registry entries may be 2-tuple (state, counties) or
            # 3-tuple (state, counties, center). Index access ignores
            # the center — we only need state + counties for the mask.
            _region_sel = st.session_state.get("wm_region")
            if _region_sel and _region_sel != "— none —":
                # Strip the 🏔/📍 prefix to find the underlying registry
                # entry. Try petroleum first, then state — same
                # ordering as the dropdown was built in.
                _region_entry = None
                _bare_label = _region_sel
                if _region_sel.startswith("🏔 "):
                    _bare_label = _region_sel[2:].lstrip()
                    _region_entry = PETROLEUM_REGIONS.get(_bare_label)
                elif _region_sel.startswith("📍 "):
                    _bare_label = _region_sel[2:].lstrip()
                    _region_entry = STATE_REGIONS.get(_bare_label)
                else:
                    # No prefix — try both registries (defensive,
                    # in case prefix logic changes upstream)
                    _region_entry = (
                        PETROLEUM_REGIONS.get(_region_sel)
                        or STATE_REGIONS.get(_region_sel)
                    )

                if _region_entry:
                    _rg_state = _region_entry[0]
                    _rg_counties = _region_entry[1]
                    # Defensive: wells_df may be empty (no rows AND no
                    # columns) if filters returned zero wells before
                    # reaching this point. Skip the region narrow rather
                    # than KeyError on the missing column.
                    if (_rg_state and _rg_counties
                            and "province_state" in wells_df.columns
                            and "county" in wells_df.columns):
                        mask &= (
                            (wells_df["province_state"] == _rg_state)
                            & (wells_df["county"].isin(_rg_counties))
                        )

            dff = wells_df[mask].copy()

        # Diagnostic — remove after confirming source filter works
        if qtype == "source" and qvalue:
            _src_col_exists = "source" in wells_df.columns
            _src_vals = sorted(wells_df["source"].dropna().unique().tolist()) \
                        if _src_col_exists else []
            _mapmsg.caption(
                f"🔬 Source filter debug: "
                f"col_exists={_src_col_exists} · "
                f"values_in_df={_src_vals} · "
                f"filter_value={repr(qvalue)} · "
                f"wells_df={len(wells_df)} rows · "
                f"dff={len(dff)} rows · "
                f"has_lat={dff['lat'].notna().sum() if not dff.empty else 0}"
            )
        if wells_df.empty:
            _mapmsg.caption(
                "🗺️ No well list loaded — pick a filter or draw an area to "
                "load wells, or use 🔶 H3 for a density overview"
                + (f" · {len(active_db)} DB layer(s)" if active_db else "")
                + (f" · {len(active_shp)} shapefile(s)" if active_shp else "")
            )
        else:
            _mapmsg.caption(
                f"**{len(dff)}** of **{len(wells_df)}** wells"
                + (f" · {len(active_db)} DB layer(s)" if active_db else "")
                + (f" · {len(active_shp)} shapefile(s)" if active_shp else "")
            )
            # Auto-route the current filtered result set to the object tray
            # (clicked_uwis + tray_well_data) so scout tickets / export work
            # with no manual "send" step — mirrors the rectangle/circle drill
            # behavior. Capped at _TRAY_AUTO_ADD_CAP.
            #
            # Replace semantics: the tray's *auto-added* portion always equals
            # the current result. Wells the user drilled by hand are left
            # alone (they're not in _auto_tray_uwis). Respects wells_suppressed
            # so a 🗑 Clear isn't instantly undone while the query is still up.
            # The current filter result IS the result set. Wholesale replace —
            # running a Query swaps the result set to exactly this query's
            # wells (capped), dropping any prior spatial-draw selection. A draw
            # sets wells_suppressed, which stands the auto-route down so the
            # draw owns the set until the next Query.
            if not dff.empty:
                # _wells_raw is the actual (push-down + spatial filtered) query
                # result; dff also carries shadow wells, so restrict to it.
                _raw_uwis_t = {str(w.get("uwi")) for w in _wells_raw if w.get("uwi")}
                _query_df = (dff[dff["uwi"].astype(str).isin(_raw_uwis_t)]
                             if _raw_uwis_t else dff.iloc[0:0])
                _res_n = len(_query_df)
                if not st.session_state.get("wells_suppressed", False):
                    _cap_df = _query_df.head(_TRAY_AUTO_ADD_CAP)
                    _new_uwis = [str(u) for u in _cap_df["uwi"].tolist()]
                    _shadow = {}
                    for _rec in _cap_df.to_dict("records"):
                        _u = str(_rec.get("uwi") or "")
                        if _u:
                            _shadow[_u] = _rec
                    st.session_state["clicked_uwis"] = _new_uwis
                    st.session_state["tray_well_data"] = _shadow
                    st.session_state["_auto_tray_uwis"] = _new_uwis
                    if _res_n > _TRAY_AUTO_ADD_CAP:
                        _mapmsg.caption(
                            f"📤 First {_TRAY_AUTO_ADD_CAP:,} of {_res_n:,} wells "
                            f"in Results — open Results for scout tickets / export.")
                    else:
                        _mapmsg.caption(
                            f"📤 {_res_n:,} well(s) in Results — open Results "
                            f"for scout tickets / export.")

        # ── Map mode toggle ──────────────────────────────────────────────
        # H3 mode: server-aggregated hex density (fast, overview).
        # Wells mode: individual (un-clustered) markers + rectangle viewport.
        # GeoJSON mode: pydeck viewer over pre-exported wells*.geojson files.
        # Default is Basemap (no auto-load); the Constrain-to flow drives
        # which density/well detail to show next.
        _mode_col, _mode_help = st.columns([3, 5])
        with _mode_col:
            _current_mode = st.session_state.get("map_mode", "none")
            # Deferred draw-handoff: a box/circle drill (handled far below,
            # AFTER these toggles render) can't write the toggle keys in the
            # same run, so it sets a flag and reruns. Apply it here, before
            # the toggle widgets instantiate → H3 off, Wells on.
            if st.session_state.pop("_pending_wells_handoff", False):
                st.session_state["wells_layer_on"] = True
                st.session_state["h3_layer_on"] = False
            # Same discipline for a FAILED H3 render: the render block sits
            # below these widgets and cannot switch the layer off itself, so it
            # parks a flag and this consumes it before the toggle instantiates.
            if st.session_state.pop("_pending_h3_off", False):
                st.session_state["h3_layer_on"] = False
            # Two independent layer toggles replace the old exclusive radio.
            # H3 density is the "find your area" overview; Wells flips on
            # automatically once you draw a box/circle (and H3 flips off —
            # the drawn handoff), but either can be controlled by hand here.
            # Both render blocks are independent `if`s, so turning both on
            # shows both layers at once. Pass `value=` only when the key is
            # absent (Streamlit forbids default + session value together).
            # Defaults from the same constants that seed map_mode above, so
            # the two cannot drift back apart into a forced rerun.
            _h3_kw = ({} if "h3_layer_on" in st.session_state
                      else {"value": _H3_DEFAULT_ON})
            _w_kw = ({} if "wells_layer_on" in st.session_state
                     else {"value": _WELLS_DEFAULT_ON})
            # NB: _mode_col is already a nested column (inside `mapcol`), so we
            # can't sub-divide it again — Streamlit allows only one level of
            # column nesting. Stack the two toggles vertically here instead.
            _h3_on = st.toggle("🔶 H3 density", key="h3_layer_on", **_h3_kw)
            _wells_on = st.toggle("📍 Wells", key="wells_layer_on", **_w_kw)
            # When H3 is switched back on by hand, make sure the hexes are
            # actually shown — a prior drill may have set grid_visible=False.
            if _h3_on and not st.session_state.get("_h3_layer_prev", _h3_on):
                st.session_state["grid_visible"] = True
            st.session_state["_h3_layer_prev"] = _h3_on
            # ── ON THE TRANSITION, NOT THE STEADY STATE ────────────────
            # This lifted suppression whenever the Wells toggle was ON, which
            # ran on EVERY render -- so "✗ Clear wells" set wells_suppressed
            # True and the very next render set it straight back to False.
            # Reported as "Clear wells doesn't seem to be working", and it
            # wasn't: the button worked and was undone a fraction of a second
            # later, which is indistinguishable from a dead button.
            #
            # The intent was right -- turning Wells ON should not leave the
            # layer blank because of a Clear from ten minutes ago. That is a
            # statement about the MOMENT the toggle goes on, so it is now
            # tested as an edge: lift only when wells_layer_on has just become
            # True. Leaving it on no longer re-lifts anything.
            _prev_wells_on = st.session_state.get("_prev_wells_layer_on")
            st.session_state["_prev_wells_layer_on"] = bool(_wells_on)
            if (_wells_on and not _prev_wells_on) and not (
                st.session_state.get("viewport_uwis")
                or st.session_state.get("_active_drill_bbox")
            ):
                # When a box/circle drill IS active, suppression stays ON: the
                # drill owns the view, and lifting it here would let
                # _need_wells pull the whole area's wells (e.g. Allen's ~22K)
                # even though only the drilled set is shown — which defeats the
                # point of drawing a box to subselect.
                st.session_state["wells_suppressed"] = False
            # Derive a single "primary" map_mode for the rest of the page that
            # still reads it (initial centroid, broad-scope guard, control
            # visibility). Wells wins when both are on.
            _new_mode = "wells" if _wells_on else ("h3" if _h3_on else "none")
            if _new_mode != _current_mode:
                st.session_state["map_mode"] = _new_mode
                st.session_state["map_mode_radio"] = _new_mode
                # ONE RERUN PER TARGET MODE, NEVER A SECOND. This rerun is
                # legitimate -- a drill handoff can change the toggles
                # mid-render -- but it assumed the next render would agree.
                # When something upstream keeps re-deriving the old mode the
                # two never converge and the page reruns forever: 357 renders,
                # 351 of them ending here before the first mark, with the
                # header frozen at mode=none wells=True. Reported as "now it
                # is looping".
                #
                # The guard is on the TARGET, not a counter: a genuine change
                # to a different mode still gets its one rerun, and the same
                # unresolved mismatch cannot ask twice.
                if st.session_state.get("_mode_fix_for") != _new_mode:
                    st.session_state["_mode_fix_for"] = _new_mode
                    _say("[map] mode %r -> %r, rerunning once"
                         % (_current_mode, _new_mode))
                    st.rerun()
                _say("[map] mode still %r != %r after a rerun -- carrying on "
                     "rather than looping" % (_current_mode, _new_mode))
            else:
                # Settled: let a future genuine transition rerun again.
                st.session_state.pop("_mode_fix_for", None)
            st.session_state["map_mode"] = _new_mode
        with _mode_help:
            if _broad_scope and not _uwi_filter_active:
                _mapmsg.caption(
                    # REPORTS, RATHER THAN ANNOUNCING A DECISION ALREADY MADE.
                    # The old wording ("only H3 is available") described a
                    # switch this code had just flipped for you. Wells now
                    # stays where you put it and the draw cap bounds the cost,
                    # so the caption's job is to say what the scope holds.
                    "🌎 **All states** — a broad scope. 📍 Wells draws at most "
                    "%s markers here; 🔶 H3 density aggregates every well. "
                    "Pick a state in **Constrain to** to narrow it."
                    % format(_WELLS_DRAW_CAP, ",")
                    + ("  ·  *Well count unavailable — fell back to H3.*"
                       if _broad_over_cap and not st.session_state.get(
                           "wells_layer_on") else "")
                )
            elif _new_mode == "h3":
                _mapmsg.caption(
                    "🔶 **H3 mode** — hex density grid from federation views. "
                    "Pick resolution below (R4 continent · R5 state · R6 county · R7 play). "
                    "Click hexes to select, **Commit** to drill."
                )
            elif _new_mode == "wells":
                _mapmsg.caption(
                    "📍 **Wells mode** — individual well markers + rectangle "
                    "viewport. Switch to 🔶 H3 for a fast density overview."
                )

                # ── Clear viewport — Wells mode equivalent of Grid's Clear ───
                # Grid mode has its own ✗ Clear button (in the c4 column of the
                # grid control row). Wells mode didn't, so a rectangle or
                # circle drill stuck around with no way to reset short of
                # using the trash-can icon in the Leaflet.Draw toolbar (which
                # only removes the drawn shape, not the wells it produced).
                #
                # This button mirrors the Grid clear logic: empties
                # viewport_uwis, viewport_gom_wells, processed_drawings, and
                # drops _drawn_bounds. Does NOT touch the tray (clicked_uwis)
                # — that's a persistent user selection with its own "🗑 Clear
                # Tray" button at the bottom of the page.
                # THE BUTTON MOVED OUT OF HERE, to sit beside 🎯 Reset view
                # where it is on screen in every mode. See _clear_wells_state.

        # GOM Trajectories toggle — rendered outside the 💾 Overlays
        # expander so it's always visible when GOM is active. The
        # Overlays expander is collapsed by default, making the checkbox
        # invisible until the user knows to look for it. Moving it here
        # keeps it alongside the other GOM-specific grid controls
        # (Selection mode, Show grid) that the user interacts with after
        # drilling wells. The Overlays expander version is removed to
        # avoid a duplicate key; this is now the single source of truth.
        # ── H3-mode controls (Session 3) ──────────────────────────────
        # Resolution selector + a small banner explaining the click-drill
        # interaction. The "Show grid" toggle from Grid mode is reused
        # (same grid_visible session key) so the user can hide hexes
        # without losing them.
        if _h3_on:
            _h3_c1, _h3_c2 = st.columns([4, 6])
            with _h3_c1:
                _cur_res = int(st.session_state.get("h3_resolution", 4))
                _h3_res_choice = st.select_slider(
                    "H3 Resolution",
                    options=[4, 5, 6, 7],
                    value=_cur_res,
                    help="R4: continent · R5: state · R6: county · R7: play",
                )
                if _h3_res_choice != _cur_res:
                    st.session_state["h3_resolution"] = int(_h3_res_choice)
                    # Clear stale click dedupe — a different resolution maps
                    # the same coords to a different hex.
                    st.session_state.pop("_last_h3_click", None)
                    st.rerun()
            with _h3_c2:
                _mapmsg.caption(
                    f"🔶 Rendering R{int(st.session_state.get('h3_resolution', 4))}"
                    f" hexes. Click a hex to drill its wells, or "
                    f"draw a box/circle to load wells (H3 hands off to Wells)."
                )

        # Build map — always show basemap even if no wells
        # A saved wm_basemap from a previous session could name a background
        # no longer offered; fall back to the first SHOWN one rather than a
        # hardcoded name that might itself be hidden one day.
        bm   = BASEMAPS.get(basemap) or BASEMAPS[_BASEMAPS_SHOWN[0]]

        # Center priority:
        #   1. _drawn_bounds — set by circle Haversine drill or cell Commit.
        #      This is the authoritative "show me this area" signal. It's
        #      stored as [[min_lat, min_lon], [max_lat, max_lon]] in session
        #      state and used directly without needing the wells in dff.
        #   2. viewport_uwis + dff lookup — older path for when wells came
        #      via the main wells query (used to be the rectangle workflow).
        #   3. Explicit zoom target from dropdown.
        #   4. Default: centroid of full filtered dataset.
        _viewport_bounds = None
        # Did THIS render actually move the camera? Set at the fit site below
        # and read by the view-persist JS. See the guard there.
        _did_fit = False

        # Path 1: _drawn_bounds — authoritative for circle/cell drills.
        # The handlers that set it already padded if appropriate.
        _drawn = st.session_state.get("_drawn_bounds")
        if _drawn and isinstance(_drawn, list) and len(_drawn) == 2:
            try:
                _db_min_lat = float(_drawn[0][0])
                _db_min_lon = float(_drawn[0][1])
                _db_max_lat = float(_drawn[1][0])
                _db_max_lon = float(_drawn[1][1])
                # Pad 15% so the selection isn't pressed against the map edge.
                # Smaller than the wells-in-dff path because _drawn_bounds is
                # already the circle's bbox, not a wells-extent-bbox.
                #
                # UNLESS THE EXTENT WAS CHOSEN, NOT DERIVED. A saved place is
                # a box someone drew and named; padding it 30% wider crosses
                # a zoom boundary and the place opens one level further out
                # than it was saved at. Leaflet takes the highest integer
                # zoom at which the bounds fit, so the step is all or
                # nothing -- measured z12 unpadded, z11 padded, on a 700x500
                # map with the live "Teapot" box.
                _exact = bool(st.session_state.get("_drawn_bounds_exact"))
                _pad_lat = 0.0 if _exact else max(
                    0.005, (_db_max_lat - _db_min_lat) * 0.15)
                _pad_lon = 0.0 if _exact else max(
                    0.005, (_db_max_lon - _db_min_lon) * 0.15)
                _viewport_bounds = [
                    [_db_min_lat - _pad_lat, _db_min_lon - _pad_lon],
                    [_db_max_lat + _pad_lat, _db_max_lon + _pad_lon],
                ]
                lat0 = (_db_min_lat + _db_max_lat) / 2
                lon0 = (_db_min_lon + _db_max_lon) / 2
                zoom0 = 11  # initial guess, fit_bounds will adjust precisely
            except (TypeError, ValueError, IndexError):
                _viewport_bounds = None

        # Path 2: viewport_uwis present in dff — legacy path for when wells
        # came in via the main query (full wells_df). Still useful in wells
        # mode or when the main dataset is loaded.
        if _viewport_bounds is None:
            _viewport_uwis_for_center = st.session_state.get("viewport_uwis", [])
            if _viewport_uwis_for_center and not dff.empty:
                _vp_set  = set(_viewport_uwis_for_center)
                _vp_subset = dff[dff["uwi"].astype(str).isin(_vp_set)]
                if not _vp_subset.empty:
                    _vp_lats = _vp_subset["lat"].astype(float)
                    _vp_lons = _vp_subset["lon"].astype(float)
                    _vp_min_lat, _vp_max_lat = float(_vp_lats.min()), float(_vp_lats.max())
                    _vp_min_lon, _vp_max_lon = float(_vp_lons.min()), float(_vp_lons.max())
                    # Pad bounds 30% so zoom is comfortable, not crowded.
                    _pad_lat = max(0.005, (_vp_max_lat - _vp_min_lat) * 0.3)
                    _pad_lon = max(0.005, (_vp_max_lon - _vp_min_lon) * 0.3)
                    _viewport_bounds = [
                        [_vp_min_lat - _pad_lat, _vp_min_lon - _pad_lon],
                        [_vp_max_lat + _pad_lat, _vp_max_lon + _pad_lon],
                    ]
                    lat0 = (_vp_min_lat + _vp_max_lat) / 2
                    lon0 = (_vp_min_lon + _vp_max_lon) / 2
                    zoom0 = 11

        if _viewport_bounds is None:
            # No viewport — use an explicit zoom-to, else compute a sensible
            # default centroid. We deliberately do NOT subscribe to
            # st_folium's live zoom/center to preserve pan state: that
            # subscription reruns the page on every fit_bounds and fought the
            # one-shot recenter. Keeping the recenter solid is worth a map
            # that may re-default its view after a non-recenter rerun.
            if zoom_target and zoom_target.get("lat"):
                lat0  = zoom_target["lat"]
                lon0  = zoom_target["lon"]
                zoom0 = zoom_target["zoom"]
                # Build a bounding box around the zoom target so it goes
                # through the same fit_bounds + SKIP_FLAG machinery as
                # drills. Without this, the view-persist JS restores the
                # user's last saved view and the zoom target never takes.
                # Box half-size shrinks as zoom level grows — roughly the
                # span folium shows at that zoom for a 500px-tall map.
                # The x2 widens the box ~1 zoom level further out, so a
                # Zoom-To lands with the area in context rather than
                # filling the whole viewport.
                _zt_zoom = zoom_target["zoom"]
                _zt_half = (180.0 / (2 ** _zt_zoom)) * 2.0   # degrees, rough
                _viewport_bounds = [
                    [lat0 - _zt_half, lon0 - _zt_half],
                    [lat0 + _zt_half, lon0 + _zt_half],
                ]
            elif (st.session_state.get("map_mode", "none") == "none"
                    and not st.session_state.get("_last_fit_sig")):
                # AN OPENING VIEW, NOT A RECURRING ONE. The _last_fit_sig
                # guard is the whole point: without it this fires on EVERY
                # render where no layer is on, which includes the render
                # right after "Go to" a saved place. The Go fitted the place
                # (oneshot, then popped), this re-fitted the lower 48 over the
                # top with oneshot=False so it then stuck -- reported as "Go
                # Teapot Clipped zoomed out to North America", and the log
                # showed both fits one after the other.
                #
                # _last_fit_sig is empty only before anything has been framed,
                # which is exactly "the map just opened". 🎯 Reset view pops
                # it, so returning to the country framing still works on
                # demand -- it just stops happening behind the operator.
                #
                # NOTHING IS BEING DRAWN, SO FRAME THE COUNTRY. Both layer
                # toggles start off, and the branches below would still
                # centroid-zoom onto whatever wells the schema happens to
                # hold -- so the map opened tight on Teapot Dome with no
                # wells on it, which reads as a broken view rather than an
                # empty one.
                #
                # BOUNDS, NOT A ZOOM NUMBER. zoom_start=4 at the CONUS
                # centroid frames "North America" on a wide window and
                # something else again on a narrow one -- a fixed zoom means
                # a different extent on every screen. Fitting the lower-48
                # box makes the framing the same everywhere, which is what
                # "the lower 48" actually asks for.
                # THE DATA FIRST, THE COUNTRY AS A FALLBACK.
                lat0, lon0, zoom0 = 39.5, -98.35, 4
                _viewport_bounds = _qry_data_extent(engine)
                if _viewport_bounds is None:
                    # Nothing loaded to frame: the lower-48 box, which is
                    # still the right answer for an empty database.
                    _viewport_bounds = [[24.5, -125.0], [49.4, -66.9]]
                else:
                    lat0 = (_viewport_bounds[0][0] + _viewport_bounds[1][0]) / 2
                    lon0 = (_viewport_bounds[0][1] + _viewport_bounds[1][1]) / 2
                    zoom0 = 7
                    _say("[map] opening on the data extent %.3f,%.3f .. "
                         "%.3f,%.3f" % (_viewport_bounds[0][0],
                                        _viewport_bounds[0][1],
                                        _viewport_bounds[1][0],
                                        _viewport_bounds[1][1]))
            elif not dff.empty:
                # Wells loaded — center on their centroid
                lat0  = dff["lat"].mean()
                lon0  = dff["lon"].mean()
                zoom0 = 7
            elif active_area["id"] == "none":
                # No area selected — show a neutral default view. Don't
                # query dv_well for a centroid; with no area picked, the
                # user hasn't said "I want to see anything specific,"
                # so we shouldn't auto-zoom into wherever the legacy
                # dv_well data happens to live. Zoom 3 gives a comfortable
                # USA-wide view that fits typical screens better than 4.
                lat0, lon0, zoom0 = 39.5, -98.35, 4  # CONUS centroid, lower-48 frame
            elif st.session_state.get("map_mode", "none") == "h3":
                # H3 mode shows a continental density layer — a precise
                # centroid doesn't help here, and the only way to get one
                # cheaply is to query the (uncacheable on first hit, slow
                # via pyodbc) _qry_well_grid. Use the US default and let
                # the user pan/zoom as they wish. This was previously the
                # cause of a 60+ second cold-load hit when the user picked
                # "All Schemas" + H3 — diagnosed 2026-05-27. Read from
                # session_state directly because the local _map_mode
                # variable isn't assigned until ~80 lines below this block.
                #
                # THE DATA EXTENT IS NOT THAT QUERY. The expensive thing this
                # branch avoids is _qry_well_grid; a MIN/MAX over the indexed
                # lat/lon pair is 0.047s and cached. So H3 mode opens on the
                # wells too -- "it opened the map at North America" was this
                # branch, with mode=h3, not the lower-48 one already fixed.
                lat0, lon0, zoom0 = 39.5, -98.35, 4
                if not st.session_state.get("_last_fit_sig"):
                    _de = _qry_data_extent(engine)
                    if _de:
                        _viewport_bounds = _de
                        lat0 = (_de[0][0] + _de[1][0]) / 2
                        lon0 = (_de[0][1] + _de[1][1]) / 2
                        zoom0 = 7
            elif "main" in active_area.get("sources", []):
                # An area that uses the main dv_well source IS selected.
                # Use the dv_well centroid as the initial view (cheap
                # aggregation query, falls back to US center on failure).
                try:
                    _center_grid = _qry_well_grid(engine, step=0.035)
                    if not _center_grid.empty:
                        lat0 = float(_center_grid["center_lat"].mean())
                        lon0 = float(_center_grid["center_lon"].mean())
                        zoom0 = 7
                    else:
                        lat0, lon0, zoom0 = 39.5, -98.35, 4
                except Exception:
                    lat0, lon0, zoom0 = 39.5, -98.35, 4
            else:
                # An area is selected that doesn't include "main" (e.g. GOM).
                # Use that area's registered center as the fallback. The
                # auto-zoom code normally handles this via _drawn_bounds,
                # but if that hasn't fired yet, fall back to AREAS center.
                _clat, _clon, _czoom = active_area["center"]
                lat0, lon0, zoom0 = float(_clat), float(_clon), int(_czoom)

        # Build map — show progress so user knows it's working

        _mapmsg.info(f"🗺 Building map for {len(dff):,} wells…")

        # NO prefer_canvas. THE COMMENT THAT WAS HERE WAS RIGHT AND I
        # OVERRODE IT. It said Leaflet.markercluster needs SVG children to
        # render its cluster bubbles, so canvas was removed globally. I
        # re-enabled it for H3 mode on the reasoning that H3 draws hexes and
        # no cluster -- and that is simply false: H3 mode shows the hex layer
        # ALONGSIDE marker layers (viewport selection, GOM markers, the
        # clustered well set). Canvas plus markercluster throws during map
        # init, and a Leaflet map that throws while initialising renders
        # nothing at all -- no basemap, no layers, a white rectangle.
        #
        # The canvas was never where the win came from anyway. Build and
        # serialise are PYTHON-side costs, and the 16x came from replacing
        # 14,727 folium.Polygon objects with one GeoJson layer; canvas only
        # changes how the browser paints what it is given. That change stays.
        m = folium.Map(location=[lat0, lon0], zoom_start=zoom0,
                       tiles=bm["tiles"], attr=bm["attr"],
                       max_zoom=bm.get("max_zoom", 19))

        # ── shapes saved with a place ───────────────────────────────────
        # AN OUTLINE, NOT A DRAW-TOOL SHAPE. These come back as a GeoJson
        # overlay: visible, in its own layer-control entry, and not editable.
        # Putting them back into Leaflet.Draw's own layer would make them
        # look editable and, worse, make them look DRAWN -- and a drawn shape
        # is what triggers a drill, so re-opening a place would silently
        # re-run a query the user did not ask for.
        #
        # Its own FeatureGroup so the layer control can switch it off, and
        # dashed so it reads as an annotation rather than a data layer.
        _psh = st.session_state.get("_place_shapes") or []
        if _psh:
            try:
                _pg = folium.FeatureGroup(name="✏️ Saved shapes", show=True)
                folium.GeoJson(
                    {"type": "FeatureCollection", "features": _psh},
                    style_function=lambda _f: {
                        "color": "#ffb300", "weight": 2,
                        "dashArray": "6,4", "fill": False},
                ).add_to(_pg)
                _pg.add_to(m)
            except Exception as _pse:
                # Never let a saved annotation stop the map drawing.
                print(f"[place_shapes] {_pse}")

        # Every OTHER basemap as a selectable layer, so the map's own layer
        # control offers satellite/topo/street without a rerun. Only the
        # chosen one was ever added, which is why the control listed a single
        # entry and the alternatives looked like they had been removed — they
        # were still in the 🖼 Background selector all along, one page rerun
        # away instead of one click.
        for _bm_name in _BASEMAPS_SHOWN:
            if _bm_name == basemap:
                continue
            _bm = BASEMAPS[_bm_name]
            try:
                folium.TileLayer(
                    tiles=_bm["tiles"], attr=_bm["attr"], name=_bm_name,
                    max_zoom=_bm.get("max_zoom", 19), overlay=False,
                    control=True, show=False).add_to(m)
            except Exception:
                pass

        # If we have a viewport, fit the map exactly to its bounds (overrides
        # the initial location/zoom_start with proper bbox-based zoom).
        # FIT WHEN THE TARGET CHANGES, NOT ON EVERY RENDER.
        #
        # Both paths that set _viewport_bounds keep doing so on every rerun: a
        # rectangle drill sets _drawn_bounds WITHOUT the one-shot flag ("so
        # they persist"), and Path 2 recomputes from viewport_uwis every time.
        # Since _has_active_fit turned any non-None _viewport_bounds into
        # SKIP_FLAG, the view-persist JS was told to stand down on every
        # render -- so touching ANY widget re-fit the camera to the drill and
        # discarded the zoom the user had done since. Reported as "zoomed into
        # wells, clicked the seismic pill, and it zoomed out".
        #
        # Path 1's own comment already had the principle: "Without this pop,
        # every subsequent rerun (cell clicks, layer toggles, etc.) would
        # re-fit the view, destroying any manual zoom the user did." Its pop
        # only applies to one-shot bounds, so persistent drills never got it.
        #
        # Guarding HERE rather than in each path is deliberate: there are four
        # ways to set _viewport_bounds and they would drift, which is the
        # lists-that-must-agree failure this codebase keeps paying for. One
        # site decides, and _did_fit becomes the honest answer to the only
        # question the JS is asking -- did Python move the camera this render?
        # SAY WHETHER THE CAMERA MOVED. "it is not zooming to the box"
        # cannot be answered from a log that records neither the bounds
        # nor the decision -- and there are three ways to end up not
        # fitting: no bounds at all, bounds identical to the last fit
        # (so deliberately skipped), or the JS restoring a saved view
        # over the top. Only the first two are visible from Python, and
        # neither said anything.
        if _viewport_bounds is not None:
            _fit_sig = repr([[round(float(v), 6) for v in _p]
                             for _p in _viewport_bounds])
            if st.session_state.get("_last_fit_sig") != _fit_sig:
                st.session_state["_last_fit_sig"] = _fit_sig
                m.fit_bounds(_viewport_bounds)
                _did_fit = True
                _say("[map] fit_bounds %s (oneshot=%s)"
                     % (_fit_sig, bool(st.session_state.get(
                        "_drawn_bounds_oneshot"))))
            else:
                _say("[map] fit SKIPPED: bounds unchanged since the "
                     "last fit %s" % _fit_sig)
        elif st.session_state.get("viewport_uwis") or st.session_state.get(
                "_h3_cell_uwis"):
            _say("[map] fit SKIPPED: a selection is active but no "
                 "bounds were derived (_drawn_bounds is absent)")

        # Consume one-shot _drawn_bounds. The area-change auto-zoom sets
        # _drawn_bounds_oneshot=True so the bounds fit the map ONCE on
        # area selection, then we drop them. Without this pop, every
        # subsequent rerun (cell clicks, layer toggles, etc.) would
        # re-fit the view, destroying any manual zoom the user did to
        # pick a specific cell. Drills (cell-Commit, circle) set
        # _drawn_bounds WITHOUT the oneshot flag, so they persist.
        #
        # IMPORTANT: capture whether THIS render is doing a fit BEFORE
        # we pop the bounds. The view-persist JS later reads SKIP_FLAG
        # to know if Python is doing a fit (in which case JS skips the
        # saved-view restore). If we pop the bounds before SKIP_FLAG is
        # computed, the JS thinks Python did NO fit, restores the old
        # GOM view, and the user lands in the ocean instead of West Texas.
        _is_oneshot_fit_this_render = bool(
            st.session_state.get("_drawn_bounds_oneshot")
        )
        # KEEP A COPY FOR THE LAYERS BEFORE THE CAMERA EATS IT. These bounds
        # serve two unrelated purposes: fitting the view once, and telling a
        # data layer what area to query. The oneshot pop below exists for the
        # first and destroys the second -- so an AREA change (which is
        # oneshot) left the reference-well layer unbounded and drawing the
        # whole master, while a circle drill (which is not) bounded it
        # correctly. Same layer, opposite behaviour, depending on how the user
        # arrived. Captured here, read by the layers ~470 lines below.
        # ...AND FALL BACK TO THE STANDING BOX, because _drawn_bounds is
        # ONE-SHOT. Capturing it before the pop fixes the render the box
        # ARRIVES on; every render after that, it is gone, and the layer went
        # back to querying the whole master. So "draw a box to see every well
        # in it" worked for exactly one render and then undid itself --
        # measured 29 Aug with the box plainly in the log
        #
        #   [map] clip ON -> 42.6178,-107.4727 .. 44.1507,-104.8377
        #   [map] geo layer geo_refwells  drew 48218  (capped sample, no bounds)
        #
        # -- the constraint known and announced on the same render the layer
        # ignored it. _clip_box is the durable one: set by the rectangle
        # handler and by nothing else, cleared only by ✗ Clear box. That is
        # what "anything added is constrained by the box" has to read.
        _layer_bounds = st.session_state.get("_drawn_bounds") or _clip_bounds_now()
        if _is_oneshot_fit_this_render:
            st.session_state.pop("_drawn_bounds", None)
            st.session_state.pop("_drawn_bounds_oneshot", None)
            # Goes with the bounds it describes. Left behind, it would make
            # the NEXT drill fit without its padding.
            st.session_state.pop("_drawn_bounds_exact", None)

        if bm.get("overlay"):
            folium.TileLayer(
                tiles=bm["overlay"], attr=bm["attr"],
                name="Labels", overlay=True,
                control=False, opacity=1.0,
            ).add_to(m)

        # ── REGION OUTLINE (us_geo) ──────────────────────────────────────
        # A petroleum region IS its counties — the registry defines Eagle Ford
        # as a state plus a county list, so drawing those counties draws the
        # play. Without it, "go to Eagle Ford" moves the camera to a rectangle
        # and the region itself is invisible: you see wells, or empty basemap,
        # with nothing saying where the play begins or ends.
        #
        # Distinct from the county overlay below, which follows the "Constrain
        # to" state selector. This follows the Go-to picker, and it is drawn
        # FIRST so the constrain-to highlight stays on top of it.
        if (_us_geo is not None and HAS_US_GEO and HAS_PETROLEUM_REGIONS):
            _rg_pick = st.session_state.get("wm_place_pick", "")
            _rg_key = str(_rg_pick).replace(" (wells)", "")
            _rg_val = (PETROLEUM_REGIONS or {}).get(_rg_key)
            if _rg_val and not str(_rg_key).startswith("—"):
                try:
                    _rg_st = _rg_val[0]
                    # Compare case- and space-insensitively: registries spell
                    # counties as they were typed, the Census file as it
                    # publishes them, and "DE WITT" vs "DEWITT" is the kind of
                    # mismatch that silently draws nothing.
                    _rg_co = {re.sub(r"[^A-Z]", "", str(c).upper())
                              for c in (_rg_val[1] or [])}
                    _rg_fc = _us_geo.state_feature_collection(_rg_st) if _rg_st else None
                    if _rg_fc and _rg_co:
                        _feats = [f for f in _rg_fc.get("features", [])
                                  if re.sub(r"[^A-Z]", "",
                                            str(f["properties"].get("county", "")).upper())
                                  in _rg_co]
                        if _feats:
                            folium.GeoJson(
                                {"type": "FeatureCollection", "features": _feats},
                                name=f"{_rg_key} counties",
                                style_function=lambda f: {
                                    "color": "#C77D28", "weight": 2.0,
                                    "fillColor": "#C77D28", "fillOpacity": 0.10},
                                tooltip=folium.GeoJsonTooltip(
                                    fields=["county"], aliases=["County:"]),
                                control=True,
                            ).add_to(m)
                except Exception:
                    # A missing county file or an unexpected property name must
                    # not take the map down with it — the region still works as
                    # a camera move.
                    pass

        # ── County boundary overlay (us_geo) ─────────────────────────────
        # When a state is chosen in the "Constrain to" control, draw that
        # state's county outlines for spatial context; the selected county
        # is highlighted. Gated on a state selection so we never render all
        # 3,221 US counties at once. Sits under the wells/grid layers.
        if (_us_geo is not None and HAS_US_GEO
                and active_area.get("id") in ("main", "all")):
            _bnd_state = st.session_state.get("wm_sc_state")
            _bnd_county = st.session_state.get("wm_sc_county")
            if _bnd_state and _bnd_state != "— all states —":
                _fc = _us_geo.state_feature_collection(_bnd_state)
                if _fc and _fc.get("features"):
                    _sel_co = (_bnd_county if (_bnd_county
                               and _bnd_county != "— all counties —") else None)

                    def _county_style(feat, _sel=_sel_co):
                        _is_sel = feat["properties"].get("county") == _sel
                        return {
                            "color": "#1D6FB8" if _is_sel else "#6b7785",
                            "weight": 2.5 if _is_sel else 0.8,
                            "fillColor": "#1D6FB8",
                            "fillOpacity": 0.15 if _is_sel else 0.0,
                            # AN INVISIBLE FILL IS STILL A HIT TARGET.
                            # fillOpacity 0 hides the county but Leaflet keeps
                            # hit-testing the polygon, so an unselected county
                            # blanketed the map and swallowed every hover: the
                            # reference wells are radius-2 circles, about a 4px
                            # target, and missing one by a pixel returned
                            # "County: X" instead. Reported as "the wells do
                            # not have a popup, I am only getting the county
                            # name" -- and the popup was bound the whole time.
                            #
                            # fill:false removes the fill entirely, so only the
                            # OUTLINE is interactive. Nothing changes visually
                            # because there was nothing to see. The selected
                            # county keeps its wash and stays clickable.
                            "fill": bool(_is_sel),
                        }

                    folium.GeoJson(
                        _fc,
                        name="County boundaries",
                        style_function=_county_style,
                        highlight_function=lambda f: {"weight": 2.0,
                                                      "color": "#1D6FB8"},
                        tooltip=folium.GeoJsonTooltip(
                            fields=["county"], aliases=["County:"]),
                        control=True,
                    ).add_to(m)

        # Mode dispatch — set by the radio toggle above the map.
        # "h3"    = fast aggregated hex-density overview (federation views)
        # "wells" = individual markers + viewport (full wells list)
        # H3 mode doesn't need the wells dataframe — only wells mode does.
        _map_mode = st.session_state.get("map_mode", "none")
        # Independent layer visibility — the two toggles drive rendering
        # directly (both blocks below are separate `if`s, so both layers
        # can draw at once). map_mode is kept only for the non-render logic
        # that still reads it (centroid, broad-scope guard, controls).
        # ── SAY WHAT THE CLIP RESOLVED TO, ONCE ──────────────────
        # The per-layer clip lines only print when that layer draws, so with
        # wells and hexes both off the toggle produced NO output at all and
        # was indistinguishable from one that had not been read. Reported as
        # exactly that. This runs whatever is on.
        # (the "tick Clip" hint is gone: a drawn box turns it on itself now)
        _clip_state = _clip_bounds_now()
        if st.session_state.get("wm_clip_to_box"):
            if _clip_state:
                _say("[map] clip ON -> %.4f,%.4f .. %.4f,%.4f"
                     % (_clip_state[0][0], _clip_state[0][1],
                        _clip_state[1][0], _clip_state[1][1]))
            else:
                # A CONTROL THAT CANNOT ACT MUST SAY SO. Silence here reads
                # as a broken toggle rather than a missing box.
                _say("[map] clip ON but no box drawn -- nothing to clip to")
                _mapmsg.info(
                    "🔲 **Clip to selection** is on and waiting for a "
                    "box — draw one with the rectangle tool and everything "
                    "drawn after it is constrained to that box.")
        _show_h3_layer = (not _render_held
                          and st.session_state.get("h3_layer_on", True))
        # Render the Wells block whenever the toggle is on OR a drill selection
        # is active (viewport_uwis / GOM). A drawn box sets viewport_uwis in the
        # handler that runs AFTER the toggles, then defers the toggle-on to the
        # next run; if that handoff slips a run, the selection wouldn't draw and
        # the user had to draw twice. Keying the block on the selection itself
        # makes a box's wells render on the same run they're set, regardless of
        # the toggle. The base "all wells" layer stays gated on `not
        # viewport_uwis`, so only the drilled wells show.
        _has_drill_selection = bool(
            st.session_state.get("viewport_uwis")
            or st.session_state.get("viewport_gom_wells")
        )
        # The hold sits OUTSIDE the or, deliberately: an active drill
        # selection draws the wells whatever the toggle says, which is
        # precisely why switching the toggle off does not stop a grind.
        _show_wells_layer = (
            not _render_held
            and (st.session_state.get("wells_layer_on", False)
                 or _has_drill_selection)
        )
        _skip_folium = False
        # STAMP BEFORE BUILDING, NOT AFTER. The build takes ~25 seconds, so
        # a Send pressed on the second screen DURING it changes the file
        # while this render is still assembling the previous choice.
        # Stamping afterwards recorded the NEW mtime against a map showing
        # the OLD selection, the watcher then saw nothing to do, and the
        # follow died silently -- it worked twice and stopped, which is
        # exactly how it was reported. Stamped here, a mid-render change
        # leaves the stamp behind the file and the watcher fires once more.
        # An extra rebuild is the right price; a stuck map is not.
        st.session_state["_seis_pref_seen"] = _seis_pref_mtime()

        if _show_h3_layer:
            # ── H3 hex density mode (Session 3) ─────────────────────────
            # Reads from dataview_federation.v_well_density_r{N} aggregation
            # views (pre-aggregated, small result set, no pyodbc large-pull).
            # Honors the schema dropdown via active_area sources.

            # Resolve schema_filter from active_area sources. Same dispatch
            # logic as Grid mode but mapped to schema names.
            _h3_sources = active_area.get("sources", [])
            # Cold-start gate: no sources selected → no H3 query. Mirrors
            # Grid's "main"/"gom" gate so an empty selection shows only the
            # basemap rather than firing the cross-schema density view.
            # AN EMPTY SOURCE IS A CHOICE THAT GOES NOWHERE — the same fault
            # as listing a database with no dv_well, one level down. The GOM
            # schema is wired into both federation views and currently holds
            # nothing, so offering it produces an empty map and no explanation.
            if "gom" in _h3_sources:
                try:
                    from sqlalchemy import text as _gomt
                    with engine.connect() as _gomc:
                        _gom_any = _gomc.execute(_gomt(
                            "SELECT TOP 1 1 FROM dataview_gom.well "
                            "WHERE surface_latitude IS NOT NULL")).scalar()
                except Exception:
                    _gom_any = None          # missing table counts as empty
                if not _gom_any:
                    _h3_sources = [x for x in _h3_sources if x != "gom"]

            _h3_has_sources = bool(_h3_sources)

            # ── WHICH SOURCE'S DENSITY ────────────────────────────────────
            # The density views now union a THIRD source: the ~4M-well master
            # header reference (WELL_REF), carrying dv_schema = 'well_ref'.
            # Without a way to choose, every hexagon blends loaded wells with
            # reference wells and the count means nothing anyone can act on.
            #
            # The whole mechanism was already here — _qry_h3_grid has taken a
            # schema_filter since it was written, and this dispatch maps an
            # area selection onto it. Only the reference case was missing.
            _h3_src_pick = st.selectbox(
                "Density source",
                ["Loaded wells", "Reference (4M)", "Everything"],
                key="h3_density_source",
                help="Which wells the hexagons count. The density views union "
                     "this database, the Gulf schema and the national "
                     "reference; blending them makes a count nobody can "
                     "interpret, so pick one.")
            if _h3_src_pick == "Reference (4M)":
                _h3_schema = "well_ref"
                _h3_has_sources = True      # the reference needs no area pick
            elif _h3_src_pick == "Everything":
                _h3_schema = None           # SUM every arm
                _h3_has_sources = True
            elif "main" in _h3_sources and "gom" in _h3_sources:
                # 'Loaded wells' with both areas: sum this database and the
                # Gulf, but NOT the reference — which is the point of the
                # setting, and is why this cannot simply pass None.
                _h3_schema = "dataview"
            elif "main" in _h3_sources:
                _h3_schema = "dataview"
            elif "gom" in _h3_sources:
                _h3_schema = "dataview_gom"
            else:
                # THE FALLBACK MUST MATCH WHAT THE PICKER SAYS. This was
                # None -- SUM EVERY ARM -- so an unrecognised source list
                # silently counted the 3.9M-well national reference while
                # the control still read "Loaded wells". A control that says
                # one thing while the query does another, and the cost is
                # not subtle: 276,679 cells in 4.43s against 1,405 in 0.11s,
                # a 40x difference, with 99.3% of the work coming from wells
                # nobody asked to see. Reported as "why is the gold master
                # even involved, I did not ask for it" -- and they had not.
                #
                # "Everything" is still one click away and still says so.
                _h3_schema = "dataview"

            # Resolution is whatever the H3 Resolution slider is set to.
            _h3_res = int(st.session_state.get("h3_resolution", 4))

            _show_h3 = (
                st.session_state.get("grid_visible", True)
                and _h3_has_sources
            )
            if _show_h3:
                _mapmsg.info(f"🔶 Loading H3 R{_h3_res} density…")
                try:
                    # SAY WHICH SOURCE, because the difference between
                    # them is 40x and it was invisible.
                    _say("[map] H3 R%d density source=%s"
                         % (_h3_res, _h3_schema or "ALL ARMS (4M reference "
                            "included)"))
                    _phase(15, f"🔶 Querying density view R{_h3_res}…")
                    _h3_df = _qry_h3_grid(engine, resolution=_h3_res,
                                          schema_filter=_h3_schema)
                    # Constrain the rendered hexes to the selected State/County
                    # bbox. The density view returns the whole country; without
                    # this, picking a state/county at R6/R7 tries to draw
                    # hundreds of thousands of hexes and the render chokes.
                    # We fetch the (BCP-fast) view, then keep only hexes whose
                    # center falls inside the constraint bbox — scales to any
                    # area with no giant IN-list.
                    if (
                        not _h3_df.empty
                        and active_area.get("id") in ("main", "all")
                        and HAS_US_GEO and _us_geo is not None
                    ):
                        _hst = st.session_state.get("wm_sc_state")
                        _hco = st.session_state.get("wm_sc_county")
                        if _hst and _hst != "— all states —":
                            _hcty = (_hco if (_hco and _hco != "— all counties —")
                                     else None)
                            _hbb = _us_geo.bbox(_hst, _hcty)
                            if _hbb:
                                _mnla, _mnlo, _mxla, _mxlo = _hbb
                                # ONE IMPLEMENTATION, at module level: the
                                # clip filter needs the same hardened parse,
                                # and a second copy is the parallel-worse-
                                # version failure this codebase keeps paying
                                # for. The str() coercion and the contained
                                # failure live there now.
                                _h3_center = _h3_cell_center
                                _ctr = _h3_df["h3"].map(_h3_center)
                                # NaN for a cell that could not be located: it
                                # then fails every bbox comparison and drops
                                # out, which is the honest answer -- a cell we
                                # cannot place cannot be shown to be inside.
                                _nan = float("nan")
                                _cla = _ctr.map(lambda p: p[0] if p else _nan)
                                _clo = _ctr.map(lambda p: p[1] if p else _nan)
                                _h3_df = _h3_df[
                                    (_cla >= _mnla) & (_cla <= _mxla)
                                    & (_clo >= _mnlo) & (_clo <= _mxlo)
                                ].reset_index(drop=True)
                    # CLIP AFTER the state/county constraint, not instead of
                    # it: they answer different questions and both can be on.
                    # _clip_bounds_now() returns None unless the operator
                    # asked AND an extent exists.
                    _clipb = _clip_bounds_now()
                    if _clipb is not None:
                        _n_before = len(_h3_df)
                        _h3_df = _clip_h3_df(_h3_df, _clipb)
                        _say("[map] clip: hexes %d -> %d"
                             % (_n_before, len(_h3_df)))
                    if not _h3_df.empty:
                        _phase(50, f"🔶 Rendering {len(_h3_df):,} hexes…")
                        # Selected hexes from session state — same buffer
                        # Grid mode uses (selected_cells), but for H3 the
                        # entries are h3 cell IDs as strings, not (lat,lon)
                        # tuples. We use a separate session key to keep
                        # selection state mode-clean.
                        _sel_h3 = set(st.session_state.get("selected_h3_cells", []))
                        _h3_interactive = (
                            st.session_state.get("gom_sel_mode", "Cells") == "Cells"
                        )
                        _hex_count = _add_h3_layer(
                            m, _h3_df,
                            selected_set=_sel_h3,
                            interactive=_h3_interactive,
                        )
                        _total_wells = int(_h3_df["well_count"].sum())
                        _sel_note = (f" · {len(_sel_h3)} selected"
                                     if _sel_h3 else "")
                        _mapmsg.info(
                            f"🔶 H3 R{_h3_res}: {_hex_count:,} hexes · "
                            f"{_total_wells:,} wells aggregated{_sel_note}"
                        )
                    else:
                        _mapmsg.warning(
                            f"🔶 H3 R{_h3_res}: no data. Density views "
                            f"may not exist — run "
                            f"create_v_well_density_h3.sql first."
                        )
                except Exception as _e:
                    _phase(100)
                    # THE TRACEBACK GOES TO THE LOG. "H3 render skipped:
                    # int() can't convert non-string with explicit base" named
                    # the symptom and nothing else -- there are three h3 calls
                    # in this block and the message did not say which. A
                    # discarded diagnostic is the failure mode CLAUDE.md opens
                    # with; the toast stays short, the log gets the stack.
                    import traceback as _tb
                    _say("[map] H3 render skipped: %s\n%s"
                          % (_e, _tb.format_exc()))
                    _mapmsg.warning(f"H3 render skipped: {_e}  (traceback in the log)")
                    # If H3 fails, drop the layer (safe — no heavy load); the
                    # user can re-enable it or turn on Wells for the full list.
                    #
                    # REQUEST FLAG, NOT AN ASSIGNMENT. `h3_layer_on` is a
                    # widget key and that widget was instantiated ~600 lines
                    # ABOVE this block, so writing it here raises — and it
                    # raises inside the handler that exists to recover from a
                    # failure, turning one honest warning into a second, louder
                    # error that points at session state instead of at the
                    # render. MEASURED 19 Aug: H3 hit placeholder hex values,
                    # this line fired, and the page reported "h3_layer_on
                    # cannot be modified after the widget is instantiated" —
                    # which describes nothing that was actually wrong.
                    # The flag is consumed before the toggles draw, next run,
                    # exactly as _pending_wells_handoff already is.
                    st.session_state["_pending_h3_off"] = True
            else:
                if not _h3_has_sources:
                    _mapmsg.info("🔶 Pick a schema in the dropdown above to see H3 density.")
                else:
                    _mapmsg.info("🔶 H3 hidden — toggle 'Show grid' to bring it back")

            # Drilled wells overlay (yellow markers on top of the density
            # layer). Skipped when the Wells block is active — it renders the
            # same drill selection, so rendering here too would double-draw.
            _viewport_uwis = st.session_state.get("viewport_uwis", [])
            if _viewport_uwis and not _show_wells_layer:
                try:
                    shadow = st.session_state.get("tray_well_data", {})
                    if shadow:
                        _vp_df = pd.DataFrame([
                            shadow[u] for u in _viewport_uwis
                            if u in shadow
                        ])
                        if not _vp_df.empty:
                            _vp_count = _add_viewport_wells(
                                m, _vp_df, _viewport_uwis
                            )
                            if _vp_count:
                                _mapmsg.info(
                                    f"🔶 H3 + {_vp_count:,} drilled wells"
                                )
                except Exception as _e:
                    _mapmsg.warning(f"Drilled wells render skipped: {_e}")
                    st.session_state["viewport_uwis"] = []
        if _show_wells_layer:
            _viewport_uwis = st.session_state.get("viewport_uwis", [])
            # Base layer: the current Query-filtered result set (dff). This
            # is what makes "running a Query" update the map. Skipped after a
            # Clear (wells_suppressed), when nothing is loaded, OR when a drill
            # (box/circle/cell) is active — a drawn selection owns the view, so
            # we show only its wells, not the whole constrained area.
            if (not st.session_state.get("wells_suppressed", False)
                    and not _viewport_uwis
                    and not dff.empty):
                _vp_excl = set(map(str, _viewport_uwis)) if _viewport_uwis else None
                # Base layer = THIS query's result only. dff also carries the
                # tray-shadow wells (so the picker and Excel export can see
                # them), but those must NOT leak onto the map — otherwise a
                # push-down filter like "has core photos" looks like it did
                # nothing, because wells auto-added to the tray by the previous
                # query reappear. Restrict the base layer to _wells_raw uwis.
                _raw_uwis = {str(w.get("uwi")) for w in _wells_raw if w.get("uwi")}
                _base_df = (dff[dff["uwi"].astype(str).isin(_raw_uwis)]
                            if _raw_uwis else dff.iloc[0:0])
                if not _base_df.empty:
                    # Enrich for richer tooltips: Directional/Vertical (does the
                    # well have a directional survey?) and cumulative production.
                    # Both are cached lookups keyed by uwi; wells with no match
                    # get 'Vertical' / no production.
                    try:
                        _uwi_tuple = tuple(_base_df["uwi"].astype(str).tolist())
                        _surv = _uwis_with_survey(engine, _uwi_tuple)
                        _prod = _uwi_cum_prod(engine, _uwi_tuple)
                        _ustr = _base_df["uwi"].astype(str)
                        _base_df = _base_df.copy()
                        _base_df["well_path"] = _ustr.map(
                            lambda u: "Directional" if u in _surv else "Vertical")
                        _base_df["cum_oil"] = _ustr.map(lambda u: _prod.get(u, (0, 0))[0])
                        _base_df["cum_gas"] = _ustr.map(lambda u: _prod.get(u, (0, 0))[1])
                    except Exception:
                        pass
                    _clipb = _clip_bounds_now()
                    if _clipb is not None:
                        _n_before = len(_base_df)
                        _base_df = _clip_wells_df(_base_df, _clipb)
                        _say("[map] clip: wells %d -> %d"
                             % (_n_before, len(_base_df)))
                    _add_wells(m, _base_df, exclude_uwis=_vp_excl,
                               ppdm=bool(st.session_state.get("wm_ppdm_symbols")))
                    if st.session_state.get("wm_show_legend", True):
                        _add_status_legend(
                            m, _base_df,
                            ppdm=bool(st.session_state.get("wm_ppdm_symbols")))
            if _viewport_uwis:
                try:
                    # Build a dataframe of the drilled wells from the shadow
                    # cache, falling back to dff for any that are also loaded.
                    _shadow = st.session_state.get("tray_well_data", {})
                    _vp_rows = []
                    for _u in _viewport_uwis:
                        if _u in _shadow:
                            _vp_rows.append(_shadow[_u])
                        elif not dff.empty:
                            _hit = dff[dff["uwi"] == _u]
                            if not _hit.empty:
                                _vp_rows.append(_hit.iloc[0].to_dict())
                    _vp_df = pd.DataFrame(_vp_rows) if _vp_rows else pd.DataFrame()
                    if not _vp_df.empty:
                        _vp_count = _add_viewport_wells(
                            m, _vp_df, _viewport_uwis,
                            ppdm=bool(st.session_state.get("wm_ppdm_symbols")))
                        if _vp_count:
                            _mapmsg.info(f"📍 Drilled: {_vp_count:,} wells shown")
                            # Legend for the drilled wells (the base-layer legend
                            # above doesn't run when a drill is active).
                            if st.session_state.get("wm_show_legend", True):
                                _add_status_legend(
                                    m, _vp_df,
                                    ppdm=bool(st.session_state.get("wm_ppdm_symbols")))
                    else:
                        _mapmsg.warning(
                            "📍 Drill returned wells but their coordinates "
                            "weren't found — try the drill again."
                        )
                except Exception as _e:
                    _mapmsg.warning(f"Viewport render skipped: {_e}")
                    st.session_state["viewport_uwis"] = []

        _mark("build: wells layer")
        if "db_trajectories" in active_db:
            _mapmsg.info("📐 Loading trajectories…")
            _add_trajectories(m, _qry_trajectories(engine))
        if "db_sticks" in active_db:
            _mapmsg.info("➖ Drawing surface→TD sticks…")
            _n_sticks = _add_survey_sticks(m, _qry_survey_sticks(engine))
            if _n_sticks:
                _mapmsg.info(f"➖ Drew {_n_sticks:,} surface→TD sticks…")
        if "db_formation_tops" in active_db:
            _mapmsg.info("📏 Loading formation tops…")
            _add_formation_tops(m, _qry_formation_tops(engine))
        if "db_dst" in active_db:
            _mapmsg.info("🧪 Loading DST intervals…")
            _add_dst(m, _qry_dst(engine))
        if "db_production" in active_db:
            _mapmsg.info("📈 Loading production…")
            _add_production_bubbles(m, _qry_production(engine))
        if "db_production_heat" in active_db:
            _mapmsg.info("🔥 Building production heatmap…")
            _wt = st.session_state.get("wm_db_prod_heat_wt", "BOE")
            _n_heat = _add_production_heatmap(m, _qry_production(engine), weight=_wt)
            if _n_heat:
                _mapmsg.info(f"🔥 Production heatmap: {_n_heat:,} producing wells…")
        if "db_documents" in active_db:
            _mapmsg.info("📄 Loading documented wells…")
            _n_docs = _add_documents_layer(m, _qry_well_documents(engine))
            if _n_docs:
                _mapmsg.info(f"📄 Plotted {_n_docs:,} documented well(s)")
        if "db_fields" in active_db:
            _add_fields(m, _qry_fields(engine))
        if "db_basins" in active_db:
            _add_basins(m, _qry_basins(engine))
        if "db_seismic_3d" in active_db:
            # THE SECOND SEISMIC CHIP, and it has to obey the same choice.
            # "Clear from map" gated the geography footprints and the 2D lines
            # and left THIS drawing, because 3D surveys arrive on their own
            # chip by a different path (FILE_SEIS_HEADER bboxes, not
            # dv_seis_set.geog). Clearing the seismic and still seeing seismic
            # reads as a broken button, and the button was fine -- it simply
            # did not know about this layer.
            _msc3 = _map_seis_choice()
            if _msc3["mode"] != "none":
                _df3 = _qry_seismic_3d(engine)
                # A PICK NARROWS THIS TOO. Filtering the frame keeps
                # _add_seismic_3d unchanged and means the survey names the
                # page offers are the ones that act here.
                if _msc3["mode"] == "pick" and _msc3["surveys"]:
                    _keep = set(_msc3["surveys"])
                    if "survey_name" in _df3.columns:
                        _df3 = _df3[_df3["survey_name"].astype(str).isin(_keep)]
                if not _df3.empty:
                    _mapmsg.info("🟦 Loading 3D seismic surveys…")
                    _add_seismic_3d(m, _df3)

        _mark("build: seismic 3d")
        # ── Native-geography layers (dv_*.geog) via geography_layers module ──
        _geo_keys = {"geo_fields": "fields", "geo_leases": "leases",
                     "geo_boundaries": "boundaries", "geo_pipelines": "pipelines",
                     "geo_seismic": "seismic"}
        _geo_on = [k for k in _geo_keys if k in active_db]
        _say("[map] geo chips on: %s" % (sorted(_geo_on) or "none"))
        # Defined HERE, not inside the try below: it is read ~1,200 lines
        # further down, and with no geography chip on, that block never runs.
        # A name that exists only on some paths is a NameError waiting for
        # the one render nobody tested.
        _geo_empty = []
        # DERIVED FROM THE CHIP LIST, NOT MAINTAINED BESIDE IT. The comment
        # here used to say "every geography-layer chip must appear in this
        # guard. A layer whose only trigger is missing here renders ONLY when
        # some other layer happens to be on -- which looks exactly like a
        # broken layer." It was right, and the guard was already wrong:
        # geo_refwells was never added, so ticking 🔵 Reference wells on its
        # own drew nothing, while ticking it beside 🟦 Leases worked. That
        # is the worst symptom available -- the layer looks intermittent
        # rather than unwired.
        #
        # A list that must agree with another list eventually does not; this
        # file pays that debt in four places already. _geo_defs IS the set of
        # chips, so the guard reads it and a chip added there can no longer
        # be missing here. The inner blocks each still test their own flag,
        # so a chip with no renderer costs one skipped branch.
        if any(_k in active_db for _k, _lbl in _geo_defs):
            try:
                from dataview.mapping.geography_layers import add_geography_layer, add_well_points
                # AN EMPTY LAYER LOOKS EXACTLY LIKE A BROKEN ONE. Both adders
                # return a feature count and both were being discarded, so a
                # chip for a table with no rows switched on, drew nothing, and
                # said nothing -- reported as "my leases are not displaying"
                # when dv_land_tract simply has none. On this database five of
                # the ten chips are in that state.
                _flag_to_label = {f: l for f, l in _geo_defs}
                for _ak in _geo_on:
                    _drew = 0
                    if _ak == "geo_leases":
                        # ── A FILE THE BROWSER CACHES, NOT PAYLOAD ────────
                        # Embedded, 4,618 leases put 4.8 MB of geometry into
                        # the map HTML on EVERY rerun and cost 1.5-2.3s of
                        # folium render -- the largest single item once real
                        # BLM data was loaded. Served as a static file:
                        # 0.008 MB and 0.31s, measured, and the browser keeps
                        # the file across renders.
                        #
                        # REBUILT ON A SIGNATURE, not on a timer and not on
                        # every render. Count plus newest row stamp: a rebuild
                        # that never fires shows stale leases, one that always
                        # fires is the cost being removed. Both are silent, so
                        # the rebuild says so in the log.
                        from dataview.mapping.geography_layers import (
                            add_lease_layer, add_lease_layer_file,
                            write_lease_geojson, lease_data_signature,
                            LEASE_GEOJSON_NAME)
                        _by = st.session_state.get("wm_lease_color_by",
                                                   "producing")
                        _lg_on = bool(st.session_state.get("wm_show_legend",
                                                           True))
                        # DW_MAP_LEASE_FILE=0 falls back to embedding, so a
                        # static-serving problem is one env var from a fix
                        # rather than a redeploy.
                        _use_file = os.environ.get("DW_MAP_LEASE_FILE", "1") != "0"
                        _sdir = os.path.join(os.path.dirname(os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__)))), "static")
                        _lpath = os.path.join(_sdir, LEASE_GEOJSON_NAME)
                        _drew = 0
                        if _use_file:
                            try:
                                _sig = lease_data_signature(engine)
                                if _sig and (
                                        st.session_state.get("_lease_gj_sig") != _sig
                                        or not os.path.exists(_lpath)):
                                    _lpath, _n, _lgd = write_lease_geojson(
                                        engine, _sdir)
                                    st.session_state["_lease_gj_sig"] = _sig
                                    st.session_state["_lease_gj_legend"] = _lgd
                                    st.session_state["_lease_gj_n"] = _n
                                    _say("[map] lease geojson rebuilt: %d "
                                         "feature(s)" % _n)
                                _lgd = st.session_state.get("_lease_gj_legend") or {}
                                add_lease_layer_file(
                                    m, _lpath, "/app/static/" + LEASE_GEOJSON_NAME,
                                    by=_by, show=True,
                                    legend=(_lgd.get(_by) if _lg_on else None),
                                    clip=_clip_bounds_now())
                                _drew = int(st.session_state.get("_lease_gj_n") or 0)
                            except Exception as _lfe:
                                # NOT swallowed: a discarded diagnostic makes
                                # the next failure undiagnosable, and this
                                # falls back silently otherwise.
                                _say("[map] lease file layer failed, embedding "
                                     "instead: %s" % str(_lfe)[:160])
                                _use_file = False
                        if not _use_file:
                            _drew = add_lease_layer(
                                m, engine, show=True, by=_by,
                                legend=_lg_on) or 0
                    elif _geo_keys[_ak] == "seismic":
                        # None, not an empty set, when nothing was chosen:
                        # empty means "the page asked for none" and would
                        # switch every survey off. That distinction is why
                        # the choice carries a MODE -- see _map_seis_choice.
                        _msc = _map_seis_choice()
                        if _msc["mode"] != "none":
                            _wanted = _msc["surveys"]
                            _drew = add_geography_layer(
                                m, engine, "seismic", show=True,
                                show_names=(set(_wanted) if _wanted else None)) or 0
                        else:
                            # Cleared on purpose, not empty for want of data.
                            _drew = 1
                    else:
                        _drew = add_geography_layer(m, engine, _geo_keys[_ak],
                                                    show=True) or 0
                    if not _drew:
                        _geo_empty.append(_flag_to_label.get(_ak, _ak))
                    # SAY WHAT THE LAYER DECIDED, TO THE LOG. "No leases"
                    # took four wrong theories to chase -- chip off, wrong
                    # extent, hold gate, freeze -- because the only signal
                    # was a caption that appears just for the ZERO case.
                    # A layer that drew 322 and a chip that was never
                    # ticked look identical from outside, and the timing
                    # cannot separate them either: the whole lease layer
                    # costs 0.06-0.11s, which is the same as the block
                    # measured when it is skipped entirely.
                    _say("[map] geo layer %-14s drew %s" % (_ak, _drew))
                # Real 2D line paths ride along with the Seismic pill. The
                # geog layer above holds SURVEY footprints; these are the
                # individual LINES inside them, which is the thing you
                # actually pick when you want a section.
                # Time-structure contours get their OWN chip rather than
                # riding on Seismic: a horizon is an interpretation over
                # a survey, and someone looking at line geometry does not
                # necessarily want a structure map drawn over it.
                # Status symbols, one FeatureGroup per kind so the layer
                # control doubles as the legend. Its own chip: the plain
                # point layer still serves the 50,000-well reference set,
                # where an SVG per well would be the H3 mistake again.
                if "geo_wellsym" in active_db:
                    from dataview.mapping.geography_layers import (
                        add_well_symbols)
                    add_well_symbols(m, engine, show=True)
                if "geo_horizons" in active_db:
                    from dataview.mapping.geography_layers import (
                        add_horizon_contours)
                    add_horizon_contours(m, engine, show=True)
                if "geo_seismic" in active_db:
                    # ONE GROUP PER SURVEY, so the lines can be switched at all.
                    # These were bare PolyLines added straight to the map, so
                    # they were in no FeatureGroup and the layer control never
                    # listed them: there was no way to turn one 2D survey off,
                    # only the whole Seismic chip.
                    #
                    # The line filter is keyed "survey|line", not the line name
                    # alone, because line names repeat across surveys and a bare
                    # name would switch off somebody else's line.
                    #
                    # EMPTY MEANS EVERYTHING, both times. A map whose page has
                    # never been used must look exactly as it does today.
                    _sc = _map_seis_choice()
                    _want_s = set(_sc["surveys"])
                    _want_l = set(_sc["lines"])
                    _groups = {}
                    # "none" draws nothing rather than drawing everything
                    # hidden: a FeatureGroup per survey sitting unticked in
                    # the layer control still costs the geometry in the
                    # payload, and the page asked for the map to be clear.
                    for _sl in ([] if _sc["mode"] == "none"
                                else _seismic_line_paths(engine)):
                        _sv = str(_sl.get("survey") or "(unnamed survey)")
                        # SKIPPED, NOT HIDDEN. An unselected survey used to
                        # become a FeatureGroup with show=False, which still
                        # ships every polyline in the payload -- ticking only
                        # a 3D volume sent five 2D lines to the browser to be
                        # drawn invisibly. "none" already skips outright, so
                        # hiding here was the odd one out as well as the
                        # wasteful one. The page is the control now; it can
                        # put the survey back.
                        if _want_s and _sv not in _want_s:
                            continue
                        _lk = "%s|%s" % (_sv, _sl.get("line"))
                        if _want_l and _lk not in _want_l:
                            continue
                        _svc = _survey_colour(_sv)
                        if _sv not in _groups:
                            # A SWATCH IN THE LAYER-CONTROL LABEL. Leaflet
                            # sets the label with innerHTML, so the entry
                            # can carry the colour it controls -- otherwise
                            # the control names the surveys and the map
                            # colours them and nothing joins the two.
                            _groups[_sv] = folium.FeatureGroup(
                                name=("<span style='display:inline-block;"
                                      "width:10px;height:10px;"
                                      "background:%s;border-radius:2px;"
                                      "margin-right:5px'></span>%s lines"
                                      % (_svc, _sv[:44])),
                                show=True)
                        # A 2 px LINE IS A 2 px CLICK TARGET. The popup is
                        # the only channel that tells the panel which SEG-Y
                        # was picked, so a line you cannot reliably hit is a
                        # section you cannot reliably open -- "how do I pick
                        # a line on the map" was a fair question about a
                        # target two pixels wide.
                        #
                        # So each line is drawn TWICE: a 14 px twin at 1%
                        # opacity first, carrying the same popup and tooltip,
                        # then the real 2 px line on top. Leaflet hit-tests
                        # the stroke, so the fat one catches everything
                        # within about 7 px and the map looks unchanged.
                        # Same FeatureGroup, so they switch together and no
                        # layer control entry is duplicated.
                        folium.PolyLine(
                            locations=_sl["pts"], color=_svc, weight=14,
                            # "dv-hit" marks the invisible twin so the
                            # picker can thicken the VISIBLE line without
                            # shrinking the click target to match.
                            class_name="dv-seis-2d dv-hit",
                            opacity=0.01,
                            tooltip=folium.Tooltip(
                                f"<b>📈 2D line</b><br>{_sl['survey']}<br>"
                                f"{_sl['line']}"),
                            popup=folium.Popup(
                                f"<b>📈 2D seismic line</b><br>"
                                f"<b>{_sl['survey']}</b><br>{_sl['line']}<br>"
                                f"EPSG {_sl['epsg'] or '—'}<br>"
                                f"{_sl['traces'] or '?'} traces"
                                + (f"<br><b>💾 "
                                   f"{_popup_safe(_sl['file_name'])}</b>"
                                   f"<br><span style='font-size:10px;"
                                   f"word-break:break-all'>"
                                   f"{_popup_safe(_sl['file'])}</span>"
                                   if _sl.get("file") else "")
                                # SAY WHERE THE SECTION WENT. This popup IS
                                # the successful click -- printing the path
                                # is how the file travels back to Python --
                                # but a white box appearing over the map
                                # reads as something getting IN THE WAY, and
                                # the section it just opened is off-screen
                                # below the map. Reported as "it draws a box
                                # when I click on a line", which cost an
                                # afternoon of looking for a drawing tool.
                                + "<br><span style='font-size:10px;"
                                  "color:#0f766e'>&#9660; section opening "
                                  "in the Seismic panel below the map"
                                  "</span>",
                                max_width=320),
                        ).add_to(_groups[_sv])
                        folium.PolyLine(
                            locations=_sl["pts"], color=_svc, weight=2,
                            class_name="dv-seis-2d",
                            opacity=0.9,
                            tooltip=folium.Tooltip(
                                f"<b>📈 2D line</b><br>{_sl['survey']}<br>"
                                f"{_sl['line']}"),
                            popup=folium.Popup(
                                f"<b>📈 2D seismic line</b><br>"
                                f"<b>{_sl['survey']}</b><br>{_sl['line']}<br>"
                                f"EPSG {_sl['epsg'] or '—'}<br>"
                                f"{_sl['traces'] or '?'} traces"
                                + (f"<br><b>💾 "
                                   f"{_popup_safe(_sl['file_name'])}</b>"
                                   f"<br><span style='font-size:10px;"
                                   f"word-break:break-all'>"
                                   f"{_popup_safe(_sl['file'])}</span>"
                                   if _sl.get("file") else "")
                                # SAY WHERE THE SECTION WENT. This popup IS
                                # the successful click -- printing the path
                                # is how the file travels back to Python --
                                # but a white box appearing over the map
                                # reads as something getting IN THE WAY, and
                                # the section it just opened is off-screen
                                # below the map. Reported as "it draws a box
                                # when I click on a line", which cost an
                                # afternoon of looking for a drawing tool.
                                + "<br><span style='font-size:10px;"
                                  "color:#0f766e'>&#9660; section opening "
                                  "in the Seismic panel below the map"
                                  "</span>",
                                max_width=320),
                        ).add_to(_groups[_sv])
                    for _fg in _groups.values():
                        _fg.add_to(m)
                if "geo_wellpath" in active_db:
                    # Wellbore paths are a geography layer like any other:
                    # minimum-curvature geometry computed by well_path_sql
                    # and STORED, so drawing is a SELECT. Only wells with
                    # real horizontal displacement have one — a vertical
                    # hole is a dot at map scale and stays a marker.
                    try:
                        from dataview.mapping.well_path import add_well_paths
                        _np = add_well_paths(
                            m, engine, name="🌀 Well paths (survey)")
                        _mapmsg.info(f"🌀 Drew {_np:,} wellbore path(s)…"
                                  if _np else
                                  "🌀 No stored paths — run well_path_sql "
                                  "apply to compute them.")
                    except Exception as _pe:
                        _mapmsg.warning(f"Well paths skipped: {_pe}")
                if "geo_wellpts" in active_db:
                    add_well_points(m, engine, show=True)
                if "geo_refwells" in active_db:
                    # INDIVIDUAL reference wells, which the density layer
                    # cannot give: v_well_density_r* answers "where are wells"
                    # for 3.9M rows, never "which wells are these".
                    #
                    # Bounded by _drawn_bounds when the app has set one. Python
                    # never learns about a pan, so this reads a value the app
                    # itself wrote rather than pretending to know the viewport;
                    # with no bounds it draws the cap and SAYS it is capped.
                    try:
                        from dataview.mapping.geography_layers import (
                            add_reference_wells)
                        # _layer_bounds, not session state: the camera's
                        # oneshot pop has already run by now.
                        _rb = _layer_bounds
                        _rn, _rscope = add_reference_wells(
                            m, engine, bounds=_rb, show=True)
                        # SAY THE TWO FACTS SEPARATELY. This used to print
                        # "(capped sample, no bounds)" whenever the fetch
                        # SAMPLED -- which says nothing about whether bounds
                        # were passed, and the two have different fixes. It
                        # cost a whole diagnosis: a box holding 32,991 wells,
                        # well under the 50,000 cap, drew the nationwide
                        # sample and the log called it "no bounds", leaving
                        # "was the box ignored?" and "was the box too big?"
                        # indistinguishable. A discarded diagnostic makes the
                        # next failure undiagnosable -- so name the bounds it
                        # actually received.
                        _say("[map] geo layer geo_refwells    drew %s  "
                             "(bounds=%s, %s)"
                             % (_rn,
                                ("%.4f,%.4f..%.4f,%.4f"
                                 % (_rb[0][0], _rb[0][1], _rb[1][0], _rb[1][1])
                                 if _rb else "NONE"),
                                "exact" if _rscope is not None
                                else "SAMPLED (scope over the 50,000 cap)"))
                        _mapmsg.info(
                            f"🔵 Drew {_rn:,} reference well(s)"
                            + ("" if _rscope is not None else
                               " — a spread sample across the whole view. "
                               "Draw a box or pick an area to see every well "
                               "in it."))
                    # NOT "as _re". "except ... as X" binds X as a LOCAL for
                    # the WHOLE function, so this one line shadowed the
                    # module-level "import re as _re" everywhere in run() --
                    # including the colour guard 1,400 lines above, which then
                    # raised UnboundLocalError on every Apply and wrote
                    # nothing. Python also DELETES the name when the except
                    # block ends, so it is unbound even after this runs.
                    except Exception as _refexc:
                        _mapmsg.warning(f"Reference wells skipped: {_refexc}")
            except Exception as _ge:
                _mapmsg.warning(f"Geography layers skipped: {_ge}")

        _mark("build: geography layers")
        # Phase 4: render individual GOM well markers after a Commit drill.
        # The Commit handler stashes drilled wells in viewport_gom_wells;
        # _add_gom_wells_markers renders them as amber-ring teal-fill
        # CircleMarkers with popups built by _build_gom_popup_html.
        # Independent of the grid layer — drilled markers can show alongside
        # OR without the grid heatmap depending on Show grid toggle state.
        _gom_drilled = st.session_state.get("viewport_gom_wells", [])
        if _gom_drilled:
            _phase(70, f"🛢 Rendering {len(_gom_drilled):,} drilled GOM wells…")
            _mapmsg.info(f"🛢 Rendering {len(_gom_drilled):,} drilled GOM wells…")
            _add_gom_wells_markers(m, _gom_drilled)
            # NOTE: do NOT _phase(100) here — bar persists to st_folium

            # GOM trajectories — draw wellbore survey paths for the same
            # drilled set when the overlay toggle is on. Uses the
            # status-filtered _gom_drilled list, so trajectories follow
            # the same status filter as the markers. Sidetracks have
            # their own well_id and render as their own polylines.
            if "db_gom_trajectories" in active_db:
                _mapmsg.info("🌀 Drawing GOM wellbore trajectories…")
                _n_traj = _add_gom_trajectories(m, _gom_drilled, engine)
                if _n_traj:
                    _mapmsg.info(f"🌀 Drew {_n_traj:,} GOM trajectories…")

            # Surface→TD sticks for the same drilled GOM set. Uses the
            # directional_survey_point table (surface → deepest point).
            if "db_sticks" in active_db:
                _mapmsg.info("➖ Drawing GOM surface→TD sticks…")
                _n_gsticks = _add_gom_survey_sticks(m, _gom_drilled, engine)
                if _n_gsticks:
                    _mapmsg.info(f"➖ Drew {_n_gsticks:,} GOM surface→TD sticks…")

        _mark("build: gom drilled")
        for lay in active_shp:
            _mapmsg.info(f"🗂 Loading {lay.get('layer_name','layer')}…")
            _add_shapefile_layer(m, engine, lay)

        # COLLAPSED. Pinned open, the control grew with the map: eight layers
        # of ~40 characters covered a third of the canvas, and the layer whose
        # name explained itself ("50,000 shown, capped - zoom in to see the
        # rest") was the widest thing on screen. Leaflet expands it on hover,
        # so nothing is hidden, and the names are short now because the
        # explanation belongs in the status line, not in a legend entry.
        folium.LayerControl(collapsed=True).add_to(m)

        # Draw toolbar — circle + rectangle. Both are bulk cell-selectors
        # for grid mode: drawing one selects every cell whose bbox intersects
        # the shape's bbox. In wells mode, both run the bbox-or-Haversine
        # wells query and populate the viewport. Wells drill happens at
        # Commit (grid mode) or directly (wells mode).
        from folium.plugins import Draw
        Draw(
            export=False,
            position="topleft",
            draw_options={
                "circle":       {
                    "shapeOptions": {"color": "#1d4ed8", "weight": 2},
                    "metric":       False,
                    "showRadius":   True,        # show radius while drawing
                    # repeatMode left False: with it True, the Leaflet.Draw
                    # event sequence (drawstart/drawstop) gets out of sync
                    # with the drag guard's DV_DRAW_ACTIVE flag, and the
                    # guard ends up eating the mouseup that finalizes the
                    # circle. False keeps circle completion reliable —
                    # click the toolbar icon once per circle. The mode
                    # radio still handles the cell-click vs circle gesture
                    # conflict, which was the part that actually mattered.
                    # "Sticky circle" can revisit later as its own task,
                    # with proper attention to the drag-guard interaction.
                    "repeatMode":   False,
                    # Lift Leaflet.Draw's hidden maxRadius cap. The default
                    # in some versions silently restricts circles to tens of
                    # km — we want to allow continent-scale circles, capped
                    # only by the 5,000-well Haversine return (which warns
                    # if exceeded). 5,000,000 m = 5,000 km, plenty of room.
                    "maxRadius":    5_000_000,
                    "minRadius":    100,         # 100 m minimum (sanity floor)
                    "feet":         False,       # use km, not feet
                },
                "rectangle":    {
                    "shapeOptions": {"color": "#1d4ed8", "weight": 2},
                    "repeatMode":   False,
                    # showArea True draws the area in the corner while
                    # dragging — useful feedback for sizing the rectangle.
                    "showArea":     True,
                    "metric":       False,
                },
                # ON, so an outline can follow a pool or a fault block
                # instead of being squared off to a rectangle. Same
                # shapeOptions as the others so a drawn shape reads the same
                # whichever tool made it, and repeatMode False for the same
                # reason the circle has it -- see that comment.
                "polygon":      {
                    "shapeOptions": {"color": "#1d4ed8", "weight": 2},
                    "repeatMode":   False,
                    "showArea":     True,
                    "metric":       False,
                    # allowIntersection False rejects a bow-tie while it is
                    # being drawn. A self-intersecting ring is invalid
                    # geography, and catching it at the mouse is far kinder
                    # than a MakeValid that silently changes the shape.
                    "allowIntersection": False,
                    "drawError": {"color": "#b91c1c", "timeout": 1200},
                },
                "marker":       False,
                "circlemarker": False,
                "polyline":     False,
            },
            edit_options={"edit": False, "remove": True},
        ).add_to(m)

        _mark("build: Draw control")
        # ── JS patch: distinguish pan-drag from click on cells ──────────
        # Problem: when the user click-and-drags inside a grid cell to pan
        # the map, Leaflet fires BOTH the pan-drag AND a click event on
        # the cell, which opens the cell's popup → streamlit-folium reports
        # the popup → Streamlit re-runs → cell gets toggled into selection.
        # That's unwanted: a drag is a pan, not a selection gesture.
        #
        # Fix: track the mouse position at mousedown. On mouseup, if the
        # cursor moved more than DRAG_THRESHOLD pixels, suppress the
        # subsequent click event by calling stopPropagation/preventDefault
        # before Leaflet's handler runs. Only "honest" clicks (no movement)
        # reach the cell's popup-open handler.
        #
        # Threshold of 5 pixels is the standard UI convention for distinguishing
        # click from drag — small enough that an unintended hand tremor doesn't
        # cancel a click, large enough that any deliberate pan registers.
        from branca.element import MacroElement
        from jinja2 import Template
        drag_guard = MacroElement()
        drag_guard._name = "dv_drag_click_guard"
        drag_guard._template = Template(u"""
            {% macro script(this, kwargs) %}
            (function() {
                function install() {
                    var maps = document.querySelectorAll('.leaflet-container');
                    if (!maps.length) {
                        setTimeout(install, 200);
                        return;
                    }
                    if (window.DV_DRAG_GUARD_INSTALLED) return;
                    window.DV_DRAG_GUARD_INSTALLED = true;

                    var DRAG_THRESHOLD = 5;   // pixels
                    var downX = null, downY = null, moved = false;

                    // Track whether a Leaflet.Draw tool is currently drawing.
                    // While drawing, the guard must NOT interfere — the draw
                    // tool's own mouseup→click pipeline needs to complete to
                    // finalize the shape. Hooking Leaflet's draw:drawstart /
                    // draw:drawstop events is the official way to know this.
                    window.DV_DRAW_ACTIVE = false;
                    function hookDrawEvents() {
                        // Look for any Leaflet map instance and subscribe to
                        // its draw events. We walk window for L objects and
                        // any _leaflet_id-bearing DOM elements.
                        if (typeof L === 'undefined') {
                            setTimeout(hookDrawEvents, 200);
                            return;
                        }
                        // Leaflet stashes the map instance on the container
                        // element under a non-standard property. Find it.
                        maps.forEach(function(el) {
                            // The map instance is associated via L._leaflet_id
                            // on the container's child elements. Walk
                            // window-level Leaflet map registry instead.
                            for (var k in window) {
                                try {
                                    var v = window[k];
                                    if (v && v._container === el &&
                                        typeof v.on === 'function') {
                                        v.on('draw:drawstart', function() {
                                            window.DV_DRAW_ACTIVE = true;
                                            // Also tag the map container so
                                            // CSS can suppress polygon clicks
                                            // for the duration of the draw —
                                            // without this, the GeoJson layer
                                            // (BOEM protraction, basins, etc.)
                                            // catches the click that should
                                            // have started the circle.
                                            el.classList.add('dv-draw-active');
                                        });
                                        v.on('draw:drawstop', function() {
                                            // Small delay so the finalize
                                            // mouseup/click sequence completes
                                            // before we re-enable the guard.
                                            setTimeout(function() {
                                                window.DV_DRAW_ACTIVE = false;
                                                el.classList.remove('dv-draw-active');
                                            }, 150);
                                        });
                                    }
                                } catch (e) { /* skip */ }
                            }
                        });
                    }
                    hookDrawEvents();

                    maps.forEach(function(mapEl) {
                        // Capture phase so we see mousedown/up BEFORE Leaflet does.
                        mapEl.addEventListener('mousedown', function(ev) {
                            if (ev.button !== 0) return;   // left button only
                            if (window.DV_DRAW_ACTIVE) return;  // hands off draw tool
                            downX = ev.clientX;
                            downY = ev.clientY;
                            moved = false;
                        }, true);

                        mapEl.addEventListener('mousemove', function(ev) {
                            if (downX === null) return;
                            if (window.DV_DRAW_ACTIVE) return;
                            var dx = Math.abs(ev.clientX - downX);
                            var dy = Math.abs(ev.clientY - downY);
                            if (dx > DRAG_THRESHOLD || dy > DRAG_THRESHOLD) {
                                moved = true;
                            }
                        }, true);

                        mapEl.addEventListener('mouseup', function(ev) {
                            if (window.DV_DRAW_ACTIVE) {
                                // Don't interfere with circle/shape finalize
                                downX = null; downY = null;
                                return;
                            }
                            downX = null; downY = null;
                            if (moved) {
                                // Suppress the click event that Leaflet will
                                // fire next on this mouseup. One-shot capture
                                // listener with a 100ms safety timeout.
                                var killClick = function(ce) {
                                    ce.stopPropagation();
                                    ce.preventDefault();
                                    mapEl.removeEventListener('click', killClick, true);
                                };
                                mapEl.addEventListener('click', killClick, true);
                                setTimeout(function() {
                                    mapEl.removeEventListener('click', killClick, true);
                                }, 100);
                            }
                        }, true);
                    });
                }
                install();
            })();
            {% endmacro %}
        """)
        _mark("build: drag_guard JS")
        drag_guard._parent = m
        m.add_child(drag_guard)

        # ── JS: persist map view (center+zoom) across Streamlit reruns ──
        # Saves the user's pan/zoom to sessionStorage on every moveend, and
        # restores on init. This is independent of streamlit-folium's
        # returned_objects — we communicate state via the browser, not
        # via Python. Result: clicking a cell triggers a rerun (Streamlit
        # rebuilds the map), but the JS restore puts the map right back
        # where the user was looking.
        #
        # Storage key: 'dv_map_view' in sessionStorage (per-tab, survives
        # reruns within the same tab, cleared on tab close).
        #
        # Conflict resolution: Python's m.fit_bounds() call wins over the
        # restore for active drills (circle / cell Commit). We signal this
        # via the window.DV_SKIP_VIEW_RESTORE flag — set just before
        # rendering when _drawn_bounds is in play.
        # _has_active_fit signals to the view-persist JS that Python is
        # doing a fit_bounds on THIS render, so JS should skip its saved-
        # view restore. Includes BOTH persistent drilled bounds (still in
        # session) AND the one-shot bounds we just consumed for this
        # render. Without the OR, area changes would lose their fit to
        # the JS's stale saved view from the previous area.
        # NOW IT IS THE LITERAL ANSWER. This used to OR together three proxies
        # for "Python is probably fitting" -- bounds in session, a consumed
        # one-shot, a non-None _viewport_bounds -- and every one of them stayed
        # true across reruns that fitted nothing, which is what pinned the
        # camera. The fit site above sets _did_fit only when it really called
        # fit_bounds, so the JS now stands down exactly when it should and
        # restores the user's view every other time.
        _has_active_fit = _did_fit
        _reset_saved_view = bool(st.session_state.pop("_reset_saved_view", False))
        view_persist = MacroElement()
        view_persist._name = "dv_view_persist"
        view_persist._template = Template(u"""
            {% macro script(this, kwargs) %}
            (function() {
                var SKIP_FLAG  = """ + ("true" if _has_active_fit else "false") + u""";
                var RESET_FLAG = """ + ("true" if _reset_saved_view else "false") + u""";
                var STORAGE_KEY = 'dv_map_view';

                // If user just hit Clear, wipe the saved view BEFORE any
                // restore logic runs.
                if (RESET_FLAG) {
                    try { sessionStorage.removeItem(STORAGE_KEY); }
                    catch (e) { /* silent */ }
                }

                function install() {
                    if (typeof L === 'undefined') {
                        setTimeout(install, 100);
                        return;
                    }
                    // Find the map instance — Leaflet via folium attaches it
                    // as a window-level variable. Walk the window for any L.Map.
                    var mapInst = null;
                    for (var k in window) {
                        try {
                            var v = window[k];
                            if (v && typeof v === 'object'
                                && v instanceof L.Map
                                && !v.__dv_view_persist_bound) {
                                mapInst = v;
                                break;
                            }
                        } catch (e) { /* skip */ }
                    }
                    if (!mapInst) {
                        setTimeout(install, 100);
                        return;
                    }
                    mapInst.__dv_view_persist_bound = true;

                    // RESTORE: read saved view and apply, UNLESS Python is
                    // actively fitting to a drilled selection. In that case
                    // the saved view would override the fit_bounds and the
                    // map wouldn't zoom to the new selection.
                    if (!SKIP_FLAG) {
                        try {
                            var raw = sessionStorage.getItem(STORAGE_KEY);
                            if (raw) {
                                var v = JSON.parse(raw);
                                if (v && typeof v.lat === 'number'
                                    && typeof v.lng === 'number'
                                    && typeof v.zoom === 'number') {
                                    // Defer until after Leaflet's own init.
                                    setTimeout(function() {
                                        mapInst.setView(
                                            [v.lat, v.lng], v.zoom,
                                            { animate: false }
                                        );
                                    }, 0);
                                }
                            }
                        } catch (e) {
                            // Bad JSON or storage disabled — silent fallback
                        }
                    }

                    // SAVE: on moveend (pan release, zoom release, programmatic
                    // setView), record the new view. Throttle to 200ms so a
                    // rapid pan doesn't write 50 times.
                    var saveTimer = null;
                    function saveView() {
                        if (saveTimer) clearTimeout(saveTimer);
                        saveTimer = setTimeout(function() {
                            try {
                                var c = mapInst.getCenter();
                                var z = mapInst.getZoom();
                                sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
                                    lat:  c.lat,
                                    lng:  c.lng,
                                    zoom: z
                                }));
                            } catch (e) { /* silent */ }
                        }, 200);
                    }
                    mapInst.on('moveend', saveView);
                    mapInst.on('zoomend', saveView);

                    // ── Live zoom-level badge (lightweight reimpl) ──────
                    // IMPORTANT: the earlier version used L.Control /
                    // mapInst.addControl(). That made pan/zoom noticeably
                    // laggy (confirmed 2026-05-28) — a custom Leaflet control
                    // participates in the map's layout and interferes with
                    // the pan/zoom render path on a continental view.
                    //
                    // This version is a plain absolutely-positioned <div>
                    // appended to the map's container, OUTSIDE Leaflet's
                    // control system. Leaflet doesn't track it, so it can't
                    // affect interaction performance. We update its text on
                    // zoomend only (once per zoom, never per-frame).
                    try {
                        if (!mapInst.__dv_zoom_badge) {
                            var container = mapInst.getContainer();
                            var badge = document.createElement('div');
                            badge.className = 'dv-zoom-badge';
                            badge.style.cssText =
                                'position:absolute;bottom:10px;left:10px;'
                              + 'z-index:500;'              // below popups(700)
                              + 'background:rgba(20,40,55,0.82);'
                              + 'color:#e8f0f4;font:600 12px/1 '
                              + 'system-ui,sans-serif;padding:5px 9px;'
                              + 'border-radius:6px;'
                              + 'box-shadow:0 1px 4px rgba(0,0,0,0.3);'
                              + 'user-select:none;pointer-events:none;';
                            badge.textContent = 'Zoom: ' + mapInst.getZoom();
                            container.appendChild(badge);
                            mapInst.__dv_zoom_badge = badge;
                            mapInst.on('zoomend', function() {
                                badge.textContent = 'Zoom: '
                                    + mapInst.getZoom();
                            });
                        }
                    } catch (e) { /* badge is non-critical — silent */ }

                    // If Python did a fit_bounds (drilled selection), save
                    // the resulting view too — so subsequent unrelated
                    // reruns keep the drilled view as the saved state.
                    if (SKIP_FLAG) {
                        setTimeout(saveView, 500);
                    }
                }
                install();
            })();
            {% endmacro %}
        """)
        _mark("build: view_persist JS")
        view_persist._parent = m
        m.add_child(view_persist)

        # -- Click-to-centre: walk the map without dragging -----------------
        # A REAL LEAFLET CONTROL, not a floating div. Every corner of this map
        # is already taken -- Draw toolbar top-left, LayerControl top-right,
        # zoom badge bottom-left, status legend bottom-right -- and the legend
        # has a variable height, so an absolutely-positioned button would sit
        # on top of something at some zoom. A control lets Leaflet stack it
        # under the LayerControl and inherit the standard button styling.
        #
        # ALWAYS ADDED, ARMED IN THE BROWSER. Gating this on a Streamlit
        # checkbox cost a rerun just to arm the mode, and put the switch in a
        # collapsed expander BELOW the map, where it was reported missing.
        # sessionStorage holds the armed state, so it survives a rerun the
        # same way the saved view does, and toggling it greys nothing.
        #
        # A CLICK ON SOMETHING IS NOT A CLICK ON THE MAP. Leaflet propagates
        # vector and marker clicks up to the map, so an unguarded handler would
        # recentre every time a well popup or an H3 cell was opened -- the two
        # clicks this map most depends on. DV_DRAW_ACTIVE, already maintained
        # by the drag guard, keeps it clear of the draw tools, which would
        # otherwise recentre on every vertex of a rectangle.
        click_centre = MacroElement()
        click_centre._name = "dv_click_centre"
        click_centre._template = Template(u"""
            {% macro script(this, kwargs) %}
            (function() {
                var KEY = "dv_centre_mode";
                function findMap() {
                    var el = document.querySelector(".leaflet-container");
                    if (!el) { return null; }
                    for (var k in window) {
                        try {
                            var v = window[k];
                            if (v && v._container === el &&
                                    typeof v.on === "function") {
                                return v;
                            }
                        } catch (e) { /* unreadable key, skip */ }
                    }
                    return null;
                }
                function install() {
                    if (typeof L === "undefined") {
                        setTimeout(install, 200); return;
                    }
                    var mapInst = findMap();
                    if (!mapInst) { setTimeout(install, 200); return; }
                    if (mapInst.__dv_centre_bound) { return; }
                    mapInst.__dv_centre_bound = true;

                    var armed = false;
                    try { armed = sessionStorage.getItem(KEY) === "1"; }
                    catch (e) { armed = false; }

                    var link = null;
                    function paint() {
                        if (!link) { return; }
                        link.style.background = armed ? "#f59e0b" : "#fff";
                        link.style.color = armed ? "#fff" : "#0f172a";
                        link.title = armed
                            ? "Click-to-centre is ON - click the map to move "
                              + "there. Click here to turn it off."
                            : "Click-to-centre: turn on, then click the map "
                              + "to re-centre without dragging.";
                    }

                    var Ctl = L.Control.extend({
                        options: { position: "topright" },
                        onAdd: function() {
                            var box = L.DomUtil.create(
                                "div", "leaflet-bar dv-centre-ctl");
                            link = L.DomUtil.create("a", "", box);
                            link.href = "#";
                            link.innerHTML = "&#10021;";
                            link.style.cssText =
                                "font-size:15px;line-height:26px;"
                                + "text-align:center;font-weight:700;";
                            L.DomEvent.disableClickPropagation(box);
                            L.DomEvent.on(link, "click", function(ev) {
                                L.DomEvent.preventDefault(ev);
                                armed = !armed;
                                try {
                                    sessionStorage.setItem(
                                        KEY, armed ? "1" : "0");
                                } catch (e) { /* private mode */ }
                                paint();
                            });
                            paint();
                            return box;
                        }
                    });
                    mapInst.addControl(new Ctl());

                    mapInst.on("click", function(e) {
                        if (!armed) { return; }
                        if (window.DV_DRAW_ACTIVE) { return; }
                        var t = e.originalEvent && e.originalEvent.target;
                        if (t && t.closest) {
                            if (t.closest(".leaflet-interactive") ||
                                t.closest(".leaflet-marker-icon") ||
                                t.closest(".leaflet-control")) { return; }
                        }
                        mapInst.panTo(e.latlng, { animate: true });
                    });
                }
                install();
            })();
            {% endmacro %}
        """)
        _mark("build: click_centre JS")
        click_centre._parent = m
        m.add_child(click_centre)

        # -- "Use current view": hand Python the window you are looking at --
        # PYTHON IS NEVER TOLD YOUR PAN OR YOUR ZOOM, and it must not be:
        # subscribing st_folium to bounds/center makes every pan and every
        # zoom a value change, and every value change re-serialises the whole
        # map. That is why the reference-well layer samples 4M rows instead of
        # reading your window, and why "draw a box" is the answer.
        #
        # But drawing a box by hand to mean "what I am already looking at" is
        # busywork. The browser knows the bounds; all this does is put them
        # into the channel that already carries a box -- the draw layer, which
        # st_folium reports through all_drawings and which stays subscribed
        # even under Freeze. One rerun, at a moment you chose, instead of one
        # per pan.
        #
        # NO NEW PLUMBING, deliberately. It fires the same draw:created that
        # the rectangle tool fires, so everything downstream is the code that
        # already runs: the 5-point ring test, the geometry hash that stops a
        # re-drill, _clip_box, the clip request, and the bounds every layer
        # reads. A second mechanism beside the first is the failure this
        # codebase keeps paying for.
        #
        # A REAL CONTROL, for the reason click-to-centre gives above: every
        # corner is taken and the legend's height varies, so a floating div
        # would sit on top of something at some zoom.
        use_view = MacroElement()
        use_view._name = "dv_use_view"
        use_view._template = Template(u"""
            {% macro script(this, kwargs) %}
            (function() {
                function findMap() {
                    var el = document.querySelector(".leaflet-container");
                    if (!el) { return null; }
                    for (var k in window) {
                        try {
                            var v = window[k];
                            if (v && v._container === el &&
                                    typeof v.on === "function") { return v; }
                        } catch (e) { /* unreadable key, skip */ }
                    }
                    return null;
                }
                // THE GROUP LEAFLET.DRAW WRITES TO. folium names it
                // `drawnItems`; the walk is the fallback if that ever
                // changes, because adding the rectangle anywhere else would
                // draw a box on screen that never reaches Python.
                function findGroup(mapInst) {
                    try {
                        if (typeof drawnItems !== "undefined" && drawnItems) {
                            return drawnItems;
                        }
                    } catch (e) { /* not defined */ }
                    for (var k in window) {
                        try {
                            var v = window[k];
                            if (v && v instanceof L.FeatureGroup
                                    && v._map === mapInst) { return v; }
                        } catch (e) { /* skip */ }
                    }
                    return null;
                }
                function install() {
                    if (typeof L === "undefined") {
                        setTimeout(install, 200); return;
                    }
                    var mapInst = findMap();
                    if (!mapInst) { setTimeout(install, 200); return; }
                    if (mapInst.__dv_useview_bound) { return; }
                    mapInst.__dv_useview_bound = true;

                    var Ctl = L.Control.extend({
                        options: { position: "topright" },
                        onAdd: function() {
                            var box = L.DomUtil.create(
                                "div", "leaflet-bar dv-useview-ctl");
                            var link = L.DomUtil.create("a", "", box);
                            link.href = "#";
                            link.innerHTML = "&#9974;";
                            link.title = "Use the current view as the box — "
                                + "bounds every layer to what you can see, "
                                + "the same as drawing a rectangle round it.";
                            link.style.cssText =
                                "font-size:15px;line-height:26px;"
                                + "text-align:center;font-weight:700;";
                            L.DomEvent.disableClickPropagation(box);
                            L.DomEvent.on(link, "click", function(ev) {
                                L.DomEvent.preventDefault(ev);
                                var grp = findGroup(mapInst);
                                var rect = L.rectangle(mapInst.getBounds());
                                // Added AND fired: folium's own draw:created
                                // handler adds it to the group, but only if
                                // one is registered. addLayer is keyed on the
                                // layer id, so doing both cannot double it.
                                if (grp) { grp.addLayer(rect); }
                                else { rect.addTo(mapInst); }
                                mapInst.fire("draw:created",
                                    {layer: rect, layerType: "rectangle"});
                            });
                            return box;
                        }
                    });
                    mapInst.addControl(new Ctl());
                }
                install();
            })();
            {% endmacro %}
        """)
        _mark("build: use_view JS")
        use_view._parent = m
        m.add_child(use_view)

        # -- Pick tools: 2D line, 3D survey ------------------------------
        # WHY A TOOL AND NOT JUST A CLICK. Clicking a line already opens
        # its section -- but only when no draw tool is armed, and an armed
        # rectangle wins every click and draws a box instead. That is the
        # design (DV_DRAW_ACTIVE deliberately lets a half-drawn shape
        # finish), so the answer is not to weaken it but to give picking a
        # tool of its own that TAKES the mode, sitting beside the draw
        # tools where a person already looks for a mode.
        #
        # ARMING DISARMS THE DRAW TOOL by sending Escape, which is what
        # Leaflet.Draw itself listens for -- version-agnostic, and it
        # cancels a part-drawn shape cleanly rather than leaving one
        # half-built with its handler still live.
        #
        # WHILE ARMED, ONLY THAT KIND IS CLICKABLE. Everything else gets
        # pointer-events:none, so a well marker or a county polygon under
        # the line cannot take the click -- which is the other half of why
        # picking a 2 px line was unreliable. The class comes from
        # class_name on the layers themselves, so nothing here has to
        # guess at layer names.
        #
        # NO PYTHON, NO RERUN. Arming is a CSS class on the container; the
        # pick itself still travels the existing popup path.
        pick_tools = MacroElement()
        pick_tools._name = "dv_seis_picker"
        # THE CSS IS NOT IN THIS TEMPLATE, and that is not a style choice.
        # A MacroElement added with m.add_child() only emits its `script`
        # macro -- an `html` macro reaches the page only from the figure
        # ROOT. Mine was written as {% macro html %} and silently never
        # rendered: the buttons appeared (JS builds those), the mode went
        # amber, and NONE of the pointer-events or highlight rules existed.
        # Verified by reading the iframe stylesheets back: zero matching
        # rules. It goes below with the other folium.Element CSS, which is
        # the pattern in this file that works.
        pick_tools._template = Template(u"""
            {% macro script(this, kwargs) %}
            (function() {
                function findMap() {
                    var el = document.querySelector(".leaflet-container");
                    if (!el) { return null; }
                    for (var k in window) {
                        try {
                            var v = window[k];
                            if (v && v._container === el &&
                                    typeof v.on === "function") { return v; }
                        } catch (e) { /* unreadable key */ }
                    }
                    return null;
                }
                function install() {
                    if (typeof L === "undefined") {
                        setTimeout(install, 200); return;
                    }
                    var mapInst = findMap();
                    if (!mapInst) { setTimeout(install, 200); return; }
                    if (mapInst.__dv_picker_bound) { return; }
                    mapInst.__dv_picker_bound = true;

                    // THE STYLESHEET IS INJECTED FROM HERE, because the
                    // usual route does not arrive. A folium.Element style
                    // block added with .add_to(m.get_root().html) is
                    // DROPPED by st_folium -- checked in the running app,
                    // in both the iframe and the parent document, and the
                    // rules are in neither. That is not new: the crosshair
                    // cursor and the .dv-draw-active click-through rule
                    // already in this file go the same way and have never
                    // applied. Injecting from the map's own script puts it
                    // in the document the SVG actually lives in.
                    try {
                        var st = document.createElement("style");
                        st.textContent =
  ".dv-pick-2d .leaflet-overlay-pane path:not(.dv-seis-2d),"
+ ".dv-pick-3d .leaflet-overlay-pane path:not(.dv-seis-3d)"
+ "{pointer-events:none !important;}"
+ ".dv-pick-2d .leaflet-marker-pane,"
+ ".dv-pick-3d .leaflet-marker-pane{pointer-events:none;}"
// WELLS, THE MIRROR OF THE OTHER TWO. 2D and 3D switch the marker pane
// off so a line under a well marker is reachable; nothing did the
// reverse, so a well under a seismic line or a lease polygon was the
// one thing you could not reliably click. Armed, every vector path
// stops taking clicks and only markers do.
+ ".dv-pick-wells .leaflet-overlay-pane path"
+ "{pointer-events:none !important;}"
+ ".dv-pick-wells .leaflet-marker-pane{pointer-events:auto;}"
+ ".dv-pick-2d .leaflet-overlay-pane path.dv-seis-2d:not(.dv-hit)"
+ "{stroke-width:5px !important;}"
+ ".dv-pick-3d .leaflet-overlay-pane path.dv-seis-3d"
+ "{stroke-width:4px !important;}"
+ ".dv-pick-2d.leaflet-container,.dv-pick-3d.leaflet-container,"
+ ".dv-pick-wells.leaflet-container"
+ "{cursor:pointer !important;}"
+ ".dv-picker-ctl a{font:700 11px/26px system-ui,sans-serif;"
+ "text-align:center;}"
// CROSSHAIR OVER THE MAP, POINTER OVER ANYTHING CLICKABLE. These two
// rules and the .dv-draw-active one below were written years ago into a
// folium.Element style block that st_folium discards, so neither has ever
// applied -- the map has had a default arrow cursor and the draw-through
// has never worked. They ride along here because this is the injection
// that demonstrably reaches the map document.
+ ".leaflet-container{cursor:crosshair !important;}"
+ ".leaflet-interactive{cursor:pointer !important;}"
// !important IS LOAD-BEARING: Leaflet sets cursor:grab on .leaflet-grab
// on the same element, so without it the crosshair silently loses.
// Measured: the rule loaded and the cursor was still grab.
// While a draw tool is live, let the click through the overlay paths so a
// circle can be drawn on top of a registered layer without the polygon
// catching it. The class is set by the drag guard on draw:drawstart.
+ ".dv-draw-active .leaflet-overlay-pane path"
+ "{pointer-events:none !important;}";
                        document.head.appendChild(st);
                    } catch (e) { /* no head yet */ }

                    var mode = null;
                    var links = {};
                    var box = mapInst.getContainer();

                    function disarmDraw() {
                        // CLICK "CANCEL", because that is the one that
                        // works. Measured against Leaflet 1.9.3 with the
                        // draw plugin actually loaded, arming the rectangle
                        // and then:
                        //
                        //   clicking the enabled toolbar button -> STILL ARMED
                        //   clicking the Cancel action link     -> disarmed
                        //
                        // The toolbar button looks like the obvious target
                        // and toggling it is what a person does by hand, but
                        // a synthetic click on it does not reach the handler
                        // that disables the mode. Cancel does.
                        try {
                            var acts = document.querySelectorAll(
                                ".leaflet-draw-actions a");
                            for (var i = 0; i < acts.length; i++) {
                                if (/cancel/i.test(acts[i].textContent)) {
                                    acts[i].click();
                                    break;
                                }
                            }
                        } catch (e) { /* no draw toolbar */ }
                        // AND KEYUP, NOT KEYDOWN. Leaflet.Draw binds its
                        // Escape cancel to KEYUP -- a keydown is ignored,
                        // which is why arming the picker turned the button
                        // amber and left the rectangle tool live, so every
                        // click still drew a box. A draw handler works at
                        // the map container, so no amount of
                        // pointer-events on the paths can stop it: the
                        // tool has to actually be put away.
                        try {
                            document.dispatchEvent(new KeyboardEvent(
                                "keyup", {key: "Escape", keyCode: 27,
                                          which: 27, bubbles: true}));
                        } catch (e) { /* older browser */ }
                        window.DV_DRAW_ACTIVE = false;
                    }
                    // AND HOLD THE MODE. If a draw tool is armed while a
                    // picker is on, the draw wins the very next click and
                    // the amber button becomes a lie. Cancel it as it
                    // starts and leave the picker armed.
                    mapInst.on("draw:drawstart", function() {
                        if (mode) { setTimeout(disarmDraw, 0); }
                    });
                    function paint() {
                        box.classList.remove("dv-pick-2d", "dv-pick-3d",
                                             "dv-pick-wells");
                        if (mode) { box.classList.add("dv-pick-" + mode); }
                        ["2d", "3d", "wells"].forEach(function(k) {
                            var a = links[k];
                            if (!a) { return; }
                            a.style.background =
                                (mode === k) ? "#f59e0b" : "#fff";
                            a.style.color =
                                (mode === k) ? "#fff" : "#0f172a";
                        });
                    }
                    function arm(k) {
                        mode = (mode === k) ? null : k;
                        if (mode) { disarmDraw(); }
                        paint();
                    }

                    var Ctl = L.Control.extend({
                        options: { position: "topright" },
                        onAdd: function() {
                            var c = L.DomUtil.create(
                                "div", "leaflet-bar dv-picker-ctl");
                            // ORDER IS 3D, 2D, WELLS -- coarsest to finest,
                            // and it reads as one family of "what am I
                            // picking" rather than two seismic buttons with
                            // something else bolted on.
                            [["3d", "3D", "Pick a 3D survey: click here, "
                                    + "then click a survey footprint."],
                             ["2d", "2D", "Pick a 2D seismic line: click "
                                    + "here, then click a line to open its "
                                    + "section below the map."],
                             ["wells", "Wells", "Pick wells: click here, "
                                    + "then click a well. Seismic lines, "
                                    + "leases and boundaries stop taking "
                                    + "clicks so the well underneath them "
                                    + "is reachable."]
                            ].forEach(function(spec) {
                                var a = L.DomUtil.create("a", "", c);
                                a.href = "#";
                                a.innerHTML = spec[1];
                                a.title = spec[2];
                                links[spec[0]] = a;
                                L.DomEvent.on(a, "click", function(ev) {
                                    L.DomEvent.preventDefault(ev);
                                    arm(spec[0]);
                                });
                            });
                            L.DomEvent.disableClickPropagation(c);
                            paint();
                            return c;
                        }
                    });
                    mapInst.addControl(new Ctl());

                    // THE MODE SURVIVES A PICK, so several lines can be
                    // collected in a row -- that is the point of a mode.
                    //
                    // Python cannot see this mode -- it lives in the
                    // browser precisely so arming costs no rerun -- so it
                    // cannot know whether to add or replace. It ALWAYS
                    // adds, and the panel offers Clear. That way the two
                    // halves never disagree about what is selected.

                    // CLICK, CLICK, CLICK -- NO POPUP IN THE WAY. The popup
                    // cannot simply be removed: streamlit-folium reports
                    // last_object_clicked_popup, the popup TEXT, and that
                    // is the only channel carrying which SEG-Y was hit. It
                    // also has a second job -- a click WITHOUT popup text
                    // falls through to the grid-cell toggler, so a silent
                    // line would start selecting H3 cells underneath it.
                    //
                    // So it still opens, and closes again in the same
                    // frame. Leaflet has already written the content by
                    // popupopen, which is what streamlit-folium reads.
                    // Only while a picker is ARMED: outside that mode a
                    // popup is the useful thing a click gives you.
                    mapInst.on("popupopen", function(e) {
                        if (!mode) { return; }
                        setTimeout(function() {
                            try { mapInst.closePopup(e.popup); }
                            catch (err) { /* already gone */ }
                        }, 0);
                    });
                }
                install();
            })();
            {% endmacro %}
        """)
        _mark("build: pick_tools JS")
        pick_tools._parent = m
        m.add_child(pick_tools)


        # THESE RULES NOW LIVE IN dv_seis_picker'S INJECTED STYLESHEET.
        # Everything in this Element is DEAD: st_folium drops a style block
        # added with add_to(m.get_root().html), verified by reading the
        # running app back -- the rules appear in neither the map iframe nor
        # the parent document. So the crosshair cursor and the draw-through
        # below have never once applied, and the comments describing their
        # behaviour describe something that never happened.
        #
        # Kept, commented, for the reasoning in it -- which is sound and was
        # worth writing down. Delete the pair together if this is ever
        # tidied; do not "restore" it, it does not work.
        _DEAD_MAP_CSS = ("""
            <style>
            .leaflet-container       { cursor: crosshair !important; }
            .leaflet-interactive     { cursor: pointer   !important; }

            /* While Leaflet.Draw is active (class set by the draw-event
               hook in dv_drag_click_guard), make GeoJson overlay paths
               click-through so the user can draw a circle on top of
               registered spatial layers (BOEM protraction, basins,
               leases, etc.) without the polygon catching the click.

               .leaflet-overlay-pane contains all SVG-rendered vector
               overlays — that includes both registered layers AND the
               draw-tool's own preview shape. The draw tool keeps its
               edit handles in a separate pane (.leaflet-marker-pane and
               its own .leaflet-draw-* layers), so suppressing this pane
               doesn't break draw-tool interactivity.

               Marker layers (wells, grid cells) live in
               .leaflet-marker-pane — unaffected by this rule. They
               continue to respond normally; the drag-guard handles the
               click-vs-pan distinction for them. */
            .dv-draw-active .leaflet-overlay-pane path {
                pointer-events: none !important;
            }

            /* -- 2D / 3D picker modes (dv_seis_picker) ---------------- */
            /* While a picker is armed only that kind of feature takes a
               click, so a well marker or a county polygon lying over a
               line cannot steal it -- the half of "a line is hard to hit"
               that no amount of aiming fixes. */
            .dv-pick-2d .leaflet-overlay-pane path:not(.dv-seis-2d),
            .dv-pick-3d .leaflet-overlay-pane path:not(.dv-seis-3d) {
                pointer-events: none !important;
            }
            .dv-pick-2d .leaflet-marker-pane,
            .dv-pick-3d .leaflet-marker-pane { pointer-events: none; }
            /* The VISIBLE line thickens; the invisible 14 px hit twin is
               excluded by dv-hit, or highlighting the line would shrink
               the target underneath it. */
            .dv-pick-2d .leaflet-overlay-pane path.dv-seis-2d:not(.dv-hit) {
                stroke-width: 5px !important;
            }
            .dv-pick-3d .leaflet-overlay-pane path.dv-seis-3d {
                stroke-width: 4px !important;
            }
            .dv-pick-2d.leaflet-container,
            .dv-pick-3d.leaflet-container { cursor: pointer !important; }
            .dv-picker-ctl a { font: 700 11px/26px system-ui, sans-serif;
                text-align: center; }
            </style>
        """)   # NOT rendered: see _DEAD_MAP_CSS above.

        _mark("build: dead-map CSS")
        # st_folium with width="100%" reserves more vertical space than the map
        # actually uses — the iframe wrapper grows tall while the map stays 500px.
        # Cap the wrapper to exactly the map height to stop the column-stretching bug.
        st.markdown("""
            <style>
            iframe { display:block !important; margin:0 !important; padding:0 !important; }
            /* Hard-cap st_folium iframe wrapper to its requested height.

               NOT iframe[srcdoc]. Measured in the browser 29 Aug: the folium
               iframe carries NO srcdoc -- it is a stCustomComponentV1 with a
               src and title="streamlit_folium.st_folium" -- so that selector
               never once matched the map it was written for. What it DID
               match is every components.html helper, which are srcdoc
               iframes: BOTH scroll-to-top scripts, each declared height=0,
               forced to 500px. One of them renders at the top of the page on
               an app_mode change, and that is the entire "the map page is
               pushed down, but only on the first launch" -- 500px of blank
               iframe above ⚙ Page controls, gone on the next rerun because
               app_mode no longer changes. It was also silently capping the
               820px seismic fragment to 500.

               Six rounds of padding work went past this. The padding was
               always 64px and always correct; the blank band was an element.
               A selector that is broader than the thing it names is a bug
               waiting for a second element to match it. */
            iframe[title="streamlit_folium.st_folium"] {
                height: 500px !important;
                max-height: 500px !important;
                vertical-align: bottom !important;
            }
            div:has(> iframe[title="streamlit_folium.st_folium"]) {
                height: 500px !important;
                max-height: 500px !important;
                margin: 0 !important;
                padding: 0 !important;
                line-height: 0 !important;
            }
            div[data-testid="element-container"]:has(iframe) {
                margin-bottom: 0 !important;
                padding-bottom: 0 !important;
            }
            /* Hide empty divs Streamlit injects */
            div[data-testid="stVerticalBlock"] > div:empty { display:none !important; }
            </style>
        """, unsafe_allow_html=True)

        _mark("build: map CSS")
        _mapmsg.info("🌐 Rendering map in browser…")
        # Try use_container_width if available (streamlit-folium >= 0.18),
        # else fall back to width=None which lets st_folium auto-size.
        # We subscribe ONLY to events we actually consume — fewer events means
        # fewer Streamlit reruns, which means fewer pan/zoom grey-outs.
        #   last_object_clicked: lat/lon of the clicked object. Used for GRID
        #     CELL clicks — the handler floor-divides by step to identify
        #     which cell. Cells have NO popups so this is the only click
        #     signal for them.
        #   last_object_clicked_popup: popup HTML/text. Used for well marker
        #     clicks (popup contains UWI or well_id).
        #   all_drawings: used for circle draw → Haversine well drill.
        # We do NOT subscribe to last_clicked (fires on every mouse-down).
        # We also do NOT subscribe to center/zoom (unreliable + triggers reruns).
        _ret = [
            "last_object_clicked",
            "last_object_clicked_popup",
            "all_drawings",
        ]
        # ── Freeze: stop clicks costing a rebuild ────────────────────────
        # A CLICK AND A REDRAW ARE THE SAME SIGNAL. Both a well marker and a
        # 2D line report through last_object_clicked_popup -- the well handler
        # reads the UWI out of the popup text and _seis_pick_from_popup reads
        # the line out of it the same way -- so every pick round-trips to
        # Python and re-serialises the whole map. Pick, popup, 13-second
        # redraw; pick again, redraw again.
        #
        # Frozen, we simply do not subscribe to the click. Leaflet still
        # opens the popup and still shows the tooltip, both purely in the
        # browser, so hovering identifies a well (name, operator, status) or
        # a line (survey, line) at no cost at all. Nothing reaches Python, so
        # nothing rebuilds.
        #
        # all_drawings STAYS SUBSCRIBED, deliberately. Freezing would be a
        # trap if it also disabled selecting: the box and circle tools still
        # drill, and they were always the way to pick MANY wells in one
        # rebuild rather than one rebuild per well.
        #
        # Read before the widget draws, which is safe: Streamlit puts a
        # widget's new value into session_state before the script runs, so
        # this sees the CURRENT state of the toggle, not the previous one.
        if st.session_state.get("wm_freeze_map", False):
            _ret = ["all_drawings"]
        # The st_folium call below is the actual long pole — it serializes
        # the whole map to HTML/JS and the browser parses + renders it.
        # That's the part the user actually waits for (10-30 sec depending
        # on map complexity). Keep the top-of-page progress visible
        # THROUGH this call so the user knows it's still working.
        # Force widget re-render when selection changes.
        # Without this, streamlit-folium preserves the iframe across reruns
        # when only the map's content changes, and selected-cell highlights
        # (blue border) don't appear until something else forces a rebuild.
        # By appending a hash of selected_cells to the widget key, every
        # selection-state change creates a "new" widget instance that
        # streamlit-folium re-serializes from scratch — picking up the
        # updated cell borders. View-persist JS uses sessionStorage so
        # pan/zoom survives key changes.
        # The widget key must reflect EVERYTHING that changes what the map
        # shows — otherwise streamlit-folium reuses the cached iframe and the
        # freshly-built map (e.g. base layer suppressed because a box was
        # drilled) never gets serialized. Originally this only hashed
        # selected_cells (H3 cell borders), so drawing a bounding box — which
        # changes viewport_uwis but not selected_cells — left the stale
        # all-wells map on screen and the box never took priority. We now fold
        # in the drill selection (viewport_uwis / GOM) and the layer toggles
        # so any drill or toggle re-serializes. The view-persist JS keeps
        # pan/zoom across key changes, so this costs a redraw, not the view.
        # Key the map widget on the H3 cell selection only. streamlit-folium
        # re-renders natively when the map content changes substantially (e.g.
        # adding drilled well markers), so we do NOT need to fold the drill
        # selection into the key — and doing so was actively harmful: it forced
        # a full iframe remount on every box/circle draw, and a remounted iframe
        # only repaints on a real browser event, so the drilled wells appeared
        # on the user's SECOND draw. The cell-selection hash stays because cell
        # border highlights are a subtle change st_folium can miss otherwise.
        # ── Reset the VIEW (not the data) ────────────────────────────────
        # Sits here, immediately above the map and at mapcol level, so it is
        # ALWAYS on screen. The first attempt put it beside "✗ Clear wells",
        # which turned out to live inside a collapsed help expander AND only
        # in wells mode — present in the code, invisible in practice.
        #
        # Distinct from Clear: that removes what is displayed, this only moves
        # the camera. Changing a filter re-queries but does not re-frame,
        # because the view-persist JS restores the previous pan/zoom on every
        # rerun — which is what makes drawing usable, and also what can leave
        # you looking at the old area with the new wells off-screen.
        # Reset on the left, Map display filling the space beside it — that
        # row was a button and a wide gap, and the display toggles were the
        # last thing left in the control rail.
        # Saved places, in a fragment: see _render_saved_places.
        _render_saved_places(engine)
        _render_seed_reference(engine)
        # RESET VIEW AND CLEAR WELLS SIT TOGETHER because they are the pair:
        # one resets the CAMERA and keeps the data, the other clears the DATA
        # and leaves the camera. That distinction is drawn all through this
        # file; having only one of them on screen is what made the other
        # impossible to find.
        # ✗ Clear box SITS WITH THE ACTIONS, not in 🏷 Map display.
        # That expander holds display SETTINGS -- legends, lease colour,
        # symbols, the clip toggle. Clearing the drawn box is an action,
        # the same kind of thing as ✗ Clear wells and 🎯 Reset view, and
        # it belongs next to them where those are looked for.
        _rv1, _rvc, _rvb, _rvf, _rvh = st.columns(
            [1.0, 1.1, 1.0, 1.2, 1.2])
        # HOLD THE MAP UNTIL THE SELECTIONS ARE MADE. Every option on this
        # page reruns the script, and the script rebuilds the map -- so
        # picking an area, then a query, then a layer costs three rebuilds to
        # see one result. Held, the options still rerun (they are cheap; the
        # map serialize is what costs) and the map is simply not drawn until
        # Apply. Off restores the draw-on-every-change behaviour.
        _rvh.toggle(
            "⏸ Hold for Map", key="wm_hold_map",
            help="Don't redraw the map on every option change. Pick your "
                 "area, query and layers, then press Apply to draw once. "
                 "Map interactions — drawing a box, drilling, picking a "
                 "line — are never held; only the options above are.")
        # THE ONE CONTROL THAT MAKES CLICKING FREE. See the _ret block above
        # for why a pick and a rebuild are the same signal. Its own column so
        # it reads as a mode, not an action -- it stays on until turned off.
        _frozen = _rvf.toggle(
            "🔒 Freeze map", key="wm_freeze_map",
            help="Stop clicks rebuilding the map. Hover still identifies "
                 "every well and line, and the box / circle tools still "
                 "select — so you can explore freely and pick in one go. "
                 "Turn OFF to click a single well or line open into a "
                 "scout ticket. OFF by default: a click opening what you "
                 "clicked is what people expect, and freezing is the "
                 "deliberate trade of that for speed.")
        if _rvc.button("✗ Clear wells", key="wells_clear_viewport",
                       use_container_width=True,
                       disabled=not _wells_on_map(),
                       help=("Remove ALL displayed wells — the drilled "
                             "selection (rectangle / circle), the base well "
                             "layer, the Results and the tray — leaving just "
                             "the basemap. Re-select an area or run a Query "
                             "to load wells again."
                             if _wells_on_map() else
                             "Nothing to clear — no wells are selected or "
                             "displayed.")):
            _clear_wells_state()
            st.rerun()
        # THE TRASH ICON IN THE DRAW TOOLBAR CANNOT DO THIS. It empties
        # all_drawings, but st_folium reports all_drawings empty on every
        # render where nobody drew -- the drawings handler says so, which is
        # why _last_drawings is only written when the list is non-empty. An
        # empty list cannot tell "you deleted the box" from "you did not draw
        # this render", so the constraint cannot be released from that signal.
        #
        # And ✗ Clear wells is too big a hammer: clearing the CONSTRAINT
        # should not cost the selection it was constraining.
        #
        # KEY ENDS "_clear", which _is_action_key() already excludes.
        if _rvb.button("✗ Clear box", key="wm_clip_clear",
                       use_container_width=True,
                       disabled=not st.session_state.get("_clip_box"),
                       help=("Drop the drawn box and stop clipping. Wells, "
                             "tray and view are kept — ✗ Clear wells is for "
                             "those."
                             if st.session_state.get("_clip_box") else
                             "Nothing to clear — no box has been drawn.")):
            # A REQUEST for the toggle: the Clip checkbox is built later on
            # this same run, and assigning a widget its own key after it
            # exists raises on a LATER run -- scar #6.
            st.session_state.pop("_clip_box", None)
            st.session_state.pop("_place_shapes", None)
            st.session_state["_clip_off_request"] = True
            _say("[map] clear box: constraint dropped")
            st.rerun()
        if _rv1.button("🎯 Reset view", key="wells_reset_view",
                       use_container_width=True,
                       help="Re-frame the map on what is currently selected. "
                            "Wells, tray and Results are kept — only the "
                            "pan/zoom is reset."):
            # Tell the view-persist JS to wipe its sessionStorage copy BEFORE
            # it tries to restore, or the browser puts the old view straight
            # back and the button appears to do nothing.
            st.session_state["_reset_saved_view"] = True
            # Drop the bounds that pin the camera so the auto-fit recomputes,
            # and reset the area tracker so its one-shot fit fires again.
            st.session_state.pop("_drawn_bounds", None)
            st.session_state.pop("_active_drill_bbox", None)
            st.session_state.pop("_zoom_target_label", None)
            # AND THE LAST-FITTED EXTENT. The fit site now skips a target it
            # has already fitted, so without this the button would clear
            # everything else and still not re-frame on a live selection --
            # exactly what it promises to do.
            st.session_state.pop("_last_fit_sig", None)
            st.session_state["_wm_prev_area_id"] = "none"
            st.rerun()

        # ── FULL WIDTH, LAID OUT SIDE BY SIDE ────────────────────
        # This was the sixth column of the action row -- about 200px -- and
        # everything in it was squeezed. The selectbox note below records a
        # horizontal radio being abandoned for exactly that reason. Out of
        # the row it gets the whole width and the four controls sit in a row
        # of their own.
        #
        # BELOW the buttons rather than beside them: those are ACTIONS and
        # these are SETTINGS, so a row each keeps that division visible
        # instead of leaving it to which column something landed in.
        with st.expander("🏷 Map display", expanded=False):
            _md1, _md2, _md3, _md4 = st.columns(4)
            with _md1:
                _show_legend = st.checkbox(
                    "🏷 Map legends", key="wm_show_legend",
                    help="Floating keys on the map: well status bottom-right, "
                         "leases bottom-left. Both fold to their title bar "
                         "— the fold is remembered in the browser, so "
                         "collapsing one costs no redraw.")
            with _md2:
                # STILL A SELECTBOX, FOR A DIFFERENT REASON NOW. The old one
                # was that this lived in a ~200px column and three horizontal
                # options wrapped mid-word ("produc / ing"). The column is a
                # quarter of the map width now, which is wider -- but there
                # are FIVE options, and five radio buttons do not fit in it
                # either. Same control, honest reason.
                #
                # ORDER PUTS THE USABLE ONES FIRST. Measured on the 10,924
                # BLM leases loaded: producing_ind fills 3 values and
                # effective_date 11 decades. Owner is synthetic (see
                # tools/assign_synthetic_lease_owners.py) and lease_status
                # has one distinct value, because the loader keeps only
                # Authorized.
                st.selectbox(
                    "🟦 Lease colour",
                    ["producing", "vintage", "size", "owner", "status"],
                    key="wm_lease_color_by",
                    help="Producing (3 groups), Vintage (effective decade, "
                         "dark = older) and Size (acreage band, dark = "
                         "bigger) all work. Owner is synthetic. Status is a "
                         "single value once non-authorized leases are "
                         "filtered out.")
            with _md3:
                # SHOW ONLY WHAT IS IN THE BOX. Selecting cells outlines them
                # and leaves everything else drawn, which is right for
                # building a selection and wrong for showing one. Captured by
                # _capture_map_view, so a saved place comes back clipped the
                # way it was saved.
                st.checkbox(
                    "🔲 Clip to selection", key="wm_clip_to_box",
                    help="Draw only what falls inside the box or circle you "
                         "drew — hexagons by their centre, wells and leases "
                         "by position. Drawing a box switches this on by "
                         "itself; ✗ Clear box turns it off again.")
            with _md4:
                _ppdm_symbols = st.checkbox(
                    "🛢 PPDM well symbols", key="wm_ppdm_symbols",
                    help="Draw wells as standard PPDM/API symbols (shape = "
                         "status) instead of plain coloured dots")
            # Click-to-centre is NOT here. It is a button ON the map -- see
            # dv_click_centre. A map control buried in a collapsed expander
            # BELOW the map is a control nobody finds, which is exactly how
            # this one was reported missing the first time.

        _mark("build: controls row")
        _sel_for_key = st.session_state.get("selected_cells", [])
        _sel_key_hash = hash(tuple(sorted(
            f"{c[0]:.4f}|{c[1]:.4f}" for c in _sel_for_key
        )))
        _map_widget_key = f"well_map_folium_{_sel_key_hash}"

        # ── hold the map until Apply ────────────────────────────────────
        # THE SIGNATURE IS DELIBERATELY NARROW, and which way it errs matters.
        # A determinant left OUT of it simply redraws immediately, which is
        # today's behaviour -- no worse. A map INTERACTION accidentally put
        # IN would leave the map refusing to draw after a drill until Apply
        # was pressed, which on camera reads as broken. So this lists the
        # option inputs explicitly and nothing that a drill, a pick or a
        # drawing touches: viewport_uwis, _drawn_bounds, _seis_pick and the
        # rest are all absent on purpose.
        # OPTION WIDGETS, NOT VALUES DERIVED FROM THEM. The first cut hashed
        # active_area["id"], qtype, basemap and active_db -- and two identical
        # fresh sessions produced different signatures ('all' vs 'none'),
        # because a cached query resolved differently on the second. A
        # signature over derived values therefore holds the map for changes
        # nobody made. A widget value changes when, and only when, someone
        # changes it, which is the actual question being asked.
        #
        # Named explicitly and kept narrow. Deliberately absent: wm_place_*
        # (Go applies itself), wm_freeze_map and wm_hold_map (meta-controls --
        # holding the map because you toggled hold is absurd), and everything
        # a drill, pick or drawing touches.
        # See _map_option_sig: derived by prefix, because the six-name tuple
        # this replaces did not include the wells or H3 layer toggles.
        _opt_sig = _map_option_sig()
        # FIRST OPEN ALWAYS DRAWS. With no recorded signature there is nothing
        # to have changed, and a page that opens holding its own map behind an
        # Apply button is a page that looks broken on arrival.
        _drawn_sig = st.session_state.get("_map_drawn_sig")
        _opts_changed = _drawn_sig is not None and _drawn_sig != _opt_sig
        # DEFAULT OFF, AND THE READ MUST MATCH THE TOGGLE. Holding meant an
        # option change drew nothing until Apply, which is right when a
        # redraw costs 10s and wrong now that leases are a cached file and
        # the density is filtered -- the map is quick enough that the extra
        # click costs more than the redraw it saves.
        #
        # A read defaulting True against a toggle defaulting False is the
        # density-fallback defect again: the control says one thing and the
        # gate does another. Both are False now, and the header logs it.
        if st.session_state.get("wm_hold_map", False) and _opts_changed:
            _skip_folium = True
            st.warning(
                "⏸ **Map held** — options changed. Finish selecting, then "
                "press Apply. (Turn off *Hold for Map* to draw on every "
                "change.)")
            if st.button("▶ Apply — draw the map", key="wm_apply_map_btn",
                         type="primary", use_container_width=True):
                st.session_state["_map_drawn_sig"] = _opt_sig
                st.rerun()
        else:
            # Drawing IS applying: record what this render is showing, so the
            # next option change is measured against what is on screen.
            st.session_state["_map_drawn_sig"] = _opt_sig

        _mark("build: hold/apply gate")
        # NAME THE LAYERS THAT HAD NOTHING TO DRAW. Switching a chip on and
        # seeing no change is indistinguishable from a broken layer, and the
        # honest answer -- that table is empty on this database -- is one the
        # reader can act on: load it, or stop clicking it.
        if _geo_empty:
            _mapmsg.caption("· ".join([
                "ℹ️ Nothing to draw for: **%s**" % "**, **".join(_geo_empty),
                "those tables have no rows with geometry in this database."]))

        # SAY IT, RIGHT ABOVE THE MAP. A mode that silently swallows clicks is
        # worse than the redraw it replaces -- the click looks broken instead
        # of cheap. This sits immediately above the map so it is read before
        # the first click, not after it.
        if st.session_state.get("wm_freeze_map", False):
            # A CAPTION, NOT A BANNER, NOW THAT THIS IS THE DEFAULT.
            # The four-line st.info was right when Freeze was a rare opt-in:
            # a mode that silently swallows clicks is worse than the redraw
            # it replaces, so it had to announce itself. Making Freeze the
            # default turned that into a paragraph describing NORMAL
            # operation on every render -- reported as "why do I keep getting
            # this message". An exception is worth a banner; the steady state
            # is worth a line. The full text still lives in the toggle help,
            # which is where someone asking "what does Freeze do" will look.
            _mapmsg.caption(
                "🔒 Frozen — hover identifies; box and circle still "
                "select. Untick **Freeze map** to click a well open.")

        # SAY IT WHERE THE MISSING THING IS. The hold already draws a banner
        # at the TOP of the page, and that was not enough: "wells and H3 are
        # on but nothing is drawing" was reported with that banner already on
        # screen. The empty map is where the confusion happens, and an
        # explanation the reader has to scroll up to find is one they do not
        # find. Same lesson as ✗ Clear wells and 🔒 Freeze map, both of
        # which announce themselves immediately above the map for this reason.
        if _render_held:
            _mapmsg.info(
                "⛔ **Render held** — the well and hexagon layers are "
                "switched off on purpose, which is why the map is empty "
                "while their toggles still read on. Nothing was lost. Press "
                "**▶ Resume** (on the banner above, or in ⚙ Page controls) "
                "to draw them again.")

        if _skip_folium:
            map_data = None
        else:
            _phase(90, "🌐 Rendering map in browser…")
            try:
                map_data = st_folium(
                m, height=500, use_container_width=True,
                returned_objects=_ret,
                key=_map_widget_key,
            )
            except TypeError:
                # Older streamlit-folium
                map_data = st_folium(
                    m, width=None, height=500,
                    returned_objects=_ret,
                    key=_map_widget_key,
                )
        _phase(100)
        # UNDER THE MAP, which is what the messages are about. This used to
        # be _mapmsg.empty() -- clearing a placeholder that sat ABOVE the map
        # and had already pushed it down for the whole render.
        _mapmsg.flush(st.container())
        if _want_scroll_top:
            _scroll_main_to_top()
        # _watch_seis_choice() USED TO BE CALLED HERE and is now registered at
        # the top of run(). See the note there: 36 statements between the two
        # points can end the render early, and each one left the browser
        # polling a fragment that no longer existed.

        st.caption(
            "💡 **Map:** Toggle **Show grid** to see/hide the density heatmap. "
            "**Click cells** to add them to a multi-select, then **Commit** to "
            "drill the combined area. **OR** draw a **circle** (left toolbar) "
            "to drill wells inside a radius directly — up to 5,000 wells. "
            "Draw a rectangle or circle to select wells into Results. **Esc** cancels."
        )

        # ── Session state init ────────────────────────────────────────
        if "clicked_uwis" not in st.session_state:
            st.session_state.clicked_uwis = []
        if "scout_uwi" not in st.session_state:
            st.session_state.scout_uwi = None
        if "map_mode" not in st.session_state:
            # Default to the plain basemap. The Constrain-to flow drives
            # whether to show H3 density or load the Wells list next.
            st.session_state.map_mode = "none"
        if "grid_visible" not in st.session_state:
            # Show the density grid by default. User can toggle off to focus
            # on already-drilled wells without grid clutter.
            st.session_state.grid_visible = True
        if "selected_cells" not in st.session_state:
            # Multi-select buffer for grid cells. Each entry is a tuple of
            # (lat_bin, lon_bin, count). Cleared after Commit drills the
            # bbox of the union of selections, or after Clear.
            st.session_state.selected_cells = []

        # ── Spatial select — circle on map drills wells via Haversine ────
        # When the user draws a circle, we run a Haversine wells-in-radius
        # query against dv_well. The 5,000-well cap applies; over that we
        # warn and don't render. This REPLACES the current viewport (cells
        # and any previous circle drill).
        #
        # streamlit-folium / Leaflet.Draw serializes a drawn circle in one
        # of two ways depending on version:
        #   (a) GeoJSON Point with `properties.radius` (true circle)
        #   (b) GeoJSON Polygon (the circle approximated as a polygon)
        # We handle both — for (a) we use center+radius directly, for (b)
        # we derive an approximate center+radius from the polygon's bbox.
        _CIRCLE_CAP = 5000

        _raw_drawings = map_data.get("all_drawings") if map_data else None
        drawings = []
        if isinstance(_raw_drawings, list):
            drawings = _raw_drawings
        elif isinstance(_raw_drawings, dict):
            drawings = _raw_drawings.get("features", [])

        # KEEP THE GEOMETRY, not just its hash. processed_drawings holds
        # hashes so a shape is not re-drilled on every rerun -- which answers
        # "have I seen this?" and cannot answer "what was it?". Saving a place
        # with the shapes that were on the map needs the shapes.
        #
        # Only when there ARE drawings: st_folium reports all_drawings as
        # empty on renders where the user did not draw, and letting that
        # overwrite the stored set would clear the shapes on the next rerun.
        if drawings:
            st.session_state["_last_drawings"] = drawings

        # Dedupe — don't reprocess the same drawing on every rerun
        if "processed_drawings" not in st.session_state:
            st.session_state.processed_drawings = set()

        if drawings:
            # "I drew a box and nothing happened" has four silent endings:
            # no drawing arrived, the drawing was already processed, the
            # cell pick came back empty, or no area was selected. Only the
            # last one said anything, so the log could not tell them apart.
            _say("[map] drawings arrived: %d (processed so far: %d)"
                 % (len(drawings),
                    len(st.session_state.get("processed_drawings") or ())))
            for drawing in drawings:
                geom   = drawing.get("geometry", {})
                gtype  = geom.get("type", "")
                coords = geom.get("coordinates", [])
                props  = drawing.get("properties", {}) or {}

                _geom_hash = hash(json.dumps(geom, sort_keys=True))
                if _geom_hash in st.session_state.processed_drawings:
                    continue

                try:
                    # Detect rectangle vs circle-as-polygon:
                    #   - Rectangle: exactly 5 ring points (4 corners + close)
                    #   - Circle approximated as polygon: 32 or 64+ points
                    # A true GeoJSON Point with radius=N indicates an actual
                    # circle from Leaflet.Draw with circle support enabled.
                    _is_rectangle = (
                        gtype == "Polygon"
                        and coords
                        and len(coords[0]) == 5
                    )

                    if _is_rectangle:
                        # ── Rectangle path: bbox-based query ────────────
                        # Use _qry_wells_in_bbox with the exact rectangle
                        # bounds — no Haversine, no inscribing circle, just
                        # a SQL BETWEEN on the indexed (lat, lon) columns.
                        # Faster and more accurate than the circle path.
                        ring = coords[0]
                        _min_lat = min(c[1] for c in ring)
                        _max_lat = max(c[1] for c in ring)
                        _min_lon = min(c[0] for c in ring)
                        _max_lon = max(c[0] for c in ring)

                        st.session_state.processed_drawings.add(_geom_hash)
                        # THE BOX, RECORDED AS ITSELF. Set here rather than in
                        # either branch below so a rectangle means the same
                        # thing to the clip whether it went on to select cells
                        # or to drill wells.
                        st.session_state["_clip_box"] = [
                            [_min_lat, _min_lon], [_max_lat, _max_lon]]
                        # CLOSE THE LOOP FROM THE OTHER SIDE. Ticking Clip
                        # before a box exists warns and does nothing, so the
                        # natural move is to untick it and draw -- which is
                        # exactly backwards, and is what happened: three
                        # renders of "clip ON but no box", then the toggle
                        # went off, then the box arrived. Saying it here
                        # means either order gets the operator there.
                        # THE BOX IS THE CONSTRAINT. Drawing one now turns
                        # the clip on by itself: "draw a box and then anything
                        # added is constrained by the box" is the whole idea,
                        # and a separate toggle you had to remember produced
                        # the exact backwards order that broke it -- tick,
                        # nothing happens, untick, draw.
                        #
                        # A REQUEST, NOT AN ASSIGNMENT. This handler runs
                        # AFTER the checkbox has drawn, and assigning a
                        # widget its own key once it exists raises on a LATER
                        # run on whatever page draws next -- scar #6. The top
                        # of run() consumes this before the widget is built.
                        if not st.session_state.get("wm_clip_to_box"):
                            st.session_state["_clip_request"] = True
                            _say("[map] box drawn -> requesting clip ON")

                        # ── A BOX OVER HEXAGONS SELECTS HEXAGONS ────────
                        # "I tried to draw a box over the cells but that did
                        # not work." It worked -- it drilled the WELLS in the
                        # box and handed off to the Wells layer, which is the
                        # right answer when you are looking at wells and the
                        # wrong one when you are looking at cells.
                        #
                        # So when the H3 layer is on, a rectangle selects the
                        # CELLS it covers, through the same store a click
                        # uses: the two gestures then compose, and a box is
                        # just a faster way to click several. Wells stay the
                        # rectangle's job whenever H3 is off, which is every
                        # pre-existing use of this tool.
                        #
                        # CENTRE-IN-BOX, not intersects. A hexagon straddling
                        # the edge is half outside what was drawn, and
                        # counting it would put wells in the selection that
                        # are visibly outside the box -- the same complaint
                        # that produced the exact-containment drill in the
                        # first place, one level up.
                        # RESOLUTION FROM SESSION STATE, and the cells from h3
                        # itself. The first version of this read _h3_res_active
                        # and _h3_df -- both assigned FURTHER DOWN the same
                        # function (14515 and 12249), so it would have raised
                        # NameError the first time anyone drew a box. h3 needs
                        # neither: polygon_to_cells returns the cells whose
                        # CENTRE lies in the polygon, which is the containment
                        # rule this wants, computed rather than looked up.
                        _box_res = int(st.session_state.get("h3_resolution", 4))
                        _say("[map] box: h3_on=%s res=R%s  %.4f,%.4f .. %.4f,%.4f"
                             % (st.session_state.get("h3_layer_on"), _box_res,
                                _min_lat, _min_lon, _max_lat, _max_lon))
                        if (st.session_state.get("h3_layer_on")
                                and _box_res in (4, 5, 6, 7)):
                            try:
                                _picked = list(h3.polygon_to_cells(
                                    h3.LatLngPoly([
                                        (_min_lat, _min_lon), (_min_lat, _max_lon),
                                        (_max_lat, _max_lon), (_max_lat, _min_lon)]),
                                    _box_res))
                            except Exception as _pce:
                                _say("[map] box->cells failed: %s" % str(_pce)[:120])
                                _picked = []
                            _say("[map] box -> %d cell(s) at R%d%s"
                                 % (len(_picked), _box_res,
                                    "" if _picked else "  (falling through to the wells drill)"))
                            if _picked:
                                _store = dict(st.session_state.get(
                                    "_h3_cell_uwis", {}))
                                _cellcol = f"h3_r{_box_res}"
                                _added = 0
                                # ONE QUERY FOR THE BOX, not one per cell.
                                # This looped over _picked calling
                                # _qry_wells_in_bbox, which runs a COUNT and
                                # a SELECT each -- fine at R4 (a few dozen
                                # cells), ~6,820 round trips at R7 where the
                                # same box is 3,410 cells. That is a hung
                                # map, and it was reported as one twice.
                                _buckets = _qry_cell_uwis_in_bbox(
                                    engine, _min_lat, _max_lat,
                                    _min_lon, _max_lon, _cellcol,
                                    where_extra=st.session_state.get(
                                        "_active_where_extra", ""))
                                # EMPTY CELLS ARE NOT SELECTED. Storing a
                                # cell with no wells drew an outline round
                                # empty ground and inflated the cell count
                                # in the message; of 3,410 cells in a box
                                # only the populated ones mean anything.
                                _want = set(str(c) for c in _picked)
                                for _cc, _us in _buckets.items():
                                    if _cc in _store or _cc not in _want or not _us:
                                        continue
                                    _store[_cc] = _us
                                    _added += 1
                                _say("[map] box: %d of %d cell(s) hold wells"
                                     % (_added, len(_picked)))
                                if _added:
                                    st.session_state["_h3_cell_uwis"] = _store
                                    st.session_state["selected_h3_cells"] = list(_store)
                                    _seen, _union = set(), []
                                    for _cu in _store.values():
                                        for _u in _cu:
                                            if _u not in _seen:
                                                _seen.add(_u)
                                                _union.append(_u)
                                    _capped = len(_union) > 5000
                                    st.session_state["viewport_uwis"] = _union[:5000]
                                    st.session_state["_drawn_bounds"] = [
                                        [_min_lat, _min_lon], [_max_lat, _max_lon]]
                                    st.session_state["_drawn_bounds_oneshot"] = True
                                    st.success(
                                        "🔶 Box: added **%d cell(s)** — "
                                        "**%s wells** across **%d cell(s)**."
                                        % (_added, format(min(len(_union), 5000), ","),
                                           len(_store)))
                                    if _capped:
                                        st.warning(
                                            "Selection capped at 5,000 wells — "
                                            "the cells hold %s. Remove cells or "
                                            "use a finer resolution."
                                            % format(len(_union), ","))
                                    st.rerun()

                        _active_sources = active_area.get("sources", [])
                        if not _active_sources:
                            # THE LAST SILENT ENDING. This warned on
                            # screen and said nothing to the log, so a
                            # box that stopped here was indistinguishable
                            # from one that never arrived.
                            _say("[map] box: no area selected -- the "
                                 "rectangle drill needs one, nothing done")
                            st.warning(
                                "⬛ Pick an area first (Area dropdown above "
                                "the map) before drawing a rectangle."
                            )
                            st.session_state.processed_drawings.discard(_geom_hash)
                            continue

                        _bbox_main: list = []
                        _bbox_gom:  list = []
                        _total_main = 0
                        _total_gom  = 0

                        with st.spinner("Querying wells in rectangle…"):
                            if "main" in _active_sources:
                                try:
                                    _bbox_main, _total_main = _qry_wells_in_bbox(
                                        engine,
                                        _min_lat, _max_lat,
                                        _min_lon, _max_lon,
                                        limit=_CIRCLE_CAP,
                                        where_extra=st.session_state.get("_active_where_extra", ""),
                                    )
                                except Exception as _qe:
                                    st.error(f"Main bbox query failed: {_qe}")
                            if "gom" in _active_sources:
                                try:
                                    _bbox_gom, _total_gom = _qry_gom_wells_in_bbox(
                                        engine,
                                        _min_lat, _max_lat,
                                        _min_lon, _max_lon,
                                        limit=_CIRCLE_CAP,
                                        where_extra=st.session_state.get("_active_where_extra_gom", ""),
                                    )
                                except Exception as _qe:
                                    st.error(f"GOM bbox query failed: {_qe}")

                        _total_found = _total_main + _total_gom

                        if _total_found == 0:
                            st.info(
                                f"No wells found in rectangle "
                                f"({_min_lat:.3f},{_min_lon:.3f}) to "
                                f"({_max_lat:.3f},{_max_lon:.3f})"
                            )
                        elif (_total_main > _CIRCLE_CAP) or (_total_gom > _CIRCLE_CAP):
                            st.warning(
                                f"⚠️ Over the {_CIRCLE_CAP:,} cap "
                                f"(main: {_total_main:,}, GOM: {_total_gom:,}) — "
                                "draw a smaller rectangle to inspect this area."
                            )
                        else:
                            # REPLACE viewport with bbox results — viewport
                            # drives the map markers; tray drives Reports/Export.
                            if _bbox_main:
                                st.session_state.viewport_uwis = [
                                    w["uwi"] for w in _bbox_main
                                ]
                                _shadow = st.session_state.get("tray_well_data", {})
                                for w in _bbox_main:
                                    _shadow[w["uwi"]] = w
                                st.session_state["tray_well_data"] = _shadow
                            else:
                                st.session_state.viewport_uwis = []
                            if _bbox_gom:
                                st.session_state["viewport_gom_wells"] = _bbox_gom
                            else:
                                st.session_state["viewport_gom_wells"] = []

                            # Fit the map to the rectangle on next render
                            st.session_state["_drawn_bounds"] = [
                                [_min_lat, _min_lon],
                                [_max_lat, _max_lon],
                            ]
                            # Drawn handoff: wells are now loaded for this
                            # area, so hand off from the H3 overview to the
                            # Wells layer on the next run (applied before the
                            # toggles instantiate). User can re-enable H3.
                            st.session_state["_pending_wells_handoff"] = True
                            # Box owns the view: suppress the broad area loader
                            # so we DON'T pull the whole area's wells (e.g.
                            # Allen county's ~22K) just to throw them away. The
                            # box already queried its own wells into
                            # viewport_uwis/shadow above; wells_suppressed makes
                            # _need_wells False (no heavy load) and keeps the
                            # base layer off, so only the box's wells load+show.
                            # Cleared when the user changes area/query (via
                            # _lift_well_suppression).
                            st.session_state["wells_suppressed"] = True
                            # Remember the box so an attribute-filter change
                            # can re-filter this exact selection in place
                            # (Scenario 2) instead of abandoning it.
                            st.session_state["_active_drill_bbox"] = (
                                _min_lat, _max_lat, _min_lon, _max_lon)

                            # Auto-add results to the Object Tray, with cap.
                            # Below the cap → straight into the tray.
                            # Above the cap → stash the candidates and prompt
                            # the user to confirm (rendered below the map).
                            _total_in_drill = len(_bbox_main) + len(_bbox_gom)
                            _parts = []
                            if _bbox_main:
                                _parts.append(f"{len(_bbox_main):,} main")
                            if _bbox_gom:
                                _parts.append(f"{len(_bbox_gom):,} GOM")

                            if _total_in_drill <= _TRAY_AUTO_ADD_CAP:
                                _added = _add_drill_results_to_tray(
                                    list(_bbox_main) + list(_bbox_gom),
                                    replace=True,
                                )
                                st.session_state.pop("_pending_drill_wells", None)
                                st.success(
                                    f"⬛ Added **{_added:,}** wells to "
                                    f"Results (from {' + '.join(_parts)} "
                                    "drilled)."
                                )
                            else:
                                # Stash for the confirm prompt
                                st.session_state["_pending_drill_wells"] = (
                                    list(_bbox_main) + list(_bbox_gom)
                                )
                                st.session_state["_pending_drill_label"] = (
                                    f"rectangle ({' + '.join(_parts)})"
                                )
                                st.info(
                                    f"⬛ Drill returned **{_total_in_drill:,}** "
                                    "wells — above the auto-add cap of "
                                    f"{_TRAY_AUTO_ADD_CAP:,}. Use the prompt "
                                    "below the map to confirm or draw a "
                                    "smaller area."
                                )
                            st.rerun()

                        # ── THE BOX IS A RESULT EVEN WHEN THE DRILL IS NOT ──
                        # Only the SUCCESSFUL drill reran. A rectangle that
                        # found nothing, or that went over the 5,000 cap,
                        # ended with a message and no redraw -- so _clip_box
                        # and the clip request sat in session_state applying
                        # to nothing, and the map on screen was the one built
                        # BEFORE the box existed. That is "I drew a box and
                        # nothing happened", and it is the common case: a box
                        # over any well-populated ground is over the cap.
                        # Measured 29 Aug -- a box near Casper holding 8,574
                        # wells warned about the cap and left the page idle,
                        # with the bounds correct in the log one line above.
                        #
                        # Drawing a box now MEANS something on its own: it
                        # clips every layer. So it earns its redraw whatever
                        # the drill did. Safe from looping because the
                        # geometry hash went into processed_drawings before
                        # any of these branches, so the next render skips it.
                        _say("[map] box handled, no drill rerun -> "
                             "rerunning so the clip applies")
                        st.rerun()

                        # Rectangle path complete — skip the circle path below
                        continue

                    # ── Circle path (existing logic) ────────────────────
                    _center_lat = _center_lon = _radius_m = None

                    if gtype == "Point" and coords:
                        # True circle: GeoJSON Point with radius in metres
                        _center_lon, _center_lat = coords[0], coords[1]
                        _radius_m = float(props.get("radius", 0))

                    elif gtype == "Polygon" and coords:
                        # Approximated as polygon — derive center + radius
                        # from the polygon's bbox.
                        ring = coords[0]
                        _min_lat = min(c[1] for c in ring)
                        _max_lat = max(c[1] for c in ring)
                        _min_lon = min(c[0] for c in ring)
                        _max_lon = max(c[0] for c in ring)
                        _center_lat = (_min_lat + _max_lat) / 2.0
                        _center_lon = (_min_lon + _max_lon) / 2.0
                        # Radius = half the diagonal in metres (rough but
                        # captures the user's intended area)
                        import math as _m
                        _dlat_m = (_max_lat - _min_lat) * 111000.0 / 2.0
                        _dlon_m = (_max_lon - _min_lon) * 111000.0 * \
                                  _m.cos(_m.radians(_center_lat)) / 2.0
                        _radius_m = _m.sqrt(_dlat_m ** 2 + _dlon_m ** 2)

                    st.session_state.processed_drawings.add(_geom_hash)

                    if _center_lat is None or _radius_m is None or _radius_m <= 0:
                        continue

                    # Determine drill targets from active_area sources, same
                    # pattern as the cell-Commit dispatch. Each source has
                    # its own circle query that handles its schema's
                    # coordinate columns and indexes.
                    _active_sources = active_area.get("sources", [])
                    if not _active_sources:
                        st.warning(
                            "⭕ Pick an area first (Area dropdown above the "
                            "map) before drawing a circle. The circle drill "
                            "needs to know which dataset to query."
                        )
                        st.session_state.processed_drawings.discard(_geom_hash)
                        continue

                    # Drill each active source. Errors from one source don't
                    # block the other.
                    _circle_main: list = []
                    _circle_gom:  list = []
                    _total_main = 0
                    _total_gom  = 0

                    with st.spinner(
                        f"Querying wells within {_radius_m/1000:.1f} km…"
                    ):
                        if "main" in _active_sources:
                            try:
                                _circle_main, _total_main = _qry_wells_in_circle(
                                    engine,
                                    _center_lat, _center_lon, _radius_m,
                                    limit=_CIRCLE_CAP,
                                    where_extra=st.session_state.get("_active_where_extra", ""),
                                )
                            except Exception as _qe:
                                st.error(f"Main circle query failed: {_qe}")

                        if "gom" in _active_sources:
                            try:
                                _circle_gom, _total_gom = _qry_gom_wells_in_circle(
                                    engine,
                                    _center_lat, _center_lon, _radius_m,
                                    limit=_CIRCLE_CAP,
                                    where_extra=st.session_state.get("_active_where_extra_gom", ""),
                                )
                            except Exception as _qe:
                                st.error(f"GOM circle query failed: {_qe}")

                    _circle_total_loaded = len(_circle_main) + len(_circle_gom)
                    _circle_total_found = _total_main + _total_gom

                    if _circle_total_found == 0:
                        st.info(
                            f"No wells found within {_radius_m/1000:.1f} km of "
                            f"({_center_lat:.4f}, {_center_lon:.4f})"
                        )
                    elif (_total_main > _CIRCLE_CAP) or (_total_gom > _CIRCLE_CAP):
                        st.warning(
                            f"⚠️ Over the {_CIRCLE_CAP:,} cap "
                            f"(main: {_total_main:,}, GOM: {_total_gom:,}) — "
                            f"draw a smaller circle to inspect this area."
                        )
                        # Don't replace viewport — keep what was there
                    else:
                        # REPLACE both viewports — circle is a fresh look,
                        # not additive. Same pattern as cell Commit.
                        if _circle_main:
                            st.session_state.viewport_uwis = [
                                w["uwi"] for w in _circle_main
                            ]
                            # Cache full data for tray/scout lookups
                            _shadow = st.session_state.get("tray_well_data", {})
                            for w in _circle_main:
                                _shadow[w["uwi"]] = w
                            st.session_state["tray_well_data"] = _shadow
                        else:
                            st.session_state.viewport_uwis = []

                        if _circle_gom:
                            st.session_state["viewport_gom_wells"] = _circle_gom
                        else:
                            st.session_state["viewport_gom_wells"] = []

                        # Map zooms to center of circle at a zoom level
                        # appropriate to see the whole radius. We do that by
                        # setting _drawn_bounds to the circle's bbox — the
                        # map fits to those bounds on next render.
                        import math as _m
                        _dlat = _radius_m / 111000.0
                        _dlon = _radius_m / (
                            111000.0 * max(_m.cos(_m.radians(_center_lat)), 0.01)
                        )
                        st.session_state["_drawn_bounds"] = [
                            [_center_lat - _dlat, _center_lon - _dlon],
                            [_center_lat + _dlat, _center_lon + _dlon],
                        ]
                        # Remember the circle's bbox so an attribute-filter
                        # change can re-filter this selection in place
                        # (Scenario 2). Note: this is the circle's bounding
                        # box, so a re-filter is a slight over-approximation
                        # of the original radius.
                        st.session_state["_active_drill_bbox"] = (
                            _center_lat - _dlat, _center_lat + _dlat,
                            _center_lon - _dlon, _center_lon + _dlon)

                        # Clear any cell-selection — circle replaces that
                        # workflow's output too
                        st.session_state["selected_cells"] = []

                        # Hide the grid — same as cell Commit. User sees the
                        # drilled wells without heatmap clutter. Toggle 'Show
                        # grid' to bring it back for another selection.
                        # Pop the widget key (not write) to avoid Streamlit's
                        # "can't modify widget state after instantiation" error.
                        st.session_state["grid_visible"] = False
                        st.session_state.pop("grid_visible_toggle", None)
                        # Drawn handoff (deferred, applied before the toggles
                        # render next run): H3 overview → Wells layer.
                        st.session_state["_pending_wells_handoff"] = True
                        # Circle owns the view: suppress the broad area loader
                        # so toggling Wells on doesn't pull the whole area's
                        # wells (same rationale as the rectangle path).
                        st.session_state["wells_suppressed"] = True

                        # Build status message reflecting which sources fired
                        _parts = []
                        if _circle_main:
                            _parts.append(f"{len(_circle_main):,} main")
                        if _circle_gom:
                            _parts.append(f"{len(_circle_gom):,} GOM")

                        # Auto-add drill results to the Object Tray, with cap.
                        # Same pattern as the rectangle path.
                        _total_in_drill = len(_circle_main) + len(_circle_gom)
                        if _total_in_drill <= _TRAY_AUTO_ADD_CAP:
                            _added = _add_drill_results_to_tray(
                                list(_circle_main) + list(_circle_gom),
                                replace=True,
                            )
                            st.session_state.pop("_pending_drill_wells", None)
                            st.success(
                                f"⭕ Added **{_added:,}** wells to Results "
                                f"within {_radius_m/1000:.1f} km "
                                f"(from {' + '.join(_parts)} drilled). "
                                "Grid hidden — toggle 'Show grid' to draw "
                                "another shape."
                            )
                        else:
                            st.session_state["_pending_drill_wells"] = (
                                list(_circle_main) + list(_circle_gom)
                            )
                            st.session_state["_pending_drill_label"] = (
                                f"circle ({_radius_m/1000:.1f} km, "
                                f"{' + '.join(_parts)})"
                            )
                            st.info(
                                f"⭕ Drill returned **{_total_in_drill:,}** "
                                "wells — above the auto-add cap of "
                                f"{_TRAY_AUTO_ADD_CAP:,}. Use the prompt "
                                "below the map to confirm or draw a "
                                "smaller area."
                            )
                        st.rerun()

                except Exception as _e:
                    st.warning(f"Circle drill failed: {_e}")

        # ── Parse click — grid cell (coords) OR well UWI (popup) ────────
        # Two distinct click sources:
        #   1. last_object_clicked   → lat/lon of clicked element. Used for
        #      grid cells (which have no popup). The handler floor-divides
        #      the click coords by the active area's step to find the
        #      cell, then toggles it in selected_cells.
        #   2. last_object_clicked_popup → popup text. Used for well marker
        #      clicks (popups contain UWI/well_id for tray pickup).

        # Determine the step for the active area's grid (for cell hit-test)
        _active_sources = active_area.get("sources", [])
        # If active area has both main and gom, we need to know which grid
        # the click landed in. Step values differ — main is 0.035°, gom
        # is 0.36°. A click could be in either. We test against both and
        # take the one whose floor-cell exists in the rendered grid data.
        # For now: if only one source is active, use that step. If both
        # are active, we'll test the click against both grids by hit-test.
        _cell_steps = []
        if "main" in _active_sources:
            _cell_steps.append(("main", 0.035))
        if "gom" in _active_sources:
            _cell_steps.append(("gom", 0.36))

        _coord_click = map_data.get("last_object_clicked") if map_data else None
        # If the same click also returned popup content, it was a well marker
        # click (markers have popups, cells don't). Skip the cell-click path
        # in that case — otherwise floor-dividing the marker's coords would
        # toggle a grid cell underneath, polluting the selection buffer.
        _click_popup = map_data.get("last_object_clicked_popup") if map_data else None
        _handled_as_cell = False

        # Selection mode gate: cell-click toggling only happens in "Cells"
        # mode. In "Circle" mode the cell-click handler is skipped entirely
        # so a press-drag-release that starts on a grid cell isn't stolen
        # by the cell toggler — the circle gesture stays unambiguous.
        _cells_mode = st.session_state.get("gom_sel_mode", "Cells") == "Cells"

        # ─────────────────────────────────────────────────────────────
        # H3 click → drill (Session 3)
        # In H3 mode, click on a hex drills wells inside its bbox via the
        # existing _qry_wells_in_bbox loader. No multi-select / Commit —
        # click acts as an immediate drill action.
        # ─────────────────────────────────────────────────────────────
        if (_map_mode == "h3" and _cells_mode and _coord_click
                and not _click_popup
                and st.session_state.get("grid_visible", True)):
            try:
                _h3_click_lat = float(_coord_click.get("lat"))
                _h3_click_lon = float(_coord_click.get("lng"))
            except (TypeError, ValueError, AttributeError):
                _h3_click_lat = _h3_click_lon = None

            if _h3_click_lat is not None and _h3_click_lon is not None:
                _h3_res_active = int(st.session_state.get("h3_resolution", 4))
                try:
                    _clicked_h3 = h3.latlng_to_cell(
                        _h3_click_lat, _h3_click_lon, _h3_res_active
                    )
                except Exception:
                    _clicked_h3 = None

                # Dedupe: streamlit-folium returns the same click coords
                # across reruns until something else is clicked. Without
                # this guard, every rerun would re-drill the same hex.
                if _clicked_h3 and st.session_state.get(
                        "_last_h3_click") != _clicked_h3:
                    st.session_state["_last_h3_click"] = _clicked_h3

                    _bbox = _h3_cell_bbox(_clicked_h3)
                    if _bbox:
                        _bb_min_lat, _bb_max_lat, _bb_min_lon, _bb_max_lon = _bbox
                        # ── THE BBOX IS NOT THE CELL ────────────────────────
                        # A hexagon's bounding box is about 25-30% larger than
                        # the hexagon, so drilling the box alone returns the
                        # corner wells too — visibly outside the shape that was
                        # clicked. Perry: "when I pick a cell the wells are not
                        # constrained by the cell shape."
                        #
                        # dv_well already holds the exact answer: h3_r4..h3_r7
                        # are latlng_to_cell of that well's own coordinates, so
                        # `h3_r<res> = <clicked cell>` IS containment in the
                        # hexagon that was drawn — the boundary comes from
                        # cell_to_boundary of the same index.
                        #
                        # The bbox STAYS as a cheap pre-filter: it is sargable
                        # on the lat/lon composite index, while the cell column
                        # is not indexed. Box first, then the exact test.
                        _cell_col = f"h3_r{_h3_res_active}"
                        _cell_pred = ""
                        if _h3_res_active in (4, 5, 6, 7) and _clicked_h3:
                            # _clicked_h3 comes from h3.latlng_to_cell — 15 hex
                            # characters, nothing to quote-escape, but bound as
                            # a literal only because these helpers take a WHERE
                            # fragment rather than parameters.
                            _safe = "".join(ch for ch in str(_clicked_h3)
                                            if ch in "0123456789abcdefABCDEF")
                            if len(_safe) == 15:
                                _cell_pred = f" AND w.{_cell_col} = '{_safe}'"
                        _h3_drill_src = active_area.get("sources", [])
                        _h3_drilled_uwis = []
                        _h3_drill_shadow = {}
                        _MAX_H3 = 5000

                        try:
                            # Main drill if "main" is active
                            if "main" in _h3_drill_src:
                                _dm, _ = _qry_wells_in_bbox(
                                    engine,
                                    _bb_min_lat, _bb_max_lat,
                                    _bb_min_lon, _bb_max_lon,
                                    limit=_MAX_H3,
                                    where_extra=st.session_state.get(
                                        "_active_where_extra", "") + _cell_pred,
                                )
                                for _r in _dm:
                                    _h3_drilled_uwis.append(_r["uwi"])
                                    _h3_drill_shadow[_r["uwi"]] = _r

                            # GOM drill if "gom" is active
                            if "gom" in _h3_drill_src:
                                _dg, _ = _qry_gom_wells_in_bbox(
                                    engine,
                                    _bb_min_lat, _bb_max_lat,
                                    _bb_min_lon, _bb_max_lon,
                                    limit=_MAX_H3,
                                    where_extra=st.session_state.get("_active_where_extra_gom", ""),
                                )
                                for _r in _dg:
                                    _key = str(_r.get("well_id") or _r.get("uwi"))
                                    _h3_drilled_uwis.append(_key)
                                    _h3_drill_shadow[_key] = _r

                            if _h3_drilled_uwis:
                                # ── CELLS ACCUMULATE ───────────────────────
                                # This assigned viewport_uwis outright, so
                                # each click threw the previous cell away and
                                # a two-hexagon selection was impossible. The
                                # highlight machinery for a multi-select was
                                # already here -- _add_h3_layer takes a
                                # selected_set and outlines those cells -- and
                                # nothing populated it. Now the click does.
                                #
                                # Kept per cell rather than as one merged list
                                # so a SECOND click on the same hexagon can
                                # take it back out again: a multi-select you
                                # cannot undo is a trap, and rebuilding the
                                # union from the parts is exact where
                                # subtracting a set of uwis would not be --
                                # two adjacent cells can share no well, but a
                                # well removed from one must not vanish from
                                # another that also holds it.
                                _cells = dict(st.session_state.get(
                                    "_h3_cell_uwis", {}))
                                if _clicked_h3 in _cells:
                                    _cells.pop(_clicked_h3, None)
                                    _verb = "removed"
                                else:
                                    _cells[_clicked_h3] = _h3_drilled_uwis
                                    _verb = "added"
                                st.session_state["_h3_cell_uwis"] = _cells
                                st.session_state["selected_h3_cells"] = \
                                    list(_cells)
                                # Union, in click order, deduplicated: a well
                                # sits in exactly one cell per resolution, but
                                # the GOM branch can contribute the same well
                                # twice, and a repeated uwi is a repeated row
                                # in the tray.
                                _seen, _union = set(), []
                                for _cu in _cells.values():
                                    for _u in _cu:
                                        if _u not in _seen:
                                            _seen.add(_u)
                                            _union.append(_u)
                                _over = len(_union) > _MAX_H3
                                if _over:
                                    _union = _union[:_MAX_H3]
                                st.session_state["viewport_uwis"] = _union
                                # Merge into the tray-shadow cache so
                                # subsequent renders can pull full well
                                # dicts without re-querying
                                _existing = dict(st.session_state.get(
                                    "tray_well_data", {}))
                                _existing.update(_h3_drill_shadow)
                                st.session_state["tray_well_data"] = _existing
                                # ── FRAME WHAT WAS CLICKED ─────────────────
                                # "it selects the well but then the map zooms
                                # out to default." In H3 mode with the Wells
                                # layer off, dff is empty -- so the centroid
                                # chain lands on `lat0, lon0, zoom0 = 39.5,
                                # -98.35, 4`, the CONUS default, and NOTHING
                                # calls fit_bounds. The camera then depends
                                # entirely on the view-persist JS restoring
                                # sessionStorage, and when that does not take,
                                # the map is simply where folium built it: the
                                # whole country.
                                #
                                # A drill should not be relying on a restore
                                # to stay put. It knows exactly where it went,
                                # so it says so: the union bbox of the
                                # SELECTED cells, which also means removing a
                                # cell re-frames the rest instead of leaving
                                # the camera on a hexagon that is no longer in
                                # the selection.
                                #
                                # ONE-SHOT, so it frames once and then leaves
                                # the camera alone -- the same flag the
                                # area-change auto-zoom uses. Without it every
                                # later rerun would re-fit and destroy any
                                # manual zoom, which is a bug this file has
                                # already been through once.
                                _bbs = [_h3_cell_bbox(_cc) for _cc in _cells]
                                _bbs = [_b for _b in _bbs if _b]
                                if _bbs:
                                    st.session_state["_drawn_bounds"] = [
                                        [min(_b[0] for _b in _bbs),
                                         min(_b[2] for _b in _bbs)],
                                        [max(_b[1] for _b in _bbs),
                                         max(_b[3] for _b in _bbs)]]
                                    st.session_state["_drawn_bounds_oneshot"] = True
                                st.success(
                                    "🔶 Hex drill: %s R%s cell %s — "
                                    "**%s wells** across **%d cell(s)**.%s"
                                    % (_verb, _h3_res_active, _clicked_h3,
                                       format(len(_union), ","), len(_cells),
                                       ("  Click a selected cell again to "
                                        "remove it; ✗ Clear wells resets."
                                        if len(_cells) == 1 else "")))
                                if _over:
                                    st.warning(
                                        "Selection capped at %s wells — the "
                                        "cells hold more. Remove a cell or "
                                        "drop to a finer resolution."
                                        % format(_MAX_H3, ","))
                                _handled_as_cell = True
                                st.rerun()
                            else:
                                st.info(
                                    f"🔶 No wells in R{_h3_res_active} "
                                    f"cell {_clicked_h3}"
                                )
                                _handled_as_cell = True
                        except Exception as _e:
                            st.warning(f"H3 drill failed: {_e}")

        # A CELL CANNOT BE CLICKED WHEN NO CELL IS DRAWN. _cell_steps comes
        # from the ACTIVE SCHEMAS, not from what is on the map, and
        # grid_visible defaults True -- so with H3 density switched OFF this
        # branch was still live, and ANY click on the map floor-divided its
        # coordinates into an invisible grid cell, toggled it into the
        # selection buffer and called st.rerun().
        #
        # That is the "I click a lease, the screen greys twice and the lease
        # information disappears" report, reproduced 29 Aug with h3=False and
        # freeze=True: render #4, then st.rerun() from this line, then render
        # #5 with a fresh map and no popup on it. Freeze does not save you
        # here -- last_object_clicked still came back -- and neither did the
        # _click_popup guard below, which assumed "markers have popups, cells
        # don't". Leases have popups; this click simply returned no popup
        # TEXT, so the guard read it as a bare coordinate click.
        #
        # Requiring the layer to be on is the honest gate, and it is the same
        # switch that decides whether the hexagons are drawn at all.
        if (_cells_mode and _coord_click and _cell_steps
                and st.session_state.get("h3_layer_on")
                and st.session_state.get("grid_visible", True)
                and not _click_popup
                and not _handled_as_cell):
            try:
                _click_lat = float(_coord_click.get("lat"))
                _click_lon = float(_coord_click.get("lng"))
            except (TypeError, ValueError, AttributeError):
                _click_lat = _click_lon = None

            if _click_lat is not None and _click_lon is not None:
                # For each active grid source, compute the cell that contains
                # this click. The "cell" is identified by its SW corner from
                # floor-division. Same algorithm the grid query uses to bin.
                # If multiple sources are active (All regions), we use the
                # FINER grid since its cells are smaller — that maps cleanly
                # to a single physical clicked rectangle.
                _best_step = min(s[1] for s in _cell_steps)
                _gc_lat = (_click_lat // _best_step) * _best_step
                _gc_lon = (_click_lon // _best_step) * _best_step
                _gc_sig = f"{_gc_lat:.4f}|{_gc_lon:.4f}"

                # Dedupe: streamlit-folium keeps returning the same click
                # coordinates across reruns until something else is clicked.
                # Without this guard we'd toggle on every rerun.
                if st.session_state.get("_last_grid_click") != _gc_sig:
                    st.session_state["_last_grid_click"] = _gc_sig

                    # Toggle this cell in/out of the selection buffer.
                    # We use a placeholder well count of 0 — the actual
                    # count isn't needed here; Commit reads the cell bbox
                    # from the (lat, lon) and queries fresh.
                    _sel = list(st.session_state.get("selected_cells", []))
                    _existing_idx = next(
                        (i for i, c in enumerate(_sel)
                         if f"{c[0]:.4f}|{c[1]:.4f}" == _gc_sig),
                        None,
                    )
                    if _existing_idx is not None:
                        _sel.pop(_existing_idx)
                    else:
                        _sel.append((_gc_lat, _gc_lon, 0))
                    st.session_state["selected_cells"] = _sel
                    _handled_as_cell = True
                    st.rerun()

        # If the click wasn't a cell click, fall through to popup-based
        # well marker handling.
        clicked = map_data.get("last_object_clicked_popup") if map_data else None
        if clicked and not _handled_as_cell:
            _clicked_str = str(clicked)
            # A REFERENCE WELL IS NOT A LOADED WELL. This identifies a
            # dv_well click by finding a 14-digit UWI in the popup text, and
            # the reference-well popup carries uwi14 -- so clicking one was
            # read as a dv_well click and sent on to the scout builder for a
            # well that is very likely not in dv_well. It is a MASTER HEADER:
            # it has no logs, no tops, no production, and nothing downstream
            # can answer for it. Seed it into dv_well first (the map panel
            # does exactly that) and then it is a well like any other.
            if "Reference well" in _clicked_str:
                _say("[map] reference-well click ignored -- not a dv_well")
                clicked = None
                _clicked_str = ""

            # GOM wells: this version of streamlit-folium strips HTML
            # attributes from the popup and returns only visible text, so
            # data-well-id never survives the round trip. The GOM popup
            # text does include a line "API <number>", and the BOEM API
            # number is unique per well — so we parse that out and look
            # the well up in viewport_gom_wells by api_well_number. From
            # the matched dict we get the real well_id (UUID) and every
            # field, then shadow-cache it in tray_well_data keyed by
            # well_id so uwi_index and the scout panel can find it.
            _uwi = None
            _gom_api_match = (
                re.search(r'\bAPI\s+(\d{8,16})\b', _clicked_str)
                if "gom" in active_area.get("sources", [])
                else None
            )
            if _gom_api_match:
                _gom_api = _gom_api_match.group(1).strip()
                _gom_pool = st.session_state.get("viewport_gom_wells", [])
                _gom_hit = next(
                    (w for w in _gom_pool
                     if str(w.get("api_well_number", "")).strip() == _gom_api),
                    None,
                )
                if _gom_hit is not None:
                    _uwi = str(_gom_hit.get("well_id", "")).strip()
                    if _uwi:
                        _shadow = st.session_state.get("tray_well_data", {})
                        # Key by well_id (the UUID); preserve every GOM
                        # field for the scout builder; tag the source so
                        # the scout panel dispatches to the GOM builder.
                        _shadow[_uwi] = {**_gom_hit, "uwi": _uwi,
                                         "_source": "gom"}
                        st.session_state["tray_well_data"] = _shadow

            # dv_well wells: data-uwi attribute, then digit-pattern
            # fallbacks. Only run if the GOM branch didn't already
            # resolve a well_id.
            if _uwi is None:
                # Primary: data-uwi attribute (works on older streamlit-folium
                # that returns full popup HTML)
                m2 = re.search(r'data-uwi="([^"]+)"', _clicked_str)
                if m2:
                    _uwi = m2.group(1).strip()
                else:
                    # Fallbacks: try several patterns for different popup
                    # formats (older HTML-preserving vs newer text-only
                    # streamlit-folium)
                    for pat in [
                        # HTML-preserving: monospace span around UWI
                        r"font-family:monospace[^>]*>([^<]+)<",
                        # KGS UWI format: literal "KGS_" + 6-12 digit KID.
                        # Must appear before the generic digit patterns
                        # below, otherwise the 10-digit KID would not match
                        # (those patterns require 12+ consecutive digits).
                        r"(KGS_\d{6,12})",
                        # 14-digit UWI surrounded by whitespace (KS, TX RRC).
                        # The popup title and UWI may be on the same line in
                        # streamlit-folium's plain-text return, so we can't
                        # require start/end of line.
                        r"(?<!\d)(\d{14})(?!\d)",
                        # 12-16 digit UWI, more permissive — only used if 14
                        # didn't match (rare format variations).
                        r"(?<!\d)(\d{12,16})(?!\d)",
                        # Dashed UWI format (e.g., "15-009-00865-0000")
                        r"(\d{2}-\d{3}-\d{5}-\d{2,4}(?:-\d{2})?)",
                        # PPDM US-prefix format
                        r"(US[0-9]{14})",
                    ]:
                        m2 = re.search(pat, _clicked_str)
                        if m2:
                            _uwi = m2.group(1).strip()
                            break

            # Clicks no longer accumulate a selection — the result set is
            # driven entirely by the filter/draw query. A marker click just
            # shows its native popup; it is not added to Results.
            _ = _uwi
        # -- Seismic click -> remember which SEG-Y was picked ------------
        # A LINE YOU CAN SEE BUT NOT OPEN IS A PICTURE, NOT A CATALOGUE.
        # The popup already names the source file; this turns that into the
        # file itself. Both cached, so re-calling them here is a dict lookup.
        #
        # DEDUPE ON THE PATH. streamlit-folium returns the same
        # last_object_clicked_popup on every rerun until something else is
        # clicked, so without this the panel would re-read the file (and a
        # 3D volume is gigabytes) on every interaction with the page.
        if clicked:
            try:
                _sh = _seis_pick_from_popup(
                    clicked, _seismic_line_paths(engine), _qry_seismic_3d(engine))
            except Exception as _se:
                _sh = None
                print(f"[seis_pick] {_se}")
            if _sh and (st.session_state.get("_seis_pick") or {}).get("path") != _sh["path"]:
                # NO st.rerun() HERE. st_folium has already triggered this
                # run by returning the click, and the panel renders below
                # in this same pass -- so a rerun would only rebuild and
                # re-serialise the whole map a second time, greying the
                # page twice for one click.
                st.session_state["_seis_pick"] = _sh
                # AND KEEP IT, so several lines can be collected by
                # clicking them in turn. The picker mode lives in the
                # browser -- that is what makes arming free -- so Python
                # cannot know whether this click meant "add" or "replace".
                # It always ADDS and the panel offers Clear, which is the
                # only version where the two halves cannot disagree about
                # what is selected.
                #
                # DEDUPED ON PATH, and the guard above already ensures a
                # repeated click on the same line never reaches here --
                # streamlit-folium returns the same popup on every rerun
                # until something else is clicked, so without both a
                # single click would append on every interaction.
                _multi = list(st.session_state.get("_seis_multi") or [])
                if not any(str(x.get("path")) == str(_sh.get("path"))
                           for x in _multi):
                    _multi.append(dict(_sh))
                    st.session_state["_seis_multi"] = _multi


        # scout_uwi (set from popups) is no longer auto-collected into a tray.
        if st.session_state.get("scout_uwi"):
            st.session_state.scout_uwi = None

        # -- The picked SEG-Y, opened in the shared file viewer ----------
        # Only when seismic is in play: the chooser is useless otherwise,
        # and both queries would run for a user who never asked for it.
        if "geo_seismic" in active_db or st.session_state.get("_seis_pick"):
            try:
                _render_seis_pick(_seismic_line_paths(engine),
                                  _qry_seismic_3d(engine))
            except Exception as _spe:
                print(f"[seis_panel] {_spe}")

        # ── Over-cap drill results ──────────────────────────────────────
        # Drills that returned more than _TRAY_AUTO_ADD_CAP wells stash their
        # results in _pending_drill_wells. The result set just takes the first
        # _TRAY_AUTO_ADD_CAP automatically — same cap the filter auto-route uses.
        _pending = st.session_state.get("_pending_drill_wells")
        if _pending:
            _add_drill_results_to_tray(_pending[:_TRAY_AUTO_ADD_CAP], replace=True)
            st.session_state.pop("_pending_drill_wells", None)
            st.session_state.pop("_pending_drill_label", None)


        # ── Results — the current query / draw IS the object set ─────────
        # Results = the drilled/clicked set, else the current viewport /
        # draw selection, so Documents / Export reflect the wells chosen
        # on the map (viewport toggle or a drawn box).
        result_uwis = (list(st.session_state.get("clicked_uwis") or [])
                       or list(st.session_state.get("viewport_uwis") or []))
        _n = len(result_uwis)
        st.markdown(f"#### 📋 Results — {_n} well(s)" if result_uwis
                    else "#### 📋 Results")

        if not result_uwis:
            st.markdown(
                "<div style='padding:6px 0'>"
                "<span style='color:#aaa;font-size:12px'>— No wells —&nbsp;&nbsp;"
                "Filter the wells or draw a circle / rectangle on the map to "
                "build a result set.</span></div>",
                unsafe_allow_html=True)
        else:
            _res_mode = st.radio(
                "Results view", ["🛢 Wells", "📄 Documents"],
                horizontal=True, key="results_mode:v1",
                label_visibility="collapsed")

            # PICKING "Documents" GOES TO THE DOCUMENTS PAGE.
            #
            # It used to render a scannable table here plus an "Open in
            # Documents page →" button underneath — two clicks to reach the
            # thing the radio already named, and a preview of the page you
            # were about to open. The radio is a destination, so treat it
            # as one.
            #
            # _render_results_documents is left in place, unused: it is a
            # perfectly good compact list and may be wanted somewhere that
            # is NOT a navigation control.
            if _res_mode == "📄 Documents":
                st.session_state["selected_entities"] = [
                    {"type": "well", "id": _u, "name": _u}
                    for _u in result_uwis]
                st.session_state["wm_docs_page"] = True
                st.session_state["_export_scroll_pending"] = True
                st.rerun()

            selected_in_results = []
            if _res_mode == "🛢 Wells":
                # Tray grid — tick wells to scope Scout Tickets / Documents.
                _grid_rows = []
                for cu in list(result_uwis):
                    well = uwi_index.get(cu, {})
                    _wn_base = well.get("well_name") or cu
                    _wn_sfx  = well.get("well_name_suffix") or ""
                    wn = (f"{_wn_base} {_wn_sfx}".strip() if _wn_sfx else _wn_base)
                    op = (well.get("operator_name") or well.get("company_name") or "")
                    _grid_rows.append({"Select": False, "UWI": str(cu),
                                       "Well": wn, "Operator": op})
                # THE GRID LIVES IN A FORM, AND THAT IS THE WHOLE POINT.
                # A data_editor outside a form reruns the entire page on every
                # cell change — so on a long result set, ticking ten wells
                # re-rendered the map ten times and each tick fought the
                # redraw. Inside a form, edits accumulate in the browser and
                # nothing reruns until a submit button is pressed.
                #
                # Selection therefore has to live somewhere that survives the
                # rerun: st.session_state["tray_selected_uwis"], keyed by UWI
                # rather than by row, so it stays correct when the result set
                # changes underneath it.
                _sel_key = "tray_selected_uwis"
                _all_uwis = [str(r["UWI"]) for r in _grid_rows]
                # Intersect with what is actually on screen: a stale tick from
                # a previous draw must not silently scope Documents to a well
                # that is no longer in the results.
                _selected = [u for u in (st.session_state.get(_sel_key) or [])
                             if u in set(_all_uwis)]

                # The editor key carries a nonce so Select all / Clear can
                # re-default it. Streamlit refuses a write to a widget's own
                # key once that widget exists, so those buttons CANNOT just
                # assign the ticks — they record the new selection, bump the
                # nonce, and rerun; this rebuild then seeds the frame from it.
                # The key still ENDS IN ':sel' because _is_action_key() keys
                # off that suffix — 'tray_grid:v2:sel', never 'tray_grid:sel:v2'.
                _nonce = st.session_state.get("tray_grid_nonce", 0)

                _grid_df = pd.DataFrame(_grid_rows)
                _grid_df["Select"] = _grid_df["UWI"].isin(set(_selected))

                with st.form(key=f"tray_form_{_nonce}", border=False):
                    _tray_edit = st.data_editor(
                        _grid_df,
                        column_config={"Select": st.column_config.CheckboxColumn(
                            "Select", width="small")},
                        disabled=["UWI", "Well", "Operator"],
                        hide_index=True, use_container_width=True,
                        height=min(360, 40 + 35 * max(1, len(_grid_df))),
                        key=f"tray_grid:{_nonce}:sel",
                    )
                    _f1, _f2, _f3, _f4 = st.columns([2, 1, 1, 3])
                    _apply = _f1.form_submit_button(
                        "✓ Apply selection", type="primary",
                        use_container_width=True)
                    # Select all / Clear are SUBMIT buttons, not plain ones, so
                    # pressing either still harvests the form first — a plain
                    # button would discard whatever was ticked but not applied.
                    _pick_all = _f2.form_submit_button(
                        f"All {len(_all_uwis)}", use_container_width=True)
                    _pick_none = _f3.form_submit_button(
                        "None", use_container_width=True)
                    _f4.markdown(
                        f"<div style='font-size:11px;color:#888;padding:9px 0 0 6px'>"
                        f"{len(_selected)} of {len(_all_uwis)} selected</div>",
                        unsafe_allow_html=True)

                if _pick_all or _pick_none:
                    st.session_state[_sel_key] = _all_uwis if _pick_all else []
                    st.session_state["tray_grid_nonce"] = _nonce + 1
                    st.rerun()
                if _apply:
                    _selected = [str(r["UWI"]) for _, r in _tray_edit.iterrows()
                                 if bool(r["Select"])]
                    st.session_state[_sel_key] = _selected
                    # Repaint once so the count and the buttons below agree with
                    # what was just applied. The "N of M selected" caption is
                    # drawn INSIDE the form, i.e. before this handler runs, so
                    # without this it reports the previous selection and reads
                    # like the Apply did nothing. One rerun per Apply is the
                    # deliberate cost; the point of the form is that ticking
                    # itself no longer causes one.
                    st.rerun()

                selected_in_results = list(_selected)
                st.markdown(
                    "<div style='font-size:11px;color:#555;padding:4px 0 6px 0'>"
                    "<b>Export</b> sends <b>all</b> results. Tick wells and press "
                    "<b>Apply selection</b> to scope <b>Documents</b> / "
                    "<b>Scout Tickets</b> to just those — ticking no longer "
                    "reloads the map on every box."
                    "</div>",
                    unsafe_allow_html=True)
            # (no else: picking "Documents" navigated away above, so this
            # branch could only ever be reached by a third radio option
            # that does not exist. Leaving a second Documents path here is
            # how the next person ends up debugging the wrong one.)

            #   Scout Tickets → picked wells only
            #   Documents     → picked wells (fallback all results)
            #   Export        → all results
            p1, p2, p3, p4 = st.columns(4)
            with p1:
                if st.button("📋 Scout Tickets",
                             key="view_summary",
                             use_container_width=True, type="primary",
                             disabled=not selected_in_results):
                    st.session_state["show_summary"] = True
                    st.session_state["_summary_uwis"] = selected_in_results
                    st.rerun()
            with p2:
                _docs_uwis = selected_in_results or result_uwis
                if st.button("📄 Documents", key="open_docs_btn",
                             use_container_width=True,
                             disabled=not _docs_uwis):
                    # Entity-aware selection the documents page consumes. Wells
                    # now; seismic/fields can be added as other entity types.
                    st.session_state["selected_entities"] = [
                        {"type": "well", "id": _u, "name": _u}
                        for _u in _docs_uwis
                    ]
                    st.session_state["wm_docs_page"] = True
                    st.session_state["_export_scroll_pending"] = True
                    st.rerun()
            with p3:
                if st.button("📊 Export", key="export_xlsx_btn",
                             use_container_width=True,
                             disabled=not result_uwis):
                    st.session_state["wm_export_page"] = True
                    # Scroll to the top once on entry — the Export button sits
                    # at the bottom of the page, and Streamlit keeps the scroll
                    # position across the rerun, so the export page would
                    # otherwise open scrolled to the bottom.
                    st.session_state["_export_scroll_pending"] = True
                    st.rerun()
            with p4:
                if st.button("🗑 Clear", key="clear_tray",
                             use_container_width=True):
                    st.session_state.clicked_uwis = []
                    st.session_state.scout_uwi    = None
                    st.session_state["show_summary"] = False
                    st.session_state["_summary_uwis"] = []
                    st.session_state["_auto_tray_uwis"] = []
                    # Also clear viewport markers and the drawing dedupe set
                    st.session_state["viewport_uwis"] = []
                    st.session_state["processed_drawings"] = set()
                    # Clear grid-click dedupe so the same cell can be
                    # re-clicked, and the drawn bounds so the map
                    # repositions correctly next time
                    st.session_state.pop("_last_grid_click", None)
                    st.session_state.pop("_drawn_bounds", None)
                    st.session_state.pop("_active_drill_bbox", None)
                    # Also clear the multi-cell selection buffer, and
                    # bring the grid back so the user can pick again.
                    st.session_state["selected_cells"] = []
                    st.session_state["grid_visible"] = True
                    st.session_state.pop("grid_visible_toggle", None)
                    st.rerun()

        # ── Scout Ticket panel — renders below the Object Tray ──────────
        _summary_uwis = st.session_state.get("_summary_uwis", [])
        if st.session_state.get("show_summary") and _summary_uwis:
            # Cache HTML — only rebuild when selection changes
            cache_key = tuple(_summary_uwis)
            if st.session_state.get("_summary_cache_key") != cache_key:
                _html = ""
                for uwi in _summary_uwis:
                    well_row = uwi_index.get(uwi)
                    if not well_row:
                        continue
                    # Dispatch by identifier shape: GOM wells are keyed by
                    # a UUID well_id (36 chars, dashed); dv_well wells use
                    # PPDM-style UWIs. A dict tagged _source="gom" (set by
                    # the GOM popup-click handler) is the explicit signal;
                    # the UUID-shape check is the fallback.
                    _is_gom = (
                        well_row.get("_source") == "gom"
                        or (isinstance(uwi, str)
                            and len(uwi) == 36
                            and uwi.count("-") == 4)
                    )
                    if _is_gom:
                        _html += _build_gom_scout_ticket_html(uwi, well_row, engine)
                    else:
                        _html += _build_scout_ticket_html(uwi, well_row, engine)
                    _html += "<div style='page-break-after:always'></div>"
                st.session_state["_summary_html"]      = _html
                st.session_state["_summary_cache_key"] = cache_key

            all_html = st.session_state.get("_summary_html", "")
            full_doc = _full_html_doc(all_html, f"Scout Tickets — {len(_summary_uwis)} wells")
            fn       = f"Scout_Tickets_{len(_summary_uwis)}_wells.html"

            _hdr = ("Scout Ticket" if len(_summary_uwis) == 1
                    else f"Scout Tickets — {len(_summary_uwis)} wells")
            st.markdown(f"#### 📋 {_hdr}")
            b1, bp, b2, _ = st.columns([1, 1, 1, 3])
            b1.download_button(
                "⬇ Save Report", data=full_doc.encode(),
                file_name=fn, mime="text/html",
                key="save_report_dl", use_container_width=True)

            # ── PDF, generated not printed ──────────────────────────────────
            # _build_batch_pdf() has existed since the multi-well panel was
            # written and was never wired to a button, so the only route to a
            # PDF was "Save Report" -> open the HTML -> browser Print. That
            # path (and Windows' "Microsoft Print to PDF" in particular)
            # flattens the text to vector outlines: the file looks right and
            # has ZERO extractable characters, so the File Catalog can't read
            # a single field out of it. WeasyPrint writes a real text layer.
            #
            # Cached in session_state against the same cache_key the HTML uses,
            # so switching wells invalidates it and re-rendering the page
            # doesn't regenerate a PDF nobody asked for.
            _pdf_key = f"_summary_pdf_{cache_key}"
            if bp.button("⬇ PDF", key="summary_pdf_btn",
                         use_container_width=True,
                         help="Generate a PDF with a real text layer. Prefer "
                              "this over printing the saved HTML — printed "
                              "PDFs contain no searchable or extractable text."):
                with st.spinner(f"Rendering {len(_summary_uwis)} ticket(s)…"):
                    _pdf, _err = _scout_ticket_pdf(
                        all_html, f"Scout Tickets — {len(_summary_uwis)} wells",
                        return_error=True)
                st.session_state[_pdf_key] = _pdf
                st.session_state[f"{_pdf_key}_err"] = _err
            if st.session_state.get(_pdf_key):
                st.download_button(
                    "📄 Save PDF", data=st.session_state[_pdf_key],
                    file_name=f"Scout_Tickets_{len(_summary_uwis)}_wells.pdf",
                    mime="application/pdf", key="summary_pdf_dl")
            elif st.session_state.get(f"{_pdf_key}_err"):
                st.error(st.session_state[f"{_pdf_key}_err"])

            if b2.button("✕ Close", key="close_summary", use_container_width=True):
                st.session_state["show_summary"] = False
                st.session_state["_summary_uwis"] = []
                st.rerun()

            st.markdown(all_html, unsafe_allow_html=True)
            # The ticket's photos are thumbnails and always will be; this is
            # where the full-size ones live. See _render_photo_gallery.
            try:
                for _u in _summary_uwis[:1]:
                    _render_mud_log(engine, _u)
            except Exception as _mlexc:
                st.caption("Mud log unavailable: %s" % _mlexc)
            try:
                _render_photo_gallery(engine, _summary_uwis)
            except Exception as _galexc:
                st.caption("Core photo gallery unavailable: %s" % _galexc)
            if st.button("✕ Close scout ticket", key="close_summary_bottom",
                         use_container_width=True):
                st.session_state["show_summary"] = False
                st.session_state["_summary_uwis"] = []
                st.rerun()
            st.markdown("---")


# Wrap the query and layer functions. MUST BE LAST: it walks globals(),
# so every _qry_/_add_ has to be defined before it runs.
_install_timers()
_install_rerun_trace()
