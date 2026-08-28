#!/usr/bin/env python3
"""SIJBA remote handler: push a reply out via Pushover.

Registers with Core as an *outbound* handler, serves /invoke + /health,
heartbeats TTL — same shape as sijba-kubectl, but direction: outbound instead
of inbound. Which sources trigger it is entirely config (PUSHOVER_MATCH_SOURCES),
so wiring a new named webhook to Pushover needs no code or handler-file change:
POST /webhook/<token> with the right token, plus a source in this env var.

Zero third-party deps — stdlib only.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIJBA_URL = os.environ.get("SIJBA_URL", "https://sijba.edv-bartl.at").rstrip("/")
SIJBA_TOKEN = os.environ.get("SIJBA_TOKEN", "").strip()
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9101"))
HANDLER_ID = os.environ.get("HANDLER_ID", "pushover")
TTL_SECONDS = int(os.environ.get("TTL_SECONDS", "90"))
HEARTBEAT_EVERY = max(15, TTL_SECONDS // 3)
REGISTER_PRIORITY = float(os.environ.get("REGISTER_PRIORITY", "50"))

PUSHOVER_APP_TOKEN = os.environ.get("PUSHOVER_APP_TOKEN", "").strip()
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_TITLE = os.environ.get("PUSHOVER_TITLE", "Sijba").strip() or "Sijba"
PUSHOVER_PRIORITY = os.environ.get("PUSHOVER_PRIORITY", "0").strip()
PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Pushover's own limits (documented): 1024 chars message, 250 chars title.
_MESSAGE_LIMIT = 1024
_TITLE_LIMIT = 250

# Which outbound envelope sources this instance answers for. Required and
# non-empty on purpose: an unset/empty match would otherwise either register
# as a silent no-op or (if core ever changed its default) an unintended
# catch-all for every reply in the system, including the web UI's own.
PUSHOVER_MATCH_SOURCES = [
    s.strip() for s in os.environ.get("PUSHOVER_MATCH_SOURCES", "").split(",") if s.strip()
]

_stop = threading.Event()


def _log(msg: str) -> None:
    print(f"[sijba-out-pushover] {msg}", file=sys.stderr, flush=True)


def _default_advertise() -> str:
    # Best-effort primary IPv4 (not 127.0.0.1). Fine for a k8s pod too: it
    # re-registers with whatever IP it currently has on every start, so a
    # rescheduled pod just re-advertises its new one. Prefer setting
    # ADVERTISE_URL explicitly to the Service DNS name in the Deployment —
    # more standard for k8s and doesn't depend on outbound routing at boot.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{LISTEN_PORT}"
    except Exception:
        return f"http://127.0.0.1:{LISTEN_PORT}"


ADVERTISE_URL = os.environ.get("ADVERTISE_URL", "").rstrip("/") or _default_advertise()


def _api(method: str, path: str, body: dict | None = None) -> dict:
    if not SIJBA_TOKEN:
        raise RuntimeError("SIJBA_TOKEN is required")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{SIJBA_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {SIJBA_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode(errors="replace")
        raise RuntimeError(f"Core {method} {path}: HTTP {exc.code} {err[:300]}") from exc


def register() -> None:
    body = {
        "id": HANDLER_ID,
        "url": ADVERTISE_URL,
        "direction": "outbound",
        "priority": REGISTER_PRIORITY,
        "match": {"source": PUSHOVER_MATCH_SOURCES},
        "invoke_path": "/invoke",
        "timeout_seconds": 15,
        "ttl_seconds": TTL_SECONDS,
        "continue_on_match": True,
    }
    res = _api("POST", "/api/register", body)
    _log(f"registered: {res}")


def heartbeat() -> None:
    _api("POST", f"/api/register/{HANDLER_ID}/heartbeat", {"ttl_seconds": TTL_SECONDS})


def unregister() -> None:
    try:
        _api("DELETE", f"/api/register/{HANDLER_ID}")
        _log("unregistered")
    except Exception as exc:
        _log(f"unregister failed: {exc}")


def _truncate(s: str, limit: int) -> str:
    s = s or ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def build_payload(envelope: dict) -> dict | None:
    """Turn an outbound envelope into a Pushover API payload, or None if
    there is nothing worth sending (empty reply)."""
    text = (envelope.get("pipeline_text") or envelope.get("text") or "").strip()
    if not text:
        return None
    source = envelope.get("source") or "sijba"
    return {
        "token": PUSHOVER_APP_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": _truncate(text, _MESSAGE_LIMIT),
        "title": _truncate(f"{PUSHOVER_TITLE} — {source}", _TITLE_LIMIT),
        "priority": PUSHOVER_PRIORITY,
    }


def send_pushover(payload: dict) -> bool:
    """POST to the Pushover API. Never raises — a delivery failure must not
    break the pipeline, same convention as every other handler in sijba."""
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(PUSHOVER_API_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = 200 <= r.status < 300
            if not ok:
                _log(f"pushover API returned {r.status}")
            return ok
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        _log(f"pushover API error {exc.code}: {body}")
        return False
    except Exception as exc:
        _log(f"pushover send failed: {exc}")
        return False


def handle_invoke(envelope: dict) -> dict:
    if not PUSHOVER_APP_TOKEN or not PUSHOVER_USER_KEY:
        _log("PUSHOVER_APP_TOKEN / PUSHOVER_USER_KEY not configured — dropping message")
        return {"continue": True}

    payload = build_payload(envelope)
    if payload is None:
        return {"continue": True}

    sent = send_pushover(payload)
    if sent:
        _log(f"sent ({len(payload['message'])} chars, source={envelope.get('source')})")
    return {"continue": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        _log(f"http: {fmt % args}")

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] in ("/health", "/"):
            self._json(200, {"ok": True, "id": HANDLER_ID})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/invoke":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            envelope = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(200, {"continue": True})
            return
        try:
            self._json(200, handle_invoke(envelope))
        except Exception as exc:
            _log(f"invoke error: {exc}")
            self._json(200, {"continue": True})


def _heartbeat_loop() -> None:
    while not _stop.wait(HEARTBEAT_EVERY):
        try:
            heartbeat()
            _log("heartbeat ok")
        except Exception as exc:
            _log(f"heartbeat failed: {exc} — re-registering")
            try:
                register()
            except Exception as exc2:
                _log(f"re-register failed: {exc2}")


def main() -> None:
    if not PUSHOVER_MATCH_SOURCES:
        _log("PUSHOVER_MATCH_SOURCES is empty — refusing to start (see README)")
        sys.exit(1)
    if not PUSHOVER_APP_TOKEN or not PUSHOVER_USER_KEY:
        _log("warning: PUSHOVER_APP_TOKEN/PUSHOVER_USER_KEY unset — will register but drop every message")

    _log(f"SIJBA_URL={SIJBA_URL}")
    _log(f"ADVERTISE_URL={ADVERTISE_URL}")
    _log(f"LISTEN={LISTEN_HOST}:{LISTEN_PORT} id={HANDLER_ID}")
    _log(f"match sources: {PUSHOVER_MATCH_SOURCES}")

    register()

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    t = threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True)
    t.start()

    def _shutdown(*_args) -> None:
        _log("shutting down…")
        _stop.set()
        unregister()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _log("ready")
    server.serve_forever()


if __name__ == "__main__":
    main()
