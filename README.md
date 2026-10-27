# xb — 以 bsk 为标准的 Edge/Chromium 增强版

bsk（[Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill)，MIT）的 CLI 命令面，加上 bsk 没有的东西：
**标签组/分屏/休眠、被控标签的视觉标记、不抢前台、真实拖拽、批量动作、租约与代际校验**。
扩展跑在你的真实 Edge 里（带登录态），本地守护进程做桥，命令行就是 `xb`。

- 浏览器侧：MV3 扩展（Edge 155 / Chromium 155 实测），`ws://127.0.0.1:8787`
- 本机侧：`bridge/server.py`（WS ← 扩展，HTTP ← CLI），只依赖 `websockets`
- 命令行：`xb.py`（命令名与 bsk 对齐，`--session` 接受但忽略）

## 这一版比 bsk 多出来的东西

| 能力 | 为什么重要 |
|---|---|
| **控制标记** `mark/unmark/marks` | 被 agent 接手的标签有呼吸绿边 + 左上角标签芯片 + favicon 换装。后台开标签组干活时，这是唯一能看见"它在动哪个标签"的手段 |
| **点击涟漪** `pulse` | click/fill/press/hover/select/upload/drag 会在落点闪一下，肉眼能跟上 agent 的动作 |
| **不抢前台** | 默认 `--background` 开标签、截图不激活标签、`click --trusted` 只在窗口可用时用真实坐标，否则回落 DOM 点击而不是把窗口拽到前台 |
| **真实拖拽** `drag` | 滑块、拖放控件、canvas 都能拖；起手点命中校验（`fromHit`） |
| **真实点击** `click --trusted` | 先 `elementFromPoint` 验证那个点确实是目标，再派发 CDP 鼠标事件；点不上就回落，绝不"点到别的元素还报成功" |
| **批量动作** `batch` | 一个来回跑最多 100 步，逐步返回结果；失败时 `outcome=applied` + `details.results`，调用方不会盲目重试造成重复提交 |
| **错误契约** | 每个失败带 `code / outcome / retryable`，CLI 映射成稳定退出码 |
| **租约与代际** `claim/release/leases` | 一个客户端认领标签后，别人动它就报 `tab_not_owned`；页面导航后 `expectGeneration` 过期报 `stale_generation`，避免在没看过的文档上乱点 |
| **工具栏面板** | 点扩展图标就能看到"现在有哪些标签被控制/被租用"，每行可一键切到前台或取消标记；命令行侧同一份数据用 `xb panel`（含 worker 重启次数与未捕获异常计数） |
| **全页/裁剪/元素截图** | `--full`、`--clip x,y,w,h`、`--selector`、`--format jpeg --quality`，长边默认截到 2000 CSS px，dpr 折算进 scale，出图尺寸可断言 |

## 安装（一次性）

```powershell
# 1) 扩展：必须选 edge-agent\extension 这一层，不是仓库根目录
edge://extensions  ->  开发人员模式  ->  加载解压缩的扩展  ->  C:\WorkSpace\HermesWorkSpace\edge-agent\extension
# 2) 自检
python xb.py status      # 期望：extension: connected  v0.3.2
```

> 扩展在 Edge 里显示为 **XBBridge**（teal 圆角方块 + 白色 X，和受控标签的 favicon 是同一个符号）。
> 改完 `manifest.json` 或图标后，`edge://extensions` 点该卡片的"重新加载"，或者 `python xb.py ext-reload`。
>
> 选成仓库根目录（`...\edge-agent`）会报
> `Cannot load extension with file or directory name __pycache__. Filenames starting with "_" are reserved`——
> 根目录里有 `bridge/`、`test/`、`xb.py` 和 Python 的 `__pycache__`，只有 `extension/` 是扩展本体（4 个文件：`manifest.json` / `sw.js` / `popup.html` / `popup.js`）。

守护进程不用手动起，任何 `xb` 命令发现它不在都会自己拉起来（日志 `.xb/server.log`）；
要前台调试 `python xb.py serve`，要停 `python xb.py stop`。

## 命令

```bash
# 读
xb navigate URL [--tab N]        xb snapshot [--tab N]           xb observe
xb text [--selector CSS]         xb get-html [REF|CSS]           xb extract [--out f.json --csv]
xb screenshot out.png [--full] [--clip 0,0,800,600] [--selector "#tbl"] [--format jpeg --quality 70]
xb screenshot out.png --scale-to 1200 --foreground   # 默认不抢前台；--foreground 才允许

# 交互（REF 形如 @e12，也可以用 CSS 选择器）
xb click @e12 | xb click "#submit" | xb click "#pad" --trusted --double
xb drag "#knob" "300,300" [--steps 12 --duration 320]      # from/to 都收 @eN | CSS | "x,y"
xb fill @e5 "文本"              xb press Enter [--ref @e5]      xb select @e6 --values cherry
xb press "Tab,Enter"                                        # 逗号序列，逐步派发
xb hover @e7                    xb scroll-to @e9                xb wheel --delta-y 600
xb focus @e5 / xb blur @e5      xb upload @e8 --files a.pdf     xb download @e4 --dir out/
xb eval "document.title"

# 批量（JSON 文件或 stdin：[{"cmd":"fill","args":{...}}, ...]）
xb batch --file steps.json --tab 12 [--continue-on-error] [--interval-ms 80]

# 可见性
xb mark --tab 12 --label "抓禅道 133620"    xb unmark --tab 12 | --all    xb marks
xb pulse --selector "#q" --tab 12

# 租约（多客户端/多 agent 不打架）
xb claim --tab 12 --label "抓禅道" [--ttl 300]     # 之后同 tab 的命令自动带 token
xb leases                                          # 谁占着哪些标签、还剩多久
xb release --tab 12 | --all
xb panel                                           # 与扩展工具栏面板同一份快照
xb ext-reload                                      # 开发用：改完 manifest/图标后让扩展从磁盘重载

# 多浏览器（日常 Edge + 测试 profile 可以同时在线，互不干扰）
xb browsers                                        # 列出已连上的浏览器：实例 id / 标签 / 版本 / 在线时长
xb label "日常"                                    # 给当前浏览器起个名（之后 --browser 日常 即可）
xb use "日常"                                      # 把某个浏览器设为这个仓库的默认目标
xb --browser 日常 open URL                          # 单次指定目标；也可用环境变量 XB_BROWSER
xb --browser c7cae23ec8fc tabs                      # 实例 id 精确指定（测试脚本用这个）

# 等待（bsk 的可靠性核心）
xb wait-ms 500
xb wait-for-element "#done" --state visible|hidden|attached|detached --timeout 15000
xb wait-for-navigation --url-pattern "submitted" --timeout 8000

# 观测（debugger 环形缓冲，最多 500 条）
xb console [--limit 30 --clear]        xb network [--filter api --failed-only]
xb emulate --width 390 --height 844    xb emulate --off
xb request-help --message "请手动登录" --timeout 300000

# 标签 / 窗口
xb tabs / xb windows / xb open URL [--background] [--group-title T --color blue] / xb close 1,2
xb tab-select 3 / xb sleep 3 / xb split 3 4 / xb unsplit <splitViewId> / xb zoom 3 1.25
xb window-resize --state normal --width 1200 --height 800 [--left 120 --top 80]

# 标签组（bsk 完全没有）
xb group new --tabs 1,2,3 --title "PR 评审" --color blue
xb group list / xb group set 7 --color green --title 改好了 --collapse
xb group add 7 8 / xb group rm 8 / xb group close 7 / xb group move 7 --index 0

# 会话 / 诊断
xb recent / xb restore [sessionId] / xb doctor / xb apis / xb logs -n 50
```

所有命令加 `--json`（放在子命令前）输出机器可读结果；失败时 stdout 是 `{"ok":false,"error":{...}}`。

## 多浏览器

扩展在 `storage.local` 里存一个稳定的 `instanceId`，每次 hello 带上它（还有人可读的 label），
桥按 id 建注册表。所以"你的日常 Edge"和"跑测试的一次性 profile"能同时连着：

| 场景 | 行为 |
|---|---|
| 只连了一个浏览器 | `browser` 省略即可，自动用它 |
| 连了多个、没指定目标 | 报 `browser_required`（HTTP 409 / exit 2），列出所有实例；不会瞎猜 |
| `--browser <id或label>` | 精确匹配 id → 精确匹配 label → 唯一前缀；歧义报 `browser_ambiguous` |
| label 打错 | `browser_not_found`（404 / exit 3） |
| 目标浏览器中途关掉 | 只有发给它的 in-flight 调用被立刻唤醒（`extension_unavailable`），别的浏览器上的命令不受影响 |

测试隔离：`python test/launch.py` 会把新 profile 的实例 id 写进 `test/.instance`，
`test/e2e.py` 给每条命令都加 `--browser`；如果同时连着多个浏览器又找不到 `.instance`，
e2e 直接拒绝运行（exit 2），避免误操作用户自己的窗口。

## 错误契约与退出码

每个失败都带三个字段，脚本可以据此决定重试与否：

```json
{"ok": false, "cmd": "click",
 "error": {"code": "target_not_found", "message": "element not found: #x (re-run snapshot)",
           "outcome": "not_started", "retryable": false}}
```

- `outcome`：`not_started`（页面没动）/ `applied`（前面的步骤已经生效，**不要整批重试**）/ `unknown`
- `retryable`：只有超时、调试器冲突这类可以安全重试

| 退出码 | code | 含义 |
|---|---|---|
| 2 | `invalid_params` / `unsupported_method` | 参数或命令名不对 |
| 3 | `no_target_tab` / `extension_unavailable` / `window_minimized` | 标签没了、扩展没连、窗口最小化导致视口 0x0 |
| 4 | `timeout` | 等超时（可重试） |
| 5 | `target_not_found` / `restricted_page` | 元素找不到、页面不可注入（`edge://` 等） |
| 6 | `tab_not_owned` / `stale_generation` / `batch_failed` | 租约冲突、代际过期、批量中途失败 |
| 7 | `debugger_conflict` | 别的调试器占着这个标签 |

## 与 bsk 的差距（42 个命令族）

| 已对齐 | 说明 |
|---|---|
| status/doctor/logs/daemon, tab, window, navigate(+back/forward/reload), snapshot, observe*, console, network, get-html, extract, click/hover/wheel/scroll-to/focus/blur/fill/press/select, upload, download, evaluate, wait-ms/wait-for-element/wait-for-navigation, request-help, emulate, screenshot | *observe 是轻量版（landmark/heading/控件统计），不是 bsk 的完整 VOM |
| **xb 独有** | 标签组全套 + `group move`；`split/unsplit`；`sleep`(tabs.discard)；`zoom`；`recent/restore`；**mark/unmark/marks/pulse**；**drag**；**batch**；**claim/release/leases**；`window-resize`；HTTP JSON 接口 |

| 尚未实现 | 为什么 |
|---|---|
| `record` / `video` | 需要录制管线（bsk 有 screencast + MP4 mux） |
| `debug`（请求重放/证据导出） | 依赖 bsk 的 network-store 与归档格式 |
| `cooperate` / `session` / `browsers` | bsk 是 Agent Window + 多实例注册；xb 直接操作你的窗口，单浏览器（租约已覆盖"谁在动这个标签"） |
| `install-skill` / `update` | 分发相关 |

## 实测踩过的坑（Edge 155）

1. **`chrome.debugger` 不能用浏览器级 CDP**：`Browser.setDownloadBehavior` 报 `wasn't found`，`Page.setDownloadBehavior` 报 `Cannot not access browser-level commands`。下载改走 `chrome.downloads` API：浏览器先存、命令报路径、CLI 再挪进 `--dir`。
2. **设备指标（emulate）挂在 DevTools 会话上**：命令结束就 detach 会把 override 一起丢掉。所以 `emulate` 保持附着，只有 `emulate --off` 才 detach。
3. **离屏/后台窗口会丢弃合成滚轮事件**：`wheel` 三级回退（`mouseWheel` → `synthesizeScrollGesture` → 页内 `scrollBy`），返回里的 `via` 说明走了哪条。
4. **后台标签不产帧，截图会一直等**：`Page.captureScreenshot` 在隐藏标签上不返回。现在先 `Page.setWebLifecycleState(active)` + 焦点仿真把它"解冻"，再截；仍不行就 20s 超时报错，绝不偷偷把标签切到前台。
5. **真实鼠标事件要求标签可用**：窗口最小化/离屏时 `mousePressed/Released` 会被丢掉（`mouseMoved` 却还有效）。`_fTrustedBox` 先用 `elementFromPoint` 验证落点，落不上就回落 DOM 点击；视口 0x0 时直接报 `window_minimized`。
6. **最小化窗口 = 视口 0x0**：新建标签页的 `innerWidth` 直接是 0，所有 `getBoundingClientRect` 都是 0。`xb window-resize --state normal` 可救；测试里 `window_sane()` 会在每次断言前自愈。
7. **`--window-position=-10000,-10000` 不要用**（Edge 155 拒绝越界 bounds，窗口会退化成 0x0）；要离屏就用正常坐标 + 不激活标签。
8. **睡眠标签（discarded）不能注入**：`executeScript` 会挂住。注入类辅助（mark/pulse/favicon）统一走 `safeInject`：跳过 discarded/特权页并设 4s 上限。
9. **`wait-for-navigation` 有竞态**：上一秒的 `press Enter` 可能已经把页面跳完，监听器才挂上。带 `--url-pattern` 时先做"已经到位"的快路径。
10. **MV3 CSP 禁 `eval`**：`eval`/`screenshot`/`hover`/`press`/`console`/`network` 都走 debugger，首次会弹「xb began debugging」提示条。
11. **`edge://`、扩展页、Web Store 不可注入**；`group close` 关掉窗口最后几个标签会让浏览器退出。
12. **SW 休眠**：keepalive alarm（30s）+ 断线指数退避（0.8s→15s）；浏览器晚于守护进程启动时，最多等一个 alarm 周期才连上。
13. **扩展卡片的"错误"按钮会保留历史记录**：`edge://extensions` 上出现红色"错误"不一定代表现在有问题——先看 `xb panel` 的 `faults`/`last fault`（worker 用 `error`/`unhandledrejection` 自己记录未捕获异常）。确认是历史记录后，点"错误 → 全部清除"抹掉即可。
14. **改完 `extension/sw.js` 必须换新 profile 重启浏览器**：unpacked 扩展不热重载；同一 profile 二次启动时命令行 `--load-extension` 有时不生效，用 `test/launch.py` 每次开新 profile 最稳。

## 结构

```
edge-agent/
  xb.py                     CLI（bsk 命令名 + 标签组 + 可见性 + 租约）
  bridge/server.py          本地桥：WS ← 扩展（多实例注册表），HTTP ← CLI（错误契约、断线即唤醒）
  extension/manifest.json   MV3：tabs tabGroups windows scripting debugger sessions downloads …
  extension/sw.js           命令路由 + 注入函数 + 标记/租约/代际 + 面板快照
  extension/popup.html/.js  工具栏面板（受控标签、租约倒计时、一键切前台）
  test/site/*               自测站点（惰性元素/表单/下拉/上传/下载/拖拽/表格/console/network）
  test/launch.py            起一个干净的测试 profile，并把它的实例 id 写进 test/.instance
  test/e2e.py               85 项端到端断言（全部锚定 test/.instance 指定的浏览器）
  test/kill-edge.ps1        只杀带指定 profile 标记的 msedge
```

## 自测

```bash
cd test/site && python -m http.server 8899 --bind 127.0.0.1   # 终端 A
python test/launch.py --profile xb-try                        # 终端 B：新 profile + 扩展
python test/e2e.py                                            # 终端 C
powershell -File test/kill-edge.ps1 -Match xb-try             # 清理
```

最近一次：**78/78 ALL GREEN**，覆盖等待/竞态、@eN 快照与引用点击、console/network 缓冲、
回车提交导航、select/hover/wheel/scroll-to、上传/下载落盘、extract 表格、emulate 手机视口、
截图几何（视口/全页/裁剪/元素/后台不抢焦点）、标签组全套、split/unsplit、sleep、recent、
错误契约（5/6 退出码与 outcome）、标记生命周期与自动标记、拖拽、可信点击、批量与部分失败语义、
租约/代际（外部调用被拒、导航后 stale_generation、release 后恢复）、面板（列表/前台/清标记）、
后台标签内等待（不抢焦点、SW 存活期间完成）。

命令覆盖：59 个声明子命令里 55 个被 e2e 实际调用；`request-help`（要人工点）、`restore`（会恢复已关标签）、
`serve`/`stop`（会把跑测试用的守护进程停掉）属于**故意手工**。

## 发布（Edge Add-ons）

```bash
python tools/build_store.py          # → dist/xb-bridge-<version>.zip（上传这个文件）
python tools/make_store_shots.py --page test/store-page.png   # → docs/store/*.png
```

`build_store.py` 不只是打包，它还会：

- 把 `sw.js` 里的 `const DEV_MODE = true` 改成 `false` —— 商店版无法让浏览器从磁盘重载扩展（`dev.reload` 返回 `unsupported_method`）
- 拦下 `__pycache__`、`.git`、`test/`、`.DS_Store` 这类会被 Partner Center 拒绝的文件
- 校验 manifest（MV3、description ≤ 132 字符、icons 16/32/48/128 全部存在）
- 打印将要上传的每个文件与最终 zip 体积

提交材料（可直接复制进 Partner Center）：`docs/STORE-LISTING.md`（属性、用途声明、逐条权限理由、远程代码声明、商店文案、审核测试说明）、`docs/PRIVACY.md`（隐私政策，托管后把 URL 填进去）。

当前包的权限是裁剪过的 9 项：`tabs tabGroups windows scripting debugger alarms storage sessions downloads`
+ `host_permissions: <all_urls>`。`history` / `bookmarks` / `contextMenus` / `idle` / `offscreen` /
`notifications` / `sidePanel` / `webNavigation` 这 8 项代码里零调用，已从 manifest 移除
（申请了用不到的高危权限是商店最常见的打回原因）。

**不想上架、只给自己/团队装**，不用审核，走企业策略：

```
Edge 策略：ExtensionSettings → 添加扩展 ID → installation_mode: force_installed
           update_url: https://<你的服务器>/updates.xml   （crx 与 xml 同目录）
```

本机验证：

```bash
python xb.py browsers        # 实例 / 标签 / 版本 / 在线时长
python xb.py panel           # 面板同一份快照（含未捕获异常计数）
python test/e2e.py           # 87 项断言，全部锚定 test/.instance 指定的浏览器
```

## 参考资料

- bsk 源码（MIT）：`C:\WorkSpace\HermesWorkSpace\bsk-reference`
  - 等待：`crates/bsk-cli/src/cli/waits.rs`、`browser_wait.rs`
  - 交互：`interaction.rs`、`wheel.rs`、`scroll.rs`
  - 文件：`upload.rs`、`download.rs`
  - 观测：`console.rs`、`network.rs`、`apps/extension/src/debug/network-store.ts`
  - 结构化抽取：`extract.rs`；iframe：`apps/extension/src/browser-driver/frame-graph.ts`（xb 目前只合并同源 frame，跨域仍走 allFrames 注入）
- 可借鉴的商用实现清单（视觉标记、错误契约、租约、claim/generation、批量、drag、跨 iframe 坐标）：
  skill `browser-extension-agent-bridge` 的 `references/workbuddy-mechanisms.md`
