"""
app_v3.py  —  DataView v3
=========================
Clean scaffolding built around the DataView schema and ML-powered imports.
Replaces the PPDM 3.9 pipeline-centric v2 with a data-domain-centric v3.

Navigation is card-based on the splash screen.
All heavy modules loaded lazily after connect.

Dialect support: SQL Server · Oracle · Snowflake
"""

from __future__ import annotations

# ── THIS REPO WINS, whatever launched us ────────────────────────────────────
# There are two copies of this code: this repo, and the deployed one under
# C:\Program Files\Data Wrangler v4\app. The installed interpreter is an
# EMBEDDED build whose sys.path carries that deployed app directory but NOT
# the script's own folder — so launching THIS file with THAT python imported
# `dataview` from the DEPLOYMENT. It is silent: the app runs, and every edit
# made here appears to have no effect. The same trap gave
# check_mirror_registry five phantom LINEAGE failures against a deployed copy
# ten entries behind.
#
# start.bat still defaults PY to that embedded interpreter, so this is not
# hypothetical. Inserting our own root at position 0 makes the question moot:
# whichever python runs this file, the modules beside it are the ones that
# load. Cheap, and it removes a whole class of "why didn't my change take".
import os
import sys as _sys
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _sys.path[:1] != [_ROOT]:
    while _ROOT in _sys.path:
        _sys.path.remove(_ROOT)
    _sys.path.insert(0, _ROOT)

import streamlit as st

# ── module trace probe (temporary) ──
import sys as _sys, os as _os, atexit as _atexit
def _dump_loaded_modules():
    base = _os.path.dirname(_os.path.abspath(__file__)).lower()
    seen = set()
    for _name, _mod in list(_sys.modules.items()):
        fp = getattr(_mod, "__file__", None)
        if not fp:
            continue
        ap = _os.path.abspath(fp); low = ap.lower()
        if base in low and "\\venv\\" not in low and "\\__pycache__\\" not in low:
            seen.add(_os.path.relpath(ap, base))
    with open(_os.path.join(base, "loaded_modules.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(seen)) + "\n")
_atexit.register(_dump_loaded_modules)
# ── end probe ─

# THE LEASE WINDOW OPENS AS A 520px STRIP, and Streamlit turns the sidebar
# into an OVERLAY DRAWER below ~768px rather than a column -- so in that
# window an expanded sidebar does not sit beside the panel, it covers it
# completely. Measured at 520x900: the panel was entirely hidden behind nav
# and Connect until the drawer was collapsed by hand, every time the window
# was opened.
#
# initial_sidebar_state applies to the FIRST render of a session, which is
# exactly the right scope: the drawer is still there for the one Connect that
# window needs, it just does not start on top of the thing it came for.
_view = ""
try:
    _view = str(st.query_params.get("view") or "")
except Exception:
    # Older Streamlit, or no params at all. An expanded sidebar is the
    # correct fallback -- it is what every other page wants.
    _view = ""

st.set_page_config(
    page_title="DataView",
    page_icon="🛢",
    layout="wide",
    initial_sidebar_state=("collapsed" if _view == "lease" else "expanded"),
)

# ═══════════════════════════════════════════════════════════════════════
# THEME SYSTEM
# ═══════════════════════════════════════════════════════════════════════

THEMES = {
    "Default (Teal)": {
        "bg":          "#0a8a96",
        "bg_mid":      "#0d8f9e",
        "sidebar":     "#0a7a8a",
        "primary":     "#1ab8c4",
        "primary_dark":"#0d8a94",
        "accent":      "#c4915a",
        "text":        "#e8f4f5",
        "text_dim":    "#7aacb5",
        "card_shadow": "rgba(26,184,196,0.15)",
        "conn_ok":     "#3dd68c",
        "conn_err":    "#f87171",
        "desc":        "Default teal palette from the Data Wrangler logo",
    },
    "High Contrast": {
        "bg":          "#000000",
        "bg_mid":      "#1a1a1a",
        "sidebar":     "#111111",
        "primary":     "#ffff00",
        "primary_dark":"#cccc00",
        "accent":      "#ff8c00",
        "text":        "#ffffff",
        "text_dim":    "#cccccc",
        "card_shadow": "rgba(255,255,0,0.2)",
        "conn_ok":     "#00ff00",
        "conn_err":    "#ff4444",
        "desc":        "WCAG AAA — maximum contrast for low vision",
    },
    "Deuteranopia (Blue/Orange)": {
        "bg":          "#1a2a4a",
        "bg_mid":      "#1e3255",
        "sidebar":     "#152038",
        "primary":     "#5b9bd5",
        "primary_dark":"#3a7abf",
        "accent":      "#f28c28",
        "text":        "#f0f4ff",
        "text_dim":    "#a0b4d0",
        "card_shadow": "rgba(91,155,213,0.2)",
        "conn_ok":     "#5b9bd5",
        "conn_err":    "#f28c28",
        "desc":        "Safe for green-blind (deuteranopia) — uses blue/orange only",
    },
    "Protanopia (Blue/Yellow)": {
        "bg":          "#1a1a3a",
        "bg_mid":      "#222250",
        "sidebar":     "#141430",
        "primary":     "#6699ff",
        "primary_dark":"#4477dd",
        "accent":      "#ffcc00",
        "text":        "#f0f0ff",
        "text_dim":    "#9999cc",
        "card_shadow": "rgba(102,153,255,0.2)",
        "conn_ok":     "#6699ff",
        "conn_err":    "#ffcc00",
        "desc":        "Safe for red-blind (protanopia) — uses blue/yellow only",
    },
    "Light / Print": {
        "bg":          "#f5f7fa",
        "bg_mid":      "#e8ecf0",
        "sidebar":     "#dde3ea",
        "primary":     "#0066cc",
        "primary_dark":"#004499",
        "accent":      "#cc6600",
        "text":        "#1a1a2e",
        "text_dim":    "#555577",
        "card_shadow": "rgba(0,102,204,0.12)",
        "conn_ok":     "#007700",
        "conn_err":    "#cc0000",
        "desc":        "Light background — good for printing and bright rooms",
    },
    "Midnight Gold": {
        "bg":          "#0e0e10",
        "bg_mid":      "#17171a",
        "sidebar":     "#0a0a0c",
        "primary":     "#ffd400",
        "primary_dark":"#2b2b31",
        "accent":      "#e0a829",
        "text":        "#ffffff",
        "text_dim":    "#ffffff",
        "card_shadow": "rgba(255,212,0,0.25)",
        "conn_ok":     "#3dd68c",
        "conn_err":    "#f87171",
        "desc":        "Midnight slate with gold accents and white text — matches the export cards",
    },
}

def _inject_theme(t: dict):
    """Inject theme CSS with !important to beat Streamlit defaults."""
    bg      = t['bg']
    bg_mid  = t['bg_mid']
    sidebar = t['sidebar']
    primary = t['primary']
    pri_dk  = t['primary_dark']
    accent  = t['accent']
    text    = t['text']
    txt_dim = t['text_dim']
    c_ok    = t['conn_ok']
    c_err   = t['conn_err']
    shadow  = t['card_shadow']
    st.markdown(f"""
    <style>
    :root {{
        --teal:       {primary};
        --teal-dark:  {pri_dk};
        --teal-dim:   {primary}22;
        --sandy:      {accent};
        --sandy-dim:  {accent}22;
        --navy:       {bg};
        --navy-mid:   {bg_mid};
        --navy-light: {primary};
        --sidebar:    {sidebar};
        --text:       {text};
        --text-dim:   {txt_dim};
    }}
    html, body, .stApp,
    [class*="css"],
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    .main, .block-container {{
        background: {bg} !important;
        color: {text} !important;
    }}
    [data-testid="stSidebar"],
    [data-testid="stSidebar"] > div {{
        background: {sidebar} !important;
    }}
    [data-testid="stSidebar"] * {{ color: {text} !important; }}
    [data-testid="stHeader"]  {{ background: {bg} !important; }}
    [data-testid="stToolbar"] {{ background: {bg} !important; }}
    /* ── TOP PADDING, SET HERE AND NOT IN A PAGE ──────────────────────
       Streamlit gives stMainBlockContainer 6rem (96px) of top padding.
       The map page used to override it ~90 lines into its own run(), so
       every render showed 96px until the script reached that line -- a
       first launch is seconds of it, a warm revisit is imperceptible.
       Reported exactly that way: "pushed down on first launch, fine if I
       start Mapping again".

       The shell runs before any page dispatch, so the rule is in force
       from the first paint and on every rerun, whatever page is open. */
    /* 64px top, not the stock 6rem/96px -- see above. The bottom is stock
       10rem (160px), which is dead space under the last control on every
       page; 48px still keeps it off the viewport edge. */
    [data-testid="stMainBlockContainer"] {{ padding-top: 64px !important;
                                            padding-bottom: 48px !important; }}

    /* An injected <style> is a real child of the flex column and that
       column has a 16px gap, so each CSS-only element costs 16px while
       rendering nothing. :only-child matches a markdown block that is
       NOTHING BUT a style tag; anything with visible content is left
       alone. Hiding the container does not disable the rules -- a <style>
       is never rendered and applies regardless of an ancestor display. */
    [data-testid="stElementContainer"]:has(
        [data-testid="stMarkdownContainer"] > style:only-child) {{
        display: none !important;
    }}
    [data-testid="stDecoration"] {{ background: {bg} !important; }}
    button[kind="primary"] {{
        background: {pri_dk} !important;
        border-color: {primary} !important;
        color: {text} !important;
    }}
    button[kind="primary"]:hover {{ background: {primary} !important; }}
    .conn-pill.connected    {{ color:{c_ok};  border-color:{c_ok}44;  background:{bg_mid}; }}
    .conn-pill.disconnected {{ color:{c_err}; border-color:{c_err}44; background:{bg_mid}; }}
    div[data-testid="stHorizontalBlock"]
    div[data-testid="stVerticalBlockBorderWrapper"] {{
        background: {bg_mid} !important;
        border: 1px solid {primary} !important;
        position: relative;
    }}
    div[data-testid="stHorizontalBlock"]
    div[data-testid="stVerticalBlockBorderWrapper"]:hover {{
        border-color: {primary} !important;
        box-shadow: 0 8px 24px {shadow};
    }}

    /* ── Scrollbars ──────────────────────────────────────────────────
       Chrome/Edge default to a thin, near-invisible bar against a dark
       background — on #0e0e10 the stock thumb is almost the same value as
       the track. Wider, and coloured from the theme rather than hardcoded,
       so it stays right when the theme changes. */
    ::-webkit-scrollbar {{
        width: 14px;
        height: 14px;
    }}
    ::-webkit-scrollbar-track {{
        background: {bg_mid} !important;
        border-radius: 7px;
    }}
    ::-webkit-scrollbar-thumb {{
        background: {accent} !important;
        border-radius: 7px;
        /* a track-coloured border insets the thumb: full 14px hit target,
           slimmer visible bar, so it reads as a control and not a slab */
        border: 3px solid {bg_mid};
        background-clip: padding-box;
    }}
    ::-webkit-scrollbar-thumb:hover {{
        background: {primary} !important;
        background-clip: padding-box;
    }}
    ::-webkit-scrollbar-corner {{ background: {bg_mid} !important; }}
    /* Firefox — no pseudo-elements, only these two properties */
    * {{
        scrollbar-width: auto;
        scrollbar-color: {accent} {bg_mid};
    }}
    </style>
    """, unsafe_allow_html=True)


# ── Global CSS — layout and fonts only, NO hardcoded colors ─────────
# All colors come from CSS variables set by _inject_theme()
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
    background: var(--navy) !important;
    color: var(--text) !important;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background: var(--sidebar) !important;
    border-right: 1px solid var(--teal-dark) !important;
}
[data-testid="stSidebar"] * { color: var(--text) !important; }
[data-testid="stSidebar"] .stButton button {
    background: transparent;
    border: 1px solid var(--navy-light);
    color: var(--text) !important;
    text-align: left;
    border-radius: 6px;
    transition: all 0.15s;
    font-size: 13px;
}
[data-testid="stSidebar"] .stButton button:hover {
    background: var(--navy-mid);
    border-color: var(--teal);
    color: var(--teal) !important;
}
[data-testid="stSidebar"] .stButton button[kind="primary"] {
    background: var(--teal-dark);
    border-color: var(--teal);
    color: #fff !important;
}

/* Main area background */
[data-testid="stAppViewContainer"] > .main {
    background: var(--navy) !important;
}
[data-testid="stHeader"] { background: var(--navy) !important; }

/* Nav cards */
div[data-testid="stHorizontalBlock"] div[data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--navy-mid) !important;
    border: 1px solid var(--navy-light) !important;
    border-radius: 12px !important;
    transition: border-color 0.2s, transform 0.15s, box-shadow 0.2s;
}
div[data-testid="stHorizontalBlock"] div[data-testid="stVerticalBlockBorderWrapper"]:hover {
    border-color: var(--teal) !important;
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(26,184,196,0.15);
}
/* The nav-card frame above is for main-area cards only. Column layouts in the
   sidebar (Database + 🔄 row, Connect/Demo, etc.) are also stHorizontalBlocks,
   so the rule leaks a second gold frame around those widgets. Scope it out of
   the sidebar — higher specificity than the rule above so it wins. */
[data-testid="stSidebar"] div[data-testid="stHorizontalBlock"]
        div[data-testid="stVerticalBlockBorderWrapper"] {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    transform: none !important;
    box-shadow: none !important;
}

/* Primary buttons — teal */
button[kind="primary"] {
    background: var(--teal-dark) !important;
    border-color: var(--teal) !important;
    color: #fff !important;
}
button[kind="primary"]:hover {
    background: var(--teal) !important;
}

/* Connection pill */
.conn-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-family: 'IBM Plex Mono', monospace;
    padding: 3px 12px;
    border-radius: 20px;
    margin-bottom: 12px;
}
.conn-pill.connected    { background:#0a2e22; color:#3dd68c; border:1px solid #3dd68c44; }
.conn-pill.disconnected { background:#2e0a0a; color:#f87171; border:1px solid #f8717144; }

/* Section header in sidebar */
.sec-hdr {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin: 20px 0 6px 0;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--navy-light);
}

/* App header */
.dv-header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px 0 8px 0;
    border-bottom: 2px solid var(--teal-dark);
    margin-bottom: 20px;
}
.dv-title {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 42px;
    color: var(--teal);
    letter-spacing: 2px;
    line-height: 1;
    margin: 0;
}
.dv-subtitle {
    font-size: 12px;
    color: var(--sandy);
    font-family: 'IBM Plex Mono', monospace;
    letter-spacing: 1px;
    margin: 0;
}
.dv-badge {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    background: var(--sandy-dim);
    color: var(--sandy);
    padding: 2px 8px;
    border-radius: 20px;
    border: 1px solid var(--sandy);
}

/* ─── Gold outlines: buttons · inputs · grids (appended override) ───────
   Uses --navy-light (= theme primary = gold under Midnight Gold) so it
   tracks the active theme. Remove this whole block to revert.            */
.stButton > button,
.stDownloadButton > button,
.stFormSubmitButton > button {
    border: 1px solid var(--navy-light) !important;
}
/* Input boxes — gold ring via box-shadow: one clean outline, no layout
   shift and no fighting baseweb's internal border. Covers text, number,
   date, textarea, selectbox and multiselect. */
.stTextInput   div[data-baseweb="input"],
.stNumberInput div[data-baseweb="input"],
.stDateInput   div[data-baseweb="input"],
.stTextArea    div[data-baseweb="textarea"],
.stSelectbox   div[data-baseweb="select"],
.stMultiSelect div[data-baseweb="select"],
[data-testid="stSidebar"] div[data-baseweb="input"],
[data-testid="stSidebar"] div[data-baseweb="select"] {
    box-shadow: 0 0 0 1px var(--navy-light) !important;
    border-radius: 6px !important;
}
/* Grid containers — gold outline around the whole grid. The cells render
   on an HTML canvas CSS can't reach, so individual gridlines stay default. */
div[data-testid="stDataFrame"],
div[data-testid="stDataFrameResizable"],
div[data-testid="stDataEditor"],
div[data-testid="stDataEditorContainer"],
div[data-testid="stDataFrameContainer"] {
    border: 1px solid var(--navy-light) !important;
    border-radius: 6px !important;
}
/* ─── Card help "?" badge + hover tooltip ──────────────────────────── */
.card-help {
    position: absolute;
    width: 18px; height: 18px;
    border-radius: 50%;
    border: 1px solid var(--navy-light);
    color: var(--navy-light);
    background: var(--navy-mid);
    font: 700 12px/16px 'IBM Plex Mono', monospace;
    text-align: center;
    cursor: help;
    z-index: 6;
    user-select: none;
}
.card-help::after {
    content: attr(data-help);
    position: absolute;
    top: 22px; right: 0;
    width: max-content; max-width: 240px;
    background: var(--navy-mid);
    color: var(--text);
    border: 1px solid var(--navy-light);
    border-radius: 6px;
    padding: 6px 9px;
    font: 400 11px/1.4 'IBM Plex Sans', sans-serif;
    text-align: left;
    white-space: pre-line;
    box-shadow: 0 6px 18px rgba(0,0,0,.45);
    opacity: 0;
    pointer-events: none;
    transition: opacity .12s ease;
    z-index: 100;
}
.card-help:hover::after { opacity: 1; }

/* ─── Commercial chrome: hide Streamlit's prototype UI ──────────────────
   Removes the Deploy button, hamburger menu, status widget, footer and the
   rainbow decoration bar so the app reads as a product, not a dev tool.    */
#MainMenu,
footer,
[data-testid="stToolbar"],
[data-testid="stToolbarActions"],
[data-testid="stDeployButton"],
[data-testid="stAppDeployButton"],
.stDeployButton,
[data-testid="stStatusWidget"],
[data-testid="stDecoration"] { display: none !important; }

/* ─── Dashboard components — used by the File Catalog / Pipeline page ──── */
.dv-card {
    background: var(--navy-mid);
    border: 1px solid var(--navy-light);
    border-radius: 12px;
    padding: 18px 20px;
    margin-bottom: 16px;
}
.dv-hero-title { font-size: 16px; font-weight: 600; color: var(--text); margin: 0; }
.dv-hero-sub   { font-size: 13px; color: var(--text-dim); margin: 2px 0 0; }

.dv-tiles { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.dv-tile  {
    flex: 1 1 120px;
    background: var(--navy-mid);
    border: 1px solid var(--navy-light);
    border-radius: 10px;
    padding: 12px 14px;
}
.dv-tile .tl {
    font-size: 11px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 1px;
    font-family: 'IBM Plex Mono', monospace;
}
.dv-tile .tn {
    font-size: 30px; line-height: 1.05; margin-top: 4px;
    font-family: 'Bebas Neue', sans-serif; letter-spacing: 1px;
    color: var(--text);
}
.dv-tile.ok   .tn { color: var(--teal); }
.dv-tile.warn .tn { color: var(--sandy); }

.dv-stepper {
    display: flex; align-items: flex-start;
    justify-content: space-between; gap: 2px; margin-top: 14px;
}
.dv-stage { display: flex; flex-direction: column; align-items: center; gap: 6px; min-width: 42px; }
.dv-dot {
    width: 26px; height: 26px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; border: 1px solid var(--navy-light); color: var(--text-dim);
}
.dv-dot.done   { background: var(--teal-dim);  color: var(--teal);  border-color: var(--teal); }
.dv-dot.active { background: var(--sandy-dim); color: var(--sandy); border-color: var(--sandy); }
.dv-lbl {
    font-size: 10px; color: var(--text-dim);
    font-family: 'IBM Plex Mono', monospace;
    text-transform: uppercase; letter-spacing: .5px;
}
.dv-bar { flex: 1; height: 2px; background: var(--navy-light); opacity: .4; margin-top: 12px; }

.dv-pill {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; font-family: 'IBM Plex Mono', monospace;
    padding: 2px 9px; border-radius: 12px;
}
.dv-pill.ok    { background: #0a2e22; color: #3dd68c; border: 1px solid #3dd68c44; }
.dv-pill.warn  { background: #2e240a; color: #e0a52e; border: 1px solid #e0a52e44; }
.dv-pill.muted { background: var(--navy-mid); color: var(--text-dim); border: 1px solid var(--navy-light); }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════
# SESSION STATE
# ═══════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def _teacup_counts_cached(_engine, _ver):
    """Teacup row counts, cached. THE SIDEBAR RENDERS ON EVERY PAGE.

    Profiling a single map render found 157 SQL round trips, and 128 of them
    were this panel: well_counts alone issues one query per child table of
    dv_well -- 54 of them, 1.4 s -- and catalog_counts another 70. Every page
    in the app paid that on every rerun, just to show three numbers in a
    collapsed expander. An expander body executes whether or not it is open,
    so "they only cost when you look" was never true.

    _ver is the cache key, not a value: bump S["_teacup_ver"] after anything
    that changes the counts and the next render recomputes. TTL is a backstop
    for changes made outside this session -- a pipeline run in a terminal.

    Leading underscore on _engine so Streamlit does not try to hash it.
    """
    _tc = _teacup_mod()
    return _tc.counts(_engine) if _tc else {}


def _teacup_mod():
    """tools/demo_teacup, or None. Imported lazily so a missing tool cannot
    stop the app from starting."""
    try:
        # app_v4 imports sys AS _sys, so a bare sys here is unbound -- and the
        # except below would have swallowed the NameError and reported the
        # tool missing forever. Import it locally and name it plainly.
        import os as _o, sys as _s
        _t = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "tools")
        if _t not in _s.path:
            _s.path.insert(0, _t)
        import demo_teacup
        return demo_teacup
    except Exception:
        return None


DEFAULTS = dict(
    engine          = None,
    dialect         = None,       # "mssql" | "oracle" | "snowflake"
    db_label        = "",         # "server/database" display string
    connected       = False,
    app_mode        = "splash",   # current page
    demo            = False,
    theme           = "Midnight Gold",
)

for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

S = st.session_state

# ── SECOND-SCREEN ENTRY ───────────────────────────
# A second browser window is a SECOND SESSION -- its own session_state, so it
# cannot read the map's pick. ?view=seis is how the map hands this window a
# line, and the map re-navigates the SAME named window rather than opening a
# tab per click, so no polling or shared store is needed.
#
# Read BEFORE the nav renders. app_mode drives the nav buttons' `type=` and
# the dispatch below; assigning it after a widget was drawn from it is
# Streamlit scar #7, and the crash would land on whatever page drew next.
try:
    _v = str(st.query_params.get("view") or "").lower()
    if _v == "seis":
        S.app_mode = "seis_view"
    elif _v == "lease":
        # SAME DOOR AS THE SEISMIC SCREEN. ?view=lease opens the lease panel
        # alone, so it can live on a second monitor beside the map.
        S.app_mode = "lease_view"
except Exception:
    pass

# ── Inject theme at top level so CSS applies to entire page ───────────
_inject_theme(THEMES[S.get("theme", "Midnight Gold")])


# ═══════════════════════════════════════════════════════════════════════
# SIDEBAR — connection + nav
# ═══════════════════════════════════════════════════════════════════════

with st.sidebar:
    # Header
    import base64 as _sb64, pathlib as _spl
    _sb_b64 = ""
    for _slp in ["assets/data_wrangler.png", "data_wrangler.png"]:
        if _spl.Path(_slp).exists():
            _sb_b64 = _sb64.b64encode(_spl.Path(_slp).read_bytes()).decode()
            break
    if _sb_b64:
        st.markdown(
            f"<img src=\"data:image/png;base64,{_sb_b64}\" "
            f"style=\"width:100%;max-width:200px;border-radius:8px\"/>",
            unsafe_allow_html=True)
    st.markdown(
        "<div style=\"font-family:\'IBM Plex Mono\',monospace;font-size:11px;"
        "color:#ffffff;font-weight:600;letter-spacing:1px;"
        "margin:6px 0 2px 0\">DATA WRANGLER SOLUTIONS LLC</div>"
        "<div style=\"font-size:10px;color:#b2e8ee;"
        "font-family:\'IBM Plex Mono\',monospace;letter-spacing:1px\">"
        "v4.0 · DataView</div>",
        unsafe_allow_html=True)

    # ── WHICH COPY OF THE CODE IS THIS? ─────────────────────────────────────
    # There are two: this repo, and a deployed copy under
    # `C:\Program Files\Data Wrangler v4\app`. Streamlit puts the LAUNCHED
    # script's folder on sys.path first, so which one you get depends entirely
    # on which app_v4.py was started — and nothing on screen said which.
    #
    # That is not hypothetical. On 16 Aug 2026 a checker run with the packaged
    # interpreter read the deployed copy (ten LINEAGE entries behind) and
    # reported five failures that did not exist in the source. Half an hour
    # went into a bug that was a path.
    #
    # So the app states it. Green when running from the folder this file lives
    # in, red when `dataview` was imported from anywhere else — which always
    # means edits are not taking effect.
    try:
        import dataview as _dv_pkg
        _code_root = _os.path.dirname(_os.path.dirname(
            _os.path.abspath(_dv_pkg.__file__)))
        _here = _os.path.dirname(_os.path.abspath(__file__))
        _same = _os.path.normcase(_code_root) == _os.path.normcase(_here)
        # SILENT WHEN CORRECT. This printed "● repo" and the full path on
        # every single run -- a line long enough to spill past the sidebar's
        # edge, spent saying the ordinary thing. The deployed second copy it
        # watched for was removed 18 Aug 2026, so the green half had nothing
        # left to tell anyone. The alarm is kept: it costs nothing, and if
        # `dataview` ever resolves somewhere else it still says so, loudly.
        if not _same:
            st.warning(
                f"`dataview` is being imported from **{_code_root}**, not from "
                f"the app you launched (**{_here}**). Edits here are not "
                f"running. Launch with the full path to this folder's "
                f"app_v4.py, or remove `..\\app` from python312._pth.")
    except Exception as _e_src:                  # never let a banner break boot
        st.caption(f"(code source unknown: {str(_e_src)[:60]})")

    # THE THEME PICKER, AT THE TOP, UNDER THE LOGO. Its header and rule are
    # kept: they are the framing that makes this read as a labelled group
    # rather than a stray dropdown, and nobody asked for them to go.
    #
    # What is gone is the three-line description that used to sit BELOW the
    # dropdown, restating the name already showing in it -- and, being the
    # last thing before the Reset button, it was the line directly above
    # Reset that was asked about.
    #
    # The theme CSS is injected before the sidebar opens, so nothing here
    # renders unthemed. A second _inject_theme call used to sit at this
    # spot and emit the same <style> block twice per run.
    st.markdown("<div class='sec-hdr'>Appearance</div>", unsafe_allow_html=True)
    theme_choice = st.selectbox(
        "Color theme",
        options=list(THEMES.keys()),
        index=list(THEMES.keys()).index(S.get("theme", "Midnight Gold")),
        key="sb_theme",
        label_visibility="collapsed")
    if theme_choice != S.get("theme"):
        S.theme = theme_choice
        st.rerun()

    # ── Connect panel ─────────────────────────────────────────────────
    with st.expander("🔌 Connect", expanded=not S.connected):
        # Suppress browser autofill / autocomplete popups on the connection
        # form. Chrome will helpfully (annoyingly) suggest previously-entered
        # values when these inputs get focus, and the suggestion box is large
        # enough to obscure the form below it. Setting autocomplete to a
        # nonsense value or "new-password" tells Chrome NOT to suggest stored
        # values. Streamlit doesn't expose autocomplete on st.text_input
        # directly, so we do it via JS targeting only inputs with our sb_*
        # keys.
        st.markdown("""
            <script>
            (function() {
                function suppressAutofill() {
                    var inputs = window.parent.document.querySelectorAll(
                        'input[aria-label="Server"], '       +
                        'input[aria-label="Database"], '     +
                        'input[aria-label="Username"], '     +
                        'input[aria-label="Password"], '     +
                        'input[aria-label="Account"], '      +
                        'input[aria-label="Warehouse"], '    +
                        'input[aria-label="Host:Port/SID"]'
                    );
                    inputs.forEach(function(inp) {
                        // 'new-password' is Chrome's "stop autofilling this" signal
                        inp.setAttribute('autocomplete', 'new-password');
                        inp.setAttribute('autocorrect', 'off');
                        inp.setAttribute('autocapitalize', 'off');
                        inp.setAttribute('spellcheck', 'false');
                    });
                }
                // Run once now, then on a delay (Streamlit may rerender)
                suppressAutofill();
                setTimeout(suppressAutofill, 500);
                setTimeout(suppressAutofill, 1500);
            })();
            </script>
        """, unsafe_allow_html=True)

        dialect = st.selectbox("Dialect",
            ["SQL Server", "Oracle", "Snowflake"], key="sb_dialect")

        if dialect == "SQL Server":
            server   = st.text_input("Server",   value=r"localhost\SQLEXPRESS", key="sb_server")
            win_auth = st.checkbox("Windows Auth", value=True, key="sb_winauth")
            username = password = ""
            if not win_auth:
                username = st.text_input("Username", key="sb_user")
                password = st.text_input("Password", type="password", key="sb_pass")
            driver   = st.selectbox("Driver",
                ["ODBC Driver 17 for SQL Server",
                 "ODBC Driver 18 for SQL Server",
                 "SQL Server"], key="sb_driver")

            # Database: dropdown populated from the server. Click 🔄 to (re)list
            # — connects to master with the settings above and reads
            # sys.databases. Falls back to a text box until listed.
            _db_list = st.session_state.get("sb_db_list")
            _dc, _bc = st.columns([4, 1])
            with _bc:
                st.write("")  # nudge the button down to align with the field
                if st.button("🔄", key="sb_list_dbs",
                             help="List databases on this server"):
                    try:
                        from dataview.core.db import DBConfig, connect as _lc
                        _lr = _lc(DBConfig(
                            server=server, database="master", driver=driver,
                            windows_auth=win_auth,
                            username=username, password=password))
                        if getattr(_lr, "ok", False):
                            from sqlalchemy import text as _lt
                            with _lr.engine.connect() as _lconn:
                                _db_list = [row[0] for row in _lconn.execute(_lt(
                                    "SELECT name FROM sys.databases "
                                    "WHERE database_id > 4 AND state = 0 "
                                    "ORDER BY name")).fetchall()]
                            try:
                                _lr.engine.dispose()
                            except Exception:
                                pass
                            st.session_state["sb_db_list"] = _db_list
                        else:
                            st.warning(getattr(_lr, "message",
                                               "Could not connect to list databases"))
                    except Exception as _le:
                        st.warning(f"Couldn't list databases: {_le}")
            with _dc:
                if _db_list:
                    # DataView_Demo, not DataView: the latter is the older,
                    # essentially empty database (0 rows in dv_well). Falling
                    # back to it made a working install look broken.
                    _idx = (_db_list.index("DataView_Demo")
                            if "DataView_Demo" in _db_list else 0)
                    database = st.selectbox("Database", _db_list, index=_idx,
                                            key="sb_database_sel")
                else:
                    database = st.text_input("Database", value="DataView_Demo",
                                             key="sb_database")

        elif dialect == "Oracle":
            server   = st.text_input("Host:Port/SID",
                                     placeholder="localhost:1521/ORCL", key="sb_server")
            database = ""
            win_auth = False
            username = st.text_input("Username", key="sb_user")
            password = st.text_input("Password", type="password", key="sb_pass")
            driver   = ""

        else:  # Snowflake
            server   = st.text_input("Account",
                                     placeholder="orgname-accountname", key="sb_server")
            database = st.text_input("Database", key="sb_database")
            win_auth = False
            username = st.text_input("Username", key="sb_user")
            password = st.text_input("Password", type="password", key="sb_pass")
            driver   = st.text_input("Warehouse", value="COMPUTE_WH", key="sb_driver")

        # ── Data model — chosen before connecting ─────────────────────
        import os as _sos
        _sreg = _sos.path.join(_sos.path.dirname(__file__), "schema_registry")
        _sfiles = sorted([f for f in _sos.listdir(_sreg)
                          if f.endswith(".json") and "schema_domain" in f]) \
                  if _sos.path.isdir(_sreg) else []
        if _sfiles:
            _slabels = {f: (f.replace("_schema_domain", "").replace(".json", "")
                             .replace("_", " ").strip().upper() or "DEFAULT")
                        for f in _sfiles}
            _sdefault = ("dataview_schema_domain.json"
                         if "dataview_schema_domain.json" in _sfiles else _sfiles[0])
            _scur = S.get("schema_file") or _sdefault
            if _scur not in _sfiles:
                _scur = _sdefault
            _spick = st.selectbox(
                "Data model", _sfiles, index=_sfiles.index(_scur),
                format_func=lambda f: _slabels.get(f, f), key="sb_schema_pick",
                help="Target schema to load into. Switching clears any in-progress import.")
            if S.get("schema_file") != _spick or S.get("ppdm_schema") is None:
                try:
                    import json as _sjson
                    with open(_sos.path.join(_sreg, _spick), encoding="utf-8") as _sfp:
                        _sraw = _sjson.load(_sfp)
                    from dataview.core.schema import load_schema_from_dict, EXPECTED_ROOT_KEY
                    # Auto-detect the array root key
                    for _sk, _sv in _sraw.items():
                        if isinstance(_sv, list):
                            if _sk != EXPECTED_ROOT_KEY:
                                _sraw[EXPECTED_ROOT_KEY] = _sraw.pop(_sk)
                            break
                    _switched = S.get("schema_file") not in (None, _spick)
                    S.ppdm_schema    = load_schema_from_dict(_sraw)
                    S.schema_variant = _slabels.get(_spick, _spick)
                    S.schema_file    = _spick
                    if _switched:
                        # New data model invalidates any in-progress import.
                        S.target_table = None
                        S.target_cols  = None
                        S.col_mapping  = None
                        S["mapping_grid_snapshot"] = None
                        S.stage = 1 if S.get("engine") is not None else 0
                    print(f"[APP] Schema loaded: {S.schema_variant} ({_spick})")
                    st.rerun()
                except Exception as _se:
                    st.error(f"Schema load failed: {_se}")

        col_a, col_b = st.columns(2)
        if col_a.button("Connect", key="sb_connect",
                        type="primary", use_container_width=True):
            with st.spinner("Connecting…"):
                try:
                    from dataview.core.db import DBConfig, connect as _connect
                    cfg = DBConfig(
                        server       = server,
                        database     = database,
                        driver       = driver,
                        windows_auth = win_auth,
                        username     = username,
                        password     = password,
                    )
                    result = _connect(cfg)
                    if result.ok:
                        S.engine    = result.engine
                        S.connected = True
                        S.dialect   = dialect.lower().replace(" ", "_")
                        S.db_label  = f"{server}/{database}" if database else server
                        # Stash the real connection parts. db.py builds the engine
                        # with a bare "mssql+pyodbc://" URL (driver/server/database
                        # live in a creator closure), so engine.url carries nothing
                        # — the detached multi-core runner needs these to rebuild
                        # the SAME connection instead of failing with IM002.
                        st.session_state["dw_conn_spec"] = {
                            "server": server, "database": database,
                            "driver": driver,
                        }
                        S.app_mode  = "splash"
                        st.rerun()
                    else:
                        st.error(result.message)
                except Exception as e:
                    st.error(str(e))

        if col_b.button("Demo", key="sb_demo",
                        use_container_width=True):
            try:
                from dataview.core.db import connect_demo
                result = connect_demo()
                S.engine    = result.engine
                S.connected = True
                S.demo      = True
                S.db_label  = "Demo Mode"
                S.app_mode  = "splash"
                st.rerun()
            except Exception as e:
                st.error(str(e))

    # ── RESET, THEN THE THEME PICKER ── BOTH BELOW CONNECT ───────
    # Connect is the first thing anyone touches, so it is the first thing in
    # the sidebar. Reset sits under it -- it is a cold start, wanted rarely
    # and never before you have tried connecting.
    if st.button("🔄  Reset", key="global_reset",
                 use_container_width=True,
                 help="Cold start: disposes the connection, clears all cached "
                      "data and every bit of session state, and returns to a "
                      "fresh, not-connected app — like restarting Streamlit."):
        try:
            if S.get("engine") is not None:
                S.engine.dispose()
        except Exception:
            pass
        for _cache in (getattr(st, "cache_data", None),
                       getattr(st, "cache_resource", None)):
            try:
                if _cache is not None:
                    _cache.clear()
            except Exception:
                pass
        # Wipe ALL session state -- DEFAULTS re-seed on the next run, landing
        # on the splash page, not connected, Midnight Gold theme.
        for _k in list(S.keys()):
            del S[_k]
        st.rerun()


    # ── Navigation (only when connected) ─────────────────────────────
    if S.connected:
        # ── ONCE LOGGED IN, THE SIDEBAR IS NOT NEEDED ────────────────
        # "We should also be able to close the sidebar at any time to give
        # more space to the applications. Once logged in we don't need it."
        # Connecting is the one thing it is required for, so connecting is
        # when it stands down -- every page gets the ~336px, not just the
        # map.
        #
        # ONCE PER PAGE LOAD, and only when it is observed to have worked:
        # the flag lives on the window and is set on aria-expanded, so a
        # sidebar re-opened by hand STAYS open. Streamlit has no API for
        # this after the session starts -- initial_sidebar_state applies to
        # the first render only, and the connection happens later -- so it
        # clicks the control a person would click.
        #
        # The » control Streamlit leaves behind is how it comes back, and
        # nav lives in there, so nothing becomes unreachable.
        try:
            from dataview.mapping.page_well_map import _collapse_sidebar_once
            _collapse_sidebar_once()
        except Exception as _cse:
            # Cosmetic only: an unreachable helper must not stop the app
            # from drawing its navigation.
            print("[app] sidebar auto-collapse skipped: %s" % _cse)

        st.markdown("<div class='sec-hdr'>Navigation</div>",
                    unsafe_allow_html=True)

        NAV = [
            ("splash",        "🏠", "Home"),
            ("well_map",      "🗺",  "Mapping"),
            ("documents",     "📂", "Documents"),
            # ("pipeline",    "📥", "Import Data"),   # retired Aug 2 —
            #   superseded by Data Assistant; uncomment to bring it back
            #   (its dispatch branch below is still live).
            ("load_data",     "📁", "Data Assistant"),
            ("docassist",     "📄", "Document Assistant"),
            # ("monitor",     "📡", "Pipeline Monitor"),  # retired Aug 2
            #   — uncomment to restore; its dispatch branch is still live.
            ("db_explorer",   "🔍", "DB Explorer"),
            ("workbench",     "🗂️", "File Catalog"),
            # ("inspector",   "🔬", "Inspector"),          # retired Aug 2
            #   — uncomment to restore; its dispatch branch is still live.
            ("ref_tables",    "📋", "Reference Tables"),
            ("spatial",       "🗺", "Spatial Loader"),
        ]
        for mode, icon, label in NAV:
            is_active = S.app_mode == mode
            if st.button(f"{icon}  {label}", key=f"nav_{mode}",
                         type="primary" if is_active else "secondary",
                         use_container_width=True):
                S.app_mode = mode
                st.rerun()

        st.markdown("<div class='sec-hdr'>Session</div>",
                    unsafe_allow_html=True)
        # WHICH DATABASE, kept from the status pill that used to sit above
        # Connect. The pill went, but this half of it earns its place: it is
        # the only thing on screen that says WHICH database is connected, and
        # the day DataView was silently selected instead of DataView_Demo
        # this line was the fastest way to see it.
        st.caption("● %s" % (S.db_label or "connected"))
        if st.button("⏏ Disconnect", key="nav_disconnect",
                     use_container_width=True):
            for k, v in DEFAULTS.items():
                S[k] = v
            # ── AND THE MAP'S SELECTIONS, WHICH DEFAULTS DOES NOT COVER ──
            # DEFAULTS resets seven keys, all about the CONNECTION. Every map
            # widget lives under its own key and survived untouched, so
            # reconnecting resumed drawing whatever was on before: "I
            # disconnected and started a new map. I entered Wyoming and the
            # county map plotted along with all the leases, uncalled for."
            # It was not a new map -- it was the old one, still selected.
            #
            # Disconnect means a clean slate, so it now means one.
            #
            # BY PREFIX, so a control added later is covered without anyone
            # remembering to add it here -- the "lists that must agree"
            # failure this repo pays for in four places. wm_ is the map,
            # lv_ the lease panel, seis_ the seismic screen. The connection
            # form is sb_ and is deliberately left alone: clearing the server
            # and database someone just typed would be its own rudeness.
            #
            # DELETED, NOT ASSIGNED. Assigning a widget's own key is the
            # Streamlit error that surfaces on a LATER run, on whatever page
            # draws next; deleting before the widget is built is the
            # supported way to restore a default. Safe here because the
            # rerun lands on the splash page, where none of them exist.
            _keep = ("sb_",)
            _drop = [k for k in list(st.session_state.keys())
                     if isinstance(k, str)
                     and k.startswith(("wm_", "lv_", "seis_"))
                     and not k.startswith(_keep)]
            # ── AND THE DATABASE CHOICE, WHICH sb_ WOULD OTHERWISE KEEP ──
            # The Database control is a selectbox created with index= pointing
            # at DataView_Demo -- but once a widget's key holds a value,
            # index= is IGNORED on every later run. DataView sorts first in
            # sys.databases, so if that selectbox was ever built before
            # DataView_Demo appeared in the fetched list, "DataView" was
            # stored and became permanent: the older, essentially empty
            # database, which has no dv_land_right at all.
            #
            # Reported as "Invalid object name 'dataview.dv_land_right'"
            # followed by "I have not changed databases" -- and that was
            # correct. The user changed nothing; the widget kept an answer
            # nobody gave it, and Disconnect preserved it because it wears
            # the sb_ prefix that protects the server and driver.
            #
            # Server, driver and auth are still kept: retyping those is the
            # rudeness this exception is carved out of. Only the database
            # goes, back to the default that is right.
            _drop += [k for k in ("sb_database_sel", "sb_database")
                      if k in st.session_state]
            # The map's own bookkeeping, which has no prefix.
            _drop += [k for k in (
                "map_mode", "wells_layer_on", "h3_layer_on", "h3_resolution",
                "_drawn_bounds", "_drawn_bounds_oneshot", "_clip_box",
                "_active_drill_bbox", "_place_shapes", "_place_pending",
                "_lease_channel_live", "_lease_rerun_for", "_lease_pref_seen",
                "_lease_gj_sig", "_lease_gj_legend", "_lease_gj_n",
                "_sidebar_collapsed_once", "_reset_saved_view",
            ) if k in st.session_state]
            for _k in _drop:
                try:
                    del st.session_state[_k]
                except Exception:
                    # A key that will not delete must not block the
                    # disconnect itself.
                    pass
            st.rerun()

        with st.expander("🗑️ Reset demo data", expanded=False):
            st.caption("Clears all loaded data from every table. Keeps the "
                       "schema and reference tables (dv_r_*, dv_country/"
                       "province_state/county).")
            try:
                from dataview.core.demo_reset import RESET_VERSION as _reset_ver
                st.caption(f"✅ reset engine loaded: **{_reset_ver}**")
            except Exception:
                st.caption("⚠️ OLD reset engine still loaded (no version "
                           "marker) — deploy demo_reset.py to modules\\ and "
                           "FULLY restart Streamlit (stop the process, not a "
                           "rerun).")
            if not S.get("_confirm_reset_demo"):
                if st.button("Reset demo data", key="reset_demo_open",
                             use_container_width=True):
                    S["_confirm_reset_demo"] = True
                    st.rerun()
            else:
                st.warning("Permanently deletes all loaded data from every "
                           "table in this database (reference tables are kept). "
                           "Continue?")
                _rc1, _rc2 = st.columns(2)
                with _rc1:
                    if st.button("✓ Confirm", key="reset_demo_confirm",
                                 type="primary", use_container_width=True):
                        try:
                            from dataview.core.demo_reset import reset_demo_data
                            S["_reset_demo_result"] = reset_demo_data(S.engine)
                        except Exception as _re:
                            S["_reset_demo_result"] = {"error": str(_re)}
                        S["_confirm_reset_demo"] = False
                        st.rerun()
                with _rc2:
                    if st.button("✗ Cancel", key="reset_demo_cancel",
                                 use_container_width=True):
                        S["_confirm_reset_demo"] = False
                        st.rerun()
            _res = S.get("_reset_demo_result")
            if _res:
                if "error" in _res:
                    st.error(f"Reset failed: {_res['error']}")
                else:
                    st.success("Reset done — " + ",  ".join(
                        f"{k}: {v}" for k, v in _res.items()))

        # ── Reset Teacup ────────────────────────────────────────────────
        # SCOPED TO THE DEMO SET ONLY, unlike the reset above it, which
        # empties every table. Three datasets, each identified by what it is
        # rather than when it arrived: the synthetic wells by their uwis, the
        # synthetic documents by their folder, the synthetic 2D by its set.
        # tools/demo_teacup.py owns the scoping and the FK order.
        with st.expander("\U0001FAD6 Reset Teacup", expanded=False):
            st.caption("Removes ONLY the Teacup demo data — the synthetic "
                       "wells, the synthetic documents and the synthetic 2D "
                       "seismic. Everything else is left alone.")
            _tc = _teacup_mod()
            if _tc is None:
                st.caption("⚠️ tools/demo_teacup.py not importable.")
            else:
                try:
                    _tcc = _teacup_counts_cached(
                        S.engine, S.get("_teacup_ver", 0))
                    _tct = sum(sum(v for v in d.values()
                                   if isinstance(v, int))
                               for d in _tcc.values())
                except Exception as _e:
                    _tcc, _tct = None, 0
                    st.caption(f"Counts unavailable: {_e}")
                if _tcc is not None:
                    for _ds in ("wells", "docs", "seismic"):
                        _n = sum(v for v in (_tcc.get(_ds) or {}).values()
                                 if isinstance(v, int))
                        st.caption(f"• {_ds}: **{_n:,}** row(s)")
                if not S.get("_confirm_reset_teacup"):
                    if st.button("Reset Teacup", key="reset_teacup_open",
                                 disabled=not _tct,
                                 use_container_width=True):
                        S["_confirm_reset_teacup"] = True
                        st.rerun()
                else:
                    st.warning(f"Permanently deletes the **{_tct:,} row(s)** "
                               "listed above — the demo set only. Continue?")
                    _tc1, _tc2 = st.columns(2)
                    with _tc1:
                        if st.button("✓ Confirm", key="reset_teacup_confirm",
                                     type="primary",
                                     use_container_width=True):
                            try:
                                S["_reset_teacup_result"] = _tc.reset_all(
                                    S.engine, apply=True)
                                # The counts just changed to zero, so retire
                                # the cached copy -- the panel would otherwise
                                # keep reporting what it removed.
                                S["_teacup_ver"] = S.get("_teacup_ver", 0) + 1
                            except Exception as _te:
                                S["_reset_teacup_result"] = {
                                    "error": str(_te)}
                            S["_confirm_reset_teacup"] = False
                            st.rerun()
                    with _tc2:
                        if st.button("✗ Cancel", key="reset_teacup_cancel",
                                     use_container_width=True):
                            S["_confirm_reset_teacup"] = False
                            st.rerun()
            _tres = S.get("_reset_teacup_result")
            if _tres:
                if "error" in _tres:
                    st.error(f"Reset Teacup failed: {_tres['error']}")
                else:
                    _lines = []
                    for _ds, _d in _tres.items():
                        if isinstance(_d, dict):
                            _lines.append("%s: %s" % (_ds, ",  ".join(
                                f"{k} {v}" for k, v in _d.items()) or "none"))
                    st.success("Teacup reset — " + " | ".join(_lines))


# ═══════════════════════════════════════════════════════════════════════
# MAIN CONTENT
# ═══════════════════════════════════════════════════════════════════════

def _nav_card(col, icon, title, desc, mode):
    with col:
        with st.container(border=True):
            st.markdown(
                f"<div style='text-align:center;font-size:26px;"
                f"padding:4px 0 2px 0'>{icon}</div>"
                f"<div style='text-align:center;font-size:14px;"
                f"font-weight:700;margin:2px 0 4px 0'>{title}</div>"
                f"<div style='text-align:center;font-size:11px;"
                f"color:#b2e8ee;margin:0 0 8px 0;line-height:1.4'>{desc}</div>",
                unsafe_allow_html=True)
            if st.button(f"Open", key=f"card_{mode}",
                         type="primary", use_container_width=True):
                S.app_mode = mode
                st.rerun()


# ── Scroll to top whenever the page (app_mode) changes ────────────────
# Streamlit keeps the previous scroll position across reruns, so switching
# pages can land you mid-page. Fire a one-shot scroll only when app_mode
# changes (not on every interaction, which would yank the view on each click).
if S.get("_last_app_mode") != S.app_mode:
    S["_last_app_mode"] = S.app_mode
    # THE MAP NEEDS ITS OWN, LATER SCROLL. This one fires here, ~170 lines
    # ABOVE the dispatch, so on the way into the map the page is still
    # empty: scrollTo(0,0) succeeds against a short page and the browser
    # then restores the old position once 15,000 lines of map have arrived.
    # Measured 29 Aug -- the map opened at scrollTop 1759 of a 3,634px page,
    # which is the whole of "the page is pushed down". page_well_map keeps
    # retrying past that restore; this flag is what asks it to.
    #
    # Set on every ENTRY, not once per session: the map's own flag was
    # seeded inside `if "_wm_page_entered" not in st.session_state`, so
    # coming back to the map a second time never scrolled at all.
    if S.app_mode == "well_map":
        st.session_state["_wm_scroll_top_pending"] = True
    import streamlit.components.v1 as _components
    _components.html(
        """
        <script>
          (function () {
            const d = window.parent.document;
            const sels = ['section.main', '[data-testid="stMain"]',
                          '[data-testid="stAppViewContainer"]', '.main', '.stApp'];
            for (const s of sels) {
              const el = d.querySelector(s);
              if (el) { try { el.scrollTo(0, 0); } catch (e) { el.scrollTop = 0; } }
            }
            try { window.parent.scrollTo(0, 0); } catch (e) {}
          })();
        </script>
        """,
        height=0,
    )


# ── card_help: a "?" badge in a card corner with a hover tooltip ──────
# Call as the FIRST element inside `with st.container(border=True):`
#   with st.container(border=True):
#       card_help("Explains what this card does", corner="tr")
#       ...card content...
def card_help(text, corner="tr"):
    """Render a gold '?' help badge in a corner of the enclosing bordered
    card. corner ∈ {'tr','tl','br','bl'} (default top-right)."""
    import html as _html
    _pos = {"tr": "top:8px;right:10px;", "tl": "top:8px;left:10px;",
            "br": "bottom:8px;right:10px;", "bl": "bottom:8px;left:10px;"}.get(
            corner, "top:8px;right:10px;")
    _safe = _html.escape(str(text), quote=True).replace("\r", "").replace("\n", "&#10;")
    st.markdown(
        f"<div class='card-help' style='{_pos}' "
        f"data-help=\"{_safe}\">?</div>",
        unsafe_allow_html=True)


# ── SPLASH ────────────────────────────────────────────────────────────
if S.app_mode == "splash":
    # ── Company header ───────────────────────────────────────────────
    import base64 as _b64, pathlib as _pl
    _logo_b64 = ""
    for _lp in ["assets/data_wrangler.png", "data_wrangler.png"]:
        if _pl.Path(_lp).exists():
            _logo_b64 = _b64.b64encode(_pl.Path(_lp).read_bytes()).decode()
            break

    _logo_html = (
        f"<img src=\"data:image/png;base64,{_logo_b64}\" "
        f"style=\"height:90px;width:auto\"/>"
        if _logo_b64 else "🛢"
    )

    st.markdown(f"""
        <div style="display:flex;align-items:center;gap:20px;
                    background:#0a5f6a;
                    padding:16px 24px;
                    border-radius:12px;
                    border-bottom:3px solid #c4915a;
                    margin-bottom:16px;
                    box-shadow:0 4px 20px rgba(0,0,0,0.3)">
            {_logo_html}
            <div>
                <div style="font-family:'Bebas Neue',sans-serif;
                            font-size:46px;color:#ffffff;
                            letter-spacing:3px;line-height:1;
                            text-shadow:1px 2px 6px rgba(0,0,0,0.5)">
                    DATA WRANGLER
                </div>
                <div style="font-size:14px;color:#c4915a;
                            font-family:'IBM Plex Mono',monospace;
                            font-weight:600;letter-spacing:2px;margin-top:2px">
                    DATA WRANGLER SOLUTIONS LLC
                </div>
                <div style="font-size:11px;color:#b2e8ee;
                            font-family:'IBM Plex Mono',monospace;
                            letter-spacing:1px;margin-top:4px">
                    PETROLEUM DATA PLATFORM &nbsp;·&nbsp;
                    SQL SERVER &nbsp;·&nbsp; ORACLE &nbsp;·&nbsp; SNOWFLAKE
                    &nbsp;·&nbsp; v4.0
                </div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    if not S.connected:
        st.info("🔌 Connect to a database using the sidebar to get started.")
    else:
        st.markdown(
            f"<p style='color:#3dd68c;font-size:12px;margin-bottom:12px'>"
            f"● Connected to {S.db_label}</p>",
            unsafe_allow_html=True)

        # Row 1
        c1, c2, c3 = st.columns(3)
        _nav_card(c1, "🗺",  "Mapping",
                  "Well map · spatial layers · AI filter · scout tickets",
                  "well_map")
        _nav_card(c2, "📁", "Data Assistant",
                  "Tabular files → plan · map · resolve FKs · load · verify",
                  "load_data")
        # Import Data card retired Aug 2 — the Data Assistant covers this
        # path now. Uncomment to restore; the "pipeline" branch below still
        # works, so this is the only line needed.
        # _nav_card(c2, "📥", "Import Data",
        #           "8-stage pipeline · CSV → staging → normalize → map → promote",
        #           "pipeline")
        _nav_card(c3, "🔍", "DB Explorer",
                  "Query · browse · export any DataView table",
                  "db_explorer")
        # Row 2
        c4, c5, c6 = st.columns(3)
        _nav_card(c4, "🗂️", "File Catalog",
                  "Scan · enrich · browse · view · extract · load",
                  "workbench")
        _nav_card(c5, "📄", "Document Assistant",
                  "Read documents · see what was extracted · teach the vocabulary",
                  "docassist")
        _nav_card(c6, "📋", "Reference Tables",
                  "Seed dv_r_well_status · dv_r_well_type · lookups",
                  "ref_tables")
        # Region Builder card retired Aug 2 — uncomment to restore (its
        # dispatch branch below is untouched).
        # _nav_card(c6, "🗺", "Region Builder",
        #      "Define state regions by lassoing counties on a map",
        #      "region_builder")
        # Row 3 retired Aug 26 — it held one live card and two retired ones,
        # so a third row of mostly empty space cost more than it carried.
        #
        # PPDM Migration IS NOW UNREACHABLE FROM THE UI, and that is worth
        # knowing rather than discovering: this card was its only entry point
        # ("migrate" is not in the sidebar NAV). Its dispatch branch below is
        # untouched and still works, so restoring is uncommenting these four
        # lines — or adding ("migrate", "⇄", "PPDM Migration") to NAV, which
        # is the better home for it if it comes back.
        #
        # Document Recogniser was already retired Aug 2 — the Document
        # Assistant replaces it (page_docshape remains the vocabulary BENCH;
        # its "docshape" branch below still works if you want it back).
        #
        # c7, c8, c9 = st.columns(3)
        # _nav_card(c7, "⇄", "PPDM Migration",
        #           "Map dataview tables to PPDM 3.9 and promote",
        #           "migrate")
        # _nav_card(c8, "🔍", "Document Recogniser",
        #           "Read a document, review what each table matched, correct the vocabulary",
        #           "docshape")
# ── SEISMIC, SECOND SCREEN ──────────────────────
elif S.app_mode == "seis_view":
    try:
        from dataview.mapping import page_well_map as _pwm
        if S.engine is None:
            # The sidebar renders for every mode and its Connect fields
            # are pre-filled, so this is one click -- a second SESSION
            # cannot inherit the first one's engine, and putting the
            # connection in the URL to avoid that would be worse.
            st.info("This window needs its own connection — press "
                    "**Connect** in the sidebar. The line you picked is "
                    "in the URL and will open once connected.")
        else:
            _pwm.render_seis_view(S.engine)
    except Exception as e:
        st.error(f"Seismic view error: {e}")

# ── LEASE SECOND SCREEN ───────────────────────────────────────────────
elif S.app_mode == "lease_view":
    try:
        from dataview.mapping import page_well_map as _pwm2
        if S.engine is None:
            st.info("This window needs its own connection — press "
                    "**Connect** in the sidebar. A second window is a second "
                    "session, so it cannot inherit the map's.")
        else:
            _pwm2.render_lease_view(S.engine)
    except Exception as e:
        import traceback
        traceback.print_exc()
        st.error(f"Lease view error: {e}")

# ── WELL MAP ──────────────────────────────────────────────────────────
elif S.app_mode == "well_map":
    try:
        from dataview.mapping import page_well_map
        fn = getattr(page_well_map, "run", None) or getattr(page_well_map, "render", None)
        if fn:
            fn(S.engine)
        else:
            st.error("page_well_map has no run() or render() function")
    except Exception as e:
        # THE TRACEBACK GOES TO THE LOG, not just the message to the screen.
        # "Well Map error: cannot access local variable" names the failure and
        # not one thing about WHERE -- and page_well_map is 15,000 lines. A
        # discarded diagnostic makes the next failure undiagnosable, which is
        # the first rule in CLAUDE.md and was being broken right here.
        import traceback
        traceback.print_exc()
        st.error(f"Well Map error: {e}")

# ── DOCUMENTS ─────────────────────────────────────────────────────────
elif S.app_mode == "documents":
    try:
        from dataview.file_catalog import page_well_documents
        page_well_documents.run(S.engine)
    except Exception as e:
        st.error(f"Documents error: {e}")

# ── DB EXPLORER ───────────────────────────────────────────────────────
elif S.app_mode == "db_explorer":
    try:
        # Make engine accessible both as attribute and dict key
        st.session_state["engine"] = S.engine
        from dataview.db_explorer import page_db_explorer
        page_db_explorer.render(S)
    except Exception as e:
        st.error(f"DB Explorer error: {e}")
        import traceback
        st.code(traceback.format_exc())

# ── MODEL OVERVIEW ────────────────────────────────────────────────────
elif S.app_mode == "schema_overview":
    try:
        from dataview.db_explorer import page_schema_overview
        page_schema_overview.run(S.engine)
    except Exception as e:
        st.error(f"Model Overview error: {e}")
        import traceback
        st.code(traceback.format_exc())

# ── FILE Inventory ──────────────────────────────────────────────────────
elif S.app_mode == "file_inv":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_file_manager
        page_file_manager.render(S.engine, _dialect)
    except Exception as e:
        st.error(f"File Manager error: {e}")

# ── FILE CATALOG ─────────────────────────────────────────────────────
elif S.app_mode == "file_catalog":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_file_catalog
        page_file_catalog.run(S.engine, _dialect)
    except Exception as e:
        import traceback
        st.error(f"File Catalog error: {e}")
        st.code(traceback.format_exc())

# ── WORKBENCH ─────────────────────────────────────────────────────────
elif S.app_mode == "workbench":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_workbench
        page_workbench.run(S.engine, _dialect)
    except Exception as e:
        st.error(f"File Catalog error: {e}")

# ── PIPELINE MONITOR ──────────────────────────────────────────────────
elif S.app_mode == "monitor":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_monitor
        page_monitor.run(S.engine, _dialect)
    except Exception as e:
        st.error(f"Monitor error: {e}")

# ── EXTRACTION INSPECTOR ──────────────────────────────────────────────
elif S.app_mode == "inspector":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_extraction_inspector
        page_extraction_inspector.run(S.engine, _dialect)
    except Exception as e:
        st.error(f"Extraction Inspector error: {e}")

elif S.app_mode == "triage":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_triage
        page_triage.render(S.engine, _dialect)
    except Exception as e:
        st.error(f"Triage error: {e}")

elif S.app_mode == "promote":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_triage
        page_triage.render_promote(S.engine, _dialect)
    except Exception as e:
        st.error(f"Promote error: {e}")


# ── SPATIAL LAYERS ────────────────────────────────────────────────────
elif S.app_mode == "file_scan":
    try:
        from dataview.core.db import _detect_dialect
        _raw = _detect_dialect(S.engine) or "mssql"
        _dialect = {"sqlserver": "mssql", "sql_server": "mssql"}.get(_raw, _raw)
        from dataview.file_catalog import page_file_manager
        page_file_manager.render(S.engine, _dialect)
    except Exception as e:
        st.error(f"File Manager error: {e}")

# ── SPATIAL LOADER ───────────────────────────────────────
# The old "📐 Spatial Layers" body lived here and could LIST and DELETE
# layers -- and was not in NAV, so nothing could reach it. page_spatial_loader
# does both of those and adds the part that was missing: browse a source, see
# what is in it, and choose. Two branches on the same app_mode meant the first
# won and the second was dead code, so they are one.
elif S.app_mode == "spatial":
    try:
        from dataview.mapping import page_spatial_loader
        page_spatial_loader.render(S.engine)
    except Exception as e:
        st.error(f"Spatial Loader error: {e}")
        import traceback
        st.code(traceback.format_exc())
# ── REFERENCE TABLES ─────────────────────────────────────────────────
elif S.app_mode == "ref_tables":
    try:
        from dataview.reference_tables import page_standards_manager
        page_standards_manager.render(S.engine)
    except Exception as e:
        st.error(f"Standards Manager error: {e}")
        import traceback
        st.code(traceback.format_exc())

elif S.app_mode == "region_builder":
       try:
           from dataview.region_builder import page_region_builder
           page_region_builder.render(S.engine)
       except Exception as e:
           st.error(f"Region Builder error: {e}")
           import traceback
           st.code(traceback.format_exc())

elif S.app_mode == "pipeline":
    try:
        from dataview.import_data import page_pipeline
        page_pipeline.render(S)
    except Exception as e:
        st.error(f"Pipeline error: {e}")
        import traceback
        st.code(traceback.format_exc())
elif S.app_mode == "dirloader":
    try:
        from dataview.import_data import page_dir_loader
        page_dir_loader.run(S.engine)
    except Exception as e:
        st.error(f"Bulk Tabular Loader error: {e}")
        import traceback
        st.code(traceback.format_exc())

# ── LOAD DATA (router: extensions pick the path) ──────────────────────
elif S.app_mode == "load_data":
    try:
        from dataview.import_data import load_router
        load_router.run(S.engine)
    except Exception as e:
        st.error(f"Load Data error: {e}")
        import traceback
        st.code(traceback.format_exc())

# ── BULK LOADER (LAS / DLIS / LIS / WITSML / PDF / Word) ──────────────
elif S.app_mode == "bulkloader":
    try:
        from dataview.import_data import bulk_dir_loader
        bulk_dir_loader.run()
    except Exception as e:
        st.error(f"Bulk Loader error: {e}")
        import traceback
        st.code(traceback.format_exc())
elif S.app_mode == "docassist":
    try:
        from dataview.file_catalog import doc_flow
        doc_flow.render(S.engine)
    except Exception as e:
        st.error(f"Document Assistant error: {type(e).__name__}: {e}")
        import traceback
        st.code(traceback.format_exc())

elif S.app_mode == "migrate":
    try:
        from dataview.migration import page_migrate
        page_migrate.render(S.engine)
    except Exception as e:
        st.error(f"PPDM Migration error: {e}")
        import traceback
        st.code(traceback.format_exc())
elif S.app_mode == "docshape":
    try:
        from dataview.file_catalog import page_docshape
        page_docshape.render(S.engine)
    except Exception as e:
        st.error(f"Document recogniser error: {type(e).__name__}: {e}")
        st.code(traceback.format_exc())
