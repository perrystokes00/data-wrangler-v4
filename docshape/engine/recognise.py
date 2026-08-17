"""
docshape.engine.recognise
=========================
Identify a table by WHAT ITS COLUMNS ARE. Domain-neutral: this module has no
idea what a UWI, a formation or a contract party is. It is given a VOCABULARY
(a pack) and applies the same matching rules to whatever that pack describes.

THE MECHANISM
-------------
    1. Normalise each header cell to a TOKEN SET, with unit and noise tokens
       stripped.                       "Top (ft MD)" -> {top, md}
    2. A field matches when an alias's tokens are a SUBSET of the header's.
       {top, md} <= {top, ft, md}      Longest alias wins.
    3. Score each SHAPE by the fraction of its required fields present.
    4. Best score above the shape's threshold wins; ties break on optional
       hits, so a specific shape beats a greedy one on the same table.

Position, column order and section titles never enter into it. That is the
whole point: vendors do not agree on names, and enumerating every layout is a
losing race, while describing what a table IS is not.

WHY THE VOCABULARY IS SEPARATE
------------------------------
Everything above is arithmetic on token sets. What makes it a PETROLEUM
recogniser is a list of aliases and shapes — and what would make it a LEGAL or
a medical one is a different list. Splitting them means a new domain is a data
file, not a fork, and a bug fixed in the matcher is fixed for every domain at
once.

IT DOES NOT GUESS. An unmatched table comes back UNKNOWN with its headers
intact for a review queue, and columns no field claimed are preserved per row
under `_extra` rather than dropped. Wrong data is worse than absent data.
"""
from __future__ import annotations

import re

# Tokens that carry no meaning for identification anywhere: ordinals, filler,
# aggregation words. A pack adds its own domain units (ft, psi, bbl for
# petroleum; per, ea, usd for something else) via Pack.noise.
# min / max / average / avg were here once and it was a mistake: they are the
# COLUMN NAMES of every statistics table ("Parameter | Min | Max | Average"),
# which then tokenised to nothing and could never be taught. The day/days
# rule: a word that is both filler and a meaningful term is never noise.
# "total" stays — well_header's md→final_td mapping is built on its erosion.
BASE_NOISE = {
    "per", "of", "the", "a", "an", "and", "total",
    "sum", "count", "pct", "percent", "value2",
}

# Bookkeeping keys that never become columns.
INTERNAL_KEYS = {"_shape", "_extra", "_file", "_table", "_score",
                 "_hdr", "_cells"}


class Recogniser:
    """Applies a pack's vocabulary to table headers.

    Holds no state beyond the pack, so one instance per pack can be shared.
    """

    def __init__(self, pack):
        self.pack = pack
        self._noise = BASE_NOISE | set(getattr(pack, "noise", ()) or ())
        self._chars = dict(getattr(pack, "char_map", {}) or {})

    # -- tokenising -------------------------------------------------------- #
    def tokens(self, cell) -> frozenset:
        """Header cell -> comparable token set.

        Two rules earned by real documents:
        * a pack may map characters that survive no punctuation strip (the
          petrophysics porosity symbol Ø would otherwise collapse to a bare
          "e" and match nothing);
        * short slash pairs are ONE token — N/S, E/W, N/G, 48/64 — because
          splitting them loses the term entirely ({n,s} never matches {ns}).
        """
        s = str(cell or "").lower()
        for a, b in self._chars.items():
            s = s.replace(a, b)
        s = re.sub(r"\b([a-z0-9]{1,3})\s*/\s*([a-z0-9]{1,3})\b", r"\1\2", s)
        s = re.sub(r"[^a-z0-9 ]+", " ", s)
        return frozenset(p for p in s.split() if p and p not in self._noise)

    def token_list(self, cell):
        """Tokens in ORDER, for phrase tests. tokens() gives the set."""
        s = str(cell or "").lower()
        for a, b in self._chars.items():
            s = s.replace(a, b)
        s = re.sub(r"\b([a-z0-9]{1,3})\s*/\s*([a-z0-9]{1,3})\b", r"\1\2", s)
        s = re.sub(r"[^a-z0-9 ]+", " ", s)
        return [p for p in s.split() if p and p not in self._noise]

    @staticmethod
    def _contiguous(alias_words, header_words):
        """Does the alias appear as an unbroken run in the header?"""
        n = len(alias_words)
        if not n or n > len(header_words):
            return False
        return any(header_words[i:i + n] == alias_words
                   for i in range(len(header_words) - n + 1))

    def field_for(self, cell):
        """Canonical field for one header cell, or None.

        Prefers the LONGEST matching alias, so "Top MD" beats the looser "Top"
        and "net pay" isn't swallowed by "net".

        Ties are broken on CONTIGUITY, which is what distinguishes a phrase
        from a coincidence. "Top of Cement (ft MD)" contains both "top of
        cement" and — scattered across the whole cell — "top…md". Both are two
        tokens after noise removal, so length alone leaves dictionary order to
        decide, and a cement top silently became a casing top depth. The
        contiguous phrase is the one the header actually says.
        """
        t = self.tokens(cell)
        if not t:
            return None
        words = self.token_list(cell)
        best, best_key = None, (0, 0)
        for field, aliases in self.pack.fields.items():
            for a in aliases:
                at = self.tokens(a)
                if not at or not at <= t:
                    continue
                key = (len(at), 1 if self._contiguous(self.token_list(a), words) else 0)
                if key > best_key:
                    best, best_key = field, key
        return best

    def header_fields(self, header):
        return [(i, c, self.field_for(c)) for i, c in enumerate(header)]

    # -- identification ---------------------------------------------------- #
    def identify(self, header):
        """(shape_name, score, {field: column_index}); shape None if unmatched."""
        found = {}
        for i, _c, f in self.header_fields(header):
            if f and f not in found:          # first column wins a duplicate
                found[f] = i

        best, best_score, best_opt = None, 0.0, -1
        for name, spec in self.pack.shapes.items():
            req = spec["required"]
            hits = [f for f in req if f in found]
            if len(hits) < spec.get("min_required", len(req)):
                continue
            score = len(hits) / len(req)
            opt = sum(1 for f in spec.get("optional", ()) if f in found)
            # The tie-break is what stops a greedy shape claiming a specific
            # table: two shapes can both score 1.00 on their required fields,
            # and the one that also explains more OPTIONAL columns is the
            # better description.
            if (score, opt) > (best_score, best_opt):
                best, best_score, best_opt = name, score, opt
        if not best:
            return None, 0.0, {}
        spec = self.pack.shapes[best]
        keep = set(spec["required"]) | set(spec.get("optional", ()))
        return best, best_score, {f: i for f, i in found.items() if f in keep}

    # -- mapping ----------------------------------------------------------- #
    def map_rows(self, shape, colmap, header, data_rows):
        """Data rows -> [{field: value}], with unmapped columns kept.

        Each row also carries `_hdr` and `_cells` (internal keys, never
        columns): the header and the raw cell list. A transform that
        rearranges STRUCTURE — the pair-grid pivot — needs positional
        truth, because a value cell that happens to resolve to a field
        ("STATE ALPHA 12H" -> state) corrupts the colmap view of which
        column is which."""
        out = []
        mapped = set(colmap.values())
        hdr = [str(c) for c in header]
        for r in data_rows:
            rec = {f: (r[i] if i < len(r) else "") for f, i in colmap.items()}
            extra = {str(c): r[i] for i, c in enumerate(header)
                     if i not in mapped and i < len(r) and str(r[i]).strip()}
            if extra:
                rec["_extra"] = extra
            rec["_shape"] = shape
            rec["_hdr"] = hdr
            rec["_cells"] = [str(v) for v in r]
            out.append(rec)
        return out

    def read_table(self, header, data_rows):
        """One call: identify, then map. Describes the whole table."""
        shape, score, colmap = self.identify(header)
        return {
            "shape": shape or "UNKNOWN",
            "score": round(score, 2),
            "columns": {f: header[i] for f, i in colmap.items()},
            "unmapped": [str(c) for i, c in enumerate(header)
                         if i not in set(colmap.values())],
            "target": self.pack.shapes.get(shape, {}).get("target"),
            "rows": self.map_rows(shape, colmap, header, data_rows)
            if shape else [],
            "row_count": len(data_rows),
        }

    # -- explanation ------------------------------------------------------- #
    def explain(self, header, log=print):
        """Show the decision. Run this when a table is misread."""
        log(f"header: {list(header)}")
        log("\n  cell -> field")
        for _i, c, f in self.header_fields(header):
            log(f"    {str(c)[:34]:36} {f or '—'}")
        found = {f for _i, _c, f in self.header_fields(header) if f}
        log("\n  shape scores")
        for name, spec in self.pack.shapes.items():
            req = spec["required"]
            hits = [f for f in req if f in found]
            opt = [f for f in spec.get("optional", ()) if f in found]
            ok = len(hits) >= spec.get("min_required", len(req))
            log(f"    {name:22} {len(hits)}/{len(req)} required"
                + (f" + {len(opt)} optional" if opt else "")
                + ("   MATCH" if ok else ""))
        shape, score, colmap = self.identify(header)
        log(f"\n  -> {shape or 'UNKNOWN'} (score {score:.2f})")
        for f, i in sorted(colmap.items()):
            log(f"       {f:16} <- {header[i]}")


def to_number(v):
    """'6,855' -> 6855.0 · '20\"' -> 20.0 · '48/64' -> None · 'K-55' -> 55.0.

    None rather than 0 on anything unparseable: a zero depth is a measurement,
    a null depth is a gap, and conflating them puts bad data in a table
    quietly. A number glued to the back of a letter is a code, not a negative —
    "K-55" is a casing grade, not minus fifty-five.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip()
    if not t or "/" in t:                 # a fraction is not a scalar
        return None
    t = t.replace(",", "")
    m = re.search(r"(?:^|[^A-Za-z0-9])(-?\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1))
    m = re.match(r"(\d+(?:\.\d+)?)", t)
    return float(m.group(1)) if m else None
