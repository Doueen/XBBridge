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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
XB = os.path.join(ROOT, "xb.py")
SITE = "http://127.0.0.1:8899"
SHOT = os.path.join(HERE, "shot.png")
DLDIR = os.path.join(HERE, "dl")

checks: list[tuple[str, bool, str]] = []


def xb(*argv, want_json=True, expect_fail=False):
    cmd = [sys.executable, XB] + (["--json"] if want_json else []) + [str(a) for a in argv]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
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


def main() -> int:
    print("== status ==")
    st = xb("status")
    check("extension connected", st["extension"]["connected"], f"v{st['extension'].get('version')}")

    print("== navigation + waits ==")
    tab = xb("open", f"{SITE}/app.html")["id"]
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
    check("group.create builds a group", grp["title"] == "xb e2e" and len(grp["tabs"]) == 3,
          f"group {grp['id']} [{grp['color']}]")
    gid = grp["id"]
    upd = xb("group", "set", gid, "--color", "green", "--title", "改名", "--collapse")
    check("group.update changes title/colour/state",
          upd["color"] == "green" and upd["title"] == "改名" and upd["collapsed"] is True)
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
