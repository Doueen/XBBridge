#!/usr/bin/env python3
"""xb bridge daemon — local WebSocket <-> HTTP gateway for the xb browser extension.

Two listeners on loopback:
  ws://127.0.0.1:8787    extensions connect here (role handshake via {"hello":...})
  http://127.0.0.1:8788  the CLI (or anything else) POSTs /rpc {"cmd":..,"args":..}

Several browsers may stay connected at once (your daily Edge plus a throw-away
test profile); each is registered by its own instanceId and addressed with
`browser` on the request. With exactly one connected, `browser` may be omitted.

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
    "browser_not_found": 404,
    "restricted_page": 409,
    "stale_generation": 409,
    "tab_not_owned": 409,
    "claim_required": 409,
    "browser_required": 409,
    "browser_ambiguous": 409,
    "extension_unavailable": 503,
    "unavailable": 503,
    "timeout": 504,
}

LOCK = threading.Lock()
EXTS: dict[str, dict] = {}      # instanceId -> {"conn", "info", "since", "seq"}
PENDING: dict[str, dict] = {}   # rid -> {"q": Queue, "instance": instanceId}
SEQ = 0
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


# ------------------------------------------------------------------ registry

def _describe(instance_id: str, entry: dict) -> dict:
    info = entry.get("info") or {}
    return {
        "instanceId": instance_id,
        "label": info.get("label") or "",
        "version": info.get("version") or "",
        "browser": info.get("browserName") or "",
        "userAgent": (info.get("browser") or "")[:160],
        "connectedAt": entry.get("since"),
        "connectedFor": round(time.time() - (entry.get("since") or time.time()), 1),
    }


def instances() -> list[dict]:
    with LOCK:
        rows = [(_describe(i, e), e.get("seq", 0)) for i, e in EXTS.items()]
    rows.sort(key=lambda r: (-r[1], r[0]["instanceId"]))
    return [r[0] for r in rows]


def _listing() -> str:
    rows = instances()
    if not rows:
        return "none connected"
    return "; ".join(
        f"{r['instanceId']}{' (' + r['label'] + ')' if r['label'] else ''} v{r['version']}"
        for r in rows)


def resolve_instance(browser: str | None) -> str:
    """Map an explicit --browser value (id or unique label/prefix) to an instance."""
    with LOCK:
        ids = list(EXTS.keys())
        infos = {i: (EXTS[i].get("info") or {}) for i in ids}
    if not ids:
        raise BridgeError(
            "no browser is connected — open Edge with the xb extension loaded "
            f"(it dials ws://127.0.0.1:{WS_PORT})",
            code="extension_unavailable", retryable=True)
    if browser:
        want = str(browser).strip()
        low = want.lower()
        hits = [i for i in ids if i == want]
        if not hits:
            hits = [i for i in ids if (infos[i].get("label") or "").lower() == low]
        if not hits:
            hits = [i for i in ids if i.startswith(want)
                    or (infos[i].get("label") or "").lower().startswith(low)]
        if not hits:
            raise BridgeError(f"no browser matches {want!r} — connected: {_listing()}",
                              code="browser_not_found",
                              details={"instances": instances()})
        if len(hits) > 1:
            raise BridgeError(
                f"{want!r} matches {len(hits)} browsers ({', '.join(hits)}); "
                "use the full instance id or give them distinct labels",
                code="browser_ambiguous", details={"matches": hits})
        return hits[0]
    if len(ids) == 1:
        return ids[0]
    raise BridgeError(
        f"{len(ids)} browsers are connected — pick one with --browser <instance|label> "
        f"(`xb browsers`); connected: {_listing()}",
        code="browser_required", details={"instances": instances()})


def _fail_instance(instance_id: str, reason: str, code: str) -> None:
    """Wake only the callers that were talking to the browser that just went away."""
    with LOCK:
        queues = [p["q"] for p in PENDING.values() if p.get("instance") == instance_id]
    for q in queues:
        try:
            q.put_nowait({"ok": False,
                          "error": {"code": code, "message": reason,
                                    "outcome": "unknown", "retryable": True}})
        except queue.Full:
            pass


def wait_for_instance(instance_id: str, seconds: float = 8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        with LOCK:
            if instance_id in EXTS:
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


# ----------------------------------------------------------------------- rpc

def call(cmd: str, args: dict | None = None, timeout: float = 30.0,
         browser: str | None = None):
    instance = resolve_instance(browser)
    try:
        return _call_once(instance, cmd, args, timeout)
    except BridgeError as exc:
        if exc.code in ("extension_unavailable", "timeout") and cmd in IDEMPOTENT \
                and wait_for_instance(instance, 8.0):
            return _call_once(instance, cmd, args, timeout)
        raise


def _call_once(instance: str, cmd: str, args: dict | None, timeout: float):
    with LOCK:
        entry = EXTS.get(instance)
        conn = entry.get("conn") if entry else None
    if conn is None:
        raise BridgeError(f"browser {instance} is no longer connected",
                          code="extension_unavailable", retryable=True)
    rid = uuid.uuid4().hex[:12]
    q: queue.Queue = queue.Queue()
    PENDING[rid] = {"q": q, "instance": instance}
    try:
        conn.send(json.dumps({"id": rid, "cmd": cmd, "args": args or {}}))
        msg = q.get(timeout=timeout)
    except queue.Empty:
        raise BridgeError(f"timeout after {timeout:g}s waiting for '{cmd}' on {instance}",
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
    global SEQ
    instance = None
    # A web page can open ws://127.0.0.1:8787 too (and would then be able to
    # register itself as a "browser" and receive commands meant for one). Page
    # handshakes always carry an http(s) Origin; extension service workers and
    # local tools do not — so reject the page case outright.
    origin = ""
    try:
        req = getattr(conn, "request", None)
        origin = (req.headers.get("Origin") or "") if req is not None else ""
    except Exception:
        origin = ""
    lowered = origin.lower()
    if lowered.startswith(("http://", "https://")):
        log(f"rejected ws connection from origin {origin!r} (websites may not join the bridge)")
        try:
            conn.close(code=1008, reason="web origins are not allowed")
        except Exception:
            pass
        return
    try:
        for raw in conn:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            hello = msg.get("hello")
            if hello is not None:
                # a reconnect of the same install replaces its old socket
                instance = hello.get("instanceId") or f"anon-{id(conn) % 100000}"
                with LOCK:
                    SEQ += 1
                    old = EXTS.get(instance) or {}
                    EXTS[instance] = {
                        "conn": conn,
                        "info": hello,
                        "since": old.get("since") or time.time(),
                        "seq": SEQ,
                    }
                label = hello.get("label")
                log(f"browser connected: {instance}"
                    f"{' (' + label + ')' if label else ''} v{hello.get('version')}"
                    f" origin={origin or '-'}")
                continue
            with LOCK:
                entry = PENDING.get(msg.get("id"))
            if entry is not None and entry.get("instance") == instance:
                entry["q"].put(msg)
    except Exception as exc:  # connection reset etc.
        if instance:
            log("ws error:", instance, exc)
    finally:
        with LOCK:
            if instance and (EXTS.get(instance) or {}).get("conn") is conn:
                EXTS.pop(instance, None)
        if instance:
            _fail_instance(instance, f"browser {instance} disconnected mid-command",
                           "extension_unavailable")
            log("browser disconnected:", instance)


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
        path = self.path.split("?")[0]
        if path in ("/health", "/browsers", "/"):
            rows = instances()
            first = rows[0] if rows else {}
            self._send(200, {
                "ok": True,
                "extension": bool(rows),          # legacy: any browser connected
                "info": first,                    # legacy: the most recent browser
                "instances": rows,
                "count": len(rows),
                "pid": os.getpid(),
                "since": STARTED,
            })
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
            self._send(400, {"ok": False, "error": {"code": "invalid_params",
                                                    "message": f"bad json: {exc}",
                                                    "outcome": "not_started",
                                                    "retryable": False}})
            return
        try:
            result = call(req.get("cmd") or "", req.get("args") or {},
                          float(req.get("timeout") or 30),
                          browser=req.get("browser") or None)
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

    log(f"xb bridge up — ws://127.0.0.1:{WS_PORT}  http://127.0.0.1:{HTTP_PORT}"
        f"  (multi-browser: `xb browsers`)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
