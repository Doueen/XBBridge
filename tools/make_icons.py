"""Draw candidate icons for the xb extension (A/B/C) + a preview sheet."""
from PIL import Image, ImageDraw, ImageFont
import os

SS = 4                      # supersample factor
BASE = 128
S = BASE * SS
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "extension", "icons")
CANDIDATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "test", "icon-candidates")
os.makedirs(OUT, exist_ok=True)
os.makedirs(CANDIDATES, exist_ok=True)

TEAL_TOP = (20, 184, 166)
TEAL_BOT = (13, 122, 111)
TEAL = (13, 148, 136)
DARK = (15, 23, 42)
CARD = (241, 245, 249)


def rounded_mask(size, radius, ss=SS):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def gradient(size, top, bottom):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        k = y / max(1, size - 1)
        g.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    return g.resize((size, size), Image.NEAREST)


def badge_x():
    """A — teal rounded badge with a bold white x."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    body = gradient(S, TEAL_TOP, TEAL_BOT).convert("RGBA")
    img.paste(body, (0, 0), rounded_mask(S, int(S * 0.26)))
    d = ImageDraw.Draw(img)
    pad = int(S * 0.29)
    w = max(2, int(S * 0.085))
    d.line([(pad, pad), (S - pad, S - pad)], fill=(255, 255, 255, 255), width=w)
    d.line([(S - pad, pad), (pad, S - pad)], fill=(255, 255, 255, 255), width=w)
    # round the stroke ends
    r = w // 2
    for cx, cy in [(pad, pad), (S - pad, pad), (pad, S - pad), (S - pad, S - pad)]:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    return img


def cursor_x():
    """B — dark card, teal pointer, the click point marked with an x."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    body = Image.new("RGBA", (S, S), DARK + (255,))
    img.paste(body, (0, 0), rounded_mask(S, int(S * 0.26)))
    d = ImageDraw.Draw(img)
    # pointer polygon (classic arrow), scaled/positioned inside the badge
    pts = [(0.30, 0.20), (0.30, 0.78), (0.45, 0.63), (0.55, 0.84),
           (0.65, 0.79), (0.55, 0.58), (0.74, 0.55)]
    poly = [(x * S, y * S) for x, y in pts]
    d.polygon(poly, fill=(255, 255, 255, 255))
    # small teal x at the pointer tip
    cx, cy, arm = 0.31 * S, 0.24 * S, 0.085 * S
    w = max(2, int(S * 0.045))
    d.line([(cx - arm, cy - arm), (cx + arm, cy + arm)], fill=TEAL_TOP + (255,), width=w)
    d.line([(cx + arm, cy - arm), (cx - arm, cy + arm)], fill=TEAL_TOP + (255,), width=w)
    return img


def badge_cursor():
    """D — teal badge with the pointer: brand colour + "something is driving"."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    body = gradient(S, TEAL_TOP, TEAL_BOT).convert("RGBA")
    img.paste(body, (0, 0), rounded_mask(S, int(S * 0.26)))
    d = ImageDraw.Draw(img)
    pts = [(0.30, 0.18), (0.30, 0.80), (0.46, 0.64), (0.56, 0.86),
           (0.67, 0.81), (0.57, 0.59), (0.76, 0.56)]
    d.polygon([(x * S, y * S) for x, y in pts], fill=(255, 255, 255, 255))
    cx, cy, arm = 0.30 * S, 0.23 * S, 0.075 * S
    w = max(2, int(S * 0.042))
    d.line([(cx - arm, cy - arm), (cx + arm, cy + arm)], fill=(6, 78, 70, 255), width=w)
    d.line([(cx + arm, cy - arm), (cx - arm, cy + arm)], fill=(6, 78, 70, 255), width=w)
    return img


def tabs_x():
    """C — your tab (teal, marked) in front of a plain one."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    body = Image.new("RGBA", (S, S), CARD + (255,))
    img.paste(body, (0, 0), rounded_mask(S, int(S * 0.26)))
    d = ImageDraw.Draw(img)
    # back tab
    d.rounded_rectangle([int(S * 0.30), int(S * 0.16), int(S * 0.84), int(S * 0.50)],
                        radius=int(S * 0.07), fill=(203, 213, 225, 255))
    # front tab
    d.rounded_rectangle([int(S * 0.16), int(S * 0.30), int(S * 0.84), int(S * 0.66)],
                        radius=int(S * 0.08), fill=TEAL + (255,))
    # white x inside the front tab
    pad = int(S * 0.11)
    w = max(2, int(S * 0.055))
    x0, y0, x1, y1 = int(S * 0.30), int(S * 0.40), int(S * 0.70), int(S * 0.56)
    d.line([(x0, y0), (x1, y1)], fill=(255, 255, 255, 255), width=w)
    d.line([(x0, y1), (x1, y0)], fill=(255, 255, 255, 255), width=w)
    # caption bar
    d.rounded_rectangle([int(S * 0.30), int(S * 0.74), int(S * 0.70), int(S * 0.80)],
                        radius=int(S * 0.03), fill=(148, 163, 184, 255))
    return img


def font(size):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def main():
    makers = [("A", "徽章 x", badge_x), ("B", "光标 x", cursor_x),
              ("C", "接管标签", tabs_x), ("D", "徽章+光标", badge_cursor)]
    sizes = [128, 48, 32, 16]
    tiles = {}
    for key, label, fn in makers:
        big = fn().resize((BASE, BASE), Image.LANCZOS)
        for s in sizes:
            img = big.resize((s, s), Image.LANCZOS) if s != BASE else big
            if s == 16:
                # 16px should be hand-tuned, not just downscaled: redraw at 4x then
                # nearest-downscale so the strokes stay crisp
                img = fn().resize((s, s), Image.LANCZOS)
            img.save(os.path.join(CANDIDATES, f"icon-{key.lower()}-{s}.png"))
            if key == "A":
                # A is the shipped mark: canonical names live in extension/icons
                img.save(os.path.join(OUT, f"icon-{s}.png"))
            tiles[(key, s)] = img
        tiles[(key, "128")] = big
        print(key, label, "->", [f"{s}px" for s in sizes])

    # ---------- preview sheet ----------
    pad, colw, top = 28, 200, 96
    sheet = Image.new("RGB", (pad * 2 + colw * 4, 470), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    f_name, f_note, f_small = font(19), font(14), font(13)

    for i, (key, label, _fn) in enumerate(makers):
        x = pad + colw * i
        d.text((x, 26), f"{key}. {label}", font=f_name, fill=(17, 24, 39))
        big = tiles[(key, "128")]
        sheet.paste(big, (x, top - 34), big)
        # size strip
        strip_y = top + 118
        d.rounded_rectangle([x, strip_y, x + 190, strip_y + 62], radius=10, fill=(243, 244, 246))
        cx = x + 14
        for s in (48, 32, 16):
            im = tiles[(key, s)]
            sheet.paste(im, (cx, strip_y + 31 - s // 2), im)
            cx += s + 22
        d.text((x, strip_y + 70), "48 / 32 / 16 px", font=f_small, fill=(107, 114, 128))
        # zoomed 16 for legibility check
        z = tiles[(key, 16)].resize((64, 64), Image.NEAREST)
        zy = strip_y + 96
        d.rounded_rectangle([x, zy, x + 190, zy + 84], radius=10, fill=(229, 231, 235))
        sheet.paste(z, (x + 14, zy + 10), z)
        d.text((x + 90, zy + 33), "16px @4x", font=f_small, fill=(55, 65, 81))

    d.text((pad, 430), "toolbar is 16px · extension card is 48px · store/detail page is 128px",
           font=f_small, fill=(107, 114, 128))
    sheet.save(os.path.join(CANDIDATES, "..", "icon-preview.png"))
    print("preview ->", os.path.abspath(os.path.join(CANDIDATES, "..", "icon-preview.png")))


if __name__ == "__main__":
    main()
