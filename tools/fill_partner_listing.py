#!/usr/bin/env python3
"""Fill the Partner Center "Store listings" detail page for xb bridge.

Everything on that page lives in Fluent web components, so this runs as injected
scripts: the description and search terms are written with the native value
setter (composed events so React/Fluent register it), and the images are uploaded
by building a real File in the page and assigning it to the hidden
<input type=file> through a DataTransfer — no native file dialog, no CDP file API
(which cannot see inputs inside shadow roots).

Usage:
  python tools/fill_partner_listing.py --tab N [--logos] [--text] [--images] [--terms] [--all]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "docs", "store")

DESCRIPTION = """xb bridge is the browser half of `xb`, a command-line agent bridge that runs on your own machine.

• Tabs, not windows — the agent opens what it needs as tabs inside a named, colour-coded group in the window you are already using. No new window appears and nothing is brought to the front unless you ask for it.

• You can always see what it touches — every controlled tab gets a thin teal frame and a small label in the corner, and its favicon switches to the xb mark, so a quick look at the tab strip tells you which tabs are under control.

• Leases — two agents (or two of your own scripts) cannot fight over the same tab: the first one to claim a tab holds it until it releases or the lease expires, and a navigation-generation check rejects commands aimed at a page that has already changed.

• Background-safe — screenshots, waits and reads work on background tabs without stealing focus; a minimized window is reported as such instead of failing silently.

• Honest errors — every failure carries a machine-readable code, whether the action was applied or not, and whether a retry is safe, so scripts never guess and never double-submit.

• Batch and drag — a list of steps can be sent in one call, and drag/real-mouse commands exist for pages that ignore synthetic events.

xb bridge is a client: it needs the open-source `xb` daemon (Python) running on the same computer. Without it the popup simply says the bridge is not connected and never touches your browsing.

No account, no cloud, no telemetry. Page content never leaves your machine; the only network connection the extension makes is a loopback WebSocket to 127.0.0.1."""

SEARCH_TERMS = ["browser automation", "AI agent", "tab group", "CLI", "automation",
                "agent bridge", "web scraping"]


# --------------------------------------------------------------------- assets
def make_logo(path: str, size: int = 300) -> str:
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGB", (1, s))
    top, bottom = (20, 184, 166), (13, 122, 111)
    for y in range(s):
        k = y / (s - 1)
        grad.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    grad = grad.resize((s, s), Image.NEAREST).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.26), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    w = int(s * 0.115)                      # stroke width
    a, b = int(s * 0.315), int(s * 0.685)   # x span
    d.line([(a, a), (b, b)], fill=(255, 255, 255, 255), width=w, joint="curve")
    d.line([(b, a), (a, b)], fill=(255, 255, 255, 255), width=w, joint="curve")
    for x, y in ((a, a), (b, b), (b, a), (a, b)):   # round the caps
        d.ellipse([x - w // 2, y - w // 2, x + w // 2, y + w // 2], fill=(255, 255, 255, 255))
    img.resize((size, size), Image.LANCZOS).save(path)
    return path


# ------------------------------------------------------------------- plumbing
def xb_js(browser: str, tab: str, js: str) -> str:
    p = subprocess.run([sys.executable, os.path.join(ROOT, "xb.py"), "--json",
                        "--browser", browser, "eval", "--tab", tab, js],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    try:
        return json.loads(out)["value"]
    except Exception:
        raise SystemExit(f"eval failed: {out[:400]} {p.stderr[:300]}")


ROOTS = """
  const roots = []; const collect = r => { roots.push(r);
    r.querySelectorAll("*").forEach(e => e.shadowRoot && collect(e.shadowRoot)); };
  collect(document);
"""

SET_NATIVE = """
  const setNative = (el, text) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, text); else el.value = text;
    for (const ev of ["input", "change", "blur"])
      el.dispatchEvent(new Event(ev, { bubbles: true, composed: true }));
  };
"""


def set_description(browser: str, tab: str) -> str:
    js = f"""(() => {{{ROOTS}{SET_NATIVE}
      let ta = null;
      for (const r of roots) for (const t of r.querySelectorAll("textarea")) {{
        const aria = t.getAttribute("aria-label") || "";
        if (/^Description/i.test(aria)) ta = t;
      }}
      if (!ta) return JSON.stringify({{ok:false, why:"description textarea not found"}});
      setNative(ta, {json.dumps(DESCRIPTION)});
      return JSON.stringify({{ok:true, chars: ta.value.length}});
    }})()"""
    return xb_js(browser, tab, js)


def add_terms(browser: str, tab: str) -> str:
    js = f"""(() => {{{ROOTS}
      const btn = [];
      for (const r of roots) for (const el of r.querySelectorAll("button,v6_he-button,a"))
        if (/^Add Term$/i.test((el.innerText||"").replace(/\\s+/g," ").trim())) btn.push(el);
      return JSON.stringify({{ok:true, found: btn.length}});
    }})()"""
    return xb_js(browser, tab, js)


def upload(browser: str, tab: str, input_index: int, files: list[str], field: str) -> str:
    payload = []
    for path in files:
        with open(path, "rb") as fh:
            payload.append({"name": os.path.basename(path),
                            "b64": base64.b64encode(fh.read()).decode()})
    js = f"""(() => {{
      const DATA = {json.dumps(payload)};
      const IDX = {input_index};
      {ROOTS}
      const inputs = [];
      for (const r of roots) for (const el of r.querySelectorAll("input[type=file]")) inputs.push(el);
      const input = inputs[IDX];
      if (!input) return JSON.stringify({{ok:false, why:"input #" + IDX + " not found", count: inputs.length}});
      const dt = new DataTransfer();
      for (const d of DATA) {{
        const bin = atob(d.b64);
        const buf = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
        dt.items.add(new File([buf], d.name, {{ type: "image/png" }}));
      }}
      input.files = dt.files;
      input.dispatchEvent(new Event("input", {{ bubbles: true, composed: true }}));
      input.dispatchEvent(new Event("change", {{ bubbles: true, composed: true }}));
      return JSON.stringify({{ok:true, field: {json.dumps(field)}, accepted: input.files.length,
                             names: [...input.files].map(f => f.name + " " + f.size)}});
    }})()"""
    return xb_js(browser, tab, js)


def state(browser: str, tab: str) -> str:
    js = f"""(() => {{{ROOTS}
      const imgs = [...document.querySelectorAll("img")].filter(i => (i.src||"").startsWith("data:") || /blob/i.test(i.src||""));
      let desc = 0, terms = 0;
      for (const r of roots) for (const t of r.querySelectorAll("textarea"))
        if (/^Description/i.test(t.getAttribute("aria-label") || "")) desc = (t.value || "").length;
      for (const r of roots) for (const el of r.querySelectorAll("input"))
        if ((el.getAttribute("aria-label") || "").toLowerCase().includes("term") || /term/i.test(el.name || "")) terms++;
      return JSON.stringify({{descChars: desc, dataImages: imgs.length, termInputs: terms,
                             thumbs: [...document.querySelectorAll("img")].length}});
    }})()"""
    return xb_js(browser, tab, js)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="daily")
    ap.add_argument("--tab", required=True)
    ap.add_argument("--logos", action="store_true", help="upload extension logo + small tile")
    ap.add_argument("--text", action="store_true", help="fill the description")
    ap.add_argument("--images", action="store_true", help="upload the three screenshots")
    ap.add_argument("--terms", action="store_true", help="report the Add Term control")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--state", action="store_true")
    args = ap.parse_args()
    everything = args.all or not (args.logos or args.text or args.images or args.terms or args.state)

    os.makedirs(ASSETS, exist_ok=True)
    logo = os.path.join(ASSETS, "logo-300.png")
    if not os.path.exists(logo):
        make_logo(logo)
        print("logo:", logo, Image.open(logo).size)

    if args.state:
        print("state:", state(args.browser, args.tab))
        return 0
    if everything or args.text:
        print("description:", set_description(args.browser, args.tab))
    if everything or args.logos:
        print("logo   :", upload(args.browser, args.tab, 0, [logo], "extension logo"))
        tile = os.path.join(ASSETS, "tile-440x280.png")
        print("tile   :", upload(args.browser, args.tab, 1, [tile], "small promotional tile"))
    if everything or args.images:
        shots = [os.path.join(ASSETS, n) for n in
                 ("shot-1-marked-tab.png", "shot-2-cli.png", "shot-3-browsers.png")]
        for shot in shots:
            print(f"shot   :", upload(args.browser, args.tab, 2, [shot], os.path.basename(shot)))
    if everything or args.terms:
        print("terms  :", add_terms(args.browser, args.tab))
    print("state  :", state(args.browser, args.tab))
    return 0


if __name__ == "__main__":
    sys.exit(main())
