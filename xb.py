#!/usr/bin/env python3
"""xb — CLI for the Edge/Chromium agent bridge.

Command names mirror `bsk` (Tencent/BrowserSkill): navigate, snapshot, click,
hover, wheel, scroll-to, focus, blur, fill, press, select, upload, download,
wait-ms, wait-for-element, wait-for-navigation, emulate, extract, text,
get-html, console, network, request-help — plus the things bsk lacks:
tab groups, split view, tab discard ("sleep"), session restore, per-tab zoom.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

WS_PORT = int(os.environ.get("XB_WS_PORT", "8787"))
HTTP_PORT = int(os.environ.get("XB_HTTP_PORT", "8788"))
BASE = f"http://127.0.0.1:{HTTP_PORT}"
# When frozen by PyInstaller the code lives in a temp _MEIPASS dir while the
# per-user state must survive: resources come from _MEIPASS, state from
# %LOCALAPPDATA%\XBBridge.
FROZEN = bool(getattr(sys, "frozen", False))
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if FROZEN:
    STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "XBBridge")
else:
    STATE_DIR = os.path.join(HERE, ".xb")
SERVER = os.path.join(HERE, "bridge", "server.py")
LOG = os.path.join(STATE_DIR, "server.log")
COLORS = ["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan"]
JSON_MODE = False
BROWSER: str | None = None      # resolved target browser (id or label), set in main()

# exit codes: stable per failure class so scripts can branch without parsing
EXIT_BY_CODE = {
    "invalid_params": 2, "unsupported_method": 2,
    "no_target_tab": 3, "extension_unavailable": 3, "unavailable": 3, "bridge_unavailable": 3,
    "window_minimized": 3, "browser_not_found": 3,
    "browser_required": 2, "browser_ambiguous": 2,
    "timeout": 4,
    "target_not_found": 5, "restricted_page": 5, "site_blocked": 5,
    "paused": 3,
    "stale_generation": 6, "tab_not_owned": 6, "claim_required": 6, "batch_failed": 6,
    "debugger_conflict": 7,
}


class XbFailure(RuntimeError):
    """A failed command with the extension's error contract attached."""

    def __init__(self, code: str, message: str, outcome: str = "unknown",
                 retryable: bool = False, cmd: str = "", details=None):
        super().__init__(message)
        self.code = code
        self.outcome = outcome
        self.retryable = retryable
        self.cmd = cmd
        self.details = details

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CODE.get(self.code, 1)

    def render(self) -> None:
        if JSON_MODE:
            err = {"code": self.code, "message": str(self),
                   "outcome": self.outcome, "retryable": self.retryable}
            if getattr(self, "details", None) is not None:
                err["details"] = self.details
            print(json.dumps({"ok": False, "cmd": self.cmd, "error": err}, ensure_ascii=False))
            return
        bits = [f"xb: {self.cmd + ' failed — ' if self.cmd else ''}{self}"]
        meta = [self.code]
        if self.outcome and self.outcome != "not_started":
            meta.append(f"outcome={self.outcome}")
        if self.retryable:
            meta.append("retryable")
        bits.append(f"     [{', '.join(meta)}]")
        print("\n".join(bits), file=sys.stderr)


# ------------------------------------------------------------------ plumbing
def http(path: str, payload: dict | None = None, timeout: float = 60.0):
    url = path if path.startswith("http") else BASE + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def daemon_alive() -> bool:
    try:
        return bool(http("/health", timeout=2).get("ok"))
    except Exception:
        return False


def do_diag(args) -> int:
    """Collect everything a bug report needs into one zip.

    A support round trip otherwise costs four messages ("what does xb status
    say", "what is in the daemon log", "which extension build", "what were the
    last faults"). This writes them all, with secrets dropped.
    """
    import platform
    import zipfile
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(STATE_DIR, f"xb-diag-{stamp}.zip")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    files: dict[str, str] = {}

    env = {
        "xb": {"frozen": FROZEN, "argv0": sys.argv[0], "state_dir": STATE_DIR},
        "python": sys.version,
        "platform": platform.platform(),
        "ws_port": WS_PORT,
        "http_port": HTTP_PORT,
        "default_browser": load_default_browser(),
        "session": os.environ.get("XB_BROWSER"),
    }
    files["env.json"] = json.dumps(env, ensure_ascii=False, indent=2)

    def capture(name: str, fn) -> None:
        try:
            value = fn()
            files[name] = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except XbFailure as exc:
            files[name] = json.dumps({"error": exc.payload() if hasattr(exc, "payload") else str(exc)},
                                     ensure_ascii=False, indent=2)
        except Exception as exc:
            files[name] = json.dumps({"error": repr(exc)}, ensure_ascii=False, indent=2)

    capture("status.json", lambda: http("/health", None, timeout=5) if daemon_alive() else {"daemon": "down"})
    capture("browsers.json", lambda: rpc("browser.list", {}))
    capture("panel.json", lambda: rpc("panel.state", {}))
    capture("tabs.json", lambda: rpc("tab.list", {})[:40])
    if args.tab:
        capture("network.json", lambda: rpc("network.read", {"tabId": args.tab, "limit": 40}))
        capture("console.json", lambda: rpc("console.read", {"tabId": args.tab, "limit": 40}))
    if os.path.exists(LOG):
        with open(LOG, "rb") as fh:
            fh.seek(max(0, os.path.getsize(LOG) - 60000))
            files["server.log"] = fh.read().decode("utf-8", "replace")
    man_path = os.path.join(HERE, "extension", "manifest.json")
    if os.path.exists(man_path):
        with open(man_path, encoding="utf-8") as fh:
            files["manifest.json"] = fh.read()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    print(f"bundle: {out}  ({len(files)} file(s), {os.path.getsize(out):,} B)")
    for name in sorted(files):
        print(f"  - {name}")
    return 0


def ensure_daemon(quiet: bool = False) -> None:
    if daemon_alive():
        return
    # 新机器最常见的失败：没装 websockets，daemon 起来就退出，报错落在 .xb/server.log 里
    # 而 CLI 只会说 "could not start bridge daemon"。先探一下，给出确切修法。
    try:
        import websockets  # noqa: F401
    except ImportError:
        raise XbFailure(
            "bridge_unavailable",
            "缺少 Python 依赖 websockets —— 先跑 `pip install websockets`（或 "
            "`pip install -r requirements.txt`）再试",
            retryable=False, cmd="daemon")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    argv = [sys.executable, "serve"] if FROZEN else [sys.executable, SERVER]
    with open(LOG, "ab") as logf:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=logf, creationflags=flags, close_fds=True)
    for _ in range(60):
        if daemon_alive():
            if not quiet:
                print(f"(started bridge daemon :{HTTP_PORT})", file=sys.stderr)
            return
        time.sleep(0.1)
    raise XbFailure("bridge_unavailable",
                    f"could not start bridge daemon — see {LOG}",
                    retryable=True, cmd="daemon")


def rpc(cmd: str, args: dict | None = None, timeout: float = 60.0):
    args = {k: v for k, v in (args or {}).items() if v is not None}  # never send nulls
    tab_id = args.get("tabId")
    if tab_id is not None and "lease" not in args:
        token = load_leases().get(str(tab_id))
        if token:
            args["lease"] = token
    ensure_daemon(quiet=True)
    payload = {"cmd": cmd, "args": args or {}, "timeout": timeout}
    if BROWSER:
        payload["browser"] = BROWSER
    try:
        res = http("/rpc", payload, timeout=timeout + 5)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body).get("error")
        except Exception:
            err = None
        if isinstance(err, dict):
            raise XbFailure(err.get("code") or "command_failed",
                            err.get("message") or f"{cmd} failed",
                            outcome=err.get("outcome") or "unknown",
                            retryable=bool(err.get("retryable")), cmd=cmd,
                            details=err.get("details"))
        raise XbFailure("command_failed", str(err or body or f"{cmd} failed"), cmd=cmd)
    except urllib.error.URLError as e:
        raise XbFailure("bridge_unavailable", f"bridge unreachable ({e})",
                        retryable=True, cmd=cmd)
    if not res.get("ok"):
        err = res.get("error")
        if isinstance(err, dict):
            raise XbFailure(err.get("code") or "command_failed",
                            err.get("message") or f"{cmd} failed",
                            outcome=err.get("outcome") or "unknown",
                            retryable=bool(err.get("retryable")), cmd=cmd,
                            details=err.get("details"))
        raise XbFailure("command_failed", str(err or f"{cmd} failed"), cmd=cmd)
    return res.get("result")


def emit(obj, as_json: bool, human=None) -> None:
    if as_json or human is None:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        human(obj)


def ids(spec: str) -> list[int]:
    return [int(x) for x in str(spec).replace(" ", "").split(",") if x]


def csv_list(spec: str) -> list[str]:
    return [x.strip() for x in str(spec).split(",") if x.strip()]


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(url or "").hostname or (url or "")
    except Exception:
        return url or ""


# ------------------------------------------------------------- browser target
def _default_browser_path() -> str:
    return os.path.join(STATE_DIR, "default-browser")


def load_default_browser() -> str:
    try:
        with open(_default_browser_path(), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def save_default_browser(value: str) -> None:
    path = _default_browser_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if value:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
            f.write("\n")
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def target_browser(explicit: str | None) -> str | None:
    """--browser wins, then XB_BROWSER, then the saved default."""
    return explicit or os.environ.get("XB_BROWSER") or load_default_browser() or None


def list_browsers() -> list[dict]:
    """Ask the daemon which browsers are connected (no browser selection needed)."""
    ensure_daemon(quiet=True)
    try:
        return http("/browsers", timeout=5).get("instances") or []
    except Exception:
        return []


def _pick_browser(rows: list[dict], want: str) -> dict:
    """Resolve a user-typed id/label/prefix against the connected browsers."""
    want = str(want).strip()
    low = want.lower()
    hits = [r for r in rows if r["instanceId"] == want]
    if not hits:
        hits = [r for r in rows if (r.get("label") or "").lower() == low]
    if not hits:
        hits = [r for r in rows if r["instanceId"].startswith(want)
                or (r.get("label") or "").lower().startswith(low)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise XbFailure("browser_not_found", f"no browser matches {want!r} — see `xb browsers`",
                        cmd="use")
    raise XbFailure("browser_ambiguous",
                    f"{want!r} matches {len(hits)} browsers: "
                    + ", ".join(h["instanceId"] for h in hits), cmd="use")


# ------------------------------------------------------------------- leases
def _lease_path() -> str:
    return os.path.join(STATE_DIR, "leases.json")


def load_leases() -> dict:
    try:
        with open(_lease_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_leases(data: dict) -> None:
    os.makedirs(os.path.dirname(_lease_path()), exist_ok=True)
    with open(_lease_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# -------------------------------------------------------------------- render
def show_tabs(tabs) -> None:
    print(f"{'ID':>7}  {'WIN':>4}  {'IDX':>3}  {'GRP':>4}  {'A':1}  TITLE")
    for t in tabs:
        g = "-" if t.get("groupId", -1) in (-1, None) else str(t["groupId"])
        mark = "*" if t.get("active") else " "
        print(f"{t['id']:>7}  {t['windowId']:>4}  {t['index']:>3}  {g:>4}  {mark:1}  {(t.get('title') or '')[:70]}")


def show_groups(groups) -> None:
    if not groups:
        print("(no tab groups)")
        return
    for g in groups:
        state = "collapsed" if g.get("collapsed") else "expanded"
        print(f"group {g['id']}  [{g.get('color', '-'):<6}]  {g.get('title') or '(untitled)'}  "
              f"— {len(g.get('tabs', []))} tab(s), win {g.get('windowId')}, {state}")
        for t in g.get("tabs", []):
            print(f"    {t['id']:>7}  {(t.get('title') or '')[:66]}")


def show_snapshot(snap) -> None:
    print(f"# {snap.get('title') or ''}\n# {snap.get('url') or ''}")
    frames = snap.get("frames") or []
    if len(frames) > 1:
        print(f"frames: {len(frames)}  " + ", ".join(f"{f['frameId']}({f['items']})" for f in frames))
    for h in (snap.get("headings") or [])[:20]:
        print("  " + h)
    print(f"\n-- text --\n{(snap.get('text') or '').strip()[:3000]}")
    items = snap.get("items") or []
    if items:
        print(f"\n-- interactive ({len(items)}) --")
        for it in items[:150]:
            extra = f" -> {it['href']}" if it.get("href") else ""
            val = f" = {it['value']!r}" if it.get("value") else ""
            print(f"  {it['ref']:<9} {it['role']:<10} {it['name']}{val}{extra}")


def do_extract(args) -> dict:
    """Extract tables, optionally walking a "next page" control and merging pages."""
    tab = getattr(args, "tab", None)
    max_pages = max(1, int(getattr(args, "max_pages", 1) or 1))
    page_wait = max(0, int(getattr(args, "page_wait", 800) or 0)) / 1000.0
    pages: list[dict] = []
    signatures: list[list] = []
    for page in range(max_pages):
        res = rpc("extract", {"tabId": tab})
        tables = res.get("tables") or []
        sig = [((tb.get("headers") or [])[:3], (tb.get("rows") or [])[:1]) for tb in tables]
        if sig in signatures:                      # the "next" click did not advance
            break
        signatures.append(sig)
        pages.append(res)
        if not getattr(args, "next", None) or page + 1 >= max_pages:
            break
        try:
            rpc("click", {"tabId": tab, "selector": args.next,
                          "ref": args.next if str(args.next).startswith("@") else None})
        except XbFailure as exc:
            print(f"(stopped after {page + 1} page(s): next control not clickable — {exc})",
                  file=sys.stderr)
            break
        time.sleep(page_wait)

    if len(pages) == 1:
        return {**pages[0], "pages": 1}

    merged: list[dict] = []
    for res in pages:
        for tb in res.get("tables") or []:
            key = tuple(tb.get("headers") or [])
            slot = next((m for m in merged if tuple(m.get("headers") or []) == key), None)
            if slot is None:
                slot = {"headers": list(key), "rows": []}
                merged.append(slot)
            slot["rows"].extend(tb.get("rows") or [])
    return {"pages": len(pages), "tables": merged,
            "url": pages[-1].get("url"), "merged": True}


def do_download(args) -> None:
    """Trigger the download, let the browser save it, then move it into --dir."""
    d = os.path.abspath(args.dir)
    os.makedirs(d, exist_ok=True)
    since = int(time.time() * 1000)
    if args.url:
        rpc("navigate", {"tabId": args.tab, "url": args.url})
    elif args.target:
        rpc("click", {"tabId": args.tab, "ref": args.target if args.target.startswith("@") else None,
                      "selector": None if args.target.startswith("@") else args.target})
    else:
        raise XbFailure("invalid_params",
                        "download needs a URL or a ref/selector that triggers a download",
                        cmd="download")

    # 同一个文件下第二次时，downloads.search 会把**上一次已完成**的记录再报一遍，
    # 而它的文件已经被我们搬走了 → "报了路径但文件不存在"。所以这里给几次机会，
    # 只接受真实存在的文件，并把已消费的路径记下来跳过。
    deadline = time.time() + max(1.0, args.timeout)
    seen: set[str] = set()
    src = ""
    while True:
        left = max(1.0, deadline - time.time())
        info = rpc("download.wait",
                   {"sinceMs": since, "timeoutMs": int(min(left, 30) * 1000)},
                   timeout=left + 20)
        candidate = info.get("filename") or ""
        if candidate and candidate not in seen and os.path.exists(candidate):
            src = candidate
            break
        if candidate:
            seen.add(candidate)
        if time.time() >= deadline:
            raise XbFailure("target_not_found",
                            f"the browser reported {candidate or 'nothing'} but no file is there "
                            f"({len(seen)} stale record(s) skipped)", cmd="download")
        time.sleep(0.3)

    dst = os.path.join(d, os.path.basename(src))
    if os.path.abspath(src) != dst:
        try:
            os.replace(src, dst)
        except OSError:
            import shutil
            shutil.copy2(src, dst)
    print(dst)


# --------------------------------------------------------------------- build
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xb", description="Edge/Chromium agent bridge (bsk-parity CLI)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--session", help="bsk compatibility: accepted and ignored (xb has one live browser)")
    p.add_argument("--browser", help="target browser: instance id or label (default: $XB_BROWSER, then `xb use`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    tgt = argparse.ArgumentParser(add_help=False)
    tgt.add_argument("target", nargs="?", help="ref (@e12) or css selector")
    # distinct dests: `fill`/`type` already have a positional called `text`
    tgt.add_argument("--text", dest="locate_text", help="locate by visible text instead of a selector")
    tgt.add_argument("--label", dest="locate_label", help="locate by aria-label / placeholder / name")
    tgt.add_argument("--exact", dest="locate_exact", action="store_true",
                     help="require an exact text/label match")
    tgt.add_argument("--tab", type=int)
    tgt.add_argument("--no-mark", action="store_true", dest="no_mark",
                     help="don't paint the control marker on this tab")
    tgt.add_argument("--mark-label", dest="mark_label", help="label shown in the marker chip")

    # infra
    sub.add_parser("status")
    sub.add_parser("pause", help="stop the agent from touching any tab (toolbar panel also has this)")
    sub.add_parser("resume", help="undo xb pause")
    sub.add_parser("host-wake", help="ask the browser to start the bridge via native messaging")
    sub.add_parser("native-host", help="run the native messaging host on stdio (used by Edge)")
    hp = sub.add_parser("host", help="install/inspect the native messaging host")
    hp.add_argument("action", nargs="?", default="status",
                    choices=["status", "install", "uninstall"])
    hp.add_argument("--extension-id", nargs="+")
    sub.add_parser("blocked", help="list host patterns the agent must never touch")
    bl = sub.add_parser("block", help="never touch this site again"); bl.add_argument("pattern")
    ub = sub.add_parser("unblock", help="remove a blocked pattern"); ub.add_argument("pattern")
    sub.add_parser("browsers", help="list connected browsers and which one is the default")
    use = sub.add_parser("use", help="make a browser the default for this repo")
    use.add_argument("target", help="instance id or label")
    lb = sub.add_parser("label", help="name the selected browser (then --browser <label> works)")
    lb.add_argument("text", nargs="?", default="")
    sub.add_parser("doctor")
    dg = sub.add_parser("diag", help="collect a support bundle (versions, state, logs) into a zip")
    dg.add_argument("--out", help="zip path (default: .xb/xb-diag-<timestamp>.zip)")
    dg.add_argument("--tab", type=int)
    sub.add_parser("apis")
    sub.add_parser("serve")
    sub.add_parser("stop")
    sub.add_parser("logs").add_argument("-n", type=int, default=40)

    # tabs
    t = sub.add_parser("tabs"); t.add_argument("--window", type=int); t.add_argument("--group", type=int)
    o = sub.add_parser("open", help="open a tab (optionally into a new group)")
    o.add_argument("url"); o.add_argument("--background", action="store_true")
    o.add_argument("--group-title"); o.add_argument("--color", choices=COLORS)
    o.add_argument("--new-group", action="store_true",
                   help="force a fresh group even if one with the same XB[...] title exists")
    c = sub.add_parser("close"); c.add_argument("tab_ids")
    s = sub.add_parser("tab-select", help="activate a tab")
    s.add_argument("tab_id", type=int); s.add_argument("--no-focus", action="store_true")
    sub.add_parser("windows")
    wr = sub.add_parser("window-resize", help="resize/move/unminimise a browser window")
    wr.add_argument("--window", type=int); wr.add_argument("--width", type=int); wr.add_argument("--height", type=int)
    wr.add_argument("--left", type=int); wr.add_argument("--top", type=int)
    wr.add_argument("--state", choices=["normal", "minimized", "maximized", "fullscreen"])
    sl = sub.add_parser("sleep", help="discard tabs (≈ Edge sleeping tabs)"); sl.add_argument("tab_ids")
    sp = sub.add_parser("split", help="split view: two tabs side by side")
    sp.add_argument("tab_id", type=int); sp.add_argument("other_tab_id", type=int)
    uns = sub.add_parser("unsplit"); uns.add_argument("split_view_id", type=int)
    zm = sub.add_parser("zoom"); zm.add_argument("tab_id", type=int); zm.add_argument("factor", nargs="?", type=float)

    # groups
    g = sub.add_parser("group")
    gs = g.add_subparsers(dest="gcmd", required=True)
    gn = gs.add_parser("new"); gn.add_argument("--tabs", required=True); gn.add_argument("--title", default="")
    gn.add_argument("--color", choices=COLORS); gn.add_argument("--collapse", action="store_true")
    gl = gs.add_parser("list"); gl.add_argument("--window", type=int)
    gu = gs.add_parser("set"); gu.add_argument("group_id", type=int); gu.add_argument("--title")
    gu.add_argument("--raw-title", action="store_true",
                    help="rename without the XB[...] prefix the extension normally adds")
    gu.add_argument("--color", choices=COLORS); gu.add_argument("--collapse", action="store_true")
    gu.add_argument("--expand", action="store_true")
    ga = gs.add_parser("add"); ga.add_argument("group_id", type=int); ga.add_argument("tabs")
    gr = gs.add_parser("rm"); gr.add_argument("tabs")
    gc = gs.add_parser("close"); gc.add_argument("group_id", type=int)
    gm = gs.add_parser("move"); gm.add_argument("group_id", type=int)
    gm.add_argument("--index", type=int, default=0); gm.add_argument("--window", type=int)

    # navigation
    n = sub.add_parser("navigate"); n.add_argument("url"); n.add_argument("--tab", type=int)
    for nm in ("navigate-back", "navigate-forward", "reload"):
        sub.add_parser(nm).add_argument("--tab", type=int)

    # reading
    sn = sub.add_parser("snapshot"); sn.add_argument("--tab", type=int)
    sn.add_argument("--max-items", type=int, default=300)
    ob = sub.add_parser("observe"); ob.add_argument("--tab", type=int)
    tx = sub.add_parser("text"); tx.add_argument("--tab", type=int); tx.add_argument("--selector")
    gh = sub.add_parser("get-html", parents=[tgt])
    lo = sub.add_parser("locate", help="find an element by text/aria-label and print its selector")
    lo.add_argument("--text"); lo.add_argument("--label"); lo.add_argument("--exact", action="store_true")
    lo.add_argument("--tab", type=int)
    ex = sub.add_parser("extract"); ex.add_argument("--tab", type=int); ex.add_argument("--out")
    ex.add_argument("--csv", action="store_true")
    ex.add_argument("--next", help="selector/ref of the 'next page' control; loops it")
    ex.add_argument("--max-pages", type=int, default=1)
    ex.add_argument("--page-wait", type=int, default=800, help="ms to wait after clicking next")
    ev = sub.add_parser("eval", help="evaluate JS (CDP)"); ev.add_argument("code"); ev.add_argument("--tab", type=int)

    # interaction
    ck = sub.add_parser("click", parents=[tgt])
    ck.add_argument("--trusted", action="store_true", help="dispatch real CDP mouse events at the element centre")
    ck.add_argument("--double", action="store_true", help="double click")
    fl = sub.add_parser("fill", parents=[tgt]); fl.add_argument("text", nargs="?", default="")
    ty = sub.add_parser("type", parents=[tgt],
                        help="click a field and type like a person (works in shadow DOM)")
    ty.add_argument("text", nargs="?", default="")
    ty.add_argument("--append", action="store_true", help="keep what is already in the field")
    hv = sub.add_parser("hover", parents=[tgt])
    st = sub.add_parser("scroll-to", parents=[tgt])
    fo = sub.add_parser("focus", parents=[tgt])
    bl = sub.add_parser("blur", parents=[tgt])
    pr = sub.add_parser("press"); pr.add_argument("keys")
    pr.add_argument("--ref"); pr.add_argument("--selector"); pr.add_argument("--tab", type=int)
    se = sub.add_parser("select", help="set <select> option(s) by value or label")
    se.add_argument("target"); se.add_argument("--values"); se.add_argument("--value"); se.add_argument("--tab", type=int)
    wh = sub.add_parser("wheel"); wh.add_argument("--delta-y", type=int, default=300)
    wh.add_argument("--delta-x", type=int, default=0)
    wh.add_argument("--ref"); wh.add_argument("--selector"); wh.add_argument("--tab", type=int)
    up = sub.add_parser("upload"); up.add_argument("target", nargs="?", default=""); up.add_argument("--files", required=True)
    up.add_argument("--shadow-index", type=int,
                    help="pick the Nth <input type=file> even inside shadow roots (no selector needed)")
    up.add_argument("--tab", type=int)
    dr = sub.add_parser("drag", help="drag from one point/element to another with real CDP events")
    dr.add_argument("from_", metavar="from", help="\"x,y\", a CSS selector or @eN")
    dr.add_argument("to", help="\"x,y\", a CSS selector or @eN")
    dr.add_argument("--tab", type=int)
    dr.add_argument("--steps", type=int, default=12)
    dr.add_argument("--duration", type=float, default=320, help="total drag time in ms")
    dr.add_argument("--no-mark", action="store_true", dest="no_mark")
    dr.add_argument("--mark-label", dest="mark_label")
    ba = sub.add_parser("batch", help="run a JSON array of {cmd,args} steps in one round trip")
    ba.add_argument("--file", help="read steps from this file (default: stdin)")
    ba.add_argument("--tab", type=int, help="default tabId for steps that omit one")
    ba.add_argument("--interval-ms", type=int, default=0, dest="interval_ms")
    ba.add_argument("--continue-on-error", action="store_true", dest="continue_on_error")
    dl = sub.add_parser("download"); dl.add_argument("target", nargs="?"); dl.add_argument("--url")
    dl.add_argument("--dir", required=True); dl.add_argument("--tab", type=int); dl.add_argument("--timeout", type=float, default=30)

    # waits
    wm = sub.add_parser("wait-ms"); wm.add_argument("ms", type=int)
    we = sub.add_parser("wait-for-element"); we.add_argument("target")
    we.add_argument("--state", default="visible", choices=["visible", "hidden", "attached", "detached"])
    we.add_argument("--timeout", type=int, default=15000); we.add_argument("--tab", type=int)
    wn = sub.add_parser("wait-for-navigation"); wn.add_argument("--url-pattern")
    wn.add_argument("--timeout", type=int, default=30000); wn.add_argument("--tab", type=int)

    # human loop / observability
    rh = sub.add_parser("request-help"); rh.add_argument("--message")
    rh.add_argument("--timeout", type=int, default=300000); rh.add_argument("--tab", type=int)
    cn = sub.add_parser("console"); cn.add_argument("--tab", type=int); cn.add_argument("--limit", type=int)
    cn.add_argument("--clear", action="store_true")
    nw = sub.add_parser("network"); nw.add_argument("--tab", type=int); nw.add_argument("--filter")
    nw.add_argument("--limit", type=int); nw.add_argument("--failed-only", action="store_true")
    nw.add_argument("--clear", action="store_true")
    nw.add_argument("--har", help="write a HAR 1.2 file")
    nw.add_argument("--wait-pattern", help="wait for a request whose URL matches this regex")
    nw.add_argument("--click", help="with --wait-pattern: arm capture, click this selector, then wait")
    nw.add_argument("--method", help="with --wait-pattern: only this HTTP method")
    nw.add_argument("--timeout", type=float, default=15.0, help="seconds to wait")
    nw.add_argument("--since", type=int, help="epoch ms lower bound")
    nw.add_argument("--body", type=int, help="print the response body of entry N")
    nw.add_argument("--replay", type=int, help="re-issue request N with the page's own session")
    nw.add_argument("--replay-body", help="override the request body when replaying")

    # capture
    for nm in ("screenshot", "shot"):
        sh = sub.add_parser(nm, help="capture the tab (viewport, full page, clip or element)")
        sh.add_argument("out")
        sh.add_argument("--tab", type=int)
        sh.add_argument("--full", action="store_true", help="whole scrollable page")
        sh.add_argument("--ref"); sh.add_argument("--selector")
        sh.add_argument("--clip", help="x,y,width,height in CSS pixels")
        sh.add_argument("--format", choices=["png", "jpeg"], default="png")
        sh.add_argument("--quality", type=int, help="jpeg quality 1-100 (default 80)")
        sh.add_argument("--scale-to", type=int, dest="scale_to", default=None,
                        help="max output dimension in CSS px (default 2000)")
        sh.add_argument("--foreground", action="store_true",
                        help="allow the captureVisibleTab fallback to bring the tab to the front")

    em = sub.add_parser("emulate"); em.add_argument("--tab", type=int); em.add_argument("--width", type=int)
    em.add_argument("--height", type=int); em.add_argument("--desktop", action="store_true")
    em.add_argument("--ua"); em.add_argument("--off", action="store_true")

    sub.add_parser("recent").add_argument("--limit", type=int, default=10)
    rs = sub.add_parser("restore"); rs.add_argument("session_id", nargs="?")

    # visibility
    mk = sub.add_parser("mark", help="paint the control marker on a tab")
    mk.add_argument("--tab", type=int); mk.add_argument("--label", default="")
    um = sub.add_parser("unmark", help="remove the marker (and restore the favicon)")
    um.add_argument("--tab", type=int); um.add_argument("--all", action="store_true")
    sub.add_parser("marks", help="list marked tabs")
    pu = sub.add_parser("pulse", help="flash a ring at a point/element")
    pu.add_argument("--tab", type=int); pu.add_argument("--ref"); pu.add_argument("--selector")
    pu.add_argument("--x", type=int); pu.add_argument("--y", type=int)
    pu.add_argument("--no-mark", action="store_true", dest="no_mark")
    pu.add_argument("--mark-label", dest="mark_label")

    # claims
    cl = sub.add_parser("claim", help="claim a tab so other callers cannot drive it")
    cl.add_argument("--tab", type=int); cl.add_argument("--label", default="")
    cl.add_argument("--ttl", type=int, default=None, help="lease TTL in seconds")
    rl = sub.add_parser("release", help="release a claim (and its marker)")
    rl.add_argument("--tab", type=int); rl.add_argument("--all", action="store_true")
    sub.add_parser("leases", help="list active claims")
    sub.add_parser("panel", help="tabs the agent controls (same view as the toolbar popup)")
    sub.add_parser("ext-reload", help="reload the unpacked extension from disk (dev: picks up manifest/icon edits)")

    return p


def main(argv=None) -> int:
    if FROZEN:     # the frozen console defaults to the OEM codepage; keep UTF-8
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    global JSON_MODE, BROWSER
    args = build_parser().parse_args(argv)
    j = args.json
    JSON_MODE = bool(j)
    BROWSER = target_browser(getattr(args, "browser", None))
    tab = getattr(args, "tab", None)

    # --text / --label: resolve to a real selector first, then every element command
    # behaves exactly as if it had been given that selector.
    wants_locate = bool(getattr(args, "locate_text", None) or getattr(args, "locate_label", None))
    if wants_locate and not (getattr(args, "target", "") or "").startswith("@"):
        loc = rpc("locate", {"tabId": tab, "text": getattr(args, "locate_text", None),
                             "label": getattr(args, "locate_label", None),
                             "exact": True if getattr(args, "locate_exact", False) else None})
        args.target = loc["selector"]
        if not JSON_MODE:
            print(f"(located {loc.get('name')!r} -> {loc['selector']}, {loc.get('candidates')} candidate(s))",
                  file=sys.stderr)

    if args.cmd in ("browsers", "use", "label"):
        rows = list_browsers()
        default = load_default_browser()

        if args.cmd == "browsers":
            if j:
                print(json.dumps({"default": default, "count": len(rows), "instances": rows},
                                 ensure_ascii=False, indent=2))
            elif not rows:
                print("(no browser connected) — open Edge with the extension loaded, "
                      f"it dials ws://127.0.0.1:{WS_PORT}")
            else:
                print(f"{'':2}{'INSTANCE':<14}{'LABEL':<16}{'VER':<8}{'BROWSER':<8}UP")
                for r in rows:
                    mark = "*" if default and (r["instanceId"] == default
                                               or (r.get("label") or "") == default) else " "
                    print(f"{mark:2}{r['instanceId']:<14}{(r.get('label') or '-'):<16}"
                          f"{(r.get('version') or '-'):<8}{(r.get('browser') or '-'):<8}"
                          f"{r.get('connectedFor', 0):.0f}s")
                if not default:
                    print("\n(*) none; `xb use <instance|label>` picks a default for this repo")
            return 0

        if args.cmd == "use":
            want = args.target
            rows = list_browsers()
            if not rows:
                raise XbFailure("browser_not_found", "no browser is connected", cmd="use")
            hit = _pick_browser(rows, want)
            save_default_browser(hit["instanceId"])
            emit({"default": hit["instanceId"], "label": hit.get("label") or "", **hit}, j,
                 lambda r: print(f"default browser -> {r['instanceId']}"
                                 + (f" ({r['label']})" if r.get("label") else "")))
            return 0

        # label
        r = rpc("label.set", {"label": args.text})
        if not default:
            save_default_browser(r["instanceId"])
        emit(r, j, lambda rr: print(f"browser {rr['instanceId']} labelled "
                                    + (f"'{rr['label']}'" if rr["label"] else "(cleared)")))
        return 0

    if args.cmd == "native-host":
        import runpy
        runpy.run_path(os.path.join(HERE, "bridge", "native_host.py"), run_name="__main__")
        return 0

    if args.cmd == "host-wake":
        r = rpc("host.wake", {}, timeout=30)
        emit(r, j, lambda rr: print(
            ("bridge up: spawned=" + str(rr.get("spawned")) + f" ws:{rr.get('wsPort')} http:{rr.get('httpPort')}")
            if rr.get("ok") else f"native host unavailable: {rr.get('error')}"))
        return 0 if r.get("ok") else 3

    if args.cmd == "host":
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "install_native_host.py")
        argv = [sys.executable, script, "--status" if args.action == "status" else
                ("--uninstall" if args.action == "uninstall" else "--install")]
        if args.extension_id:
            argv += ["--extension-id", *args.extension_id]
        return subprocess.call(argv)

    if args.cmd == "diag":
        return do_diag(args)

    if args.cmd in ("pause", "resume", "blocked", "block", "unblock"):
        if args.cmd in ("pause", "resume"):
            r = rpc(args.cmd, {})
            emit(r, j, lambda rr: print("agent paused — every tab command will refuse until `xb resume`"
                                        if rr.get("paused") else "agent resumed"))
        else:
            if args.cmd == "blocked":
                r = rpc("block.list", {})
                emit(r, j, lambda rr: print(chr(10).join(rr.get("blocked") or []) or "(nothing blocked)"))
            elif args.cmd == "block":
                r = rpc("block.add", {"pattern": args.pattern})
                emit(r, j, lambda rr: print(f"blocked {rr['added']}  ({len(rr['blocked'])} pattern(s))"))
            else:
                r = rpc("block.remove", {"pattern": args.pattern})
                emit(r, j, lambda rr: print(f"unblocked {rr['removed']}  ({len(rr['blocked'])} left)"))
        return 0

    if args.cmd == "status":
        if not daemon_alive():
            ensure_daemon(quiet=True)
        h = http("/health")
        rows = h.get("instances") or []
        # only ask a browser for its tabs when the target is unambiguous
        ask = bool(rows) and (BROWSER is not None or len(rows) == 1)
        tabs, groups = [], []
        if ask:
            try:
                tabs = rpc("tab.list", {})
                groups = rpc("group.list", {})
            except XbFailure:
                ask = False       # a stale --browser/use target must not break status
        out = {"daemon": {"http": BASE, "pid": h.get("pid"), "ws_port": WS_PORT},
               "extension": {"connected": bool(rows), "browsers": rows, "count": len(rows)},
               "default": load_default_browser(),
               "target": BROWSER,
               "tabs": len(tabs) if ask else None,
               "groups": len(groups) if ask else None}

        def human(o):
            d, e = o["daemon"], o["extension"]
            print(f"daemon      : pid {d['pid']}  http {d['http']}  ws :{d['ws_port']}")
            if not e["connected"]:
                print("extension   : NOT connected")
                if FROZEN:
                    print("\n-> the browser extension is not talking to this bridge yet."
                          "\n   install XBBridge from the Edge Add-ons store (or load extension/ from the"
                          "\n   repository in developer mode), then open its toolbar panel once.")
                else:
                    print("\n-> load the extension: edge://extensions -> 开发人员模式 -> 加载解压缩的扩展 -> "
                          f"{os.path.join(HERE, 'extension')}")
                return
            print(f"extension   : {e['count']} browser(s) connected")
            default = o["default"]
            for r in e["browsers"]:
                mark = "*" if default and r["instanceId"] == default else " "
                label = f" ({r['label']})" if r.get("label") else ""
                print(f"  {mark} {r['instanceId']:<14}{label:<18}v{r.get('version', '-'):<8}"
                      f"{r.get('connectedFor', 0):.0f}s")
            if e["count"] == 1:
                print(f"tabs/groups : {o['tabs']} tab(s), {o['groups']} group(s)")
            elif o["target"]:
                print(f"target      : {o['target']} → {o['tabs']} tab(s), {o['groups']} group(s)")
            else:
                print(f"target      : ambiguous — pick one with `xb use <instance|label>` "
                      f"or --browser (no tabs queried)")
        emit(out, j, human)
        return 0

    if args.cmd == "serve":
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if FROZEN:
            # the exe cannot re-exec a .py file; run the bundled daemon in-process
            import runpy
            runpy.run_path(SERVER, run_name="__main__")
            return 0
        return subprocess.call([sys.executable, SERVER])
    if args.cmd == "stop":
        try:
            http("/shutdown", {}, timeout=3)
            print("bridge daemon stopped")
        except Exception:
            print("bridge daemon was not running")
        return 0
    if args.cmd == "logs":
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                print("".join(f.readlines()[-args.n:]))
        except FileNotFoundError:
            print(f"(no log yet at {LOG})")
        return 0
    if args.cmd in ("doctor", "apis"):
        cmd = "doctor" if args.cmd == "doctor" else "diag.apis"
        print(json.dumps(rpc(cmd, {}), ensure_ascii=False, indent=2))
        return 0

    # ---- tabs / windows / groups ----
    if args.cmd == "tabs":
        emit(rpc("tab.list", {"windowId": args.window, "groupId": args.group}), j, show_tabs)
    elif args.cmd == "windows":
        emit(rpc("window.list", {}), j, lambda ws_: [print(
            f"{w['id']:>4}  {'focused' if w['focused'] else '       '}  {w['tabs']:>3} tab(s)  {w['title'][:60]}")
            for w in ws_])
    elif args.cmd == "window-resize":
        wid = args.window
        if wid is None:
            tabs = rpc("tab.list", {})
            active = next((t for t in tabs if t.get("active")), tabs[0] if tabs else None)
            if not active:
                raise XbFailure("no_target_tab", "no tab to infer the window from", cmd="window-resize")
            wid = active["windowId"]
        emit(rpc("window.resize", {"windowId": wid, "width": args.width, "height": args.height,
                                   "left": args.left, "top": args.top, "state": args.state}), j,
             lambda r: print(f"window {r['id']} -> {r.get('width')}x{r.get('height')} ({r.get('state')})"))
    elif args.cmd == "open":
        ai = {"url": args.url, "active": not args.background}
        if args.group_title or args.color or args.new_group:
            ai["group"] = {k: v for k, v in (("title", args.group_title), ("color", args.color)) if v}
            if args.new_group:
                ai["group"]["reuse"] = False
        emit(rpc("tab.create", ai), j, lambda t: print(
            f"opened tab {t['id']} (group {t.get('groupIdOfCreated') or '-'}"
            f"{', joined existing' if t.get('groupReused') else ''})  {t.get('title', '')}"))
    elif args.cmd == "close":
        emit(rpc("tab.close", {"tabIds": ids(args.tab_ids)}), j,
             lambda r: print("closed:", ", ".join(map(str, r["closed"]))))
    elif args.cmd == "tab-select":
        emit(rpc("tab.select", {"tabId": args.tab_id, "focus": not args.no_focus}), j,
             lambda t: print(f"active tab {t['id']}  {t.get('title', '')}"))
    elif args.cmd == "sleep":
        emit(rpc("tab.sleep", {"tabIds": ids(args.tab_ids)}), j,
             lambda r: print("; ".join(f"{x['id']}:{'sleeping' if x.get('discarded') else x.get('error')}" for x in r)))
    elif args.cmd == "split":
        emit(rpc("tab.split", {"tabId": args.tab_id, "otherTabId": args.other_tab_id}), j,
             lambda r: print(f"split view {r['splitViewId']} over tabs {r['tabs']}"))
    elif args.cmd == "unsplit":
        emit(rpc("tab.unsplit", {"splitViewId": args.split_view_id}), j,
             lambda r: print(f"unsplit {r['unsplit']}"))
    elif args.cmd == "zoom":
        emit(rpc("tab.zoom", {"tabId": args.tab_id, "factor": args.factor}), j,
             lambda r: print(f"tab {r['tabId']} zoom {r['zoom']}"))
    elif args.cmd == "group":
        if args.gcmd == "new":
            ai = {"tabIds": ids(args.tabs), "title": args.title or None, "color": args.color,
                  "collapsed": True if args.collapse else None}
            emit(rpc("group.create", {k: v for k, v in ai.items() if v is not None}), j,
                 lambda gg: show_groups([gg]))
        elif args.gcmd == "list":
            emit(rpc("group.list", {"windowId": args.window}), j, show_groups)
        elif args.gcmd == "set":
            ai = {"groupId": args.group_id}
            if args.title is not None:
                ai["title"] = args.title
                if getattr(args, "raw_title", False):
                    ai["rawTitle"] = True
            if args.color:
                ai["color"] = args.color
            if args.collapse:
                ai["collapsed"] = True
            if args.expand:
                ai["collapsed"] = False
            emit(rpc("group.update", ai), j, lambda gg: print(
                f"group {gg['id']} -> [{gg['color']}] {gg.get('title', '')} "
                f"({'collapsed' if gg.get('collapsed') else 'expanded'})"))
        elif args.gcmd == "add":
            emit(rpc("group.add", {"groupId": args.group_id, "tabIds": ids(args.tabs)}), j,
                 lambda r: print(f"added {r['added']} to group {r['groupId']}"))
        elif args.gcmd == "rm":
            emit(rpc("group.remove", {"tabIds": ids(args.tabs)}), j,
                 lambda r: print("ungrouped:", ", ".join(map(str, r["ungrouped"]))))
        elif args.gcmd == "close":
            emit(rpc("group.close", {"groupId": args.group_id}), j,
                 lambda r: print(f"closed group {r['closedGroup']} ({len(r['closedTabs'])} tabs)"))
        elif args.gcmd == "move":
            emit(rpc("group.move", {"groupId": args.group_id, "index": args.index,
                                    "windowId": args.window}), j, lambda gg: print(
                f"group {gg['id']} moved in window {gg.get('windowId')}"))

    # ---- navigation ----
    elif args.cmd == "navigate":
        emit(rpc("navigate", {"tabId": tab, "url": args.url}), j,
             lambda r: print(f"tab {r['tabId']} -> {r['url']}"))
    elif args.cmd in ("navigate-back", "navigate-forward"):
        emit(rpc(args.cmd, {"tabId": tab}), j, lambda r: print(f"tab {r['tabId']} {r['dir']}"))
    elif args.cmd == "reload":
        emit(rpc("tab.reload", {"tabId": tab}), j, lambda r: print(f"reloaded {r['reloaded']}"))

    # ---- reading ----
    elif args.cmd == "snapshot":
        emit(rpc("snapshot", {"tabId": tab, "maxItems": args.max_items}), j, show_snapshot)
    elif args.cmd == "observe":
        print(json.dumps(rpc("observe", {"tabId": tab}), ensure_ascii=False, indent=2))
    elif args.cmd == "text":
        emit(rpc("text", {"tabId": tab, "selector": args.selector}), j,
             lambda r: print((r.get("text") or "").strip()))
    elif args.cmd == "get-html":
        r = rpc("get-html", {"tabId": tab,
                             "ref": args.target if (args.target or "").startswith("@") else None,
                             "selector": None if (args.target or "").startswith("@") else args.target})
        if j:
            print(json.dumps({"selector": r.get("selector"), "url": r.get("url"),
                              "html": r.get("html"), "bytes": len(r.get("html") or "")}, ensure_ascii=False))
        else:
            print(r["html"])
    elif args.cmd == "extract":
        extracted = do_extract(args)
        r = extracted
        if args.out:
            if args.csv:
                with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    for tb in r.get("tables", []):
                        w.writerow(tb.get("headers", []))
                        w.writerows(tb.get("rows", []))
                        w.writerow([])
            else:
                with open(args.out, "w", encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False, indent=2)
            print(f"written {args.out}  ({r.get('pages', 1)} page(s), "
                  f"{sum(len(t.get('rows') or []) for t in r.get('tables') or [])} row(s))")
        else:
            print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "locate":
        emit(rpc("locate", {"tabId": tab, "text": args.text, "label": args.label,
                            "exact": True if args.exact else None}), j,
             lambda rr: print(f"{rr.get('name')!r} -> {rr['selector']}  "
                              f"({rr.get('candidates')} candidate(s), <{rr.get('tag')}>)"))
    elif args.cmd == "eval":
        emit(rpc("evaluate", {"tabId": tab, "code": args.code}, timeout=120), j,
             lambda r: print(json.dumps(r.get("value"), ensure_ascii=False, indent=2)))

    # ---- interaction ----
    elif args.cmd in ("click", "hover", "scroll-to", "focus", "blur"):
        ai = {"tabId": tab, "ref": args.target if (args.target or "").startswith("@") else None,
              "selector": None if (args.target or "").startswith("@") else args.target,
              "noMark": True if getattr(args, "no_mark", False) else None,
              "markLabel": getattr(args, "mark_label", None),
              "trusted": True if getattr(args, "trusted", False) else None,
              "double": True if getattr(args, "double", False) else None}
        emit(rpc(args.cmd, ai), j, lambda r: print(f"{args.cmd}: " + json.dumps(
            {k: v for k, v in r.items() if k != "tabId"}, ensure_ascii=False)))
    elif args.cmd == "fill":
        ai = {"tabId": tab, "text": args.text,
              "ref": args.target if (args.target or "").startswith("@") else None,
              "selector": None if (args.target or "").startswith("@") else args.target,
              "noMark": True if getattr(args, "no_mark", False) else None,
              "markLabel": getattr(args, "mark_label", None)}
        emit(rpc("fill", ai), j, lambda r: print(f"filled {r.get('filled')} = {r.get('value')!r}"))
    elif args.cmd == "type":
        emit(rpc("type", {"tabId": tab, "marker": args.target, "text": args.text,
                          "clear": False if getattr(args, "append", False) else True,
                          "markLabel": getattr(args, "mark_label", None)}, timeout=120), j,
             lambda r: print(f"typed {r['typed']} chars into {r['field']} "
                             f"({r.get('aria') or 'unnamed field'})"))
    elif args.cmd == "press":
        emit(rpc("press", {"tabId": tab, "ref": args.ref, "selector": args.selector,
                           "keys": args.keys,
                           "noMark": True if getattr(args, "no_mark", False) else None,
                           "markLabel": getattr(args, "mark_label", None)},
                 ), j, lambda r: print(f"pressed {r.get('pressed')}"))
    elif args.cmd == "select":
        vals = csv_list(args.values or args.value or "")
        emit(rpc("select", {"tabId": tab, "ref": args.target if args.target.startswith("@") else None,
                            "selector": None if args.target.startswith("@") else args.target,
                            "values": vals}), j,
             lambda r: print(f"selected {r.get('values')} (matched {r.get('matched')}) -> value={r.get('value')!r}"))
    elif args.cmd == "wheel":
        emit(rpc("wheel", {"tabId": tab, "ref": args.ref, "selector": args.selector,
                           "deltaY": args.delta_y, "deltaX": args.delta_x}), j,
             lambda r: print(f"wheel dy={r['deltaY']} -> scrollY={r.get('scrollY')}"))
    elif args.cmd == "upload":
        sel = None if (args.target or "").startswith("@") or not args.target else args.target
        emit(rpc("upload", {"tabId": tab,
                            "ref": args.target if (args.target or "").startswith("@") else None,
                            "selector": sel,
                            "shadowIndex": args.shadow_index,
                            "files": [os.path.abspath(f) for f in csv_list(args.files)]}), j,
             lambda r: print(f"uploaded {r['files']} into "
                             + (r.get("selector") or f"shadow input #{r.get('shadowIndex')}")))
    elif args.cmd == "drag":
        emit(rpc("drag", {"tabId": tab, "from": args.from_, "to": args.to,
                          "steps": args.steps, "durationMs": args.duration,
                          "noMark": True if args.no_mark else None,
                          "markLabel": args.mark_label}, timeout=120), j,
             lambda r: print(f"dragged {r['from']['x']},{r['from']['y']} -> "
                             f"{r['to']['x']},{r['to']['y']} in {r['steps']} steps"))
    elif args.cmd == "batch":
        try:
            raw = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
        except OSError as exc:
            raise XbFailure("invalid_params", f"cannot read steps: {exc}", cmd="batch")
        try:
            steps = json.loads(raw)
        except Exception as exc:
            raise XbFailure("invalid_params", f"steps are not valid JSON: {exc}", cmd="batch")
        if isinstance(steps, dict) and "steps" in steps:
            steps = steps["steps"]
        r = rpc("batch", {"tabId": tab, "steps": steps, "intervalMs": args.interval_ms or None,
                          "stopOnError": False if args.continue_on_error else None},
                timeout=300)
        if j:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            for row in r.get("results", []):
                if row.get("ok"):
                    print(f"  ok   [{row['index']}] {row['cmd']}")
                else:
                    err = row.get("error") or {}
                    print(f"  FAIL [{row['index']}] {row['cmd']}: {err.get('code')} {err.get('message')}")
            print(f"{r.get('applied')}/{r.get('steps')} step(s) applied")
    elif args.cmd == "download":
        do_download(args)

    # ---- waits ----
    elif args.cmd == "wait-ms":
        emit(rpc("wait-ms", {"ms": args.ms}), j, lambda r: print(f"waited {r['waitedMs']}ms"))
    elif args.cmd == "wait-for-element":
        emit(rpc("wait-for-element", {"tabId": tab, "selector": args.target, "state": args.state,
                                      "timeoutMs": args.timeout}), j,
             lambda r: print(f"ok: {r['state']} after {r['ms']}ms"))
    elif args.cmd == "wait-for-navigation":
        emit(rpc("wait-for-navigation", {"tabId": tab, "urlPattern": args.url_pattern,
                                         "timeoutMs": args.timeout},
                 timeout=args.timeout / 1000 + 10), j,
             lambda r: print(f"navigated -> {r['url']} ({r['ms']}ms)"))

    # ---- human loop / buffers ----
    elif args.cmd == "request-help":
        emit(rpc("request-help", {"tabId": tab, "message": args.message, "timeoutMs": args.timeout},
                 timeout=args.timeout / 1000 + 15), j,
             lambda r: print(f"human handled it in {r['ms']}ms"))
    elif args.cmd == "console":
        r = rpc("console.read", {"tabId": tab, "limit": args.limit, "clear": args.clear})
        if j:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            for m in r["messages"]:
                print(f"[{m.get('level') or m.get('kind')}] {m.get('text')}")
            print(f"({r['count']} message(s))")
    elif args.cmd == "network":
        if args.har:
            har = rpc("network.har", {"tabId": tab, "filter": args.filter, "sinceMs": args.since},
                      timeout=120)
            with open(args.har, "w", encoding="utf-8") as f:
                json.dump(har, f, ensure_ascii=False, indent=2)
            entries = (har.get("log") or {}).get("entries") or []
            if j:
                print(json.dumps({"har": args.har, "entries": len(entries),
                                  "bytes": os.path.getsize(args.har)}, ensure_ascii=False))
            else:
                print(f"{len(entries)} entries -> {args.har}")
            return 0
        if args.wait_pattern:
            since = args.since
            if args.click:
                armed = rpc("network.arm", {"tabId": tab}, timeout=30)
                since = armed["sinceMs"]
                rpc("click", {"tabId": tab,
                              "ref": args.click if str(args.click).startswith("@") else None,
                              "selector": None if str(args.click).startswith("@") else args.click})
            elif since is None:
                armed = rpc("network.arm", {"tabId": tab}, timeout=30)
                since = armed["sinceMs"]
            r = rpc("network.wait", {"tabId": tab, "urlPattern": args.wait_pattern,
                                     "method": args.method, "sinceMs": since,
                                     "timeoutMs": int(args.timeout * 1000)},
                    timeout=args.timeout + 15)
            if not r.get("matched"):
                # a miss is an error, not a result: keep stdout a single document
                if not j:
                    print(f"TIMEOUT after {r.get('waitedMs')}ms — {r.get('buffered')} request(s) buffered")
                raise XbFailure("timeout", f"no request matched {args.wait_pattern!r} within {args.timeout:g}s",
                                retryable=True, cmd="network")
            emit(r, j, lambda rr: print(
                f"matched after {rr.get('waitedMs')}ms: [{rr['index']}] "
                f"{rr['entry']['method']} {rr['entry']['status']} {rr['entry']['url'][:110]}"))
            return 0
        if args.body is not None:
            r = rpc("network.body", {"tabId": tab, "index": args.body}, timeout=60)
            if j:
                print(json.dumps(r, ensure_ascii=False, indent=2))
            else:
                print(r.get("body") or "")
            return 0
        if args.replay is not None:
            r = rpc("network.replay", {"tabId": tab, "index": args.replay,
                                       "body": args.replay_body}, timeout=120)
            emit(r, j, lambda rr: print(
                f"replay [{args.replay}] {rr['method']} {rr['status']} {rr['bytes']}B" + chr(10)
                + (rr.get("text") or "")[:4000]))
            return 0
        r = rpc("network.read", {"tabId": tab, "filter": args.filter, "limit": args.limit,
                                 "failedOnly": args.failed_only, "clear": args.clear})
        if j:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            for q in r["requests"]:
                flag = "OK " if q.get("ok") else "ERR"
                status = q.get("status") or q.get("error") or "?"
                print(f"{flag} {str(status):<5} {str(q.get('method')):<6} {str(q.get('url'))[:110]}")
            print(f"({r['count']} request(s))")

    # ---- capture ----
    elif args.cmd in ("screenshot", "shot"):
        ai = {"tabId": tab, "full": getattr(args, "full", False),
              "ref": getattr(args, "ref", None), "selector": getattr(args, "selector", None),
              "clip": getattr(args, "clip", None), "format": getattr(args, "format", "png"),
              "quality": getattr(args, "quality", None),
              "maxDimension": getattr(args, "scale_to", None),
              "foreground": True if getattr(args, "foreground", False) else None}
        r = rpc("screenshot", ai, timeout=120)
        b64 = r.get("base64") or (r.get("dataUrl") or "").split(",", 1)[1]
        with open(args.out, "wb") as f:
            f.write(base64.b64decode(b64))
        info = {"out": args.out, "bytes": os.path.getsize(args.out), "via": r.get("via"),
                "width": r.get("width"), "height": r.get("height"),
                "css": {"w": r.get("cssWidth"), "h": r.get("cssHeight")},
                "scale": r.get("scale"), "format": r.get("format")}
        if j:
            print(json.dumps(info, ensure_ascii=False))
        else:
            dims = f"{r.get('width')}x{r.get('height')}px" if r.get("width") else "?"
            print(f"screenshot -> {args.out} ({info['bytes']}B, {dims}, via {r.get('via')})")
    elif args.cmd == "emulate":
        if args.off:
            emit(rpc("emulate", {"tabId": tab, "off": True}), j, lambda r: print("emulation off"))
        else:
            emit(rpc("emulate", {"tabId": tab, "width": args.width, "height": args.height,
                                 "mobile": False if args.desktop else None, "userAgent": args.ua}), j,
                 lambda r: print(f"emulating {r['emulation']} viewport={r.get('viewport')}"))

    # ---- sessions ----
    elif args.cmd == "recent":
        rows = rpc("sessions.recent", {"limit": args.limit})
        if j:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("(nothing recently closed)")
        else:
            for x in rows:
                print(f"{x['sessionId']}  {x['kind']:<6} {(x['title'] or x['url'])[:60]}")
    elif args.cmd == "restore":
        emit(rpc("sessions.restore", {"sessionId": args.session_id}), j,
             lambda r: print(f"restored {r['restored']}"))

    # ---- visibility ----
    elif args.cmd == "mark":
        emit(rpc("mark", {"tabId": tab, "label": args.label}), j,
             lambda r: print(f"marked tab {r['tabId']}" + (f" — {r['label']}" if r.get("label") else "")))
    elif args.cmd == "unmark":
        emit(rpc("unmark", {"tabId": tab, "all": True if args.all else None}), j,
             lambda r: print("cleared: " + (", ".join(map(str, r.get("cleared") or [])) or "(nothing marked)")))
    elif args.cmd == "marks":
        r = rpc("marks", {})
        if j:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif not r.get("count"):
            print("(no marked tabs)")
        else:
            for m in r["marks"]:
                live = m.get("overlay") or {}
                state = "overlay ok" if live.get("marked") else ("overlay gone" if live else "-")
                print(f"{m['tabId']:>7}  {state:<12}  {(m.get('label') or '(default)')[:60]}")
    elif args.cmd == "pulse":
        emit(rpc("pulse", {"tabId": tab, "ref": args.ref, "selector": args.selector,
                           "x": args.x, "y": args.y,
                           "noMark": True if args.no_mark else None,
                           "markLabel": args.mark_label}), j,
             lambda r: print(f"pulse at {r['x']},{r['y']}"))

    # ---- claims ----
    elif args.cmd == "claim":
        ttl_ms = None if args.ttl is None else args.ttl * 1000
        r = rpc("tab.claim", {"tabId": tab, "label": args.label, "ttlMs": ttl_ms})
        leases = load_leases()
        leases[str(r["tabId"])] = r["token"]
        save_leases(leases)
        emit(r, j, lambda rr: print(f"claimed tab {rr['tabId']} (gen {rr['generation']}, "
                                    f"expires in {rr['expiresInMs'] // 1000}s) token={rr['token'][:8]}…"))
    elif args.cmd == "release":
        r = rpc("tab.release", {"tabId": tab, "all": True if args.all else None})
        leases = load_leases()
        for tid in r.get("released") or []:
            leases.pop(str(tid), None)
        save_leases(leases)
        emit(r, j, lambda rr: print("released: " + (", ".join(map(str, rr.get("released") or [])) or "(nothing claimed)")))
    elif args.cmd == "leases":
        rows = rpc("lease.list", {})
        if j:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("(no active claims)")
        else:
            for l in rows:
                print(f"{l['tabId']:>7}  {l['expiresInMs'] // 1000:>4}s  gen {l['generation']:<4} "
                      f"{(l.get('label') or '(unlabelled)')[:50]}")
    elif args.cmd == "panel":
        st = rpc("panel.state", {})
        if j:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            state = "connected" if st.get("connected") else "NOT connected"
            t = st.get("totals") or {}
            w = st.get("worker") or {}
            print(f"{st.get('name') or 'xb'} {st.get('version')}  "
                  f"[{st.get('instanceId') or '?'}{' · ' + st['label'] if st.get('label') else ''}]  "
                  f"bridge {'connected' if st.get('connected') else 'NOT connected'}  :{st.get('wsPort')}  "
                  f"{t.get('tabs', 0)} tab(s), {t.get('marked', 0)} marked, {t.get('leased', 0)} leased"
                  f"  ·  worker boots {w.get('boots', 0)} cmds {w.get('cmds', 0)} errors {w.get('errors', 0)}"
                  f" faults {w.get('faults', 0)}")
            fault = w.get("lastFault")
            if fault:
                print(f"  last fault: {fault.get('kind')} — {str(fault.get('message'))[:120]}")
            rows = st.get("rows") or []
            if not rows:
                print("(no tab is claimed or marked)")
            for r in rows:
                bits = ["受控" if r.get("marked") else "—"]
                if r.get("lease"):
                    bits.append(f"租约 {r['lease']['expiresInMs'] // 1000}s")
                bits.append(f"gen {r['generation']}")
                if r.get("active"):
                    bits.append("前台")
                if r.get("discarded"):
                    bits.append("休眠")
                print(f"  {r['tabId']:>7}  {' '.join(bits):<28} {r.get('label') or r.get('title') or ''}"
                      f"  [{_host(r.get('url'))}]")
    elif args.cmd == "ext-reload":
        emit(rpc("dev.reload", {}), j,
             lambda r: print(f"reloading extension v{r.get('version')} from disk — it should reconnect in a few seconds"))
    return 0


def run(argv=None) -> int:
    """Entry point: turn a failed command into the documented exit code."""
    try:
        return main(argv)
    except XbFailure as exc:
        exc.render()
        return exc.exit_code
    except KeyboardInterrupt:
        print("xb: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(run())
