"""Pure-function tests: payload construction and the invoke contract.

No network calls — send_pushover is monkeypatched everywhere it matters.
handle_invoke must never raise or return anything but {"continue": True},
same convention as every handler in sijba core.
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as pushover  # noqa: E402


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(pushover, "PUSHOVER_APP_TOKEN", "app-tok")
    monkeypatch.setattr(pushover, "PUSHOVER_USER_KEY", "user-key")
    monkeypatch.setattr(pushover, "PUSHOVER_TITLE", "Sijba")
    monkeypatch.setattr(pushover, "PUSHOVER_PRIORITY", "0")


# ------------------------------------------------------------------
# build_payload
# ------------------------------------------------------------------

def test_build_payload_uses_pipeline_text():
    env = {"pipeline_text": "licht ist an", "source": "webhook:voice"}

    payload = pushover.build_payload(env)

    assert payload["message"] == "licht ist an"
    assert payload["token"] == "app-tok"
    assert payload["user"] == "user-key"
    assert payload["title"] == "Sijba — webhook:voice"
    assert payload["priority"] == "0"


def test_build_payload_falls_back_to_text():
    payload = pushover.build_payload({"text": "fallback", "source": "webhook:voice"})
    assert payload["message"] == "fallback"


def test_build_payload_prefers_pipeline_text_over_text():
    env = {"pipeline_text": "final", "text": "original", "source": "webhook:voice"}
    assert pushover.build_payload(env)["message"] == "final"


def test_empty_reply_produces_no_payload():
    assert pushover.build_payload({"pipeline_text": "", "source": "webhook:voice"}) is None
    assert pushover.build_payload({"source": "webhook:voice"}) is None


def test_whitespace_only_reply_produces_no_payload():
    assert pushover.build_payload({"pipeline_text": "   \n", "source": "x"}) is None


def test_message_is_truncated_to_pushovers_limit():
    env = {"pipeline_text": "x" * 2000, "source": "webhook:voice"}

    payload = pushover.build_payload(env)

    assert len(payload["message"]) == pushover._MESSAGE_LIMIT
    assert payload["message"].endswith("…")


def test_title_includes_the_source_for_disambiguation():
    payload = pushover.build_payload({"pipeline_text": "x", "source": "webhook:iot"})
    assert payload["title"] == "Sijba — webhook:iot"


def test_missing_source_falls_back_to_a_generic_title():
    payload = pushover.build_payload({"pipeline_text": "x"})
    assert payload["title"] == "Sijba — sijba"


# ------------------------------------------------------------------
# handle_invoke — the /invoke contract
# ------------------------------------------------------------------

def test_handle_invoke_sends_and_continues(monkeypatch):
    sent = []
    monkeypatch.setattr(pushover, "send_pushover", lambda p: sent.append(p) or True)

    resp = pushover.handle_invoke({"pipeline_text": "hi", "source": "webhook:voice"})

    assert resp == {"continue": True}
    assert len(sent) == 1


def test_handle_invoke_never_breaks_the_pipeline_on_send_failure(monkeypatch):
    monkeypatch.setattr(pushover, "send_pushover", lambda p: False)

    resp = pushover.handle_invoke({"pipeline_text": "hi", "source": "webhook:voice"})

    assert resp == {"continue": True}


def test_handle_invoke_skips_sending_when_unconfigured(monkeypatch):
    monkeypatch.setattr(pushover, "PUSHOVER_APP_TOKEN", "")
    called = []
    monkeypatch.setattr(pushover, "send_pushover", lambda p: called.append(p))

    resp = pushover.handle_invoke({"pipeline_text": "hi", "source": "webhook:voice"})

    assert resp == {"continue": True}
    assert called == []


def test_handle_invoke_skips_sending_for_empty_text(monkeypatch):
    called = []
    monkeypatch.setattr(pushover, "send_pushover", lambda p: called.append(p))

    resp = pushover.handle_invoke({"pipeline_text": "", "source": "webhook:voice"})

    assert resp == {"continue": True}
    assert called == []


# ------------------------------------------------------------------
# HTTP handler contract (mirrors what core's plugin_runner sends over
# /invoke and what it does with a malformed response)
# ------------------------------------------------------------------

def test_http_invoke_roundtrip(monkeypatch):
    import http.client
    import threading

    monkeypatch.setattr(pushover, "send_pushover", lambda p: True)
    server = pushover.ThreadingHTTPServer(("127.0.0.1", 0), pushover.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        conn.request("GET", "/health")
        r = conn.getresponse()
        assert r.status == 200
        assert r.read()

        body = b'{"pipeline_text": "hallo", "source": "webhook:voice"}'
        conn.request("POST", "/invoke", body=body,
                     headers={"Content-Length": str(len(body))})
        r = conn.getresponse()
        assert r.status == 200
        import json
        assert json.loads(r.read()) == {"continue": True}
    finally:
        server.shutdown()


def test_http_invoke_bad_json_still_returns_continue():
    import http.client
    import threading

    server = pushover.ThreadingHTTPServer(("127.0.0.1", 0), pushover.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = b"not json"
        conn.request("POST", "/invoke", body=body,
                     headers={"Content-Length": str(len(body))})
        r = conn.getresponse()
        assert r.status == 200
        import json
        assert json.loads(r.read()) == {"continue": True}
    finally:
        server.shutdown()
