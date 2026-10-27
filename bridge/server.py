#!/usr/bin/env python3
"""xb bridge daemon — local WebSocket <-> HTTP gateway for the xb browser extension.

Two listeners on loopback:
  ws://127.0.0.1:8787    the extension connects here (role handshake via {"hello":...})
  http://127.0.0.1:8788  the CLI (or anything else) POSTs /rpc {"cmd":..,"args":..}

No third-party deps beyond `websockets`.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.sync.server import serve

WS_PORT = int(os.environ.get("XB_WS_PORT", "8787"))
HTTP_PORT = int(os.environ.get("XB_HTTP_PORT", "8788"))
MAX_MSG = 96 * 1024 * 1024  # screenshots come back as base64 data URLs

# HTTP status mirrors the failure class so automation can branch without
# parsing prose.
STATUS_BY_CODE = {
    "invalid_params": 400,
    "unsupported_method": 400,
    "target_not_found": 404,
    "no_target_tab": 404,
    "restricted_page": 409,
    "stale_generation": 409,
    "tab_not_owned": 409,
    "claim_required": 409,
    "extension_unavailable": 503,
    "unavailable": 503,
    "timeout": 504,
}

LOCK = threading.Lock()
EXT = None            # live ServerConnection to the extension
EXT_INFO: dict = {}   # hello payload
PENDING: dict[str, queue.Queue] = {}
STARTED = time.time()


def log(*parts) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}]", *parts, flush=True)


class BridgeError(RuntimeError):
    """Failure carrying the same contract the extension uses."""

    def __init__(self, message: str, code: str = "unavailable",
                 outcome: str = "not_started", retryable: bool = False, details=None):
        super().__init__(message)
        self.code = code
        self.outcome = outcome
        self.retryable = retryable
        self.details = details

    def payload(self) -> dict:
        out = {"code": self.code, "message": str(self), "outcome": self.outcome,
               "retryable": self.retryable}
        if self.details is not None:
            out["details"] = self.details
        return out


def _fail_pending(reason: str, code: str) -> None:
    """Wake every in-flight caller when the extension goes away.

    Without this a call waits out its whole timeout after a disconnect, which
    looks like a hung command instead of a lost browser.
    """
    with LOCK:
        queues = list(PENDING.values())
    for q in queues:
        try:
            q.put_nowait({"ok": False,
                          "error": {"code": code, "message": reason,
                                    "outcome": "unknown", "retryable": True}})
        except queue.Full:
            pass


def wait_for_extension(seconds: float = 8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        with LOCK:
            if EXT is not None:
                return True
        time.sleep(0.15)
    return False


# Commands that can be replayed without side effects. A worker restart mid-call
# otherwise surfaces as a 60s timeout even though nothing happened.
IDEMPOTENT = {
    "ping", "doctor", "diag.apis", "browser.info", "debugger.state",
    "tab.list", "tab.get", "window.list", "panel.state", "panel.focus",
    "lease.list", "marks", "sessions.recent",
    "snapshot", "observe", "text", "get-html", "extract", "evaluate",
    "console.read", "network.read", "screenshot",
}


def call(cmd: str, args: dict | None = None, timeout: float = 30.0):
    try:
        return _call_once(cmd, args, timeout)
    except BridgeError as exc:
        if exc.code in ("extension_unavailable", "timeout") and cmd in IDEMPOTENT \
                and wait_for_extension(8.0):
            return _call_once(cmd, args, timeout)
        raise


def _call_once(cmd: str, args: dict | None = None, timeout: float = 30.0):
    with LOCK:
        conn = EXT
    if conn is None:
        raise BridgeError(
            "no extension connected — open Edge with the xb extension loaded "
            f"(it dials ws://127.0.0.1:{WS_PORT})",
            code="extension_unavailable", retryable=True,
        )
    rid = uuid.uuid4().hex[:12]
    q: queue.Queue = queue.Queue()
    PENDING[rid] = q
    try:
        conn.send(json.dumps({"id": rid, "cmd": cmd, "args": args or {}}))
        msg = q.get(timeout=timeout)
    except queue.Empty:
        raise BridgeError(f"timeout after {timeout:g}s waiting for '{cmd}'",
                          code="timeout", outcome="unknown", retryable=True)
    finally:
        PENDING.pop(rid, None)
    if not msg.get("ok"):
        err = msg.get("error")
        if isinstance(err, dict):
            raise BridgeError(err.get("message") or f"{cmd} failed",
                              code=err.get("code") or "command_failed",
                              outcome=err.get("outcome") or "unknown",
                              retryable=bool(err.get("retryable")),
                              details=err.get("details"))
        raise BridgeError(str(err or f"{cmd} failed"), code="command_failed",
                          outcome="unknown")
    return msg.get("result")


def ws_handler(conn) -> None:
    global EXT
    try:
        for raw in conn:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            hello = msg.get("hello")
            if hello is not None:
                with LOCK:
                    EXT = conn
                    EXT_INFO.clear()
                    EXT_INFO.update(hello)
                    EXT_INFO["connectedAt"] = time.time()
                log("extension connected:", hello.get("name"), hello.get("version"))
                continue
            q = PENDING.get(msg.get("id"))
            if q is not None:
                q.put(msg)
    except Exception as exc:  # connection reset etc.
        if conn is EXT:
            log("ws error:", exc)
    finally:
        with LOCK:
            if EXT is conn:
                EXT = None
        _fail_pending("the extension disconnected mid-command", "extension_unavailable")
        log("extension disconnected")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence per-request logging
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/"):
            with LOCK:
                alive = EXT is not None
                info = dict(EXT_INFO)
            info.pop("stats", None)
            self._send(200, {"ok": True, "extension": alive, "info": info,
                             "pid": os.getpid(), "since": STARTED})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if path == "/shutdown":
            self._send(200, {"ok": True, "stopping": True})
            threading.Thread(target=lambda: (time.sleep(0.15), os._exit(0)), daemon=True).start()
            return
        if path != "/rpc":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            req = json.loads(raw or b"{}")
        except Exception as exc:
            self._send(400, {"ok": False, "error": f"bad json: {exc}"})
            return
        try:
            result = call(req.get("cmd") or "", req.get("args") or {},
                          float(req.get("timeout") or 30))
            self._send(200, {"ok": True, "result": result})
        except BridgeError as exc:
            self._send(STATUS_BY_CODE.get(exc.code, 400),
                       {"ok": False, "error": exc.payload()})
        except Exception as exc:
            self._send(500, {"ok": False,
                             "error": {"code": "bridge_error",
                                       "message": f"{type(exc).__name__}: {exc}",
                                       "outcome": "unknown", "retryable": False}})


def main() -> int:
    ws = serve(ws_handler, "127.0.0.1", WS_PORT, ping_interval=15,
               ping_timeout=None, max_size=MAX_MSG, compression=None, close_timeout=2)
    threading.Thread(target=ws.serve_forever, name="ws", daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()

    log(f"xb bridge up — ws://127.0.0.1:{WS_PORT}  http://127.0.0.1:{HTTP_PORT}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
