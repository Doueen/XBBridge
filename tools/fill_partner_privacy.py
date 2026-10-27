#!/usr/bin/env python3
"""Fill the Partner Center "Privacy" form for xb bridge through the xb bridge.

The page renders its fields inside Fluent web components (shadow roots), so
document.querySelector cannot see them; the whole job runs as one injected
script that walks shadow roots, sets values with the native setter (so React /
Fluent pick the change up), then reports what it actually did.

Usage:  python tools/fill_partner_privacy.py [--browser daily] [--tab 1654217648] [--dry]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FIELDS = {
    "single purpose": (
        "xb bridge lets a locally running command-line agent drive the user's own Edge tabs: "
        "it opens tabs in a named tab group, marks the tabs it controls with a visible overlay, "
        "reads pages on request, and performs clicks, typing and scrolling the user explicitly "
        "asked for. It is a client for a companion daemon on the same machine (127.0.0.1:8787); "
        "it does no work on its own and sends nothing off the device."
    ),
    "tabs justification": (
        "List, open, focus and close the user's tabs - the entry point for every command the user "
        "runs. Nothing is read or changed without a command addressed to a specific tab."
    ),
    "tabgroups justification": (
        "Keep the tabs opened for one agent task inside a single named, colour-coded group, so the "
        "user can see them at a glance and close them in one action."
    ),
    "windows justification": (
        "Read window bounds and minimized state to decide whether a screenshot or a real mouse "
        "event is possible at all; a minimized window reports a 0x0 viewport."
    ),
    "scripting justification": (
        "Inject a pointer-events:none overlay that marks the tab currently under control, and read "
        "page text or a snapshot only when the user runs text/snapshot for that tab."
    ),
    "debugger justification": (
        "Used only by the explicit 'click --trusted' command, to dispatch genuine mouse events on "
        "pages that ignore synthetic ones. No page data, network traffic or console output is read "
        "through the debugger."
    ),
    "alarms justification": (
        "Re-attach the background service worker to the local daemon after the browser puts it to "
        "sleep (15-second keep-alive), so a later command does not hang."
    ),
    "storage justification": (
        "Store a random local instance id, the human label the user gives the browser, the table of "
        "marked tabs and the lease tokens. Local only, never uploaded or synced."
    ),
    "sessions justification": (
        "Power the 'sessions.recent' command, which lists recently closed tabs - only when the user "
        "asks for that list."
    ),
    "downloads justification": (
        "Power the 'download' command, which saves one file the user asked for into a directory the "
        "user named."
    ),
    "host permission justification": (
        "The user chooses at run time which page the agent works on, so the set of domains cannot be "
        "declared in advance. Nothing is read from a page until the user issues a command for that "
        "tab, and no data ever leaves the machine."
    ),
}

CERTIFICATIONS = [
    "I do not sell or transfer user data to third parties, outside of the approved use cases",
    "I do not use or transfer user data for purposes that are unrelated to my item's single purpose",
    "I do not use or transfer user data to determine creditworthiness or for lending purposes",
]

REMOTE_CODE_NO = "No, I am not using remote code"

JS = r"""
(() => {
  const DATA = __DATA__;
  const CERTS = __CERTS__;
  const NO_REMOTE = __NO_REMOTE__;
  const DRY = __DRY__;

  const report = { filled: [], missing: [], remote: null, certs: [], notes: [] };

  // walk every root, shadow roots included
  const roots = [];
  const collect = (root) => {
    roots.push(root);
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) collect(el.shadowRoot);
  };
  collect(document);

  const ctxOf = (el) => {                    // nearest short ancestor text = the field label
    let n = el.getRootNode().host || el;
    for (let k = 0; k < 6 && n; k++) {
      const t = (n.innerText || n.textContent || "").replace(/\s+/g, " ").trim();
      if (t && t.length < 200) return t;
      n = n.parentElement || (n.getRootNode() && n.getRootNode().host) || null;
    }
    return "";
  };

  const setNative = (el, text) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
                : el instanceof HTMLInputElement ? HTMLInputElement.prototype : null;
    const setter = proto && Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true, composed: true }));
  };

  // ---- textareas -----------------------------------------------------------
  const textareas = [];
  for (const root of roots) {
    for (const ta of root.querySelectorAll("textarea")) {
      const aria = ta.getAttribute("aria-label") || "";
      if (/Message AI Assistant/i.test(aria)) continue;     // the support chat widget
      textareas.push(ta);
    }
  }
  const used = new Set();
  for (const [key, text] of Object.entries(DATA)) {
    let hit = textareas.find(ta => !used.has(ta) && ctxOf(ta).toLowerCase().includes(key));
    if (!hit) { report.missing.push(key); continue; }
    used.add(hit);
    if (!DRY) setNative(hit, text);
    report.filled.push({ key, chars: text.length });
  }

  // ---- "are you using remote code?" --------------------------------------
  const radios = [];
  for (const root of roots) {
    for (const el of root.querySelectorAll('input[type="radio"],[role="radio"]')) radios.push(el);
  }
  const noRadio = radios.find(el => (el.getAttribute("aria-label") || ctxOf(el) || "")
                                    .toLowerCase().includes("not using remote code"));
  if (noRadio) {
    const box = noRadio.tagName.toLowerCase() === "input" ? noRadio
              : (noRadio.querySelector("input") || noRadio);
    const already = box.checked === true || noRadio.getAttribute("aria-checked") === "true";
    if (!already && !DRY) box.click();
    report.remote = { found: true, wasChecked: already, checkedNow: box.checked === true || noRadio.getAttribute("aria-checked") === "true" };
  } else {
    report.remote = { found: false, radios: radios.length };
  }

  // ---- certification checkboxes ------------------------------------------
  const boxes = [];
  for (const root of roots) {
    for (const el of root.querySelectorAll('input[type="checkbox"]')) boxes.push(el);
  }
  for (const want of CERTS) {
    const box = boxes.find(el => ctxOf(el).toLowerCase().startsWith(want.slice(0, 40).toLowerCase()));
    if (!box) { report.certs.push({ want: want.slice(0, 42), found: false }); continue; }
    if (!box.checked && !DRY) box.click();
    report.certs.push({ want: want.slice(0, 42), found: true, checked: box.checked });
  }

  // ---- anything that still looks like a data category must stay unchecked --
  const dataCats = boxes.filter(el => /For example:/.test(ctxOf(el)));
  report.dataCategories = { total: dataCats.length, checked: dataCats.filter(b => b.checked).length };
  report.counters = [...document.querySelectorAll("*")]
    .filter(e => !e.shadowRoot && /^0\/1000$/.test((e.textContent || "").trim())).length;
  return JSON.stringify(report);
})()
"""


def xb(*args: str) -> str:
    p = subprocess.run([sys.executable, os.path.join(ROOT, "xb.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="daily")
    ap.add_argument("--tab", required=True)
    ap.add_argument("--dry", action="store_true", help="report what it would do, change nothing")
    args = ap.parse_args()

    js = (JS.replace("__DATA__", json.dumps(FIELDS, ensure_ascii=False))
            .replace("__CERTS__", json.dumps(CERTIFICATIONS, ensure_ascii=False))
            .replace("__NO_REMOTE__", json.dumps(REMOTE_CODE_NO))
            .replace("__DRY__", "true" if args.dry else "false"))

    out = xb("--json", "--browser", args.browser, "eval", "--tab", args.tab, js)
    try:
        payload = json.loads(out)
        report = json.loads(payload["value"])
    except Exception:
        print("raw:", out[:1500])
        return 1

    print(f"filled {len(report['filled'])} textarea(s):")
    for row in report["filled"]:
        print(f"  {row['key']:<32}{row['chars']:>5} chars")
    if report["missing"]:
        print("MISSING:", ", ".join(report["missing"]))
    print("remote code:", json.dumps(report["remote"], ensure_ascii=False))
    print("certifications:")
    for c in report["certs"]:
        print(f"  {c['want']:<44}found={c['found']} checked={c.get('checked')}")
    print("data categories:", json.dumps(report["dataCategories"]))
    print("counters still at 0/1000:", report["counters"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
