"""Local process-ticket tracker so agent/session work stays off product GitHub.

Accessed via: `harness tasks new|list|done|import` -> cli._run_tasks -> this module.

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
import time
from datetime import datetime, timezone
from pathlib import Path

from harness_cli.report import _sanitize


# `state` is a fixed, validated enum. "open"/"done" are the original two
# values (mark_done -> "done"); "blocked"/"duplicate"/"dropped" were added
# for `tasks import` (grooming backlog ingestion) so a ticket can record why
# it isn't actionable without inventing a free-text status column.
VALID_STATES = frozenset({"open", "done", "blocked", "duplicate", "dropped"})


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


def _reserve_new_ticket(store: Path, fields: dict) -> dict:
    """Reserve a fresh id and write it exclusively, retrying past collisions.

    Shared by `create_ticket` and `import_tickets`'s create path so the id/
    created_at/session/labels scaffold and the O_EXCL retry loop exist in
    exactly one place.

    Past bug (#8): `_next_id` (scan-then-write) combined with `_atomic_write`
    (unconditional replace) let two concurrent `tasks new` calls compute the
    same next id and one silently clobber the other's ticket file with no
    error. Exclusive create (O_CREAT|O_EXCL) turns that race into a retry
    with a fresh id, and a bounded number of collisions into a clear error
    instead of a silent lost update.
    """
    last_error: FileExistsError | None = None
    for _ in range(_MAX_CREATE_ATTEMPTS):
        ticket_id = _next_id(store)
        ticket = {
            "id": ticket_id,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": os.environ.get("TASK_SESSION") or "",
            "labels": _labels(),
            **fields,
        }
        try:
            _exclusive_write(store / f"{ticket_id}.json", ticket)
        except FileExistsError as exc:
            last_error = exc
            continue
        return ticket
    raise RuntimeError(
        f"could not reserve a harness task id after {_MAX_CREATE_ATTEMPTS} attempts"
    ) from last_error


def create_ticket(title: str, body: str = "") -> dict:
    """Reserve a ticket id and write it exclusively, retrying past collisions."""
    store = task_dir()
    store.mkdir(parents=True, exist_ok=True)
    fields = {
        "title": title,
        "body": body,
        "state": "open",
        "phase": None,
        "group": None,
        "blocked_reason": None,
        "duplicate_of": None,
        "source": None,
        "external_id": None,
    }
    try:
        return _reserve_new_ticket(store, fields)
    except RuntimeError as exc:
        raise RuntimeError(
            f"could not reserve a harness task id after {_MAX_CREATE_ATTEMPTS} attempts "
            "(concurrent `tasks new` collision)"
        ) from exc


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


def _index_by_source_external_id(store: Path) -> dict[tuple[str, str], Path]:
    """Scan `store` once and map (source, external_id) -> ticket file path.

    Tickets without both fields set (i.e. everything created by `tasks new`,
    and any hand-edited file) are simply absent from the index and can never
    collide with an import.
    """
    index: dict[tuple[str, str], Path] = {}
    if not store.is_dir():
        return index
    for path in store.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        source = payload.get("source")
        external_id = payload.get("external_id")
        if isinstance(source, str) and source and isinstance(external_id, str) and external_id:
            index[(source, external_id)] = path
    return index


_IMPORT_LOCK_NAME = ".import.lock"
_IMPORT_LOCK_TIMEOUT = 30.0
_IMPORT_LOCK_POLL = 0.1
# A lock file older than this is treated as abandoned (the holder crashed or
# was killed without reaching the `finally` cleanup -- routine in this
# ecosystem's detached-agent workflow) and reclaimed rather than waited out
# forever. Comfortably above any realistic import duration on a local store.
_IMPORT_LOCK_STALE_AFTER = 300.0


def _acquire_import_lock(store: Path, timeout: float | None = None) -> Path:
    """Exclusively claim `store`'s import lock file, waiting up to `timeout` seconds.

    `import_tickets` builds its (source, external_id) dedup index once, then
    upserts every record against that snapshot. Without a store-wide lock,
    two concurrent `tasks import` runs both see a given (source, external_id)
    as absent, both reserve a fresh id via `_exclusive_write` (which only
    guarantees *id* uniqueness, not *key* uniqueness), and both create a
    ticket for it — silently breaking the documented "re-running is a no-op"
    guarantee. Serializing the whole import behind one lock file closes that
    window; a run that can't acquire the lock within `timeout` fails loudly
    instead of racing.

    A lock file older than `_IMPORT_LOCK_STALE_AFTER` is reclaimed rather
    than waited out forever (the holder likely crashed without reaching the
    `finally` cleanup). Reclaiming never blindly `unlink`s the file a
    concurrent reclaimer might have just replaced: it `os.rename`s the stale
    path to a PID-unique name first. POSIX rename on a given source path
    only ever succeeds for one caller -- once it succeeds the source name no
    longer exists, so a second process racing the same reclaim gets
    `FileNotFoundError` and does nothing further, instead of deleting a lock
    a third process already re-created in the meantime.
    """
    if timeout is None:
        timeout = _IMPORT_LOCK_TIMEOUT
    lock_path = store / _IMPORT_LOCK_NAME
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0  # lock vanished between the failed create and stat; just retry
            if age > _IMPORT_LOCK_STALE_AFTER:
                reclaim_path = (
                    store / f"{_IMPORT_LOCK_NAME}.reclaim-{os.getpid()}-{time.monotonic_ns()}"
                )
                try:
                    os.rename(lock_path, reclaim_path)
                except OSError:
                    pass  # another process already reclaimed or refreshed it; just retry
                else:
                    reclaim_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"another `tasks import` appears to be in progress ({lock_path} exists); "
                    "remove it manually if a previous run crashed without cleaning up"
                ) from None
            time.sleep(_IMPORT_LOCK_POLL)


def _validate_import_records(records: list[dict]) -> None:
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"import record must be an object, got {record!r}")
        source = record.get("source")
        external_id = record.get("external_id")
        if not isinstance(source, str) or not source:
            raise ValueError(
                f"import record missing non-empty 'source' (external_id={external_id!r})"
            )
        if not isinstance(external_id, str) or not external_id:
            raise ValueError(f"import record missing non-empty 'external_id' (source={source!r})")
        state = record.get("state", "open")
        if state not in VALID_STATES:
            raise ValueError(
                f"invalid state {state!r} for source={source!r} external_id={external_id!r} "
                f"(must be one of {sorted(VALID_STATES)})"
            )
        for field_name in ("title", "body"):
            value = record.get(field_name, "")
            if not isinstance(value, str):
                raise ValueError(
                    f"invalid {field_name!r} for source={source!r} external_id={external_id!r}: "
                    f"must be a string, got {value!r}"
                )
        for field_name in ("phase", "group", "blocked_reason"):
            value = record.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"invalid {field_name!r} for source={source!r} external_id={external_id!r}: "
                    f"must be a string or null, got {value!r}"
                )
        dup_ext = record.get("duplicate_of_external_id")
        if dup_ext is not None and (not isinstance(dup_ext, str) or not dup_ext):
            raise ValueError(
                f"invalid 'duplicate_of_external_id' for source={source!r} external_id={external_id!r}: "
                f"must be a non-empty string or null, got {dup_ext!r}"
            )
        if dup_ext is not None and dup_ext == external_id:
            raise ValueError(
                f"duplicate_of_external_id for source={source!r} external_id={external_id!r} "
                "cannot reference itself"
            )


def _validate_duplicate_targets(records: list[dict], index: dict[tuple[str, str], Path]) -> None:
    """`duplicate_of_external_id` must resolve before any record is written.

    A target is valid if it already exists in the store, or is the
    `external_id` of another record in this same batch (same `source`) —
    covering forward references, since records are processed top to bottom.
    """
    batch_keys = {(r["source"], r["external_id"]) for r in records}
    for record in records:
        target_external_id = record.get("duplicate_of_external_id")
        if target_external_id is None:
            continue
        key = (record["source"], target_external_id)
        if key not in index and key not in batch_keys:
            raise ValueError(
                f"duplicate_of_external_id={target_external_id!r} for source={record['source']!r} "
                f"external_id={record['external_id']!r} does not match any existing or imported ticket"
            )


def _resolve_duplicate_targets(store: Path, records: list[dict]) -> None:
    """Rewrite `duplicate_of` (int) from each record's `duplicate_of_external_id`.

    Runs after every record in the batch has been upserted (so forward
    references are already on disk), using a fresh full-store scan so a
    target created earlier in this same batch, or in a previous import run,
    both resolve to the real internal ticket id -- never the bare grooming
    item number, which lives in a different, coincidentally-overlapping
    namespace.
    """
    index = _index_by_source_external_id(store)
    for record in records:
        target_external_id = record.get("duplicate_of_external_id")
        if target_external_id is None:
            continue
        own_path = index[(record["source"], record["external_id"])]
        target_path = index[(record["source"], target_external_id)]
        target_id = json.loads(target_path.read_text(encoding="utf-8")).get("id")
        own_payload = json.loads(own_path.read_text(encoding="utf-8"))
        if own_payload.get("duplicate_of") != target_id:
            own_payload["duplicate_of"] = target_id
            _atomic_write(own_path, own_payload)


def import_tickets(records: list[dict]) -> tuple[int, int]:
    """Idempotently upsert `records` into the local ticket store.

    Matching key is (source, external_id): a record whose (source, external_id)
    already exists in the store is updated in place (same ticket id, same
    created_at); anything else is created fresh. Re-running the same input
    twice is a no-op the second time (0 created, N updated with identical
    values). Every record must carry a non-empty `source` and `external_id`,
    and a `state` from `VALID_STATES` (defaults to "open" if omitted); an
    optional `duplicate_of_external_id` (another record's `external_id`,
    same `source`) is resolved to the real internal ticket id and stored as
    `duplicate_of`.

    All *structural* validation (types, the state enum, and every
    `duplicate_of_external_id` resolving to a real target) runs before any
    file is written, so a malformed record anywhere in the batch never
    leaves a half-applied import. A mid-batch I/O failure while writing
    (disk full, permissions changing under us) is not rolled back -- records
    processed earlier in the same run stay committed.

    The whole run is serialized behind a store-wide lock (see
    `_acquire_import_lock`) so two concurrent imports can't both decide the
    same (source, external_id) is absent and create a duplicate ticket.
    """
    _validate_import_records(records)

    store = task_dir()
    store.mkdir(parents=True, exist_ok=True)
    lock_path = _acquire_import_lock(store)
    try:
        index = _index_by_source_external_id(store)
        _validate_duplicate_targets(records, index)

        created = 0
        updated = 0
        for record in records:
            source = record["source"]
            external_id = record["external_id"]
            key = (source, external_id)
            fields = {
                "title": record.get("title", ""),
                "body": record.get("body", ""),
                "state": record.get("state", "open"),
                "phase": record.get("phase"),
                "group": record.get("group"),
                "blocked_reason": record.get("blocked_reason"),
                "duplicate_of": None,
                "source": source,
                "external_id": external_id,
            }
            existing_path = index.get(key)
            if existing_path is not None:
                try:
                    payload = json.loads(existing_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"existing ticket file for source={source!r} external_id={external_id!r} "
                        f"is unreadable: {exc}"
                    ) from exc
                payload.update(fields)
                _atomic_write(existing_path, payload)
                updated += 1
                continue

            ticket = _reserve_new_ticket(store, fields)
            index[key] = store / f"{ticket['id']}.json"
            created += 1

        _resolve_duplicate_targets(store, records)
    finally:
        lock_path.unlink(missing_ok=True)

    return created, updated


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
    try:
        ticket = create_ticket(args.title, args.body or "")
    except RuntimeError as exc:
        print(_sanitize(str(exc)), file=sys.stderr)
        return 1
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


def _cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(_sanitize(f"cannot read {path}: {exc}"), file=sys.stderr)
        return 1
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(_sanitize(f"invalid JSON in {path}: {exc}"), file=sys.stderr)
        return 1
    if not isinstance(records, list):
        print(_sanitize(f"{path} must contain a JSON array of ticket records"), file=sys.stderr)
        return 1
    try:
        created, updated = import_tickets(records)
    except (ValueError, RuntimeError, OSError) as exc:
        print(_sanitize(str(exc)), file=sys.stderr)
        return 1
    total = created + updated
    print(f"{created} created / {updated} updated / {total} total")
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
