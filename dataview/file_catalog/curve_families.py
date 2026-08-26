"""Which petrophysical family a log curve mnemonic belongs to, and how a
standard triple-combo display is laid out.

    from dataview.file_catalog.curve_families import classify, propose_tracks
    classify("RILD")            -> ("RES_DEEP", "resistivity, deep")
    propose_tracks(["GRD","SPR","CALD","ILD","RHOB","NPHI"])
                                -> [Track(1, "GR / SP", [...]), ...]

WHY A LIST AND NOT A LOOKUP OF STANDARD NAMES
---------------------------------------------
The mnemonics in this database are VINTAGE, not modern. Counted over
dv_well_log_curve: the commonest SP is SPR (669), gamma is GRD (549) and GRR
(324) before plain GR (220), caliper is CALD (610), and the resistivity suite
runs ILD, RILM, RILD, ILM, SN, ASN, CILD, RFOC, SFL, IL, LL8. A list built
from a modern service-company mnemonic dictionary matches almost none of that
-- it would classify perhaps a fifth of the curves and quietly leave the rest
unassigned, which reads as "this log has no resistivity".

So the patterns below were written against what the catalogue actually holds,
and `unclassified()` exists to report what still falls through: a family list
is only as good as its miss rate, and the miss rate has to be visible.

THE TRACK LAYOUT is the conventional triple-combo, and its conventions are not
cosmetic:

    1  GR, SP            correlation and shale volume
    2  Caliper           hole condition; bit size is the reference
    3  Resistivity       LOGARITHMIC, 0.2-2000. Linear resistivity is
                         unreadable -- the useful range spans four decades.
    4  Neutron, Density  paired on REVERSED scales so the curves cross where
                         porosity and lithology disagree. The crossover is the
                         gas indicator; drawing them on independent scales
                         throws away the whole point of the pair.
    5  Sw                computed, 0-1

Anything unclassified goes to a track of its own rather than being dropped.
"""

import re

# (family, label, pattern) -- ORDER MATTERS. The first match wins, so the
# specific patterns precede the general ones: RILD must be read as deep
# induction before a bare "R" prefix can claim it.
FAMILIES = [
    # ── gamma ────────────────────────────────────────────────────────────
    ("GR",        "gamma ray",            r"^(SGR[DR]?|CGR|GR[DRNS]?|GRAY|GAMMA|GRC|GRTO|HGR|ECGR)$"),
    # ── spontaneous potential ────────────────────────────────────────────
    ("SP",        "spontaneous potential", r"^(SPR?|SPC|SSP|SPDH)$"),
    # ── caliper ──────────────────────────────────────────────────────────
    ("CAL",       "caliper",              r"^(CAL[DRIMXY]?|CALI|CALS|HCAL|MCAL|C[1-9]|C1[0-9]|CALP|HDIA|HMIN|HMNO)$"),
    # BIT SIZE IS THE CALIPER'S REFERENCE, not a curve on its own. Washouts
    # only mean anything against the hole you drilled, so it belongs in that
    # track and is styled as a straight reference line.
    ("BS",        "bit size",             r"^(BS|BITSIZE|BIT)$"),
    # ── resistivity, deepest first ───────────────────────────────────────
    # Array tools name their spacings: AHT/AT nn and RLAn run shallow to deep,
    # so the number decides the family. Written out rather than range-matched
    # because the two families number in opposite directions.
    ("RES_DEEP",  "resistivity, deep",    r"^(R?ILD|LL?D|RLLD|RDEP|RT|RD|AT90|AHT90|M2R9|DEEP|IDPH|RLA[45]|RLL|GURD?|GUARD)$"),
    ("RES_MED",   "resistivity, medium",  r"^(R?ILM|LLM|RLLM|RMED|RM|AT60|AT30|AHT60|AHT30|M2R6|MED|IMPH|RLA3)$"),
    ("RES_SHAL",  "resistivity, shallow", r"^(SFL[UR]?|RSFL|MSFL|RMLL|MLL|LL8|LLS|RLLS|RSHAL|RS|SN|ASN|LN|AT10|AT20|AHT10|AHT20|M2R1|RXOZ?|RFOC|CILD|MINV|MNOR|SHAL|RLA[12])$"),
    ("RES_OTHER", "resistivity, other",   r"^(IL|ILX|RES|RESIS|COND|CILM|CON[DM]?)$"),
    # ── porosity pair ────────────────────────────────────────────────────
    ("NEU",       "neutron porosity",     r"^(N?PHI|NPHI|TNPH|CN|CNC|CNCF|CNL|CNSS|CNLS|NEUT|NPOR|NPRL|APLC|HNPO|SNP)$"),
    ("DEN",       "bulk density",         r"^(RHO[BZ]?|DEN|ZDEN|RHOZ|DENB|HRHO)$"),
    ("DEN_COR",   "density correction",   r"^(DRHO|ZCOR|DCOR|HDRA)$"),
    ("PHI",       "porosity, computed",   r"^(D?PHI|DPOR|POR[DZNS]?|POR|PHI[TEDNZ]?|SPHI|PIGN)$"),
    # Photoelectric factor: a lithology curve, drawn in the density track
    # because that is the tool it comes off and where a reader looks for it.
    ("PE",        "photoelectric factor", r"^(PEF?[ZB]?|PEFZ|PEDN|U)$"),
    # ── saturation ───────────────────────────────────────────────────────
    ("SW",        "water saturation",     r"^(SW[ETA]?|SWE|SWT|SUWI|BVW)$"),
    # ── sonic ────────────────────────────────────────────────────────────
    ("SON",       "sonic",                r"^(DT[CLPSM]?|AC|SONIC|DTCO|DTSM|TT|ITT)$"),
    # ── housekeeping: real curves, but not part of the display ───────────
    ("TENS",      "cable tension",        r"^(TEN[SDR]?|TENSION)$"),
    ("MISC_ENG",  "drilling / mud",       r"^(ROP|TGAS|MUD|MW|WOB|RPM|SPM|PP)$"),
    ("CORR",      "correlation / repeat", r"^(CORR|REPEAT|CBL|VDL)$"),
    ("GEOM",      "hole geometry",        r"^(GDEV|DEVI|HAZI|AZIM|LAT|LONG?|ICV|FORX)$"),
]

_COMPILED = [(f, lbl, re.compile(p, re.I)) for f, lbl, p in FAMILIES]

DEPTH_MNEMONICS = {"DEPT", "DEPTH", "MD", "TVD", "TVDSS"}


def classify(mnemonic):
    """(family, label) for a mnemonic, or (None, None) when nothing matches."""
    m = (mnemonic or "").strip().upper()
    if not m:
        return None, None
    if m in DEPTH_MNEMONICS:
        return "DEPTH", "depth"
    for fam, label, pat in _COMPILED:
        if pat.match(m):
            return fam, label
    return None, None


# ── the display ───────────────────────────────────────────────────────────
# Each track: (number, title, families it takes, scale, log?)
# The scales are the conventional ones. Density is REVERSED against neutron on
# purpose -- see the module docstring.
TRACK_TEMPLATE = [
    (1, "GR / SP",        ["GR", "SP"],                       None,        False),
    (2, "Caliper",        ["CAL", "BS"],                      (6.0, 16.0), False),
    (3, "Resistivity",    ["RES_SHAL", "RES_MED", "RES_DEEP",
                           "RES_OTHER"],                      (0.2, 2000), True),
    (4, "Neutron / Density", ["NEU", "DEN", "DEN_COR", "PHI", "PE"], None,   False),
    (5, "Sw",             ["SW"],                             (0.0, 1.0),  False),
]

# Per-family scale and colour, applied inside a track.
FAMILY_STYLE = {
    "GR":        {"scale": (0, 150),     "unit": "API",   "colour": "#1E8449"},
    "SP":        {"scale": (-80, 20),    "unit": "mV",    "colour": "#111111"},
    "CAL":       {"scale": (6, 16),      "unit": "in",    "colour": "#7F5A2E"},
    "RES_DEEP":  {"scale": (0.2, 2000),  "unit": "ohmm",  "colour": "#C0392B"},
    "RES_MED":   {"scale": (0.2, 2000),  "unit": "ohmm",  "colour": "#1F6FB2"},
    "RES_SHAL":  {"scale": (0.2, 2000),  "unit": "ohmm",  "colour": "#16A085"},
    "RES_OTHER": {"scale": (0.2, 2000),  "unit": "ohmm",  "colour": "#8E44AD"},
    # NEUTRON RUNS RIGHT-TO-LEFT AND DENSITY LEFT-TO-RIGHT so the two cross.
    "NEU":       {"scale": (0.45, -0.15), "unit": "v/v",  "colour": "#1F6FB2"},
    "DEN":       {"scale": (1.95, 2.95), "unit": "g/cc",  "colour": "#C0392B"},
    "DEN_COR":   {"scale": (-0.25, 0.25), "unit": "g/cc", "colour": "#7F8C8D"},
    "PHI":       {"scale": (0.45, -0.15), "unit": "v/v",  "colour": "#8E44AD"},
    "SW":        {"scale": (1.0, 0.0),   "unit": "v/v",   "colour": "#2E86C1"},
    "BS":        {"scale": (6, 16),      "unit": "in",    "colour": "#7F8C8D",
                  "dash": True},
    "PE":        {"scale": (0, 10),      "unit": "b/e",   "colour": "#8E44AD"},
    "GEOM":      {"scale": None,         "unit": "",      "colour": "#95A5A6"},
    "SON":       {"scale": (140, 40),    "unit": "us/ft", "colour": "#B9770E"},
    "TENS":      {"scale": None,         "unit": "lb",    "colour": "#95A5A6"},
    "MISC_ENG":  {"scale": None,         "unit": "",      "colour": "#566573"},
    "CORR":      {"scale": None,         "unit": "",      "colour": "#95A5A6"},
}


def propose_tracks(mnemonics, include_unclassified=True):
    """The prefilled track layout for the curves a log actually has.

    A TRACK WITH NOTHING IN IT IS NOT DRAWN. A vintage induction log has no
    neutron and no Sw; showing four empty boxes beside two full ones tells the
    reader the log is missing something when it is simply an older tool
    string. Only tracks with curves come back, renumbered.
    """
    by_family = {}
    unknown = []
    for m in mnemonics:
        fam, _label = classify(m)
        if fam == "DEPTH":
            continue
        if fam is None:
            unknown.append(m)
        else:
            by_family.setdefault(fam, []).append(m)

    out, n = [], 0
    for _num, title, fams, scale, is_log in TRACK_TEMPLATE:
        curves = []
        for f in fams:
            curves.extend(by_family.get(f, []))
        if not curves:
            continue
        n += 1
        out.append({"track": n, "title": title, "curves": curves,
                    "scale": scale, "log": is_log})

    # everything real but not part of the standard display
    extra = []
    for fam in ("SON", "TENS", "MISC_ENG", "CORR", "GEOM"):
        extra.extend(by_family.get(fam, []))
    if extra:
        n += 1
        out.append({"track": n, "title": "Other", "curves": extra,
                    "scale": None, "log": False})
    if unknown and include_unclassified:
        n += 1
        out.append({"track": n, "title": "Unclassified", "curves": unknown,
                    "scale": None, "log": False})
    return out


def unclassified(mnemonics):
    """The mnemonics no pattern claims. The miss rate has to be visible, or a
    family list quietly decays as new tools appear in the catalogue."""
    return sorted({m for m in mnemonics
                   if classify(m)[0] is None and m.strip()})
