"""Generate docs/data-sources.html from build/source_manifest.csv.

GENERATED, NOT HAND-WRITTEN. The manifest is the evidence -- agency, source
URL, terms URL, attribution requirement, restrictions, date checked -- and this
page is only its presentation. Written by hand the two would drift, and an
acknowledgement page that disagrees with the research behind it is worse than
no page at all: it makes a public claim nobody can trace.

    python tools/build_data_sources_page.py

Re-run after any change to the manifest.

THE WELL COUNT IS DEDUPLICATED BY STATE, and that is not a detail. Texas was
downloaded twice (`Texas` and `New Texas`, both from the Railroad Commission),
so summing the manifest rows credits the RRC with 1,433,510 wells against a
master holding 716,755 of them. The first draft of this page published
4,747,807 wells for a 4,031,052-row master. A number on a public page that
does not reconcile to the database is exactly the confident wrong value this
codebase exists to avoid, so the total is asserted against the master here and
the script refuses to write a page that does not add up.
"""
import csv
import html
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MANIFEST = "build/source_manifest.csv"
OUT = "docs/data-sources.html"
# The master's own row count, asserted so a silent double-count cannot ship.
MASTER_ROWS = 4031052

KS_CITATION = ("The source of this material is the Kansas Geological Survey "
               "website at http://www.kgs.ku.edu/. All Rights Reserved.")

# Reproduced in each agency's own words where they state a preferred form.
ATTRIBUTIONS = [
    ("Kansas Geological Survey", KS_CITATION, True),
    ("Illinois State Geological Survey",
     "Material from the ISGS is credited to the Illinois State Geological "
     "Survey, Prairie Research Institute, University of Illinois.", False),
    ("Ohio Department of Natural Resources",
     "Well data compiled by the Ohio Department of Natural Resources, Division "
     "of Oil and Gas Resources Management, which reserves publication rights "
     "to the material.", False),
    ("Kentucky Geological Survey",
     "Oil and gas well data copyright Kentucky Geological Survey, University "
     "of Kentucky.", False),
    ("California Geologic Energy Management Division",
     "Well data from CalGEM, published under a Creative Commons Attribution "
     "licence.", False),
    ("Washington Geological Survey",
     "Cited following the Washington Geological Survey's published citation "
     "guidelines.", False),
]


def load():
    rows = list(csv.DictReader(open(MANIFEST, encoding="utf-8")))
    wells = lambda r: int(r["wells_in_master"] or 0)
    byagency = defaultdict(list)
    for r in rows:
        byagency[r["agency_from_filenames"]].append(r)
    out = []
    for name, rs in byagency.items():
        # ONE COUNT PER STATE, not per directory -- see the module docstring.
        per_state = {}
        for x in rs:
            per_state[x["state"]] = max(per_state.get(x["state"], 0), wells(x))
        best = max(rs, key=wells)
        out.append({
            "name": name,
            "wells": sum(per_state.values()),
            "url": best["official_url"],
            "terms": best["terms_url"],
            "attr": best["attribution_required"],
            "restrict": best["restrictions"],
            "states": ", ".join(sorted(s for s in per_state if s)),
            "vintage": max(x["newest_file"] for x in rs),
        })
    out.sort(key=lambda a: (-a["wells"], a["name"]))
    return out


def main():
    ag = load()
    total = sum(a["wells"] for a in ag)
    if total != MASTER_ROWS:
        raise SystemExit(
            "REFUSING TO WRITE: the page would claim %s wells but the master "
            "holds %s. Fix the aggregation before publishing a number nobody "
            "can reconcile." % (format(total, ","), format(MASTER_ROWS, ",")))
    E = html.escape

    def link(u, txt):
        return '<a href="%s" rel="noopener">%s</a>' % (E(u), E(txt)) if u else ""

    cards = []
    for a in ag:
        flag = ""
        if a["attr"].startswith("YES"):
            flag = ' <span class="c-amber">attribution required</span>'
        elif a["attr"].startswith("VERIFY"):
            flag = ' <span class="c-amber">terms vary by layer</span>'
        cards.append(
            '  <div class="card">\n'
            '    <div class="eyebrow">%s &middot; %s</div>\n'
            '    <h3>%s</h3>\n'
            '    <p class="sub">%s%s</p>\n'
            '    <p class="sub">%s%s</p>\n'
            '  </div>'
            % (E(a["states"] or "&mdash;"),
               (format(a["wells"], ",") + " wells") if a["wells"]
               else "obtained, not loaded",
               E(a["name"]), E(a["restrict"]), flag,
               link(a["url"], "data source"),
               (" &middot; " + link(a["terms"], "terms")) if a["terms"] else ""))

    attrib = "\n".join(
        '  <div class="card">\n'
        '    <div class="eyebrow">%s</div>\n'
        '    <p class="%s">%s</p>\n'
        '  </div>' % (E(nm), "pull" if verbatim else "sub", E(txt))
        for nm, txt, verbatim in ATTRIBUTIONS)

    page = TEMPLATE % {
        "n_agencies": len(ag),
        "n_wells": format(total, ","),
        "from": min(a["vintage"] for a in ag),
        "to": max(a["vintage"] for a in ag),
        "attributions": attrib,
        "cards": "\n".join(cards),
    }
    open(OUT, "w", encoding="utf-8").write(page)
    print("wrote %s (%.1f KB)" % (OUT, len(page) / 1024))
    print("  %d agencies, %s wells -- reconciles to the master"
          % (len(ag), format(total, ",")))


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Data sources &amp; acknowledgements &mdash; Data Wrangler</title>
<meta name="description" content="Every public agency whose well data appears in Data Wrangler's reference well set, with the source, the terms it was published under, and when it was taken.">
<link rel="stylesheet" href="assets/site.css">
</head>
<body>

<header class="topbar">
  <div class="wrap">
    <a class="brand" href="index.html">DATA<span>&middot;</span>WRANGLER</a>
    <nav class="topnav">
      <a href="index.html#what">The platform</a>
      <a href="index.html#films">Walkthroughs</a>
      <a href="components.html">Components</a>
      <a href="data-sources.html" aria-current="page">Data sources</a>
    </nav>
  </div>
</header>

<main>

<div class="hero" style="padding:80px 0 56px">
  <div class="wrap">
    <div class="eyebrow">Acknowledgements</div>
    <h1 style="max-width:22ch">Whose data<br>this is</h1>
    <p class="lede">
      Data Wrangler's reference well set is built from public records published
      by state regulators, state geological surveys and federal agencies. It is
      their work. This page names every one of them, links to the data as they
      publish it, and states the terms it was taken under.
    </p>
    <p class="sub">
      %(n_agencies)d agencies &middot; %(n_wells)s well records &middot;
      obtained %(from)s to %(to)s. Data Wrangler reads and normalises this data;
      the agencies' own systems remain the authoritative record.
    </p>
  </div>
</div>

<section class="wrap">
  <h2>Required attributions</h2>
  <p class="sub">
    Several agencies ask to be credited by name. Those requests are reproduced
    here in their own words.
  </p>
%(attributions)s
</section>

<section class="wrap">
  <h2>Every source</h2>
  <p class="sub">
    Ordered by how much of the reference set each contributes.
    &ldquo;Obtained, not loaded&rdquo; means the data is held but is not in the
    current build.
  </p>
%(cards)s
</section>

<section class="wrap">
  <h2>How this data is used</h2>
  <p class="sub">
    The reference set resolves well identifiers found in documents and logs, and
    places wells on a map. It is a normalised copy of published public records.
    Every agency here disclaims warranty as to accuracy and completeness, and so
    do we &mdash; check the source before relying on a value.
  </p>
  <p class="sub">
    Corrections are welcome. If your agency is listed incorrectly, or you would
    prefer different wording, please get in touch and it will be changed.
  </p>
</section>

</main>

<footer>
  <div class="wrap">
    <div>
      <strong style="font-family:var(--mono);color:var(--ink)">Data Wrangler Solutions LLC</strong><br>
      Petroleum data management &middot; SQL Server &middot; Oracle &middot; Snowflake
    </div>
    <div>
      <a href="components.html">Component documentation</a>
    </div>
  </div>
</footer>

</body>
</html>
"""

if __name__ == "__main__":
    main()
