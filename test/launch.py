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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=f"xb-{int(time.time())}")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--wait", type=float, default=45)
    ap.add_argument("--keep", action="store_true", help="reuse the profile if it exists")
    args = ap.parse_args()

    profile = os.path.join(SCRATCH, args.profile)
    if os.path.isdir(profile) and not args.keep:
        shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile, exist_ok=True)

    url = f"http://127.0.0.1:{args.port}/app.html"
    cmd = [
        edge_path(),
        f"--user-data-dir={profile}",
        f"--load-extension={EXT}",
        f"--disable-extensions-except={EXT}",
        "--no-first-run", "--no-default-browser-check",
        "--window-size=1200,800", "--window-position=120,80",
        url,
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # the bridge daemon must exist before the extension dials in
    subprocess.run([sys.executable, os.path.join(ROOT, "xb.py"), "status"],
                   capture_output=True, text=True)

    started = time.time()
    while time.time() - started < args.wait:
        if connected(8788):
            print(profile)
            return 0
        time.sleep(1)
    print(f"extension did not connect within {args.wait:g}s (profile {profile})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
