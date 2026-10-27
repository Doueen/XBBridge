#!/usr/bin/env python3
"""Icon lab: render a set of candidate marks for XBBridge and a contact sheet.

Every candidate is drawn at 1024 px and downsampled, so 128 and 16 px stay clean.
The sheet shows each mark at 128 px, at real 16 px on a light and a dark toolbar,
so the pick is made on evidence instead of on the big picture.

Usage:  python tools/icon_lab.py [--out test/icon-lab]
"""
from __future__ import annotations

import argparse
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
S = 1024                    # supersampled canvas
TEAL_TOP = (45, 212, 191)
TEAL_BOT = (13, 122, 111)
TEAL = (13, 148, 136)
DEEP = (11, 34, 32)
DARK = (17, 24, 39)
WHITE = (255, 255, 255, 255)
FONTS = r"C:\Windows\Fonts"


def font(size: int, bold: bool = True, cjk: bool = False):
    names = (["msyhbd.ttc", "simhei.ttf"] if cjk else []) or (["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"]
                                                              if bold else ["segoeui.ttf", "arial.ttf"])
    for n in names:
        p = os.path.join(FONTS, n)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def vgrad(size: int, top, bottom) -> Image.Image:
    g = Image.new("RGB", (1, size))
    for y in range(size):
        k = y / max(1, size - 1)
        g.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    return g.resize((size, size), Image.NEAREST).convert("RGBA")


def badge(radius_ratio: float = 0.26, top=TEAL_TOP, bottom=TEAL_BOT) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * radius_ratio), fill=255)
    img.paste(vgrad(S, top, bottom), (0, 0), mask)
    return img


def disc(fill) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, S - 1, S - 1], fill=fill)
    return img


def stroke(d: ImageDraw.ImageDraw, pts, width: int, fill=WHITE) -> None:
    """Round-capped polyline."""
    d.line(pts, fill=fill, width=width, joint="curve")
    r = width // 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def mark_badge_x() -> Image.Image:
    """1 — current: teal badge, plain white X."""
    img = badge()
    d = ImageDraw.Draw(img)
    stroke(d, [(S * .32, S * .32), (S * .68, S * .68)], int(S * .115))
    stroke(d, [(S * .68, S * .32), (S * .32, S * .68)], int(S * .115))
    return img


def mark_bridge() -> Image.Image:
    """2 — bridge silhouette: deck, two piers, shallow arch."""
    img = badge()
    d = ImageDraw.Draw(img)
    w = int(S * .072)
    d.line([(S * .18, S * .60), (S * .82, S * .60)], fill=WHITE, width=w)          # deck
    for x in (S * .30, S * .70):                                                   # piers
        d.line([(x, S * .60), (x, S * .80)], fill=WHITE, width=w)
    d.arc([S * .18, S * .30, S * .82, S * .90], 180, 360, fill=WHITE, width=w)     # arch
    return img


def bezier(p0, p1, p2, n=40):
    out = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        out.append((x, y))
    return out


def mark_pylon_x() -> Image.Image:
    """3 — X with a bridge tower at each end."""
    img = badge()
    d = ImageDraw.Draw(img)
    stroke(d, [(S * .34, S * .30), (S * .66, S * .70)], int(S * .105))
    stroke(d, [(S * .66, S * .30), (S * .34, S * .70)], int(S * .105))
    for x in (S * .22, S * .78):
        d.rounded_rectangle([x - S * .035, S * .26, x + S * .035, S * .74],
                            radius=int(S * .035), fill=WHITE)
    d.line([(S * .24, S * .42), (S * .76, S * .42)], fill=WHITE, width=int(S * .045))
    return img


def mark_monogram() -> Image.Image:
    """4 — 'XB' monogram."""
    img = badge()
    d = ImageDraw.Draw(img)
    f = font(int(S * .40))
    text = "XB"
    box = d.textbbox((0, 0), text, font=f)
    d.text(((S - (box[2] - box[0])) / 2 - box[0], (S - (box[3] - box[1])) / 2 - box[1]),
           text, font=f, fill=WHITE)
    return img


def mark_arch_x() -> Image.Image:
    """5 — X drawn with two bowed strokes: reads as two bridge arches crossing."""
    img = badge()
    d = ImageDraw.Draw(img)
    w = int(S * .095)
    stroke(d, bezier((S * .30, S * .30), (S * .82, S * .40), (S * .70, S * .70)), w)
    stroke(d, bezier((S * .70, S * .30), (S * .18, S * .40), (S * .30, S * .70)), w)
    return img


def mark_windows() -> Image.Image:
    """9 — two browser windows joined by a teal bar: 'bridged windows'."""
    img = badge()
    d = ImageDraw.Draw(img)
    for x0 in (S * .12, S * .62):
        d.rounded_rectangle([x0, S * .34, x0 + S * .26, S * .66], radius=int(S * .045),
                            outline=WHITE, width=int(S * .035))
        d.line([(x0, S * .45), (x0 + S * .26, S * .45)], fill=WHITE, width=int(S * .030))
    d.line([(S * .40, S * .50), (S * .62, S * .50)], fill=WHITE, width=int(S * .075))
    for x in (S * .40, S * .62):
        r = int(S * .045)
        d.ellipse([x - r, S * .50 - r, x + r, S * .50 + r], fill=WHITE)
    return img


def mark_deck_x() -> Image.Image:
    """10 — X with plank ticks: the strokes read as two bridge decks."""
    img = badge(top=(15, 23, 42), bottom=(8, 12, 22))
    d = ImageDraw.Draw(img)
    w = int(S * .105)
    stroke(d, [(S * .30, S * .30), (S * .70, S * .70)], w, (94, 234, 212, 255))
    stroke(d, [(S * .70, S * .30), (S * .30, S * .70)], w, (94, 234, 212, 255))
    thin = max(8, int(S * .022))
    for k in range(3, 8):
        t = k / 10
        cx, cy = S * (.30 + .40 * t), S * (.30 + .40 * t)
        d.line([(cx - S * .045, cy + S * .045), (cx + S * .045, cy - S * .045)],
               fill=(15, 23, 42, 255), width=thin)
    return img


def mark_nodes() -> Image.Image:
    """6 — two endpoints joined by a sagging cable."""
    img = badge(top=(30, 41, 59), bottom=(11, 20, 34))
    d = ImageDraw.Draw(img)
    x0, x1, ytop, sag = S * .27, S * .73, S * .34, S * .26
    pts = [(x0 + (x1 - x0) * i / 48, ytop + sag * math.sin(math.pi * i / 48)) for i in range(49)]
    stroke(d, pts, int(S * .052), (94, 234, 212, 255))
    for x in (x0, x1):
        r = int(S * .072)
        d.ellipse([x - r, ytop - r, x + r, ytop + r], fill=WHITE)
    return img


def mark_glow_x() -> Image.Image:
    """7 — dark tile, bright teal X with a soft halo."""
    img = badge(top=(23, 32, 46), bottom=(10, 16, 26))
    blur = __import__("PIL.ImageFilter", fromlist=["GaussianBlur"]).GaussianBlur
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dg = ImageDraw.Draw(glow)
    for w, a in ((int(S * .20), 90), (int(S * .15), 140)):
        for a_, b_ in (((S * .29, S * .29), (S * .71, S * .71)), ((S * .71, S * .29), (S * .29, S * .71))):
            dg.line([a_, b_], fill=(45, 212, 191, a), width=w, joint="curve")
    img.alpha_composite(glow.filter(blur(S * .04)))
    d = ImageDraw.Draw(img)
    stroke(d, [(S * .29, S * .29), (S * .71, S * .71)], int(S * .10), (153, 246, 228, 255))
    stroke(d, [(S * .71, S * .29), (S * .29, S * .71)], int(S * .10), (153, 246, 228, 255))
    return img


def mark_circle_x() -> Image.Image:
    """8 — white disc, teal X: reads on any coloured toolbar."""
    img = disc(WHITE)
    d = ImageDraw.Draw(img)
    stroke(d, [(S * .30, S * .30), (S * .70, S * .70)], int(S * .115), TEAL + (255,))
    stroke(d, [(S * .70, S * .30), (S * .30, S * .70)], int(S * .115), TEAL + (255,))
    return img


THEMES = {
    # tile top, tile bottom, stroke colour, hairline border
    "teal":  ((45, 212, 191), (13, 122, 111), (255, 255, 255, 255), None),
    "light": ((255, 255, 255), (233, 243, 243), (18, 165, 148, 255), (206, 224, 224, 255)),
    "dark":  ((15, 23, 42), (8, 12, 22), (94, 234, 212, 255), None),
}


def deck_x(theme: str = "teal", ticks: int = 4, stroke_w: float = .105,
           x_from: float = .30, x_to: float = .70) -> Image.Image:
    """The chosen mark: an X whose descending stroke carries bridge-deck planks.

    theme "teal"  -> the badge colour of candidate 1, white X, planks cut in the tile
    theme "light" -> near-white tile, teal X
    theme "dark"  -> the original candidate 10
    """
    top, bottom, ink, edge = THEMES[theme]
    img = badge(top=top, bottom=bottom)
    d = ImageDraw.Draw(img)
    if edge:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * .26), outline=edge,
                            width=max(2, int(S * .008)))
    w = int(S * stroke_w)
    stroke(d, [(S * x_from, S * x_from), (S * x_to, S * x_to)], w, ink)
    stroke(d, [(S * x_to, S * x_from), (S * x_from, S * x_to)], w, ink)
    if ticks:
        thin = max(6, int(S * .022))
        span = x_to - x_from
        for k in range(ticks):
            t = (k + 1) / (ticks + 1)
            cx, cy = S * (x_from + span * t), S * (x_from + span * t)
            # sample the tile gradient so the plank gap matches the background
            ratio = max(0.0, min(1.0, cy / S))
            notch = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)) + (255,)
            d.line([(cx - w * .55, cy + w * .55), (cx + w * .55, cy - w * .55)],
                   fill=notch, width=thin)
    return img


def build_icon(px: int, theme: str = "teal") -> Image.Image:
    """Per-size art: at 16 px the plank ticks would just be noise."""
    if px <= 16:
        big = deck_x(theme=theme, ticks=0, stroke_w=.125)
    elif px <= 32:
        big = deck_x(theme=theme, ticks=2, stroke_w=.115)
    else:
        big = deck_x(theme=theme, ticks=4, stroke_w=.105)
    return big.resize((px, px), Image.LANCZOS)


def mark_deck_light() -> Image.Image:
    """10L — light tile, teal X with deck ticks."""
    return deck_x(theme="light")


def mark_deck_light_clean() -> Image.Image:
    """10L-c — same but no ticks: calmest at 16 px."""
    return deck_x(theme="light", ticks=0)


def mark_deck_light_tint() -> Image.Image:
    """10L-b — light teal-tinted tile instead of near-white."""
    img = badge(top=(240, 253, 250), bottom=(205, 235, 230))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * .26),
                        outline=(178, 216, 210, 255), width=max(2, int(S * .008)))
    w = int(S * .105)
    stroke(d, [(S * .30, S * .30), (S * .70, S * .70)], w, (11, 122, 111, 255))
    stroke(d, [(S * .70, S * .30), (S * .30, S * .70)], w, (11, 122, 111, 255))
    thin = max(6, int(S * .022))
    for k in range(4):
        t = (k + 1) / 5
        cx, cy = S * (.30 + .40 * t), S * (.30 + .40 * t)
        d.line([(cx - w * .55, cy + w * .55), (cx + w * .55, cy - w * .55)],
               fill=(228, 245, 242, 255), width=thin)
    return img


CANDIDATES = [
    ("1-badge-x", mark_badge_x),
    ("2-bridge", mark_bridge),
    ("3-pylon-x", mark_pylon_x),
    ("4-monogram", mark_monogram),
    ("5-arch-x", mark_arch_x),
    ("6-nodes", mark_nodes),
    ("7-glow-x", mark_glow_x),
    ("8-disc-x", mark_circle_x),
    ("9-windows", mark_windows),
    ("10-deck-x", mark_deck_x),
    ("10L-deck-light", mark_deck_light),
    ("10Lb-deck-tint", mark_deck_light_tint),
    ("10Lc-deck-clean", mark_deck_light_clean),
    ("11-deck-teal", lambda: deck_x(theme="teal")),
]


def install(theme: str = "teal") -> None:
    """Write the chosen mark into extension/icons at every size the manifest lists."""
    out = os.path.join(ROOT, "extension", "icons")
    os.makedirs(out, exist_ok=True)
    for px in (16, 32, 48, 128):
        icon = build_icon(px, theme=theme)
        path = os.path.join(out, f"icon-{px}.png")
        icon.save(path)
        print(f"  {os.path.basename(path):<14}{px}x{px}  {os.path.getsize(path):,} B")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "test", "icon-lab"))
    ap.add_argument("--install", action="store_true",
                    help="also write the chosen light deck mark into extension/icons")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    made = {}
    for name, fn in CANDIDATES:
        big = fn()
        for px in (128, 48, 32, 16):
            made[(name, px)] = big.resize((px, px), Image.LANCZOS)
        made[(name, 128)].save(os.path.join(args.out, f"{name}-128.png"))

    # contact sheet
    cols, pad, cell_w, cell_h = 4, 26, 270, 250
    rows = (len(CANDIDATES) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w + pad * 2, rows * cell_h + pad * 2 + 150), (246, 248, 251))
    d = ImageDraw.Draw(sheet)
    for i, (name, _) in enumerate(CANDIDATES):
        cx = pad + (i % cols) * cell_w
        cy = pad + (i // cols) * cell_h
        d.rounded_rectangle([cx, cy, cx + cell_w - 18, cy + cell_h - 20], radius=16,
                            fill=(255, 255, 255), outline=(224, 229, 238))
        icon = made[(name, 128)]
        sheet.paste(icon, (cx + 22, cy + 20), icon)
        d.text((cx + 168, cy + 34), name.split("-")[0], font=font(26), fill=(16, 24, 40))
        d.text((cx + 168, cy + 70), name.split("-", 1)[1].replace("-", " "), font=font(17, False),
               fill=(102, 112, 133))
        # real 16px on light and dark
        for j, (bg, label) in enumerate((((255, 255, 255), "light"), ((32, 33, 36), "dark"))):
            bx, by = cx + 168, cy + 104 + j * 46
            d.rounded_rectangle([bx, by, bx + 74, by + 40], radius=8, fill=bg,
                                outline=(210, 216, 226) if j == 0 else None)
            sheet.paste(made[(name, 16)], (bx + 12, by + 12), made[(name, 16)])
            sheet.paste(made[(name, 32)], (bx + 36, by + 4), made[(name, 32)])
            d.text((bx + 82, by + 12), label, font=font(14, False), fill=(150, 160, 172))

    # actual-size strip
    strip_y = rows * cell_h + pad + 10
    d.text((pad, strip_y - 4), "16 px at real size", font=font(18, False), fill=(102, 112, 133))
    d.rounded_rectangle([pad, strip_y + 26, cols * cell_w + pad, strip_y + 74], radius=10, fill=(32, 33, 36))
    for i, (name, _) in enumerate(CANDIDATES):
        sheet.paste(made[(name, 16)], (pad + 24 + i * (cell_w - 40), strip_y + 42), made[(name, 16)])
    sheet.save(os.path.join(args.out, "contact-sheet.png"))

    print(f"wrote {len(CANDIDATES)} candidates + contact sheet to {args.out}")
    print("  " + ", ".join(n for n, _ in CANDIDATES))
    print("  sheet:", os.path.join(args.out, "contact-sheet.png"),
          Image.open(os.path.join(args.out, "contact-sheet.png")).size)
    if args.install:
        print("installing the light deck mark into extension/icons:")
        install(theme="teal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
