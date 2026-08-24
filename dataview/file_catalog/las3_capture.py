"""
las3_capture.py — LAS 3.0 data sets into the cat_* mirrors that already exist.

WHY THIS IS SMALL
-----------------
Nothing downstream needs inventing. cat_well_dir_srvy_hdr / _sta already
promote, already carry the FK gates, and dv_well_dir_srvy_sta already draws on
the map. So this is a mapping, not a feature: read the columns the file
declares, write the columns the mirror holds, and leave the rest NULL.

WHAT IT REFUSES TO INVENT
-------------------------
ns_offset, ew_offset, dls, surface_latitude/longitude are computed or surveyed
values a LAS 3.0 ~Inclinometry section does not carry. They stay NULL. A
plausible-looking dogleg severity nobody measured would plot, export and get
quoted — the failure this codebase is built around.

contractor_ba_id stays NULL too, even though the well header names a service
company: that column is an FK to dv_business_associate, and seeding an entity
parent is a DECISION, not a step. extract_core makes the same call for LAS
service companies, and for the same reason.

THE HEADER IS WRITTEN WITH THE STATIONS, ALWAYS
-----------------------------------------------
dv_well_dir_srvy_sta carries fk_srvy_sta_hdr (survey_id) -> dv_well_dir_srvy_hdr.
Staging stations without their header is the exact shape that left 153 log
curves unpromotable this morning: the child stages, the parent does not, and
promote can only refuse. Both rows come out of one function here so they cannot
drift apart.
"""
from __future__ import annotations

import os

# Mnemonic aliases, deliberately SHORT. A wide alias table is a guess about
# other people's files; these are the spellings the LAS 3.0 spec sample and
# the common vendor variants actually use. Anything unrecognised is left out
# rather than mapped onto a column it might not mean.
_MD = ("MD", "DEPT", "DEPTH", "MDEPTH")
_TVD = ("TVD", "TVDEPTH")
_AZIM = ("AZIM", "AZI", "AZ")
_INCL = ("DEVI", "INCL", "INC", "DEV")       # deviation IS inclination

SOURCE = "LAS"                                # registered in dv_r_source


def _pick(mnemonics, names):
    """Index of the first mnemonic matching `names`, else None."""
    upper = [m.upper() for m in mnemonics]
    for n in names:
        if n in upper:
            return upper.index(n)
    return None


def _pad14(uwi):
    """The catalog's UWI form: digits only, right-padded to 14.

    Same transform promote applies (build_promote_sql right-pads to 14), so a
    station's uwi compares equal to its well's on both sides of the gate. A
    mismatch here is invisible until an FK suppression clause silently stops
    matching, which CLAUDE.md records costing six weeks.
    """
    s = "".join(ch for ch in str(uwi or "") if ch.isdigit())
    return (s + "0" * 14)[:14] if s else None


def _depth_uom(las3):
    """The file's index unit, from ~Well STRT — or None.

    ~Inclinometry declares no unit on MD/TVD in the spec sample; the well
    section does (STRT .M). That is the file stating its own answer, which is
    the only kind this codebase acts on. Unregistered codes are NOT filtered
    here: promote's dv_r_uom gate holds the row and now names the reason, which
    is more useful than silently writing NULL and losing the file's own claim.
    """
    for line in las3.raw_sections.get("Well", []):
        s = line.strip()
        if s.upper().startswith("STRT"):
            dot = s.find(".")
            if dot > 0:
                unit = s[dot + 1:].split()[0] if s[dot + 1:].split() else ""
                return (unit or None)
    return None


def inclinometry_rows(las3, uwi=None, inventory_id=None, source_path=None,
                      set_name="Inclinometry"):
    """{cat_table: [row dicts]} for one ~Inclinometry set, header included.

    Returns {} when the set is absent or carries no usable station — an empty
    result is not an error, and a file with no survey simply has none.
    """
    s = las3.sets.get(set_name)
    if s is None or not s.rows:
        return {}

    i_md = _pick(s.mnemonics, _MD)
    i_tvd = _pick(s.mnemonics, _TVD)
    i_azi = _pick(s.mnemonics, _AZIM)
    i_inc = _pick(s.mnemonics, _INCL)
    if i_md is None:
        # Without a measured depth a station has no position on the wellbore.
        # Refusing the whole set is right: partial stations would draw a
        # survey that is not the one in the file.
        return {}

    w14 = _pad14(uwi or las3.well.get("UWI") or las3.well.get("API"))
    if not w14:
        return {}                             # nothing to hang the survey on

    # Stable per FILE, and readable, which matters for a demo. Two surveys for
    # one well come from two files and so get two ids; two Inclinometry sets in
    # ONE file are distinguished by set_name (~Inclinometry[1], [2]).
    base = os.path.splitext(os.path.basename(str(source_path or "")))[0]
    stem = base or (inventory_id or "")[:12] or w14
    suffix = "" if set_name == "Inclinometry" else "_" + set_name.replace(
        "Inclinometry", "").strip("[]")
    survey_id = f"INCL_{stem}{suffix}"[:80]

    uom = _depth_uom(las3)
    mds, sta = [], []
    for n, row in enumerate(s.rows, start=1):
        md = row[i_md] if i_md < len(row) else None
        if md is None:
            continue                          # a station with no depth is not one
        mds.append(md)
        sta.append({
            "uwi": w14,
            "survey_id": survey_id,
            # zero-padded so a string sort is depth order — station_id is
            # nvarchar, and '10' sorting before '2' makes a survey read as
            # nonsense in any grid that orders by it
            "station_id": f"{n:05d}",
            "md": md,
            "tvd": row[i_tvd] if i_tvd is not None and i_tvd < len(row) else None,
            "azim": row[i_azi] if i_azi is not None and i_azi < len(row) else None,
            "incl": row[i_inc] if i_inc is not None and i_inc < len(row) else None,
            "depth_ouom": uom,
            "source": SOURCE,
            "row_created_by": "DataWrangler",
        })
    if not sta:
        return {}

    hdr = {
        "uwi": w14,
        "survey_id": survey_id,
        # The file says nothing about who ran the survey or when, and the well
        # header's DATE is the LOG date, not the survey date. NULL, not a
        # guess that would read as recorded fact.
        "survey_type": None,
        "survey_date": None,
        "contractor_ba_id": None,
        "survey_top_depth": min(mds),
        "survey_base_depth": max(mds),
        "depth_ouom": uom,
        "active_ind": "Y",
        "remark": f"LAS 3.0 ~{set_name}",
        "source": SOURCE,
        "row_created_by": "DataWrangler",
    }
    return {"cat_well_dir_srvy_hdr": [hdr], "cat_well_dir_srvy_sta": sta}


def all_sets(las3, uwi=None, inventory_id=None, source_path=None):
    """Every mappable set in the file, merged into {cat_table: [rows]}.

    Inclinometry only, for now. The others (Core, Tops, Test, Perforations)
    have mirrors waiting and are the obvious next additions, but each needs its
    own column mapping read off the real files rather than assumed — which is
    the whole lesson of the last two commits.
    """
    out: dict = {}
    for name in sorted(las3.sets):
        if not name.startswith("Inclinometry"):
            continue
        part = inclinometry_rows(las3, uwi, inventory_id, source_path, name)
        for tbl, rows in part.items():
            out.setdefault(tbl, []).extend(rows)
    return out
