'use strict';
/* xb panel — lists the tabs the agent currently controls, with a one-click
 * "bring it forward" and per-row / global ways to drop the marker or claim. */

const dot = document.getElementById('dot');
const ver = document.getElementById('ver');
const totals = document.getElementById('totals');
const rowsEl = document.getElementById('rows');
const pauseBtn = document.getElementById('pause');
const blockedEl = document.getElementById('blocked');

let busy = false;

/* i18n: every visible string comes from _locales so the store listing can
 * ship English and Chinese without a second build. */
function t(key, ...subs) {
  let msg = '';
  try { msg = chrome.i18n.getMessage(key, subs.map(String)) || ''; } catch (e) { msg = ''; }
  return msg || key;
}

function localizeStatic() {
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    const msg = t(node.getAttribute('data-i18n'));
    if (msg && msg !== node.getAttribute('data-i18n')) node.textContent = msg;
  });
  document.querySelectorAll('[data-i18n-title]').forEach((node) => {
    const msg = t(node.getAttribute('data-i18n-title'));
    if (msg && msg !== node.getAttribute('data-i18n-title')) node.title = msg;
  });
}

function send(op, extra) {
  return chrome.runtime.sendMessage(Object.assign({ xb: true, op }, extra || {}));
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function host(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return url || ''; }
}

function renderEmpty(state) {
  rowsEl.replaceChildren();
  const box = el('div', 'empty');
  if (!state.connected) {
    box.append(el('strong', '', t('emptyBridge')), el('p', '', t('emptyBridgeHint')));
  } else {
    box.append(el('strong', '', t('emptyTabs')), el('p', '', t('emptyTabsHint')));
  }
  rowsEl.append(box);
}

function renderRow(row) {
  const wrap = el('div', 'row');
  wrap.append(el('div', 'title', row.label || row.title || `tab ${row.tabId}`));
  wrap.append(el('div', 'url', `${host(row.url)}${row.discarded ? ' · ' + t('dormant') : ''}`));

  const meta = el('div', 'meta');
  if (row.marked) meta.append(el('span', 'chip', t('chipMarked')));
  if (row.lease) {
    meta.append(el('span', 'chip warn', t('chipLease', Math.round(row.lease.expiresInMs / 1000))));
  }
  meta.append(el('span', 'chip', t('chipGen', row.generation)));
  meta.append(el('span', 'spacer'));

  const focus = el('button', '', row.active ? t('alreadyActive') : t('bringForward'));
  focus.disabled = row.active;
  focus.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    try {
      const res = await send('focus', { tabId: row.tabId });
      if (res && res.ok) window.close();
    } finally { busy = false; }
  });

  const drop = el('button', '', row.lease ? t('release') : t('unmark'));
  drop.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    drop.disabled = true;
    try {
      const res = await send(row.lease ? 'release' : 'unmark', { tabId: row.tabId });
      if (res && res.ok) render(res.result);
    } finally { busy = false; drop.disabled = false; }
  });

  meta.append(focus, drop);
  wrap.append(meta);
  return wrap;
}

function renderSafety(state) {
  const paused = state && state.paused === true;
  if (pauseBtn) {
    pauseBtn.textContent = paused ? t('resume') : t('pause');
    pauseBtn.classList.toggle('on', paused);
  }
  if (blockedEl) {
    const n = (state && state.blocked ? state.blocked.length : 0);
    blockedEl.textContent = n ? t('blockedCount', n) : t('blockedNone');
  }
}

function render(state) {
  if (!state) return;
  renderSafety(state);
  ver.textContent = state.label ? `${state.label} · v${state.version}` : `v${state.version}`;
  ver.title = `instance ${state.instanceId || '?'} — xb --browser 可用这个 id 或上面的名字`;
  dot.classList.toggle('on', !!state.connected);
  const t = state.totals || {};
  const counts = state.totals || {};
  totals.textContent = state.connected
    ? t('connected', state.wsPort, counts.tabs ?? 0, counts.marked ?? 0, counts.leased ?? 0)
    : t('notConnected');

  if (!state.rows || !state.rows.length) {
    renderEmpty(state);
    return;
  }
  rowsEl.replaceChildren(...state.rows.map(renderRow));
}

async function refresh() {
  try {
    const res = await send('state');
    if (res && res.ok) render(res.result);
    else totals.textContent = t('readFailed', (res && res.error) || 'unknown');
  } catch (e) {
    totals.textContent = t('readFailed', e.message);
  }
}

document.getElementById('unmark-all').addEventListener('click', async () => {
  const res = await send('unmark', { all: true });
  if (res && res.ok) render(res.result);
});

document.getElementById('reconnect').addEventListener('click', async () => {
  const res = await send('reconnect');
  if (res && res.ok) render(res.result);
});

localizeStatic();
refresh();
setInterval(refresh, 2000);
