#!/usr/bin/env python3
"""Turn a portrait into the ASCII art drawn on the left of the card.

    python3 card/make_ascii.py photo.jpg --mask mask.png

Needs Pillow and numpy (only for this one-off step; the daily workflow does
not run it). Writes two files next to this script:

    ascii-dark.txt   bright pixels -> dense glyphs (for the dark card)
    ascii-light.txt  dark pixels   -> dense glyphs (for the light card)

The optional mask (white = person, black = background) blanks the background.
Glyph densities are measured from the same font the card embeds, so the
shading matches what GitHub renders.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONT = HERE / "font.woff2"

# Must match ART_COLS / ART_ROWS / ART_FONT_SIZE / ART_LINE_HEIGHT in generate.py.
COLS, ROWS = 60, 50
CELL_W, CELL_H = 6.0, 10.0  # font-size 10px, 0.6em advance, 10px line height

RAMP = " .'`,:;!|/()1?+*xoXO#%&8B@$"


def glyph_ramp(chars: str) -> list[tuple[float, str]]:
    """Ink coverage of every candidate glyph, normalised to 0..1."""
    size = 60
    font = ImageFont.truetype(str(FONT), size)
    cell_area = (size * 0.6) * (size * CELL_H / 10.0)
    out = []
    for ch in dict.fromkeys(chars + " "):
        img = Image.new("L", (size, size * 2), 0)
        ImageDraw.Draw(img).text((0, size // 2), ch, font=font, fill=255)
        out.append((float(np.asarray(img, dtype=np.float64).sum() / 255.0 / cell_area), ch))
    top = max(d for d, _ in out)
    return sorted((d / top, ch) for d, ch in out)


def pick(level: float, ramp: list[tuple[float, str]]) -> str:
    return min(ramp, key=lambda dc: abs(dc[0] - level))[1]


def auto_crop(mask: np.ndarray, aspect: float, zoom: float) -> tuple[int, int, int, int]:
    """Box of the given aspect starting just above the head, centred on it.

    zoom = share of the space below the top of the head to keep
    (1 = down to the bottom edge, smaller = tighter on the face)."""
    h, w = mask.shape
    rows = np.where(mask.max(axis=1) > 0.5)[0]
    top = int(rows[0]) if len(rows) else 0
    head = mask[top : top + int(0.35 * (h - top))]
    ys, xs = np.nonzero(head > 0.5)
    cx = float(xs.mean()) if len(xs) else w / 2
    y0 = max(0, top - int(0.03 * h))
    box_h = min(h - y0, (h - y0) * zoom)
    box_w = min(box_h * aspect, w)
    box_h = box_w / aspect
    x0 = int(round(min(max(cx - box_w / 2, 0), w - box_w)))
    return x0, y0, int(round(x0 + box_w)), int(round(y0 + box_h))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photo")
    ap.add_argument("--mask", help="white = person, black = background")
    ap.add_argument("--zoom", type=float, default=0.8, help="1 = head to bottom edge, less = tighter")
    ap.add_argument("--debug", help="also save the downsampled grayscale grid to this PNG")
    ap.add_argument("--ramp", default=RAMP, help="glyphs to shade with (sorted by measured ink)")
    ap.add_argument("--gamma", type=float, default=0.9, help="<1 brightens mid-tones")
    ap.add_argument("--equalize", type=float, default=0.6,
                    help="0..1 blend of histogram equalisation (spreads tones over the whole ramp)")
    ap.add_argument("--local", type=float, default=0.8, help="local-contrast boost on the grid (0 = off)")
    ap.add_argument("--floor", type=float, default=0.03, help="minimum density inside the silhouette")
    ap.add_argument("--ceil", type=float, default=1.0, help="maximum density (lower avoids solid '@' blocks)")
    args = ap.parse_args()

    photo = Image.open(args.photo).convert("L")
    if args.mask:
        mask_img = Image.open(args.mask).convert("L").resize(photo.size, Image.BILINEAR)
    else:
        mask_img = Image.new("L", photo.size, 255)
    mask = np.asarray(mask_img, dtype=np.float64) / 255.0

    aspect = (COLS * CELL_W) / (ROWS * CELL_H)
    box = auto_crop(mask, aspect, args.zoom)
    photo = photo.crop(box).filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))
    mask_img = mask_img.crop(box)

    small = photo.resize((COLS, ROWS), Image.BOX)
    if args.local:  # boost local contrast so eyes, brows and mouth survive the ramp
        small = small.filter(ImageFilter.UnsharpMask(radius=2, percent=int(args.local * 100), threshold=0))
    lum = np.asarray(small, dtype=np.float64) / 255.0
    fg = np.asarray(mask_img.resize((COLS, ROWS), Image.BOX), dtype=np.float64) / 255.0 > 0.5

    # Stretch contrast using only the person's pixels, then blend in histogram
    # equalisation so the face spreads across the ramp instead of saturating.
    lo, hi = np.percentile(lum[fg], [2, 98]) if fg.any() else (0.0, 1.0)
    stretched = np.clip((lum - lo) / max(hi - lo, 1e-6), 0, 1)
    ranks = np.zeros_like(lum)
    if fg.any():
        order = np.sort(lum[fg])
        ranks = np.searchsorted(order, lum, side="right") / len(order)
    v = ((1 - args.equalize) * stretched + args.equalize * ranks) ** args.gamma
    if args.debug:
        grid = np.where(fg, v, 0) * 255
        Image.fromarray(grid.astype(np.uint8)).resize(
            (int(COLS * CELL_W), int(ROWS * CELL_H)), Image.NEAREST).save(args.debug)

    ramp = glyph_ramp(args.ramp)
    span = args.ceil - args.floor
    for name, levels in (("dark", v), ("light", 1 - v)):
        lines = []
        for r in range(ROWS):
            row = "".join(
                pick(args.floor + span * levels[r, c], ramp) if fg[r, c] else " "
                for c in range(COLS)
            )
            lines.append(row.rstrip())
        (HERE / f"ascii-{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote ascii-{name}.txt ({COLS}x{ROWS}, crop {box})")


if __name__ == "__main__":
    main()
