#!/usr/bin/env python3
"""Build a single-file xb.exe with PyInstaller.

Needs a Python that has both `websockets` and `pyinstaller`:

  python tools/build_exe.py                 # uses the interpreter running this script
  python tools/build_exe.py --python "C:/.../Python312/python.exe"

The frozen exe carries bridge/server.py and bridge/native_host.py as data files;
`xb serve` and `xb native-host` both work from it, so a machine with the exe
needs no Python at all.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="interpreter with pyinstaller installed")
    ap.add_argument("--clean", action="store_true", help="drop build/ and dist/xb.exe first")
    args = ap.parse_args()

    probe = subprocess.run([args.python, "-c", "import PyInstaller, websockets; print('ok')"],
                           capture_output=True, text=True)
    if "ok" not in probe.stdout:
        raise SystemExit(f"{args.python} lacks pyinstaller/websockets:\n{probe.stderr.strip()}")

    if args.clean:
        shutil.rmtree(BUILD, ignore_errors=True)
        target = os.path.join(DIST, "xb.exe")
        if os.path.exists(target):
            os.remove(target)

    sep = ";" if os.name == "nt" else ":"
    cmd = [
        args.python, "-m", "PyInstaller",
        "--onefile", "--noconfirm", "--clean",
        "--name", "xb",
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
        # bridge/server.py is shipped as data (exec'd with runpy), so PyInstaller
        # cannot see its imports: name them explicitly.
        "--hidden-import", "websockets",
        "--hidden-import", "websockets.asyncio.server",
        "--hidden-import", "http.server",
        "--hidden-import", "socketserver",
        "--hidden-import", "queue",
        "--hidden-import", "uuid",
        "--add-data", f"{os.path.join(ROOT, 'bridge', 'server.py')}{sep}bridge",
        "--add-data", f"{os.path.join(ROOT, 'bridge', 'native_host.py')}{sep}bridge",
        "--console",
        os.path.join(ROOT, "xb.py"),
    ]
    print(" ".join(cmd), "\n")
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        return rc

    exe = os.path.join(DIST, "xb.exe" if os.name == "nt" else "xb")
    if not os.path.exists(exe):
        raise SystemExit(f"build finished but {exe} is missing")
    print(f"\nbuilt: {exe}  ({os.path.getsize(exe):,} B)")
    print("smoke test: dist/xb.exe status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
