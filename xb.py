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
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "bridge", "server.py")
LOG = os.path.join(HERE, ".xb", "server.log")
COLORS = ["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan"]
JSON_MODE = False

# exit codes: stable per failure class so scripts can branch without parsing
EXIT_BY_CODE = {
    "invalid_params": 2, "unsupported_method": 2,
    "no_target_tab": 3, "extension_unavailable": 3, "unavailable": 3, "bridge_unavailable": 3,
    "timeout": 4,
    "target_not_found": 5, "restricted_page": 5,
    "stale_generation": 6, "tab_not_owned": 6, "claim_required": 6,
    "debugger_conflict": 7,
}


class XbFailure(RuntimeError):
    """A failed command with the extension's error contract attached."""

    def __init__(self, code: str, message: str, outcome: str = "unknown",
                 retryable: bool = False, cmd: str = ""):
        super().__init__(message)
        self.code = code
        self.outcome = outcome
        self.retryable = retryable
        self.cmd = cmd

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CODE.get(self.code, 1)

    def render(self) -> None:
        if JSON_MODE:
            print(json.dumps({"ok": False, "cmd": self.cmd,
                              "error": {"code": self.code, "message": str(self),
                                        "outcome": self.outcome,
                                        "retryable": self.retryable}},
                             ensure_ascii=False))
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


def ensure_daemon(quiet: bool = False) -> None:
    if daemon_alive():
        return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(LOG, "ab") as logf:
        subprocess.Popen([sys.executable, SERVER], stdin=subprocess.DEVNULL,
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
    ensure_daemon(quiet=True)
    try:
        res = http("/rpc", {"cmd": cmd, "args": args or {}, "timeout": timeout}, timeout=timeout + 5)
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
                            retryable=bool(err.get("retryable")), cmd=cmd)
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
                            retryable=bool(err.get("retryable")), cmd=cmd)
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
    info = rpc("download.wait", {"sinceMs": since, "timeoutMs": int(args.timeout * 1000)},
               timeout=args.timeout + 20)
    src = info.get("filename") or ""
    if not src or not os.path.exists(src):
        raise XbFailure("target_not_found",
                        f"the browser reported {src!r} but no file is there", cmd="download")
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
    sub = p.add_subparsers(dest="cmd", required=True)

    tgt = argparse.ArgumentParser(add_help=False)
    tgt.add_argument("target", nargs="?", help="ref (@e12) or css selector")
    tgt.add_argument("--tab", type=int)
    tgt.add_argument("--no-mark", action="store_true", dest="no_mark",
                     help="don't paint the control marker on this tab")
    tgt.add_argument("--mark-label", dest="mark_label", help="label shown in the marker chip")

    # infra
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("apis")
    sub.add_parser("serve")
    sub.add_parser("stop")
    sub.add_parser("logs").add_argument("-n", type=int, default=40)

    # tabs
    t = sub.add_parser("tabs"); t.add_argument("--window", type=int); t.add_argument("--group", type=int)
    o = sub.add_parser("open", help="open a tab (optionally into a new group)")
    o.add_argument("url"); o.add_argument("--background", action="store_true")
    o.add_argument("--group-title"); o.add_argument("--color", choices=COLORS)
    c = sub.add_parser("close"); c.add_argument("tab_ids")
    s = sub.add_parser("tab-select", help="activate a tab")
    s.add_argument("tab_id", type=int); s.add_argument("--no-focus", action="store_true")
    sub.add_parser("windows")
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
    ex = sub.add_parser("extract"); ex.add_argument("--tab", type=int); ex.add_argument("--out")
    ex.add_argument("--csv", action="store_true")
    ev = sub.add_parser("eval", help="evaluate JS (CDP)"); ev.add_argument("code"); ev.add_argument("--tab", type=int)

    # interaction
    ck = sub.add_parser("click", parents=[tgt])
    fl = sub.add_parser("fill", parents=[tgt]); fl.add_argument("text", nargs="?", default="")
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
    up = sub.add_parser("upload"); up.add_argument("target"); up.add_argument("--files", required=True)
    up.add_argument("--tab", type=int)
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

    return p


def main(argv=None) -> int:
    global JSON_MODE
    args = build_parser().parse_args(argv)
    j = args.json
    JSON_MODE = bool(j)
    tab = getattr(args, "tab", None)

    if args.cmd == "status":
        if not daemon_alive():
            ensure_daemon(quiet=True)
        h = http("/health")
        ext = h.get("info") or {}
        tabs = rpc("tab.list", {}) if h.get("extension") else []
        groups = rpc("group.list", {}) if h.get("extension") else []
        out = {"daemon": {"http": BASE, "pid": h.get("pid"), "ws_port": WS_PORT},
               "extension": {"connected": h.get("extension"), **ext},
               "tabs": len(tabs), "groups": len(groups)}

        def human(o):
            d, e = o["daemon"], o["extension"]
            print(f"daemon      : pid {d['pid']}  http {d['http']}  ws :{d['ws_port']}")
            print(f"extension   : {'connected' if e['connected'] else 'NOT connected'}"
                  + (f"  v{e.get('version')}" if e.get("version") else ""))
            if e.get("browser"):
                print(f"browser     : {e['browser'][:90]}")
            print(f"tabs/groups : {o['tabs']} tab(s), {o['groups']} group(s)")
            if not e["connected"]:
                print("\n-> load the extension: edge://extensions -> 开发人员模式 -> 加载解压缩的扩展 -> "
                      f"{os.path.join(HERE, 'extension')}")
        emit(out, j, human)
        return 0

    if args.cmd == "serve":
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
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
    elif args.cmd == "open":
        ai = {"url": args.url, "active": not args.background}
        if args.group_title or args.color:
            ai["group"] = {k: v for k, v in (("title", args.group_title), ("color", args.color)) if v}
        emit(rpc("tab.create", ai), j, lambda t: print(
            f"opened tab {t['id']} (group {t.get('groupIdOfCreated') or '-'})  {t.get('title', '')}"))
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
        r = rpc("extract", {"tabId": tab})
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
            print(f"written {args.out}")
        else:
            print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "eval":
        emit(rpc("evaluate", {"tabId": tab, "code": args.code}, timeout=120), j,
             lambda r: print(json.dumps(r.get("value"), ensure_ascii=False, indent=2)))

    # ---- interaction ----
    elif args.cmd in ("click", "hover", "scroll-to", "focus", "blur"):
        ai = {"tabId": tab, "ref": args.target if (args.target or "").startswith("@") else None,
              "selector": None if (args.target or "").startswith("@") else args.target,
              "noMark": True if getattr(args, "no_mark", False) else None,
              "markLabel": getattr(args, "mark_label", None)}
        emit(rpc(args.cmd, ai), j, lambda r: print(f"{args.cmd}: " + json.dumps(
            {k: v for k, v in r.items() if k != "tabId"}, ensure_ascii=False)))
    elif args.cmd == "fill":
        ai = {"tabId": tab, "text": args.text,
              "ref": args.target if (args.target or "").startswith("@") else None,
              "selector": None if (args.target or "").startswith("@") else args.target,
              "noMark": True if getattr(args, "no_mark", False) else None,
              "markLabel": getattr(args, "mark_label", None)}
        emit(rpc("fill", ai), j, lambda r: print(f"filled {r.get('filled')} = {r.get('value')!r}"))
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
        emit(rpc("upload", {"tabId": tab, "ref": args.target if args.target.startswith("@") else None,
                            "selector": None if args.target.startswith("@") else args.target,
                            "files": [os.path.abspath(f) for f in csv_list(args.files)]}), j,
             lambda r: print(f"uploaded {r['files']} into {r['selector']}"))
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
