"""
load_router.py — one door for loading a directory. Look at what's in the folder and
send it down the right path:

    loadable   csv, xlsx/xlsm/xltx/xls, las, dlis, lis, xml/wml, pdf, docx/doc/odt
    anything else → listed as unsupported, not loaded

DEFAULT: bulk_dir_loader (route B) for everything. It reads every supported type — CSVs and
Excel included, through page_dir_loader.profile_directory, which also explodes workbooks to
one CSV per sheet — and it carries the governance: UWI gate, content-hash file catalog, BCP
staging, data-quality report, value repairs, date-format detection, dry run, verify.

Route A (page_dir_loader) is NOT retired. It stays one click away via the ⇄ button on the
routed page, because it still does one thing B doesn't: it repairs values inline in a
per-table flow. Use it when a folder needs that hand-holding.

NB page_dir_loader can't be deleted regardless — bulk_dir_loader imports its deterministic
core (profile_directory, fingerprint_cols, load_catalog, _norm).

Wire into app_v3.py:

    elif S.app_mode == "load_data":
        try:
            from dataview.import_data import load_router
            load_router.run(S.engine)
        except Exception as e:
            st.error(f"Load Data error: {e}")
"""
from __future__ import annotations
import os
import glob

try:
    import streamlit as st
except Exception:
    st = None

# route A — tabular
A_EXTS = (".csv", ".xlsx", ".xlsm", ".xltx", ".xls")
# route B — well / document files
B_EXTS = (".las", ".dlis", ".lis", ".xml", ".wml", ".pdf", ".docx", ".doc", ".odt")

_SHEET_DIR = "_xl_sheets"        # page_dir_loader's sidecar folder — not a source

# Settings worth carrying from one folder to the next; everything else is scan state that
# MUST go, or the next folder renders the previous folder's plan (a stale bdl_scan/dl_plan is
# indistinguishable from "the scan found nothing").
_KEEP_ON_RESET = ("bdl_server", "bdl_db", "bdl_cat", "bdl_bulk", "bdl_schema", "bdl_recursive",
                  "dl_cat", "dl_recursive", "lr_recursive")


def _scroll_to_top():
    """Streamlit keeps the scroll position across reruns — after finishing a load at the
    bottom of a long page, send the operator back to the directory box."""
    try:
        import streamlit.components.v1 as _components
    except Exception:
        return
    _components.html(
        """
        <script>
          const go = () => {
            try {
              const doc = window.parent.document;
              const sels = ['section.main', 'section[data-testid="stMain"]',
                            '.stMainBlockContainer', '[data-testid="stAppViewContainer"]',
                            '[data-testid="stVerticalBlock"]'];
              for (const s of sels) {
                const el = doc.querySelector(s);
                if (el && el.scrollTo) el.scrollTo(0, 0);
                if (el) el.scrollTop = 0;
              }
              doc.documentElement.scrollTop = 0;
              doc.body.scrollTop = 0;
              window.parent.scrollTo(0, 0);
            } catch (e) {}
          };
          // retry past Streamlit's own scroll restoration, which lands after render
          [0, 60, 150, 300, 600, 1000, 1500].forEach(t => setTimeout(go, t));
        </script>
        """,
        height=0,
    )


def _load_another(ss):
    """Back to the directory picker, reset for a genuinely different folder."""
    keep = {k: ss[k] for k in _KEEP_ON_RESET if k in ss}
    for k in [k for k in list(ss.keys())
              if k.startswith(("bdl_", "dl_", "lr_"))]:
        ss.pop(k, None)
    ss.update(keep)
    ss["lr_scroll_top"] = True


def classify(directory, recursive=False):
    """→ {'A': [paths], 'B': [paths], 'C': [paths]} for one directory."""
    out = {"A": [], "B": [], "C": []}
    if not directory or not os.path.isdir(directory):
        return out
    pat = os.path.join(directory, "**", "*") if recursive else os.path.join(directory, "*")
    for p in glob.glob(pat, recursive=recursive):
        if not os.path.isfile(p):
            continue
        base = os.path.basename(p)
        if base.startswith("~$"):                       # Excel lock files
            continue
        if _SHEET_DIR in os.path.normpath(p).split(os.sep):   # our own generated sheets
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in A_EXTS:
            out["A"].append(p)
        elif ext in B_EXTS:
            out["B"].append(p)
        else:
            out["C"].append(p)
    return out


def _counts(paths):
    """{'.csv': 6, '.pdf': 2} for display."""
    c = {}
    for p in paths:
        e = os.path.splitext(p)[1].lower() or "(none)"
        c[e] = c.get(e, 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def _fmt(counts):
    return " · ".join(f"{n} {e}" for e, n in counts.items()) if counts else "—"


def _listing(label, paths):
    """Show a file list in an expander — falling back to a plain caption if we're already
    inside one (Streamlit forbids nesting, and a file list isn't worth an exception)."""
    def _body():
        for p in sorted(paths)[:200]:
            st.markdown(f"- `{os.path.basename(p)}`")
        if len(paths) > 200:
            st.caption(f"…and {len(paths) - 200} more")
    try:
        with st.expander(label):
            _body()
    except Exception:
        st.caption(label)
        _body()


def _go_a(ss, directory):
    ss["lr_dir"] = directory          # remembered so we can switch loaders on the same folder
    ss["dl_dir"] = directory          # pre-seed so page_dir_loader doesn't re-ask
    ss["dl_recursive"] = bool(ss.get("lr_recursive"))
    ss["dl_stage"] = "pick"
    ss["lr_route"] = "A"


def _go_b(ss, directory):
    ss["lr_dir"] = directory          # remembered so we can switch loaders on the same folder
    ss["bdl_dir"] = directory         # pre-seed bulk_dir_loader's directory
    ss["bdl_recursive"] = bool(ss.get("lr_recursive"))   # carry recursion, or B scans flat
    ss["lr_route"] = "B"


def _switch_route(ss):
    """Run the SAME folder through the other loader. The two stage completely differently
    (A inserts per table, B extracts → BCP → staging → promote), so this clears the previous
    loader's state rather than trying to carry it across."""
    d = ss.get("lr_dir") or ss.get("dl_dir") or ss.get("bdl_dir") or ""
    to = "B" if ss.get("lr_route") == "A" else "A"
    keep = {k: ss[k] for k in _KEEP_ON_RESET if k in ss}
    for k in [k for k in list(ss.keys()) if k.startswith(("bdl_", "dl_", "lr_"))]:
        ss.pop(k, None)
    ss.update(keep)
    (_go_b if to == "B" else _go_a)(ss, d)
    ss["lr_scroll_top"] = True


def run(engine=None, dialect=None):
    if st is None:
        return
    ss = st.session_state
    if ss.pop("lr_scroll_top", False):
        _scroll_to_top()

    # once routed, hand off to the chosen loader (with a way back)
    # Route B is the default for EVERY folder — it reads every supported type, CSV and Excel
    # included. So there is no routing decision left to make, and the screen that used to make
    # it (directory box → "Scan & route" → classify → always B) was pure overhead: you typed
    # the folder, then bulk_dir_loader asked for it again. Worse, the counts that screen drew
    # were discarded by its own st.rerun() before they could render, and the unsupported-file
    # listing after that rerun was unreachable.
    #
    # Go straight to B. bulk_dir_loader asks for the directory ONCE and does the only real
    # scan. The router keeps what it is actually good for: ⇄ to route A, and "Load another
    # folder", both on the routed page. _load_another pops lr_route, which lands back here and
    # re-defaults to B with an empty directory box.
    route = ss.get("lr_route") or "B"
    ss["lr_route"] = route
    if route in ("A", "B"):
        top = st.columns([1, 1, 3])
        if top[0].button("← Load another folder", key="lr_back_top"):
            _load_another(ss); st.rerun()
        if route == "B":
            _lbl, _help = ("⇄ Use the tabular loader",
                           "Same folder, route A (page_dir_loader): one table at a time, with "
                           "per-table FK resolution and inline value repair. Slower, but it "
                           "cleans values as it loads.")
        else:
            _lbl, _help = ("⇄ Back to the main loader",
                           "Same folder, route B (bulk_dir_loader): the default — UWI gate, "
                           "file catalog, BCP staging, data-quality report, dry run, verify.")
        if top[1].button(_lbl, key="lr_switch", help=_help):
            _switch_route(ss); st.rerun()
        top[2].caption(
            "Route B · main loader — CSV · Excel · LAS · DLIS · LIS · WITSML · PDF · Word"
            if route == "B" else
            "Route A · tabular loader — one table at a time (CSV / Excel)")
        if route == "A":
            try:
                from dataview.import_data import page_dir_loader as _a
            except Exception:
                import page_dir_loader as _a
            _a.run(engine, dialect)
        else:
            try:
                from dataview.import_data import bulk_dir_loader as _b
            except Exception:
                import bulk_dir_loader as _b
            _b.run()
        # same way out at the bottom — these pages are long, and after a load finishes you
        # shouldn't have to scroll back up to point at the next folder.
        st.divider()
        if st.button("← Load another folder", key="lr_back_bottom"):
            _load_another(ss); st.rerun()
        return

    # Unreachable: `route` is defaulted to "B" above, so control never arrives here. What
    # stood here was the old pre-scan screen — a directory box, a recursion checkbox, and a
    # "Scan & route" button that classified the folder and then routed to B unconditionally.
    # classify() is kept: it is a cheap, pure extension census, and the "N unsupported files"
    # answer it produces is worth surfacing INSIDE the loader some day — it was never visible
    # here, because the st.rerun() that followed threw the rendered counts away.


render = main = show = app = run
