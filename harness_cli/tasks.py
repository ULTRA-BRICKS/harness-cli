"""Local process-ticket tracker so agent/session work stays off product GitHub.

Accessed via: `harness tasks new|list|done|import` -> cli._run_tasks -> this module.

Assumptions: tickets are local JSON files only. This module never calls `gh`,
never hits api.github.com, and never inspects the cwd git remote. Store path is
$HARNESS_TASK_DIR, else $XDG_STATE_HOME/harness-cli/tasks, else
~/.local/state/harness-cli/tasks.

Past bugs: conloca/landing#263 -- a process ticket titled "Review documentation
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


_MAX_CREATE_ATTEMPTS = 50


def _exclusive_write(path: Path, payload: dict) -> None:
    """Create `path` only if it does not already exist; raise FileExistsError otherwise.

    Unlike `_atomic_write` (temp file + `Path.replace`, which replaces unconditionally),
    this never clobbers a file another process already created for the same id.
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


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


_VALID_STATES = frozenset({"open", "done", "blocked", "duplicate", "dropped"})

# Default `tasks list` visibility: only "open" tickets, matching the shipped default of
# hiding "done". Explicit allow-list (not `state != "open"`) so a newly added state
# (blocked, duplicate, dropped) can never silently leak into the default view the way an
# implicit exclusion list would - `--all` is required to see anything non-open.
_DEFAULT_LIST_STATES = frozenset({"open"})


def _validate_state(state: str) -> str:
    if state not in _VALID_STATES:
        raise ValueError(f"invalid ticket state {state!r}; must be one of {sorted(_VALID_STATES)}")
    return state


# The mutable grooming-import fields shared by `_update_ticket` and `import_tickets`'
# create path. One source of truth so adding a field can't update one writer and
# forget the other (past review finding: these were hand-listed independently in
# three places and could silently drift).
_GROOMING_FIELDS = ("phase", "group", "blocked_reason", "duplicate_of")


def create_ticket(
    title: str,
    body: str = "",
    *,
    state: str = "open",
    phase: str | None = None,
    group: str | None = None,
    blocked_reason: str | None = None,
    duplicate_of: int | None = None,
    source: str | None = None,
    external_id: str | None = None,
) -> dict:
    """Reserve a ticket id and write it exclusively, retrying past collisions.

    Past bug (#8): `_next_id` (scan-then-write) combined with `_atomic_write`
    (unconditional replace) let two concurrent `tasks new` calls compute the
    same next id and one silently clobber the other's ticket file with no
    error. Exclusive create (O_CREAT|O_EXCL) turns that race into a retry
    with a fresh id, and a bounded number of collisions into a clear error
    instead of a silent lost update.

    `phase`/`group`/`blocked_reason`/`duplicate_of`/`source`/`external_id` are additive
    fields (all optional, default null) used by grooming-backlog imports (see
    `import_tickets`); a hand-created `tasks new` ticket simply leaves them null.
    """
    _validate_state(state)
    store = task_dir()
    store.mkdir(parents=True, exist_ok=True)
    last_error: FileExistsError | None = None
    for _ in range(_MAX_CREATE_ATTEMPTS):
        ticket_id = _next_id(store)
        ticket = {
            "id": ticket_id,
            "title": title,
            "body": body,
            "state": state,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": os.environ.get("TASK_SESSION") or "",
            "labels": _labels(),
            "phase": phase,
            "group": group,
            "blocked_reason": blocked_reason,
            "duplicate_of": duplicate_of,
            "source": source,
            "external_id": external_id,
        }
        try:
            _exclusive_write(store / f"{ticket_id}.json", ticket)
        except FileExistsError as exc:
            last_error = exc
            continue
        return ticket
    raise RuntimeError(
        f"could not reserve a harness task id after {_MAX_CREATE_ATTEMPTS} attempts "
        "(concurrent `tasks new` collision)"
    ) from last_error


def list_tickets(include_done: bool = False) -> list[dict]:
    """List tickets.

    Default view is the explicit allow-list `_DEFAULT_LIST_STATES` (open only);
    `include_done=True` (the CLI's `--all`) returns every state, including tickets an
    older, field-less ticket file predates this module's schema for (missing fields
    read back as absent via `.get(...)`, never a crash).
    """
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
        if not include_done and payload.get("state") not in _DEFAULT_LIST_STATES:
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


def _update_ticket(ticket_id: int, item: dict) -> dict | None:
    """Overwrite an existing ticket's mutable fields from an import record in place.

    Never touches `id`/`created_at`/`session`/`labels`/`source`/`external_id` - those
    are the ticket's identity, not import payload.
    """
    path = task_dir() / f"{ticket_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["title"] = item.get("title", "")
    payload["body"] = item.get("body", "")
    payload["state"] = item.get("state", "open")
    for field in _GROOMING_FIELDS:
        payload[field] = item.get(field)
    _atomic_write(path, payload)
    return payload


def import_tickets(items: list[dict]) -> dict:
    """Idempotently upsert tickets from a list of import records.

    Each item is a dict with `title`, `body`, `phase`, `group`, `state`,
    `blocked_reason`, `duplicate_of`, `source`, `external_id`. An item whose
    (`source`, `external_id`) pair matches an existing ticket updates that ticket in
    place (never creates a second one for the same external id); otherwise a new
    ticket is created. Re-running the same import file is therefore a no-op on the
    second pass. Validates every item's shape and `state` before writing anything, so
    a single bad record fails the whole import instead of partially applying it.

    An item with a missing `source` or `external_id` has no dedup identity and is
    always created (never upserted) - re-running such a record duplicates it, so
    every import record MUST carry both. Single-writer assumption: concurrent
    `import_tickets` calls race on the same in-memory dedup snapshot the way
    concurrent `tasks new` calls used to race on ticket ids (see the `create_ticket`
    docstring); this module has no cross-process import lock.
    """
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"import item {index} must be a JSON object, got {type(item).__name__}"
            )
        try:
            _validate_state(item.get("state", "open"))
        except ValueError as exc:
            raise ValueError(
                f"import item {index} (external_id={item.get('external_id')!r}): {exc}"
            ) from exc

    store = task_dir()
    store.mkdir(parents=True, exist_ok=True)
    index_by_key: dict[tuple[str, str], dict] = {}
    for ticket in list_tickets(include_done=True):
        source = ticket.get("source")
        external_id = ticket.get("external_id")
        if source is not None and external_id is not None:
            index_by_key[(source, external_id)] = ticket

    created = 0
    updated = 0
    for item in items:
        source = item.get("source")
        external_id = item.get("external_id")
        has_key = source is not None and external_id is not None
        key = (source, external_id) if has_key else None
        existing = index_by_key.get(key) if key is not None else None
        if existing is not None:
            _update_ticket(existing["id"], item)
            updated += 1
        else:
            new_ticket = create_ticket(
                item.get("title", ""),
                item.get("body", ""),
                state=item.get("state", "open"),
                source=source,
                external_id=external_id,
                **{field: item.get(field) for field in _GROOMING_FIELDS},
            )
            if key is not None:
                index_by_key[key] = new_ticket
            created += 1
    return {"created": created, "updated": updated, "total": len(items)}


def _cmd_new(args: argparse.Namespace) -> int:
    try:
        ticket = create_ticket(args.title, args.body or "")
    except RuntimeError as exc:
        print(_sanitize(str(exc)), file=sys.stderr)
        return 1
    print(_sanitize(str(ticket["title"])))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        raw = Path(args.path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(_sanitize(f"could not read {args.path}: {exc}"), file=sys.stderr)
        return 1
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(_sanitize(f"invalid JSON in {args.path}: {exc}"), file=sys.stderr)
        return 1
    if not isinstance(items, list):
        print(
            _sanitize(f"{args.path} must contain a JSON array of ticket objects"), file=sys.stderr
        )
        return 1
    try:
        summary = import_tickets(items)
    except (ValueError, RuntimeError) as exc:
        print(_sanitize(str(exc)), file=sys.stderr)
        return 1
    print(
        f"{summary['created']} created, {summary['updated']} updated, of {summary['total']} total"
    )
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
    command = args.tasks_command
    if command == "new":
        return _cmd_new(args)
    if command == "list":
        return _cmd_list(args)
    if command == "done":
        return _cmd_done(args)
    if command == "import":
        return _cmd_import(args)
    return 0
