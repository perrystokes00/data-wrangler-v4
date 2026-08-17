# How to answer a vocabulary request

**For the customer: do not fill anything in.** Attach `vocabulary_request.json`,
attach your current `petroleum.py`, and paste this file as-is. Everything the
expert needs is already in the request.

---

You are being asked to extend a **docshape vocabulary pack** so that tables a
customer's documents contain are recognised. You have been given:

- `vocabulary_request.json` — the situation, exported from their app
- `petroleum.py` — the exact pack they are running
- optionally `baseline.json` — a snapshot of what their documents read today

Produce **a complete replacement `petroleum.py`**, plus a short plain-English
summary of what you changed and why, plus the verification command at the end
of this sheet. Do not produce a patch, a diff, or a fragment: the customer
installs a whole file.

## How the recogniser works

Read this before changing anything; most bad answers come from not knowing it.

1. Each header cell is normalised to a **token set**. Punctuation is stripped,
   filler words are removed, short slash pairs fuse (`N/S` → `ns`,
   `API / UWI` → `apiuwi`).
2. A field matches a cell when one of its **aliases' tokens is a subset** of
   the cell's tokens. `{top, md}` matches `Top (ft MD)`.
3. **The longest matching alias wins**; ties break toward the alias that
   appears as a contiguous phrase in the cell.
4. Each **shape** scores as the fraction of its `required` fields present. The
   best score above the shape's threshold wins. **Ties break on how many
   `optional` fields also matched** — this is how a specific shape beats a
   greedy one.

## The rules that matter

**Shapes**

- A shape's `required` fields must be the ones that make it **different**, not
  the ones that happen to be present. Prefer a field no other shape requires.
- Never make one shape's `required` a **superset** of another's. The other
  shape will keep winning the tie-break. Put the shared fields in `optional`
  instead — they still win ties, without the collision.
- `min_required` below `len(required)` lets vendors omit a column. Use it when
  the request shows the same table with columns missing.
- Leave `"target": None`. Where rows land is the customer's schema decision.

**Aliases**

- Aliases come from **wordings the documents actually use** — they are in the
  request under `unresolved_wordings`. Do not invent spellings.
- Never build an alias around a filler word. Filler is stripped before
  matching, so `"total depth md"` really means `{depth, md}` — far broader
  than it reads, and it will hijack other tables.
- When two wordings tie at one token, the **longer alias wins outright**. If
  `Min Value` must mean `stat_min` and `param_value` also claims `value`, add
  the two-token alias `"min value"` rather than fighting the tie.
- **An alias claimed by two fields is decided by dict order** — the field
  listed first in `fields` wins, always. If the customer needs the other one,
  you must either remove the loser's alias or reorder the dict. No UI action
  can fix this, so it must be fixed here.
- Modifier words (`min`, `max`, `avg`) belong at the **end** of the `fields`
  dict, because in `Avg Oil` the modifier must lose to the term.

**Things that are not vocabulary problems**

- A table whose header is garbled, or a document with no tables at all, is an
  **extraction** problem. Say so; do not invent aliases for garbled text.
- A label/value **pair grid** (odd columns are labels, even columns are their
  values) can be *recognised* by a shape keyed on the labels in its first row,
  but extracting it to one row needs a pivot transform. Say which you are
  providing.

## What not to do

- Do not remove or rename an existing shape or field unless the request shows
  it is actively causing a wrong match — and say so explicitly if you do.
- Do not add an alias without checking it does not already resolve elsewhere.
  `petroleum.py` is in front of you; search it.
- Do not change `numeric`, `noise`, or `char_map` unless the request shows a
  specific need. `noise` in particular is dangerous: a word that is both
  filler and a real column name must never be noise.
- Do not claim you tested anything you did not run.

## Your answer must contain

1. **The complete `petroleum.py`.**
2. **One short paragraph per change**: what table it addresses, how many of
   their documents it affects (the request says), and why the required fields
   discriminate.
3. **A prediction table** — for each group in the request, what it identifies
   as after your change. The customer will verify this, so be honest about
   anything you are unsure of.
4. **Anything you did NOT fix**, and why. Extraction problems and pair grids
   without a pivot belong here.
5. **The verification command**, exactly:

```
py -m dataview.file_catalog.vocab_check check --pack-file petroleum.py --baseline baseline.json
```

If they have no baseline, tell them to make one first:

```
py -m dataview.file_catalog.vocab_check snapshot --in <their document folder> --out baseline.json
```

The check reports FIXED and REGRESSED against their own documents and exits
non-zero on any regression. **A regression means your answer is wrong**, not
that their documents are unusual — expect to be sent the ✗ lines back.
