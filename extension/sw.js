'use strict';
/*
 * xb — Edge/Chromium Agent Bridge (service worker)
 *
 * Connects to the local bridge daemon over WebSocket, executes commands with
 * chrome.tabs / chrome.tabGroups / chrome.scripting / chrome.debugger.
 *
 * Protocol:  {id, cmd, args}  ->  {id, ok:true, result} | {id, ok:false, error}
 */

const WS_PORT = 8787;
const RECONNECT_MIN = 800;
const RECONNECT_MAX = 15000;
const KEEPALIVE_ALARM = 'xb-keepalive';

let ws = null;
let backoff = RECONNECT_MIN;
let connecting = false;
let stats = { connectedAt: null, lastCmdAt: null, cmds: 0, errors: 0 };

const log = (...a) => console.log('[xb]', ...a);

/* --------------------------------------------------------------- errors */

/*
 * Every failure carries a machine-readable contract:
 *   code      stable identifier the CLI maps to an exit code
 *   outcome   not_started | applied | unknown  — did the page change?
 *   retryable safe to run again without duplicating a side effect
 * Callers must never retry outcome=applied blindly: a second submit/click
 * would be applied twice.
 */
class XbError extends Error {
  constructor(code, message, opts) {
    super(message);
    const o = opts || {};
    this.name = 'XbError';
    this.code = code;
    this.outcome = o.outcome || 'not_started';
    this.retryable = !!o.retryable;
    this.details = o.details;
  }
}

function fail(code, message, opts) { return new XbError(code, message, opts); }

const ERROR_PATTERNS = [
  [/viewport is 0x0|window is minimized/i, 'window_minimized'],
  [/unknown command/i, 'unsupported_method'],
  [/no active tab/i, 'no_target_tab'],
  [/No tab with id|Invalid tab ID|tab was closed|Tab not found/i, 'no_target_tab'],
  [/Cannot access contents of|The extensions gallery cannot be scripted|chrome:\/\/|edge:\/\/|Cannot access a/i, 'restricted_page'],
  [/re-run snapshot|no element with data-xb-ref|element not found|selector not found|no longer available/i, 'target_not_found'],
  [/timeout|timed out/i, 'timeout'],
  [/stale|generation/i, 'stale_generation'],
  [/not owned|another (client|session)/i, 'tab_not_owned'],
  [/already attached|Another debugger|debugger is not attached|Detached while/i, 'debugger_conflict'],
  [/requires|required|must be one of|invalid|unsupported/i, 'invalid_params']
];

function classifyError(message) {
  for (const [re, code] of ERROR_PATTERNS) if (re.test(message)) return code;
  return 'command_failed';
}

function errorPayload(e) {
  if (e instanceof XbError) {
    const out = { code: e.code, message: e.message, outcome: e.outcome, retryable: e.retryable };
    if (e.details !== undefined) out.details = e.details;
    return out;
  }
  const message = (e && e.message) ? e.message : String(e);
  const code = classifyError(message);
  return {
    code,
    message,
    // a command that threw before replying has not changed the page, unless the
    // failure says otherwise (timeouts leave the outcome genuinely unknown)
    outcome: code === 'timeout' ? 'unknown' : 'not_started',
    retryable: code === 'timeout' || code === 'debugger_conflict'
  };
}

/* ------------------------------------------------------------------ socket */

function connect() {
  if (connecting) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  connecting = true;
  let sock;
  try {
    sock = new WebSocket(`ws://127.0.0.1:${WS_PORT}`);
  } catch (e) {
    connecting = false;
    return retry();
  }
  ws = sock;
  sock.onopen = () => {
    connecting = false;
    backoff = RECONNECT_MIN;
    stats.connectedAt = Date.now();
    log('connected');
    try {
      sock.send(JSON.stringify({
        hello: {
          role: 'extension',
          name: 'xb',
          version: chrome.runtime.getManifest().version,
          browser: navigator.userAgent,
          stats
        }
      }));
    } catch (e) { /* ignore */ }
  };
  sock.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    handle(msg);
  };
  sock.onclose = () => { connecting = false; stats.connectedAt = null; retry(); };
  sock.onerror = () => { try { sock.close(); } catch (e) {} };
}

function retry() {
  const delay = backoff;
  backoff = Math.min(backoff * 2, RECONNECT_MAX);
  setTimeout(connect, delay);
}

function reply(id, ok, payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const msg = ok ? { id, ok: true, result: payload } : { id, ok: false, error: payload };
  ws.send(JSON.stringify(msg));
}

async function handle(msg) {
  stats.cmds += 1;
  stats.lastCmdAt = Date.now();
  if (!msg || !msg.cmd) return;
  const id = msg.id;
  try {
    const fn = COMMANDS[msg.cmd];
    if (!fn) throw fail('unsupported_method', `unknown command: ${msg.cmd}`);
    const result = await fn(msg.args || {});
    if (result && result.ok === false && result.error) throw result.error;
    reply(id, true, result);
  } catch (e) {
    stats.errors += 1;
    const payload = errorPayload(e);
    stats.lastError = payload;
    reply(id, false, payload);
  }
}

/* ---------------------------------------------------------------- helpers */

async function getTab(tabId, guardArgs) {
  let tab;
  if (tabId === undefined || tabId === null) {
    const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!t) throw new Error('no active tab');
    tab = t;
  } else {
    tab = await chrome.tabs.get(Number(tabId));
  }
  if (guardArgs) guardTab(tab, guardArgs);
  return tab;
}

/* ---- generations and leases ----
 * A generation is bumped whenever the tab navigates. A caller that pins
 * expectGeneration gets stale_generation instead of acting on a document it
 * never inspected. A lease says "this tab is mine right now": other callers are
 * refused until it expires or is released.
 */
const GEN = new Map();        // tabId -> navigation counter
const LEASES = new Map();     // tabId -> {token, label, generation, claimedAt, expiresAt}
const LEASE_TTL_MS = 2 * 60 * 1000;

function bumpGeneration(tabId) {
  GEN.set(tabId, (GEN.get(tabId) || 0) + 1);
  return GEN.get(tabId);
}

function generationOf(tabId) { return GEN.get(tabId) || 0; }

function leaseView(tabId, l) {
  return { tabId, token: l.token, label: l.label || '', generation: l.generation,
           ageMs: Date.now() - l.claimedAt, expiresInMs: Math.max(0, l.expiresAt - Date.now()) };
}

function guardTab(tab, a) {
  const args = a || {};
  const lease = LEASES.get(Number(tab.id));
  if (lease) {
    if (args.lease !== lease.token) {
      throw fail('tab_not_owned',
        `tab ${tab.id} is claimed by another client${lease.label ? ' (' + lease.label + ')' : ''}; `
        + 'release it or pass its lease token', {});
    }
    lease.expiresAt = Date.now() + LEASE_TTL_MS;
  }
  if (args.expectGeneration !== undefined && Number(args.expectGeneration) !== generationOf(tab.id)) {
    throw fail('stale_generation',
      `tab ${tab.id} navigated since generation ${args.expectGeneration} `
      + `(now ${generationOf(tab.id)}); re-inspect before acting`, {});
  }
  return tab;
}

function reclaimLeases() {
  const now = Date.now();
  for (const [tabId, lease] of Array.from(LEASES.entries())) {
    if (lease.expiresAt > now) continue;
    LEASES.delete(tabId);
    clearMark(tabId).catch(() => {});
  }
}
setInterval(reclaimLeases, 15 * 1000);

function shapeTab(t) {
  return {
    id: t.id,
    windowId: t.windowId,
    index: t.index,
    groupId: t.groupId === undefined ? -1 : t.groupId,
    active: !!t.active,
    pinned: !!t.pinned,
    audible: !!t.audible,
    status: t.status,
    generation: generationOf(t.id),
    title: t.title,
    url: t.url
  };
}

function shapeGroup(g) {
  return {
    id: g.id,
    windowId: g.windowId,
    title: g.title,
    color: g.color,
    collapsed: g.collapsed,
    shared: g.shared
  };
}

const COLORS = ['grey', 'blue', 'red', 'yellow', 'green', 'pink', 'purple', 'cyan'];

/* injected page functions (must be self-contained) */

function _pageText(sel) {
  const el = sel ? document.querySelector(sel) : document.body;
  if (!el) return { error: 'selector not found: ' + sel };
  return { url: location.href, title: document.title, text: el.innerText || '' };
}

function _pageHtml(sel) {
  const el = sel ? document.querySelector(sel) : document.documentElement;
  if (!el) return { error: 'selector not found: ' + sel };
  return { url: location.href, title: document.title, html: el.outerHTML };
}

function _pageSnapshot() {
  const SEL = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[onclick],[contenteditable=true]';
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
    return r.width > 0 && r.height > 0;
  };
  document.querySelectorAll('[data-xb-i]').forEach((el) => el.removeAttribute('data-xb-i'));
  const els = Array.from(document.querySelectorAll(SEL)).filter(vis).slice(0, 400);
  const items = els.map((el, i) => {
    el.setAttribute('data-xb-i', String(i));
    const name = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    const item = {
      i,
      tag: el.tagName.toLowerCase(),
      name,
      type: el.getAttribute('type') || undefined,
      href: el.href || undefined,
      disabled: el.disabled || undefined
    };
    return item;
  });
  const headings = Array.from(document.querySelectorAll('h1,h2,h3')).slice(0, 40)
    .map((h) => `${h.tagName.toLowerCase()}: ${(h.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100)}`);
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 6000),
    headings,
    items
  };
}

function _pageClick(i) {
  const el = document.querySelector(`[data-xb-i="${i}"]`);
  if (!el) return { error: 'no element with data-xb-i=' + i + ' (re-run snapshot)' };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const target = document.elementFromPoint(cx, cy) || el;
  target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: cx, clientY: cy }));
  target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, clientX: cx, clientY: cy }));
  target.click();
  return { clicked: { tag: target.tagName.toLowerCase(), text: (target.innerText || '').slice(0, 80) } };
}

function _pageFill(i, text) {
  const el = document.querySelector(`[data-xb-i="${i}"]`);
  if (!el) return { error: 'no element with data-xb-i=' + i + ' (re-run snapshot)' };
  el.scrollIntoView({ block: 'center' });
  el.focus();
  if (el.isContentEditable) {
    el.innerText = text;
  } else {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) setter.set.call(el, text); else el.value = text;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { filled: el.tagName.toLowerCase(), value: el.value !== undefined ? el.value : text };
}

function _pageQuery(sel) {
  const nodes = Array.from(document.querySelectorAll(sel)).slice(0, 200);
  return nodes.map((el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 120),
      href: el.href || undefined,
      box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }
    };
  });
}

async function attachDebugger(tabId) {
  const s = dbgState(tabId);
  if (s.attached) { touchDbg(tabId); return s; }
  try {
    await chrome.debugger.attach({ tabId }, '1.3');
    s.own = true;
  } catch (e) {
    const msg = String(e.message || e);
    // DevTools (or some other client) already holds it: use it, but never detach it
    if (!/already attached|Another debugger/i.test(msg)) throw e;
    s.own = false;
  }
  s.attached = true;
  touchDbg(tabId);
  return s;
}

async function withDebugger(tabId, fn, keepAttached) {
  const s = await attachDebugger(tabId);
  try {
    return await fn({ tabId });
  } finally {
    if (!keepAttached) {
      if (s.own) {
        try { await chrome.debugger.detach({ tabId }); } catch (e) {}
        s.attached = false;
      } else {
        touchDbg(tabId);
      }
    }
  }
}

async function inject(tabId, func, args, frameId) {
  const tab = await getTab(tabId);
  if (tab.discarded) throw fail('tab_discarded', `tab ${tab.id} is discarded (sleeping); reload it first`);
  const target = { tabId: tab.id };
  if (frameId !== undefined && frameId !== null && Number(frameId) !== 0) target.frameIds = [Number(frameId)];
  const res = await chrome.scripting.executeScript({
    target,
    world: 'ISOLATED',
    func,
    args: args || []
  });
  const first = res && res[0];
  if (!first) throw new Error('script produced no result');
  if (first.error) throw new Error(String(first.error.message || first.error));
  return first.result;
}

/* ---- v0.2 additions: refs, rects, waits, keys, buffers, frames ---- */

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function timeoutAfter(ms, message) {
  return sleep(ms).then(() => { throw fail('timeout', message, { retryable: true }); });
}

/* Injections into a discarded or privileged page can never resolve; bound them so
 * cosmetic work (markers, pulses) can never hang a command. */
async function safeInject(tabId, func, args, frameId, ms) {
  try {
    const tab = await chrome.tabs.get(Number(tabId));
    if (tab.discarded) return null;
    if (/^(chrome|edge|about|devtools|view-source|extension):/i.test(tab.url || '')) return null;
    return await Promise.race([
      inject(tabId, func, args, frameId),
      timeoutAfter(ms === undefined ? 4000 : ms, 'the page did not answer an injected helper')
    ]);
  } catch (e) {
    return null;
  }
}

/* refs: "@e12" (main frame) or "@e12f4711" (frame 4711), or a plain css selector */
function parseRef(ref) {
  if (typeof ref !== 'string') return null;
  const m = /^@e(\d+)(?:f(\d+))?$/.exec(ref.trim());
  if (!m) return null;
  return { i: Number(m[1]), frameId: m[2] === undefined ? 0 : Number(m[2]) };
}

function refSelector(i) { return `[data-xb-ref="${i}"]`; }

/* run an injected function against ref|selector on the right frame */
/* a tab right after `navigate` may still be loading: goBack/goForward refuse then */
async function tabSettled(tabId, timeoutMs) {
  const limit = timeoutMs === undefined ? 3000 : timeoutMs;
  const started = Date.now();
  let t = await chrome.tabs.get(tabId);
  while (t.status !== 'complete' && Date.now() - started < limit) {
    await sleep(120);
    t = await chrome.tabs.get(tabId);
  }
  return t;
}

async function goHistory(a, dir) {
  const tab0 = await getTab(a.tabId);
  await tabSettled(tab0.id, 3000);
  const before = (await chrome.tabs.get(tab0.id)).url;
  for (let i = 0; i < 4; i++) {
    try {
      if (dir === 'back') await chrome.tabs.goBack(tab0.id);
      else await chrome.tabs.goForward(tab0.id);
      return { tabId: tab0.id, dir, via: 'tabs.' + (dir === 'back' ? 'goBack' : 'goForward'), attempts: i + 1 };
    } catch (e) {
      await sleep(300);
    }
  }
  // last resort: the in-page history API (works while the entry is still settling)
  await inject(tab0.id, (d) => { if (d === 'back') history.back(); else history.forward(); return true; }, [dir]);
  const started = Date.now();
  while (Date.now() - started < 2500) {
    await sleep(150);
    const t = await chrome.tabs.get(tab0.id);
    if (t.status === 'complete' && t.url !== before) {
      return { tabId: t.id, dir, via: 'history.' + dir, url: t.url };
    }
  }
  throw new Error(dir === 'back' ? 'no history to go back to (the tab was opened directly)' : 'no forward history entry');
}

async function onTarget(a, func, extraArgs) {
  const raw = a.ref !== undefined ? a.ref : a.selector;
  const ref = parseRef(raw);
  let selector, frameId = 0;
  if (ref) { selector = refSelector(ref.i); frameId = ref.frameId; }
  else if (typeof raw === 'string' && raw.length) selector = raw;
  else throw new Error('ref or selector required (e.g. @e12 or "#submit")');
  const tab = await getTab(a.tabId, a);
  const out = await inject(tab.id, func, [selector].concat(extraArgs || []), frameId);
  if (out && out.error) throw new Error(out.error);
  return { tab, out, selector, frameId };
}

function _fClickSel(sel) {
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel + ' (re-run snapshot)' };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const target = document.elementFromPoint(cx, cy) || el;
  for (const type of ['mousedown', 'mouseup']) {
    target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, clientX: cx, clientY: cy }));
  }
  target.click();
  return { clicked: { tag: target.tagName.toLowerCase(), text: (target.innerText || '').slice(0, 80) } };
}

function _fFillSel(sel, text) {
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel + ' (re-run snapshot)' };
  el.scrollIntoView({ block: 'center' });
  el.focus();
  if (el.isContentEditable) {
    el.innerText = text;
  } else {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) setter.set.call(el, text); else el.value = text;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { filled: el.tagName.toLowerCase(), value: el.value !== undefined ? el.value : text };
}

function _fSelect(sel, values) {
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel };
  if (el.tagName.toLowerCase() !== 'select') return { error: 'not a <select>: ' + el.tagName.toLowerCase() };
  const want = (Array.isArray(values) ? values : [values]).map(String);
  let matched = 0;
  for (const opt of Array.from(el.options)) {
    const hit = want.includes(opt.value) || want.includes((opt.textContent || '').trim());
    if (hit) { opt.selected = true; matched += 1; }
    else if (!el.multiple) { opt.selected = false; }
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { values: want, matched, value: el.value };
}

function _fFocus(sel, on) {
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel };
  if (on) el.focus(); else el.blur();
  return { active: document.activeElement === el };
}

function _fScrollTo(sel) {
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel };
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  return { y: Math.round(window.scrollY), x: Math.round(window.scrollX),
           box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } };
}

function _fBox(sel) {
  if (window.innerWidth === 0 || window.innerHeight === 0) {
    return { error: 'viewport is 0x0 — the browser window is minimized or collapsed; restore it before using coordinates' };
  }
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
           w: Math.round(r.width), h: Math.round(r.height), vw: window.innerWidth, vh: window.innerHeight };
}

function _fWaitElement(sel, state, timeoutMs) {
  const test = () => {
    const el = document.querySelector(sel);
    const visible = !!el && (() => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    })();
    if (state === 'attached') return !!el;
    if (state === 'detached') return !el;
    if (state === 'hidden') return !visible;
    return visible;
  };
  return new Promise((resolve) => {
    const started = Date.now();
    if (test()) return resolve({ ok: true, ms: 0, state });
    const timer = setInterval(() => {
      if (test()) { clearInterval(timer); resolve({ ok: true, ms: Date.now() - started, state }); return; }
      if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        resolve({ ok: false, ms: Date.now() - started, state, error: 'timeout waiting for ' + state + ': ' + sel });
      }
    }, 100);
  });
}

function _fExtract() {
  const tables = Array.from(document.querySelectorAll('table')).map((t, ti) => {
    const rows = Array.from(t.rows).map((r) => Array.from(r.cells).map((c) => (c.innerText || '').replace(/\s+/g, ' ').trim()));
    const headRows = t.tHead ? t.tHead.rows.length : 0;
    const headers = headRows ? Array.from(t.tHead.rows[headRows - 1].cells).map((c) => (c.innerText || '').trim()) : (rows[0] || []);
    return { index: ti, headers, rows: rows.slice(headRows ? headRows : 1),
             rowCount: Math.max(0, rows.length - (headRows ? headRows : 1)), colCount: headers.length };
  }).filter((t) => t.rowCount > 0 || t.headers.length);
  const lists = Array.from(document.querySelectorAll('ul,ol')).slice(0, 15).map((l) => ({
    ordered: l.tagName.toLowerCase() === 'ol',
    items: Array.from(l.children).slice(0, 300).map((li) => (li.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean)
  })).filter((l) => l.items.length > 1);
  return { url: location.href, title: document.title, tables, lists };
}

function _fObserve() {
  const count = (sel) => document.querySelectorAll(sel).length;
  return {
    url: location.href, title: document.title,
    landmarks: {
      header: count('header,[role=banner]'), nav: count('nav,[role=navigation]'), main: count('main,[role=main]'),
      aside: count('aside,[role=complementary]'), footer: count('footer,[role=contentinfo]'), forms: count('form')
    },
    headings: Array.from(document.querySelectorAll('h1,h2,h3,h4')).slice(0, 60)
      .map((h) => `${h.tagName.toLowerCase()}: ${(h.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100)}`),
    controls: {
      links: count('a[href]'), buttons: count('button,[role=button]'), inputs: count('input'),
      selects: count('select'), textareas: count('textarea'), iframes: count('iframe'), images: count('img')
    },
    textSample: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 4000)
  };
}

function _fViewport() { return { w: window.innerWidth, h: window.innerHeight, y: Math.round(window.scrollY) }; }

function _fPageBox(sel) {
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  return {
    x: r.left + window.scrollX, y: r.top + window.scrollY,
    w: r.width, h: r.height,
    dpr: window.devicePixelRatio || 1,
    doc: { w: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0),
           h: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0) }
  };
}

function _fCenter() { return { x: Math.round(window.innerWidth / 2), y: Math.round(window.innerHeight / 2) }; }

/* Scroll to the element, then report where it really is and whether a real
 * mouse event at that point would actually land on it. */
function _fTrustedBox(sel) {
  if (window.innerWidth === 0 || window.innerHeight === 0) {
    return { error: 'viewport is 0x0 — the browser window is minimized or collapsed; restore it before using coordinates' };
  }
  const el = document.querySelector(sel);
  if (!el) return { error: 'element not found: ' + sel };
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  const x = Math.round(r.left + r.width / 2);
  const y = Math.round(r.top + r.height / 2);
  const at = document.elementFromPoint(x, y);
  const hit = !!at && (at === el || el.contains(at) || at.contains(el));
  return { x, y, w: Math.round(r.width), h: Math.round(r.height), hit,
           at: at ? (at.tagName.toLowerCase() + (at.id ? '#' + at.id : '')) : null,
           scrollY: Math.round(window.scrollY) };
}

function _fHelpOverlay(message, id) {
  const host = document.createElement('div');
  host.attachShadow({ mode: 'open' });
  host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:auto;';
  host.shadowRoot.innerHTML = '<style>' +
    '.wrap{position:fixed;left:50%;top:24px;transform:translateX(-50%);max-width:520px;font:14px/1.5 system-ui,sans-serif;' +
    'background:#111;color:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 8px 30px rgba(0,0,0,.35);display:flex;gap:12px;align-items:flex-start}' +
    '.msg{flex:1;white-space:pre-wrap}button{font:inherit;background:#2f6feb;color:#fff;border:0;border-radius:6px;padding:6px 14px;cursor:pointer}' +
    '</style><div class="wrap"><div class="msg"></div><button id="xb-done">我已处理完</button></div>';
  host.shadowRoot.querySelector('.msg').textContent = message;
  host.id = 'xb-help-' + id;
  host.shadowRoot.querySelector('#xb-done').addEventListener('click', () => {
    window['__xbHelpDone_' + id] = true;
    host.remove();
  });
  document.documentElement.appendChild(host);
  return { shown: true };
}

function _fHelpPoll(id) { return { done: window['__xbHelpDone_' + id] === true }; }

/* ---- control markers: make an agent-driven tab visible to the human ----
 * Everything lives in a shadow root so page CSS can't restyle it, and every
 * element is pointer-events:none so it can never swallow a real click.
 */
function _fMark(opts) {
  const o = opts || {};
  const ID = 'xb-mark-root';
  const old = document.getElementById(ID);
  if (old) old.remove();
  const root = document.createElement('div');
  root.id = ID;
  // the label lives on the DOM, not on window: CDP eval runs in the main world
  // and must be able to read it back while the overlay was painted from ISOLATED
  root.setAttribute('data-xb-label', o.label || '');
  root.setAttribute('data-xb-marked-at', String(Date.now()));
  Object.assign(root.style, {
    all: 'initial', position: 'fixed', inset: '0', width: '100vw', height: '100vh',
    pointerEvents: 'none', zIndex: '2147483647'
  });
  const sh = root.attachShadow({ mode: 'open' });
  const style = document.createElement('style');
  style.textContent =
    '.frame{position:fixed;inset:0;box-sizing:border-box;border:2px solid rgba(20,190,160,.85);' +
    'box-shadow:inset 0 0 16px rgba(20,190,160,.22),inset 0 0 40px rgba(20,190,160,.10);' +
    'animation:xbBreathe 2.4s ease-in-out infinite alternate}' +
    '.chip{position:fixed;left:12px;top:12px;display:flex;align-items:center;gap:7px;' +
    'font:12px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;letter-spacing:.2px;' +
    'background:rgba(9,26,23,.88);color:#8af3da;padding:5px 11px;border-radius:999px;' +
    'border:1px solid rgba(20,190,160,.55);box-shadow:0 2px 10px rgba(0,0,0,.25)}' +
    '.dot{width:7px;height:7px;border-radius:50%;background:#2ee6b8;box-shadow:0 0 7px #2ee6b8;' +
    'animation:xbBlink 1.3s ease-in-out infinite}' +
    '@keyframes xbBreathe{from{opacity:.42}to{opacity:.95}}' +
    '@keyframes xbBlink{0%,100%{opacity:1}50%{opacity:.25}}';
  const frame = document.createElement('div');
  frame.className = 'frame';
  const chip = document.createElement('div');
  chip.className = 'chip';
  const dot = document.createElement('span');
  dot.className = 'dot';
  chip.append(dot, document.createTextNode(o.label || 'xb 正在操作这个标签'));
  sh.append(style, frame, chip);
  (document.documentElement || document.body).appendChild(root);
  window.__xbMark = { at: Date.now(), label: o.label || '' };
  return { marked: true, label: o.label || '', overlayId: ID };
}

function _fUnmark() {
  const el = document.getElementById('xb-mark-root');
  if (el) el.remove();
  try { delete window.__xbMark; } catch (e) { window.__xbMark = undefined; }
  return { marked: false, removed: !!el };
}

function _fMarkState() {
  const el = document.getElementById('xb-mark-root');
  return {
    marked: !!el,
    label: el ? (el.getAttribute('data-xb-label') || '') : null,
    markedAt: el ? Number(el.getAttribute('data-xb-marked-at') || 0) : null
  };
}

/* One short ring at the point that is about to be acted on. */
function _fPulse(o) {
  const p = o || {};
  const x = Number(p.x) || 0, y = Number(p.y) || 0;
  const w = Number(p.w) || 0, h = Number(p.h) || 0;
  const root = document.createElement('div');
  root.id = 'xb-pulse-root';
  Object.assign(root.style, {
    all: 'initial', position: 'fixed', inset: '0', pointerEvents: 'none', zIndex: '2147483646'
  });
  const sh = root.attachShadow({ mode: 'open' });
  const box = document.createElement('div');
  const size = Math.max(26, Math.min(120, Math.max(w, h) || 34));
  const cx = w ? x : x, cy = h ? y : y;
  Object.assign(box.style, {
    position: 'fixed', left: `${Math.round(cx - size / 2)}px`, top: `${Math.round(cy - size / 2)}px`,
    width: `${size}px`, height: `${size}px`, borderRadius: '50%',
    border: '2px solid rgba(46,230,184,.95)', boxSizing: 'border-box',
    boxShadow: '0 0 12px rgba(46,230,184,.65), inset 0 0 8px rgba(46,230,184,.35)'
  });
  sh.append(box);
  (document.documentElement || document.body).appendChild(root);
  const done = () => root.remove();
  try {
    const anim = box.animate(
      [{ transform: 'scale(.45)', opacity: 1 }, { transform: 'scale(1.35)', opacity: 0 }],
      { duration: 620, easing: 'ease-out' }
    );
    anim.onfinish = done;
    setTimeout(done, 900);
  } catch (e) { setTimeout(done, 320); }
  return { pulsed: true };
}

/* Tint the favicon while the tab is agent-controlled (survives navigation). */
function _fFavicon(o) {
  const p = o || {};
  const ID = 'xb-favicon-link';
  const html = document.documentElement;
  if (p.off) {
    const made = document.getElementById(ID);
    if (made) made.remove();
    const original = html.getAttribute('data-xb-favicon');
    if (original !== null) {
      const link = document.createElement('link');
      link.rel = 'icon';
      link.href = original;
      document.head.appendChild(link);
      html.removeAttribute('data-xb-favicon');
    }
    return { favicon: 'restored' };
  }
  const links = Array.from(document.querySelectorAll('link[rel~="icon"],link[rel="shortcut icon"]'));
  if (!html.getAttribute('data-xb-favicon')) {
    html.setAttribute('data-xb-favicon', links.length ? links[links.length - 1].href : '');
  }
  const c = document.createElement('canvas');
  c.width = 32; c.height = 32;
  const g = c.getContext('2d');
  if (g) {
    g.fillStyle = p.color || '#0d9488';
    g.beginPath(); g.arc(16, 16, 15, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#eafff8';
    g.font = 'bold 19px -apple-system,system-ui,sans-serif';
    g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText('x', 16, 17);
  }
  const href = g ? c.toDataURL('image/png') : '';
  if (!href) return { favicon: 'unsupported' };
  let made = document.getElementById(ID);
  if (!made) {
    made = document.createElement('link');
    made.id = ID;
    made.rel = 'icon';
    document.head.appendChild(made);
  }
  made.href = href;
  return { favicon: 'tinted' };
}

/* ---- mark registry: which tabs the agent currently controls ---- */

const MARKS = new Map();          // tabId -> {label, ts, favicon}
const MARK_TTL_MS = 15 * 60 * 1000;
const MARK_STORE_KEY = 'xbMarks';

function markView(tabId, m) {
  return { tabId, label: m.label || '', ageMs: Date.now() - m.ts, ttlMs: MARK_TTL_MS,
           favicon: m.favicon !== false };
}

async function persistMarks() {
  try {
    const rows = Array.from(MARKS.entries()).map(([tabId, m]) => ({ tabId, label: m.label, ts: m.ts }));
    await chrome.storage.session.set({ [MARK_STORE_KEY]: rows });
  } catch (e) { /* session storage is best-effort */ }
}

async function restoreMarks() {
  try {
    const got = await chrome.storage.session.get(MARK_STORE_KEY);
    const rows = got && got[MARK_STORE_KEY];
    if (!Array.isArray(rows)) return;
    for (const row of rows) {
      if (Date.now() - (row.ts || 0) > MARK_TTL_MS) continue;
      const tabId = Number(row.tabId);
      try {
        await chrome.tabs.get(tabId);
      } catch (e) { continue; }   // tab died while the worker was asleep
      await paintMark(tabId, row.label).catch(() => {});
      MARKS.set(tabId, { label: row.label || '', ts: row.ts || Date.now(), favicon: true });
    }
  } catch (e) { /* ignore */ }
}

async function paintMark(tabId, label) {
  const out = await safeInject(tabId, _fMark, [{ label: label || '' }]);
  await safeInject(tabId, _fFavicon, [{}]);
  return out;
}

async function clearMark(tabId) {
  await safeInject(tabId, _fUnmark, []);
  await safeInject(tabId, _fFavicon, [{ off: true }]);
  MARKS.delete(tabId);
  await persistMarks();
}

/** Mark a tab once; re-marking only updates the label. */
async function ensureMark(tabId, label) {
  const existing = MARKS.get(tabId);
  if (existing) {
    if (label && label !== existing.label) {
      existing.label = label;
      existing.ts = Date.now();
      await paintMark(tabId, label).catch(() => {});
      await persistMarks();
    }
    return { ...markView(tabId, existing), reused: true };
  }
  await paintMark(tabId, label);
  const rec = { label: label || '', ts: Date.now(), favicon: true };
  MARKS.set(tabId, rec);
  await persistMarks();
  return markView(tabId, rec);
}

async function pulseAt(tabId, selector, frameId) {
  try {
    const box = await safeInject(tabId, _fBox, [selector], frameId);
    if (box && !box.error) {
      await safeInject(tabId, _fPulse, [{ x: box.x, y: box.y, w: box.w, h: box.h }]);
    }
  } catch (e) { /* a pulse must never fail the action */ }
}

/** Mutating commands call this: the human can always see which tab is in use. */
async function markTab(tab, a) {
  if (a && a.noMark === true) return false;
  const m = await ensureMark(tab.id, a && a.markLabel).catch(() => null);
  return !!m;
}

/* Resolve "x,y", a CSS selector, or @eN into viewport coordinates. */
async function resolvePoint(tabId, spec) {
  if (spec === undefined || spec === null || spec === '') {
    throw fail('invalid_params', 'point required: "x,y", a CSS selector or @eN');
  }
  if (typeof spec === 'object') {
    const x = Number(spec.x), y = Number(spec.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) throw fail('invalid_params', 'point needs numeric x and y');
    return { x, y, via: 'object' };
  }
  const text = String(spec).trim();
  const m = /^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$/.exec(text);
  if (m) return { x: Number(m[1]), y: Number(m[2]), via: 'coords' };
  const ref = parseRef(text);
  const selector = ref ? refSelector(ref.i) : text;
  const box = await inject(tabId, _fBox, [selector], ref ? ref.frameId : 0);
  if (box && box.error) throw fail('target_not_found', box.error);
  return { x: box.x, y: box.y, w: box.w, h: box.h, via: 'element' };
}

/** Real input needs the page to believe it is focused: an offscreen/background
 *  window otherwise drops mousePressed/Released while keeping mouseMoved. */
async function focusEmulation(t) {
  try { await chrome.debugger.sendCommand(t, 'Emulation.setFocusEmulationEnabled', { enabled: true }); }
  catch (e) { /* older builds: real clicks then need a focused window */ }
}

const mousePoint = (p) => ({ x: Math.round(p.x), y: Math.round(p.y) });

setInterval(() => {
  const now = Date.now();
  let changed = false;
  for (const [tabId, m] of Array.from(MARKS.entries())) {
    if (now - m.ts > MARK_TTL_MS) { MARKS.delete(tabId); changed = true; }
  }
  if (changed) persistMarks().catch(() => {});
}, 60 * 1000);

/* ----------------------------------------------------------------- debugger */

const DBG = new Map();   // tabId -> {console:[], network:[], netIds:Map, timer, own, attached}
const DBG_IDLE_MS = 10 * 60 * 1000;
const MAX_BUFFER = 500;

function dbgState(tabId) {
  let s = DBG.get(tabId);
  if (!s) {
    s = { console: [], network: [], netIds: new Map(), timer: null, own: false, attached: false };
    DBG.set(tabId, s);
  }
  return s;
}

function touchDbg(tabId) {
  const s = dbgState(tabId);
  if (s.timer) clearTimeout(s.timer);
  s.timer = setTimeout(() => { detachDebugger(tabId).catch(() => {}); }, DBG_IDLE_MS);
}

async function detachDebugger(tabId) {
  const s = DBG.get(tabId);
  if (!s) return;
  if (s.timer) clearTimeout(s.timer);
  if (s.attached && s.own) {
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }
  s.attached = false;
  s.timer = null;
}

function pushBuf(arr, item) {
  arr.push(item);
  if (arr.length > MAX_BUFFER) arr.splice(0, arr.length - MAX_BUFFER);
}

function preview(v) {
  if (v === undefined) return undefined;
  if (v && typeof v === 'object' && 'value' in v) return preview(v.value);
  if (v && typeof v === 'object' && 'description' in v) return String(v.description).slice(0, 400);
  try {
    const s = typeof v === 'string' ? v : JSON.stringify(v);
    return s === undefined ? undefined : String(s).slice(0, 400);
  } catch (e) { return String(v).slice(0, 200); }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const tabId = source && source.tabId;
  if (tabId === undefined) return;
  const s = DBG.get(tabId);
  if (!s) return;
  const ts = Date.now();
  try {
    if (method === 'Runtime.consoleAPICalled') {
      pushBuf(s.console, {
        kind: 'console', ts, level: params.type,
        text: (params.args || []).map((a) => preview(a)).filter((x) => x !== undefined).join(' ')
      });
    } else if (method === 'Runtime.exceptionThrown') {
      const d = params.exceptionDetails || {};
      pushBuf(s.console, { kind: 'exception', ts, level: 'error',
        text: (d.exception && d.exception.description) || d.text || 'exception', url: d.url, line: d.lineNumber });
    } else if (method === 'Log.entryAdded') {
      const e = params.entry || {};
      pushBuf(s.console, { kind: 'log', ts, level: e.level, text: e.text, url: e.url });
    } else if (method === 'Network.requestWillBeSent') {
      s.netIds.set(params.requestId, {
        id: params.requestId, ts, method: params.request.method, url: params.request.url,
        type: (params.type || '').toLowerCase(), status: null, ok: null, error: null, size: null
      });
    } else if (method === 'Network.responseReceived') {
      const r = s.netIds.get(params.requestId);
      if (r) {
        r.status = params.response.status;
        r.mime = params.response.mimeType;
        r.ok = params.response.status < 400;
        r.fromCache = !!params.response.fromDiskCache;
      }
    } else if (method === 'Network.loadingFinished') {
      const r = s.netIds.get(params.requestId);
      if (r) { r.size = params.encodedDataLength; pushBuf(s.network, r); s.netIds.delete(params.requestId); }
    } else if (method === 'Network.loadingFailed') {
      const r = s.netIds.get(params.requestId);
      if (r) { r.ok = false; r.error = params.errorText; pushBuf(s.network, r); s.netIds.delete(params.requestId); }
    }
  } catch (e) { /* never break the event loop */ }
});

chrome.debugger.onDetach.addListener((source) => {
  const s = DBG.get(source.tabId);
  if (s) { s.attached = false; s.own = false; }
});

/* --------------------------------------------------------------------- keys */

const KEYMAP = {
  Enter: { key: 'Enter', code: 'Enter', vk: 13, text: '\r' },
  Tab: { key: 'Tab', code: 'Tab', vk: 9 },
  Escape: { key: 'Escape', code: 'Escape', vk: 27 },
  Esc: { key: 'Escape', code: 'Escape', vk: 27 },
  Backspace: { key: 'Backspace', code: 'Backspace', vk: 8 },
  Delete: { key: 'Delete', code: 'Delete', vk: 46 },
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', vk: 38 },
  ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', vk: 40 },
  ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', vk: 37 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', vk: 39 },
  Home: { key: 'Home', code: 'Home', vk: 36 },
  End: { key: 'End', code: 'End', vk: 35 },
  PageUp: { key: 'PageUp', code: 'PageUp', vk: 33 },
  PageDown: { key: 'PageDown', code: 'PageDown', vk: 34 },
  Space: { key: ' ', code: 'Space', vk: 32, text: ' ' }
};

function keySpec(name, modifiers) {
  if (KEYMAP[name]) return { ...KEYMAP[name], modifiers };
  if (name.length === 1) {
    const ch = name;
    const upper = ch.toUpperCase();
    const spec = {
      key: ch,
      code: /[0-9]/.test(ch) ? 'Digit' + ch : /[a-zA-Z]/.test(ch) ? 'Key' + upper : '',
      vk: upper.charCodeAt(0),
      modifiers
    };
    if (!(modifiers & 2) && !(modifiers & 1)) spec.text = ch;
    return spec;
  }
  const m = /^F([1-9]|1[0-2])$/.exec(name);
  if (m) return { key: name, code: name, vk: 111 + Number(m[1]), modifiers };
  throw new Error('unsupported key: ' + name);
}

async function dispatchKey(tabId, combo) {
  const parts = String(combo).split('+').map((s) => s.trim()).filter(Boolean);
  const keyName = parts.pop();
  let modifiers = 0;
  for (const p of parts) {
    const l = p.toLowerCase();
    if (l === 'alt') modifiers |= 1;
    else if (l === 'ctrl' || l === 'control') modifiers |= 2;
    else if (l === 'meta' || l === 'cmd' || l === 'win') modifiers |= 4;
    else if (l === 'shift') modifiers |= 8;
    else throw new Error('unknown modifier: ' + p);
  }
  const spec = keySpec(keyName, modifiers);
  const base = { modifiers, key: spec.key, code: spec.code, windowsVirtualKeyCode: spec.vk, nativeVirtualKeyCode: spec.vk };
  return await withDebugger(tabId, async (t) => {
    await chrome.debugger.sendCommand(t, 'Input.dispatchKeyEvent', { type: 'rawKeyDown', ...base });
    if (spec.text) {
      await chrome.debugger.sendCommand(t, 'Input.dispatchKeyEvent', { type: 'char', text: spec.text, unmodifiedText: spec.text, ...base });
    }
    await chrome.debugger.sendCommand(t, 'Input.dispatchKeyEvent', { type: 'keyUp', ...base });
    return { pressed: combo };
  });
}

function waitForNav(tabId, before, match, timeoutMs, started) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`timeout after ${timeoutMs}ms waiting for navigation`));
    }, timeoutMs);
    const onChange = (id, info, tab) => {
      if (Number(id) !== Number(tabId)) return;
      const url = info.url || (tab && tab.url) || '';
      if (info.status !== 'loading' && info.status !== 'complete') return;
      if (match) { if (!match.test(url)) return; }
      else if (url === before) return;
      cleanup();
      resolve({ url, ms: Date.now() - started });
    };
    function cleanup() { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(onChange); }
    chrome.tabs.onUpdated.addListener(onChange);
  });
}

function _fSnapshot() {
  const SEL = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch],[onclick],[contenteditable=true]';
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') return false;
    return r.width > 0 && r.height > 0;
  };
  document.querySelectorAll('[data-xb-ref]').forEach((el) => el.removeAttribute('data-xb-ref'));
  const els = Array.from(document.querySelectorAll(SEL)).filter(vis).slice(0, 400);
  const items = els.map((el, i) => {
    el.setAttribute('data-xb-ref', String(i));
    const name = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 120);
    const tag = el.tagName.toLowerCase();
    let role = el.getAttribute('role') || '';
    if (!role) {
      if (tag === 'a') role = 'link';
      else if (tag === 'button') role = 'button';
      else if (tag === 'select') role = 'combobox';
      else if (tag === 'textarea') role = 'textbox';
      else if (tag === 'input') {
        const ty = (el.type || 'text').toLowerCase();
        role = ty === 'checkbox' ? 'checkbox' : ty === 'radio' ? 'radio' : (ty === 'submit' || ty === 'button') ? 'button' : 'textbox';
      } else role = 'generic';
    }
    return {
      i, tag, role, name,
      type: el.getAttribute('type') || undefined,
      href: el.href || undefined,
      value: el.value !== undefined && el.value !== '' ? String(el.value).slice(0, 80) : undefined,
      checked: el.checked === true ? true : undefined,
      disabled: el.disabled || undefined
    };
  });
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 8000),
    headings: Array.from(document.querySelectorAll('h1,h2,h3')).slice(0, 40)
      .map((h) => `${h.tagName.toLowerCase()}: ${(h.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100)}`),
    items
  };
}

/* --------------------------------------------------------------- commands */

const COMMANDS = {
  /* ===== bsk-parity surface (v0.2) ===== */

  async doctor() {
    const namespaces = ['tabs', 'tabGroups', 'windows', 'sessions', 'bookmarks', 'history', 'downloads',
      'scripting', 'debugger', 'sidePanel', 'contextMenus', 'idle', 'offscreen', 'notifications',
      'management', 'storage', 'alarms', 'webNavigation', 'action', 'runtime'];
    const permissions = {};
    for (const n of namespaces) permissions[n] = typeof chrome[n] !== 'undefined';
    return {
      version: chrome.runtime.getManifest().version,
      userAgent: navigator.userAgent,
      permissions,
      features: {
        tabGroups: typeof chrome.tabGroups !== 'undefined',
        splitView: typeof (chrome.tabs && chrome.tabs.createSplit) === 'function',
        discard: typeof (chrome.tabs && chrome.tabs.discard) === 'function',
        sessionRestore: typeof chrome.sessions !== 'undefined',
        sidePanel: typeof chrome.sidePanel !== 'undefined'
      },
      debuggerSessions: Array.from(DBG.entries()).map(([tabId, s]) => ({
        tabId, own: s.own, attached: s.attached, console: s.console.length, network: s.network.length
      })),
      stats
    };
  },

  async 'window.resize'(a) {
    if (a.windowId === undefined) throw new Error('windowId required');
    const props = {};
    for (const k of ['width', 'height', 'left', 'top']) if (a[k] !== undefined) props[k] = Number(a[k]);
    if (a.state) props.state = a.state;
    const w = await chrome.windows.update(Number(a.windowId), props);
    return { id: w.id, width: w.width, height: w.height, state: w.state };
  },

  async navigate(a) {
    if (!a.url) throw new Error('url required');
    const tab = await getTab(a.tabId, a);
    await chrome.tabs.update(tab.id, { url: a.url });
    return { tabId: tab.id, url: a.url };
  },

  async 'navigate-back'(a) { return await goHistory(a, 'back'); },

  async 'navigate-forward'(a) { return await goHistory(a, 'forward'); },

  async snapshot(a) {
    const tab = await getTab(a.tabId);
    const maxItems = a.maxItems === undefined ? 300 : Number(a.maxItems);
    let results;
    try {
      results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, world: 'ISOLATED', func: _fSnapshot, args: []
      });
    } catch (e) {
      results = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, world: 'ISOLATED', func: _fSnapshot, args: []
      });
    }
    const frames = [];
    const items = [];
    for (const r of results) {
      const fid = r.frameId === undefined ? 0 : r.frameId;
      if (r.error || !r.result) continue;
      const d = r.result;
      frames.push({ frameId: fid, url: d.url, title: d.title, items: d.items.length });
      for (const it of d.items) {
        it.ref = fid === 0 ? `@e${it.i}` : `@e${it.i}f${fid}`;
        items.push({ ...it, frameId: fid });
      }
    }
    const mainRes = results.find((r) => (r.frameId || 0) === 0);
    const main = (mainRes && mainRes.result) || {};
    return {
      tabId: tab.id,
      url: main.url || tab.url,
      title: main.title || tab.title,
      text: main.text || '',
      headings: main.headings || [],
      frames,
      items: items.slice(0, maxItems)
    };
  },

  async observe(a) {
    const tab = await getTab(a.tabId);
    return { tabId: tab.id, ...(await inject(tab.id, _fObserve, [])) };
  },

  async 'get-html'(a) {
    const raw = a.ref !== undefined ? a.ref : a.selector;
    const ref = parseRef(raw);
    const selector = ref ? refSelector(ref.i) : (raw || null);
    const tab = await getTab(a.tabId);
    const out = await inject(tab.id, _pageHtml, [selector], ref ? ref.frameId : 0);
    if (out.error) throw new Error(out.error);
    return { tabId: tab.id, url: out.url, html: out.html };
  },

  async text(a) {
    const tab = await getTab(a.tabId);
    const out = await inject(tab.id, _pageText, [a.selector || null]);
    if (out.error) throw new Error(out.error);
    return { tabId: tab.id, url: out.url, title: out.title, text: out.text };
  },

  async extract(a) {
    const tab = await getTab(a.tabId);
    return { tabId: tab.id, ...(await inject(tab.id, _fExtract, [])) };
  },

  async evaluate(a) {
    if (!a.code) throw new Error('code required');
    const tab = await getTab(a.tabId);
    return await withDebugger(tab.id, async (t) => {
      const res = await chrome.debugger.sendCommand(t, 'Runtime.evaluate', {
        expression: String(a.code), returnByValue: true, awaitPromise: true, userGesture: true
      });
      if (res.exceptionDetails) {
        const d = res.exceptionDetails;
        throw new Error((d.exception && d.exception.description) || d.text || 'evaluate failed');
      }
      return { tabId: tab.id, value: res.result ? res.result.value : null, type: res.result ? res.result.type : null };
    });
  },

  async click(a) {
    const raw = a.ref !== undefined ? a.ref : a.selector;
    const ref = parseRef(raw);
    const selector = ref ? refSelector(ref.i) : raw;
    if (!selector) throw new Error('ref or selector required');
    const tab = await getTab(a.tabId, a);
    if (a.flash !== false) await pulseAt(tab.id, selector, ref ? ref.frameId : 0);
    const marked = a.noMark === true ? null : await ensureMark(tab.id, a.markLabel).catch(() => null);
    if (a.trusted === true) {
      // real CDP mouse events: needed by canvas widgets and elements that only
      // react to trusted input. Verify the hit before firing — a stale scroll
      // position would otherwise click whatever happens to sit at those pixels.
      const box = await inject(tab.id, _fTrustedBox, [selector], ref ? ref.frameId : 0);
      if (box.error) throw fail('target_not_found', box.error);
      if (!box.hit) {
        const out2 = await inject(tab.id, _fClickSel, [selector], ref ? ref.frameId : 0);
        if (out2.error) throw fail('target_not_found', out2.error);
        return { tabId: tab.id, ...out2, marked: !!marked,
                 trusted: { requested: true, fellBack: true, at: box.at } };
      }
      const count = a.double === true ? 2 : 1;
      await withDebugger(tab.id, async (t) => {
        await focusEmulation(t);
        const at = mousePoint(box);
        await chrome.debugger.sendCommand(t, 'Input.dispatchMouseEvent',
          { type: 'mouseMoved', ...at, button: 'none', buttons: 0, clickCount: 0 });
        for (let n = 1; n <= count; n += 1) {
          await chrome.debugger.sendCommand(t, 'Input.dispatchMouseEvent',
            { type: 'mousePressed', ...at, button: 'left', buttons: 1, clickCount: n });
          await sleep(16);
          await chrome.debugger.sendCommand(t, 'Input.dispatchMouseEvent',
            { type: 'mouseReleased', ...at, button: 'left', buttons: 0, clickCount: n });
        }
      });
      return { tabId: tab.id, clicked: { via: 'cdp', x: box.x, y: box.y, clicks: count }, marked: !!marked };
    }
    const out = await inject(tab.id, _fClickSel, [selector], ref ? ref.frameId : 0);
    if (out.error) throw new Error(out.error);
    return { tabId: tab.id, ...out, marked: !!marked };
  },

  async drag(a) {
    const tab = await getTab(a.tabId, a);
    let from;
    if (typeof a.from === 'string' && !/^\s*-?\d+(?:\.\d+)?\s*,/.test(a.from)) {
      const ref = parseRef(a.from);
      const sel = ref ? refSelector(ref.i) : a.from;
      from = await inject(tab.id, _fTrustedBox, [sel], ref ? ref.frameId : 0);
      if (from.error) throw fail('target_not_found', from.error);
    } else {
      from = await resolvePoint(tab.id, a.from);
    }
    const to = await resolvePoint(tab.id, a.to);
    const steps = Math.max(2, Math.min(120, Number(a.steps === undefined ? 12 : a.steps)));
    const durationMs = Math.max(30, Math.min(5000, Number(a.durationMs === undefined ? 320 : a.durationMs)));
    if (a.flash !== false) {
      await inject(tab.id, _fPulse, [{ x: from.x, y: from.y, w: from.w, h: from.h }]).catch(() => {});
    }
    const marked = await markTab(tab, a);
    const start = mousePoint(from), end = mousePoint(to);
    await withDebugger(tab.id, async (t) => {
      await focusEmulation(t);
      const send = (type, p, extra) => chrome.debugger.sendCommand(t, 'Input.dispatchMouseEvent', { type, ...p, ...extra });
      await send('mouseMoved', start, { button: 'none', buttons: 0, clickCount: 0 });
      await send('mousePressed', start, { button: 'left', buttons: 1, clickCount: 1 });
      for (let i = 1; i <= steps; i += 1) {
        const k = i / steps;
        const p = { x: Math.round(start.x + (end.x - start.x) * k), y: Math.round(start.y + (end.y - start.y) * k) };
        await send('mouseMoved', p, { button: 'left', buttons: 1 });
        await sleep(Math.max(4, durationMs / steps));
      }
      await send('mouseReleased', end, { button: 'left', buttons: 0, clickCount: 1 });
    });
    return { tabId: tab.id, dragged: true, from: start, to: end, steps, durationMs,
             fromHit: from.hit, at: from.at, marked };
  },

  async fill(a) {
    const raw = a.ref !== undefined ? a.ref : a.selector;
    const ref = parseRef(raw);
    const selector = ref ? refSelector(ref.i) : raw;
    if (!selector) throw new Error('ref or selector required');
    const tab = await getTab(a.tabId, a);
    if (a.flash !== false) await pulseAt(tab.id, selector, ref ? ref.frameId : 0);
    const marked = a.noMark === true ? null : await ensureMark(tab.id, a.markLabel).catch(() => null);
    const out = await inject(tab.id, _fFillSel, [selector, a.text === undefined ? '' : String(a.text)], ref ? ref.frameId : 0);
    if (out.error) throw new Error(out.error);
    return { tabId: tab.id, ...out, marked: !!marked };
  },

  async select(a) {
    const values = a.values !== undefined ? a.values : a.value;
    if (values === undefined) throw new Error('values required');
    const r = await onTarget(a, _fSelect, [Array.isArray(values) ? values : [values]]);
    const marked = await markTab(r.tab, a);
    return { tabId: r.tab.id, ...r.out, marked };
  },

  async focus(a) {
    const r = await onTarget(a, _fFocus, [true]);
    return { tabId: r.tab.id, ...r.out };
  },

  async blur(a) {
    const r = await onTarget(a, _fFocus, [false]);
    return { tabId: r.tab.id, ...r.out };
  },

  async 'scroll-to'(a) {
    const r = await onTarget(a, _fScrollTo, []);
    return { tabId: r.tab.id, ...r.out };
  },

  async hover(a) {
    const r = await onTarget(a, _fBox, []);
    const { x, y } = r.out;
    if (a.flash !== false) await inject(r.tab.id, _fPulse, [{ x, y }]).catch(() => {});
    await withDebugger(r.tab.id, async (t) => {
      await chrome.debugger.sendCommand(t, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
    });
    const marked = await markTab(r.tab, a);
    return { tabId: r.tab.id, hovered: r.selector, x, y, marked };
  },

  async wheel(a) {
    const tab = await getTab(a.tabId);
    let x = a.x, y = a.y;
    if (x === undefined || y === undefined) {
      if (a.ref !== undefined || a.selector !== undefined) {
        const box = await onTarget(a, _fBox, []);
        x = box.out.x; y = box.out.y;
      } else {
        const c = await inject(tab.id, _fCenter, []);
        x = c.x; y = c.y;
      }
    }
    const dy = a.deltaY === undefined ? 300 : Number(a.deltaY);
    const dx = a.deltaX === undefined ? 0 : Number(a.deltaX);
    const start = await inject(tab.id, _fViewport, []);
    let via = 'mouseWheel';
    try {
      await withDebugger(tab.id, async (t) => {
        await chrome.debugger.sendCommand(t, 'Input.dispatchMouseEvent', {
          type: 'mouseWheel', x, y, deltaX: dx, deltaY: dy, button: 'none', buttons: 0
        });
      });
    } catch (e) { /* fall through to the next strategy */ }
    let pos = await inject(tab.id, _fViewport, []);
    if (Math.abs(pos.y - start.y) < 1) {
      via = 'gesture';
      try {
        await withDebugger(tab.id, async (t) => {
          await chrome.debugger.sendCommand(t, 'Input.synthesizeScrollGesture', {
            x, y, xDistance: -dx, yDistance: -dy, speed: 800
          });
        });
      } catch (e) { /* fall through */ }
      pos = await inject(tab.id, _fViewport, []);
    }
    if (Math.abs(pos.y - start.y) < 1) {
      // offscreen / background windows drop synthetic wheel events; scroll directly
      via = 'script';
      pos = await inject(tab.id, (delta) => {
        window.scrollBy(0, delta);
        return { w: window.innerWidth, h: window.innerHeight, y: Math.round(window.scrollY) };
      }, [dy]);
    }
    return { tabId: tab.id, deltaY: dy, scrollY: pos.y, moved: Math.abs(pos.y - start.y), via,
             viewport: { w: pos.w, h: pos.h } };
  },

  async press(a) {
    const keys = a.keys !== undefined ? a.keys : a.key;
    if (!keys) throw new Error('keys required (e.g. "Enter", "Control+A")');
    const tab = await getTab(a.tabId, a);
    if (a.ref !== undefined || a.selector !== undefined) {
      await onTarget(a, _fFocus, [true]).catch(() => {});
    }
    const marked = await markTab(tab, a);
    const combo = Array.isArray(keys) ? String(keys[0]) : String(keys);
    // a sequence ("Enter,Tab") types several combos with a small gap, like bsk does
    const seq = Array.isArray(keys) ? keys.map(String) : String(keys).split(',').map((s) => s.trim()).filter(Boolean);
    if (seq.length > 1) {
      const done = [];
      for (const c of seq) {
        done.push(await dispatchKey(tab.id, c));
        await sleep(Number(a.intervalMs || 60));
      }
      return { tabId: tab.id, pressed: seq, sequence: done, marked };
    }
    return { tabId: tab.id, ...(await dispatchKey(tab.id, combo)), marked };
  },

  async upload(a) {
    const files = (a.files || []).map(String);
    if (!files.length) throw new Error('files required (absolute paths)');
    const raw = a.ref !== undefined ? a.ref : a.selector;
    if (typeof raw !== 'string' || !raw.length) throw new Error('ref or selector of the file input required');
    const ref = parseRef(raw);
    const selector = ref ? refSelector(ref.i) : raw;
    const tab = await getTab(a.tabId, a);
    const marked = await markTab(tab, a);
    return await withDebugger(tab.id, async (t) => {
      const doc = await chrome.debugger.sendCommand(t, 'DOM.getDocument', { depth: -1, pierce: true });
      const q = await chrome.debugger.sendCommand(t, 'DOM.querySelector', { nodeId: doc.root.nodeId, selector });
      if (!q || !q.nodeId) throw new Error('file input not found: ' + selector);
      await chrome.debugger.sendCommand(t, 'DOM.setFileInputFiles', { nodeId: q.nodeId, files });
      return { tabId: tab.id, selector, files, marked };
    });
  },

  // chrome.debugger refuses browser-level CDP (Browser.* / Page.setDownloadBehavior),
  // so downloads are captured through the downloads API instead: the browser saves
  // the file, we report where, and the CLI moves it into --dir.
  async 'download.wait'(a) {
    const since = Number(a.sinceMs || (Date.now() - 60000));
    const timeoutMs = Number(a.timeoutMs || 30000);
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const items = await chrome.downloads.search({ orderBy: ['-startTime'], limit: 10 });
      const hit = items.find((d) => Date.parse(d.startTime) >= since - 1500);
      if (hit) {
        let cur = hit;
        while (cur.state === 'in_progress' && Date.now() - started < timeoutMs) {
          await sleep(200);
          const again = await chrome.downloads.search({ id: hit.id });
          cur = again[0] || cur;
        }
        if (cur.state === 'interrupted') throw new Error('download interrupted: ' + (cur.error || 'unknown'));
        return { id: cur.id, filename: cur.filename, bytes: cur.fileSize || 0, url: cur.url, state: cur.state };
      }
      await sleep(200);
    }
    throw new Error('no download started within ' + timeoutMs + 'ms');
  },

  async 'download.prepare'(a) { return { ok: true, note: 'downloads are captured via the downloads API' }; },

  async 'wait-ms'(a) {
    const ms = Number(a.ms === undefined ? 500 : a.ms);
    await sleep(Math.max(0, Math.min(ms, 300000)));
    return { waitedMs: ms };
  },

  async 'wait-for-element'(a) {
    const state = a.state || 'visible';
    const timeoutMs = Number(a.timeoutMs === undefined ? 15000 : a.timeoutMs);
    const raw = a.ref !== undefined ? a.ref : a.selector;
    const ref = parseRef(raw);
    let selector, frameId = 0;
    if (ref) { selector = refSelector(ref.i); frameId = ref.frameId; }
    else if (typeof raw === 'string' && raw.length) selector = raw;
    else throw new Error('ref or selector required');
    const tab = await getTab(a.tabId);
    const out = await inject(tab.id, _fWaitElement, [selector, state, timeoutMs], frameId);
    if (!out.ok) throw new Error(out.error);
    return { tabId: tab.id, selector, state, ms: out.ms };
  },

  async 'wait-for-navigation'(a) {
    const tab = await getTab(a.tabId);
    const timeoutMs = Number(a.timeoutMs === undefined ? 30000 : a.timeoutMs);
    const match = a.urlPattern ? new RegExp(String(a.urlPattern)) : null;
    const started = Date.now();
    // fast path: the caller usually fired the action (press/click) in the previous
    // command, so the navigation may already be done before this listener attaches
    if (match && match.test(tab.url) && tab.status === 'complete') {
      return { tabId: tab.id, url: tab.url, status: tab.status, ms: 0, already: true };
    }
    await waitForNav(tab.id, tab.url, match, timeoutMs, started);
    const after = await chrome.tabs.get(tab.id);
    return { tabId: tab.id, url: after.url, status: after.status, ms: Date.now() - started };
  },

  async 'request-help'(a) {
    const message = String(a.message || '需要你在这个标签页里完成一步操作（登录 / 验证码 / 确认）。');
    const timeoutMs = Number(a.timeoutMs === undefined ? 300000 : a.timeoutMs);
    const tab = await getTab(a.tabId);
    const id = String(Date.now());
    await inject(tab.id, _fHelpOverlay, [message, id]);
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      await sleep(500);
      let done;
      try { done = await inject(tab.id, _fHelpPoll, [id]); }
      catch (e) { throw new Error('page went away while waiting for the human'); }
      if (done && done.done) return { tabId: tab.id, handled: true, ms: Date.now() - started };
    }
    throw new Error(`timeout after ${timeoutMs}ms waiting for the human`);
  },

  async 'console.read'(a) {
    const tab = await getTab(a.tabId);
    const s = await attachDebugger(tab.id);
    await withDebugger(tab.id, async (t) => {
      await chrome.debugger.sendCommand(t, 'Runtime.enable', {});
      await chrome.debugger.sendCommand(t, 'Log.enable', {});
    }, true);
    touchDbg(tab.id);
    const limit = Number(a.limit) || 0;
    const messages = limit ? s.console.slice(Math.max(0, s.console.length - limit)) : s.console.slice();
    if (a.clear) s.console.length = 0;
    return { tabId: tab.id, count: messages.length, messages };
  },

  async 'network.read'(a) {
    const tab = await getTab(a.tabId);
    const s = await attachDebugger(tab.id);
    await withDebugger(tab.id, async (t) => {
      await chrome.debugger.sendCommand(t, 'Network.enable', { maxTotalBufferSize: 20000000, maxResourceBufferSize: 5000000 });
    }, true);
    touchDbg(tab.id);
    let items = s.network.slice();
    if (a.filter) {
      const re = new RegExp(String(a.filter), 'i');
      items = items.filter((r) => re.test(r.url) || re.test(String(r.status)));
    }
    if (a.failedOnly) items = items.filter((r) => r.ok === false);
    const limit = Number(a.limit) || 0;
    const requests = limit ? items.slice(Math.max(0, items.length - limit)) : items;
    if (a.clear) s.network.length = 0;
    return { tabId: tab.id, count: requests.length, requests };
  },

  async emulate(a) {
    const tab = await getTab(a.tabId);
    if (a.off) {
      await withDebugger(tab.id, async (t) => {
        await chrome.debugger.sendCommand(t, 'Emulation.clearDeviceMetricsOverride', {});
        await chrome.debugger.sendCommand(t, 'Emulation.setUserAgentOverride', { userAgent: '' });
        await chrome.debugger.sendCommand(t, 'Emulation.setTouchEmulationEnabled', { enabled: false });
      });
      await detachDebugger(tab.id);
      return { tabId: tab.id, emulation: 'off' };
    }
    const width = Number(a.width || 390);
    const height = Number(a.height || 844);
    const deviceScaleFactor = Number(a.deviceScaleFactor || 3);
    const mobile = a.mobile !== false;
    // device metrics live on the DevTools session: detaching would drop them, so the
    // debugger session stays attached until `emulate --off` (or the idle sweeper)
    const apply = async () => {
      await withDebugger(tab.id, async (t) => {
        await chrome.debugger.sendCommand(t, 'Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor, mobile });
        if (a.userAgent) await chrome.debugger.sendCommand(t, 'Emulation.setUserAgentOverride', { userAgent: String(a.userAgent) });
        if (a.touch !== false) await chrome.debugger.sendCommand(t, 'Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
      }, true);
    };
    // a navigation committing right now can swallow the override; apply, measure, retry once
    await apply();
    let view = await inject(tab.id, _fViewport, []);
    let applied = view.w === width;
    if (!applied) {
      await sleep(250);
      await apply();
      view = await inject(tab.id, _fViewport, []);
      // 980 is the correct mobile layout width for pages without a viewport meta
      applied = view.w === width || view.w === 980;
    }
    return {
      tabId: tab.id,
      emulation: { width, height, deviceScaleFactor, mobile },
      viewport: { w: view.w, h: view.h },
      applied
    };
  },

  async screenshot(a) { return await COMMANDS['page.screenshot'](a); },

  async 'debugger.state'() {
    return Array.from(DBG.entries()).map(([tabId, s]) => ({
      tabId, own: s.own, attached: s.attached, console: s.console.length,
      network: s.network.length, pendingRequests: s.netIds.size
    }));
  },

  async 'diag.apis'() {
    const ns = ['tabs', 'tabGroups', 'windows', 'sessions', 'bookmarks', 'history', 'downloads',
      'cookies', 'scripting', 'debugger', 'sidePanel', 'readingList', 'contextMenus', 'commands',
      'idle', 'offscreen', 'notifications', 'management', 'storage', 'alarms', 'webNavigation',
      'tabCapture', 'declarativeNetRequest', 'identity', 'permissions', 'action', 'runtime', 'userScripts'];
    const present = {};
    for (const n of ns) present[n] = typeof chrome[n] !== 'undefined';
    const methods = (o) => (o ? Object.keys(o).filter((k) => typeof o[k] === 'function') : []);
    return {
      userAgent: navigator.userAgent,
      namespaces: present,
      tabsMethods: methods(chrome.tabs),
      tabGroupsMethods: methods(chrome.tabGroups),
      sessionsMethods: methods(chrome.sessions),
      has: {
        'tabs.discard': typeof (chrome.tabs && chrome.tabs.discard) === 'function',
        'tabGroups.move': typeof (chrome.tabGroups && chrome.tabGroups.move) === 'function',
        'windows.create': typeof (chrome.windows && chrome.windows.create) === 'function',
        'action.setBadgeText': !!(chrome.action && chrome.action.setBadgeText)
      }
    };
  },

  /* ---- visibility: which tabs is the agent driving right now ---- */

  async mark(a) {
    const tab = await getTab(a.tabId);
    const view = await ensureMark(tab.id, a.label === undefined ? '' : String(a.label));
    return { tabId: tab.id, ...view };
  },

  async unmark(a) {
    if (a.all === true) {
      const ids = Array.from(MARKS.keys());
      for (const id of ids) await clearMark(id);
      return { cleared: ids };
    }
    const tab = await getTab(a.tabId);
    const had = MARKS.has(tab.id);
    await clearMark(tab.id);
    return { tabId: tab.id, cleared: had ? [tab.id] : [] };
  },

  async marks(a) {
    const rows = [];
    for (const [tabId, m] of MARKS.entries()) {
      let live = null;
      if (a.live !== false) {
        live = await safeInject(tabId, _fMarkState, []);
        if (live === null) live = { error: 'unreachable' };
      }
      rows.push({ ...markView(tabId, m), overlay: live });
    }
    return { count: rows.length, marks: rows };
  },

  async pulse(a) {
    const tab = await getTab(a.tabId);
    let x = a.x, y = a.y;
    if (x === undefined || y === undefined) {
      if (a.ref !== undefined || a.selector !== undefined) {
        const r = await onTarget(a, _fBox, []);
        x = r.out.x; y = r.out.y;
      } else {
        const c = await inject(tab.id, _fCenter, []);
        x = c.x; y = c.y;
      }
    }
    await inject(tab.id, _fPulse, [{ x, y, w: a.w, h: a.h }]);
    return { tabId: tab.id, pulsed: true, x, y };
  },

  /*
   * Run several commands in one round trip. Partial application is reported
   * honestly: outcome=applied means earlier steps already changed the page, so
   * a blind retry of the whole batch would duplicate them.
   */
  async batch(a) {
    const steps = Array.isArray(a.steps) ? a.steps : [];
    if (!steps.length) throw fail('invalid_params', 'steps must be a non-empty array');
    if (steps.length > 100) throw fail('invalid_params', 'at most 100 steps per batch');
    const results = [];
    const applied = () => results.some((r) => r.ok);
    for (let i = 0; i < steps.length; i += 1) {
      const step = steps[i] || {};
      const cmd = String(step.cmd || '');
      const fn = COMMANDS[cmd];
      if (!fn || cmd === 'batch') {
        const error = { code: 'unsupported_method', message: `step ${i}: unsupported command ${cmd}`,
                        outcome: 'not_started', retryable: false };
        results.push({ index: i, cmd, ok: false, error });
        if (a.stopOnError !== false) {
          throw fail('batch_failed', `step ${i} (${cmd}) is not a runnable command`,
                     { outcome: applied() ? 'applied' : 'not_started', details: { results } });
        }
        continue;
      }
      try {
        const args = Object.assign({}, step.args || {});
        if (args.tabId === undefined && a.tabId !== undefined) args.tabId = a.tabId;
        const result = await fn(args);
        results.push({ index: i, cmd, ok: true, result });
      } catch (e) {
        const error = errorPayload(e);
        results.push({ index: i, cmd, ok: false, error });
        if (a.stopOnError !== false) {
          throw fail('batch_failed', `step ${i} (${cmd}) failed: ${error.message}`,
                     { outcome: applied() ? 'applied' : 'not_started', retryable: false,
                       details: { results } });
        }
      }
      if (a.intervalMs) await sleep(Number(a.intervalMs));
    }
    return { steps: steps.length, applied: results.filter((r) => r.ok).length, results };
  },

  /* ---- claims: who owns this tab right now ---- */

  async 'tab.claim'(a) {
    const tab = await getTab(a.tabId);
    const generation = generationOf(tab.id);
    if (a.generation !== undefined && Number(a.generation) !== generation) {
      throw fail('stale_generation',
        `tab ${tab.id} moved on since generation ${a.generation} (now ${generation}); list tabs again`, {});
    }
    const existing = LEASES.get(tab.id);
    if (existing && a.token !== undefined && existing.token !== a.token) {
      throw fail('tab_not_owned',
        `tab ${tab.id} is already claimed${existing.label ? ' (' + existing.label + ')' : ''}`, {});
    }
    const ttl = Math.max(5000, Math.min(30 * 60 * 1000, Number(a.ttlMs || LEASE_TTL_MS)));
    const lease = {
      token: a.token !== undefined ? String(a.token) : (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),
      label: a.label === undefined ? (existing ? existing.label : '') : String(a.label),
      generation,
      claimedAt: existing ? existing.claimedAt : Date.now(),
      expiresAt: Date.now() + ttl
    };
    LEASES.set(tab.id, lease);
    const marked = await ensureMark(tab.id, lease.label).catch(() => null);
    return { ...leaseView(tab.id, lease), generation, marked: !!marked };
  },

  async 'tab.release'(a) {
    if (a.all === true) {
      const ids = Array.from(LEASES.keys());
      for (const id of ids) {
        LEASES.delete(id);
        await clearMark(id);
      }
      return { released: ids };
    }
    const tab = await getTab(a.tabId);
    const had = LEASES.delete(tab.id);
    if (had) await clearMark(tab.id);
    return { released: had ? [tab.id] : [] };
  },

  async 'lease.list'() {
    reclaimLeases();
    return Array.from(LEASES.entries()).map(([tabId, l]) => leaseView(tabId, l));
  },

  /* ---- Chromium/Edge extras ---- */

  async 'tab.sleep'(a) {
    // ≈ Edge「睡眠标签页」的手动版：丢弃渲染进程，保留标签
    const list = (a.tabIds || (a.tabId !== undefined ? [a.tabId] : [])).map(Number);
    if (!list.length) throw new Error('tabIds required');
    const out = [];
    for (const id of list) {
      try {
        const t = await chrome.tabs.discard(id);
        out.push({ id, discarded: t ? t.discarded : true });
      } catch (e) {
        out.push({ id, error: String(e.message || e) });
      }
    }
    return out;
  },

  async 'group.move'(a) {
    if (a.groupId === undefined) throw new Error('groupId required');
    const props = { index: a.index === undefined ? 0 : Number(a.index) };
    if (a.windowId !== undefined) props.windowId = Number(a.windowId);
    const g = await chrome.tabGroups.move(Number(a.groupId), props);
    return shapeGroup(g);
  },

  async 'sessions.recent'(a) {
    if (!chrome.sessions) throw new Error('sessions API unavailable in this browser');
    const items = await chrome.sessions.getRecentlyClosed({ maxResults: Number(a.limit) || 10 });
    return items.map((i) => ({
      sessionId: (i.tab || i.window || {}).sessionId,
      kind: i.window ? 'window' : 'tab',
      tabs: i.window ? i.window.tabs.length : 1,
      title: i.tab ? i.tab.title : '',
      url: i.tab ? i.tab.url : ''
    }));
  },

  async 'sessions.restore'(a) {
    if (!chrome.sessions) throw new Error('sessions API unavailable in this browser');
    const s = await chrome.sessions.restore(a.sessionId ? String(a.sessionId) : undefined);
    return { restored: s ? Object.keys(s) : null };
  },

  // Verified on Edge 155: createSplit takes an ARRAY of tab ids and returns a
  // *split-view id* (not a tab id); unsplit takes that split-view id.
  async 'tab.split'(a) {
    const t1 = await getTab(a.tabId);
    const ids = [t1.id];
    if (a.otherTabId !== undefined && Number(a.otherTabId) !== t1.id) ids.push(Number(a.otherTabId));
    if (typeof chrome.tabs.createSplit !== 'function') throw new Error('split view API not available (needs a recent Chromium/Edge)');
    const splitViewId = await chrome.tabs.createSplit(ids);
    return { splitViewId, tabs: ids };
  },

  async 'tab.unsplit'(a) {
    if (typeof chrome.tabs.unsplit !== 'function') throw new Error('unsplit API not available');
    const id = a.splitViewId !== undefined ? Number(a.splitViewId) : (a.tabId !== undefined ? Number(a.tabId) : NaN);
    if (!Number.isFinite(id)) throw new Error('splitViewId (or tabId) required');
    await chrome.tabs.unsplit(id);
    return { unsplit: id };
  },

  async 'tab.zoom'(a) {
    const t = await getTab(a.tabId);
    if (a.factor !== undefined) {
      await chrome.tabs.setZoom(t.id, Number(a.factor));
    }
    return { tabId: t.id, zoom: await chrome.tabs.getZoom(t.id) };
  },

  async ping() {
    return { pong: Date.now(), version: chrome.runtime.getManifest().version };
  },

  async 'browser.info'() {
    const windows = await chrome.windows.getAll({ populate: false });
    const tabs = await chrome.tabs.query({});
    const groups = await chrome.tabGroups.query({});
    return {
      name: 'xb',
      version: chrome.runtime.getManifest().version,
      userAgent: navigator.userAgent,
      windows: windows.length,
      tabs: tabs.length,
      groups: groups.length,
      stats
    };
  },

  async 'tab.list'(a) {
    const q = {};
    if (a.windowId !== undefined && a.windowId !== null) q.windowId = Number(a.windowId);
    else if (a.currentWindow) q.currentWindow = true;
    const tabs = await chrome.tabs.query(q);
    tabs.sort((x, y) => (x.windowId - y.windowId) || (x.index - y.index));
    return tabs.map(shapeTab);
  },

  async 'tab.get'(a) {
    return shapeTab(await getTab(a.tabId));
  },

  async 'tab.create'(a) {
    const props = { active: a.active !== false };
    if (a.url) props.url = a.url;
    if (a.windowId !== undefined) props.windowId = Number(a.windowId);
    if (a.index !== undefined) props.index = Number(a.index);
    const tab = await chrome.tabs.create(props);
    let groupId = null;
    if (a.group) {
      groupId = await chrome.tabs.group({ tabIds: [tab.id], createProperties: a.windowId !== undefined ? { windowId: Number(a.windowId) } : undefined });
      if (a.group.title || a.group.color) {
        const upd = {};
        if (a.group.title) upd.title = a.group.title;
        if (a.group.color) upd.color = a.group.color;
        await chrome.tabGroups.update(groupId, upd);
      }
      if (a.group.collapsed !== undefined) await chrome.tabGroups.update(groupId, { collapsed: !!a.group.collapsed });
    }
    return { ...shapeTab(await chrome.tabs.get(tab.id)), groupIdOfCreated: groupId };
  },

  async 'tab.close'(a) {
    const ids = (a.tabIds || (a.tabId !== undefined ? [a.tabId] : [])).map(Number);
    if (!ids.length) throw new Error('tabIds required');
    await chrome.tabs.remove(ids);
    return { closed: ids };
  },

  async 'tab.select'(a) {
    const tab = await getTab(a.tabId);
    await chrome.tabs.update(tab.id, { active: true });
    if (a.focus !== false) await chrome.windows.update(tab.windowId, { focused: true });
    return shapeTab(await chrome.tabs.get(tab.id));
  },

  async 'tab.move'(a) {
    const ids = (a.tabIds || [a.tabId]).map(Number);
    const props = { index: a.index === undefined ? -1 : Number(a.index) };
    if (a.windowId !== undefined) props.windowId = Number(a.windowId);
    const moved = await chrome.tabs.move(ids, props);
    const arr = Array.isArray(moved) ? moved : [moved];
    return arr.map(shapeTab);
  },

  async 'tab.reload'(a) {
    await chrome.tabs.reload(Number(a.tabId));
    return { reloaded: Number(a.tabId) };
  },

  async 'tab.navigate'(a) {
    if (!a.url) throw new Error('url required');
    const tab = await getTab(a.tabId, a);
    await chrome.tabs.update(tab.id, { url: a.url });
    return { tabId: tab.id, url: a.url };
  },

  async 'tab.borrow'(a) {
    // "borrow" a user tab into the agent's window (bsk-compatible naming)
    const targetWindow = a.windowId !== undefined ? Number(a.windowId) : undefined;
    const tab = await getTab(a.tabId);
    const moved = await chrome.tabs.move([tab.id], { windowId: targetWindow === undefined ? chrome.windows.WINDOW_ID_CURRENT : targetWindow, index: -1 });
    return shapeTab(Array.isArray(moved) ? moved[0] : moved);
  },

  async 'window.list'(a) {
    const wins = await chrome.windows.getAll({ populate: true });
    return wins.map((w) => ({
      id: w.id,
      focused: !!w.focused,
      state: w.state,
      type: w.type,
      tabs: (w.tabs || []).length,
      title: (w.tabs || []).find((t) => t.active)?.title || ''
    }));
  },

  /* ---- tab groups: the thing bsk 0.3.2 cannot do ---- */

  async 'group.create'(a) {
    const ids = (a.tabIds || []).map(Number);
    if (!ids.length) throw new Error('tabIds required (at least one tab)');
    if (a.color && !COLORS.includes(a.color)) throw new Error(`color must be one of ${COLORS.join('|')}`);
    const createProperties = {};
    if (a.windowId !== undefined) createProperties.windowId = Number(a.windowId);
    else createProperties.windowId = (await getTab(ids[0])).windowId;
    const groupId = await chrome.tabs.group({ tabIds: ids, createProperties });
    const upd = {};
    if (a.title !== undefined) upd.title = String(a.title);
    if (a.color) upd.color = a.color;
    if (a.collapsed !== undefined) upd.collapsed = !!a.collapsed;
    const group = Object.keys(upd).length ? await chrome.tabGroups.update(groupId, upd) : await chrome.tabGroups.get(groupId);
    const tabs = await chrome.tabs.query({ groupId });
    return { ...shapeGroup(group), tabs: tabs.map(shapeTab) };
  },

  async 'group.list'(a) {
    const q = {};
    if (a.windowId !== undefined) q.windowId = Number(a.windowId);
    const groups = await chrome.tabGroups.query(q);
    const out = [];
    for (const g of groups) {
      const tabs = await chrome.tabs.query({ groupId: g.id });
      out.push({ ...shapeGroup(g), tabs: tabs.map(shapeTab) });
    }
    return out;
  },

  async 'group.update'(a) {
    if (a.groupId === undefined) throw new Error('groupId required');
    if (a.color && !COLORS.includes(a.color)) throw new Error(`color must be one of ${COLORS.join('|')}`);
    const upd = {};
    if (a.title !== undefined) upd.title = String(a.title);
    if (a.color !== undefined) upd.color = a.color;
    if (a.collapsed !== undefined) upd.collapsed = !!a.collapsed;
    if (!Object.keys(upd).length) throw new Error('nothing to update (title|color|collapsed)');
    const g = await chrome.tabGroups.update(Number(a.groupId), upd);
    return shapeGroup(g);
  },

  async 'group.add'(a) {
    if (a.groupId === undefined) throw new Error('groupId required');
    const ids = (a.tabIds || []).map(Number);
    if (!ids.length) throw new Error('tabIds required');
    const gid = await chrome.tabs.group({ groupId: Number(a.groupId), tabIds: ids });
    return { groupId: gid, added: ids };
  },

  async 'group.remove'(a) {
    const ids = (a.tabIds || []).map(Number);
    if (!ids.length) throw new Error('tabIds required');
    await chrome.tabs.ungroup(ids);
    return { ungrouped: ids };
  },

  async 'group.close'(a) {
    if (a.groupId === undefined) throw new Error('groupId required');
    const tabs = await chrome.tabs.query({ groupId: Number(a.groupId) });
    const ids = tabs.map((t) => t.id);
    if (ids.length) await chrome.tabs.remove(ids);
    return { closedGroup: Number(a.groupId), closedTabs: ids };
  },

  /* ---- page ---- */

  async 'page.text'(a) {
    return await inject(a.tabId, _pageText, [a.selector || null]);
  },

  async 'page.html'(a) {
    return await inject(a.tabId, _pageHtml, [a.selector || null]);
  },

  async 'page.snapshot'(a) {
    return await inject(a.tabId, _pageSnapshot, []);
  },

  async 'page.click'(a) {
    if (a.i === undefined) throw new Error('i required (from page.snapshot)');
    return await inject(a.tabId, _pageClick, [Number(a.i)]);
  },

  async 'page.fill'(a) {
    if (a.i === undefined) throw new Error('i required (from page.snapshot)');
    return await inject(a.tabId, _pageFill, [Number(a.i), String(a.text === undefined ? '' : a.text)]);
  },

  async 'page.query'(a) {
    if (!a.selector) throw new Error('selector required');
    return await inject(a.tabId, _pageQuery, [a.selector]);
  },

  async 'page.eval'(a) {
    // Arbitrary JS needs the debugger (MV3 CSP blocks eval in extension worlds).
    if (!a.code) throw new Error('code required');
    const tab = await getTab(a.tabId);
    const target = { tabId: tab.id };
    let attached = false;
    try {
      await chrome.debugger.attach(target, '1.3');
      attached = true;
    } catch (e) {
      // already attached by this extension is fine; anything else is fatal
      if (!String(e.message || e).includes('Another debugger') && !String(e.message || e).includes('already attached')) throw e;
    }
    try {
      const res = await chrome.debugger.sendCommand(target, 'Runtime.evaluate', {
        expression: String(a.code),
        returnByValue: true,
        awaitPromise: true,
        userGesture: true
      });
      if (res.exceptionDetails) {
        const d = res.exceptionDetails;
        throw new Error((d.exception && d.exception.description) || d.text || 'evaluate failed');
      }
      return { value: res.result ? res.result.value : null, type: res.result ? res.result.type : null };
    } finally {
      if (attached && a.keepAttached !== true) {
        try { await chrome.debugger.detach(target); } catch (e) {}
      }
    }
  },

  async 'page.screenshot'(a) {
    const tab = await getTab(a.tabId);
    const fmt = a.format === 'jpeg' ? 'jpeg' : 'png';
    const maxDim = a.maxDimension === undefined ? 2000 : Math.max(100, Number(a.maxDimension));
    const full = a.full === true;
    let clip = null;
    try {
      const metrics = await withDebugger(tab.id, (t) => chrome.debugger.sendCommand(t, 'Page.getLayoutMetrics', {}));
      const viewport = metrics.cssVisualViewport || metrics.visualViewport || {};
      const content = metrics.cssContentSize || metrics.contentSize || {};

      if (a.ref || a.selector) {
        const sel = parseRef(a.ref) ? refSelector(parseRef(a.ref).i) : a.selector;
        const box = await inject(tab.id, _fPageBox, [sel], parseRef(a.ref) ? parseRef(a.ref).frameId : 0);
        if (box.error) throw fail('target_not_found', box.error);
        clip = { x: box.x, y: box.y, width: box.w, height: box.h };
      } else if (typeof a.clip === 'string' || a.clipSelector) {
        const raw = a.clipSelector || a.clip;
        const parts = String(raw).split(',').map((s) => Number(s.trim()));
        if (parts.length !== 4 || parts.some((n) => !Number.isFinite(n))) {
          throw fail('invalid_params', 'clip must be "x,y,width,height" in CSS pixels');
        }
        clip = { x: parts[0], y: parts[1], width: parts[2], height: parts[3] };
      } else if (a.clip && typeof a.clip === 'object') {
        clip = { x: Number(a.clip.x) || 0, y: Number(a.clip.y) || 0,
                 width: Number(a.clip.width), height: Number(a.clip.height) };
      }

      const area = clip || (full
        ? { x: content.x || 0, y: content.y || 0, width: content.width, height: content.height }
        : { x: viewport.pageX || 0, y: viewport.pageY || 0, width: viewport.clientWidth, height: viewport.clientHeight });

      if (!area.width || !area.height) throw fail('invalid_params', 'the captured area has no size');
      if (clip && full) throw fail('invalid_params', 'clip and full cannot be used together');

      // output is capped by maxDim in CSS pixels; dpr is folded into scale so the
      // PNG is exactly css*scale pixels instead of css*dpr (which used to be huge)
      let dpr = 1;
      try {
        const r = await withDebugger(tab.id, (t) => chrome.debugger.sendCommand(t, 'Runtime.evaluate',
          { expression: 'window.devicePixelRatio', returnByValue: true }));
        const v = r && r.result && r.result.value;
        if (typeof v === 'number' && v > 0) dpr = v;
      } catch (e) { /* headless/odd pages: assume 1 */ }

      const cssScale = Math.min(1, maxDim / Math.max(area.width, area.height));
      const captureClip = { ...area, scale: cssScale / dpr };
      const params = {
        format: fmt,
        clip: captureClip,
        // a hidden/background tab produces no compositor frame on demand; asking
        // for the area beyond the viewport makes the renderer paint it offscreen
        // instead of waiting forever for a frame that will never arrive
        captureBeyondViewport: !!clip || full || tab.active === false
      };
      if (fmt === 'jpeg') params.quality = a.quality === undefined ? 80 : Math.max(1, Math.min(100, Number(a.quality)));

      const res = await withDebugger(tab.id, (t) => Promise.race([
        chrome.debugger.sendCommand(t, 'Page.captureScreenshot', params),
        sleep(Number(a.timeoutMs || 20000)).then(() => {
          throw fail('timeout', 'the page produced no frame for capture in time', { retryable: true });
        })
      ]));
      if (!res || !res.data) throw fail('capture_failed', 'the page produced no screenshot data');
      return {
        tabId: tab.id, via: 'cdp', format: fmt, base64: res.data,
        width: Math.round(area.width * cssScale), height: Math.round(area.height * cssScale),
        cssWidth: Math.round(area.width), cssHeight: Math.round(area.height),
        scale: Number((cssScale / dpr).toFixed(4)), devicePixelRatio: dpr,
        full: full || undefined, clip: clip || undefined
      };
    } catch (e) {
      if (a.fallback === false || !(e instanceof Error)) throw e;
      // captureVisibleTab needs the tab to be front-most. Doing that silently is
      // exactly the "the agent yanked my tab" complaint, so it is opt-in only.
      if (a.foreground !== true) {
        if (e.code) throw e;
        throw fail('capture_unavailable',
          'CDP capture failed and the fallback would need to bring this tab to the front; '
          + 'pass foreground=true to allow that', { retryable: true });
      }
      await chrome.tabs.update(tab.id, { active: true });
      const dataUrl = await Promise.race([
        chrome.tabs.captureVisibleTab(tab.windowId, { format: fmt }),
        new Promise((_, rej) => setTimeout(() => rej(fail('timeout', 'captureVisibleTab timed out', { retryable: true })), 15000))
      ]);
      return { tabId: tab.id, via: 'captureVisibleTab', format: fmt, dataUrl };
    }
  },

  async 'debugger.attach'(a) {
    const tab = await getTab(a.tabId);
    await chrome.debugger.attach({ tabId: tab.id }, '1.3');
    return { attached: tab.id };
  },

  async 'debugger.detach'(a) {
    const tab = await getTab(a.tabId);
    await chrome.debugger.detach({ tabId: tab.id });
    return { detached: tab.id };
  },

  async 'cdp'(a) {
    if (!a.method) throw new Error('method required');
    const tab = await getTab(a.tabId);
    const target = { tabId: tab.id };
    try {
      await chrome.debugger.attach(target, '1.3');
    } catch (e) { /* assume attached */ }
    const res = await chrome.debugger.sendCommand(target, a.method, a.params || {});
    return res;
  }
};

/* ------------------------------------------------------------------ boot */

chrome.runtime.onInstalled.addListener(() => { connect(); });
chrome.runtime.onStartup.addListener(() => { connect(); });
chrome.action.onClicked.addListener(() => { backoff = RECONNECT_MIN; connect(); });

/* a mark dies with its tab, and an overlay dies with its document: repaint */
chrome.tabs.onRemoved.addListener((tabId) => {
  LEASES.delete(tabId);
  GEN.delete(tabId);
  if (!MARKS.has(tabId)) return;
  MARKS.delete(tabId);
  persistMarks().catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === 'loading' || typeof info.url === 'string') bumpGeneration(tabId);
  if (info.status !== 'complete') return;
  const m = MARKS.get(tabId);
  if (!m) return;
  paintMark(tabId, m.label).catch(() => {});
});

chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== KEEPALIVE_ALARM) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) connect();
});

connect();
restoreMarks().catch(() => {});
log('service worker booted');