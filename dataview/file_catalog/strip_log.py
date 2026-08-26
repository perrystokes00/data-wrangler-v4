"""A scrolling, true-scale strip log for wireline curves.

    from dataview.file_catalog import strip_log
    strip_log.render(df, depth_col, file_path=path, engine=engine)

WHAT THIS REPLACES
------------------
The previous plot was plotly, one subplot per curve, `height=650` FIXED. That
height is the whole problem: a 300 ft log and a 5,300 ft log render at exactly
the same size, so there is no depth scale. You cannot ask for five inches to a
hundred feet, which is how a log is read and the only way two intervals can be
compared. It also asked the reader to pick curves from a multiselect before
drawing anything, when the curves themselves say where they belong.

This draws the conventional triple-combo automatically, at a real px/ft scale,
and scrolls like the paper.

THE CONVENTIONS ARE NOT DECORATION
----------------------------------
Resistivity is logarithmic over 0.2-2000 because four decades of useful range
cannot be read on a linear axis. Neutron runs 0.45 to -0.15 against density
1.95 to 2.95 so the two CROSS where lithology and porosity disagree -- that
crossover is the gas indicator, and drawing the pair on independent autoscales
throws away the reason they share a track. Both come from dv_r_log_mnemonic,
which is editable in the Standards Manager, so they are Perry's to change
rather than mine to hard-code.

DECIMATION IS MIN/MAX PER COLUMN OF PIXELS, not "every Nth sample". A 42-curve
LAS at 1 ft over 3,900 ft is 163,000 points; sub-sampling it drops the spikes,
and on a log the spikes are the bed boundaries. Min/max keeps the envelope, so
a decimated curve still shows every excursion -- it just draws two points per
pixel column instead of forty.
"""

import json
import math
import os

from dataview.file_catalog.curve_families import (classify, propose_tracks,
                                                  FAMILY_STYLE, TRACK_TEMPLATE,
                                                  DEFAULT_TRACKS)

DEPTH_NAMES = {"DEPT", "DEPTH", "MD", "TVD", "TVDSS"}
MAX_POINTS_PER_CURVE = 1200


def _catalog(engine):
    """{mnemonic: {...}} from dv_r_log_mnemonic, or {} when it is not there.

    The table is ADVISORY. A curve it has never heard of still plots -- see the
    note in tools/seed_log_mnemonics.py about why this has no foreign key."""
    if engine is None:
        return {}
    try:
        from sqlalchemy import text
        with engine.connect() as c:
            if not c.execute(text(
                    "SELECT OBJECT_ID('dataview.dv_r_log_mnemonic')")).scalar():
                return {}
            rows = c.execute(text(
                "SELECT mnemonic, family, scale_min, scale_max, log_scale, "
                "track_hint, colour, line_style, unit "
                "FROM dataview.dv_r_log_mnemonic WHERE active_ind='Y'"))
            return {r[0].strip().upper(): {
                "family": r[1], "min": r[2], "max": r[3],
                "log": (r[4] or "N").upper() == "Y", "track": r[5],
                "colour": r[6], "dash": (r[7] or "") == "dash", "unit": r[8]}
                for r in rows if r[0]}
    except Exception:
        return {}


def _decimate(depths, values, target):
    """Min/max per bucket, so an excursion survives being drawn small."""
    n = len(depths)
    if n <= target:
        return [(d, v) for d, v in zip(depths, values)]
    step = max(2, int(math.ceil(n / float(target / 2))))
    out = []
    for i in range(0, n, step):
        chunk = [(d, v) for d, v in
                 zip(depths[i:i + step], values[i:i + step]) if v is not None]
        if not chunk:
            out.append((depths[i], None))
            continue
        lo = min(chunk, key=lambda t: t[1])
        hi = max(chunk, key=lambda t: t[1])
        # keep them in depth order so the polyline does not zig-zag backwards
        out.extend(sorted((lo, hi), key=lambda t: t[0]))
    return out


def build_spec(df, depth_col=None, engine=None, title="", subtitle=""):
    """Turn a curve DataFrame into a track specification."""
    import numpy as np

    cols = list(df.columns)
    if depth_col is None:
        depth_col = next((c for c in cols if str(c).upper() in DEPTH_NAMES),
                         cols[0])
    depth = np.asarray(df[depth_col], dtype="float64")
    good = np.isfinite(depth)
    if not good.any():
        return None
    depth = depth[good]
    lo, hi = float(np.nanmin(depth)), float(np.nanmax(depth))
    if not (hi > lo):
        return None

    cat = _catalog(engine)
    mnemonics = [c for c in cols if c != depth_col]
    layout = propose_tracks(mnemonics)

    def style(m):
        key = str(m).strip().upper()
        c = cat.get(key)
        fam, _lbl = classify(key)
        base = FAMILY_STYLE.get(fam, {})
        sc = base.get("scale")
        out = {"family": fam,
               "min": sc[0] if sc else None,
               "max": sc[1] if sc else None,
               "log": False,
               "colour": base.get("colour", "#333333"),
               "dash": bool(base.get("dash")),
               "unit": base.get("unit") or ""}
        if c:
            out.update({k: c[k] for k in ("min", "max", "log", "colour",
                                          "unit")
                        if c.get(k) is not None})
            out["dash"] = c.get("dash", out["dash"])
        return out

    tracks, held = [], []
    for t in layout:
        # NOT DRAWN BY DEFAULT, BUT NOT LOST EITHER. A log carries
        # tension, deviation and every array-induction variant the tool
        # recorded; a default view that plots all of them is unreadable,
        # and one that silently drops them is worse. They are named in
        # the caption so the reader knows they exist and can catalogue
        # them into a track.
        if t["title"] not in DEFAULT_TRACKS:
            held.extend(t["curves"])
            continue
        curves = []
        for m in t["curves"]:
            vals = np.asarray(df[m], dtype="float64")[good]
            finite = np.isfinite(vals)
            if not finite.any():
                continue
            st_ = style(m)
            if st_["min"] is None or st_["max"] is None:
                # NO CONVENTION FOR THIS CURVE, so autoscale -- but on the 5th
                # and 95th percentile, not min/max. One spike would otherwise
                # flatten the whole trace against an axis.
                v = vals[finite]
                st_["min"] = float(np.percentile(v, 5))
                st_["max"] = float(np.percentile(v, 95))
                if st_["min"] == st_["max"]:
                    st_["max"] = st_["min"] + 1.0
                st_["auto"] = True
            pts = _decimate(depth.tolist(),
                            [None if not f else float(x)
                             for x, f in zip(vals, finite)],
                            MAX_POINTS_PER_CURVE)
            curves.append({
                "name": str(m), "colour": st_["colour"], "dash": st_["dash"],
                "min": st_["min"], "max": st_["max"], "log": bool(st_["log"]),
                "unit": st_["unit"], "auto": bool(st_.get("auto")),
                "d": [round(d, 2) for d, _v in pts],
                "v": [None if v is None else round(v, 4) for _d, v in pts],
            })
        if curves:
            tracks.append({"title": t["title"], "curves": curves})

    return {"lo": lo, "hi": hi, "tracks": tracks, "held": held,
            "title": title, "subtitle": subtitle,
            "depth_name": str(depth_col)}


# ── the page ──────────────────────────────────────────────────────────────
_HTML = r"""
<style>
* { box-sizing: border-box; }
.slog { font-family: ui-sans-serif, system-ui, "Segoe UI", sans-serif;
        color: #14181F; background: #FFFFFF; }
.slog .bar { position: sticky; top: 0; z-index: 8; background: #F1F3F7;
             border-bottom: 1px solid #C7CEDA; padding: 5px 9px;
             display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.slog .bar .t { font-weight: 600; font-size: 13px; margin-right: auto; }
.slog .bar .t small { font-weight: 400; color: #6B7683; margin-left: 8px; }
.slog .bar label { font-size: 10px; letter-spacing: .08em;
                   text-transform: uppercase; color: #6B7683; }
.slog .bar input[type=range] { width: 110px; vertical-align: middle; }
.slog .bar button { font: inherit; font-size: 12px; padding: 2px 8px;
                    border: 1px solid #C7CEDA; background: #fff;
                    border-radius: 3px; cursor: pointer; }
.slog .bar button:hover { background: #E7EBF2; }
.slog .now { font-variant-numeric: tabular-nums; font-weight: 600;
             color: #1B4F9C; font-size: 13px; min-width: 5.5em;
             text-align: right; }
.slog .heads { position: sticky; top: 30px; z-index: 7; height: 30px;
               background: #E7EBF2; border-bottom: 1px solid #C7CEDA; }
.slog .heads .h { position: absolute; top: 0; height: 30px; overflow: hidden;
                  border-left: 1px solid #C7CEDA; padding: 2px 4px;
                  font-size: 9.5px; line-height: 1.15; text-align: center;
                  color: #3C4855; }
.slog .heads .h b { display: block; font-size: 10px; }
.slog .grip { position: absolute; top: 0; height: 30px; width: 9px;
              margin-left: -4px; cursor: col-resize; z-index: 9; }
.slog .grip:hover { background: rgba(27,79,156,.25); }
.slog .wrap { overflow: auto; background: #fff; }
.slog .sheet { position: relative; }
.slog .trk { position: absolute; top: 0; bottom: 0;
             border-left: 1px solid #C9CFD8; }
.slog .trk:first-child { border-left: 0; }
.slog .trk svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.slog .dt { position: absolute; left: 0; right: 0; text-align: center;
            font-size: 9.5px; color: #14181F; background: #fff;
            font-variant-numeric: tabular-nums; }
</style>
<div class="slog">
  <div class="bar">
    <span class="t" id="ttl"></span>
    <label>Scale</label>
    <button type="button" id="zo">&minus;</button>
    <input type="range" id="z" min="0.2" max="20" step="0.1" value="2">
    <button type="button" id="zi">+</button>
    <span id="zv" style="font-size:11px;color:#6B7683;min-width:4.5em"></span>
    <button type="button" id="rs">Reset</button>
    <span class="now" id="now"></span>
  </div>
  <div class="heads" id="heads"></div>
  <div class="wrap" id="wrap"><div class="sheet" id="sheet"></div></div>
</div>
<script>
(function () {
  var S = __SPEC__, H = __HEIGHT__;
  var SVGNS = "http://www.w3.org/2000/svg";
  var wrap = document.getElementById("wrap");
  var sheet = document.getElementById("sheet");
  var heads = document.getElementById("heads");
  var span = S.hi - S.lo;
  var ppf = 2, widths = null;

  document.getElementById("ttl").innerHTML =
    S.title + (S.subtitle ? " <small>" + S.subtitle + "</small>" : "");
  wrap.style.height = (H - 62) + "px";

  function norm(v, c) {
    if (v === null) return null;
    var a = c.min, b = c.max;
    if (c.log) {
      var la = Math.log10(Math.max(a, 1e-6)), lb = Math.log10(Math.max(b, 1e-6));
      var x = (Math.log10(Math.max(v, Math.max(a, 1e-6))) - la) / (lb - la);
      return Math.max(0, Math.min(1, x));
    }
    // a > b is a REVERSED track (neutron, density porosity, Sw). The same
    // expression handles it, which is why the scale is stored as an ordered
    // pair rather than a range plus a flag that can disagree with it.
    if (a === b) return 0.5;
    return Math.max(0, Math.min(1, (v - a) / (b - a)));
  }

  var TRACKS = S.tracks;
  function build() {
    sheet.textContent = ""; heads.textContent = "";
    TRACKS.forEach(function (t, i) {
      var d = document.createElement("div");
      d.className = "trk"; sheet.appendChild(d); t.el = d;

      var h = document.createElement("div");
      h.className = "h";
      h.innerHTML = "<b>" + t.title + "</b>" +
        t.curves.map(function (c) {
          return "<span style='color:" + c.colour + "'>" + c.name +
                 (c.auto ? "*" : "") + "</span>";
        }).join(" · ");
      heads.appendChild(h); t.h = h;

      if (i > 0) {
        var g = document.createElement("div");
        g.className = "grip"; g.dataset.i = i;
        heads.appendChild(g); t.g = g;
      }

      var g2 = document.createElementNS(SVGNS, "svg");
      g2.setAttribute("preserveAspectRatio", "none");
      g2.setAttribute("viewBox", "0 0 1 " + span);
      for (var dd = Math.ceil(S.lo / 50) * 50; dd <= S.hi; dd += 50) {
        var ln = document.createElementNS(SVGNS, "line");
        ln.setAttribute("x1", 0); ln.setAttribute("x2", 1);
        ln.setAttribute("y1", dd - S.lo); ln.setAttribute("y2", dd - S.lo);
        ln.setAttribute("stroke", (dd % 250 === 0) ? "#B9C2CF" : "#EBEEF3");
        ln.setAttribute("stroke-width", (dd % 250 === 0) ? 0.9 : 0.5);
        ln.setAttribute("vector-effect", "non-scaling-stroke");
        g2.appendChild(ln);
      }
      t.curves.forEach(function (c) {
        var pts = [], segs = [];
        for (var k = 0; k < c.d.length; k++) {
          var x = norm(c.v[k], c);
          if (x === null) { if (pts.length > 1) segs.push(pts); pts = []; }
          else { pts.push(x.toFixed(4) + "," + (c.d[k] - S.lo).toFixed(2)); }
        }
        if (pts.length > 1) segs.push(pts);
        segs.forEach(function (p) {
          var pl = document.createElementNS(SVGNS, "polyline");
          pl.setAttribute("points", p.join(" "));
          pl.setAttribute("fill", "none");
          pl.setAttribute("stroke", c.colour);
          pl.setAttribute("stroke-width", 1);
          if (c.dash) pl.setAttribute("stroke-dasharray", "4 3");
          pl.setAttribute("vector-effect", "non-scaling-stroke");
          g2.appendChild(pl);
        });
      });
      d.appendChild(g2);
    });

    // the depth column, its own track between 1 and 2
    var dep = document.createElement("div");
    dep.className = "trk"; dep.id = "depcol"; sheet.appendChild(dep);
    for (var dd = Math.ceil(S.lo / 100) * 100; dd <= S.hi; dd += 100) {
      var e = document.createElement("div");
      e.className = "dt"; e.dataset.depth = dd; e.textContent = dd;
      dep.appendChild(e);
    }
    var dh = document.createElement("div");
    dh.className = "h"; dh.innerHTML = "<b>" + S.depth_name + "</b>";
    heads.appendChild(dh); dep.h = dh;
    sheet.depCol = dep;
  }

  function layout() {
    if (!widths) {
      var avail = Math.max(520, wrap.clientWidth - 4);
      var each = (avail - 46) / TRACKS.length;
      widths = TRACKS.map(function () { return each; });
      widths.depth = 46;
    }
    var W = widths.reduce(function (a, b) { return a + b; }, 0) + widths.depth;
    sheet.style.width = W + "px";
    sheet.style.height = (span * ppf) + "px";
    var x = 0;
    TRACKS.forEach(function (t, i) {
      // depth column sits after the first track, the way a log is printed
      if (i === 1) {
        sheet.depCol.style.left = x + "px";
        sheet.depCol.style.width = widths.depth + "px";
        sheet.depCol.h.style.left = x + "px";
        sheet.depCol.h.style.width = widths.depth + "px";
        x += widths.depth;
      }
      t.el.style.left = x + "px"; t.el.style.width = widths[i] + "px";
      t.h.style.left = x + "px"; t.h.style.width = widths[i] + "px";
      if (t.g) t.g.style.left = x + "px";
      x += widths[i];
    });
    if (TRACKS.length < 2) {
      sheet.depCol.style.left = x + "px";
      sheet.depCol.style.width = widths.depth + "px";
      sheet.depCol.h.style.left = x + "px";
      sheet.depCol.h.style.width = widths.depth + "px";
    }
    var nodes = sheet.querySelectorAll("[data-depth]");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].style.top = ((+nodes[i].dataset.depth - S.lo) * ppf - 6) + "px";
    }
    document.getElementById("zv").textContent =
      (ppf < 1 ? ppf.toFixed(2) : ppf.toFixed(1)) + " px/ft";
    readDepth();
  }

  function readDepth() {
    var d = S.lo + wrap.scrollTop / ppf;
    document.getElementById("now").textContent = d.toFixed(0) + " ft";
  }
  function setZoom(v) {
    var at = S.lo + wrap.scrollTop / ppf;
    // ROUND FINER THAN THE STEP YOU MULTIPLY BY, or zoom sticks. Snapping
    // to half-units made 0.5 x 1.4 = 0.7 round straight back to 0.5, so on a
    // long log -- which opens at the bottom of the range -- the + button did
    // nothing at all and the control looked broken.
    ppf = Math.max(0.2, Math.min(20, Math.round(v * 10) / 10));
    document.getElementById("z").value = ppf;
    layout();
    wrap.scrollTop = (at - S.lo) * ppf;
    readDepth();
  }
  document.getElementById("z").addEventListener("input", function () {
    setZoom(+this.value);
  });
  document.getElementById("zi").addEventListener("click", function () {
    setZoom(ppf * 1.4);
  });
  document.getElementById("zo").addEventListener("click", function () {
    setZoom(ppf / 1.4);
  });
  document.getElementById("rs").addEventListener("click", function () {
    widths = null; setZoom(2);
  });
  wrap.addEventListener("scroll", readDepth, {passive: true});

  var drag = null;
  heads.addEventListener("pointerdown", function (e) {
    var g = e.target.closest(".grip"); if (!g) return;
    e.preventDefault();
    drag = {i: +g.dataset.i, x: e.clientX, w: widths[+g.dataset.i - 1]};
  });
  window.addEventListener("pointermove", function (e) {
    if (!drag) return;
    // Only the dragged column changes; everything right of it shifts.
    widths[drag.i - 1] = Math.max(40, drag.w + (e.clientX - drag.x));
    layout();
  });
  window.addEventListener("pointerup", function () { drag = null; });

  build();
  // fit the whole log on first sight, then let the reader zoom in
  ppf = Math.max(0.2, Math.min(20, (H - 90) / span));
  document.getElementById("z").value = ppf;
  layout();
})();
</script>
"""


def render(df, depth_col=None, file_path="", engine=None, height=760,
           key=None, title=None):
    """Draw the strip. Returns False when there is nothing plottable."""
    import streamlit as st
    import streamlit.components.v1 as components

    spec = build_spec(
        df, depth_col=depth_col, engine=engine,
        title=title or (os.path.basename(file_path) if file_path else "Log"),
        subtitle="")
    if not spec or not spec["tracks"]:
        return False

    n_curves = sum(len(t["curves"]) for t in spec["tracks"])
    spec["subtitle"] = "%.0f–%.0f ft · %d curves in %d tracks" % (
        spec["lo"], spec["hi"], n_curves, len(spec["tracks"]))

    html = (_HTML.replace("__SPEC__", json.dumps(spec))
                 .replace("__HEIGHT__", str(int(height))))
    components.html(html, height=height, scrolling=False)
    notes = []
    auto = sorted({c["name"] for t in spec["tracks"] for c in t["curves"]
                   if c.get("auto")})
    if auto:
        notes.append("Autoscaled (5th–95th percentile), no catalogued scale: "
                     + ", ".join(auto[:10]) + (" …" if len(auto) > 10 else ""))
    if spec.get("held"):
        h = sorted(set(spec["held"]))
        notes.append("%d curve(s) not on the standard display: %s%s"
                     % (len(h), ", ".join(h[:10]),
                        " …" if len(h) > 10 else ""))
    if notes:
        st.caption(" · ".join(notes) + "  — set a family and scale in "
                   "Reference Tables → dv_r_log_mnemonic to place them.")
    return True
