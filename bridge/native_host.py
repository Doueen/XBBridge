#!/usr/bin/env python3
"""Native messaging host for XBBridge.

Edge cannot execute a local program, and the extension has no way to start
``bridge/server.py`` on its own. This host is the standard escape hatch: it is
registered under HKCU (see tools/install_native_host.py), and the service
worker pings it with ``sendNativeMessage`` once at startup. The host makes sure
the bridge daemon is listening and answers with the port numbers.

Protocol (Chrome native messaging): each message is a 4-byte little-endian
length followed by that many bytes of UTF-8 JSON. One request per launch for
``sendNativeMessage``; the loop also supports ``connectNative`` (many messages).

Messages
  {"type": "ensure"}  -> start the daemon if needed, wait for the port
  {"type": "ping"}    -> liveness only
  {"type": "stop"}    -> terminate the daemon (used by `xb stop --hard`)
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(HERE, "server.py")
WS_PORT = int(os.environ.get("WS_PORT", "8787"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8788"))
START_TIMEOUT = float(os.environ.get("XB_START_TIMEOUT", "12"))
LOG = os.environ.get("XB_NATIVE_LOG") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "XBBridge", "native-host.log")


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def bridge_up() -> bool:
    """Readiness is the HTTP health endpoint.

    Poking the websocket port with a bare TCP connect makes the daemon log a
    "did not receive a valid HTTP request" handshake error every time, which is
    noise in the very log users are told to read.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/health", timeout=0.6) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def read_message() -> dict | None:
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("<I", raw)
    if length == 0 or length > 64 * 1024 * 1024:
        return None
    body = sys.stdin.buffer.read(length)
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def write_message(payload: dict) -> None:
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(blob)))
    sys.stdout.buffer.write(blob)
    sys.stdout.buffer.flush()


def python_cmd() -> list[str]:
    """Prefer the frozen binary next to us, else the interpreter that runs us."""
    exe = os.path.join(ROOT, "dist", "xb.exe")
    if os.path.exists(exe) and getattr(sys, "frozen", False):
        return [exe]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, SERVER]


def ensure_bridge() -> dict:
    if bridge_up():
        return {"ok": True, "spawned": False, "wsPort": WS_PORT, "httpPort": HTTP_PORT}

    cmd = python_cmd()
    if cmd[0].endswith("xb.exe"):
        cmd = cmd + ["serve"]
    log(f"spawning: {cmd}")
    creation = 0
    if os.name == "nt":
        creation = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
            close_fds=True,
        )
    except Exception as exc:                      # pragma: no cover - env specific
        log(f"spawn failed: {exc!r}")
        return {"ok": False, "error": f"spawn failed: {exc}"}

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if bridge_up():
            return {"ok": True, "spawned": True, "wsPort": WS_PORT, "httpPort": HTTP_PORT}
        time.sleep(0.2)
    return {"ok": False, "error": f"bridge did not listen within {START_TIMEOUT:g}s"}


def handle(msg: dict) -> dict:
    kind = str(msg.get("type") or "ensure").lower()
    if kind == "ping":
        return {"ok": True, "up": bridge_up(), "wsPort": WS_PORT, "httpPort": HTTP_PORT}
    if kind == "ensure":
        out = ensure_bridge()
        out["type"] = "ensure"
        return out
    return {"ok": False, "error": f"unknown type: {kind}"}


def main() -> int:
    # sendNativeMessage starts us, sends one message, waits for one reply.
    msg = read_message()
    if msg is None:
        log("no message on stdin")
        return 1
    reply = handle(msg)
    log(f"reply: {reply}")
    write_message(reply)
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
