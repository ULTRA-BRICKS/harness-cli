"""Local live web view of the harness agents landscape.

Accessed via: `harness agents web` -> cli._run_agents -> this module. Binds
ThreadingHTTPServer (default 127.0.0.1:7888). `--port 0` is ephemeral.

Assumptions: loopback by default; Host allowlist is localhost / 127.0.0.1 / ::1.
Reads only — no path traversal, no writes. Peer probes use a short TCP timeout
and never block listing when a peer is down. HTML is sanitized.

Past bugs: none yet — new command. Port 7878 is the review dashboard, so this
view defaults to 7888.
"""

from __future__ import annotations

import argparse
import html
import json
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from harness_cli.agents import build_landscape
from harness_cli.report import _sanitize

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7888
PEER_TIMEOUT = 0.2
SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_SECONDS = 15.0
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
CACHE_SECONDS = 1.0
_payload_cache: dict = {"at": 0.0, "data": None}
PEERS = (
    {"name": "Review dashboard", "url": "http://127.0.0.1:7878"},
    {"name": "Spec-web", "url": "http://127.0.0.1:7920"},
    {"name": "rig config-web", "url": "http://127.0.0.1:8787"},
    {"name": "rig evolve", "url": "http://127.0.0.1:8797"},
    {"name": "3d web", "url": "http://127.0.0.1:8733"},
)


def _esc(value) -> str:
    return html.escape(_sanitize(str(value)), quote=True)


def _tcp_up(url: str, timeout: float = PEER_TIMEOUT) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or DEFAULT_HOST
    port = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_peers(timeout: float = PEER_TIMEOUT) -> list[dict]:
    rows = []
    for peer in PEERS:
        try:
            up = _tcp_up(peer["url"], timeout)
        except OSError:
            up = False
        rows.append({"name": peer["name"], "url": peer["url"], "up": up})
    return rows


def header_host(raw: str) -> str:
    value = (raw or "").split("/")[0].strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end]
    return value.split(":")[0].strip()


def client_ip(addr: str) -> str:
    ip = (addr or "").strip().lower()
    if ip.startswith("::ffff:"):
        return ip[7:]
    return ip


def request_allowed(host_header: str, peer_ip: str) -> bool:
    return header_host(host_header) in ALLOWED_HOSTS and client_ip(peer_ip) in LOOPBACK_CLIENTS


def landscape_payload() -> dict:
    now = time.monotonic()
    cached = _payload_cache.get("data")
    if cached is not None and now - float(_payload_cache["at"]) < CACHE_SECONDS:
        return cached
    data = build_landscape(include_all=False)
    data["peers"] = probe_peers()
    _payload_cache["at"] = now
    _payload_cache["data"] = data
    return data


def _li(parts: list[str]) -> str:
    return "<li>" + " ".join(parts) + "</li>"


def _agent_li(row: dict) -> str:
    return _li([
        _esc(row.get("status")),
        _esc(row.get("harness")),
        _esc(row.get("kind")),
        _esc(row.get("name")),
        "—",
        _esc(row.get("last_text") or ""),
    ])


def _question_li(row: dict) -> str:
    return _li([
        _esc(row.get("agent") or ""),
        _esc(row.get("question") or row.get("title") or ""),
    ])


def _task_li(row: dict) -> str:
    return _li([
        _esc(row.get("id")),
        _esc(row.get("state")),
        _esc(row.get("title")),
    ])


def _section(rows: list, formatter) -> str:
    if not rows:
        return "<li>(none)</li>"
    return "".join(formatter(row) for row in rows)


def render_html(data: dict) -> str:
    nav_parts = []
    for peer in PEERS:
        nav_parts.append(
            '<a href="' + _esc(peer["url"]) + '">' + _esc(peer["name"]) + "</a>"
        )
    nav = " ".join(nav_parts)
    chunks = [
        "<!DOCTYPE html><html><head><meta charset=utf-8>",
        "<title>harness agents</title>",
        "<style>body{font-family:sans-serif;margin:1.5rem}",
        "nav a{margin-right:1rem}</style></head><body>",
        "<h1>harness agents</h1>",
        "<nav>" + nav + "</nav>",
        "<h2>Agents</h2><ul>" + _section(data.get("agents") or [], _agent_li) + "</ul>",
        "<h2>Questions</h2><ul>" + _section(data.get("questions") or [], _question_li) + "</ul>",
        "<h2>Tasks</h2><ul>" + _section(data.get("tasks") or [], _task_li) + "</ul>",
        "</body></html>",
    ]
    return "".join(chunks)


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    ThreadingHTTPServer.daemon_threads = True
    return ThreadingHTTPServer((host, port), LandscapeHandler)


class LandscapeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _host_allowed(self) -> bool:
        peer = self.client_address[0] if self.client_address else ""
        return request_allowed(self.headers.get("Host") or "", peer)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._send(403, b"forbidden\n", "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path
        if path == "/":
            body = render_html(landscape_payload()).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/landscape":
            body = json.dumps(landscape_payload()).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/api/peers":
            body = json.dumps(probe_peers()).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/api/health":
            body = json.dumps({"ok": True, "service": "harness-agents"}).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/events":
            self._sse()
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        last_beat = time.monotonic()
        try:
            while not getattr(self.server, "_sse_stop", False):
                payload = json.dumps(landscape_payload(), sort_keys=True)
                if payload != last:
                    self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last = payload
                now = time.monotonic()
                if now - last_beat >= SSE_HEARTBEAT_SECONDS:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_beat = now
                time.sleep(SSE_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.close_connection = True


def run(args: argparse.Namespace) -> int:
    host = getattr(args, "host", None) or DEFAULT_HOST
    port = int(getattr(args, "port", DEFAULT_PORT))
    httpd = make_server(host, port)
    bound = httpd.server_address[1]
    url = f"http://{host}:{bound}/"
    print(url)
    if not getattr(args, "no_open", False):
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
