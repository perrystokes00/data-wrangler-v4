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
MASTER = "WELL_REF.well_ref.well_master_public_v2"

# THE COUNTS COME FROM THE MASTER ITSELF, NOT FROM THE MANIFEST.
# This used to assert the manifest's total against a hard-coded MASTER_ROWS =
# 4031052. That guard caught a double-count once and then rotted: when the
# master was rebuilt from the source files it held 3,140,361 wells, the
# constant still said 4,031,052, the manifest still summed to 4,031,052 -- and
# the check PASSED, printing "reconciles to the master" while writing a public
# page describing a table that no longer existed. A number is only reconciled
# if it is compared to the thing it claims to describe, so both sides are now
# read live and the manifest supplies evidence (URLs, terms, dates) alone.


def db(database="DataView_Demo"):
    import urllib.parse
    from sqlalchemy import create_engine
    cs = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;"
          "DATABASE=%s;Trusted_Connection=yes;" % database)
    return create_engine("mssql+pyodbc:///?odbc_connect="
                         + urllib.parse.quote_plus(cs))


def master_counts():
    """(per-state well counts, total) straight from the published table."""
    from sqlalchemy import text
    with db("WELL_REF").connect() as c:
        rows = {(r[0] or "").strip(): r[1] for r in c.execute(text(
            "SELECT province_state, COUNT(*) FROM %s GROUP BY province_state"
            % MASTER))}
        total = c.execute(text("SELECT COUNT(*) FROM %s" % MASTER)).scalar()
    return rows, total


def licences():
    from sqlalchemy import text
    with db().connect() as c:
        return {r[0]: r[1] for r in c.execute(text(
            "SELECT province_state, licence_class FROM dataview.dv_source_licence"))}

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


WHY_NOT = {
    "UNVERIFIED": "held, not published &mdash; no terms of use found, so we asked",
    "RESTRICTED": "held, not published &mdash; the agency restricts redistribution",
    "AGGREGATOR": "held, not published &mdash; obtained via an aggregator, not the agency",
}


def load():
    rows = list(csv.DictReader(open(MANIFEST, encoding="utf-8")))
    counts, total = master_counts()
    lic = licences()
    byagency = defaultdict(list)
    for r in rows:
        byagency[r["agency_from_filenames"]].append(r)

    # A STATE BELONGS TO ONE AGENCY CARD. Indiana arrives under two folders
    # (`Indiana` and `Indiana(Limited Data)`) whose agency names differ, so
    # both cards claimed all 78,257 of its wells and the page overclaimed by
    # exactly that. Deduplicating per state INSIDE a card was not enough --
    # the collision is BETWEEN cards. Whichever card holds the most rows for a
    # state carries its count; the other still lists the state, with none.
    owner = {}
    for name, rs in byagency.items():
        for x in rs:
            s = (x["state"] or "").strip()
            if not s:
                continue
            n = int(x["wells_in_master"] or 0)
            if s not in owner or n > owner[s][1]:
                owner[s] = (name, n)

    out = []
    for name, rs in byagency.items():
        # ONE COUNT PER STATE, not per directory -- Texas was downloaded twice
        # (`Texas` and `New Texas`, both from the RRC), and summing the rows
        # credited the RRC with 1,433,510 wells against 716,755 real ones.
        states = sorted({(x["state"] or "").strip()
                         for x in rs if (x["state"] or "").strip()})
        published = {s: counts.get(s, 0) for s in states
                     if counts.get(s) and owner.get(s, ("",))[0] == name}
        held = [s for s in states if not counts.get(s)]
        best = max(rs, key=lambda r: int(r["wells_in_master"] or 0))
        why = ""
        if held and not published:
            classes = {lic.get(s) for s in held} - {None}
            why = WHY_NOT.get(sorted(classes)[0] if classes else "",
                              "held, not published")
        out.append({
            "name": name,
            "wells": sum(published.values()),
            "url": best["official_url"],
            "terms": best["terms_url"],
            "attr": best["attribution_required"],
            "restrict": best["restrictions"],
            "states": ", ".join(states),
            "why_not": why,
            "vintage": max(x["newest_file"] for x in rs),
        })
    out.sort(key=lambda a: (-a["wells"], a["name"]))
    return out, total


def main():
    ag, master_total = load()
    total = sum(a["wells"] for a in ag)
    if total != master_total:
        raise SystemExit(
            "REFUSING TO WRITE: the page would claim %s wells but %s holds %s. "
            "Fix the aggregation before publishing a number nobody can "
            "reconcile." % (format(total, ","), MASTER,
                            format(master_total, ",")))
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
               else (a["why_not"] or "obtained, not published"),
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
    Ordered by how much of the published set each contributes. Where a card
    says &ldquo;held, not published&rdquo;, we have the agency&rsquo;s data but
    have not included it: either the agency restricts redistribution, or it
    publishes no terms of use at all and we have written to ask rather than
    assume. Those states are absent from the figures above.
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
