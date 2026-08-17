# docshape — runbook

Recognises tables in documents by **what their columns are**, not where they
sit. This file is the "something isn't recognised, now what" guide. For the
architecture, see `docshape_Specification.docx`.

---

## The 30-second version

```powershell
# What is in a document, and what does the recogniser make of it?
py -m docshape propose --file "C:\path\to\document.pdf"

# Same across a folder, grouped so one shape fixes many files
py -m docshape propose --dir "C:\path\to\folder" --limit 50

# What vocabularies exist, and are they coherent?
py -m docshape packs
py -m docshape shapes --pack petroleum

# Capture into a portable file (no database needed)
py -m docshape capture --in "C:\docs" --backend duckdb --db capture.duckdb
py -m docshape summary --backend duckdb --db capture.duckdb
```

There is also a UI: the **Document Recogniser** page in DataView
(`dataview/file_catalog/page_docshape.py`). Batch mode there groups
unrecognised tables by header signature, which tells you whether twenty files
share one problem or have twenty.

---

## Fixing a document that isn't recognised

### 1. Ask what it saw

```powershell
py -m docshape propose --file "C:\docs\COMPLETION_42001205760000.docx"
```

For each table it prints: the header, which cells already resolve to a field,
which nothing claims, how close every existing shape came, sample values, and
a suggested `fields` / `shapes` entry to paste.

### 2. Decide which of three things it is

**(a) An existing attribute worded differently.** The field exists; this
document calls it something else. Add an alias:

```python
"tvd": ["tvd", "true vertical depth", "true vert dep", "vert dep"],
```

**(b) An attribute the vocabulary has no concept of.** Add the field, then use
it. `cement_top` was this — "Top of Cement" was silently resolving to `top_md`,
the casing string's own depth.

**(c) A table type with no shape.** Add a shape. See below — this is the one
with a trap in it.

Rule of thumb: if `propose` says *"no field claims these"* for most of the
header, you need a shape. If it claims most of them and misses one or two, you
need aliases.

### 3. Verify

```powershell
py -m docshape packs                      # validates every pack
py -m docshape propose --file <same file> # should now name the shape
py -m docshape propose --dir <folder>     # did it fix the other files too?
```

`packs` catches the silent killer: a shape whose **required** field has no
aliases can never match, and nothing reports it — the table just returns
UNKNOWN.

---

## Writing a shape

```python
"perforations": {
    "required": ["shot_count", "top_md"],
    "optional": ["base_md", "shot_density", "gun_type", "phasing",
                 "formation", "date", "perf_status", "well_status", "uwi"],
    "min_required": 2,
    "target": "cat_well_perforation",
},
```

**`required` must DISCRIMINATE, not merely be present.** The obvious choice for
perforations is `[top_md, base_md]` — and it fails, because `formation_tops`
already claims those and wins. `shot_count` is what makes a perf table a perf
table, and no other shape requires it.

**`optional` decides ties.** Two shapes can both score 1.00 on their required
fields; the one explaining more optional columns wins. A new shape overlapping
a general one needs enough optional fields to out-explain it.

**`target: None` is a legitimate state.** The shape is recognised and
accumulated, and goes no further until a destination exists. Five petroleum
shapes are deliberately in this state: `key_value`, `cement_bond`,
`operations_npt`, `fluid_sample`, `curve_readings`.

**`columns`** maps fields to destination columns. Verify these against the real
DDL, not from memory — several of ours were wrong on the first pass.

---

## Traps that cost real time

**A word that is both a unit and a term must never be noise.** `day` was in the
petroleum noise list (as in "Days On"), which eroded the alias `"day no"` to a
bare `{no}` — and then `Bit No` resolved to a *day* field. Same class of bug as
a bare `api` alias claiming the `GR (API)` curve column.

**Generic words are resolved by the shape, not the engine.** `Status` means
well status in a header table and perf status in a perforation table. The
engine reads a cell without knowing which table it is in, so the *shape* claims
the field and its `columns` map says what it means there.

**Longest alias wins; ties break on contiguity.** `"Top of Cement (ft MD)"`
contains both `top of cement` and — scattered — `top…md`. Both are two tokens.
The contiguous phrase is what the header actually says.

**Aliases go in the pack, not the engine.** `TOC` is unambiguous in petroleum
and would be badly wrong in a legal pack. That is precisely why vocabulary is
per-domain.

**One cell holding two attributes cannot be fixed here.** `Top / Base MD (ft)`
picks one. That needs cell splitting, which nothing does yet.

---

## Corrections from the UI (overlay)

The Document Recogniser page writes to `<pack>_overlay.json` beside the pack —
never to the pack file itself. The pack is hand-written and version-controlled;
the overlay is learned per deployment and promotable after review.

- aliases **extend** the base list, never replace it
- shapes **replace** wholesale — a shape is one coherent claim
- `disabled` switches a shape off reversibly
- the 🧪 **Sandbox** toggle writes to `<pack>_sandbox.json` instead, layered on
  top of the overlay, with a promote button once a batch confirms it

When overlay entries have proved themselves, fold them into the `.py` pack and
clear the overlay. **The pack is what ships.**

---

## Adding a domain

Create `packs/<name>.py` exposing `fields` and `shapes` (everything else has
defaults). `py -m docshape packs` validates it. Nothing else changes — the
engine, readers, backends and store never learn what domain they are serving.
`legal.py` exists as a worked second example.

---

## Where things live

```
docshape/
    engine/recognise.py    matching, scoring, coercion — no domain
    packs/                 vocabularies + overlay machinery
    readers/               pdf/docx/xlsx tables; las/segy parsed natively
    backends/              duckdb, mssql, oracle, snowflake
    store.py               provenance, review columns, capture
    propose.py             "what is this document?"
```

Every module's docstring explains its own reasoning. `propose.py` and
`store.py` are the two worth reading first.

**In DataView**, `dataview/file_catalog/shape_loader.py` is the bridge:
docshape says what a table *is*, shape_loader decides where it lands *in this
database* — parent keys, UOM codes, `INVENTORY_ID`, and the handoff to
`catalog_capture.capture()`.
