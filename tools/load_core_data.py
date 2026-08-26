r"""The core domain for a well: runs, plug analyses and photographs.

THIS LOADER OWNS THREE TABLES -- dv_well_core, dv_well_core_sample and
dv_well_core_photo -- and nothing else may write them. Core data is not a
flat table that happens to be in Excel: what joins its parts is a DOMAIN
fact. A plug at 5454.2 ft belongs to core run 5 because of where it sits; a
photo shot on the 14th belongs to core 6 because that is what was cut that
day. The Bulk Tabular Loader maps columns onto a target and has no way to
know either, and expressing it as column mappings would be worse than a
loader that simply says it.

The ownership matters more than the convenience. Two writers on one table
is how MIRROR_TABLES and LINEAGE came apart, and how demo_reset and
clear_catalog came to disagree about what to protect. One door.

WHAT MAKES THIS LOADABLE AT ALL. The photos carry no well in their names, and
for a while it looked like they never could be placed. They can, because the CD
says so in three places and none of it has to be guessed:

  * Core_read_me.txt  -- "Core Data Set from Well 48-X-28". The whole CD is one
    well, so every photo on it belongs to that well.
  * CORE ACCOUNTING.xls -- the core runs: number, date, top, bottom, feet cut,
    feet recovered, lithology. That is dv_well_core, written down by the people
    who cut it.
  * The file names -- SLAB PHOTOS are "5300-5312.jpg", a depth interval, with a
    "uv" suffix for the ultraviolet frames. Sandia photos are session-named,
    but every one carries an EXIF DateTimeOriginal, and a core was cut on a
    known date.

So a slab photo is placed by DEPTH and a Sandia photo by its CAPTURE DATE, and
both land on a core run that came out of the accounting sheet.

EXIF BEATS THE FILE NAME, and it matters. 157 of 168 agree; 11 are named
2004_0515 and were shot on the 14th -- filed in the next morning's session
folder. The filename is a batch label, the EXIF is when the shutter fired, so
those 11 belong to core 6 and not core 7. Trusting the name would put eleven
photos on the wrong rock and nothing on screen would say so.

WHAT IS NOT PLACED IS HELD. 24 photos are dated 2004-06-04, when no core was
cut -- a later session, probably in the lab. They are reported with that reason
and not written: a photo with an invented depth plots, exports and gets quoted,
and this table is read by people looking at rock.

    python tools/load_core_photos.py                 # plan only
    python tools/load_core_photos.py --apply
    python tools/load_core_data.py --remove --apply
"""
import argparse
import datetime
import glob
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

CD = os.path.join(
    r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler",
    "training", "Teapot_Dome", "DataSets", "Core", "CD Files")
WELL_NUM = "48-X-28"
SOURCE = "PHOTO"                       # registered in dv_r_source
SOURCE_LAB = "LAB"                     # laboratory analysis report
CREATED_BY = "CORE_PHOTO_LOADER"
SLAB_RE = re.compile(r"^(\d{3,5})\s*-\s*(\d{3,5})\s*(uv)?", re.I)
SEQ_RE = re.compile(r"(\d{3,5})(?=\.[A-Za-z]+$)")


def _uwi(engine, well_num):
    """The one well this CD belongs to, or a refusal.

    BY well_num, NOT well_name. The well is named "RMOTC" and NUMBERED
    "48-X-28"; searching the name finds nothing and reads as "the well is not
    loaded". Refusing on anything but exactly one match is the point -- two
    wells numbered the same would otherwise silently take one at random.
    """
    from sqlalchemy import text
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT uwi, well_name FROM dataview.dv_well "
            "WHERE REPLACE(REPLACE(UPPER(well_num),'-',''),' ','') = :n"),
            {"n": well_num.upper().replace("-", "").replace(" ", "")}).fetchall()
    if len(rows) != 1:
        raise SystemExit(
            "Expected exactly one well numbered %s, found %d. Load the well "
            "first, or say which uwi." % (well_num, len(rows)))
    return str(rows[0][0]), str(rows[0][1])


def _cores():
    """[{num, date, top, base, cut, rec, lith}] from CORE ACCOUNTING.xls."""
    import pandas as pd
    p = os.path.join(CD, "CORE ACCOUNTING.xls")
    if not os.path.exists(p):
        raise SystemExit("No CORE ACCOUNTING.xls at %s" % p)
    raw = pd.read_excel(p, header=None)
    out = []
    for _i, r in raw.iterrows():
        try:
            num = int(r[0])
            dt = pd.to_datetime(r[1]).date()
        except Exception:
            continue                    # title rows, header row, blanks
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        out.append({"num": num, "date": dt, "top": _f(r[2]), "base": _f(r[3]),
                    "cut": _f(r[4]), "rec": _f(r[5]),
                    "lith": (str(r[6]).strip() if r[6] is not None else None)})
    return out


# The stacked lab header is four rows deep (11-14) and the data starts at
# 16, so the columns are taken BY POSITION, from the layout printed on the
# sheet. Reading row 0 as the header -- which is what the Bulk Tabular
# Loader does -- yields "Unnamed: 0 .. Unnamed: 10" and nothing to map.
SAMPLE_COLS = {0: "core_num", 1: "sample_id", 2: "depth", 3: "stress_psi",
               4: "k_air", 5: "k_klink", 6: "porosity_pct",
               7: "grain_density", 8: "sw_pct", 9: "so_pct",
               10: "total_sat_pct"}
PP_BOOK = os.path.join("CORE P&P ANALYSES",
                       "RMOTC DOE 48x28 Well Core Data W-85011 7-27-04.xls")


def _num(v):
    """A float, or (None, note) when the lab wrote a limit instead.

    "<0.0001" IS NOT 0.0001 AND IT IS NOT ZERO. It means the plug was below
    what the permeameter could resolve, and either number is a measurement
    the lab declined to make. Stored NULL with the limit kept in the remark,
    because a tight-gas permeability of 0.0001 plots, exports and gets
    quoted -- and a NULL is visible while a fabricated number is not.
    """
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None, None
    if s.startswith("<") or s.startswith(">"):
        return None, "reported as %s" % s
    try:
        return float(s), None
    except ValueError:
        return None, None


def _samples(cores):
    """[{...}] plug analyses from the vertical and horizontal sheets.

    THE CORE NUMBER IS IN THE SHEET. Column 0 of every row is the core the
    plug was cut from -- the lab recorded it -- so the plug is placed by
    what was written down rather than by matching its depth to an interval.
    Depth is still checked against that core and a disagreement is reported
    rather than silently trusted either way.
    """
    import pandas as pd
    p = os.path.join(CD, PP_BOOK)
    if not os.path.exists(p):
        return [], ["no %s" % PP_BOOK]
    by_num = {c["num"]: c for c in cores}
    out, notes = [], []
    for sheet, orient in (("Vertical Data", "VERTICAL PLUG"),
                          ("Horizontal Data", "HORIZONTAL PLUG")):
        try:
            raw = pd.read_excel(p, sheet_name=sheet, header=None)
        except Exception as e:
            notes.append("%s unreadable: %s" % (sheet, e))
            continue
        for _i, r in raw.iterrows():
            try:
                cnum = int(float(str(r[0]).strip()))
            except (TypeError, ValueError):
                continue            # title, header and blank rows
            d, _ = _num(r[2])
            sid = str(r[1]).strip()
            if d is None or not sid or sid.lower() == "nan":
                continue
            core = by_num.get(cnum)
            if not core:
                notes.append("sample %s cites core %d, which is not in the "
                             "accounting sheet" % (sid, cnum))
                continue
            rem = []
            vals = {}
            for idx, key in SAMPLE_COLS.items():
                if key in ("core_num", "sample_id", "depth"):
                    continue
                v, note = _num(r[idx]) if idx < len(r) else (None, None)
                vals[key] = v
                if note:
                    rem.append("%s %s" % (key, note))
            # DEPTH AGAINST ITS OWN CORE. The lab said which run; if the
            # depth falls outside it, say so rather than move the plug.
            if core["top"] is not None and core["base"] is not None:
                if not (core["top"] - 1 <= d <= core["base"] + 1):
                    rem.append("depth %.1f ft is outside core %d (%.0f-%.0f)"
                               % (d, cnum, core["top"], core["base"]))
            out.append({"core": core, "sample_id": sid, "depth": d,
                        "orient": orient, "vals": vals,
                        "remark": "; ".join(rem)})
    return out, notes


def _exif_date(path):
    """When the shutter fired, or None. Never the file's mtime.

    An mtime is when the file was COPIED -- off this CD, onto this disk -- and
    is the same for every photo here. Using it would look like a date and mean
    nothing.
    """
    try:
        from PIL import Image
        import PIL.ExifTags as ET
        ex = getattr(Image.open(path), "_getexif", lambda: None)() or {}
        named = {ET.TAGS.get(k, k): v for k, v in ex.items()}
        raw = named.get("DateTimeOriginal") or named.get("DateTime")
        if raw:
            return datetime.datetime.strptime(
                str(raw)[:19], "%Y:%m:%d %H:%M:%S").date()
    except Exception:
        pass
    return None


def _image_meta(path):
    """(width, height, dpi, kb, sha1)."""
    w = h = 0
    dpi = 0.0
    try:
        from PIL import Image
        im = Image.open(path)
        w, h = im.width, im.height
        d = im.info.get("dpi")
        if d:
            dpi = float(d[0])
    except Exception:
        pass
    kb = os.path.getsize(path) / 1024.0
    sha = hashlib.sha1()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(blk)
    return w, h, dpi, kb, sha.hexdigest()


def _core_for_depth(cores, top, base):
    """The core run a depth interval belongs to -- most overlap wins."""
    best, best_ov = None, 0.0
    for c in cores:
        if c["top"] is None or c["base"] is None:
            continue
        ov = min(base, c["base"]) - max(top, c["top"])
        if ov > best_ov:
            best, best_ov = c, ov
    return best


def plan(engine):
    uwi, wname = _uwi(engine, WELL_NUM)
    cores = _cores()
    by_date = {c["date"]: c for c in cores}
    rows, held = [], []

    # -- slab photos: depth is in the name -----------------------------
    for f in sorted(glob.glob(os.path.join(CD, "SLAB PHOTOS", "*.*"))):
        nm = os.path.basename(f)
        m = SLAB_RE.match(nm)
        if not m:
            held.append((nm, "SLAB", "no depth interval in the file name"))
            continue
        top, base = float(m.group(1)), float(m.group(2))
        c = _core_for_depth(cores, top, base)
        if not c:
            held.append((nm, "SLAB",
                         "%.0f-%.0f ft is outside every cored interval"
                         % (top, base)))
            continue
        rows.append({
            "f": f, "core": c, "top": top, "base": base,
            "photo_type": "SLAB",
            "lighting": "UV" if m.group(3) else "WHITE",
            # NOT RECORDED, NOT INVENTED. Slab frames carry no tray number and
            # no EXIF; 0 means "not stated" and the remark says which date the
            # row actually carries.
            "tray": 0,
            "date": _exif_date(f) or c["date"],
            "date_src": "EXIF" if _exif_date(f) else "core date (no EXIF)",
        })

    # -- Sandia photos: the capture date is in the file ------------------
    for f in sorted(glob.glob(os.path.join(CD, "Sandia Core Photos", "*.*"))):
        nm = os.path.basename(f)
        d = _exif_date(f)
        if not d:
            held.append((nm, "WHOLE CORE", "no EXIF capture date"))
            continue
        c = by_date.get(d)
        if not c:
            held.append((nm, "WHOLE CORE",
                         "no core was cut on %s" % d.isoformat()))
            continue
        seq = SEQ_RE.search(nm)
        rows.append({
            "f": f, "core": c, "top": c["top"], "base": c["base"],
            "photo_type": "WHOLE CORE", "lighting": "WHITE",
            "tray": int(seq.group(1)) if seq else 0,
            "date": d, "date_src": "EXIF",
        })
    samples, snotes = _samples(cores)
    for s in snotes:
        held.append(("core analyses", "SAMPLE", s))
    return uwi, wname, cores, rows, held, samples


def write(engine, uwi, cores, rows, samples=()):
    from sqlalchemy import text
    core_id = lambda n: "%s-C%02d" % (WELL_NUM, n)

    # COUNTED BEFORE AND AFTER, NOT FROM rowcount. An "IF NOT EXISTS ...
    # INSERT" that SKIPS does not report 0: the core and photo statements
    # came back -1 each, so a re-run announced "-12 core run(s)" and
    # "-184 photo(s)" -- which reads as a deletion -- and the sample
    # statement returned positive garbage that summed to 1,904 against a
    # table holding 112 rows. Neither number was survivable, and clamping
    # them would only have hidden the second one. Two counts and a
    # subtraction cannot be wrong about what landed.
    _TABLES = ("dv_well_core", "dv_well_core_photo", "dv_well_core_sample")

    def _counts(cx):
        return {t: cx.execute(text(
            "SELECT COUNT(*) FROM dataview.[%s] WHERE uwi = :u" % t),
            {"u": uwi}).scalar() for t in _TABLES}
    with engine.begin() as c:
        _before = _counts(c)
        for cr in cores:
            # A SKIPPED "IF NOT EXISTS" RETURNS -1, NOT 0. Summing it made a
            # re-run report "-12 core run(s)": a count that goes negative is
            # a report that lies, and it reads as a deletion.
            c.execute(text("""
                IF NOT EXISTS (SELECT 1 FROM dataview.dv_well_core
                               WHERE uwi = :u AND core_id = :cid)
                INSERT INTO dataview.dv_well_core
                      (uwi, core_id, core_num, top_depth, base_depth,
                       depth_ouom, core_length, recovery_length, recovery_pct,
                       length_ouom, core_date, strat_unit_name, active_ind,
                       source, row_created_by)
                VALUES (:u, :cid, :num, :top, :base, 'FT', :cut, :rec, :pct,
                        'FT', :dt, :lith, 'Y', :src, :by)"""),
                {"u": uwi, "cid": core_id(cr["num"]), "num": cr["num"],
                 "top": cr["top"], "base": cr["base"], "cut": cr["cut"],
                 "rec": cr["rec"],
                 "pct": (100.0 * cr["rec"] / cr["cut"]
                         if cr["cut"] and cr["rec"] else None),
                 "dt": cr["date"], "lith": cr["lith"],
                 "src": SOURCE, "by": CREATED_BY})
        for r in rows:
            w, h, dpi, kb, sha = _image_meta(r["f"])
            nm = os.path.basename(r["f"])
            # PHOTO_ID IS THE HASH, not the file name. Two CDs can carry the
            # same "0001.JPG"; the same bytes are the same photograph however
            # the folder is renamed, and a re-run must not duplicate rows.
            c.execute(text("""
                IF NOT EXISTS (SELECT 1 FROM dataview.dv_well_core_photo
                               WHERE uwi = :u AND photo_id = :pid)
                INSERT INTO dataview.dv_well_core_photo
                      (uwi, core_id, photo_id, photo_type, lighting,
                       top_depth, base_depth, depth_ouom, tray_num, photo_date,
                       file_path, file_name, file_ext, file_size_kb, file_hash,
                       resolution_dpi, width_px, height_px, active_ind,
                       remark, source, row_created_by)
                VALUES (:u, :cid, :pid, :pt, :lt, :top, :base, 'FT', :tray,
                        :dt, :fp, :fn, :ext, :kb, :sha, :dpi, :w, :h, 'Y',
                        :rm, :src, :by)"""),
                {"u": uwi, "cid": core_id(r["core"]["num"]), "pid": sha[:40],
                 "pt": r["photo_type"], "lt": r["lighting"],
                 "top": r["top"], "base": r["base"], "tray": r["tray"],
                 "dt": r["date"], "fp": r["f"], "fn": nm,
                 "ext": os.path.splitext(nm)[1].lower(), "kb": kb, "sha": sha,
                 "dpi": dpi, "w": float(w), "h": float(h),
                 "rm": "core %d %s; photo date from %s"
                       % (r["core"]["num"], r["core"]["lith"] or "",
                          r["date_src"]),
                 "src": SOURCE, "by": CREATED_BY})
        for s in samples:
            v = s["vals"]
            def _pc(x):
                # PERCENT IN, FRACTION STORED. The column is *_frac and the
                # sheet prints percent; loading 6.9 into a fraction would
                # read as 690% porosity and still plot.
                return None if x is None else x / 100.0
            c.execute(text("""
                IF NOT EXISTS (SELECT 1 FROM dataview.dv_well_core_sample
                               WHERE uwi = :u AND core_id = :cid
                                 AND sample_id = :sid)
                INSERT INTO dataview.dv_well_core_sample
                      (uwi, core_id, sample_id, sample_type, sample_depth,
                       depth_ouom, porosity_frac, permeability_air_md,
                       permeability_klinkenberg_md, grain_density_g_cc,
                       water_saturation_frac, oil_saturation_frac,
                       active_ind, remark, source, row_created_by)
                VALUES (:u, :cid, :sid, :st, :dep, 'FT', :phi, :ka, :kk,
                        :rho, :sw, :so, 'Y', :rm, :src, :by)"""),
                {"u": uwi, "cid": core_id(s["core"]["num"]),
                 "sid": s["sample_id"], "st": s["orient"],
                 "dep": s["depth"], "phi": _pc(v.get("porosity_pct")),
                 "ka": v.get("k_air"), "kk": v.get("k_klink"),
                 "rho": v.get("grain_density"),
                 "sw": _pc(v.get("sw_pct")), "so": _pc(v.get("so_pct")),
                 "rm": s["remark"] or None,
                 "src": SOURCE_LAB, "by": CREATED_BY})
        # The rollups the core table keeps for the photos beneath it.
        c.execute(text("""
            UPDATE k SET photo_count = x.n,
                         has_uv_photos = CASE WHEN x.uv > 0 THEN 'Y' ELSE 'N' END,
                         photo_folder_path = :cd,
                         row_changed_by = :by, row_changed_date = GETDATE()
              FROM dataview.dv_well_core k
              CROSS APPLY (SELECT COUNT(*) n,
                                  SUM(CASE WHEN p.lighting = 'UV' THEN 1 ELSE 0 END) uv
                             FROM dataview.dv_well_core_photo p
                            WHERE p.uwi = k.uwi AND p.core_id = k.core_id) x
             WHERE k.uwi = :u"""), {"u": uwi, "cd": CD, "by": CREATED_BY})
        _after = _counts(c)
    return (_after["dv_well_core"] - _before["dv_well_core"],
            _after["dv_well_core_photo"] - _before["dv_well_core_photo"],
            _after["dv_well_core_sample"] - _before["dv_well_core_sample"])


def remove(engine, uwi):
    from sqlalchemy import text
    with engine.begin() as c:
        p = c.execute(text("DELETE FROM dataview.dv_well_core_photo "
                           "WHERE uwi = :u AND row_created_by = :by"),
                      {"u": uwi, "by": CREATED_BY}).rowcount
        s = c.execute(text("DELETE FROM dataview.dv_well_core_sample "
                           "WHERE uwi = :u AND row_created_by = :by"),
                      {"u": uwi, "by": CREATED_BY}).rowcount
        # LAST, because the photos and the samples both hang off it.
        k = c.execute(text("DELETE FROM dataview.dv_well_core "
                           "WHERE uwi = :u AND row_created_by = :by"),
                      {"u": uwi, "by": CREATED_BY}).rowcount
    return p, s, k


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Load RMOTC core photos onto their well and core run.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    if a.remove:
        uwi, _ = _uwi(engine, WELL_NUM)
        if not a.apply:
            print("Would delete this loader's rows for %s. Add --apply." % uwi)
            return 0
        p, s, k = remove(engine, uwi)
        print("Deleted %d photo(s), %d plug analysis(es) and %d core run(s)."
              % (p, s, k))
        return 0

    uwi, wname, cores, rows, held, samples = plan(engine)
    print("Well        : %s  (%s %s)" % (uwi, wname, WELL_NUM))
    print("Core runs   : %d from CORE ACCOUNTING.xls" % len(cores))
    print("Photos      : %d placed, %d held" % (len(rows), len(held)))
    print("Plug tests  : %d from the P&P workbook" % len(samples))
    print()
    from collections import Counter
    for k, n in sorted(Counter(
            "core %02d  %s" % (r["core"]["num"], r["photo_type"])
            for r in rows).items()):
        print("   %-22s %3d" % (k, n))
    if held:
        print()
        print("HELD -- not written, and why:")
        for why, n in sorted(Counter(h[2] for h in held).items()):
            print("   %-46s %3d" % (why, n))
    if not a.apply:
        print("\nPLAN ONLY -- nothing written. Re-run with --apply.")
        return 0
    n_core, n_photo, n_samp = write(engine, uwi, cores, rows, samples)
    print("\nWrote %d core run(s), %d photo(s), %d plug analysis(es)."
          % (n_core, n_photo, n_samp))
    print("Undo with:  python tools/load_core_data.py --remove --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
