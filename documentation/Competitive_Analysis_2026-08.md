# Data Wrangler v4 — Competitive Analysis
**August 2026**

---

## The one-sentence version

Everyone can read your documents now. We're the only one that puts what they say
into a database that stays correct.

---

## The market has three layers. We are the only product in all three.

| Layer | What it does | Who plays there |
|---|---|---|
| **Destination schema** | Owns a governed E&P data model; enforces keys, FKs, reference codes | Quorum EnergyIQ (TDM), Petrosys dbMap, Katalyst iGlass |
| **Extraction** | Turns documents into structured output | Collide, rannsCDE, LlamaParse, Unstructured, Energent.ai, scanning bureaus |
| **Content** | Sells the data itself | Enverus, S&P Global, TGS |

Data Wrangler v4 spans all three: DataView (schema + mapping), File Catalog and
Document Assistant (extraction), Data Assistant (structured load), and 3.9 million
public-agency well headers shipped in the box.

---

## Layer 1 — The PPDM incumbents

### Quorum EnergyIQ — the direct schema competitor

The closest architectural match, and the one that matters most.

- Quorum acquired EnergyIQ in Feb 2020.
- EnergyIQ had already absorbed petroWEB's EnterpriseDB, Navigator and Gateway
  (wellbore + GIS).
- Quorum then merged with Aucerna and acquired TietoEVRY's Oil & Gas software
  business (Energy Components, DaWinci).

The EIQ stack is three layers: **TDM** (the well master foundation), **IQexchange**
(ingestion and synchronisation), **IQinsights** (analytics and visualisation).

That is structurally the same shape as Data Wrangler — foundation, ingestion,
exploration. The difference is everything around it.

**What this means for us:** the one product that could match our schema story is now
a component inside a large upstream suite serving 1,800+ customers, sold to companies
that already run Quorum accounting and land. It is stickier at the top of the market
and *less* interested in the bottom. Their own positioning targets "midsized to large
E&P companies."

**What they have that we don't:** reference customers, SOC 2, services organisation,
integrations with OpenWorks, GeoGraphix, ARIES, Kingdom.

**What we have that they don't:** a populated database on day one. TDM ships as a
schema you then spend a services engagement filling.

### Petrosys dbMap, Katalyst iGlass

Same fundamental posture — enterprise-priced, services-heavy, empty schema on
delivery. Katalyst additionally sells physical data management and digitisation
services we don't compete with and shouldn't pretend to.

---

## Layer 2 — This is what changed

**The AI extraction layer has commoditised.** In early August the extraction
competitors were generic — Grooper, scanning bureaus, log-digitisation point tools.
That is no longer true.

### Collide — the sharpest new entrant

Positions as "AI software built for oil and gas operators — purpose-built for
upstream E&P, not generic AI." Targets engineers, landmen and field teams.

Their published pitch:
- Lease terms extracted from 50-year-old scanned contracts
- Well logs and daily drilling reports queried in plain English
- Regulatory filing automation (Texas RRC W-10, G-10)
- Case study: a client inherited 80GB of PDF lease records after a $700M
  acquisition; extracted to structured data "in days instead of months"

Forward-deployed engineer model, production in weeks.

### The commodity tier

rannsCDE (350+ document types, no-code, template-free), LlamaParse, Unstructured,
Energent.ai. Extraction is now semantic reasoning by LLM/VLM rather than template
OCR, which means new document types no longer require new templates.

### The consequence, stated plainly

**"We use AI to extract data from your documents" is no longer a differentiator.**
Any pitch built on that claim will be met with three competitors saying the same
thing, two of them with reference customers.

Anything in our materials that leads with AI extraction needs rewriting.

---

## Layer 3 — Data providers

Enverus, S&P Global, TGS. They sell curated data, not a place to put yours. The
standard objection — "we already buy Enverus" — is answered by pointing at the
customer's own file server: Enverus doesn't hold their AFEs, their scout tickets,
their directional surveys, or the well files they inherited in the last acquisition.

---

## The two moats

Both are structural. Neither is a sprint away for the extraction vendors.

### 1. We own the destination schema

Extraction vendors emit structured output into whatever the customer already has —
usually a spreadsheet or a data lake. We emit into a governed PPDM derivative with:

- FK resolution against live introspection
- Reference-code seeding with an explicit human decision
- Gates that **hold** rows rather than inserting orphans
- Provenance from every row back to the source document

That last group is the part that cannot be bolted on. A vendor whose product is
"turn documents into JSON" has no opinion about what happens when a formation top
arrives for a well that isn't in the database yet. We hold it and say why. They
insert it and the customer finds out in eighteen months.

This is a different product category, not a feature gap.

### 2. The 3.9 million well headers

A customer starts with a populated database rather than an empty schema. This got
*sharper* this month, not weaker — the one incumbent that could have matched it is
now inside an enterprise suite priced out of our market.

### Their own material makes our argument

Collide's site cites the MIT finding that 95% of generative AI pilots fail, and
attributes the failure to the data layer underneath rather than the model. Gartner
is quoted elsewhere predicting organisations will abandon 60% of AI projects that
lack AI-ready data.

That is our thesis, published by a competitor. The extraction vendors are
successfully making the case that the data layer is the problem — and they don't
sell one.

---

## Honest gaps

Do not let marketing discover these in front of a prospect.

| Gap | Mitigation |
|---|---|
| No reference customers | Lead with their own documents in the demo, not case studies |
| No SOC 2 | On-prem deployment; their data never leaves their building |
| Single-person continuity risk | Escrow, documented handoff, source availability terms |
| No seismic trace management or digitisation services | Partner or decline; don't bluff |
| No track record | Pilot pricing that makes trying it cheap |

The continuity question will come up on every serious call. Have an answer written
down before Tom needs it.

---

## Targets

**Tier 1**
- PE-backed independents, 100–2,000 wells, post-acquisition
- Non-operated WI and mineral/royalty companies
- A&D advisory and due-diligence shops

**Tier 2**
- State geological surveys (source of the headers; credential value)
- Small consultancies and service shops as resellers
- Legacy operators with ARO/P&A record obligations

**Avoid**
- Majors and large independents — incumbents plus procurement
- Production and accounting software buyers — wrong problem
- Anyone with no document backlog — no urgency, no sale

---

## Positioning lines

Use these. They survive contact with the current market.

> Everyone can read your documents now. We're the only one that puts what they say
> into a database that stays correct.

> The extraction vendors will hand you structured output. Into what?

> You don't start with an empty schema. You start with 3.9 million wells.

> When a record can't be loaded correctly, we hold it and tell you why. We don't
> guess, and we don't insert something that looks right.

---

## Objection handling

**"We already use Enverus / S&P."**
Those are data feeds. They don't contain your AFEs, your scout tickets, or the well
files you inherited last acquisition. This is for your data.

**"How is this different from [AI extraction vendor]?"**
They extract. We extract into a governed database with referential integrity and
provenance. Ask them where the data lands and what happens when it doesn't fit.

**"What if you get hit by a bus?"**
[Written answer required — escrow, source terms, documentation. Do not improvise.]

**"Can you do this at enterprise scale?"**
Not the target. This is built for companies too small to employ a data manager.
Saying so is a qualifier, not a weakness.

---

## What to watch

1. **An extraction vendor adding a schema.** The likeliest threat. Collide is the
   candidate — forward-deployed engineers, operator relationships, and momentum. If
   they ship a governed destination model, the moat narrows fast.
2. **OSDU adoption in the midmarket.** Currently an enterprise concern. If it reaches
   companies at our size, "we're a PPDM derivative" becomes a question rather than a
   credential.
3. **Quorum moving down-market.** Unlikely given the suite economics, but a
   self-serve EIQ tier would be a direct hit.

---

## Sources

- Quorum EnergyIQ product page — https://www.quorumsoftware.com/products/energyiq
- EIQ three-layer architecture — https://www.quorumsoftware.com/blog/posts/digital-journey-series-the-energy-data-platform/
- Quorum/EnergyIQ acquisition — https://www.quorumsoftware.com/about/press-releases/quorum-software-acquires-energyiq/
- petroWEB assets, Aucerna merger, TietoEVRY acquisition — https://www.privsource.com/acquisitions/deal/quorum-software-acquires-energyiq-bWSxOl
- Collide — https://collide.io/ and https://collide.io/ai-software-oil-and-gas
- rannsCDE — https://rannsolve.com/blog/ai-document-processing-for-oil-and-gas/
