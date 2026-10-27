# Partner Center 填写材料（Edge Add-ons）

按 Microsoft 文档 Step 5 ~ Step 8 逐项对应，可直接复制。英文版在前（Partner Center 表单是英文），
中文说明在后。

---

## Step 5 · Properties

| 字段 | 填什么 |
|---|---|
| Category | **Developer tools**（备选：Productivity） |
| Website | 你的仓库地址或项目主页（可选，但填了审核更快） |
| Support contact detail | 邮箱或 issue 地址 |
| Mature content | 不勾 |

---

## Step 6 · Privacy

### Purpose of the extension（单一用途声明，必填）

> XBBridge lets a locally running command-line agent drive the user's own Edge tabs:
> it opens tabs in a named tab group, marks the tabs it controls with a visible overlay,
> reads pages on request, and performs clicks/typing/scrolling the user asked for.
> It is a client for a companion daemon on the same machine (`127.0.0.1:8787`);
> it does no work on its own and sends nothing off the device.

中文：XBBridge 是本地命令行 agent 的浏览器端客户端——按你的指令在**你自己的** Edge 标签里
开标签组、打可见标记、读页面、点击/输入/滚动。它本身不做事，也不把任何东西传出本机。

### Permissions justification（逐条，必填）

| Permission | Justification（直接粘贴） |
|---|---|
| `tabs` | List, open, focus and close the user's tabs — the entry point for every command the user runs. |
| `tabGroups` | Keep tabs opened for an agent task inside one named, colour-coded group so the user can see and close them in one action. |
| `windows` | Read window bounds and minimized state to decide whether a screenshot or a real mouse event is possible. |
| `scripting` | Inject a `pointer-events: none` overlay that marks the tab under control, and read page text/snapshot when the user asks for it. |
| `debugger` | Used **only** by the explicit `click --trusted` command, to dispatch genuine input events on pages that ignore synthetic ones. No page data is read through it. |
| `alarms` | Keep the service worker re-attached to the local daemon (a 15-second keep-alive). |
| `storage` | Store the local instance id, the tab-mark table and lease tokens. Local only, never uploaded. |
| `sessions` | `sessions.recent` — list recently closed tabs, only when the user asks for it. |
| `downloads` | `download` — save a file the user asked to download into a directory they named. |
| `host_permissions: <all_urls>` | The user chooses which page the agent works on at run time, so the domain set cannot be declared in advance. Nothing is read until the user issues a command for that tab. |

### Remote code

> **No.** All code is inside the package. The extension runs no remote script, no `eval`,
> no remote configuration; its only network connection is a loopback WebSocket to
> `ws://127.0.0.1:8787` on the same machine.

### Data usage certification

> The extension does not collect, transmit or sell any user data. There is no analytics,
> no telemetry, no advertising and no third-party service. Page content is returned to
> the user's own local process and written to their own disk.

### Privacy policy URL

托管 `docs/PRIVACY.md` 后填那个 URL（GitHub Pages / Gitee Pages / 公司内站点都行）。

---

## Step 7 · Store listing

**Extension name**（来自 manifest，只读）：`XBBridge`

**Short description / Summary**：

> Let a local CLI agent work in your own Edge tabs — named tab groups, visible control
> markers, tab leases. Requires the companion xb daemon on the same machine.

**Detailed description（EN）**：

> XBBridge is the browser half of `xb`, a command-line agent bridge that runs on your own
> machine.
>
> • **Tabs, not windows** — the agent opens what it needs as tabs in a named, colour-coded
>   group inside the window you are already using. Nothing pops up in your face.
> • **You can always see what it touches** — every controlled tab gets a teal outline and a
>   small label, and its favicon switches to the xb mark, so a glance at the tab strip tells
>   you which tabs are under control.
> • **Leases** — two agents (or two of your own scripts) cannot fight over the same tab: the
>   first one to claim it holds it until it releases or the lease expires.
> • **Background-safe** — screenshots and waits work on background tabs without stealing
>   focus; nothing is brought to the front unless you ask for it.
> • **Honest errors** — every failure carries a code, an outcome (`applied` / `not_started`
>   / `unknown`) and whether a retry is safe, so scripts never guess.
>
> XBBridge is a *client*: it needs the `xb` daemon (open source, Python) running on the same
> computer. Without it the popup simply says the bridge is not connected and never touches
> your browsing.
>
> No account, no cloud, no telemetry. Page content never leaves your machine.

**Detailed description（中文）**：

> XBBridge 是命令行 agent 桥 `xb` 的浏览器端。它在**你正在用的那个窗口**里，把 agent 需要的
> 页面开成一个命名标签组：不弹新窗口、不抢焦点。
>
> • 受控标签有可见标记（青色描边 + 名字），favicon 也会换成 xb 图标，扫一眼标签栏就知道谁在被控制
> • 租约防止两个 agent 抢同一个标签
> • 后台标签也能截图、等待，除非你明确要求，否则不会切到前台
> • 出错必带错误码、是否已生效、能否重试
>
> 它是客户端：需要同机运行 `xb` 守护进程（开源、Python）。没连上时，面板只会提示未连接，
> 不会碰你的浏览。无账号、无云端、无遥测，页面内容不出本机。

**Search terms**：`browser automation`, `AI agent`, `tab group`, `CLI`, `automation`, `agent bridge`

**Screenshots**（1280×800，1~10 张）：
1. 受控标签 + 标记（`docs/store/shot-1-marked-tab.png`）
2. 工具栏面板（`docs/store/shot-2-panel.png`）
3. 命令行输出（`docs/store/shot-3-cli.png`）

**Small promotional tile**：`docs/store/tile-440x280.png`

---

## Step 8 · Certification testing notes（最容易被打回的一段，务必写）

> **This extension needs a companion daemon to do anything, and that is by design.**
>
> 1. Install the extension. Click its toolbar icon: the popup honestly reports
>    "bridge not connected" (`桥没连上`) and explains that a local daemon is required.
>    No page is read at this point.
> 2. To see it work: run the companion daemon from
>    `https://<your-repo>#quick-start` (`python xb.py serve`, Python 3.9+), then
>    `python xb.py open https://example.com` — a tab opens in the current window,
>    inside a group named "xb", with a teal marker and the xb favicon.
> 3. All permissions listed above are exercised by commands in
>    `README.md#命令`; `debugger` is touched only by `xb click --trusted`.
> 4. `dev.reload` (extensions reloading themselves from disk) is compiled out of this
>    build: in the store build the command returns `unsupported_method`.
> 5. The extension never contacts a remote host. Its only socket is
>    `ws://127.0.0.1:8787`, and the daemon refuses WebSocket handshakes that carry an
>    `http(s)://` origin, so a website cannot impersonate the extension.

---

## 提交记录（2026-10-27）

| 项 | 值 |
|---|---|
| 状态 | **In review**（Microsoft 审核中，官方说明 7 个工作日内出结果） |
| 版本 | 0.3.4 |
| Product ID | `9865aa85-56ab-4717-9c18-d57886d7f407` |
| Store ID | `0RDCKCZKMWH2` |
| CRX ID | `fdhphelkbllilnniggopgdkmbfabgngo` |
| Category | Developer Tools |
| Visibility | Public（241/241 市场） |
| Languages | English (United States) |
| 包 | `XBBridge-0.3.4.zip`（9 权限 + `<all_urls>`） |

审核表单的选择：**"Yes, I need to provide credentials, accounts, or other info for testers"** —— 不是要账号，而是测试者必须知道要先起本地守护进程，否则只会看到"未连接"。认证备注（1959/2000 字符）原文如下：

```
NO CREDENTIALS ARE NEEDED. XBBridge is a client: it needs the companion "xb" daemon (open source, Python 3.9+) running on the same machine, and it does nothing on its own.

Why: the extension lets a local command-line agent drive the user's own browser tabs. The daemon is the agent side, the extension is the browser side. Without it, the popup honestly reports "bridge not connected" and no page is read.

How to test the functionality:
1) Install and click the toolbar icon: it reports the bridge is not connected (expected).
2) Start the daemon per the project README linked from the support contact: python xb.py serve
3) Run: python xb.py open https://example.com
   A tab opens in the CURRENT window, inside a group named XBBridge, with a teal frame, a label chip at the bottom-left and the XBBridge favicon; the popup lists the controlled tab.
4) python xb.py panel lists controlled tabs; python xb.py group close XBBridge cleans up.

Where each permission is used:
- tabs, tabGroups, windows: open/group/focus/close tabs, read window bounds and minimized state
- scripting: the pointer-events:none control overlay, and text snapshots the user asks for
- alarms: 15s service-worker keep-alive so a later command does not hang
- storage: local instance id, tab-mark table, lease tokens (never uploaded)
- sessions: sessions.recent, only when the user runs it
- downloads: the download command only
- debugger: only by "click --trusted", to dispatch real mouse events on pages that ignore synthetic ones; no page data is read through it
- host <all_urls>: the user picks the page at run time; nothing is read until a command targets that tab

No remote code, no eval, no telemetry. The only socket is a loopback WebSocket to ws://127.0.0.1:8787, and the daemon rejects handshakes carrying an http(s) origin, so a website cannot impersonate the extension. "dev.reload" (self-reload from disk) is compiled out of this build and returns unsupported_method.
```

> 后续更新版本时：改 manifest 版本号 → `python tools/build_store.py` → Packages 页 Replace 上传 → Store listings 若图标/文案有变则重传 → Publish 提交（每次都要重新填认证备注）。
