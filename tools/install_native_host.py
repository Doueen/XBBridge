#!/usr/bin/env python3
"""Register (or remove) the XBBridge native messaging host for Edge.

Writes the host manifest next to the user profile and points
HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\<name> at it. No admin
rights needed, nothing outside %LOCALAPPDATA% and HKCU is touched.

  python tools/install_native_host.py --extension-id fdhphelkbllilnniggopgdkmbfabgngo
  python tools/install_native_host.py --uninstall
  python tools/install_native_host.py --status
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HOST_NAME = "com.doueen.xbbridge"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST_SCRIPT = os.path.join(ROOT, "bridge", "native_host.py")
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "XBBridge", "nm")
MANIFEST_PATH = os.path.join(DATA_DIR, f"{HOST_NAME}.json")
REG_KEY = rf"HKCU\Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}"


def write_manifest(extension_ids: list[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    launcher = HOST_SCRIPT
    if not getattr(sys, "frozen", False):
        # a .py host needs its interpreter spelled out in the manifest
        launcher = os.path.join(DATA_DIR, "run_host.cmd")
        with open(launcher, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(f'@echo off\r\n"{sys.executable}" "{HOST_SCRIPT}" %*\r\n')
    manifest = {
        "name": HOST_NAME,
        "description": "XBBridge native host: keeps the local bridge daemon listening",
        "path": launcher,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{eid}/" for eid in extension_ids],
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"manifest : {MANIFEST_PATH}")
    print("origins  :", ", ".join(manifest["allowed_origins"]))


def register() -> None:
    subprocess.run(["reg", "add", REG_KEY, "/ve", "/t", "REG_SZ", "/d", MANIFEST_PATH, "/f"],
                   check=True, capture_output=True, text=True, encoding="mbcs", errors="replace")
    print(f"registry : {REG_KEY} -> {MANIFEST_PATH}")


def unregister() -> None:
    res = subprocess.run(["reg", "delete", REG_KEY, "/f"], capture_output=True,
                         text=True, encoding="mbcs", errors="replace")
    print("registry : removed" if res.returncode == 0 else f"registry : {res.stderr.strip() or 'not present'}")


def status() -> int:
    res = subprocess.run(["reg", "query", REG_KEY, "/ve"], capture_output=True,
                         text=True, encoding="mbcs", errors="replace")
    present = res.returncode == 0
    print(f"registry : {'present' if present else 'absent'}  ({REG_KEY})")
    print(f"manifest : {'present' if os.path.exists(MANIFEST_PATH) else 'missing'}  ({MANIFEST_PATH})")
    if present and os.path.exists(MANIFEST_PATH):
        manifest = json.load(open(MANIFEST_PATH, encoding="utf-8"))
        print(f"origins  : {', '.join(manifest.get('allowed_origins') or [])}")
        print(f"path     : {manifest.get('path')}")
    return 0 if present else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extension-id", nargs="+",
                    default=["fdhphelkbllilnniggopgdkmbfabgngo"],
                    help="one or more extension ids allowed to call the host")
    ap.add_argument("--install", action="store_true", help="default action")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        return status()
    if args.uninstall:
        unregister()
        if os.path.exists(MANIFEST_PATH):
            os.remove(MANIFEST_PATH)
            print("manifest : deleted")
        return 0
    write_manifest(args.extension_id)
    register()
    print("\nthen: edge://extensions -> XBBridge -> reload; the service worker wakes the bridge on startup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
