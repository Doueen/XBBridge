#!/usr/bin/env python3
"""Build the Microsoft Edge Add-ons / Chrome Web Store package.

What it does beyond zipping:
  * flips `DEV_MODE` to false, so `dev.reload` (make the browser re-read the
    extension from disk) is refused in the shipped build
  * refuses to package junk (__pycache__, .git, test dirs, stray dotfiles)
  * validates the manifest and every icon path it references
  * prints the exact file list + sizes that will be uploaded

Usage:  python tools/build_store.py [--out dist]
Result: dist/xb-bridge-<version>.zip  (upload this file as-is)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import re
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.path.join(ROOT, "extension")

JUNK_DIRS = {"__pycache__", ".git", ".idea", "node_modules", "test", "dist"}
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def stage(dest: str) -> None:
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    for base, dirs, files in os.walk(EXT):
        dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
        rel = os.path.relpath(base, EXT)
        target = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            if name in JUNK_NAMES or name.endswith((".pyc", ".log", ".map")):
                print(f"  skip {os.path.join(rel, name)}")
                continue
            shutil.copy2(os.path.join(base, name), os.path.join(target, name))

    sw = os.path.join(dest, "sw.js")
    src = open(sw, encoding="utf-8").read()
    if "const DEV_MODE = true;" not in src:
        raise SystemExit("sw.js: cannot find `const DEV_MODE = true;` — did the marker change?")
    open(sw, "w", encoding="utf-8", newline="\n").write(
        src.replace("const DEV_MODE = true;", "const DEV_MODE = false;", 1))


def validate(dest: str) -> dict:
    man = json.load(open(os.path.join(dest, "manifest.json"), encoding="utf-8"))
    for field in ("manifest_version", "name", "version", "description"):
        if not man.get(field):
            raise SystemExit(f"manifest: missing {field}")
    if man["manifest_version"] != 3:
        raise SystemExit("manifest: must be MV3")

    # name / description / action title are __MSG_*__ refs now: validate the
    # localized text the store will actually show, and that the default locale
    # defines every key the manifest mentions.
    default_locale = man.get("default_locale")
    if not default_locale:
        raise SystemExit("manifest: default_locale missing while using __MSG_ refs")
    loc_path = os.path.join(dest, "_locales", default_locale, "messages.json")
    if not os.path.exists(loc_path):
        raise SystemExit(f"manifest: {loc_path} missing")
    messages = json.load(open(loc_path, encoding="utf-8"))
    refs = set(re.findall(r"__MSG_(\w+)__", json.dumps(man, ensure_ascii=False)))
    missing = sorted(r for r in refs if r not in messages)
    if missing:
        raise SystemExit(f"_locales/{default_locale}: undefined message(s) {missing}")
    desc = messages.get("extDesc", {}).get("message", "")
    if len(desc) > 132:
        raise SystemExit(f"description is {len(desc)} chars, store limit is 132")
    for loc in sorted(os.listdir(os.path.join(dest, "_locales"))):
        other = json.load(open(os.path.join(dest, "_locales", loc, "messages.json"), encoding="utf-8"))
        gap = sorted(set(messages) ^ set(other))
        if gap:
            raise SystemExit(f"_locales/{loc}: key mismatch vs {default_locale}: {gap}")

    icons = list((man.get("icons") or {}).values())
    icons += list(((man.get("action") or {}).get("default_icon") or {}).values())
    for path in set(icons):
        if not os.path.exists(os.path.join(dest, path)):
            raise SystemExit(f"manifest references a missing icon: {path}")

    required = {"16": 16, "32": 32, "48": 48, "128": 128}
    for size, px in required.items():
        path = (man.get("icons") or {}).get(size)
        if not path:
            raise SystemExit(f"manifest: icons.{size} missing (store requires 128, we ship all four)")
    return man


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    stage_dir = os.path.join(args.out, "xb-bridge")
    print("staging from extension/ ...")
    stage(stage_dir)
    man = validate(stage_dir)

    sw = open(os.path.join(stage_dir, "sw.js"), encoding="utf-8").read()
    if "const DEV_MODE = false;" not in sw:
        raise SystemExit("stage: DEV_MODE was not flipped")
    if "dev.reload is not available" not in sw:
        raise SystemExit("stage: dev.reload guard missing")

    zip_path = os.path.join(args.out, f"XBBridge-{man['version']}.zip")
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, dirs, files in os.walk(stage_dir):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(base, name)
                arc = os.path.relpath(full, stage_dir).replace(os.sep, "/")
                z.write(full, arc)
                total += os.path.getsize(full)
                print(f"  + {arc:<28}{os.path.getsize(full):>8,} B")

    print(f"\nmanifest : {man['name']} {man['version']}  (MV{man['manifest_version']})")
    print(f"perms    : {', '.join(man['permissions'])}")
    print(f"hosts    : {', '.join(man.get('host_permissions') or [])}")
    print(f"payload  : {total:,} B → {os.path.getsize(zip_path):,} B zipped")
    print(f"upload   : {zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
