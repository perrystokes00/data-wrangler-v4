"""
docshape.packs.overlay
======================
Corrections as data, layered over a hand-written pack.

WHY NOT EDIT THE PACK
---------------------
A pack is Python. A UI that edited it would be generating source code, and the
file would have two authors — a person writing considered vocabulary and a
program appending whatever a reviewer clicked. That ends badly in three
separate ways: merge conflicts, a review process that has to read diffs of
generated code, and no way for a customer deployment to carry its own
vocabulary without forking yours.

So the pack stays the base, and corrections go to a JSON overlay beside it.
Same pattern as the migration app's column_synonyms.json, for the same reason:
the layer a machine writes should be separable from the layer a person wrote.

    base pack  (petroleum.py)      considered, version-controlled, yours
    overlay    (petroleum.json)    learned, per-deployment, promotable

MERGE RULES, and they are deliberate
------------------------------------
    fields      overlay aliases EXTEND the base list, never replace it.
                A correction adds a wording; it does not discard the wordings
                that already worked.
    shapes      overlay REPLACES a base shape of the same name, or adds a new
                one. A shape is a single coherent claim — half-overriding it
                would produce a definition nobody wrote.
    columns     per-shape, replaces.
    numeric     union.
    noise       union.
    disabled    a list of shape names to switch OFF without deleting them,
                for when a shape turns out to be claiming tables it shouldn't.

Every correction records who made it and when, so an overlay can be reviewed
before its contents are promoted into the base pack.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

OVERLAY_VERSION = 1


def default_path(pack_name, directory=None):
    """Where a pack's overlay lives: beside the pack unless told otherwise."""
    if directory:
        return os.path.join(directory, f"{pack_name}_overlay.json")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, f"{pack_name}_overlay.json")


def empty(pack_name):
    return {
        "pack": pack_name, "version": OVERLAY_VERSION,
        "fields": {}, "shapes": {}, "columns": {}, "numeric": [],
        "noise": [], "disabled": [], "log": [],
    }


def load_overlay(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for k, v in empty(data.get("pack", "")).items():
        data.setdefault(k, v)
    return data


def save_overlay(overlay, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(overlay, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)          # atomic: a crash never leaves a half file
    return path


# --------------------------------------------------------------------------- #
# Recording corrections
# --------------------------------------------------------------------------- #
def add_alias(overlay, field, alias, by=None, note=None):
    """Teach the pack that a header wording means a field.

    The alias is stored lower-cased and whitespace-normalised because that is
    how the tokeniser will see it; storing the raw cell would work by accident
    and confuse anyone reading the file.
    """
    alias = " ".join(str(alias or "").split()).lower()
    if not alias:
        return overlay
    lst = overlay["fields"].setdefault(field, [])
    if alias not in lst:
        lst.append(alias)
        overlay["log"].append({
            "action": "alias", "field": field, "alias": alias,
            "by": by, "at": datetime.now().isoformat(timespec="seconds"),
            "note": note})
    return overlay


def add_shape(overlay, name, required, optional=None, target=None,
              min_required=2, columns=None, by=None, note=None):
    overlay["shapes"][name] = {
        "required": list(required),
        "optional": list(optional or []),
        "min_required": min_required,
        "target": target,
    }
    if columns:
        overlay["columns"][name] = dict(columns)
    overlay["log"].append({
        "action": "shape", "shape": name, "required": list(required),
        "target": target, "by": by,
        "at": datetime.now().isoformat(timespec="seconds"), "note": note})
    return overlay


def set_numeric(overlay, fields, by=None):
    for f in fields:
        if f not in overlay["numeric"]:
            overlay["numeric"].append(f)
    return overlay


def disable_shape(overlay, name, by=None, note=None):
    """Switch a shape off without deleting it — reversible, and the reason is
    recorded. Used when a shape turns out to claim tables it shouldn't."""
    if name not in overlay["disabled"]:
        overlay["disabled"].append(name)
        overlay["log"].append({
            "action": "disable", "shape": name, "by": by,
            "at": datetime.now().isoformat(timespec="seconds"), "note": note})
    return overlay


def enable_shape(overlay, name):
    if name in overlay["disabled"]:
        overlay["disabled"].remove(name)
    return overlay


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #
class _Overlaid:
    """A pack with one or more overlays merged over it, in order.

    A shim rather than a mutated module: the base is imported once and shared,
    and mutating it would leak one deployment's corrections into every other
    consumer in the same process.

    Layers apply left to right, so a SANDBOX sees everything the overlay has
    already established and can be evaluated against it — testing a new
    wording against a stale vocabulary would prove nothing.
    """

    def __init__(self, base, *overlays):
        self._base = base
        self._overlays = [o for o in overlays if o]

        self.fields = {k: list(v) for k, v in base.fields.items()}
        self.shapes = dict(base.shapes)
        self.columns = dict(getattr(base, "columns", {}))
        self.numeric = set(getattr(base, "numeric", set()))
        self.noise = set(getattr(base, "noise", set()))

        for ov in self._overlays:
            for field, aliases in (ov.get("fields") or {}).items():
                have = self.fields.setdefault(field, [])
                for a in aliases:
                    if a not in have:
                        have.append(a)
            self.shapes.update(ov.get("shapes") or {})
            for name in (ov.get("disabled") or []):
                self.shapes.pop(name, None)
            self.columns.update(ov.get("columns") or {})
            self.numeric |= set(ov.get("numeric") or [])
            self.noise |= set(ov.get("noise") or [])

    def __getattr__(self, item):
        # transforms, char_map, identity helpers and anything else the base
        # defines pass straight through
        return getattr(self._base, item)


def apply_overlay(base_pack, *overlays):
    live = [o for o in overlays if o]
    return _Overlaid(base_pack, *live) if live else base_pack


def sandbox_path(pack_name, directory=None):
    """Where speculative work lives, beside the overlay it may be promoted to."""
    if directory:
        return os.path.join(directory, f"{pack_name}_sandbox.json")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, f"{pack_name}_sandbox.json")


def load_with_overlay(pack_name="petroleum", path=None, directory=None):
    """The normal entry point: base pack plus its overlay, if one exists."""
    from docshape.packs import load
    base = load(pack_name)
    p = path or default_path(pack_name, directory)
    ov = load_overlay(p)
    return apply_overlay(base, ov), ov, p


def load_layered(pack_name="petroleum", directory=None, use_sandbox=False):
    """Base + overlay, and the sandbox on top when asked.

    Returns (pack, overlay, overlay_path, sandbox, sandbox_path). The caller
    writes to the sandbox when experimenting and to the overlay when it has
    decided. Both are always LOADED so a sandbox change can be judged against
    the established vocabulary rather than against the bare pack.
    """
    from docshape.packs import load
    base = load(pack_name)
    op = default_path(pack_name, directory)
    sp = sandbox_path(pack_name, directory)
    ov = load_overlay(op)
    sb = load_overlay(sp) if use_sandbox else None
    pack = apply_overlay(base, ov, sb)
    return pack, ov, op, sb, sp


def promote_sandbox(sandbox, overlay):
    """Fold sandbox entries into the overlay. Sandbox is left for the caller
    to clear, so a failed write never loses the work."""
    if not sandbox:
        return overlay, 0
    n = 0
    for field, aliases in (sandbox.get("fields") or {}).items():
        have = overlay["fields"].setdefault(field, [])
        for a in aliases:
            if a not in have:
                have.append(a)
                n += 1
    for name, spec in (sandbox.get("shapes") or {}).items():
        overlay["shapes"][name] = spec
        n += 1
    for name, cols in (sandbox.get("columns") or {}).items():
        overlay["columns"][name] = cols
    for f in (sandbox.get("numeric") or []):
        if f not in overlay["numeric"]:
            overlay["numeric"].append(f)
    for name in (sandbox.get("disabled") or []):
        if name not in overlay["disabled"]:
            overlay["disabled"].append(name)
            n += 1
    overlay["log"].extend(sandbox.get("log") or [])
    overlay["log"].append({
        "action": "promote_sandbox", "entries": n,
        "at": datetime.now().isoformat(timespec="seconds")})
    return overlay, n


def promote_summary(overlay, log=print):
    """What an overlay contains, for review before folding it into the pack."""
    log(f"overlay for pack '{overlay.get('pack')}' "
        f"({len(overlay.get('log', []))} recorded change(s))")
    if overlay.get("fields"):
        log(f"\n  aliases added to {len(overlay['fields'])} field(s):")
        for f, al in sorted(overlay["fields"].items()):
            log(f"     {f:22} {al}")
    if overlay.get("shapes"):
        log(f"\n  {len(overlay['shapes'])} shape(s):")
        for n, spec in sorted(overlay["shapes"].items()):
            log(f"     {n:22} requires {spec['required']}"
                f"  -> {spec.get('target') or '(no target)'}")
    if overlay.get("disabled"):
        log(f"\n  disabled: {overlay['disabled']}")
    return overlay
