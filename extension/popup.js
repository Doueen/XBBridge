'use strict';
/* xb panel — lists the tabs the agent currently controls, with a one-click
 * "bring it forward" and per-row / global ways to drop the marker or claim. */

const dot = document.getElementById('dot');
const ver = document.getElementById('ver');
const totals = document.getElementById('totals');
const rowsEl = document.getElementById('rows');

let busy = false;

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
    box.append(el('strong', '', '桥没连上'),
               el('p', '', '本机 8787 端口没有守护进程。先跑一条 xb 命令把桥拉起来，再点"重连桥"。'));
  } else {
    box.append(el('strong', '', '没有受控标签'),
               el('p', '', 'xb 还没有碰过任何标签。被 click / fill / claim 碰过的标签会出现在这里。'));
  }
  rowsEl.append(box);
}

function renderRow(row) {
  const wrap = el('div', 'row');
  wrap.append(el('div', 'title', row.label || row.title || `tab ${row.tabId}`));
  wrap.append(el('div', 'url', `${host(row.url)}${row.discarded ? ' · 已休眠' : ''}`));

  const meta = el('div', 'meta');
  if (row.marked) meta.append(el('span', 'chip', '受控'));
  if (row.lease) meta.append(el('span', 'chip warn', `租约 ${Math.round(row.lease.expiresInMs / 1000)}s`));
  meta.append(el('span', 'chip', `gen ${row.generation}`));
  meta.append(el('span', 'spacer'));

  const focus = el('button', '', row.active ? '已在前台' : '切到前台');
  focus.disabled = row.active;
  focus.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    try {
      const res = await send('focus', { tabId: row.tabId });
      if (res && res.ok) window.close();
    } finally { busy = false; }
  });

  const drop = el('button', '', row.lease ? '释放' : '取消标记');
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

function render(state) {
  if (!state) return;
  ver.textContent = `v${state.version}`;
  dot.classList.toggle('on', !!state.connected);
  const t = state.totals || {};
  totals.textContent = state.connected
    ? `已连接 :${state.wsPort} · ${t.tabs ?? 0} 个标签 · 受控 ${t.marked ?? 0} · 租约 ${t.leased ?? 0}`
    : '未连接守护进程';

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
    else totals.textContent = `读取失败：${(res && res.error) || 'unknown'}`;
  } catch (e) {
    totals.textContent = `读取失败：${e.message}`;
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

refresh();
setInterval(refresh, 2000);
