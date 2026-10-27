#!/usr/bin/env python3
"""Compose the Edge Add-ons store images from real captures.

Nothing here is mocked: shot 1 is an actual page screenshot taken through the
bridge, shots 2-3 are real command output rendered in a terminal frame, and the
tile reuses the shipped 128px icon.

Usage:
  python tools/make_store_shots.py --page test/store-page.png
Outputs into docs/store/: shot-1-marked-tab.png (1280x800),
shot-2-cli.png (1280x800), shot-3-browsers.png (1280x800), tile-440x280.png
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "docs", "store")

W, H = 1280, 800
BG = (18, 21, 26)
FG = (232, 236, 241)
MUTED = (150, 160, 172)
TEAL = (0, 168, 150)
AMBER = (226, 168, 62)

FONTS = r"C:\Windows\Fonts"
MONO_CANDIDATES = ["consola.ttf", "cour.ttf", "lucon.ttf"]
SANS_CANDIDATES = ["segoeui.ttf", "arial.ttf", "calibri.ttf"]
SANS_BOLD = ["segoeuib.ttf", "arialbd.ttf", "calibrib.ttf"]
# Segoe UI has no CJK glyphs: Chinese would render as tofu boxes
CJK_CANDIDATES = ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "Deng.ttf"]


def _has_cjk(text: str) -> bool:
    return any("⺀" <= ch <= "鿿" or "＀" <= ch <= "￯" for ch in text)


def font(kind: str, size: int, text: str = ""):
    if _has_cjk(text):
        names = ["msyhbd.ttc"] if kind == "bold" else CJK_CANDIDATES
    else:
        names = {"mono": MONO_CANDIDATES, "sans": SANS_CANDIDATES, "bold": SANS_BOLD}[kind]
    for name in names:
        path = os.path.join(FONTS, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def caption(d: ImageDraw.ImageDraw, y: int, title: str, sub: str) -> None:
    d.text((56, y), title, font=font("bold", 40, title), fill=FG)
    d.text((56, y + 56), sub, font=font("sans", 22, sub), fill=MUTED)


def rounded(img: Image.Image, radius: int = 14) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1],
                                           radius=radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def shot_page(page_path: str, out_path: str) -> None:
    page = Image.open(page_path).convert("RGB")
    # fit into the 1280 x 560 content area, keeping aspect
    box_w, box_h = W - 112, 448
    scale = min(box_w / page.width, box_h / page.height)
    page = page.resize((max(1, int(page.width * scale)), max(1, int(page.height * scale))),
                       Image.LANCZOS)
    img, d = canvas()
    caption(d, 64, "Controlled tabs are marked, in the window you already use",
            "受控标签带可见标记与 favicon 换装，不弹新窗口、不抢焦点")
    x = (W - page.width) // 2
    y = 216
    img.paste(page, (x, y))
    d.rectangle([x - 3, y - 3, x + page.width + 2, y + page.height + 2],
                outline=TEAL, width=3)
    d.text((56, y + box_h + 30), "xb bridge · tab groups · visible marks · leases",
           font=font("sans", 20), fill=TEAL)
    img.save(out_path)


def shot_terminal(title: str, sub: str, text: str, out_path: str) -> None:
    img, d = canvas()
    caption(d, 64, title, sub)
    pad, top = 56, 200
    lines = text.strip().splitlines()[:22]
    panel_h = min(H - top - 40, 46 + 27 * len(lines) + 26)
    d.rounded_rectangle([pad, top, W - pad, top + panel_h], radius=12,
                        fill=(12, 14, 18), outline=(48, 54, 62), width=1)
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([pad + 18 + i * 22, top + 16, pad + 30 + i * 22, top + 28], fill=color)
    y = top + 46
    for line in lines:
        color = FG
        if line.startswith("$") or line.startswith("C:\\") or line.startswith("cd "):
            color = TEAL
        elif "PASS" in line or "ALL GREEN" in line:
            color = TEAL
        elif line.strip().startswith("*") or "租约" in line:
            color = AMBER
        d.text((pad + 22, y), line[:96], font=font("mono", 19, line), fill=color)
        y += 27
    img.save(out_path)


def shot_tile(icon_path: str, out_path: str) -> None:
    tw, th = 440, 280
    tile = Image.new("RGB", (tw, th), BG)
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle([0, 0, tw - 1, th - 1], radius=18, outline=(40, 46, 54), width=2)
    icon = Image.open(icon_path).convert("RGBA").resize((96, 96), Image.LANCZOS)
    tile.paste(icon, (36, 92), icon)
    d.text((156, 96), "xb bridge", font=font("bold", 34, "xb bridge"), fill=FG)
    d.text((156, 140), "tabs, not windows", font=font("sans", 19), fill=TEAL)
    cjk = "受控标签 · 可见标记 · 租约"
    d.text((156, 170), cjk, font=font("sans", 17, cjk), fill=MUTED)
    tile.save(out_path)


def xb(*argv: str) -> str:
    p = subprocess.run([sys.executable, os.path.join(ROOT, "xb.py")] + list(argv),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (p.stdout or p.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True, help="real screenshot taken via `xb screenshot`")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    shot_page(args.page, os.path.join(OUT, "shot-1-marked-tab.png"))
    shot_terminal("Every controlled tab, one glance",
                  "工具栏面板与 xb panel 同一份快照：谁被控制、租约还剩多久",
                  xb("panel"), os.path.join(OUT, "shot-2-cli.png"))
    shot_terminal("Multiple browsers, explicitly addressed",
                  "多浏览器同时在线，--browser 精确指定，错误带码可重试",
                  "$ xb browsers\n" + xb("browsers") + "\n\n$ xb status\n" + xb("status"),
                  os.path.join(OUT, "shot-3-browsers.png"))
    shot_tile(os.path.join(ROOT, "extension", "icons", "icon-128.png"),
              os.path.join(OUT, "tile-440x280.png"))
    for name in ("shot-1-marked-tab.png", "shot-2-cli.png", "shot-3-browsers.png",
                 "tile-440x280.png"):
        path = os.path.join(OUT, name)
        with Image.open(path) as im:
            print(f"  {name:<26}{im.width}x{im.height}  {os.path.getsize(path):,} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
