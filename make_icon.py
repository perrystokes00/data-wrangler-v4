"""
make_icon.py — turn the logo PNG into a proper Windows .ico.

    python make_icon.py
    python make_icon.py --src assets\\logo.png --out assets\\data_wrangler.ico
    python make_icon.py --crop-square      # centre-crop instead of padding

WHY THIS EXISTS
---------------
The first attempt produced a 256x147 icon — every size entry 16:9, because
Pillow honours the source aspect ratio rather than refusing. Windows expects
SQUARE icons and stretches or letterboxes anything else, which is what a
wrong-looking thumbnail actually is.

An .ico is a CONTAINER. Windows picks a size per context: 16px in a title
bar, 32px on the desktop, 256px in large-icon view. Save one size and every
other context gets a scaled, soft version.

A NOTE ON WIDE LOGOS: padding a wordmark into a square leaves it mostly empty
space, and at 16x16 it is an illegible smudge. Icons are read at 16 and 32
pixels far more often than at 256. If the logo has a distinct emblem, crop
that for the icon (--crop-square, or point --src at a cropped file) and keep
the wordmark for the app header.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=r"assets\data_wrangler.png")
    ap.add_argument("--out", default=r"assets\data_wrangler.ico")
    ap.add_argument("--crop-square", action="store_true",
                    help="centre-crop to square instead of padding with transparency")
    a = ap.parse_args()

    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed:  pip install pillow", file=sys.stderr)
        return 2

    src = Path(a.src)
    if not src.exists():
        print(f"not found: {src.resolve()}", file=sys.stderr)
        return 2

    im = Image.open(src).convert("RGBA")
    print(f"source: {src}  {im.width}x{im.height}")

    if im.width != im.height:
        if a.crop_square:
            s = min(im.size)
            left, top = (im.width - s) // 2, (im.height - s) // 2
            im = im.crop((left, top, left + s, top + s))
            print(f"centre-cropped to {s}x{s}")
        else:
            s = max(im.size)
            canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))   # transparent
            canvas.paste(im, ((s - im.width) // 2, (s - im.height) // 2), im)
            im = canvas
            print(f"padded to {s}x{s} (transparent) — use --crop-square for the "
                  f"other behaviour")

    # Pillow will NOT upscale a requested size beyond the source, so resize the
    # square up front. A 147px source asked for a 256 entry silently yields 147.
    if im.width < 256:
        print(f"warning: source is only {im.width}px — upscaling to 256, which "
              f"will look soft. A larger original would be better.")
    im = im.resize((256, 256), Image.LANCZOS)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, sizes=SIZES)

    check = Image.open(out)
    got = sorted(check.info.get("sizes", []))
    print(f"wrote {out}  ({out.stat().st_size:,} bytes)")
    print(f"  base {check.size} · entries {got}")
    if any(w != h for w, h in got):
        print("  >>> NOT SQUARE — Windows will stretch this", file=sys.stderr)
        return 1
    if (256, 256) not in got:
        print("  >>> no 256x256 entry", file=sys.stderr)
        return 1
    print("  square, all sizes present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
