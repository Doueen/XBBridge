"""Browser-free check of the toolbar panel renderer.

The panel is the one surface the e2e suite cannot reach — Edge refuses to
script extension pages, so a broken ``render()`` ships silently and only
shows up as "读取失败：…" when the user clicks the toolbar icon (v0.4.0
shipped exactly that: a local ``const t`` shadowed the i18n helper).

This runs ``extension/popup.js`` in node against a stub ``chrome`` + a
minimal DOM, feeds it realistic ``panel.state`` payloads and asserts what
the panel would actually display. Skipped (exit 0) when node is missing.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POPUP = ROOT / "extension" / "popup.js"
LOCALES = ROOT / "extension" / "_locales" / "zh_CN" / "messages.json"

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const [popupPath, localesPath] = process.argv.slice(2);
const src = fs.readFileSync(popupPath, 'utf8');
const msgs = JSON.parse(fs.readFileSync(localesPath, 'utf8'));

class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this._text = undefined;
    this.className = ''; this.title = ''; this.disabled = false;
    this.attrs = {}; this.listeners = {};
    this.classList = { on: false, toggle: (c, v) => { this.classList.on = v; } };
  }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() {
    if (this._text !== undefined) return String(this._text);
    return this.children.map((c) => c.textContent).join('');
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
}

function getMessage(key, subs) {
  const entry = msgs[key];
  if (!entry) return '';
  let msg = entry.message || '';
  const slots = [];
  for (const [name, def] of Object.entries(entry.placeholders || {})) {
    const idx = Number(String(def.content || '').replace(/[^0-9]/g, '')) - 1;
    if (idx >= 0) slots.push({ name, idx });
  }
  if (slots.length) {
    /* chrome matches $NAME$ case-insensitively */
    for (const { name, idx } of slots) {
      const val = subs && subs[idx] !== undefined ? String(subs[idx]) : '';
      msg = msg.replace(new RegExp('[$]' + name + '[$]', 'gi'), () => val);
    }
  }
  return msg;
}

async function renderOnce(state) {
  const byId = new Map();
  for (const id of ['dot', 'ver', 'totals', 'rows', 'pause', 'blocked', 'unmark-all', 'reconnect']) {
    byId.set(id, new El('div'));
  }
  const document = {
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => new El(tag),
    querySelectorAll: () => [],
  };
  const chrome = {
    i18n: { getMessage: (key, subs) => getMessage(key, subs) },
    runtime: { sendMessage: async () => ({ ok: true, result: state }) },
  };
  const sandbox = { document, chrome, setInterval: () => 0, console, URL, setTimeout };
  vm.createContext(sandbox);
  let crash = null;
  try { vm.runInContext(src, sandbox, { filename: popupPath }); }
  catch (e) { crash = String((e && e.message) || e); }
  await new Promise((r) => setTimeout(r, 30));
  const rows = byId.get('rows').children;
  return {
    crash,
    totals: byId.get('totals').textContent,
    ver: byId.get('ver').textContent,
    blocked: byId.get('blocked').textContent,
    pause: byId.get('pause').textContent,
    rows: rows.length,
    rowText: rows.length ? rows.map((r) => r.textContent).join(' | ') : '',
  };
}

const cases = [
  {
    name: 'connected with a marked row',
    state: {
      connected: true, version: '0.4.0', label: '', instanceId: 'test-instance', wsPort: 8787,
      paused: false, blocked: ['a.example'], totals: { tabs: 1, marked: 1, leased: 0 },
      rows: [{ tabId: 42, title: 'xb panel smoke', url: 'http://127.0.0.1:8899/app.html',
               marked: true, active: false, generation: 3, discarded: false }],
    },
  },
  {
    name: 'bridge offline',
    state: {
      connected: false, version: '0.4.0', label: '', instanceId: 'test-instance', wsPort: 8787,
      paused: true, blocked: [], totals: { tabs: 0, marked: 0, leased: 0 }, rows: [],
    },
  },
];

const out = [];
for (const c of cases) out.push(Object.assign({ name: c.name }, await renderOnce(c.state)));
console.log(JSON.stringify(out));
"""


def run() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node not found (panel render check needs it)")
        return 0
    for path in (POPUP, LOCALES):
        if not path.exists():
            print(f"FAIL: missing {path}")
            return 1

    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "panel_harness.mjs"
        harness.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(POPUP), str(LOCALES)],
            capture_output=True, text=True, timeout=120,
            cwd=str(tmp), encoding="utf-8", errors="replace",
        )
    if proc.returncode != 0:
        print(proc.stdout.strip())
        print(proc.stderr.strip()[-2000:])
        print("FAIL: harness did not run")
        return 1

    try:
        results = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        print(proc.stdout.strip()[:2000])
        print(f"FAIL: unreadable harness output ({exc})")
        return 1

    failures = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  [{'ok' if cond else 'FAIL'}] {name}{'' if cond else ' — ' + detail}")
        if not cond:
            failures.append(name)

    for res in results:
        label = res["name"]
        print(f"- {label}")
        check(f"{label}: no crash", not res["crash"], res["crash"] or "")
        check(f"{label}: no throw leaked into totals",
              "\u8bfb\u53d6\u5931\u8d25" not in res["totals"], res["totals"])
        check(f"{label}: no unsubstituted placeholder",
              "$" not in res["totals"] + res["blocked"] + res["pause"] + res["rowText"],
              f'{res["totals"]} {res["blocked"]} {res["rowText"]}')

    first, offline = results
    check("connected: totals shows the bridge port", "8787" in first["totals"], first["totals"])
    check("connected: tab count is rendered", "1 \u4e2a\u6807\u7b7e" in first["totals"], first["totals"])
    check("connected: one row rendered", first["rows"] == 1, str(first["rows"]))
    check("connected: row carries the tab title",
          "xb panel smoke" in first["rowText"], first["rowText"])
    check("connected: row offers a focus button",
          "\u5207\u5230\u524d\u53f0" in first["rowText"], first["rowText"])
    check("connected: blocked count rendered",
          "1" in first["blocked"] and "\u5c4f\u853d" in first["blocked"], first["blocked"])
    check("offline: totals says not connected",
          "\u672a\u8fde\u63a5" in offline["totals"], offline["totals"])
    check("offline: empty state explains the missing bridge",
          offline["rows"] == 1 and "\u6865\u6ca1\u8fde\u4e0a" in offline["rowText"], offline["rowText"])
    check("offline: pause button flips to resume",
          "\u6062\u590d" in offline["pause"], offline["pause"])

    if failures:
        print(f"\n{len(failures)} panel check(s) failed")
        return 1
    print("\npanel render: ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(run())
