#!/usr/bin/env python3
"""Launch a throw-away Edge profile with the xb extension loaded, for e2e runs.

Two things this encodes (both learned the hard way on Edge 155):
  * a fresh --user-data-dir per run: an unpacked extension loaded by command line
    is not re-attached to an existing profile on restart
  * an on-screen window rect: chrome.windows.update refuses bounds that are not
    >=50% on screen, and a window parked at -10000/-10000 ends up with a 0x0
    viewport, which makes every rect-based command look broken

Usage:  python test/launch.py [--profile xbNN] [--port 8899] [--wait 45]
Prints the profile path and exits 0 once the extension has connected.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.path.join(ROOT, "extension")
SCRATCH = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "hermes", "cache", "scratch")
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def edge_path() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("msedge.exe not found")


def connected(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return bool(json.loads(r.read().decode()).get("extension"))
    except Exception:
        return False


def browsers(port: int = 8788) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/browsers", timeout=2) as r:
            return json.loads(r.read().decode()).get("instances") or []
    except Exception:
        return []


def rpc(browser: str, cmd: str, args: dict | None = None, timeout: float = 8) -> object:
    payload = {"cmd": cmd, "args": args or {}, "browser": browser, "timeout": timeout}
    req = urllib.request.Request("http://127.0.0.1:8788/rpc",
                                 data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 3) as r:
        return json.loads(r.read().decode()).get("result")


def pick_test_instance(marker: str, before: set[str], wait: float) -> dict | None:
    """Which connected browser is the throw-away one?

    Connect timing alone is not enough: a service worker that was asleep can
    reconnect with the very same instance id at the wrong moment. Anchor on
    evidence instead — the test profile is the one holding a tab on the test
    site, preferring an instance that was not connected before this launch.
    """
    end = time.time() + wait
    while time.time() < end:
        candidates = []
        for b in browsers():
            try:
                tabs = rpc(b["instanceId"], "tab.list")
            except Exception:
                continue
            if any(marker in (t.get("url") or "") for t in (tabs or [])):
                candidates.append(b)
        if candidates:
            fresh = [b for b in candidates if b["instanceId"] not in before]
            pick = (fresh or sorted(candidates, key=lambda b: b.get("connectedFor", 1e9)))[0]
            return pick
        time.sleep(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=f"xb-{int(time.time())}")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--wait", type=float, default=45)
    ap.add_argument("--keep", action="store_true", help="reuse the profile if it exists")
    ap.add_argument("--select-only", action="store_true",
                    help="don't launch anything; just find the test browser among the connected ones")
    args = ap.parse_args()

    marker = f"127.0.0.1:{args.port}"
    if args.select_only:
        pick = pick_test_instance(marker, set(), args.wait)
        if not pick:
            print(f"no connected browser has a tab on {marker}", file=sys.stderr)
            return 1
        with open(os.path.join(HERE, ".instance"), "w", encoding="utf-8") as f:
            f.write(pick["instanceId"] + "\n")
        print(json.dumps({"instance": pick["instanceId"]}, ensure_ascii=False))
        return 0

    profile = os.path.join(SCRATCH, args.profile)
    if os.path.isdir(profile) and not args.keep:
        shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile, exist_ok=True)

    url = f"http://{marker}/app.html"
    cmd = [
        edge_path(),
        f"--user-data-dir={profile}",
        f"--load-extension={EXT}",
        f"--disable-extensions-except={EXT}",
        "--no-first-run", "--no-default-browser-check",
        "--window-size=1200,800", "--window-position=120,80",
        url,
    ]
    # the bridge daemon must exist before the extension dials in
    subprocess.run([sys.executable, os.path.join(ROOT, "xb.py"), "status"],
                   capture_output=True, text=True)

    before = {b["instanceId"] for b in browsers()}
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    pick = pick_test_instance(marker, before, args.wait)
    if not pick:
        print(f"no browser picked up {marker} within {args.wait:g}s (profile {profile})",
              file=sys.stderr)
        return 1
    with open(os.path.join(HERE, ".instance"), "w", encoding="utf-8") as f:
        f.write(pick["instanceId"] + "\n")
    print(json.dumps({"profile": profile, "instance": pick["instanceId"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
