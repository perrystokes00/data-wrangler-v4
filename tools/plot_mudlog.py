"""Draw a mud log strip from a WellSight export -- the viewer's plot, rebuilt.

    python tools/plot_mudlog.py                          # 5250-5400 ft
    python tools/plot_mudlog.py --top 5400 --base 5600
    python tools/plot_mudlog.py --overview --columns 1    # the whole log

Reads the .dat written by the viewer's File > Export > "Write all data to an
ASCII file". Everything drawn here is a value the viewer itself wrote out:
curve names, units, sample step, the scales AND THE DEPTHS THEY CHANGE AT,
coded lithology, coded oil shows, and the three text tracks.

WHAT THIS REPLACED, AND WHY IT IS WORTH THE PARAGRAPH
-----------------------------------------------------
The first version read the .LOG binary directly, because that was the only way
in before the viewer was installed. It found a float32 array and called it ROP
on four grounds that each looked like independent confirmation:

    integer-valued          a mud logger records whole minutes per foot
    97% inside 0-30         which is the scale the viewer prints
    spikes to 122           hard rock running off-scale
    2,606 at 2.0 ft         exactly the logged interval

The array is TG. Total gas. Real ROP tops out at 45 with a mean of 2.45; that
array peaks at 122 with a mean of 11.22, which is TG to two decimals. Its
neighbour at 0x078F0E, which looked like a scaled copy of it, is C1 -- methane
is a component of total gas, which is why the two tracked each other so
closely that the ratio looked like a display artefact.

The "0-30 scale" argument was the weakest and the most persuasive. The ROP
scale is 0-5 for most of the hole and changes four times:

    520 ft  0-5      5200 ft  0-10      5300 ft  0-30      5656 ft  0-10

so the curve was being tested against a range that governs 356 ft of a 5,215 ft
log, and drawn at six times too wide a scale everywhere else. Four weak
arguments agreeing is not four confirmations; it is one assumption wearing four
hats. What caught it was not more analysis -- it was putting the plot beside
the viewer's own render and asking a geologist whether they looked alike.

tools/load_mudlog.py still reads the binary and still writes dv_well_mud_log
and dv_well_shows. It needs only the header and the descriptions and gets both
right. It does not identify curves, and this plot no longer asks it to.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mudlog_export import parse

DEFAULT_DAT = r"C:\Bulk\mudlog_test\export.dat"

# The 21 codes the Lithology track declares, in the colours a geologist reads
# without a legend: sand yellow, shale grey, carbonate blue, evaporite magenta.
LITH_COLOUR = {
    "SS": "#F2D64B", "SLTST": "#B7A05E", "CONGL": "#D8B26A",
    "SHALE": "#8E9AA6", "SHGY": "#7E8A96", "SHCOL": "#9AA6B2",
    "CLYST": "#A69A8E", "BENT": "#C2B89E",
    "DOL": "#7FB2E5", "LMST": "#5FC8D6", "MRLST": "#9CC9D6",
    "ANHY": "#D96FD9", "GYP": "#E2A0E2", "SALT": "#F0DCEE",
    "CHT": "#6E7C8A", "COAL": "#2A2A2A", "BREC": "#B08A7A",
    "IGNE": "#C06A5A", "META": "#A07AA0", "TILL": "#BFB0A0",
    "BLANK": "#FFFFFF",
}
LITH_HATCH = {"SS": "...", "SHALE": "---", "SHGY": "---", "SHCOL": "---",
              "DOL": "///", "LMST": "\\\\\\", "ANHY": "xxx", "GYP": "xxx",
              "SLTST": "..."}

# TG first and heaviest -- it is the envelope the components sit inside.
GAS_STYLE = [("TG", "#111111", 0.9), ("C1", "#C0392B", 0.6),
             ("C2", "#1F6FB2", 0.6), ("C3", "#1E8449", 0.6),
             ("C4", "#B9770E", 0.6)]

_ENG_READING = re.compile(
    r"^\s*(WOB|RPM|PP|SPM|WT|VIS|WL|FC|PH|CL|Ca|MW|FL)\b", re.I)


def formation_tops(ex, max_repeats=2):
    """The entries in the annotation tracks that are FORMATION TOPS.

    A TOP IS NAMED ONCE; AN OPERATION REPEATS. "CG" -- connection gas -- occurs
    about twenty-five times down this hole, "Trip Gas" a dozen, and a rule that
    labels every non-reading annotation buries Mowry, Dakota and Alcova under
    them. Counting the repeats separates the two without a keyword list, which
    matters because the vocabulary is the mud logger's, not a standard.

    Dates and anything carrying digits or footage marks are operations too --
    a top is a name.

    THE REPEAT COUNT ALONE IS NOT ENOUGH. Nine one-off entries survive it --
    "Trip for bit", "Wait on bit repairs", "No drill rate", "Drilling Ahead" --
    because they happen once and carry no digits. What separates them is that
    an operation is a VERB and a formation is a NAME, so a short list of rig
    vocabulary does the rest. It is a list, and lists rot; the repeat count
    carries most of the work and this only catches its tail."""
    from collections import Counter
    ops_word = re.compile(
        r"\b(trip|wait|drill|drilling|pressure|gas|circ|repair|repairs|bit|"
        r"bha|toh|tih|rate|log|logging|released|change|core|ahead|cut|set|"
        r"mix|commence|commenced)\b", re.I)
    seen = Counter()
    rows = []
    for name in ("Eng. Data 2", "Eng. Data"):
        for d, txt in ex.text(name):
            t = " ".join(txt.split())
            if _ENG_READING.match(t):
                continue
            rows.append((d, t))
            seen[t] += 1
    tops, ops = [], []
    for d, t in sorted(rows):
        looks = (seen[t] <= max_repeats and len(t) <= 26
                 and not re.search(r"\d|'|\"|&|/", t)
                 and not ops_word.search(t))
        (tops if looks else ops).append((d, t))
    return tops, ops

SHOW_COLOUR = {"EVEN": "#0B7A2F", "SPOTTED": "#C08A1E", "DEAD": "#8A6A4A",
               "QUES": "#9AA3AE"}


def _norm(value, lo, hi, log=False):
    """A value in 0..1 of its track, or None.

    CURVES ARE DRAWN IN TRACK SPACE, NOT VALUE SPACE, because the scale changes
    with depth. Normalising each sample against the scale in force where it
    sits is what keeps a piecewise-scaled curve continuous on the page -- which
    is what the viewer does when it reprints the legend mid-log."""
    if value is None or lo is None or hi is None or hi == lo:
        return None
    if log:
        import math
        lo_ = max(lo, 1e-6)
        v = max(value, lo_)
        return min(max((math.log10(v) - math.log10(lo_))
                       / (math.log10(hi) - math.log10(lo_)), 0.0), 1.0)
    return min(max((value - lo) / (hi - lo), 0.0), 1.0)


def _curve_xy(track, lo, hi, log=False):
    """Line segments for a curve, broken at nulls rather than joined across."""
    segs, cur = [], ([], [])
    for d, v in track.data:
        if not lo - 10 <= d <= hi + 10:
            continue
        mn, mx = track.scale_at(d)
        x = _norm(v, mn, mx, log)
        if x is None:
            if len(cur[0]) > 1:
                segs.append(cur)
            cur = ([], [])
        else:
            cur[0].append(x)
            cur[1].append(d)
    if len(cur[0]) > 1:
        segs.append(cur)
    return segs


def _wrap(text, width, maxlines=7):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines[:maxlines])


def _lith_spans(ex, lo, hi):
    out = []
    for top, base, labels in ex.choice("Lithology"):
        base = base if base is not None else top + 2.0
        if base < lo or top > hi:
            continue
        out.append((max(top, lo), min(base, hi),
                    labels[0] if labels else "BLANK"))
    return out


def _frame(ax, lo, hi):
    ax.set_ylim(hi, lo)                      # depth increases downward
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_color("#7A7A7A")
        sp.set_linewidth(0.7)


# The track widths, shared by the detail plot and by the HTML header that sits
# above a scrolling strip -- they have to agree or the sticky labels drift off
# the columns they name.
TRACK_RATIOS = [2.6, 0.42, 0.55, 4.0, 2.2, 2.6]
TRACK_NAMES = ["ROP min/ft", "Depth", "Lith", "Sample descriptions",
               "Gas 1-1000 log", "Shows · tops · cores · operations"]
MARGIN_L, MARGIN_R = 0.025, 0.988


def track_bounds():
    """Left/right edge of each track as a fraction of image width.

    Published so a caller drawing its own header can line up with the plot.
    Deriving it here rather than restating the numbers in the HTML is what
    stops the two from drifting the first time a ratio is tuned."""
    total = float(sum(TRACK_RATIOS))
    span = MARGIN_R - MARGIN_L
    out, x = [], MARGIN_L
    for r in TRACK_RATIOS:
        w = span * r / total
        out.append((x, x + w))
        x += w
    return out


def draw(ex, lo, hi, out, scale=5.0, width=17.0, dpi=150, bare=False):
    """The detail plot, in the viewer's own track order.

    bare=True drops the title and the axis labels and pins the margins to a
    FIXED FRACTION rather than a fixed number of inches. That is what makes
    tiles stack into a continuous roll: every tile is the same width, its
    tracks land on the same pixels, and the depth scale runs unbroken across
    the seam. bbox_inches="tight" is also skipped -- it crops to content, so
    two tiles with different amounts of text come out different widths and the
    columns visibly jump at every join."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    height = max(5.0, (hi - lo) / 100.0 * scale)
    k = scale / 5.0
    fig = plt.figure(figsize=(width, height), facecolor="white")
    top_m = 1.0 if bare else 1 - 0.42 / height
    bot_m = 0.0 if bare else 0.5 / height
    gs = fig.add_gridspec(1, 6, width_ratios=TRACK_RATIOS,
                          wspace=0.02, left=MARGIN_L, right=MARGIN_R,
                          top=top_m, bottom=bot_m)
    ax_rop, ax_dep, ax_lith, ax_desc, ax_gas, ax_ann = (
        fig.add_subplot(gs[0, i]) for i in range(6))
    for ax in (ax_rop, ax_dep, ax_lith, ax_desc, ax_gas, ax_ann):
        _frame(ax, lo, hi)

    first = int(lo // 50) * 50
    for d in range(first, int(hi) + 51, 10):
        if not lo <= d <= hi:
            continue
        heavy = (d % 50 == 0)
        for ax in (ax_rop, ax_lith, ax_desc, ax_gas, ax_ann):
            ax.axhline(d, color="#000000" if heavy else "#DDDDDD",
                       lw=0.8 if heavy else 0.4, zorder=1 if heavy else 0)
        ax_dep.axhline(d, color="#000000" if heavy else "#DDDDDD",
                       lw=0.8 if heavy else 0.4)
        if heavy:
            ax_dep.text(0.5, d, "%d" % d, ha="center", va="center",
                        fontsize=8.5 * k, rotation=90,
                        bbox=dict(fc="white", ec="none", pad=0.6))
    ax_dep.set_xlim(0, 1)

    # ── ROP ─────────────────────────────────────────────────────────────
    rop = ex.curve("ROP")
    ax_rop.set_xlim(0, 1)
    for v in (0.25, 0.5, 0.75):
        ax_rop.axvline(v, color="#EAEAEA", lw=0.5, zorder=0)
    if rop:
        for xs, ys in _curve_xy(rop, lo, hi):
            ax_rop.plot(xs, ys, color="#111111", lw=0.8, zorder=3)
        for d, mn, mx in rop.scales:
            if lo < d < hi:
                ax_rop.axhline(d, color="#C0392B", lw=1.0, ls="--", zorder=4)
                ax_rop.text(0.02, d, "scale %g–%g" % (mn, mx),
                            fontsize=6.6 * k, color="#C0392B", va="bottom",
                            zorder=5,
                            bbox=dict(fc="white", ec="none", alpha=0.85,
                                      pad=0.4))
        mn, mx = rop.scale_at((lo + hi) / 2.0)
        if not bare:
            ax_rop.set_xlabel("ROP %s   %g – %g" % (rop.unit, mn, mx),
                              fontsize=8, labelpad=2)

    for d, txt in ex.text("Eng. Data"):
        if lo <= d <= hi and _ENG_READING.match(txt):
            ax_rop.text(0.54, d, txt[:24], fontsize=6.4 * k, va="center",
                        zorder=5,
                        bbox=dict(fc="white", ec="none", alpha=0.85, pad=0.4))

    # ── lithology, coded ────────────────────────────────────────────────
    ax_lith.set_xlim(0, 1)
    for top, base, name in _lith_spans(ex, lo, hi):
        ax_lith.add_patch(Rectangle(
            (0, top), 1, base - top,
            facecolor=LITH_COLOUR.get(name, "#DDDDDD"),
            hatch=LITH_HATCH.get(name, ""), edgecolor="#666666", lw=0.3,
            zorder=2))
    if not bare:
        ax_lith.set_xlabel("Lith", fontsize=8, labelpad=2)

    # ── descriptions ────────────────────────────────────────────────────
    ax_desc.set_xlim(0, 1)
    for d, txt in ex.text("Geol. Descrs."):
        if lo <= d <= hi:
            ax_desc.text(0.012, d, _wrap(txt, 66), fontsize=7.0 * k,
                         va="top", ha="left", color="#0F3D8C", zorder=4,
                         linespacing=1.16)
    if not bare:
        ax_desc.set_xlabel("Sample descriptions", fontsize=8, labelpad=2)

    # ── gas ─────────────────────────────────────────────────────────────
    ax_gas.set_xlim(0, 1)
    for v in (0.33, 0.67):
        ax_gas.axvline(v, color="#EAEAEA", lw=0.5, zorder=0)
    drawn = []
    for name, colour, lw in GAS_STYLE:
        t = ex.curve(name)
        if not t:
            continue
        segs = _curve_xy(t, lo, hi, log=True)
        if not segs:
            continue
        for xs, ys in segs:
            ax_gas.plot(xs, ys, color=colour, lw=lw, zorder=3)
        drawn.append(name)
    if drawn and not bare:
        tg = ex.curve("TG")
        mn, mx = tg.scale_at((lo + hi) / 2.0) if tg else (1, 1000)
        ax_gas.set_xlabel("Gas %s  %g–%g log\n%s"
                          % (tg.unit if tg else "", mn, mx, "  ".join(drawn)),
                          fontsize=7.6, labelpad=2)

    # ── shows, tops, cores, operations ──────────────────────────────────
    ax_ann.set_xlim(0, 1)
    for d, _b, labels in ex.choice("Oil Shows"):
        if lo <= d <= hi:
            lab = labels[0] if labels else "?"
            ax_ann.plot([0.032], [d], marker="o", ms=3.6,
                        color=SHOW_COLOUR.get(lab, "#9AA3AE"), zorder=5)
    # THE LINE ALWAYS DRAWS, THE LABEL SOMETIMES DOESN'T. Tops, core points and
    # dates land within a few feet of each other around a coring run -- Opeche,
    # "Core #2", and a date all inside 8 ft here -- and printing every one
    # overprints them into an unreadable stack. Dropping the LABEL keeps the
    # depth mark honest; dropping the line would lose the datum.
    events = []
    for name in ("Eng. Data 2", "Eng. Data"):
        for d, txt in ex.text(name):
            if lo <= d <= hi and not _ENG_READING.match(txt):
                events.append((d, txt))
    seen, last = set(), -1e9
    min_gap = (hi - lo) / (height * 9.0)
    for d, txt in sorted(events):
        if (d, txt) in seen:
            continue
        seen.add((d, txt))
        core = re.match(r"^\s*Core", txt, re.I)
        colour = "#A3231E" if core else "#1F6B3A"
        ax_ann.axhline(d, color=colour, lw=0.9, zorder=3)
        if d - last < min_gap:
            continue
        last = d
        ax_ann.text(0.07, d, _wrap(txt, 40, 4), fontsize=7.0 * k,
                    va="top", color=colour, zorder=4,
                    bbox=dict(fc="white", ec="none", alpha=0.9, pad=0.5))
    if not bare:
        ax_ann.set_xlabel("Shows · tops · cores · operations",
                          fontsize=8, labelpad=2)

    if bare:
        fig.savefig(out, dpi=dpi, facecolor="white")
        plt.close(fig)
        return out
    h = ex.header
    fig.suptitle("%s   ·   %.0f – %.0f ft   ·   KB %s   ·   %s"
                 % (h.get("Well Name", "?"), lo, hi,
                    h.get("K.B. Elevation", "?"),
                    h.get("Geologist Name", "")),
                 fontsize=11, y=1 - 0.1 / height)
    fig.savefig(out, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def draw_overview(ex, out, height_in=9.5, width_in=16.0, dpi=150, columns=1):
    """The whole log on one sheet, in side-by-side depth columns.

    The descriptions are dropped: at one screen height this runs about 6 ft to
    the pixel and they cannot be set legibly. What survives compression is the
    rock, the drilling rate, the gas and the tops."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Patch

    lo_all, hi_all = ex.depth_range
    lo_all = lo_all if lo_all is not None else 520.0
    hi_all = hi_all if hi_all is not None else 5780.0
    rop = ex.curve("ROP")
    tops, ops = formation_tops(ex)
    per = (hi_all - lo_all) / columns

    fig = plt.figure(figsize=(width_in, height_in), facecolor="white")
    outer = fig.add_gridspec(1, columns, wspace=0.07, left=0.028, right=0.992,
                             top=0.932, bottom=0.085)

    for col in range(columns):
        lo = lo_all + col * per
        hi = lo + per
        # THE RATIOS DEPEND ON HOW MANY COLUMNS SHARE THE SHEET. At one column
        # the whole width goes to a single strip, so it goes to the tracks that
        # can use it; at two or more every track is fighting for room.
        ratios = ([2.6, 0.34, 0.62, 0.30, 2.2, 1.5] if columns == 1
                  else [1.7, 0.42, 0.50, 0.28, 1.3, 1.5])
        inner = outer[0, col].subgridspec(1, 6, width_ratios=ratios,
                                          wspace=0.035)
        ax_rop, ax_dep, ax_lith, ax_show, ax_gas, ax_top = (
            fig.add_subplot(inner[0, n]) for n in range(6))
        for ax in (ax_rop, ax_dep, ax_lith, ax_show, ax_gas, ax_top):
            _frame(ax, lo, hi)

        step = 100 if per <= 1600 else 200
        for d in range(int(lo // step) * step, int(hi) + step + 1, step):
            if not lo <= d <= hi:
                continue
            heavy = (d % (step * 5) == 0)
            for ax in (ax_rop, ax_lith, ax_show, ax_gas, ax_top):
                ax.axhline(d, color="#000000" if heavy else "#E4E4E4",
                           lw=0.55 if heavy else 0.3,
                           zorder=1 if heavy else 0)
            ax_dep.axhline(d, color="#000000" if heavy else "#E4E4E4",
                           lw=0.55 if heavy else 0.3)
            if heavy:
                ax_dep.text(0.5, d, "%d" % d, ha="center", va="center",
                            fontsize=7.0,
                            bbox=dict(fc="white", ec="none", pad=0.4))
        ax_dep.set_xlim(0, 1)

        ax_rop.set_xlim(0, 1)
        if rop:
            for xs, ys in _curve_xy(rop, lo, hi):
                ax_rop.plot(xs, ys, color="#16181C", lw=0.45, zorder=3)
            for d, mn, mx in rop.scales:
                if lo < d < hi:
                    ax_rop.axhline(d, color="#C0392B", lw=0.8, ls="--",
                                   zorder=4)
                    ax_rop.text(0.02, d, "%g–%g" % (mn, mx), fontsize=6.0,
                                color="#C0392B", va="bottom", zorder=5,
                                bbox=dict(fc="white", ec="none", alpha=0.85,
                                          pad=0.3))

        ax_lith.set_xlim(0, 1)
        for top, base, name in _lith_spans(ex, lo, hi):
            ax_lith.add_patch(Rectangle(
                (0, top), 1, base - top,
                facecolor=LITH_COLOUR.get(name, "#DDDDDD"),
                edgecolor="none", zorder=2))

        ax_show.set_xlim(0, 1)
        for d, _b, labels in ex.choice("Oil Shows"):
            if not lo <= d <= hi:
                continue
            lab = labels[0] if labels else "?"
            ax_show.add_patch(Rectangle(
                (0.1, d), 0.8, max(per / 300.0, 2.0),
                facecolor=SHOW_COLOUR.get(lab, "#9AA3AE"),
                edgecolor="none", zorder=3))

        ax_gas.set_xlim(0, 1)
        for name, colour, lw in GAS_STYLE:
            t = ex.curve(name)
            if not t:
                continue
            for xs, ys in _curve_xy(t, lo, hi, log=True):
                ax_gas.plot(xs, ys, color=colour, lw=lw * 0.6, zorder=3)

        ax_top.set_xlim(0, 1)
        # Operations get an unlabelled tick at the right edge; only the tops
        # are named. See formation_tops for why counting repeats is the test.
        for d, _txt in ops:
            if lo <= d <= hi:
                ax_top.plot([0.965], [d], marker="_", ms=6, mew=1.0,
                            color="#A3231E", zorder=3)
        min_gap = per / (height_in * 9.0)
        last = -1e9
        for d, txt in tops:
            if not lo <= d <= hi:
                continue
            ax_top.axhline(d, color="#1F6B3A", lw=0.9, zorder=3)
            if d - last < min_gap:
                continue
            last = d
            ax_top.text(0.04, d, "%s  %.0f" % (txt, d), fontsize=6.8,
                        va="center", color="#14532B", zorder=4,
                        bbox=dict(fc="white", ec="none", alpha=0.9, pad=0.35))

        if col == 0:
            mn, mx = rop.scale_at(lo) if rop else (0, 5)
            ax_rop.set_xlabel("ROP min/ft\n%g–%g at top" % (mn, mx),
                              fontsize=7.0, labelpad=2, color="#444444")
            ax_lith.set_xlabel("Lith", fontsize=7.0, labelpad=2,
                               color="#444444")
            ax_show.set_xlabel("Show", fontsize=7.0, labelpad=2,
                               color="#444444")
            ax_gas.set_xlabel("Gas 1–1000 log", fontsize=7.0, labelpad=2,
                              color="#444444")
            ax_top.set_xlabel("Tops · cores", fontsize=7.0, labelpad=2,
                              color="#444444")
        ax_dep.set_xlabel("%d–%d" % (lo, hi), fontsize=7.0, labelpad=2,
                          color="#444444")

    used = []
    for _t, _b, labels in ex.choice("Lithology"):
        if labels and labels[0] not in used:
            used.append(labels[0])
    handles = [Patch(facecolor=LITH_COLOUR.get(n, "#DDDDDD"), label=n)
               for n in sorted(used)]
    handles += [Patch(facecolor=c, label=n) for n, c, _lw in GAS_STYLE]
    fig.legend(handles=handles, loc="lower center",
               ncol=min(len(handles), 16), frameon=False, fontsize=7.0,
               bbox_to_anchor=(0.5, 0.004), handlelength=1.1,
               handleheight=0.9, columnspacing=1.3)

    h = ex.header
    fig.suptitle("%s   ·   %.0f–%.0f ft   ·   KB %s   ·   "
                 "%d descriptions · %d coded lithology · %d shows"
                 % (h.get("Well Name", "?"), lo_all, hi_all,
                    h.get("K.B. Elevation", "?"),
                    len(ex.text("Geol. Descrs.")),
                    len(ex.choice("Lithology")), len(ex.choice("Oil Shows"))),
                 fontsize=10.0, y=0.975)
    fig.savefig(out, dpi=dpi, facecolor="white")
    plt.close(fig)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", default=DEFAULT_DAT,
                    help="the .dat from File > Export > All Data")
    ap.add_argument("--top", type=float, default=5250.0)
    ap.add_argument("--base", type=float, default=5400.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--overview", action="store_true")
    ap.add_argument("--tiles", metavar="DIR",
                    help="render the whole log as seamless tiles into "
                         "DIR, for a scrolling strip")
    ap.add_argument("--tile-ft", type=float, default=500.0,
                    help="feet per tile")
    ap.add_argument("--columns", type=int, default=1)
    ap.add_argument("--scale", type=float, default=5.0,
                    help="detail plot: inches per 100 ft")
    ap.add_argument("--height", type=float, default=9.5,
                    help="overview: height in inches")
    ap.add_argument("--width", type=float, default=17.0)
    ap.add_argument("--dpi", type=int, default=150)
    a = ap.parse_args(argv)

    if not os.path.exists(a.export):
        raise SystemExit(
            "No export at %s.\n"
            "Open the .LOG in the WellSight viewer and use\n"
            "  File > Export > Write all data to an ASCII file" % a.export)
    ex = parse(a.export)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if a.tiles:
        os.makedirs(a.tiles, exist_ok=True)
        lo_all, hi_all = ex.depth_range
        n, d = 0, lo_all
        while d < hi_all:
            top, base = d, min(d + a.tile_ft, hi_all)
            p = os.path.join(a.tiles, "tile_%02d.png" % n)
            draw(ex, top, base, p, scale=a.scale, width=a.width,
                 dpi=a.dpi, bare=True)
            print("   %s  %.0f-%.0f" % (os.path.basename(p), top, base))
            n += 1
            d = base
        print("%d tile(s) of %.0f ft in %s" % (n, a.tile_ft, a.tiles))
        return 0

    if a.overview:
        out = a.out or os.path.join(root, "mudlog_overview.png")
        draw_overview(ex, out, height_in=a.height, width_in=a.width,
                      dpi=a.dpi, columns=a.columns)
        print(out)
        print("   whole log · %d column(s) · %.0f x %.0f px"
              % (a.columns, a.width * a.dpi, a.height * a.dpi))
        return 0

    out = a.out or os.path.join(root, "mudlog_%d_%d.png"
                                % (int(a.top), int(a.base)))
    draw(ex, a.top, a.base, out, scale=a.scale, width=a.width, dpi=a.dpi)
    print(out)
    n = len([1 for d, _t in ex.text("Geol. Descrs.") if a.top <= d <= a.base])
    print("   %.0f-%.0f ft at %.1f in/100ft · %d description(s) in range"
          % (a.top, a.base, a.scale, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
