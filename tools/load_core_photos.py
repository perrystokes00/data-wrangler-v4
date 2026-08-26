r"""Load the RMOTC core photos onto their well, their core run and their depth.

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
    python tools/load_core_photos.py --remove --apply
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
    return uwi, wname, cores, rows, held


def write(engine, uwi, cores, rows):
    from sqlalchemy import text
    core_id = lambda n: "%s-C%02d" % (WELL_NUM, n)
    n_core = n_photo = 0
    with engine.begin() as c:
        for cr in cores:
            n_core += c.execute(text("""
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
                 "src": SOURCE, "by": CREATED_BY}).rowcount
        for r in rows:
            w, h, dpi, kb, sha = _image_meta(r["f"])
            nm = os.path.basename(r["f"])
            # PHOTO_ID IS THE HASH, not the file name. Two CDs can carry the
            # same "0001.JPG"; the same bytes are the same photograph however
            # the folder is renamed, and a re-run must not duplicate rows.
            n_photo += c.execute(text("""
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
                 "src": SOURCE, "by": CREATED_BY}).rowcount
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
    return n_core, n_photo


def remove(engine, uwi):
    from sqlalchemy import text
    with engine.begin() as c:
        p = c.execute(text("DELETE FROM dataview.dv_well_core_photo "
                           "WHERE uwi = :u AND row_created_by = :by"),
                      {"u": uwi, "by": CREATED_BY}).rowcount
        k = c.execute(text("DELETE FROM dataview.dv_well_core "
                           "WHERE uwi = :u AND row_created_by = :by"),
                      {"u": uwi, "by": CREATED_BY}).rowcount
    return p, k


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
        p, k = remove(engine, uwi)
        print("Deleted %d photo(s) and %d core run(s)." % (p, k))
        return 0

    uwi, wname, cores, rows, held = plan(engine)
    print("Well        : %s  (%s %s)" % (uwi, wname, WELL_NUM))
    print("Core runs   : %d from CORE ACCOUNTING.xls" % len(cores))
    print("Photos      : %d placed, %d held" % (len(rows), len(held)))
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
    n_core, n_photo = write(engine, uwi, cores, rows)
    print("\nWrote %d core run(s) and %d photo(s)." % (n_core, n_photo))
    print("Undo with:  python tools/load_core_photos.py --remove --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
