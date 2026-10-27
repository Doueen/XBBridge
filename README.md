# xb — 以 bsk 为标准的 Edge/Chromium 增强版

bsk（[Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill)，MIT）的 CLI 命令面 + **bsk 没有的标签组/分屏/休眠**。
扩展跑在你的真实 Edge 里（带登录态），本地守护进程做桥，命令行就是 `xb`。

- 浏览器侧：MV3 扩展（Edge 155 / Chromium 155 实测），`ws://127.0.0.1:8787`
- 本机侧：`bridge/server.py`（WS ← 扩展，HTTP ← CLI），只依赖 `websockets`
- 命令行：`xb.py`（命令名与 bsk 对齐，`--session` 接受但忽略）

## 安装（一次性）

```powershell
# 1) 扩展
edge://extensions  ->  开发人员模式  ->  加载解压缩的扩展  ->  选 edge-agent\extension
# 2) 自检
python xb.py status      # 期望：extension: connected  v0.2.0
```

守护进程不用手动起，任何 `xb` 命令发现它不在都会自己拉起来（日志 `.xb/server.log`）；
要前台调试 `python xb.py serve`，要停 `python xb.py stop`。

## 命令（bsk 对齐）

```bash
# 读
xb navigate URL [--tab N]        xb snapshot [--tab N]           xb observe
xb text [--selector CSS]        xb get-html [REF|CSS]          xb extract [--out f.json --csv]
xb screenshot out.png [--full] [--ref @e12]

# 交互（REF 形如 @e12，也可以用 CSS 选择器）
xb click @e12 | xb click "#submit"
xb fill @e5 "文本"              xb press Enter [--ref @e5]      xb select @e6 --values cherry
xb hover @e7                    xb scroll-to @e9                xb wheel --delta-y 600
xb focus @e5 / xb blur @e5      xb upload @e8 --files a.pdf     xb download @e4 --dir out/
xb eval "document.title"

# 等待（bsk 的可靠性核心）
xb wait-ms 500
xb wait-for-element "#done" --state visible|hidden|attached|detached --timeout 15000
xb wait-for-navigation --url-pattern "submitted" --timeout 8000

# 观测（debugger 环形缓冲，最多 500 条）
xb console [--limit 30 --clear]        xb network [--filter api --failed-only]
xb emulate --width 390 --height 844    xb emulate --off
xb request-help --message "请手动登录" --timeout 300000

# 标签 / 窗口
xb tabs / xb windows / xb open URL [--group-title T --color blue] / xb close 1,2
xb tab-select 3 / xb sleep 3 / xb split 3 4 / xb unsplit <splitViewId> / xb zoom 3 1.25

# 标签组（bsk 完全没有）
xb group new --tabs 1,2,3 --title "PR 评审" --color blue
xb group list / xb group set 7 --color green --title 改好了 --collapse
xb group add 7 8 / xb group rm 8 / xb group close 7 / xb group move 7 --index 0

# 会话 / 诊断
xb recent / xb restore [sessionId] / xb doctor / xb logs -n 50
```

所有命令加 `--json`（放在子命令前）输出机器可读结果。

## 与 bsk 的差距（42 个命令族，覆盖 34）

| 已对齐 | 说明 |
|---|---|
| status/doctor/logs/daemon, tab, window, navigate(+back/forward/reload), snapshot, observe*, console, network, get-html, extract, click/hover/wheel/scroll-to/focus/blur/fill/press/select, upload, download, evaluate, wait-ms/wait-for-element/wait-for-navigation, request-help, emulate, screenshot | *observe 是轻量版（landmark/heading/控件统计），不是 bsk 的完整 VOM |
| **xb 独有** | 标签组全套 + `group move`；`split/unsplit` 分屏；`sleep`(tabs.discard)；`zoom`；`recent/restore`；HTTP JSON 接口（任何脚本可调） |

| 尚未实现 | 为什么 |
|---|---|
| `record` / `video` | 需要录制管线（bsk 有 screencast + MP4 mux） |
| `debug`（请求重放/证据导出） | 依赖 bsk 的 network-store 与归档格式 |
| `cooperate` / `session` / `browsers` | bsk 是 Agent Window + 多实例注册；xb 直接操作你的窗口，单浏览器 |
| `install-skill` / `update` | 分发相关 |

## 实测踩过的坑（Edge 155）

1. **`chrome.debugger` 不能用浏览器级 CDP**：`Browser.setDownloadBehavior` 报 `wasn't found`，`Page.setDownloadBehavior` 报 `Cannot not access browser-level commands`。下载改为走 `chrome.downloads` API：浏览器先存、命令报路径、CLI 再挪进 `--dir`。
2. **设备指标（emulate）挂在 DevTools 会话上**：命令结束就 detach 会把 override 一起丢掉。所以 `emulate` 保持附着，只有 `emulate --off` 才 detach。
3. **离屏/后台窗口会丢弃合成滚轮事件**：`wheel` 现在三级回退（`mouseWheel` → `synthesizeScrollGesture` → 页内 `scrollBy`），返回里的 `via` 说明走了哪条。
4. **`wait-for-navigation` 有竞态**：上一秒的 `press Enter` 可能已经把页面跳完，监听器才挂上。带 `--url-pattern` 时先做“已经到位”的快路径。
5. **MV3 CSP 禁 `eval`**：`eval`/`screenshot`/`hover`/`press`/`console`/`network` 都走 debugger，首次会弹「xb began debugging」提示条。
6. **`edge://`、扩展页、Web Store 不可注入**；`group close` 关掉窗口最后几个标签会让浏览器退出。
7. **SW 休眠**：keepalive alarm（30s）+ 断线指数退避（0.8s→15s）；浏览器晚于守护进程启动时，最多等一个 alarm 周期才连上。

## 结构

```
edge-agent/
  xb.py                     CLI（bsk 命令名 + 标签组 + 分屏/休眠）
  bridge/server.py          本地桥：WS ← 扩展，HTTP ← CLI
  extension/manifest.json   MV3：tabs tabGroups windows scripting debugger sessions downloads …
  extension/sw.js           命令路由（bsk 对齐 + 标签组扩展）
  test/site/*               自测站点（惰性元素/表单/下拉/上传/下载/表格/console/network）
  test/e2e.py               35 项端到端断言
  test/kill-edge.ps1        只杀带指定 profile 标记的 msedge
```

## 自测

```bash
cd test/site && python -m http.server 8899 --bind 127.0.0.1        # 终端 A
"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
  --user-data-dir="$TMP/xbprofile" \
  --load-extension="C:/WorkSpace/HermesWorkSpace/edge-agent/extension" \
  http://127.0.0.1:8899/app.html                                    # 终端 B
python test/e2e.py                                                  # 终端 C
powershell -File test/kill-edge.ps1 -Match xbprofile                # 清理
```

最近一次：**35/35 ALL GREEN**（等待/竞态、@eN 快照与引用点击、console/network 缓冲、回车提交导航、
select/hover/wheel/scroll-to、上传/下载落盘、extract 表格、emulate 手机视口、截图、标签组全套、
split/unsplit、sleep、recent）。

## 参考资料

- bsk 源码（MIT）：`C:\WorkSpace\HermesWorkSpace\bsk-reference`
  - 等待：`crates/bsk-cli/src/cli/waits.rs`、`browser_wait.rs`
  - 交互：`interaction.rs`、`wheel.rs`、`scroll.rs`
  - 文件：`upload.rs`、`download.rs`
  - 观测：`console.rs`、`network.rs`、`apps/extension/src/debug/network-store.ts`
  - 结构化抽取：`extract.rs`；iframe：`apps/extension/src/browser-driver/frame-graph.ts`（xb 目前只合并同源 frame，跨域仍走 allFrames 注入）
