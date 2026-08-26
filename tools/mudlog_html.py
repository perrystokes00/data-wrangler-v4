"""Write the mud log as one self-contained HTML file you can open full screen.

    python tools/mudlog_html.py                       # -> mudlog_48X28.html
    python tools/mudlog_html.py --out C:\\Bulk\\48X28.html
    python tools/mudlog_html.py --export C:\\Bulk\\mudlog_test\\export.dat

Double-click the result. It opens in any browser with no server, no Python and
no network: press F11 for full screen, scroll the log, drag the column
dividers, and zoom with the slider or Ctrl-scroll.

WHY A FILE AND NOT A SCREENSHOT
-------------------------------
A PNG of a 5,200 ft log is either unreadable or enormous -- at a legible five
inches to a hundred feet it is 260 inches tall. The HTML carries the DATA and
draws it in the browser, so the same 0.15 MB file is legible at any scale, its
descriptions can be selected and copied, and the columns can be resized to
whatever the reader is doing. It is about forty times smaller than the tiled
images it replaced.

WHAT IS IN IT is whatever the WellSight viewer exported -- curve names, units
and sample step, the ROP scale and the four depths it changes at, the coded
lithology, the coded oil shows, and all three text tracks. Nothing here is
inferred from the .LOG binary; see tools/mudlog_export.py for why that matters.

The page template lives beside this file as mudlog_strip_template.html so it
stays editable as HTML rather than as a string inside Python.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mudlog_export import parse
from plot_mudlog import formation_tops, LITH_COLOUR, GAS_STYLE, SHOW_COLOUR

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "mudlog_strip_template.html")
DEFAULT_DAT = r"C:\Bulk\mudlog_test\export.dat"


def _curve(ex, name):
    t = ex.curve(name)
    if not t:
        return None
    # The depth step is fixed, so only the values travel; a null stays null and
    # breaks the line in the browser exactly as it does in the static plot.
    return {"name": t.name, "unit": t.unit, "step": t.step,
            "d0": t.data[0][0] if t.data else ex.depth_range[0],
            "scales": [[d, mn, mx] for d, mn, mx in t.scales],
            "v": [None if v is None else round(v, 3) for _d, v in t.data]}


def build_data(ex):
    """Everything the page draws, as one JSON-able dict."""
    lo, hi = ex.depth_range
    tops, _ops = formation_tops(ex)
    return {
        "header": ex.header,
        "range": [lo, hi],
        "rop": _curve(ex, "ROP"),
        "gas": [c for c in (_curve(ex, n) for n, _c, _w in GAS_STYLE) if c],
        "gasColour": {n: c for n, c, _w in GAS_STYLE},
        "lith": [[round(t, 1), round((b if b is not None else t + 2), 1), l[0]]
                 for t, b, l in ex.choice("Lithology") if l],
        "lithColour": LITH_COLOUR,
        "shows": [[round(d, 1), l[0]]
                  for d, _b, l in ex.choice("Oil Shows") if l],
        "showColour": SHOW_COLOUR,
        "descr": [[round(d, 1), t] for d, t in ex.text("Geol. Descrs.")],
        "eng": [[round(d, 1), t] for d, t in ex.text("Eng. Data")],
        "eng2": [[round(d, 1), t] for d, t in ex.text("Eng. Data 2")],
        "tops": [[round(d, 1), n] for d, n in tops],
    }


def build_html(ex, standalone=True):
    """The page. standalone wraps it in a document so a browser can open it.

    THE WRAPPER IS NOT COSMETIC. Without a doctype the browser falls into
    quirks mode, where document.scrollingElement is the body and window scroll
    events never fire -- the depth readout freezes and every jump silently
    does nothing. That cost an hour when a test harness served the fragment
    raw; the fragment itself was fine.
    """
    tpl = open(TEMPLATE, encoding="utf-8").read()
    body = tpl.replace("__DATA__", json.dumps(build_data(ex)))
    if not standalone:
        return body
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "</head><body>" + body + "</body></html>")


def write(export_path=DEFAULT_DAT, out=None):
    ex = parse(export_path)
    well = (ex.header.get("Well Name") or "mudlog").replace(" ", "")
    out = out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "mudlog_%s.html" % well)
    html = build_html(ex)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out, ex, len(html)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", default=DEFAULT_DAT,
                    help="the .dat from File > Export > All Data")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if not os.path.exists(a.export):
        raise SystemExit(
            "No export at %s.\n"
            "Open the .LOG in the WellSight viewer and use\n"
            "  File > Export > Write all data to an ASCII file" % a.export)
    out, ex, n = write(a.export, a.out)
    lo, hi = ex.depth_range
    print(out)
    print("   %s · %.0f-%.0f ft · %.2f MB"
          % (ex.header.get("Well Name", "?"), lo, hi, n / 1048576.0))
    print("   %d descriptions · %d lithology intervals · %d shows · %d tops"
          % (len(ex.text("Geol. Descrs.")), len(ex.choice("Lithology")),
             len(ex.choice("Oil Shows")), len(formation_tops(ex)[0])))
    print("   Open it in a browser and press F11 for full screen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
