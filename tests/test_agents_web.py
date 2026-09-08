from __future__ import annotations

import http.client
import json
import threading

import pytest

from harness_cli import cli

PEER_HREFS = (
    "http://127.0.0.1:7878",
    "http://127.0.0.1:7920",
    "http://127.0.0.1:8787",
    "http://127.0.0.1:8797",
    "http://127.0.0.1:8733",
)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_TASK_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("HARNESS_QUESTIONS_DIR", str(tmp_path / "questions"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def _get(port, path, host=None, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {"Host": host or f"127.0.0.1:{port}"}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, resp.getheader("Content-Type") or "", body
    finally:
        conn.close()


def test_agents_web_help_lists_flags(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["agents", "web", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "--host" in help_text
    assert "--port" in help_text
    assert "--no-open" in help_text


def test_agents_web_port_0_prints_url(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    from harness_cli import agents_web

    monkeypatch.setattr(agents_web.ThreadingHTTPServer, "serve_forever", lambda self: None)

    def _must_not_open(*_a, **_k):
        raise AssertionError("webbrowser.open must not run with --no-open")

    monkeypatch.setattr(agents_web.webbrowser, "open", _must_not_open)
    rc = cli.main(["agents", "web", "--port", "0", "--no-open"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://127.0.0.1:" in out


def _start_server():
    from harness_cli import agents_web

    httpd = agents_web.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, httpd.server_address[1]


def _stop_server(httpd, thread):
    httpd._sse_stop = True
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def test_web_serves_html_landscape_peers_and_health(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "1.json").write_text(
        json.dumps({"id": 1, "title": "web ticket", "state": "open"}),
        encoding="utf-8",
    )
    httpd = thread = None
    try:
        httpd, thread, port = _start_server()
        status, ctype, raw = _get(port, "/")
        html = raw.decode("utf-8")
        assert status == 200
        assert "text/html" in ctype
        lowered = html.lower()
        assert "agents" in lowered
        assert "questions" in lowered
        assert "tasks" in lowered
        for href in PEER_HREFS:
            assert href in html
        status, _ctype, raw = _get(port, "/api/landscape")
        landscape = json.loads(raw.decode("utf-8"))
        assert status == 200
        assert "generated_at" in landscape
        assert "window" in landscape
        assert "agents" in landscape
        assert "questions" in landscape
        assert "tasks" in landscape
        assert "peers" in landscape
        titles = [row.get("title") for row in landscape["tasks"]]
        assert "web ticket" in titles
        status, _ctype, raw = _get(port, "/api/peers")
        peers = json.loads(raw.decode("utf-8"))
        assert status == 200
        urls = {row["url"] for row in peers}
        for href in PEER_HREFS:
            assert href in urls
        for row in peers:
            assert "name" in row and "url" in row and "up" in row
        status, _ctype, raw = _get(port, "/api/health")
        health = json.loads(raw.decode("utf-8"))
        assert status == 200
        assert health == {"ok": True, "service": "harness-agents"}
    finally:
        if httpd is not None:
            _stop_server(httpd, thread)


def test_web_host_allowlist_and_unknown_path(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    httpd = thread = None
    try:
        httpd, thread, port = _start_server()
        status, _ctype, _body = _get(port, "/api/health", host="evil.example.com")
        assert status == 403
        status, _ctype, _body = _get(port, "/api/health", host=f"127.0.0.1:{port}")
        assert status == 200
        status, _ctype, _body = _get(port, "/no-such-route")
        assert status == 404
        status, _ctype, _body = _get(port, "/../../etc/passwd")
        assert status in (400, 403, 404)
    finally:
        if httpd is not None:
            _stop_server(httpd, thread)


def test_web_sse_events_stream(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    httpd = thread = None
    conn = None
    try:
        httpd, thread, port = _start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        assert resp.status == 200
        ctype = resp.getheader("Content-Type") or ""
        assert ctype.startswith("text/event-stream")
        buf = b""
        for _ in range(40):
            chunk = resp.read1(256)
            if not chunk:
                break
            buf += chunk
            if b"data:" in buf or b": " in buf:
                break
        assert b"data:" in buf or buf.startswith(b":") or b"event:" in buf, buf[:400]
    finally:
        if conn is not None:
            conn.close()
        if httpd is not None:
            _stop_server(httpd, thread)

def test_web_html_contains_eventsource_and_events(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    httpd = thread = None
    try:
        httpd, thread, port = _start_server()
        status, _ctype, raw = _get(port, "/")
        html = raw.decode("utf-8")
        assert status == 200
        assert "EventSource" in html
        assert "/events" in html
    finally:
        if httpd is not None:
            _stop_server(httpd, thread)


def test_request_allowed_rejects_spoofed_host_and_non_loopback_client():
    from harness_cli.agents_web import request_allowed

    assert request_allowed("127.0.0.1", "127.0.0.1") is True
    assert request_allowed("localhost:7888", "127.0.0.1") is True
    assert request_allowed("[::1]:7888", "::1") is True
    assert request_allowed("evil.example.com", "127.0.0.1") is False
    assert request_allowed("127.0.0.1", "8.8.8.8") is False
    assert request_allowed("localhost", "::ffff:127.0.0.1") is True
