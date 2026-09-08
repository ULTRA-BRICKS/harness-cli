"""Local process-ticket tracker so agent/session work stays off product GitHub.

Accessed via: `harness task new|list|done` -> cli._run_task -> this module.

Assumptions: tickets are local JSON files only. This module never calls `gh`,
never hits api.github.com, and never inspects the cwd git remote. Store path is
$HARNESS_TASK_DIR, else $XDG_STATE_HOME/harness-cli/tasks, else
~/.local/state/harness-cli/tasks.

Past bugs: conloca/landing#263 — a process ticket titled "Review documentation
diff for performance regressions" was filed as a GitHub issue in a product
repo. Guarded by
tests/test_tasks.py::test_process_ticket_does_not_create_github_product_issue.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.report import _sanitize


def task_dir() -> Path:
    explicit = os.environ.get("HARNESS_TASK_DIR")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "harness-cli" / "tasks"
    return Path.home() / ".local" / "state" / "harness-cli" / "tasks"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _next_id(store: Path) -> int:
    highest = 0
    if not store.is_dir():
        return 1
    for path in store.glob("*.json"):
        try:
            number = int(path.stem)
        except ValueError:
            continue
        highest = max(highest, number)
    return highest + 1


def _labels() -> list[str]:
    labels = ["agent"]
    session = os.environ.get("TASK_SESSION")
    if session:
        labels.append(f"session:{session}")
    return labels


def _ticket_id(payload: dict) -> int | None:
    try:
        number = int(payload["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if number < 1:
        return None
    return number


def create_ticket(title: str, body: str = "") -> dict:
    store = task_dir()
    ticket_id = _next_id(store)
    ticket = {
        "id": ticket_id,
        "title": title,
        "body": body,
        "state": "open",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": os.environ.get("TASK_SESSION") or "",
        "labels": _labels(),
    }
    _atomic_write(store / f"{ticket_id}.json", ticket)
    return ticket


def list_tickets(include_done: bool = False) -> list[dict]:
    store = task_dir()
    if not store.is_dir():
        return []
    tickets: list[dict] = []
    for path in store.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if not include_done and payload.get("state") != "open":
            continue
        if _ticket_id(payload) is None:
            continue
        tickets.append(payload)
    tickets.sort(key=_ticket_id)
    return tickets


def mark_done(ticket_id: int) -> dict | None:
    path = task_dir() / f"{ticket_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["state"] = "done"
    _atomic_write(path, payload)
    return payload


def _cmd_new(args: argparse.Namespace) -> int:
    ticket = create_ticket(args.title, args.body or "")
    print(_sanitize(str(ticket["title"])))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    tickets = list_tickets(include_done=bool(args.all))
    if args.json:
        print(json.dumps(tickets))
        return 0
    for ticket in tickets:
        print(
            f"{_sanitize(str(ticket.get('id', '')))} "
            f"{_sanitize(str(ticket.get('state', '')))} "
            f"{_sanitize(str(ticket.get('title', '')))}"
        )
    return 0


def _cmd_done(args: argparse.Namespace) -> int:
    ticket_id = args.ticket_id
    if mark_done(ticket_id) is None:
        print(f"task not found: {ticket_id}", file=sys.stderr)
        return 1
    return 0


def run(args: argparse.Namespace) -> int:
    command = args.task_command
    if command == "new":
        return _cmd_new(args)
    if command == "list":
        return _cmd_list(args)
    if command == "done":
        return _cmd_done(args)
    return 0
