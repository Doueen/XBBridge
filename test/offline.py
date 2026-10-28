#!/usr/bin/env python3
"""Browser-free test suite — the part of the contract CI can check.

Starts a real bridge daemon on throwaway ports and connects *fake* extensions
(plain websocket clients speaking the same hello/dispatch protocol). That covers
the multi-browser registry, error mapping and origin policy without Edge.

  python test/offline.py            # prints PASS lines, exit 1 on any failure
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "bridge", "server.py")
PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}" + (f"   [{detail}]" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeExtension:
    """Minimal stand-in for extension/sw.js: hello, then dispatch replies."""

    def __init__(self, ws_port: int, instance_id: str, label: str, tabs: list[dict]):
        self.instance_id = instance_id
        self.label = label
        self.tabs = tabs
        self.thread = threading.Thread(target=self._run, args=(ws_port,), daemon=True)
        self.ready = threading.Event()
        self.seen: list[str] = []

    def _run(self, ws_port: int) -> None:
        with connect(f"ws://127.0.0.1:{ws_port}", origin="chrome-extension://fakeci00000000000000000000000000",
                     open_timeout=10, max_size=None) as ws:
            ws.send(json.dumps({"hello": {"role": "extension", "name": "fake", "version": "ci",
                                          "browser": "Fake/1.0", "instanceId": self.instance_id,
                                          "label": self.label, "stats": {}}}))
            self.ready.set()
            while True:
                try:
                    raw = ws.recv(timeout=30)
                except Exception:
                    return
                msg = json.loads(raw)
                cmd = msg.get("cmd")
                self.seen.append(cmd)
                if cmd == "tab.list":
                    reply = {"id": msg["id"], "ok": True, "result": self.tabs}
                elif cmd == "boom":
                    reply = {"id": msg["id"], "ok": False, "error": {
                        "code": "target_not_found", "message": "no such element",
                        "outcome": "not_started", "retryable": False}}
                elif cmd == "slow":
                    time.sleep(3)
                    reply = {"id": msg["id"], "ok": True, "result": {"slow": True}}
                else:
                    reply = {"id": msg["id"], "ok": False, "error": {
                        "code": "unsupported_method", "message": f"unknown command: {cmd}",
                        "outcome": "not_started", "retryable": False}}
                ws.send(json.dumps(reply))

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("fake extension never registered")


def post(port: int, payload: dict, timeout: float = 15) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/rpc",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> int:
    ws_port, http_port = free_port(), free_port()
    env = dict(os.environ, XB_WS_PORT=str(ws_port), XB_HTTP_PORT=str(http_port))
    daemon = subprocess.Popen([sys.executable, SERVER], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                status, body = get(http_port, "/health")
                if status == 200 and body.get("ok"):
                    break
            except Exception:
                time.sleep(0.15)
        else:
            print(daemon.stdout.read() if daemon.stdout else "")
            print("daemon never came up")
            return 1

        print("== daemon basics ==")
        status, body = get(http_port, "/health")
        check("/health answers on a clean daemon", status == 200 and body.get("ok") is True)
        check("no browser yet -> extension false", body.get("extension") is False, str(body.get("count")))

        print("== origin policy ==")
        rejected = False
        detail = ""
        try:
            with connect(f"ws://127.0.0.1:{ws_port}", origin="http://evil.example",
                         open_timeout=8, max_size=None) as ws:
                try:
                    ws.recv(timeout=5)
                except Exception as exc:
                    detail = type(exc).__name__
            rejected = True   # server closed the socket after the handshake
        except Exception as exc:
            rejected = True
            detail = type(exc).__name__
        check("a web origin cannot become a 'browser'", rejected, detail or "closed")

        print("== single browser ==")
        a = FakeExtension(ws_port, "aaaa11112222", "ci-a", [{"id": 1, "url": "https://a.test/", "title": "A"}])
        a.start()
        status, body = get(http_port, "/health")
        check("hello registers the instance", body.get("count") == 1
              and (body.get("instances") or [{}])[0].get("label") == "ci-a", str(body.get("count")))
        check("extensionId is derived from the origin",
              (body.get("instances") or [{}])[0].get("extensionId", "").startswith("fakeci"),
              str((body.get("instances") or [{}])[0].get("extensionId")))

        status, body = post(http_port, {"cmd": "tab.list"})
        check("request with one browser needs no target", status == 200 and body.get("ok") is True)
        check("result passes through untouched", (body.get("result") or [{}])[0].get("title") == "A")

        status, body = post(http_port, {"cmd": "boom"})
        code = (body.get("error") or {}).get("code")
        check("extension errors keep their code + status",
              status == 404 and code == "target_not_found", f"http {status} {code}")

        status, body = post(http_port, {"cmd": "definitely-not-a-command"})
        check("unknown command -> 400 unsupported_method",
              status == 400 and (body.get("error") or {}).get("code") == "unsupported_method",
              f"http {status}")

        print("== two browsers ==")
        b = FakeExtension(ws_port, "bbbb33334444", "ci-b", [{"id": 2, "url": "https://b.test/", "title": "B"}])
        b.start()
        status, body = post(http_port, {"cmd": "tab.list"})
        check("ambiguous target is refused, not guessed",
              status == 409 and (body.get("error") or {}).get("code") == "browser_required",
              f"http {status}")

        status, body = post(http_port, {"cmd": "tab.list", "browser": "ci-b"})
        check("label selects the right browser",
              status == 200 and (body.get("result") or [{}])[0].get("title") == "B")

        status, body = post(http_port, {"cmd": "tab.list", "browser": "bbbb"})
        check("unique instanceId prefix works", status == 200 and (body.get("result") or [{}])[0].get("id") == 2)

        status, body = post(http_port, {"cmd": "tab.list", "browser": "ci"})
        check("ambiguous prefix -> browser_ambiguous",
              status == 409 and (body.get("error") or {}).get("code") == "browser_ambiguous",
              f"http {status}")

        status, body = post(http_port, {"cmd": "tab.list", "browser": "nope"})
        check("unknown target -> browser_not_found",
              status == 404 and (body.get("error") or {}).get("code") == "browser_not_found",
              f"http {status}")

        print("== native host framing ==")
        frame_msg = json.dumps({"type": "ping"}).encode()
        import struct
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "bridge", "native_host.py")],
                              input=struct.pack("<I", len(frame_msg)) + frame_msg,
                              capture_output=True, timeout=30,
                              env=dict(os.environ, WS_PORT=str(ws_port), HTTP_PORT=str(http_port)))
        ok = len(proc.stdout) >= 4
        reply = {}
        if ok:
            (n,) = struct.unpack("<I", proc.stdout[:4])
            ok = n == len(proc.stdout) - 4
            reply = json.loads(proc.stdout[4:4 + n].decode())
        check("native host speaks the 4-byte length framing", ok, f"{len(proc.stdout)} bytes")
        check("native host reports the listening bridge", reply.get("up") is True, str(reply))
    finally:
        try:
            post(http_port, {"cmd": "__shutdown__"}, timeout=3)
        except Exception:
            pass
        try:
            daemon.terminate()
            daemon.wait(timeout=10)
        except Exception:
            daemon.kill()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
