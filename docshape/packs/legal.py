"""
docshape.packs.legal
====================
A LEGAL vocabulary — contracts, leases, agreements. It exists to prove the
engine is domain-neutral: nothing in docshape/engine changes to support it,
because the engine never learned what a well was in the first place.

This is a starting sketch, not a finished pack. The shapes below are the
tables that recur across commercial agreements — payment schedules, party
lists, obligations, deliverables, fee rates — and the aliases are what those
columns get called. A working deployment would grow it the same way the
petroleum pack grew: run it against real documents, and when a header isn't
recognised, add the word.

`target` is None throughout: there is no legal schema to load into yet, so
every shape is recognised and accumulated rather than promoted. That is a
deliberate state, not an omission — the capture store keeps the rows, and
where they eventually land is a decision for whoever has the database.
"""
from __future__ import annotations

import re

noise = {"usd", "gbp", "eur", "cad", "ea", "each", "no", "num", "ref",
         "incl", "excl", "net", "gross", "vat", "tax"}
char_map = {}

fields = {
    # parties and identity
    "party_name":     ["party", "party name", "name", "entity", "counterparty",
                       "company", "legal name"],
    "party_role":     ["role", "capacity", "party type", "designation"],
    "matter_id":      ["matter", "matter id", "matter no", "contract no",
                       "contract id", "agreement no", "reference", "our ref"],
    "jurisdiction":   ["jurisdiction", "governing law", "law", "venue",
                       "forum"],
    "address":        ["address", "registered address", "principal place"],
    # dates and term
    "effective_date": ["effective date", "commencement", "start date",
                       "date of agreement"],
    "expiry_date":    ["expiry", "expiry date", "end date", "termination date",
                       "expiration"],
    "due_date":       ["due date", "due", "payment date", "date due",
                       "deadline", "by date"],
    "term":           ["term", "duration", "period", "notice period"],
    "signed_date":    ["signed", "date signed", "execution date"],
    # money
    "amount":         ["amount", "value", "sum", "consideration", "fee",
                       "price", "charge", "total due"],
    "currency":       ["currency", "ccy"],
    "rate":           ["rate", "hourly rate", "unit price", "unit rate",
                       "day rate"],
    "quantity":       ["quantity", "qty", "units", "hours"],
    "tax_rate":       ["vat rate", "tax rate", "sales tax"],
    "invoice_no":     ["invoice", "invoice no", "invoice number"],
    "instalment":     ["instalment", "installment", "payment no", "milestone"],
    # obligations and clauses
    "clause_ref":     ["clause", "clause no", "section", "article",
                       "paragraph", "provision"],
    "clause_type":    ["clause type", "category", "type", "heading"],
    "obligation":     ["obligation", "requirement", "covenant", "undertaking",
                       "commitment"],
    "responsible":    ["responsible", "owner", "accountable", "assigned to",
                       "obligor"],
    "deliverable":    ["deliverable", "work product", "output", "service",
                       "scope item"],
    "status":         ["status", "state", "progress"],
    "description":    ["description", "details", "particulars", "narrative",
                       "summary"],
    "risk":           ["risk", "risk level", "severity", "exposure"],
    "penalty":        ["penalty", "liquidated damages", "late fee"],
}

shapes = {
    "payment_schedule": {
        "required": ["due_date", "amount"],
        "optional": ["instalment", "description", "currency", "invoice_no",
                     "status", "matter_id"],
        "min_required": 2, "target": None,
    },
    "parties": {
        "required": ["party_name", "party_role"],
        "optional": ["address", "jurisdiction", "matter_id", "signed_date"],
        "min_required": 2, "target": None,
    },
    "obligations": {
        "required": ["clause_ref", "obligation"],
        "optional": ["responsible", "due_date", "status", "clause_type",
                     "penalty", "risk"],
        "min_required": 2, "target": None,
    },
    "deliverables": {
        "required": ["deliverable", "due_date"],
        "optional": ["responsible", "status", "amount", "description"],
        "min_required": 2, "target": None,
    },
    "fee_schedule": {
        "required": ["rate", "deliverable"],
        "optional": ["quantity", "currency", "amount", "description"],
        "min_required": 2, "target": None,
    },
    "clause_index": {
        "required": ["clause_ref", "clause_type"],
        "optional": ["description", "risk", "status"],
        "min_required": 2, "target": None,
    },
}

numeric = {"amount", "rate", "quantity", "tax_rate", "instalment"}
columns = {}
transforms = {}

identity_field = "matter_id"


def normalise_identity(v):
    """Matter/contract reference, upper-cased with separators stripped."""
    s = re.sub(r"[^A-Za-z0-9]+", "", str(v or "")).upper()
    return s or None


def identity_from_name(path):
    """A contract-looking reference in the file name, e.g. MSA-2024-0117."""
    import os
    m = re.search(r"\b([A-Z]{2,6}[-_]\d{2,4}[-_]?\d{0,6})\b",
                  os.path.basename(str(path or "")).upper())
    return normalise_identity(m.group(1)) if m else None

_ID_PATTERNS = [
    re.compile(r"(?:matter|contract|agreement|our\s+ref(?:erence)?)\s*"
               r"(?:no\.?|number|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-_/]{3,24})", re.I),
    re.compile(r"\b([A-Z]{2,6}[-_]\d{2,4}[-_]\d{2,6})\b"),
]


def identity_from_text(text):
    """Find a matter or contract reference in the document's text."""
    if not text:
        return None
    for pat in _ID_PATTERNS:
        m = pat.search(text)
        if m:
            got = normalise_identity(m.group(1))
            if got and len(got) >= 4:
                return got
    return None

def subject_from_text(text):
    """The agreement's title, when no reference number is stated."""
    if not text:
        return None
    m = re.search(r"^\s*(.{6,90}?(?:AGREEMENT|CONTRACT|LEASE|MSA|NDA|"
                  r"STATEMENT OF WORK).{0,40})\s*$", text, re.I | re.M)
    return " ".join(m.group(1).split())[:120] if m else None

