"""Safe Codex updater with rollback after bounded health probes.

Accessed via: `harness codex update` -> cli._run_codex -> this module's `run`.

Codex is usually installed outside any managed checkout, most commonly as the Homebrew cask.
This module wraps that external update path: preserve the currently working binary, run the
updater, probe the candidate (`--version`, `--help`, `completion zsh`, each with its own
timeout), and restore the last known good binary when the candidate hangs or fails before
producing usable output.

Assumptions: the selected `codex` is healthy before the update (an unhealthy one is refused,
never "updated"); the updater replaces the binary in place or retargets its symlink chain.
Backups live under $HARNESS_CODEX_BACKUP_DIR, else $XDG_STATE_HOME/harness-cli/codex-backups,
else ~/.local/state/harness-cli/codex-backups (see `default_backup_dir`).

History: moved here from rig-cli's `rig codex update` (riglib/codex_update.py, backups under
~/.cache/rig/codex-backups) — updating/probing an agent harness binary is harness-cli's concern,
not the dev-environment reconciler's. Born from agent-tools#252 ("Codex updater must verify new
binary and roll back on hang"): a Homebrew cask update must never strand the machine without a
working codex.

Exit codes (stable, kept from `rig codex update` so scripts port unchanged): 0 updated,
2 invalid --probe-timeout, 8 update failed or rolled back, 127 codex or updater missing.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

DEFAULT_PROBE_TIMEOUT_S = 5.0
DEFAULT_UPDATE_TIMEOUT_S = 600.0

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UPDATE_FAILED = 8
EXIT_MISSING = 127


@dataclass(frozen=True)
class ProbeStep:
    name: str
    args: tuple[str, ...]


PROBE_STEPS: tuple[ProbeStep, ...] = (
    ProbeStep("version", ("--version",)),
    ProbeStep("help", ("--help",)),
    ProbeStep("completion", ("completion", "zsh")),
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def combined(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    version: str | None
    message: str


@dataclass(frozen=True)
class CodexSnapshot:
    codex_path: Path
    resolved_path: Path
    backup_path: Path
    was_symlink: bool
    symlink_target: str | None = None
    symlink_chain: tuple[tuple[Path, str], ...] = ()


@dataclass(frozen=True)
class UpdateResult:
    status: str
    message: str
    previous_version: str | None = None
    candidate_version: str | None = None
    backup_path: Path | None = None
    exit_code: int = EXIT_UPDATE_FAILED


def default_backup_dir() -> Path:
    """Where last-known-good binaries go; evaluated per call so env overrides apply."""
    explicit = os.environ.get("HARNESS_CODEX_BACKUP_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "harness-cli" / "codex-backups"
    return Path.home() / ".local" / "state" / "harness-cli" / "codex-backups"


def default_update_command(codex_path: Path) -> list[str]:
    """The default updater, biased toward the Homebrew cask incident this guard was added for."""
    brew = shutil.which("brew")
    if brew and _path_is_homebrew_cask_codex(codex_path, Path(brew)):
        return [brew, "upgrade", "--cask", "codex"]
    return [str(codex_path), "update"]


def find_codex_path(path: str | Path | None = None) -> Path:
    """Resolve the Codex executable path without mutating the live install."""
    if path is not None:
        explicit = Path(path).expanduser()
        if not explicit.exists():
            raise FileNotFoundError(f"codex path does not exist: {explicit}")
        if not os.access(explicit, os.X_OK):
            raise FileNotFoundError(f"codex path is not executable: {explicit}")
        return explicit
    found = shutil.which("codex")
    if not found:
        raise FileNotFoundError("codex is not on PATH; pass --path to the Codex binary")
    return Path(found)


def probe_codex(
    codex_path: str | Path, *, timeout_s: float = DEFAULT_PROBE_TIMEOUT_S
) -> ProbeResult:
    """Run version/help/completion probes, each with its own timeout."""
    path = Path(codex_path)
    version: str | None = None
    for step in PROBE_STEPS:
        cmd = [str(path), *step.args]
        res = run_bounded(cmd, timeout_s=timeout_s)
        if res.timed_out:
            return ProbeResult(False, version, f"{step.name} probe timed out after {timeout_s:g}s")
        if res.returncode != 0:
            tail = _tail(res.combined)
            return ProbeResult(False, version, f"{step.name} probe exited {res.returncode}: {tail}")
        output = res.combined.strip()
        if not output:
            return ProbeResult(False, version, f"{step.name} probe produced no output")
        if step.name == "version":
            version = output.splitlines()[0].strip()
    return ProbeResult(True, version, f"probes passed ({version or 'version unknown'})")


def safe_update(
    *,
    codex_path: str | Path | None = None,
    update_command: list[str] | None = None,
    backup_dir: str | Path | None = None,
    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
) -> UpdateResult:
    """Run a Codex update command and roll back if the candidate fails bounded probes.

    Raises ``FileNotFoundError`` when the selected Codex binary cannot be found or executed.
    """
    if probe_timeout_s <= 0 or not math.isfinite(probe_timeout_s):
        raise ValueError("probe_timeout_s must be positive")
    path = find_codex_path(codex_path)
    before = probe_codex(path, timeout_s=probe_timeout_s)
    if not before.ok:
        return UpdateResult(
            "error", f"current codex is not healthy; update refused: {before.message}"
        )

    try:
        snapshot = snapshot_current_codex(
            path,
            backup_dir=backup_dir if backup_dir is not None else default_backup_dir(),
            version=before.version,
        )
    except OSError as exc:
        return UpdateResult("error", f"could not back up current codex: {exc}")
    command = update_command or default_update_command(path)
    update_timeout_s = _update_timeout_s()
    update = run_bounded(command, timeout_s=update_timeout_s)
    rollback_reason: str | None = None
    exit_code = EXIT_UPDATE_FAILED
    if update.timed_out:
        rollback_reason = f"update command timed out after {update_timeout_s:g}s"
    elif update.returncode != 0:
        rollback_reason = f"update command exited {update.returncode}: {_tail(update.combined)}"
        if update.returncode == EXIT_MISSING:
            exit_code = EXIT_MISSING
    if rollback_reason is not None:
        return _rollback_result(
            rollback_reason,
            restore_snapshot(snapshot),
            snapshot,
            probe_timeout_s=probe_timeout_s,
            previous_version=before.version,
            exit_code=exit_code,
        )

    candidate = probe_codex(path, timeout_s=probe_timeout_s)
    if candidate.ok:
        return UpdateResult(
            "updated",
            f"codex healthy after update: {candidate.version or 'version unknown'}",
            previous_version=before.version,
            candidate_version=candidate.version,
            backup_path=snapshot.backup_path,
            exit_code=EXIT_OK,
        )

    return _rollback_result(
        f"candidate failed: {candidate.message}",
        restore_snapshot(snapshot),
        snapshot,
        probe_timeout_s=probe_timeout_s,
        previous_version=before.version,
        candidate_version=candidate.version,
    )


def snapshot_current_codex(
    codex_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
    version: str | None = None,
) -> CodexSnapshot:
    """Copy the current resolved Codex binary to a rollback-owned backup path."""
    path = Path(codex_path)
    resolved = path.resolve(strict=True)
    target_dir = Path(backup_dir if backup_dir is not None else default_backup_dir()).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = _slug(version or "unknown")
    backup = target_dir / f"codex-last-good-{suffix}-{stamp}"
    shutil.copy2(resolved, backup)
    symlink_chain = _read_symlink_chain(path)
    was_symlink = bool(symlink_chain)
    symlink_target = symlink_chain[0][1] if symlink_chain else None
    return CodexSnapshot(
        codex_path=path,
        resolved_path=resolved,
        backup_path=backup,
        was_symlink=was_symlink,
        symlink_target=symlink_target,
        symlink_chain=symlink_chain,
    )


def restore_snapshot(snapshot: CodexSnapshot) -> str | None:
    """Restore the executable path to the saved good binary; return an error string on failure."""
    try:
        snapshot.codex_path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.was_symlink:
            _restore_symlink_target(snapshot)
        else:
            _restore_binary_at_path(snapshot.backup_path, snapshot.codex_path)
        return None
    except OSError as exc:
        return str(exc)


def run(args: argparse.Namespace) -> int:
    """`harness codex update` handler: thread CLI args into `safe_update` and report."""
    command = list(getattr(args, "update_command", []) or [])
    if command and command[0] == "--":
        command = command[1:]
    if args.probe_timeout is not None and (
        args.probe_timeout <= 0 or not math.isfinite(args.probe_timeout)
    ):
        print("codex update: --probe-timeout must be positive")
        return EXIT_USAGE
    try:
        result = safe_update(
            codex_path=args.path,
            update_command=command or None,
            backup_dir=args.backup_dir,
            probe_timeout_s=(
                args.probe_timeout if args.probe_timeout is not None else DEFAULT_PROBE_TIMEOUT_S
            ),
        )
    except FileNotFoundError as exc:
        print(f"codex update: {exc}")
        return EXIT_MISSING

    label = {"updated": "updated", "rolled_back": "rolled back", "error": "error"}
    print(f"codex update: {label.get(result.status, result.status)}: {result.message}")
    if result.backup_path is not None:
        print(f"last-known-good backup: {result.backup_path}")
    return result.exit_code


def run_bounded(cmd: list[str], *, timeout_s: float) -> CommandResult:
    """Run ``cmd`` with a timeout and kill its process group on hangs."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandResult(EXIT_MISSING, "", str(exc))
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return CommandResult(proc.returncode, out or "", err or "")
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            out, err = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            _close_pipe(proc.stdout)
            _close_pipe(proc.stderr)
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return CommandResult(124, "", "", True)
        code = proc.returncode if proc.returncode is not None else 124
        return CommandResult(code, out or "", err or "", True)


def _rollback_result(
    reason: str,
    rollback_error: str | None,
    snapshot: CodexSnapshot,
    *,
    probe_timeout_s: float,
    previous_version: str | None,
    candidate_version: str | None = None,
    exit_code: int = EXIT_UPDATE_FAILED,
) -> UpdateResult:
    if rollback_error is not None:
        return UpdateResult(
            "error",
            _rollback_message(reason, rollback_error, snapshot),
            previous_version=previous_version,
            candidate_version=candidate_version,
            backup_path=snapshot.backup_path,
        )
    restored = probe_codex(snapshot.codex_path, timeout_s=probe_timeout_s)
    if not restored.ok:
        return UpdateResult(
            "error",
            (
                f"{reason}; rollback restored files but codex is still unhealthy: "
                f"{restored.message}; backup at {snapshot.backup_path}"
            ),
            previous_version=previous_version,
            candidate_version=candidate_version,
            backup_path=snapshot.backup_path,
        )
    return UpdateResult(
        "rolled_back",
        _rollback_message(reason, None, snapshot),
        previous_version=previous_version,
        candidate_version=candidate_version,
        backup_path=snapshot.backup_path,
        exit_code=exit_code,
    )


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    # os.killpg/SIGKILL are POSIX-only; on Windows fall back to killing the direct child.
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        proc.kill()
        return
    try:
        killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _close_pipe(pipe: IO[str] | None) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except OSError:
        pass


def _restore_symlink_target(snapshot: CodexSnapshot) -> None:
    """Restore bytes to the resolved target and recreate the original symlink chain."""
    if not snapshot.symlink_chain:
        raise OSError("snapshot is missing original symlink chain")
    snapshot.resolved_path.parent.mkdir(parents=True, exist_ok=True)
    _restore_binary_at_path(snapshot.backup_path, snapshot.resolved_path)
    for link, target in reversed(snapshot.symlink_chain):
        _replace_symlink(link, target)


def _read_symlink_chain(path: Path) -> tuple[tuple[Path, str], ...]:
    chain: list[tuple[Path, str]] = []
    current = path
    seen: set[Path] = set()
    while current.is_symlink():
        if current in seen:
            raise OSError(f"symlink loop while snapshotting codex path: {path}")
        seen.add(current)
        target = os.readlink(current)
        chain.append((current, target))
        next_path = Path(target)
        if not next_path.is_absolute():
            next_path = current.parent / next_path
        current = next_path
    return tuple(chain)


def _restore_binary_at_path(backup_path: Path, target: Path) -> None:
    tmp = target.parent / f".{target.name}.harness-codex-rollback-{os.getpid()}"
    try:
        shutil.copy2(backup_path, tmp)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _replace_symlink(link: Path, target: str | None) -> None:
    if target is None:
        raise OSError("snapshot is missing original symlink target")
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.parent / f".{link.name}.harness-codex-rollback-link-{os.getpid()}"
    try:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(target)
        os.replace(tmp, link)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _path_is_homebrew_cask_codex(codex_path: Path, brew_path: Path) -> bool:
    """True when the selected Codex binary is the Homebrew Codex cask."""
    try:
        brew_prefix = _homebrew_prefix(brew_path)
        caskroom = brew_prefix / "Caskroom" / "codex"
        candidates = (codex_path.resolve(), codex_path.absolute())
        under_caskroom = any(path == caskroom or caskroom in path.parents for path in candidates)
        return under_caskroom and _brew_cask_codex_installed(brew_path)
    except OSError:
        return False


def _homebrew_prefix(brew_path: Path) -> Path:
    res = run_bounded([str(brew_path), "--prefix"], timeout_s=10)
    text = res.combined.strip()
    prefix = text.splitlines()[0].strip() if res.returncode == 0 and text else ""
    if prefix:
        return Path(prefix).expanduser().resolve()
    return brew_path.resolve().parent.parent


def _brew_cask_codex_installed(brew_path: Path) -> bool:
    res = run_bounded([str(brew_path), "list", "--cask", "codex"], timeout_s=30)
    return res.returncode == 0


def _update_timeout_s() -> float:
    raw = os.environ.get("HARNESS_CODEX_UPDATE_TIMEOUT_S")
    if not raw:
        return DEFAULT_UPDATE_TIMEOUT_S
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_UPDATE_TIMEOUT_S
    return parsed if parsed > 0 and math.isfinite(parsed) else DEFAULT_UPDATE_TIMEOUT_S


def _rollback_message(reason: str, rollback_error: str | None, snapshot: CodexSnapshot) -> str:
    if rollback_error is not None:
        return f"{reason}; rollback FAILED: {rollback_error}; backup at {snapshot.backup_path}"
    return (
        f"{reason}; rolled back {snapshot.codex_path} to last known good backup "
        f"{snapshot.backup_path}"
    )


def _tail(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "(no output)"
    line = stripped.splitlines()[-1].strip()
    return line[:500]


def _slug(value: str) -> str:
    chars = []
    for ch in value:
        if ch.isalnum() or ch in ".-_":
            chars.append(ch)
        elif ch.isspace():
            chars.append("-")
    return "".join(chars).strip("-")[:80] or "unknown"
