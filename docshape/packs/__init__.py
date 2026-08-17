"""
docshape.packs
==============
A PACK is a domain vocabulary. It is data, not code: lists of aliases, table
shapes, and where each shape lands in a database. The engine applies the same
matching to whichever pack is loaded, so adding an industry means adding a
file here rather than forking the recogniser.

    from docshape.packs import load
    pack = load("petroleum")

A pack is any module exposing:

    fields              {canonical_field: [alias phrases]}          REQUIRED
    shapes              {shape: {required, optional, min_required,
                                 target}}                           REQUIRED
    numeric             {fields coerced to numbers}                 optional
    columns             {shape: {field: [db column candidates]}}    optional
    transforms          {shape: callable(rows) -> rows}             optional
    noise               {domain unit tokens to ignore}              optional
    char_map            {char: replacement} applied before tokens   optional
    identity_field      the field naming the subject                optional
    normalise_identity  callable                                    optional
    identity_from_name  callable(path)                              optional

validate() is worth running on a new pack: a shape whose required field has no
entry in `fields` can never match, and that failure is SILENT — the table just
comes back UNKNOWN with no indication why.
"""
from __future__ import annotations

import importlib
import os

_CACHE = {}


def _pack_mtime(mod):
    f = getattr(mod, "__file__", None)
    try:
        return os.path.getmtime(f) if f else None
    except OSError:
        return None


def load(name="petroleum"):
    """Import a pack by short name or dotted path, with light validation.

    RELOADS WHEN THE FILE CHANGES. A pack is a .py, so Python's module
    cache — and this module's own _CACHE on top of it — would otherwise
    hold the version imported when the process started, and a corrected
    vocabulary would not take effect until a full restart. That cost real
    time: a pack was edited, the app kept reading the old one, and the
    obvious conclusion was that the edit had not worked.

    Overlays and sandboxes never had this problem because they are JSON
    read from disk on every call; the base pack should behave the same.
    Checking one mtime per load is cheap next to the matching that
    follows.
    """
    cached = _CACHE.get(name)
    if cached is not None:
        mod, stamp = cached
        now = _pack_mtime(mod)
        if now is not None and stamp is not None and now == stamp:
            return mod
        try:
            mod = importlib.reload(mod)
        except Exception:
            _CACHE.pop(name, None)          # fall through to a fresh import
        else:
            return _finish(name, mod)
    mod = importlib.import_module(
        name if "." in name else f"docshape.packs.{name}")
    for attr in ("fields", "shapes"):
        if not getattr(mod, attr, None):
            raise ValueError(f"pack {name!r} has no {attr}")
    # Defaults so callers never have to test for absence.
    for attr, default in (("numeric", set()), ("columns", {}),
                          ("transforms", {}), ("noise", set()),
                          ("char_map", {}), ("identity_field", None)):
        if not hasattr(mod, attr):
            setattr(mod, attr, default)
    for attr in ("normalise_identity", "identity_from_name"):
        if not hasattr(mod, attr):
            setattr(mod, attr, lambda _v: None)
    return _finish(name, mod)


def _finish(name, mod):
    """Validate, apply defaults, cache with the file's mtime."""
    for attr in ("fields", "shapes"):
        if not getattr(mod, attr, None):
            raise ValueError(f"pack {name!r} has no {attr}")
    for attr, default in (("numeric", set()), ("columns", {}),
                          ("transforms", {}), ("noise", set()),
                          ("char_map", {}), ("identity_field", None)):
        if not hasattr(mod, attr):
            setattr(mod, attr, default)
    for attr in ("normalise_identity", "identity_from_name"):
        if not hasattr(mod, attr):
            setattr(mod, attr, lambda _v: None)
    _CACHE[name] = (mod, _pack_mtime(mod))
    return mod


def available():
    """Pack names shipped in this package.

    Uses __path__ directly rather than re-importing docshape.packs: the
    top-level package re-exports this function, and any alias there would
    shadow the module attribute and break the lookup.
    """
    import importlib
    import pkgutil
    out = []
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        # A module in this folder is not automatically a VOCABULARY —
        # overlay.py lives here and is machinery. Listing it offered a "pack"
        # that load() then refused, so ask each module whether it actually
        # declares fields and shapes.
        try:
            mod = importlib.import_module(f"docshape.packs.{m.name}")
        except Exception:
            continue
        if getattr(mod, "fields", None) and getattr(mod, "shapes", None):
            out.append(m.name)
    return sorted(out)


def validate(pack, log=print):
    """Report shapes that can never match, and columns pointing nowhere.

    Returns a list of problems; empty means the pack is coherent.
    """
    problems = []
    known = set(pack.fields)
    for shape, spec in pack.shapes.items():
        req = list(spec.get("required", ()))
        opt = list(spec.get("optional", ()))
        if not req:
            problems.append(f"{shape}: no required fields — matches nothing")
        for f in req + opt:
            if f not in known:
                problems.append(
                    f"{shape}: field {f!r} has no aliases"
                    + (" — REQUIRED, so this shape can NEVER match"
                       if f in req else ""))
        mr = spec.get("min_required", len(req))
        if mr > len(req):
            problems.append(
                f"{shape}: min_required {mr} exceeds {len(req)} required")
    for shape, colmap in getattr(pack, "columns", {}).items():
        if shape not in pack.shapes:
            problems.append(f"columns has no matching shape: {shape!r}")
        for f in colmap:
            if f not in known and not f.startswith("_"):
                problems.append(f"columns[{shape}]: unknown field {f!r}")
    for shape in getattr(pack, "transforms", {}):
        if shape not in pack.shapes:
            problems.append(f"transforms has no matching shape: {shape!r}")
    for p in problems:
        log(f"  !! {p}")
    if not problems:
        log(f"-- pack ok: {len(pack.fields)} field(s), "
            f"{len(pack.shapes)} shape(s), "
            f"{sum(1 for s in pack.shapes.values() if s.get('target'))} "
            f"with a target")
    return problems
