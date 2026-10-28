#!/usr/bin/env python3
"""End-to-end check of the xb bridge (v0.2, bsk-parity surface) against a live Edge.

Prereq: bridge daemon can start itself, a browser with the extension loaded, and
the static test site on http://127.0.0.1:8899 (python -m http.server in test/site).

Run:  python test/e2e.py
Exit 0 = every assertion passed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
XB = os.path.join(ROOT, "xb.py")
SITE = "http://127.0.0.1:8899"
BASE = "http://127.0.0.1:8788"      # bridge HTTP endpoint (raw rpc for negative tests)
SHOT = os.path.join(HERE, "shot.png")
DLDIR = os.path.join(HERE, "dl")

# test/launch.py records the throw-away browser it started. Addressing it by
# instance means the user's own Edge can stay connected without catching stray
# commands (or losing its tabs to the test run).
def _test_instance() -> str | None:
    try:
        with open(os.path.join(HERE, ".instance"), encoding="utf-8") as f:
            value = f.read().strip()
        return value or None
    except OSError:
        return None


TARGET = _test_instance()
BROWSER_ARGS = ["--browser", TARGET] if TARGET else []
_KEEP = object()        # raw_rpc sentinel: "use the test browser"

checks: list[tuple[str, bool, str]] = []
HEALING = False


def xb(*argv, want_json=True, expect_fail=False):
    global HEALING

    def once():
        cmd = [sys.executable, XB] + BROWSER_ARGS + (["--json"] if want_json else []) \
            + [str(a) for a in argv]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")

    p = once()
    # a window that got minimized mid-run makes every coordinate command fail or
    # hang; heal it and give the command one more chance before asserting
    if not HEALING and p.returncode in (3, 4):
        HEALING = True
        try:
            window_sane()
        except Exception:
            pass
        finally:
            HEALING = False
        p = once()
    if expect_fail:
        return p.returncode, p.stdout + p.stderr
    if p.returncode != 0:
        raise RuntimeError(f"xb {' '.join(map(str, argv))} -> rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    if not want_json:
        return p.stdout.strip()
    out = p.stdout.strip()
    return json.loads(out) if out else None


def check(name: str, cond: bool, detail: str = "") -> None:
    checks.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def xb_error(*argv):
    """Run a command expected to fail; return (exit code, error envelope)."""
    rc, out = xb(*argv, expect_fail=True)
    payload = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except Exception:
                pass
    return rc, payload.get("error") or {}


def window_sane(tab: int | None = None) -> None:
    """A minimized window reports a 0x0 viewport on Edge 155.

    Every rect-based command then fails for reasons unrelated to the bridge, so
    restore the window (and lay the given tab out) before asserting anything.
    """
    for _ in range(3):
        wins = xb("windows")
        normal = any(w.get("state") == "normal" for w in wins)
        target = tab
        if target is None:
            tabs = xb("tabs")
            target = next((t["id"] for t in tabs if t["active"]), tabs[0]["id"] if tabs else None)
        if normal and target is not None and xb("eval", "innerWidth", "--tab", target)["value"]:
            return
        xb("window-resize", "--state", "normal", "--width", 1200, "--height", 800, "--left", 120, "--top", 80)
        if target is not None:
            xb("tab-select", target, "--no-focus")
        xb("wait-ms", 400)


def main() -> int:
    # With several browsers reachable, never gamble on which one gets the
    # commands: e2e must be pinned to the throw-away profile launch.py started.
    listing = xb("browsers")
    rows = (listing.get("instances") if isinstance(listing, dict) else []) or []
    if not TARGET:
        print(f"refusing to run: {len(rows)} browser(s) connected but no test/.instance — "
              "start a throw-away one with `python test/launch.py` first", file=sys.stderr)
        return 2

    print("== status ==")
    st = xb("status")
    check("extension connected", st["extension"]["connected"], f"v{st['extension'].get('version')}")
    window_sane()

    print("== navigation + waits ==")
    tab = xb("open", f"{SITE}/app.html")["id"]
    window_sane(tab)
    xb("wait-ms", 300)
    w = xb("wait-for-element", "#btn-slow", "--tab", tab, "--timeout", 8000)
    check("wait-for-element finds an existing element", w["ms"] >= 0, f"{w['ms']}ms")

    xb("click", "#btn-slow", "--tab", tab)
    w = xb("wait-for-element", "#delayed", "--state", "visible", "--timeout", 6000, "--tab", tab)
    check("wait-for-element blocks until lazily shown", w["ms"] > 100, f"visible after {w['ms']}ms")

    rc, msg = xb("wait-for-element", "#nope-not-there", "--timeout", 700, "--tab", tab, expect_fail=True)
    check("wait-for-element times out with a clear error", rc != 0 and "timeout" in msg.lower(), msg.strip().splitlines()[-1][:70])

    print("== snapshot refs ==")
    snap = xb("snapshot", "--tab", tab)
    refs = {it["name"]: it["ref"] for it in snap["items"]}
    check("snapshot emits @eN refs", all(r.startswith("@e") for r in refs.values()) and len(refs) >= 6,
          f"{len(snap['items'])} items")
    check("snapshot carries roles", any(it["role"] == "combobox" for it in snap["items"]),
          ",".join(sorted({it["role"] for it in snap["items"]})))

    print("== safety: blocked sites + pause ==")
    st_tab = xb("open", f"{SITE}/app.html")["id"]
    xb("wait-for-element", "#btn-slow", "--tab", st_tab)
    xb("block", "127.0.0.1")
    rc, err = xb_error("text", "--tab", st_tab)
    check("a blocked host refuses tab commands", rc == 5 and err.get("code") == "site_blocked",
          f"rc={rc} {err.get('code')}")
    codes = xb("blocked")
    check("blocked list survives a round trip", "127.0.0.1" in (codes.get("blocked") or []),
          str(codes.get("blocked")))
    xb("unblock", "127.0.0.1")
    back = xb("text", "--tab", st_tab)
    check("unblocking restores access", bool(back.get("text")), f"{len(back.get('text') or '')} chars")
    xb("pause")
    rc, err = xb_error("click", "#btn-slow", "--tab", st_tab)
    check("pause refuses mutations", rc == 3 and err.get("code") == "paused",
          f"rc={rc} {err.get('code')}")
    pane = xb("panel")
    check("panel reports the paused state", pane.get("paused") is True, str(pane.get("paused")))
    xb("resume")
    ok = xb("click", "#btn-slow", "--tab", st_tab)
    check("resume lets commands through again", ok.get("clicked") is not None)
    xb("close", st_tab)

    print("== semantic locate ==")
    lt = xb("open", f"{SITE}/list.html")["id"]
    xb("wait-for-element", "#pageinfo", "--tab", lt)
    found = xb("locate", "--text", "下一页", "--tab", lt)
    check("locate finds an element by visible text",
          found.get("found") is True and found.get("selector", "").startswith("[data-xb-locate="),
          f"{found.get('name')!r} from {found.get('candidates')} candidate(s)")
    by_label = xb("locate", "--label", "下一页", "--tab", lt)
    check("locate works by aria-label / placeholder too", by_label.get("found") is True)
    rc, err = xb_error("locate", "--text", "这个按钮不存在", "--tab", lt)
    check("locate miss -> target_not_found", rc == 5 and err.get("code") == "target_not_found",
          f"rc={rc} {err.get('code')}")

    print("== network: wait / replay / har ==")
    n_before = len(xb("network", "--tab", lt).get("requests") or [])
    xb("click", "--text", "调用接口 data.json", "--tab", lt)
    hit = xb("network", "--wait-pattern", "data.json", "--tab", lt, "--timeout", "8")
    check("network --wait-pattern catches the request", hit.get("matched") is True,
          f"index {hit.get('index')} after {hit.get('waitedMs')}ms")
    if hit.get("matched"):
        idx = hit["index"]
        body = xb("network", "--body", str(idx), "--tab", lt)
        check("network --body returns the response body", '"ok"' in (body.get("body") or ""),
              (body.get("body") or "")[:60])
        rep = xb("network", "--replay", str(idx), "--tab", lt)
        check("network --replay re-issues it with the page session",
              rep.get("ok") is True and '"ok"' in (rep.get("text") or ""),
              f"{rep.get('status')} {rep.get('bytes')}B")
    har_path = os.path.join(HERE, "har-check.json")
    xb("network", "--har", har_path, "--tab", lt)
    har = json.load(open(har_path, encoding="utf-8"))
    entries = (har.get("log") or {}).get("entries") or []
    check("network --har writes HAR 1.2 entries",
          har["log"]["version"] == "1.2" and len(entries) >= 1
          and any("data.json" in e["request"]["url"] for e in entries),
          f"{len(entries)} entries")
    os.remove(har_path)

    print("== paginated extract ==")
    merged = xb("extract", "--next", "#next", "--max-pages", "3", "--page-wait", "350", "--tab", lt)
    rows = []
    for tb in merged.get("tables") or []:
        if any(h == "序号" for h in (tb.get("headers") or [])):
            rows = tb.get("rows") or []
    check("extract walks the next control and merges pages",
          merged.get("pages") == 3 and len(rows) == 6,
          f"pages={merged.get('pages')} rows={len(rows)}")
    check("merged rows come from every page",
          {r[1] for r in rows} >= {"登录", "退款", "开票"},
          str([r[1] for r in rows]))
    xb("close", lt)

    print("== console / network buffers ==")
    xb("console", "--tab", tab, "--clear")
    xb("network", "--tab", tab, "--clear")
    log_ref = next((r for n, r in refs.items() if "打印日志" in n), None)
    check("found the log button ref", log_ref is not None, str(log_ref))
    xb("click", log_ref, "--tab", tab)
    time.sleep(1.2)
    con = xb("console", "--tab", tab, "--limit", 30)
    texts = " ".join(m["text"] for m in con["messages"])
    check("console buffer captured console.log", "xb-test-log 42" in texts, f"{con['count']} msg")
    check("console buffer captured console.warn", "xb-test-warn" in texts)
    net = xb("network", "--tab", tab, "--filter", "data.json")
    check("network buffer captured the fetch", net["count"] >= 1 and net["requests"][0]["status"] == 200,
          f"{net['count']} req, status={net['requests'][0]['status'] if net['count'] else '-'}")

    print("== fill + press Enter + navigation ==")
    xb("fill", "#q", "hello-xb", "--tab", tab)
    got = xb("eval", "document.getElementById('q').value", "--tab", tab)["value"]
    check("fill writes into the input", got == "hello-xb", str(got))
    xb("press", "Enter", "--tab", tab)
    nav = xb("wait-for-navigation", "--url-pattern", "submitted", "--timeout", 8000, "--tab", tab)
    check("press Enter submits the form", "submitted" in nav["url"], nav["url"].rsplit("/", 1)[-1])
    xb("navigate", f"{SITE}/app.html", "--tab", tab)
    xb("wait-for-element", "#q", "--tab", tab, "--timeout", 8000)

    print("== select / hover / wheel / scroll-to ==")
    s = xb("select", "#fruit", "--values", "cherry", "--tab", tab)
    check("select sets the <select> value", s["value"] == "cherry", f"matched={s['matched']}")
    val = xb("eval", "document.getElementById('selout').textContent", "--tab", tab)["value"]
    check("select fired the change handler", val == "cherry", str(val))

    before = xb("eval", "getComputedStyle(document.getElementById('hoverout')).display", "--tab", tab)["value"]
    xb("hover", "#hoverme", "--tab", tab)
    after = xb("eval", "getComputedStyle(document.getElementById('hoverout')).display", "--tab", tab)["value"]
    check("hover triggers CSS :hover", before == "none" and after == "block", f"{before} -> {after}")

    wy = xb("wheel", "--delta-y", 700, "--tab", tab)["scrollY"]
    check("wheel scrolls the page", wy > 100, f"scrollY={wy}")
    sy = xb("scroll-to", "#bottom", "--tab", tab)["y"]
    check("scroll-to jumps to the element", sy > 500, f"scrollY={sy}")

    print("== upload / download ==")
    up = xb("upload", "#file", "--files", os.path.join(HERE, "site", "upload-me.txt"), "--tab", tab)
    name = xb("eval", "document.getElementById('file').files[0] ? document.getElementById('file').files[0].name : null",
              "--tab", tab)["value"]
    check("upload puts the file on the input", name == "upload-me.txt", str(up["files"]))

    if os.path.isdir(DLDIR):
        for f in os.listdir(DLDIR):
            os.remove(os.path.join(DLDIR, f))
    out = xb("download", "#dl", "--dir", DLDIR, "--tab", tab, want_json=False)
    ok = os.path.exists(out) and os.path.getsize(out) > 0
    check("download writes the file to disk", ok, f"{os.path.basename(out)} {os.path.getsize(out) if ok else 0}B")

    print("== extract / observe ==")
    ex = xb("extract", "--tab", tab)
    t0 = ex["tables"][0] if ex["tables"] else {}
    check("extract finds the table", t0.get("headers") == ["项目", "状态", "负责人"], str(t0.get("headers")))
    check("extract keeps every row", t0.get("rowCount") == 3, f"{t0.get('rowCount')} rows")
    ob = xb("observe", "--tab", tab)
    check("observe reports controls", ob["controls"]["selects"] >= 1, json.dumps(ob["controls"], ensure_ascii=False))

    print("== emulate ==")
    em = xb("emulate", "--width", 390, "--height", 844, "--tab", tab)
    check("emulate switches to a phone viewport",
          em.get("applied") is True and em["viewport"]["w"] in (390, 980), f"{em['viewport']}")
    xb("emulate", "--off", "--tab", tab)
    back = xb("eval", "innerWidth", "--tab", tab)["value"]
    check("emulate --off restores the viewport", back > 390, f"innerWidth={back}")

    print("== screenshot ==")
    xb("screenshot", SHOT, "--tab", tab)
    check("screenshot writes a PNG", os.path.getsize(SHOT) > 2000, f"{os.path.getsize(SHOT)}B")

    print("== tab groups / split / sleep ==")
    t2 = xb("open", f"{SITE}/beta.html", "--background")["id"]
    t3 = xb("open", f"{SITE}/gamma.html", "--background")["id"]
    grp = xb("group", "new", "--tabs", f"{tab},{t2},{t3}", "--title", "xb e2e", "--color", "blue")
    check("group.create brands the title XB[...]",
          grp["title"] == "XB[xb e2e]" and grp.get("baseTitle") == "xb e2e",
          f"{grp['title']} / base={grp.get('baseTitle')}")
    check("group.create builds a group", len(grp["tabs"]) == 3,
          f"group {grp['id']} [{grp['color']}]")
    gid = grp["id"]
    upd = xb("group", "set", gid, "--color", "green", "--title", "改名", "--collapse")
    check("group.update changes title/colour/state",
          upd["color"] == "green" and upd["title"] == "XB[改名]" and upd["collapsed"] is True,
          str(upd["title"]))
    raw = xb("group", "set", gid, "--title", "裸名", "--raw-title")
    check("--raw-title opts out of the prefix", raw["title"] == "裸名", str(raw["title"]))
    back = xb("group", "set", gid, "--title", "改名")
    check("re-setting brands again", back["title"] == "XB[改名]", str(back["title"]))
    t4 = xb("open", f"{SITE}/alpha.html", "--background")["id"]
    xb("group", "add", gid, t4)
    n = len([g for g in xb("group", "list") if g["id"] == gid][0]["tabs"])
    check("group.add grows the group", n == 4, f"{n} tabs")
    xb("group", "rm", t4)
    n = len([g for g in xb("group", "list") if g["id"] == gid][0]["tabs"])
    check("group.rm shrinks it back", n == 3, f"{n} tabs")

    sp = xb("split", tab, t2)
    check("split creates a split view", isinstance(sp.get("splitViewId"), int), str(sp["splitViewId"]))
    un = xb("unsplit", sp["splitViewId"])
    check("unsplit tears it down", un["unsplit"] == sp["splitViewId"])

    sl = xb("sleep", t3)
    check("sleep discards the tab", any(x.get("discarded") for x in sl), str(sl))

    print("== sessions ==")
    xb("close", t4)
    time.sleep(0.6)
    rec = xb("recent", "--limit", 5)
    check("recent sees the closed tab", len(rec) >= 1, f"{len(rec)} entries")

    print("== error contract ==")
    rc, err = xb_error("click", "#nothing-like-this")
    check("missing element -> code target_not_found + exit 5",
          rc == 5 and err.get("code") == "target_not_found", f"rc={rc} {err.get('code')}")
    check("failed click reports not_started", err.get("outcome") == "not_started", str(err.get("outcome")))
    bad_tab = xb("open", "edge://settings")["id"]
    rc, err = xb_error("text", "--tab", bad_tab)
    check("restricted page -> code restricted_page",
          rc == 5 and err.get("code") == "restricted_page", f"rc={rc} {err.get('code')}")
    xb("close", bad_tab)

    print("== control marker ==")
    xb("mark", "--tab", tab, "--label", "e2e marker")
    label = xb("eval", "(document.getElementById('xb-mark-root')||{}).getAttribute"
                       "&&document.getElementById('xb-mark-root').getAttribute('data-xb-label')", "--tab", tab)["value"]
    check("mark paints an overlay with its label", label == "e2e marker", str(label))
    fav = xb("eval", "!!document.getElementById('xb-favicon-link')", "--tab", tab)["value"]
    check("mark tints the favicon", fav is True, str(fav))
    xb("unmark", "--tab", tab)
    gone = xb("eval", "!!document.getElementById('xb-mark-root')", "--tab", tab)["value"]
    check("unmark removes the overlay", gone is False, str(gone))
    xb("click", "#btn-log", "--tab", tab)
    auto = xb("eval", "!!document.getElementById('xb-mark-root')", "--tab", tab)["value"]
    check("acting on a tab marks it automatically", auto is True, str(auto))
    xb("unmark", "--all")

    print("== group branding / reuse ==")
    g1 = xb("open", f"{SITE}/app.html", "--background", "--group-title", "ep 复用", "--color", "purple")
    g2 = xb("open", f"{SITE}/beta.html", "--background", "--group-title", "ep 复用", "--color", "purple")
    check("a second tab with the same title joins the same group",
          g1["groupIdOfCreated"] == g2["groupIdOfCreated"] and g2.get("groupReused") is True,
          f"{g1['groupIdOfCreated']} vs {g2['groupIdOfCreated']} reused={g2.get('groupReused')}")
    same = [x for x in xb("group", "list") if x["title"] == "XB[ep 复用]"]
    check("only one group carries that title",
          len(same) == 1 and len(same[0]["tabs"]) == 2,
          f"{len(same)} group(s), {len(same[0]['tabs']) if same else 0} tab(s)")
    g3 = xb("open", f"{SITE}/gamma.html", "--background", "--group-title", "ep 复用",
            "--color", "purple", "--new-group")
    check("--new-group forces a separate group",
          g3["groupIdOfCreated"] != g1["groupIdOfCreated"] and g3.get("groupReused") is False,
          f"{g3['groupIdOfCreated']} vs {g1['groupIdOfCreated']}")
    xb("close", f"{g1['id']},{g2['id']},{g3['id']}")

    print("== shadow DOM ==")
    sh = xb("open", f"{SITE}/shadow.html")["id"]
    xb("wait-for-element", "#s-out", "--tab", sh)
    snap = xb("snapshot", "--tab", sh)
    items = snap.get("items") or []
    check("snapshot reports the shadow roots it walked", (snap.get("shadowRoots") or 0) >= 2,
          f"shadowRoots={snap.get('shadowRoots')}")
    by_name = {i.get("name"): i for i in items}
    check("snapshot sees a button inside a shadow root",
          "影子按钮" in by_name and by_name["影子按钮"].get("inShadow") is True,
          f"{len(items)} item(s)")
    if "影子按钮" in by_name:
        xb("click", by_name["影子按钮"]["ref"], "--tab", sh)
        out = xb("eval", "document.getElementById('s-out').textContent", "--tab", sh)["value"]
        check("click by @ref crosses the shadow boundary", out == "影子按钮被点击", str(out))
    s_input = next((i for i in items if i.get("tag") == "input"
                    and (i.get("type") or "text") == "text" and i.get("inShadow")), None)
    if s_input:
        xb("fill", s_input["ref"], "影子文本", "--tab", sh)
        val = xb("eval", "document.getElementById('shadow-host').shadowRoot"
                         ".getElementById('s-input').value", "--tab", sh)["value"]
        check("fill by @ref reaches a shadow input", val == "影子文本", str(val))
    s_sel = next((i for i in items if i.get("tag") == "select"), None)
    if s_sel:
        xb("select", s_sel["ref"], "--values", "yi", "--tab", sh)
        val = xb("eval", "document.getElementById('shadow-host').shadowRoot"
                         ".getElementById('s-select').value", "--tab", sh)["value"]
        check("select by @ref reaches a shadow <select>", val == "yi", str(val))
    if "两层嵌套按钮" in by_name:
        xb("click", by_name["两层嵌套按钮"]["ref"], "--tab", sh)
        out = xb("eval", "document.getElementById('n-out').textContent", "--tab", sh)["value"]
        check("nested shadow roots (two levels) are reachable", out == "嵌套按钮被点击", str(out))
    else:
        check("nested shadow roots (two levels) are reachable", False, "button not in snapshot")
    xb("close", sh)

    window_sane()
    print("== drag / trusted click ==")
    knob_before = xb("eval", "document.getElementById('knobout').textContent", "--tab", tab)["value"]
    dr = xb("drag", "#knob", "300,300", "--tab", tab, "--steps", 8, "--duration", 200)
    knob_after = xb("eval", "document.getElementById('knobout').textContent", "--tab", tab)["value"]
    check("drag moves a pointer-driven element", knob_after != knob_before, f"{knob_before} -> {knob_after}")
    check("drag verifies it started on the element", dr.get("fromHit") is True, str(dr.get("at")))
    clicks_before = int(xb("eval", "document.getElementById('clickout').textContent", "--tab", tab)["value"])
    xb("click", "#pad", "--trusted", "--tab", tab, "--no-mark")
    clicks_after = int(xb("eval", "document.getElementById('clickout').textContent", "--tab", tab)["value"])
    check("trusted click fires a real click", clicks_after == clicks_before + 1, f"{clicks_before} -> {clicks_after}")

    print("== batch ==")
    steps = os.path.join(HERE, "steps-e2e.json")
    with open(steps, "w", encoding="utf-8") as f:
        json.dump([{"cmd": "fill", "args": {"selector": "#q", "text": "batch-e2e"}},
                   {"cmd": "select", "args": {"selector": "#fruit", "values": ["cherry"]}}], f)
    br = xb("batch", "--file", steps, "--tab", tab)
    val = xb("eval", "JSON.stringify([document.getElementById('q').value, "
                      "document.getElementById('selout').textContent])", "--tab", tab)["value"]
    check("batch applies every step in one trip", br["applied"] == 2 and "batch-e2e" in val, val)
    with open(steps, "w", encoding="utf-8") as f:
        json.dump([{"cmd": "fill", "args": {"selector": "#q", "text": "partial"}},
                   {"cmd": "click", "args": {"selector": "#nope-batch"}}], f)
    rc, err = xb_error("batch", "--file", steps, "--tab", tab)
    check("a failed batch reports outcome=applied (no blind retry)",
          rc == 6 and err.get("outcome") == "applied" and len(err.get("details", {}).get("results", [])) == 2,
          f"rc={rc} outcome={err.get('outcome')}")
    os.remove(steps)

    window_sane()
    print("== capture geometry ==")
    vp = xb("eval", "JSON.stringify([innerWidth, innerHeight, "
                    "document.documentElement.scrollHeight])", "--tab", tab)["value"]
    iw, ih, doc_h = json.loads(vp)
    shot = xb("screenshot", os.path.join(HERE, "shot-vp.png"), "--tab", tab)
    check("viewport capture matches the viewport",
          abs(shot["width"] - iw) <= 20 and abs(shot["height"] - ih) <= 20,
          f"{shot['width']}x{shot['height']} vs {iw}x{ih}")
    full = xb("screenshot", os.path.join(HERE, "shot-full.png"), "--tab", tab, "--full")
    check("full-page capture is taller than the viewport and capped at scale-to",
          full["height"] > ih and max(full["width"], full["height"]) <= 2000,
          f"{full['width']}x{full['height']} css={full['css']}")
    clip = xb("screenshot", os.path.join(HERE, "shot-clip.png"), "--tab", tab, "--clip", "0,0,200,120")
    check("clip capture has exactly the requested size",
          (clip["width"], clip["height"]) == (200, 120), f"{clip['width']}x{clip['height']}")
    el = xb("screenshot", os.path.join(HERE, "shot-el.png"), "--tab", tab, "--selector", "#tbl")
    box = json.loads(xb("eval", "JSON.stringify((()=>{const r=document.getElementById('tbl')"
                                 ".getBoundingClientRect();return [Math.round(r.width),Math.round(r.height)]})())",
                        "--tab", tab)["value"])
    check("element capture is the element box", abs(el["width"] - box[0]) <= 4 and abs(el["height"] - box[1]) <= 4,
          f"{el['width']}x{el['height']} vs {box}")
    active_before = next(t["id"] for t in xb("tabs") if t["active"])
    bg = xb("open", f"{SITE}/beta.html", "--background")["id"]
    xb("screenshot", os.path.join(HERE, "shot-bg.png"), "--tab", bg)
    active_after = next(t["id"] for t in xb("tabs") if t["active"])
    check("capturing a background tab does not steal focus", active_before == active_after,
          f"active {active_before} -> {active_after}")
    xb("close", bg)
    for f in ("shot-vp.png", "shot-full.png", "shot-clip.png", "shot-el.png", "shot-bg.png"):
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            os.remove(p)

    print("== claims / generations ==")
    def raw_rpc(cmd, args, browser=_KEEP):
        payload = {"cmd": cmd, "args": args}
        target = TARGET if browser is _KEEP else browser
        if target:
            payload["browser"] = target
        req = urllib.request.Request(f"{BASE}/rpc", data=json.dumps(payload).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    gens = {t["id"]: t.get("generation") for t in xb("tabs")}
    check("tab.list exposes a generation", gens.get(tab) is not None, str(gens.get(tab)))
    cl = xb("claim", "--tab", tab, "--label", "e2e claim")
    check("claim returns a token and marks the tab", bool(cl.get("token")) and cl.get("marked") is True,
          f"gen {cl.get('generation')}")
    code, body = raw_rpc("click", {"tabId": tab, "selector": "#q"})
    check("a caller without the lease is refused", code == 409 and body.get("error", {}).get("code") == "tab_not_owned",
          f"http {code}")
    xb("fill", "#q", "with-lease", "--tab", tab)
    got = xb("eval", "document.getElementById('q').value", "--tab", tab)["value"]
    check("the claiming client keeps working", got == "with-lease", str(got))
    gen_before = cl["generation"]
    xb("navigate", f"{SITE}/app.html", "--tab", tab)
    xb("wait-for-element", "#q", "--tab", tab, "--timeout", 8000)
    token = cl["token"]
    code, body = raw_rpc("click", {"tabId": tab, "selector": "#q", "lease": token, "expectGeneration": gen_before})
    check("a navigation invalidates the pinned generation",
          code == 409 and body.get("error", {}).get("code") == "stale_generation", f"http {code}")
    xb("release", "--all")
    code, body = raw_rpc("click", {"tabId": tab, "selector": "#q"})
    check("release frees the tab again", code == 200, f"http {code}")

    print("== misc commands ==")
    txt = xb("text", "--tab", tab)["text"]
    check("text returns page text", "xb 全功能测试台" in txt, f"{len(txt)} chars")
    html = xb("get-html", "#tbl", "--tab", tab)["html"]
    check("get-html returns the element markup", "<table" in html.lower(), f"{len(html)} chars")
    xb("focus", "#q", "--tab", tab)
    focused = xb("eval", "document.activeElement.id", "--tab", tab)["value"]
    xb("blur", "#q", "--tab", tab)
    blurred = xb("eval", "document.activeElement.id", "--tab", tab)["value"]
    check("focus/blur move the active element", focused == "q" and blurred != "q", f"{focused} -> {blurred}")
    z = xb("zoom", tab, "1.25")
    xb("zoom", tab, "1")
    check("zoom applies a factor", abs(z["zoom"] - 1.25) < 0.01, str(z["zoom"]))
    xb("navigate", f"{SITE}/beta.html", "--tab", tab)
    back = xb("navigate-back", "--tab", tab)
    check("navigate-back returns to the previous page", "app.html" in back["url"], back["url"].rsplit("/", 1)[-1])
    fwd = xb("navigate-forward", "--tab", tab)
    check("navigate-forward goes back again", fwd.get("dir") == "forward", str(fwd.get("via")))
    rl = xb("reload", "--tab", tab)
    check("reload reports the tab", rl["reloaded"] == tab, str(rl["reloaded"]))
    xb("navigate", f"{SITE}/app.html", "--tab", tab)
    marks = xb("marks")
    check("marks lists the registry", isinstance(marks.get("marks"), list), f"{marks.get('count')} mark(s)")
    leases = xb("leases")
    check("leases lists active claims", isinstance(leases, list), f"{len(leases)} lease(s)")
    alias = xb("shot", os.path.join(HERE, "shot-alias.png"), "--tab", tab)
    ok_alias = os.path.exists(os.path.join(HERE, "shot-alias.png")) and alias["bytes"] > 1000
    if ok_alias:
        os.remove(os.path.join(HERE, "shot-alias.png"))
    check("shot is an alias of screenshot", ok_alias, f"{alias['bytes']}B")
    pu = xb("pulse", "--selector", "#q", "--tab", tab)
    check("pulse resolves an element to a point", pu["pulsed"] is True and pu["x"] > 0, f"{pu['x']},{pu['y']}")
    doc = xb("doctor")
    check("doctor reports capabilities", doc.get("version") and "features" in doc, str(doc.get("version")))
    ap = xb("apis")
    check("apis reports namespaces", isinstance(ap.get("namespaces"), dict), f"{len(ap.get('namespaces') or {})} ns")
    logs = xb("logs", "-n", "3", want_json=False)
    check("logs prints the daemon log",
          "bridge up" in logs or "browser connected" in logs or "extension" in logs,
          logs.splitlines()[-1][:40] if logs else "")
    xb("unmark", "--all")

    print("== background work ==")
    bg2 = xb("open", f"{SITE}/app.html", "--background")["id"]
    active0 = next(t["id"] for t in xb("tabs") if t["active"])
    xb("click", "#btn-slow", "--tab", bg2)
    wbg = xb("wait-for-element", "#delayed", "--state", "visible", "--timeout", 8000, "--tab", bg2)
    active1 = next(t["id"] for t in xb("tabs") if t["active"])
    check("waits work inside a background tab", wbg["ms"] < 8000, f"{wbg['ms']}ms")
    check("background work never moved the active tab", active0 == active1, f"{active0} -> {active1}")
    xb("close", bg2)

    print("== panel ==")
    bg = xb("open", f"{SITE}/beta.html", "--background")["id"]
    xb("mark", "--tab", bg, "--label", "panel row")
    state = xb("panel")
    row = next((r for r in state["rows"] if r["tabId"] == bg), None)
    check("panel lists a marked tab with its label", row is not None and row["label"] == "panel row",
          f"{len(state['rows'])} row(s), v{state['version']}")
    check("panel reports bridge state and totals",
          state["connected"] is True and isinstance(state["totals"], dict), str(state["totals"]))
    code, body = raw_rpc("panel.focus", {"tabId": bg})
    now_active = [t["id"] for t in xb("tabs") if t["active"]]
    check("panel.focus brings the tab forward", code == 200 and now_active == [bg], str(now_active))
    code, body = raw_rpc("panel.unmark", {"all": True})
    check("panel.unmark --all clears the rows",
          code == 200 and body["result"]["totals"]["marked"] == 0, str(body.get("result", {}).get("totals")))
    xb("close", bg)

    print("== multi-browser ==")
    listing = xb("browsers")
    rows = listing.get("instances") if isinstance(listing, dict) else (listing or [])
    ids = [r["instanceId"] for r in rows]
    check("browsers lists every connected instance", bool(ids), f"{len(ids)}: {ids}")
    if TARGET:
        check("the launched test browser is addressable", TARGET in ids, TARGET)
        lab = xb("label", "e2e-test")
        check("label.set stores a label", lab.get("label") == "e2e-test", str(lab))
        code, body = raw_rpc("ping", {}, browser="e2e-test")
        check("a label resolves to its browser", code == 200 and body.get("ok") is True, str(code))
        code, body = raw_rpc("ping", {}, browser="definitely-not-a-browser")
        check("unknown browser -> browser_not_found (404)",
              code == 404 and body.get("error", {}).get("code") == "browser_not_found",
              f"{code} {body.get('error', {}).get('code')}")
        xb("label", "")
        lab2 = xb("panel")
        check("label cleared", not lab2.get("label"), str(lab2.get("label")))
    if len(ids) > 1:
        code, body = raw_rpc("ping", {}, browser=None)
        check("several browsers + no target -> browser_required (409)",
              code == 409 and body.get("error", {}).get("code") == "browser_required",
              f"{code} {body.get('error', {}).get('code')}")

    print("== bridge hardening ==")
    from websockets.sync.client import connect as ws_connect
    try:
        with ws_connect("ws://127.0.0.1:8787",
                        additional_headers={"Origin": "https://evil.example"},
                        open_timeout=3, close_timeout=2) as evil:
            evil.send(json.dumps({"hello": {"role": "extension", "instanceId": "evil-origin"}}))
            try:
                evil.recv(timeout=2)
            except Exception:
                pass
    except Exception:
        pass    # a handshake refusal is the expected outcome
    rows = [r["instanceId"] for r in (xb("browsers") or {}).get("instances", [])]
    check("a website cannot register itself as a browser",
          "evil-origin" not in rows and TARGET in rows, f"{len(rows)} instance(s)")

    print("== teardown ==")
    keeper = xb("open", f"{SITE}/gamma.html")["id"]
    xb("group", "close", gid)
    gone = [g for g in xb("group", "list") if g["id"] == gid]
    check("group.close empties the group", gone == [] or len(gone[0]["tabs"]) == 0)
    check("window survived the group close", any(t["id"] == keeper for t in xb("tabs")))

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    for name, _, detail in failed:
        print("  FAILED:", name, detail)
    print("ALL GREEN" if not failed else "FAILURES ABOVE")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
