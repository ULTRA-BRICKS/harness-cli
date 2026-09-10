"""Live omp (Oh My Pi) control via ACP — a faithful, one-shot port of tg-ctl's
`ensureOmpAcp`/`promptOmpAcp` (tg-cli `features/tg-ctl/omp.ts` +
`tg-ctl` lines ~1100-1275, as of tg-cli main 2026-09).

Accessed via: `harness nudge <omp-agent-id> <message>` -> nudge.py's per-kind
registry -> this module. This is the ONLY place in harness-cli (or, after this
change, anywhere) that speaks the omp ACP JSON-RPC protocol; tg-cli's own
hand-rolled client is being retired in favor of calling `harness nudge`.

Protocol facts (proven on omp 18.1.14):
  - Framing is NDJSON — one JSON-RPC object per line on stdin AND stdout.
    LSP Content-Length framing makes `omp acp` exit 0 with empty stdout; do
    not use it (tg-cli#341).
  - `session/load` (never `session/new` here — this always targets an
    existing live session, never an ephemeral sibling) needs `--allow-home`
    and must NOT pass `--no-session` on the omp argv.
  - `initialize` and `session/load` get a fixed 15s timeout; only the final
    `session/prompt` call uses the caller-supplied timeout, because a prompt
    only resolves at turn completion and a healthy turn can legitimately run
    long.

Assumptions: this is fire-and-forget, at-most-once-per-silence-episode - unlike
tg-ctl's `ompAcpClients` map, there is no persistent client cache here. Every
`nudge()` call spawns a fresh `omp acp` child and kills it when done.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from harness_cli import __version__
from harness_cli.transcripts import _open_transcript_atomically

_INIT_TIMEOUT_SECONDS = 15.0
_SESSION_LOAD_TIMEOUT_SECONDS = 15.0


def resolve_target(path: Path) -> dict | None:
    """Scan an omp session jsonl for its `type:"session"` record.

    Returns `{"cwd": ..., "session_id": ...}` once both fields are found, else
    None. Never raises — a vanished/corrupt/unreadable file is just "cannot
    resolve", mirroring agents.py's own "malformed input yields empty" rule.
    """
    cwd: str | None = None
    session_id: str | None = None
    try:
        with _open_transcript_atomically(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or '"session"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError, RecursionError):
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "session":
                    continue
                rec_cwd = rec.get("cwd")
                if isinstance(rec_cwd, str) and rec_cwd:
                    cwd = rec_cwd
                rec_id = rec.get("id")
                if isinstance(rec_id, str) and rec_id:
                    session_id = rec_id
                if cwd and session_id:
                    return {"cwd": cwd, "session_id": session_id}
    except OSError:
        return None
    return None


class _AcpReader:
    """Background reader thread: parses NDJSON lines off `omp acp`'s stdout."""

    def __init__(self, stdout) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(stdout,), daemon=True)
        self._thread.start()

    def _run(self, stdout) -> None:
        try:
            for raw in stdout:
                line = raw.strip()
                if not line.startswith("{"):
                    continue
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, ValueError, RecursionError):
                    continue
                self._queue.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)  # sentinel: stdout closed

    def get(self, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for omp acp response")
        try:
            return self._queue.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("timed out waiting for omp acp response") from None


def _write(proc: subprocess.Popen, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _call(
    proc: subprocess.Popen, reader: _AcpReader, message: dict, timeout_seconds: float
) -> dict:
    """Send `message`, then wait (up to `timeout_seconds`) for its matching reply.

    Notifications and permission requests with a different/absent id are
    ignored — they belong to omp's own session/update stream, not this call.
    """
    _write(proc, message)
    expected_id = message["id"]
    method = message.get("method", "call")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            received = reader.get(deadline)
        except TimeoutError:
            raise TimeoutError(f"ACP {method} timed out") from None
        if received is None:
            raise RuntimeError("omp acp process closed stdout")
        if not isinstance(received, dict) or received.get("id") != expected_id:
            continue
        error = received.get("error")
        if error is not None:
            message_text = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(message_text or "ACP error")
        return received


def _session_id_from_result(message: dict) -> str | None:
    result = message.get("result")
    if not isinstance(result, dict):
        return None
    session_id = result.get("sessionId")
    return session_id if isinstance(session_id, str) and session_id else None


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _build_initialize(call_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientInfo": {"name": "harness-cli", "version": __version__},
            "clientCapabilities": {},
        },
    }


def _build_session_load(call_id: int, cwd: str, session_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "session/load",
        "params": {"cwd": cwd, "mcpServers": [], "sessionId": session_id},
    }


def _build_session_prompt(call_id: int, session_id: str, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
    }


def nudge(target: dict, message: str, timeout_seconds: float) -> dict:
    """One-shot: spawn `omp acp`, initialize, session/load, session/prompt, kill.

    `target` is a `resolve_target()` result (`{"cwd", "session_id"}`).
    Returns `{"ok": True}` or `{"ok": False, "error": "..."}`, never raises.
    """
    cwd = target.get("cwd") or "/"
    session_id = target.get("session_id")
    if not session_id:
        return {"ok": False, "error": "no session id resolved for this agent"}

    try:
        proc = subprocess.Popen(
            ["omp", "acp", "--allow-home", f"--cwd={cwd}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "omp binary not found on PATH"}

    reader = _AcpReader(proc.stdout)
    try:
        _call(proc, reader, _build_initialize(1), _INIT_TIMEOUT_SECONDS)
        loaded = _call(
            proc, reader, _build_session_load(2, cwd, session_id), _SESSION_LOAD_TIMEOUT_SECONDS
        )
        loaded_session_id = _session_id_from_result(loaded) or session_id
        _call(proc, reader, _build_session_prompt(3, loaded_session_id, message), timeout_seconds)
        return {"ok": True}
    except (TimeoutError, RuntimeError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        _terminate(proc)
