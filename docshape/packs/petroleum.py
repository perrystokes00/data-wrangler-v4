"""
docshape.packs.petroleum
========================
The PETROLEUM vocabulary: what a well-document table can be, and what its
columns are called across the vendors that produce them.

Nothing in here is machinery. The engine does the tokenising, matching and
scoring; this file only says which words mean which attribute, which
attributes make up which kind of table, and where each lands in a database.
A different industry is a different file with the same structure —
docshape.packs.legal would name parties, effective dates and clause types, and
the engine would not know the difference.

WHAT A PACK PROVIDES
--------------------
    fields      canonical attribute -> alias phrases
    shapes      table kind -> {required, optional, min_required, target}
    numeric     which fields are coerced to numbers
    columns     shape -> {field: [candidate database columns]}
    transforms  shape -> callable, for tables that must change shape
    noise       domain units that carry no meaning for identification
    char_map    characters to translate before tokenising
    identity    the field that identifies the subject, plus its normaliser

MAINTENANCE IS A WORD, NOT A PARSER. When a header isn't recognised, add an
alias here. Every fix in this file applies to every document from every vendor
at once; a parser per layout does not.
"""
from __future__ import annotations

import os
import re

# Petroleum units and report furniture. Stripped before matching so that
# "Proppant (lbs)" and "Proppant" are one thing. BASE_NOISE in the engine
# already covers the domain-neutral filler.
noise = {
    "ft", "m", "in", "inch", "inches", "psi", "bbl", "bbls", "mcf", "mmcf",
    "lb", "lbs", "bpm", "scf", "gal", "ppg", "ppf", "sacks", "sks", "d",
    "deg", "degrees", "api14",
    # NOT "day"/"days": they are units ("Days On") AND meaningful terms
    # ("Day", "Day No"). Listing them as noise eroded the alias "day no" to a
    # bare {no}, which then matched any header containing "No" — "Bit No"
    # resolved to a day field. Same failure as a unit shadowing an identifier.
}

# Ø / ø is the porosity symbol ("Øe" = effective porosity). The punctuation
# strip would delete it and leave a bare "e", which matches nothing.
char_map = {"\u00f8": "phi", "\u00d8": "phi",
            # A LABEL says "API:"; a UNIT says "(API)". The colon is the
            # whole distinction, and the punctuation strip was erasing it —
            # which is why uwi could never safely own a bare "api" alias
            # (it would claim every gamma curve's unit). Marking the colon
            # as a token lets "api lbl" mean the identifier while "(API)"
            # stays a unit. Domain judgement, so it lives here, not in the
            # engine.
            ":": " lbl "}

fields = {
    # identity
    # NOT bare "api": it appears as a UNIT ("GR (API)") far more often than as
    # an identifier, and a 1-token alias would win that tie and mislabel the
    # gamma curve as a well identifier.
        # NOT a bare "api" alias: API is ALSO the unit of a gamma-ray curve,
    # so "GR (API)" would resolve to the well number. A column headed
    # only "API" therefore stays unmapped — the honest trade against
    # corrupting every gamma log in the corpus.
"uwi":          ["uwi", "api number", "api no", "well id", "api 14",
                     "api14", "uwi14", "well identifier",
                     # "API / UWI": the short-slash-pair rule fuses two
                     # <=3-char sides into ONE token (the same rule that
                     # keeps N/S and 48/64 whole), so the cell arrives as
                     # {apiuwi} and neither "api number" nor "uwi" can
                     # subset-match it. The fused spelling is the alias.
                     "apiuwi", "uwiapi",
                     # "API:" — api followed by the colon marker (see
                     # char_map). Two tokens, so "GR (API)" with its bare
                     # {gr, api} never matches it.
                     "api lbl"],
    # RFT / MDT pressure testing
    "pretest_pressure": ["pre test", "pretest", "pre test psi"],
    "gradient":     ["gradient", "pressure gradient"],
    "mobility":     ["mobility"],
    "well_name":    ["well name", "well", "lease well"],
    # depths
    "top_md":       ["top md", "top depth", "depth top", "top", "from"],
    "base_md":      # "PERF_BOT_FT" pairs with a PERF_TOP_FT that already reaches top_md,
    # so its partner belongs on base_md. Two tokens, so it beats the bare
    # "bot"/"base" match cleanly. The paired-depth pattern again: whenever
    # a document states a top it usually states the bottom beside it.
    ["base md", "perf bot", "perf bottom", "bot md", "bottom depth", "bottom md", "base depth", "base", "to"],
    "tvd":          ["tvd", "shoe depth tvd", "shoe tvd", "set depth tvd", "top tvd", "tvd top", "tvd ft", "true vertical depth", "true vert dep",
                     "vert dep", "true vert", "tv depth"],
    "md":           ["md", "measured depth", "depth"],
    # geology
    "formation":    ["formation", "strat unit", "stratigraphic unit",
                     "zone", "zone name", "marker", "horizon"],
    # NOTE: bare "unit" was a formation alias and it was UNWINNABLE from the
    # UI. It also aliases `units`; both are one token, so the tie fell to
    # dict order and every curve table's "Unit" column (GAPI, G/CC, OHMM)
    # resolved to a FORMATION. Pointing the teach form's dropdown at units
    # could not fix it — extending units with a wording it already has
    # changes nothing. "Strat Unit" still resolves to formation above.
    "lithology":    ["lithology", "lith", "rock type"],
    # FREE TEXT. Half the tables in a scout ticket carry one — "Note",
    # "Remarks", "Comments" — and every cat_ table already has a `remark`
    # column waiting for it. NOT "description": that is already an alias on
    # `event`, where it names what happened during an operation.
    "remark":       ["remark", "remarks", "note", "notes", "comment",
                     "comments"],
    # A formation top is somebody's PICK. Who made it and when are part of
    # the claim — two geologists disagreeing is a finding, not an error.
    "interpreter":  ["picked by", "interpreter", "interpreted by",
                     "geologist", "analyst", "picked", "author"],
    # Perforation intervals as a vendor writes them. perf_interval is what
    # makes a perforation table DIFFERENT from a formation-tops table: both
    # carry a formation and a top depth, so without a discriminating field
    # the greedy shape wins every time (formation_tops requires only
    # formation + top_md, which almost any depth-indexed table satisfies).
    "perf_interval": ["perforations", "perfs", "perforated", "perf interval", "perforated interval",
                      "perf length", "interval perforated"],
    # casing
    "casing_string": ["casing string", "string", "casing"],
    "od":           ["od", "size", "outside diameter", "casing size"],
    "weight":       ["weight", "wt"],
    # CASING HARDWARE. Both were on the known-unclaimed list for casing:
    # a scout ticket states how many centralizers ran and what float
    # equipment was on the string, and neither had anywhere to go.
    # Centralizers is usually a COUNT, float equipment a description.
    "centralizers": ["centralizers", "centralizer count", "cent",
                     "no of centralizers"],
    "float_equipment": ["float equipment", "float equip", "float",
                        "float collar", "float shoe"],
    "grade":        ["grade"],
    "set_depth":    ["set depth", "shoe md", "shoe depth", "shoe", "landed"],
    "cement_sacks": ["cement sacks", "cement", "sacks", "slurry volume"],
    # "Top of Cement" was resolving to top_md — the bare alias "top" matched
    # and won, putting a cement top into the casing string's own top depth.
    # A two-token alias beats a one-token one, so naming the attribute fixes
    # it. TOC is included because the industry uses it constantly; it would be
    # a bad alias in a pack where TOC means table of contents.
    "cement_top":   ["cement top", "top of cement", "toc", "toc md"],
    "cement_base":  ["cement base", "base of cement", "boc", "cement bottom"],
    # frac / completion
    "stage":        ["stage", "stg", "stage id"],
    "clusters":     ["clusters", "cluster count", "perf clusters",
                     "cluster spacing"],
    # FLUID TYPE FIRST, and it owns the bare word. A tops or DST table's
    # "Fluid" column holds OIL / GAS / WATER — a type. A frac table's fluid
    # column is a VOLUME and says so ("Fluid (bbl)", "Fluid Volume",
    # "Slurry"). Bare "fluid" belonged to fluid_vol, which put the word
    # WATER on course for a volume column and nothing downstream would have
    # caught it. These tie at one token, so ORDER decides: type before vol.
    # BARE "fluid" STAYS WITH THE VOLUME, deliberately. It is genuinely
    # ambiguous — a tops or DST table means the fluid TYPE (OIL/GAS/WATER),
    # a frac or completion table means a VOLUME — and the unit that would
    # settle it is stripped: "bbl" is noise, so "FLUID_BBL", "Fluid (bbl)"
    # and a bare "Fluid" all reduce to the same single token. One field
    # must therefore own the bare word globally, and it is fluid_vol
    # because the completion and frac paths already depend on that.
    # fluid_type takes only wordings that are UNAMBIGUOUS on their own.
    # Where a tops table's Fluid column needs to mean the type, that is a
    # SHAPE's job via its column map — the same resolution used for
    # "Status" meaning well_status in a header and perf_status in a
    # perforation table.
    "fluid_type":   ["fluid type", "fluid phase", "phase", "produced fluid",
                     "hydrocarbon type"],
    "fluid_vol":    ["fluid", "fluid volume", "fluid vol", "slurry",
                     "water pumped", "total fluid"],
    # INSTANTANEOUS SHUT-IN PRESSURE — the pressure the moment pumping
    # stops, which is what closure stress is read from. It was an alias on
    # `pressure`, so a stage table stating BOTH "ISIP" and "Max Press"
    # gave one of them to pressure and dropped the other: the same column
    # cannot hold two measurements. Now its own field.
    "isip":         ["isip", "instantaneous shut in pressure",
                     "shut in pressure", "sip"],
    "max_pressure": ["max press", "max pressure", "maximum pressure",
                     "peak pressure", "max treating pressure"],
    # THE RIG that drilled it — a scout ticket names it and nothing claimed
    # the word.
    "rig":          ["rig", "rig name", "rig no", "rig number", "rig id"],
    # PROPPANT INTENSITY is pounds PER FOOT, a different measurement from
    # total proppant, and a completion sheet states both side by side —
    # "Prop Intensity (lbs/ft)" was losing to "Proppant (lbs)".
    # NOTE the wordings here must survive noise removal: "proppant per
    # ft" reduces to {proppant} and would steal the plain proppant
    # column, and "lbs per ft"/"ppf" reduce to {} and match nothing.
    "prop_intensity": ["prop intensity", "proppant intensity",
                       "intensity lbs ft"],
    # THE FLUID SYSTEM is a recipe name (slickwater, crosslink), not a
    # volume. Two tokens, so it beats bare "fluid" on fluid_vol.
    "fluid_system": ["fluid system", "frac fluid", "fluid recipe",
                     "stim fluid", "treatment fluid"],
    # SURFACE EQUIPMENT a well test states: what it flowed through.
    "tubing":       ["tubing", "tubing size", "tubing od", "tbg"],
    "separator":    ["separator", "separator type", "sep type"],
    # THE DATUM a depth is measured FROM. "Depth Datum" was resolving to
    # md and then being blocked by the well's own total depth.
    "depth_datum":  ["depth datum", "datum", "measured from", "depth ref",
                     "elevation datum"],
    "proppant":     ["proppant", "prop", "sand", "prop mass", "proppant mass"],
    # FLOWING PRESSURES on a well test — wellhead and bottomhole. Distinct
    # from the generic `pressure`, because a test states both and one
    # column must not swallow the other.
    "fwhp":         ["fwhp", "flowing wellhead pressure", "wellhead pressure",
                     "whp", "flowing whp"],
    "fbhp":         ["fbhp", "flowing bottomhole pressure", "bhp",
                     "bottomhole pressure", "flowing bhp"],
    # VERTICAL SECTION — the horizontal distance along the survey's
    # reference azimuth. A Landmark survey states it beside N/S and E/W.
    "vsec":         ["vsec", "v sec", "vertical section", "vert sec",
                     "v section", "vertical sect"],
    "pressure":     ["press", "pressure", "treating pressure", "whp"],
    "rate":         ["rate", "pump rate", "injection rate"],
    # production / test
    "date":         ["date", "test date", "report date", "prod date", "period"],
    "oil":          ["oil", "oil rate", "oil production", "bopd"],
    "gas":          ["gas", "gas rate", "gas production", "mcfd"],
    "water":        ["water", "water rate", "water production", "bwpd"],
    # BARRELS OF OIL EQUIVALENT — a combined measure, not a third fluid.
    # "Barrels of Oil Equivalent" spelled out was resolving to `oil`
    # (the alias "oil" is a subset of those tokens), which would have put
    # a combined figure in the oil column. The abbreviations carry the
    # meaning, so they are listed first and the spelled-out form is
    # included explicitly to beat that subset match.
    "boe":          ["boe", "boepd", "boed", "boe d", "mboe", "boe per day",
                     "barrels of oil equivalent", "oil equivalent"],
    "gor":          ["gor", "gas oil ratio"],
    "choke":        ["choke", "choke size"],
    "days_on":      ["days on", "days online", "on production"],
    # directional
    "inclination":  ["inc", "incl", "inclination", "angle", "drift"],
    "azimuth":      ["azi", "azim", "azimuth", "bearing", "hazi"],
    "dls":          ["dls", "dogleg", "dogleg severity", "dog leg",
                     "dog leg sev", "dogleg sev"],
    "ns":           ["ns", "north south", "northing"],
    "ew":           ["ew", "east west", "easting"],
    "closure":      ["closure", "closure distance"],
    "net_gross":    ["ng", "net gross", "net to gross"],
    # HYDROCARBON PORE VOLUME — porosity x (1 - Sw) x thickness, the number
    # a petrophysical summary exists to produce. On the July
    # recognised-but-unclaimed list since the first scout tickets.
    "hc_pore_vol":  ["hc pore vol", "hydrocarbon pore volume", "hcpv",
                     "hc pore volume", "pore volume", "hc pv"],
    "shale_vol":    ["vsh", "vshale", "v shale", "avg vcl", "vcl", "shale volume", "clay volume"],
    "gross":        ["gross", "gross interval"],
    # petrophysics
    "porosity":     ["porosity", "phi", "phie", "phi e", "por", "phit"],
    "permeability": # THE UNIT COLLIDES WITH ANOTHER FIELD. Permeability is measured in
    # MILLIDARCIES, and "mD" tokenises to exactly "md" — measured depth.
    # So "Perm (mD)" and "K (mD)" both resolved to `md`, silently filing a
    # permeability under a depth. Bare "k" has the mirror problem: it
    # matched "Cost ($K)" because "$K" reduces to the same token. Two-token
    # aliases settle both directions; bare "perm"/"k" stay for headers that
    # really are just that.
    ["permeability", "perm md", "k md", "permeability md",
                     "perm mdarcy", "perm", "k"],
    "saturation":   ["sw", "saturation", "water saturation"],
    "net_pay":      ["net pay", "pay", "net"],
    # key/value property tables
    "parameter":    ["parameter", "property", "attribute", "item", "measure"],
    # A KEY-VALUE TABLE'S VALUE COLUMN IS OFTEN TITLED WITH THE
    # MEASUREMENT, not the word "Value": "Parameter | 30-Day IP | Units"
    # with rows Oil Rate / Gas Rate / Water Rate. IP (initial potential)
    # is the common case and is a real industry term, so it belongs here
    # rather than as a per-document attribute — "30-Day IP", "24-Hour IP"
    # and "IP (BOPD)" all carry the bare token and resolve by subset.
    # Without this, "30-Day IP" resolved to `day`, which is the
    # noise-adjacent trap wearing a different hat.
    "param_value":  ["value", "result", "reading", "cutoff value",
                     "ip", "initial potential", "ip test"],
    "units":        ["units", "unit", "uom"],
    "method":       ["method", "basis", "technique", "source"],
    # cement bond
    "contact_type": ["contact type", "contact"],
    "owc":          ["owc", "oil water contact"],
    "goc":          ["goc", "gas oil contact"],
    "gwc":          ["gwc", "gas water contact"],
    # perforations
    "shot_count":   ["shots", "shot count", "holes", "no of shots"],
    "shot_density": ["spf", "shots per foot", "shot density", "spm"],
    "gun_type":     ["gun", "gun type", "charge", "charge type"],
    "phasing":      ["phasing", "phase", "gun phasing"],
    "perf_status":  ["perf status", "interval status"],
    "interval":     ["interval", "interval md", "interval ft md", "depth interval md", "depth interval", "range"],
    "amplitude":    ["amplitude", "cbl amplitude", "cbl"],
    "bond":         ["bond", "cement bond", "bond quality"],
    # operations / NPT
    "event":        ["event", "activity", "operation", "description"],
    "duration":     ["hrs", "hours", "duration", "elapsed", "hours"],
    # An end-of-well report states PLANNED against ACTUAL, so two cost
    # and two day columns sit side by side and one would block the
    # other. Each gets its own field.
    "afe_cost":     ["afe cost", "authorized cost", "authorised cost",
                     "budget cost", "afe amount"],
    "afe_days":     ["afe days", "planned days", "budget days",
                     "afe day"],
    "cost":         ["cost", "cost k", "cost usd", "cost mm", "total cost",
                     "cost m", "afe cost", "spend"],
    "category":     ["category", "class", "npt category", "code"],
    "day":          ["day", "day no", "report day"],
    # fluid sample / PVT
    "sample_type":  ["sample type", "sample"],
    # A DST/well test states what KIND of test it was and how it turned
    # out. "Type" and "Result" are both generic English words, so they are
    # resolved where the context is — the dst shape claims them, exactly
    # as `perforations` claims well_status and means perf_status by it.
    "test_type":    ["test type", "type", "test kind", "dst type"],
    "test_result":  ["test result", "result", "outcome", "recovery type"],
    "thickness":    ["thickness", "gross thickness", "interval thickness",
                     "bed thickness"],
    "recovery":     ["recovery", "rec"],
    "length":       ["length", "cored length"],
    "show":         ["show", "hydrocarbon show", "oil show"],
    "photos":       ["photos", "photo count"],
    "orientation":  ["orientation", "well orientation"],
    "lateral":      ["lateral", "lateral length"],
    "stage_count":  ["stages", "stage count", "total stages",
                     "frac stages", "stage no"],
    "well_type":    ["well type", "type of well"],
    "well_status":  ["status", "well status"],
    "operator":     ["operator", "operator name"],
    "field_name":   ["field", "field name"],
    "county":       ["county", "parish"],
    "state":        ["state", "province", "province state"],
    "spud_date":    ["spud date", "spudded"],
    "completion_date": ["completion date", "completed",
                        "comp date", "compl date"],
    "total_depth":  ["total depth", "td"],
    # "total depth md" was here and it was a trap: "total" is noise, so the
    # alias eroded to a bare {depth, md} and hijacked every plain
    # "Depth (ft MD)" column (curve listings, RFT stations) away from md —
    # which then invited a taught "depth (ft md)"->md alias that collided
    # with casing's "Set Depth (ft MD)". A noise word inside an alias
    # makes the alias mean something shorter than it reads. Plain
    # "Depth (ft MD)" now resolves to md directly, and well_header's
    # md->final_td column map already handles the header-block case.
    # Coordinates. Absent from the pack until now because the documents
    # that drove it state a location in words, not degrees — but a
    # spreadsheet well header states them plainly and they were reaching
    # nothing.
    "latitude":     ["latitude", "lat", "surface latitude", "surface lat",
                     "y coord"],
    "longitude":    ["longitude", "long", "lon", "surface longitude",
                     "surface long", "x coord"],
    "surface_loc":  ["surface location", "surface loc"],
    "kb_elev":      ["kb elevation", "kb elev", "kelly bushing"],
    "collection":   ["collection point", "sample point", "collected"],
    "api_gravity":  ["api gravity", "gravity", "api grav"],
    "temperature":  ["temp", "temperature"],
    # SOUR/INERT GAS CONTENT and the lab's own verdict on the sample. H2S
    # is a safety number before it is a chemistry one, so losing it is
    # worse than losing most columns here.
    "h2s":            ["h2s", "hydrogen sulphide", "hydrogen sulfide",
                       "sour gas"],
    "co2":            ["co2", "carbon dioxide"],
    # "Sample Quality" was BLOCKED, not unrecognised: it resolves to
    # sample_type via "sample", which the table's own "Sample Type"
    # column had already filled.
    "sample_quality": ["sample quality", "quality", "sample condition",
                       "condition"],
    "bsw":          ["bsw", "bs w", "basic sediment water"],
    # raw curve readings
    "gamma":        ["gr", "gamma", "gamma ray"],
    "density":      ["rhob", "bulk density", "density"],
    "neutron":      ["nphi", "neutron", "neutron porosity"],
    "resistivity":  ["rt", "resistivity", "ild", "rild"],
    # operations time logs and end-of-well summaries
    "time_slot":    ["time slot", "time hrs", "time period", "time"],
    "rig_release":  ["rig release", "rig released", "release date"],
    # wireline curve summaries and logging-run headers
    "curve_mnem":   ["curve", "curve name", "mnemonic", "mnem",
                     "curve mnemonic", "log curve"],
    "log_type":     ["log type", "log suite", "survey type", "tool type",
                     "logging type"],
    # statistics columns ("Parameter | Min | Max | Average | Units") —
    # possible at all only because min/max/average left the engine noise.
    # LAST in this dict ON PURPOSE: in "Avg Oil (bbl/d)" the stat word is a
    # MODIFIER, and its bare alias ties the real term at one token — ties
    # go to whichever field the dict lists first, so the term must come
    # first and the stat words last. Moving these up broke the well-test
    # production table the day they were added.
    # The two-token forms are not redundant: "Min Value" is {min, value},
    # which the bare "min" matches at one token — exactly tying
    # param_value's bare "value" and losing on dict order. The longer alias
    # wins outright, so the column maps where it belongs.
    "stat_min":     ["min", "minimum", "min value", "minimum value"],
    "stat_max":     ["max", "maximum", "max value", "maximum value"],
    "stat_avg":     ["average", "avg", "mean", "avg value", "average value"],
}

shapes = {
    "casing": {
        "required": ["casing_string", "set_depth"],
        "optional": ["od", "weight", "grade", "cement_sacks", "tvd",
                     "cement_top", "cement_base", "centralizers",
                     "float_equipment"],
        # dv_well_casing EXISTS; the cat_well_casing staging MIRROR does not.
        # Set the target anyway — shape_loader reflects the table and skips
        # cleanly ("not found in file_catalog") until the mirror is created, so
        # this needs no second edit once it is.
        "min_required": 2, "target": "cat_well_casing",
    },
    "frac_stage": {
        "required": ["stage", "top_md"],
        # length + formation come from the COMPLETION reports' stage tables;
        # they were recognised as fields but not claimed by this shape, so
        # propose reported them as unclaimed on every one of those documents.
        "optional": ["base_md", "clusters", "fluid_vol", "proppant", "pressure", "rate", "length", "formation", "interval", "remark", "isip", "max_pressure", "prop_intensity", "fluid_system"],
        "min_required": 2, "target": "cat_well_stimulation",
    },
    "formation_tops": {
        "required": ["formation", "top_md"],
        "optional": ["base_md", "lithology", "tvd", "uwi", "net_pay", "well_name", "fluid_vol", "date", "interpreter", "remark", "thickness"],
        "min_required": 2, "target": "cat_well_formation_top",
    },
    "production": {
        "required": ["date", "oil"],
        "optional": ["gas", "water", "gor", "choke", "days_on", "pressure", "boe", "uwi", "well_name", "operator", "well_status", "test_type", "duration", "fwhp", "fbhp", "remark", "rate"],
        "min_required": 2, "target": "cat_prod_volume",
    },
    "directional_survey": {
        "required": ["md", "inclination", "azimuth"],
        "optional": ["tvd", "ns", "ew", "dls", "closure", "vsec"],
        "min_required": 2, "target": "cat_well_dir_srvy_sta",
    },
    "petrophysics": {
        "required": ["top_md", "porosity"],
        "optional": ["base_md", "permeability", "saturation", "net_pay", "formation", "net_gross", "shale_vol", "gross", "hc_pore_vol", "remark"],
        # cat_well_petro_interp is the INTERPRETATION HEADER (Archie params,
        # methods, input log ids). Zone rows with top/base/phi/sw go in
        # cat_well_petro_zone.
        "min_required": 2, "target": "cat_well_petro_zone",
    },
    "dst": {
        "required": ["date", "top_md"],
        "optional": ["base_md", "oil", "gas", "pressure", "test_type", "test_result", "api_gravity", "remark", "duration", "param_value"],
        "min_required": 2, "target": "cat_well_dst",
    },

    # ── Recognised, but with NO cat_ table behind them ──────────────────────
    # target=None is a deliberate state, not an omission. These are real data
    # the documents carry and the schema has nowhere to put; naming them means
    # the report says "cement bond log, 2 rows, no target" instead of UNKNOWN,
    # which turns a mystery into a schema decision. Give one a target and it
    # starts loading with no other change.
    "perforations": {
        # Overlaps formation_tops, which requires only [formation, top_md] —
        # a perf table has both. It wins on the (score, optional-hits)
        # tie-break because it also explains shots, density, gun, phasing and
        # status, which formation_tops cannot. ANY shape overlapping a general
        # one needs enough OPTIONAL fields to out-explain it.
        # TWO WAYS TO BE A PERFORATION TABLE, because vendors write them
        # both ways: with a shot count, or with a perforated interval.
        # Listing both as required with min_required 1 means either alone
        # identifies the table. Requiring shot_count outright cost this
        # shape a real vendor sheet (UWI · WELL_NAME · PERF_TOP_FT ·
        # PERF_BOT_FT · PERF_INTERVAL_FT · FORMATION · PERF_DATE) that
        # states no shot count at all — formation_tops claimed it.
        # top_md moves to OPTIONAL: it is what formation_tops requires, so
        # it can never be what distinguishes a perforation table.
        # ONE required field, not two. min_required lets a shape match on a
        # SUBSET, but the SCORE is still matched/len(required) — so two
        # required fields with one present scores 0.50 and loses outright to
        # formation_tops at 1.00. A shape that must beat a general one has
        # to score 1.00, which means requiring only what is genuinely always
        # there. perf_interval covers both vendor styles: the interval sheet
        # states it outright, and a shot-count sheet heads its depth column
        # "Perf Interval Top", which contains it.
        "required": ["perf_interval"],
        # "Status" resolves to well_status because that alias already exists
        # and the engine matches a cell without knowing which table it is in.
        # A generic word means different things in different tables, so the
        # SHAPE claims it and the column map below says what it means here.
        "optional": ["shot_count", "top_md", "base_md", "shot_density",
                     "gun_type", "phasing", "formation", "date",
                     "perf_status", "well_status", "uwi", "well_name"],
        "min_required": 1, "target": "cat_well_perforation",
    },
    "core_run": {
        # Beats formation_tops on the same table via the (score, optional-hits)
        # tie-break: both score 1.00 on two required fields, but a core run
        # table also carries recovery, length, show, date and photo count.
        "required": ["recovery", "top_md"],
        "optional": ["formation", "base_md", "length", "show", "date",
                     "photos", "uwi"],
        "min_required": 2, "target": "cat_well_core",
    },
    "completion": {
        # cat_well_completion has been sitting unused — completion_type,
        # well_orientation, lateral_length_ft, stage_count, total_fluid_bbl,
        # total_proppant_lbs all have homes already.
        # ORIENTATION IS NOT REQUIRED, and requiring it cost this shape its
        # own tables: a vendor completions sheet states a completion date,
        # stage count, proppant and fluid but never says horizontal or
        # vertical, so completion could not match and well_header claimed
        # the table on UWI + WELL_NAME alone. completion_date discriminates
        # by itself — "comp date"/"completion date" appears on completion
        # tables and nowhere else — and the long optional list wins the
        # tie against any general shape scoring the same.
        "required": ["completion_date"],
        "optional": ["orientation", "stage_count", "proppant", "fluid_vol", "lateral", "clusters", "formation", "top_md", "base_md", "uwi", "well_name", "operator", "test_type", "prop_intensity", "fluid_system"],
        "min_required": 1, "target": "cat_well_completion",
    },
    "well_header": {
        # The ticket's own header block: one row per WELL, not per detail.
        "required": ["uwi", "well_name"],
        # md is here for the pair-grid case: "Total Depth" erodes to a bare
        # {depth} because "total" is engine base noise, and {depth} ties to
        # md by dictionary order. The SHAPE resolves the generic word (the
        # Status -> perf_status precedent): in a well header, a bare depth
        # IS the TD — the columns map below sends md to final_td.
        "optional": ["operator", "field_name", "county", "state", "well_type", "well_status", "orientation", "spud_date", "completion_date", "total_depth", "surface_loc", "kb_elev", "md", "latitude", "longitude", "rig_release", "tvd", "lateral", "azimuth", "duration", "cost", "afe_cost", "afe_days", "remark", "rig", "depth_datum"],
        "min_required": 2, "target": "cat_well",
    },
    "fluid_contacts": {
        # Was UNKNOWN until the DDL showed cat_well_formation_top carries
        # owc_depth / goc_depth / gwc_depth. The tops study's second table has
        # a home after all.
        "required": ["formation", "contact_type"],
        "optional": ["uwi", "md", "top_md", "method"],
        "min_required": 2, "target": "cat_well_formation_top",
    },
    "core_sample": {
        "required": ["sample_type", "porosity"],
        "optional": ["md", "top_md", "base_md", "permeability", "saturation",
                     "lithology"],
        "min_required": 2, "target": "cat_well_core_sample",
    },
    "doc_header_block": {
        # The document's own header, written as Field/Value pairs rather than
        # as columns: "Well Name | SMITH 36-4", "UWI | 42-001-20576-00-00".
        # Common at the top of completion and test reports.
        #
        # WHY A SEPARATE SHAPE and not an alias: the label column is headed
        # "Field", and `field_name` already claims that word — legitimately,
        # because well_header has a Field column meaning the OIL FIELD. A bare
        # "field" alias on `parameter` would tie with it at one token and let
        # dictionary order decide which won. So the SHAPE disambiguates: in a
        # two-column Field/Value grid, "Field" is a label; in a well header it
        # is a place.
        "required": ["field_name", "param_value"],
        "optional": ["units", "method", "parameter"],
        "min_required": 2, "target": "cat_well",
    },
    "rft_pressure_test": {
        # Formation pressure stations from an RFT/MDT run. Required fields
        # chosen for DISCRIMINATION: no other shape requires pretest or
        # mobility. gradient is optional rather than required because the
        # one real example's header typesets the three pressure names as a
        # gapless run the reader must cut positionally — the gradient
        # column's recovered wording is not guaranteed readable.
        "required": ["pretest_pressure", "mobility"],
        "optional": ["md", "tvd", "formation", "pressure", "gradient", "fluid_vol", "fluid_type", "remark"],
        "min_required": 2, "target": None,
    },
    "daily_time_log": {
        # A drilling report's 24-hour operations log: one row per time
        # slot. time_slot is required and unique to this shape; duration
        # and the depth pair are optional because vendors drop them.
        "required": ["time_slot", "event"],
        "optional": ["duration", "top_md", "base_md", "method", "remark"],
        "min_required": 2, "target": None,
    },
    # A WELL TEST REPORT'S HEADER BLOCK. Needed as its own shape because
    # half these documents carry no UWI at all (the *_REPORT#### variants
    # are generated without one), so well_header — which requires uwi and
    # well_name — can never match them, and the block was falling out
    # entirely along with the operator and well name that are the only
    # things left to identify the well by.
    #
    # Required on test_type + date, which no other shape requires
    # together: a discriminating pair, not merely a present one. Where a
    # test header DOES carry a UWI this still wins over well_header on
    # coverage, which is right — it explains the whole block.
    "well_test_header": {
        "required": ["test_type", "date"],
        "optional": ["operator", "well_name", "uwi", "field_name", "state",
                     "county", "formation", "perf_interval", "tubing",
                     "separator", "md", "tvd", "remark", "choke",
                     "api_gravity", "gor"],
        "min_required": 2,
        "target": None,
    },
    "eow_summary_pairs": {
        # End-of-well summary written as label/value PAIRS ("Spud Date: |
        # 2024-01-08 | Rig Release: | 2024-05-22"). rig_release is unique
        # to this shape, so the pair is discriminating. Like every pair
        # grid, recognition is solved here; one-clean-row extraction still
        # waits on the pivot being generalised beyond well_header.
        "required": ["spud_date", "rig_release"],
        "optional": ["md", "total_depth", "tvd", "uwi", "well_name", "operator", "state", "county", "field_name", "lateral", "azimuth", "duration", "cost", "afe_cost", "afe_days", "remark"],
        "min_required": 2, "target": None,
    },
    "curve_summary": {
        # "Curve | Unit | Min Value | Max Value" — one row per logged curve.
        # curve_mnem is required because it is what makes this table
        # DIFFERENT; the stat columns are optional on purpose. Requiring
        # them would make this a superset of parameter_stats' required set
        # and the two would compete on every stats-shaped table. As
        # optionals they still win the tie: both score 1.00 here, and the
        # shape explaining MORE optional columns takes it.
        "required": ["curve_mnem"],
        "optional": ["units", "stat_min", "stat_max", "stat_avg",
                     "lithology"],
        "min_required": 1, "target": None,
    },
    "log_run_header": {
        # The header block of a wireline/LWD run, written as label/value
        # PAIRS ("Log Date | 2001-06-03 | Log Type | WIRELINE"). Only the
        # first pair's labels are visible to identification — the rest sit
        # in body cells — so date + log_type is what there is to match on,
        # and log_type is unique to this shape. Extraction to one clean row
        # still needs the pair-grid pivot generalised beyond well_header.
        "required": ["date", "log_type"],
        "optional": ["top_md", "base_md", "interval", "method", "uwi"],
        "min_required": 2, "target": None,
    },
    "parameter_stats": {
        # "Parameter | Min | Max | Average | Units" — a statistics summary,
        # not a key/value table (no single value column, so key_value never
        # claims it). stat_min/stat_max are required and no other shape
        # requires them: discriminating by construction.
        "required": ["stat_min", "stat_max"],
        "optional": ["parameter", "stat_avg", "units"],
        "min_required": 2, "target": None,
    },
    "key_value": {
        # Structurally different: each ROW is a field, not a record. These
        # belong folded into the parent document's header, not a detail table.
        "required": ["parameter", "param_value"],
        "optional": ["units", "method"],
        "min_required": 2, "target": None,
    },
    "cement_bond": {
        "required": ["amplitude", "bond"],
        "optional": ["md", "interval", "top_md", "base_md", "remark"],
        "min_required": 2, "target": None,
    },
    "operations_npt": {
        "required": ["event", "category"],
        "optional": ["day", "duration", "cost", "md", "date", "remark"],
        "min_required": 2, "target": None,
    },
    "fluid_sample": {
        "required": ["sample_type", "api_gravity"],
        "optional": ["collection", "temperature", "pressure", "gor", "bsw", "md", "formation", "remark", "h2s", "co2", "sample_quality"],
        "min_required": 2, "target": None,
    },
    "curve_readings": {
        "required": ["md", "gamma"],
        "optional": ["density", "neutron", "resistivity", "porosity",
                     "saturation", "shale_vol", "lithology"],
        "min_required": 2, "target": None,
    },
}

numeric = {
    "pretest_pressure", "gradient", "mobility",
    "stat_min", "stat_max", "stat_avg",
    "top_md", "base_md", "tvd", "md", "net_pay", "weight", "set_depth",
    "cement_sacks", "stage", "fluid_vol", "proppant", "pressure", "rate",
    "clusters", "oil", "gas", "water", "gor", "days_on", "inclination",
    "azimuth", "dls", "ns", "ew", "closure", "porosity", "permeability",
    "saturation", "shale_vol", "net_gross", "gross",
    "_rate", "_volume", "owc", "goc", "gwc", "api_gravity", "temperature",
    "bsw", "duration", "cost", "gamma", "density", "neutron", "resistivity",
    "recovery", "length", "photos", "lateral", "stage_count", "total_depth",
    "kb_elev", "shot_count", "shot_density", "cement_top", "cement_base",
}

columns = {
    "casing": {
        # Against dv_well_casing's columns — the cat_ mirror inherits them.
        # `set_depth` is the string's SHOE, which is its base_depth; top_depth
        # is left for a document that states a hanger depth.
        "uwi": ["uwi"], "casing_string": ["casing_type"],
        "od": ["od_in"], "weight": ["weight_lb_ft"], "grade": ["grade"],
        "set_depth": ["base_depth"], "top_md": ["top_depth"],
        "cement_sacks": ["cement_volume_sacks"],
        "cement_top": ["cement_top"], "cement_base": ["cement_base"],
    },
    "formation_tops": {
        "uwi": ["uwi"], "formation": ["strat_unit_name"],
        "top_md": ["top_depth"], "base_md": ["base_depth"],
        "tvd": ["tvd_top"], "lithology": ["lithology"],
"gross": ["gross_thickness"],
        # NET PAY IS NOT GROSS THICKNESS. Gross is top-to-base; net pay is
        # the part that counts. Pointing both at gross_thickness meant
        # whichever the document stated last silently overwrote the other,
        # and the column would read as gross while holding net. Left
        # unmapped until cat_well_formation_top has a net_pay column —
        # extra_json keeps the value meanwhile, which is honest.
        # net_pay and fluid_type are ADDED COLUMNS (see the ALTERs shipped
        # with pack v11). Until they exist on a given deployment, capture
        # intersects the row against the live table and drops the unknown
        # key with a log line — so naming them here is safe early and
        # starts working the moment the columns appear.
        "net_pay": ["net_pay"],
        # THE SAME FIELD MEANS DIFFERENT THINGS IN DIFFERENT TABLES, which
        # is what a per-shape column map is for. In a frac or completion
        # table fluid_vol is a VOLUME in barrels; in a tops table the
        # "Fluid" column holds OIL / GAS / WATER — a phase. Bare "fluid"
        # cannot be split by alias (the unit that would disambiguate is
        # stripped as noise), so it resolves to fluid_vol everywhere and
        # THIS shape says what it means here.
        "fluid_vol": ["fluid_type"],
        # interp_date / interpreter_ba_id are real columns on the target.
        # NOTE the second is a BA *id* and documents state a NAME
        # ("Geologist_1"), so this lands a name in an id column until an
        # entity-resolution step exists — the same gap as DV_BUSINESS_ASSOCIATE
        # in the loader. Recorded rather than silently mapped.
        "date": ["interp_date"], "interpreter": ["interpreter_ba_id"],
    },
    "perforations": {
        # column names verified against dv_well_perforation
        "uwi": ["uwi"], "top_md": ["top_depth"], "base_md": ["base_depth"],
        "shot_count": ["shot_count"], "shot_density": ["shot_density"],
        "gun_type": ["gun_type"], "phasing": ["phasing_deg"],
        "formation": ["strat_unit_name"], "date": ["perf_date"],
        "perf_status": ["perf_status"], "well_status": ["perf_status"],
    },
    "core_run": {
        "uwi": ["uwi"], "formation": ["strat_unit_name"],
        "top_md": ["top_depth"], "base_md": ["base_depth"],
        "length": ["core_length"], "recovery": ["recovery_pct"],
        "show": ["core_show"], "date": ["core_date"],
        "photos": ["photo_count"],
    },
    "completion": {
        "uwi": ["uwi"], "completion_date": ["completion_date"],
        "orientation": ["well_orientation"], "formation": ["strat_unit_name"],
        "lateral": ["lateral_length_ft"], "stage_count": ["stage_count"],
        "fluid_vol": ["total_fluid_bbl"], "proppant": ["total_proppant_lbs"],
    },
    "doc_header_block": {
        # Same destinations as well_header — the transform above turns the
        # rotated Field/Value block into exactly that shape of row, so the two
        # must agree or the same document would load differently depending on
        # which layout it happened to use.
        "uwi": ["uwi"], "well_name": ["well_name"],
        "operator": ["operator_name", "operator_ba_id"],
        "field_name": ["field_name", "field_id"],
        "county": ["county"], "state": ["province_state"],
        "spud_date": ["spud_date"], "completion_date": ["completion_date"],
        "total_depth": ["final_td"], "kb_elev": ["kb_elevation"],
        "well_type": ["well_type"], "well_status": ["well_status"],
        "orientation": ["well_profile_type"],
        # ground_elev / lat / lon were in this map until `packs validate`
        # pointed out the pack has no such FIELDS — a destination column for
        # an attribute nothing can produce. These documents carry no
        # coordinates anyway; that is the one thing the header block lacks.
        #
        # tvd IS a field and has no column on dv_well, so it is reported as
        # unmapped rather than silently dropped.
    },
    "well_header": {
        "uwi": ["uwi"], "well_name": ["well_name"],
        "operator": ["operator_name", "operator_ba_id"],
        "field_name": ["field_name", "field_id"], "county": ["county"],
        "state": ["province_state"], "well_type": ["well_type"],
        # split_well_type puts the profile here; dv_well.well_profile_type
        # and dv_r_well_profile_type carry it, mirroring PPDM's own split
        # between r_well_type and r_well_profile_type.
        "orientation": ["well_profile_type"],
        "well_status": ["well_status"], "spud_date": ["spud_date"],
        "completion_date": ["completion_date"], "total_depth": ["final_td"],
        "md": ["final_td"],
        "kb_elev": ["kb_elevation"],
    },
    "fluid_contacts": {
        "uwi": ["uwi"], "formation": ["strat_unit_name"],
        "owc": ["owc_depth"], "goc": ["goc_depth"], "gwc": ["gwc_depth"],
    },
    "frac_stage": {
        "uwi": ["uwi"], "stage": ["stage_num"],
        "top_md": ["stage_top_depth"], "base_md": ["stage_base_depth"],
        "clusters": ["num_clusters"], "fluid_vol": ["fluid_volume_bbl"],
        "proppant": ["proppant_mass_lbs"],
        "pressure": ["max_treating_pressure_psi"],
        "rate": ["max_rate_bpm"], "date": ["stage_date"],
    },
    "directional_survey": {
        "uwi": ["uwi"], "md": ["md"], "inclination": ["incl"],
        "azimuth": ["azim"], "tvd": ["tvd"],
        "ns": ["ns_offset"], "ew": ["ew_offset"], "dls": ["dls"],
        # no `closure` column on cat_well_dir_srvy_sta — reported, not dropped
    },
    "petrophysics": {
        "uwi": ["uwi"], "formation": ["zone_name"],
        "top_md": ["top_depth"], "base_md": ["base_depth"],
        "gross": ["gross_thickness"], "net_pay": ["net_thickness"],
        "net_gross": ["net_to_gross"], "shale_vol": ["vsh_avg"],
        "porosity": ["phi_effective_avg"], "saturation": ["sw_avg"],
        "permeability": ["perm_avg_md"],
    },
    "production": {
        "uwi": ["UWI"], "date": ["period_date"],
        "days_on": ["days_on_prod"],
        # oil/gas/water are NOT columns here — see unpivot_production
        "_fluid": ["fluid_type"], "_volume": ["volume"],
        "_rate": ["avg_daily_rate"],
    },
    "dst": {
        "uwi": ["uwi"], "date": ["test_date"],
        "top_md": ["top_depth"], "base_md": ["base_depth"],
        "oil": ["max_oil_rate"], "gas": ["max_gas_rate"],
        "water": ["max_water_rate"], "gor": ["gor"],
        "api_gravity": ["api_gravity"], "pressure": ["max_shut_in_pressure"],
        "formation": ["strat_unit_name"],
    },
    "core_sample": {
        "uwi": ["uwi"], "sample_type": ["sample_type"],
        "md": ["sample_depth"], "top_md": ["top_depth"],
        "base_md": ["base_depth"], "porosity": ["porosity_frac"],
        "permeability": ["permeability_air_md"],
        "saturation": ["water_saturation_frac"], "lithology": ["lithology"],
    },
}

def unpivot_production(rows):
    """A wide IP/flow table -> one row per FLUID, which is how cat_prod_volume
    is shaped.

    The table reads `Oil (bbl/d) | Gas (Mcf/d) | Water (bbl/d)` across, but
    cat_prod_volume stores fluid_type + volume + avg_daily_rate DOWN. That is a
    genuine pivot, not a rename — one document row becomes three database rows —
    and it's the same columns-become-rows transform the PPDM fan-out does.

    Rates are per-day figures on these tickets, so they land in avg_daily_rate
    and `volume` is left for the loader to derive if it wants days_on x rate.
    """
    out = []
    for r in rows:
        base = {k: v for k, v in r.items()
                if k in ("uwi", "date", "days_on") and v not in (None, "")}
        for field, fluid in (("oil", "OIL"), ("gas", "GAS"),
                             ("water", "WATER")):
            val = r.get(field)
            if val in (None, ""):
                continue
            out.append({**base, "_fluid": fluid, "_rate": val,
                        "_shape": "production"})
    return out


def pivot_fluid_contacts(rows):
    """Long contact rows -> the wide owc/goc/gwc columns the table actually has.

    The document lists one row per contact (`Contact Type | Depth MD`), while
    cat_well_formation_top carries owc_depth, goc_depth and gwc_depth as three
    separate columns. This is the production unpivot in reverse: several
    document rows collapse into one database row per formation.
    """
    by_key = {}
    for r in rows:
        kind = str(r.get("contact_type") or "").strip().upper()
        depth = r.get("md") or r.get("top_md")
        col = ("owc" if "OWC" in kind or "OIL" in kind else
               "goc" if "GOC" in kind else
               "gwc" if "GWC" in kind else None)
        if not col or depth in (None, ""):
            continue
        key = (r.get("uwi") or "", r.get("formation") or "")
        rec = by_key.setdefault(key, {"uwi": key[0], "formation": key[1],
                                      "_shape": "fluid_contacts"})
        rec[col] = depth
    return list(by_key.values())
def pivot_header_block(rows):
    """Field/Value pairs -> ONE well-header row.

    The label column IS a header, rotated ninety degrees: "Well Name",
    "Spud Date", "Total Depth (ft MD)" are the same strings that appear as
    column headings in a tabular well header. So resolve each label with the
    pack's OWN alias matching rather than a second hand-written lookup — one
    vocabulary, used both ways, and an alias added for a column header works
    for the rotated form for free.

    Labels that resolve to nothing are kept under _extra, not dropped: a
    header block is exactly where a document puts the attribute nobody
    anticipated.
    """
    from docshape.engine.recognise import Recogniser
    from docshape.packs import load as _load
    rec = Recogniser(_load("petroleum"))

    out, extra = {}, {}
    for r in rows:
        label = r.get("field_name")
        value = r.get("param_value")
        if label is None or str(label).strip() == "":
            continue
        if value is None or str(value).strip() == "":
            continue
        fld = rec.field_for(label)
        # field_name is what the LABEL column matched; it never means the
        # oil field here unless the label itself says so.
        if fld and fld not in ("field_name", "param_value"):
            out.setdefault(fld, str(value).strip())
        elif fld == "field_name" and str(label).strip().lower() in (
                "field", "field name"):
            out.setdefault("field_name", str(value).strip())
        else:
            extra[str(label).strip()] = str(value).strip()
    if not out:
        return []
    if extra:
        out["_extra"] = extra
    out["_shape"] = "doc_header_block"
    return [out]


def pivot_pair_grid(rows):
    """A well header laid out as label/value PAIRS across the page —
    "API / UWI | 42329100010000 | Well Name | ANADARKO MIDL 001" on the
    first line, then "Operator | … | Licensee | …" below — is a header
    rotated ninety degrees TWICE over: every odd column is labels, every
    even column is values, and the first line's own pair became the
    "column names" when the reader promoted row 0.

    Attached to well_header, because after the apiuwi alias that is what
    the first line identifies as. So DETECT the form from the data before
    touching anything: in a pair grid the cells sitting in the mapped
    fields are LABEL WORDS the vocabulary knows (Operator, Status, Field);
    in a genuine columnar header they are identifiers, names and dates it
    doesn't. Below half recognised → columnar → rows pass through
    untouched.

    Reconstruction uses what map_rows preserved, positionally: the mapped
    fields (in column order) are the label columns, each row's _extra
    values (in column order) are the value columns, and the CONSTANT
    _extra KEYS are the first line's value cells — the actual UWI and well
    name. Labels resolve through the pack's own alias matching, exactly
    like pivot_header_block: one vocabulary, used both ways.
    """
    from docshape.engine.recognise import Recogniser, INTERNAL_KEYS
    from docshape.packs import load as _load
    rec = Recogniser(_load("petroleum"))

    if not rows:
        return rows

    out, extra = {}, {}

    def put(label, value):
        v = str(value).strip()
        lab = str(label).strip()
        if not v or not lab:
            return
        fld = rec.field_for(lab)         # colon intact — it IS the marker
        lab = lab.rstrip(":")            # ...but display keys drop it
        # field_name is what a label column matches; it only means the oil
        # field when the label itself says so (the July 27 lesson).
        if fld == "field_name" and lab.lower() not in ("field", "field name"):
            fld = None
        if fld and fld != "param_value":
            if fld in out:               # second claim on a taken field —
                extra[lab] = v           # keep the pair, don't drop it
            else:
                out[fld] = v
        else:
            extra[lab] = v

    hdr = rows[0].get("_hdr")
    if hdr and rows[0].get("_cells") is not None:
        # POSITIONAL path (engine supplies the raw cells): walk every line
        # — the header line included, since its own pair holds the actual
        # uwi and well name — as (label, value) at (i, i+1). The colmap is
        # never consulted, so a VALUE that happens to resolve to a field
        # ("STATE ALPHA 12H" -> state) cannot corrupt the structure.
        body = [r.get("_cells") or [] for r in rows]
        evens = [c for line in body for c in line[0::2] if str(c).strip()]
        if not evens:
            return rows
        hits = sum(1 for c in evens
                   if rec.field_for(c))
        if hits / len(evens) < 0.5:
            return rows                  # columnar — not ours to rearrange
        for line in [list(hdr)] + body:
            for i in range(0, len(line) - 1, 2):
                put(line[i], line[i + 1])
    else:
        # FALLBACK for rows mapped by an older engine (no _cells): the
        # original colmap/_extra reconstruction. Works when no value cell
        # resolves to a field; the positional path above removes even that
        # caveat.
        label_cells = [v for r in rows for k, v in r.items()
                       if k not in INTERNAL_KEYS]
        if not label_cells:
            return rows
        hits = sum(1 for v in label_cells if rec.field_for(v))
        if hits / len(label_cells) < 0.5:
            return rows
        first = rows[0]
        mapped = [k for k in first if k not in INTERNAL_KEYS]
        for f, v in zip(mapped, list((first.get("_extra") or {}).keys())):
            out.setdefault(f, str(v).strip())
        for r in rows:
            labels = [r[k] for k in r if k not in INTERNAL_KEYS]
            values = list((r.get("_extra") or {}).values())
            if len(labels) != len(values):
                continue
            for lab, val in zip(labels, values):
                put(lab, val)

    if not out:
        return rows
    if extra:
        out["_extra"] = extra
    out["_shape"] = "well_header"
    return [out]


# ── well_type carries TWO attributes in one string ────────────────────────
# Documents write "OIL — Horizontal": a well TYPE and a well PROFILE joined by
# a dash. dv_r_well_type holds OIL/GAS/DRY/INJECTION and correctly refuses the
# composite, so 82 rows were held on values whose left half was already in the
# vocabulary. Adding "OIL — Horizontal" as a code would encode a cross-product
# — four types by three profiles is twelve codes — and would make "every
# horizontal well" unanswerable, because the profile would be buried inside a
# type string.
#
# Split ONLY when the other half is a recognised profile word. A dash inside a
# value we do not understand is left exactly as found: a transform that
# guesses is worse than a value that gets reported.
_PROFILE_WORDS = {
    "horizontal":   "HORIZONTAL",
    "vertical":     "VERTICAL",
    "directional":  "DIRECTIONAL",
    "deviated":     "DIRECTIONAL",
    "slant":        "DIRECTIONAL",
    "sidetrack":    "SIDETRACK",
    "multilateral": "MULTILATERAL",
}

# EM DASH FIRST, and note it is \u2014 WITH SPACES AROUND IT, not a hyphen. A
# splitter matching only "-" misses every one of these, which is exactly the
# kind of near-miss that looks like the transform never ran.
_TYPE_SPLITS = ("\u2014", "\u2013", "-", "/", "|")


def split_well_type(rows):
    """OIL — Horizontal  ->  well_type=OIL, orientation=HORIZONTAL.

    Either side may hold the profile — both "OIL — Horizontal" and
    "Horizontal — OIL" occur — so check both before deciding.
    """
    for r in rows:
        v = r.get("well_type")
        if not isinstance(v, str) or not v.strip():
            continue
        for ch in _TYPE_SPLITS:
            if ch not in v:
                continue
            left, _, right = v.partition(ch)
            lo, ro = left.strip(), right.strip()
            if ro.lower() in _PROFILE_WORDS:
                r["well_type"], prof = lo, _PROFILE_WORDS[ro.lower()]
            elif lo.lower() in _PROFILE_WORDS:
                r["well_type"], prof = ro, _PROFILE_WORDS[lo.lower()]
            else:
                break            # not a profile — leave the value alone
            # setdefault: a document that stated orientation SEPARATELY has
            # said so explicitly, and that beats anything inferred from a
            # composite type string.
            r.setdefault("orientation", prof)
            break
    return rows


def well_header_rows(rows):
    """pivot_pair_grid, then split the composite well_type."""
    return split_well_type(pivot_pair_grid(rows))


def header_block_rows(rows):
    """pivot_header_block, then split the composite well_type."""
    return split_well_type(pivot_header_block(rows))


transforms = {"production": unpivot_production,
              "fluid_contacts": pivot_fluid_contacts,
              "doc_header_block": header_block_rows,
              "well_header": well_header_rows,
    # THE SAME PIVOT, THE SAME FORM. An end-of-well summary and a logging
    # run header are written exactly like a pair-grid well header — odd
    # columns are labels, even columns their values, and the first pair
    # became the "column names" when the reader promoted row 0. Both
    # identified correctly for weeks and then extracted as three rows of
    # labels, because the transform was only ever attached to well_header.
    # pivot_pair_grid detects the form from the DATA and passes a genuine
    # columnar table through untouched, so attaching it costs nothing.
    # Both carry the same composite well_type as a pair-grid header, so they
    # take the composed form too — not the bare pivot.
    "eow_summary_pairs": well_header_rows,
    "log_run_header": well_header_rows,}


# ── identity ───────────────────────────────────────────────────────────────
# Every pack names the field that identifies its SUBJECT and how to normalise
# it. For petroleum that is the well: a 14-character UWI. For a legal pack it
# might be a matter or contract number. The engine never refers to "uwi" — it
# asks the pack.
identity_field = "uwi"


def normalise_identity(v):
    """Right-pad to the 14-character key the loaders store.

    Documents abbreviate: "42-013-20689" drops the last four digits, so an
    exact comparison against a stored UWI matches nothing.
    """
    d = re.sub(r"[^0-9]", "", str(v or ""))
    return (d + "0" * 14)[:14] if d else None


def identity_from_name(path):
    """A 10-14 digit run in the FILE NAME, as last-resort identity.

    Real archives name files after the well far more often than not. Used only
    when neither the row nor the classifier supplied one, so it can never
    override real evidence.
    """
    m = re.search(r"(?<!\d)(\d{10,14})(?!\d)", os.path.basename(str(path or "")))
    return normalise_identity(m.group(1)) if m else None

# API / UWI as it appears in document TEXT. Ordered most-specific first: a
# labelled value is stronger evidence than a bare number that happens to have
# fourteen digits.
_ID_PATTERNS = [
    re.compile(r"(?:API|UWI)\s*(?:/\s*(?:API|UWI)\s*)?[:#]?\s*"
               r"(\d{2}[-\s]?\d{3}[-\s]?\d{5}(?:[-\s]?\d{2,4})?)", re.I),
    re.compile(r"(?:API|UWI)\s*(?:/\s*(?:API|UWI)\s*)?[:#]?\s*(\d{10,14})", re.I),
    re.compile(r"\bUS(\d{10,14})\b"),
    re.compile(r"(?<![\d-])(\d{2}-\d{3}-\d{5}(?:-\d{2,4})?)(?![\d-])"),
    re.compile(r"(?<!\d)(\d{14})(?!\d)"),
]


def identity_from_text(text):
    """Find a well identifier in a document's text.

    Used when the document has no header TABLE for the recogniser to read —
    which is most vendor reports, where the API sits in a letterhead block as
    "API / UWI: 42001205760000" rather than in a grid.
    """
    if not text:
        return None
    for pat in _ID_PATTERNS:
        m = pat.search(text)
        if m:
            got = normalise_identity(m.group(1))
            if got and got != "0" * 14:
                return got
    return None

_NAME_PATTERNS = [
    re.compile(r"WELL\s*NAME\s*[:#]\s*([^\n]{2,60}?)\s*(?:API|UWI|FIELD|"
               r"OPERATOR|STATE|COUNTY|$)", re.I),
    re.compile(r"\bWELL\s*[:#]\s*([^\n]{2,60})", re.I),
]


def subject_from_text(text):
    """The well's NAME, when the document gives no identifier.

    Real vendor reports often print "API / UWI:" with nothing after it — the
    Baker Hughes survey does exactly that — and identify the well only by
    name. Capturing the name is not the same as identifying the well, and
    docshape deliberately can't resolve one to the other: that needs a
    reference table, which would make the capture stage depend on a database.
    So record what the document said and let MIGRATION resolve it.
    """
    if not text:
        return None
    for pat in _NAME_PATTERNS:
        m = pat.search(text)
        if m:
            got = " ".join(m.group(1).split()).strip(" .:-")
            if got and len(got) > 2:
                return got[:120]
    return None

