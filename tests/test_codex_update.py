"""`harness codex update` — safe Codex updater with rollback after bounded probes.

Ported from rig-cli (tests/test_tools.py "Codex safe update" + tests/test_cli.py codex tests)
when the command moved from `rig codex update` to harness-cli.

Hermetic: every fake codex/updater/brew lives under tmp_path; no test runs the real codex,
brew, or any installer. The rollback tests exercise real process spawns of POSIX shell-script
fakes and symlink chains, so they are POSIX-only (CI runs on Linux); the CLI threading,
missing-updater, backup-dir, and timeout tests run everywhere.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from harness_cli import codex_update
from harness_cli.cli import main

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell-script fakes, symlink chains, and process groups",
)

# A HANG guard only — the fake binaries answer instantly, so any value works for a healthy run.
# It is generous on purpose: a sub-second budget races three process spawns against machine load
# and lost whenever the suite ran next to other work (a load-dependent test is a bug, not a flake).
_PROBE_TIMEOUT_S = 30.0
# A hanging fake carries this sentinel; the ``instant_hang`` fixture makes its probe time out at
# once, so one generous budget serves the healthy binaries and the hang never waits it out.
_HANG_SENTINEL = "# harness-test: hangs forever"


@pytest.fixture(autouse=True)
def _hermetic_backup_dir(tmp_path, monkeypatch):
    """No test may fall through to the real ~/.local/state backup dir."""
    monkeypatch.delenv("HARNESS_CODEX_BACKUP_DIR", raising=False)
    monkeypatch.delenv("HARNESS_CODEX_UPDATE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def _write_fake_codex(path: Path, version: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \"${1:-}\" in\n"
        f"  --version) echo 'codex-cli {version}' ;;\n"
        "  --help) echo 'Codex CLI help' ;;\n"
        "  completion) echo '#compdef codex' ;;\n"
        "  *) echo 'ok' ;;\n"
        "esac\n"
    )
    path.chmod(0o755)


def _write_hanging_codex(path: Path) -> None:
    path.write_text(f"#!/bin/sh\n{_HANG_SENTINEL}\nsleep 30\n")
    path.chmod(0o755)


@pytest.fixture
def instant_hang(monkeypatch):
    """A hanging fake times out INSTANTLY instead of sleeping out the probe budget.

    Every healthy binary stays on the REAL spawn path (``run_bounded``) under the hang-guard budget;
    only a binary carrying :data:`_HANG_SENTINEL` (symlinks followed) short-circuits to a timed-out
    result. The kill-on-timeout mechanics keep their own tests (``test_run_bounded_*``).
    """
    real = codex_update.run_bounded

    def run_or_hang(cmd, *, timeout_s):
        # the sentinel is matched on the line right after the shebang ONLY: an update script
        # that writes a hanging fake via a heredoc carries the same text further down and must
        # still run for real (else the update never happens and the rollback passes vacuously)
        try:
            lines = Path(cmd[0]).read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        hangs = len(lines) > 1 and lines[1] == _HANG_SENTINEL
        if hangs:
            return codex_update.CommandResult(-9, "", "", timed_out=True)
        return real(cmd, timeout_s=timeout_s)

    monkeypatch.setattr(codex_update, "run_bounded", run_or_hang)


def _write_failing_codex(path: Path) -> None:
    path.write_text("#!/bin/sh\necho 'broken candidate' >&2\nexit 42\n")
    path.chmod(0o755)


def _write_marker_dependent_codex(path: Path, marker: Path, version: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f'test -f "{marker}" || exit 77\n'
        "case \"${1:-}\" in\n"
        f"  --version) echo 'codex-cli {version}' ;;\n"
        "  --help) echo 'Codex CLI help' ;;\n"
        "  completion) echo '#compdef codex' ;;\n"
        "  *) echo 'ok' ;;\n"
        "esac\n"
    )
    path.chmod(0o755)


def _write_update_script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(0o755)
    return path


def _update(live: Path, update: Path, backup_dir: Path) -> codex_update.UpdateResult:
    return codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=backup_dir,
        probe_timeout_s=_PROBE_TIMEOUT_S,
    )


def _healthy(path: Path) -> bool:
    return codex_update.probe_codex(path, timeout_s=_PROBE_TIMEOUT_S).ok


# ── CLI ───────────────────────────────────────────────────────────────────────────
def test_codex_without_subcommand_prints_help(capsys):
    rc = main(["codex"])

    assert rc == 0
    assert "update" in capsys.readouterr().out


def test_codex_update_cli_threads_args(capsys, monkeypatch):
    captured = {}

    def fake_safe_update(**kwargs):
        captured.update(kwargs)
        return codex_update.UpdateResult(
            "updated",
            "codex healthy after update: codex-cli test",
            backup_path=Path("/tmp/codex-backup"),
            exit_code=0,
        )

    monkeypatch.setattr(codex_update, "safe_update", fake_safe_update)

    rc = main(
        [
            "codex",
            "update",
            "--path",
            "/tmp/codex",
            "--backup-dir",
            "/tmp/backups",
            "--probe-timeout",
            "1.25",
            "--",
            "brew",
            "reinstall",
            "--cask",
            "codex",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "codex update: updated" in out
    assert "last-known-good backup:" in out
    assert captured == {
        "codex_path": "/tmp/codex",
        "update_command": ["brew", "reinstall", "--cask", "codex"],
        "backup_dir": "/tmp/backups",
        "probe_timeout_s": 1.25,
    }


def test_codex_update_cli_defaults_probe_timeout_and_updater(monkeypatch):
    captured = {}

    def fake_safe_update(**kwargs):
        captured.update(kwargs)
        return codex_update.UpdateResult("updated", "ok", exit_code=0)

    monkeypatch.setattr(codex_update, "safe_update", fake_safe_update)

    assert main(["codex", "update"]) == 0
    assert captured == {
        "codex_path": None,
        "update_command": None,
        "backup_dir": None,
        "probe_timeout_s": codex_update.DEFAULT_PROBE_TIMEOUT_S,
    }


def test_codex_update_cli_returns_nonzero_after_rollback(capsys, monkeypatch):
    monkeypatch.setattr(
        codex_update,
        "safe_update",
        lambda **kwargs: codex_update.UpdateResult(
            "rolled_back",
            "candidate failed",
            exit_code=codex_update.EXIT_UPDATE_FAILED,
        ),
    )

    rc = main(["codex", "update", "--path", "/tmp/codex"])

    out = capsys.readouterr().out
    assert rc == codex_update.EXIT_UPDATE_FAILED == 8
    assert "rolled back" in out


def _stub_healthy_probe(monkeypatch) -> None:
    # The subject is the updater's exit code, not the health probe of the live binary: a real
    # fake under a short probe timeout is load-dependent (three spawns race the timer).
    monkeypatch.setattr(
        codex_update,
        "probe_codex",
        lambda path, *, timeout_s: codex_update.ProbeResult(True, "codex-cli test", "probes passed"),
    )


def _write_live(tmp_path: Path) -> Path:
    live = tmp_path / "codex"
    live.write_text("#!/usr/bin/env bash\nexit 0\n")
    live.chmod(0o755)
    return live


def test_codex_update_cli_missing_updater_returns_127(tmp_path, capsys, monkeypatch):
    live = _write_live(tmp_path)
    _stub_healthy_probe(monkeypatch)
    missing = tmp_path / "missing-updater"

    rc = main(
        [
            "codex",
            "update",
            "--path",
            str(live),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--probe-timeout",
            "0.5",
            "--",
            str(missing),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 127
    assert "update command exited 127" in out


def test_codex_update_cli_missing_codex_returns_127(tmp_path, capsys):
    rc = main(["codex", "update", "--path", str(tmp_path / "no-such-codex")])

    out = capsys.readouterr().out
    assert rc == 127
    assert "codex path does not exist" in out


def test_codex_update_cli_rejects_nonpositive_probe_timeout(capsys):
    for value in ("0", "-1", "nan", "inf"):
        rc = main(["codex", "update", "--probe-timeout", value])

        out = capsys.readouterr().out
        assert rc == 2
        assert "--probe-timeout must be positive" in out


# ── backup location ───────────────────────────────────────────────────────────────
def test_default_backup_dir_prefers_explicit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_BACKUP_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert codex_update.default_backup_dir() == tmp_path / "explicit"


def test_default_backup_dir_uses_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_CODEX_BACKUP_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert codex_update.default_backup_dir() == tmp_path / "state" / "harness-cli" / "codex-backups"


def test_default_backup_dir_falls_back_to_local_state(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_CODEX_BACKUP_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    expected = tmp_path / ".local" / "state" / "harness-cli" / "codex-backups"
    assert codex_update.default_backup_dir() == expected


def test_backup_lands_in_harness_state_dir_by_default(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.delenv("HARNESS_CODEX_BACKUP_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    live = _write_live(tmp_path)
    _stub_healthy_probe(monkeypatch)

    rc = main(["codex", "update", "--path", str(live), "--", str(tmp_path / "missing-updater")])

    backups = list((state / "harness-cli" / "codex-backups").iterdir())
    assert rc == 127
    assert len(backups) == 1
    assert backups[0].name.startswith("codex-last-good-codex-cli-test-")
    assert str(backups[0]) in capsys.readouterr().out


# ── bounded runs ──────────────────────────────────────────────────────────────────
def test_run_bounded_kills_a_hanging_child(tmp_path):
    started = time.monotonic()

    result = codex_update.run_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5
    )

    assert result.timed_out
    assert time.monotonic() - started < 15


def test_run_bounded_missing_binary_is_127(tmp_path):
    result = codex_update.run_bounded([str(tmp_path / "nope")], timeout_s=5)

    assert result.returncode == 127
    assert not result.timed_out


@posix_only
def test_run_bounded_returns_after_timeout_with_detached_pipe_holder(tmp_path):
    script = tmp_path / "detached-pipe-holder.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 -c 'import os, time; os.setsid(); time.sleep(2)' &\n"
        "sleep 30\n"
    )
    script.chmod(0o755)

    result = codex_update.run_bounded([str(script)], timeout_s=0.1)

    assert result.timed_out


# ── safe_update: rollback paths (real spawns of POSIX fakes) ──────────────────────
@posix_only
def test_safe_codex_update_rolls_back_hanging_candidate(tmp_path, instant_hang):
    good = tmp_path / "codex-good"
    hanging = tmp_path / "codex-hangs"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    live.symlink_to(good)
    update = _write_update_script(tmp_path / "replace-with-hang.sh", f"ln -sfn {hanging} {live}\n")

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert result.exit_code == codex_update.EXIT_UPDATE_FAILED
    assert live.is_symlink()
    assert live.resolve() == good
    assert _healthy(live)
    assert "timed out" in result.message


@posix_only
def test_safe_codex_update_rolls_back_nonzero_candidate(tmp_path):
    good = tmp_path / "codex-good"
    failing = tmp_path / "codex-fails"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_failing_codex(failing)
    live.symlink_to(good)
    update = _write_update_script(
        tmp_path / "replace-with-failing.sh", f"ln -sfn {failing} {live}\n"
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert live.resolve() == good
    assert "exited 42" in result.message


@posix_only
def test_safe_codex_update_rolls_back_failing_updater(tmp_path):
    good = tmp_path / "codex-good"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    live.symlink_to(good)
    update = _write_update_script(
        tmp_path / "half-update.sh", f"rm -f {live}\necho 'download failed' >&2\nexit 3\n"
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert result.exit_code == codex_update.EXIT_UPDATE_FAILED
    assert "update command exited 3: download failed" in result.message
    assert live.resolve() == good
    assert _healthy(live)


@posix_only
def test_safe_codex_update_restores_missing_original_symlink_target(tmp_path, instant_hang):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    hanging = tmp_path / "caskroom" / "0.144.1" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    hanging.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    live.symlink_to(good)
    update = _write_update_script(
        tmp_path / "remove-old-and-replace-with-hang.sh",
        f"rm -f {good}\nln -sfn {hanging} {live}\n",
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert live.resolve() == good
    assert good.is_file()
    assert _healthy(live)


@posix_only
def test_safe_codex_update_preserves_intermediate_symlink_on_rollback(tmp_path, instant_hang):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    hanging = tmp_path / "caskroom" / "0.144.1" / "codex"
    shim = tmp_path / "opt" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    hanging.parent.mkdir(parents=True)
    shim.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    shim.symlink_to(good)
    live.symlink_to(shim)
    update = _write_update_script(
        tmp_path / "remove-old-and-replace-top-link-with-hang.sh",
        f"rm -f {good}\nln -sfn {hanging} {live}\n",
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert live.is_symlink()
    assert live.readlink() == shim
    assert shim.is_symlink()
    assert shim.readlink() == good
    assert good.is_file()
    assert _healthy(live)


@posix_only
def test_safe_codex_update_restores_retargeted_intermediate_symlink(tmp_path, instant_hang):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    hanging = tmp_path / "caskroom" / "0.144.1" / "codex"
    shim = tmp_path / "opt" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    hanging.parent.mkdir(parents=True)
    shim.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    shim.symlink_to(good)
    live.symlink_to(shim)
    update = _write_update_script(
        tmp_path / "retarget-intermediate-to-hang.sh", f"ln -sfn {hanging} {shim}\n"
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert live.is_symlink()
    assert live.readlink() == shim
    assert shim.is_symlink()
    assert shim.readlink() == good
    assert live.resolve() == good
    assert _healthy(live)


@posix_only
def test_safe_codex_update_restores_overwritten_original_symlink_target(tmp_path, instant_hang):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    live.symlink_to(good)
    update = _write_update_script(
        tmp_path / "overwrite-old-target-with-hang.sh",
        f"cat > {good} <<'SH'\n#!/usr/bin/env bash\n{_HANG_SENTINEL}\nsleep 30\nSH\n"
        f"chmod +x {good}\n",
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert live.resolve() == good
    assert _healthy(live)
    assert "codex-cli 0.142.4" in good.read_text(encoding="utf-8")


@posix_only
def test_safe_codex_update_rolls_back_plain_binary(tmp_path, instant_hang):
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(live, "0.142.4")
    live.chmod(0o700)
    update = _write_update_script(
        tmp_path / "overwrite-plain-binary-with-hang.sh",
        f"cat > {live} <<'SH'\n#!/usr/bin/env bash\n{_HANG_SENTINEL}\nsleep 30\nSH\n"
        f"chmod +x {live}\n",
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "rolled_back", result.message
    assert not live.is_symlink()
    assert _healthy(live)
    assert live.stat().st_mode & 0o777 == 0o700
    assert result.backup_path is not None
    assert result.backup_path.stat().st_mode & 0o777 == 0o700


@posix_only
def test_safe_codex_update_errors_when_restored_codex_is_unhealthy(tmp_path):
    marker = tmp_path / "runtime-marker"
    good = tmp_path / "codex-good"
    failing = tmp_path / "codex-failing"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    marker.write_text("present", encoding="utf-8")
    _write_marker_dependent_codex(good, marker, "0.142.4")
    _write_failing_codex(failing)
    live.symlink_to(good)
    update = _write_update_script(
        tmp_path / "break-runtime-and-replace.sh", f"rm -f {marker}\nln -sfn {failing} {live}\n"
    )

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "error"
    assert "rollback restored files but codex is still unhealthy" in result.message
    assert result.exit_code == codex_update.EXIT_UPDATE_FAILED


def test_safe_codex_update_reports_current_unhealthy_before_update(tmp_path, instant_hang):
    live = tmp_path / "bin" / "codex"
    marker = tmp_path / "update-ran"
    live.parent.mkdir()
    _write_hanging_codex(live)
    update = _write_update_script(tmp_path / "update.sh", f"printf x > {marker}\n")

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "error"
    assert result.exit_code == codex_update.EXIT_UPDATE_FAILED
    assert "current codex is not healthy" in result.message
    assert not marker.exists()


@posix_only
def test_safe_codex_update_reports_rollback_failure(tmp_path, monkeypatch):
    good = tmp_path / "codex-good"
    failing = tmp_path / "codex-fails"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_failing_codex(failing)
    live.symlink_to(good)
    update = _write_update_script(
        tmp_path / "replace-with-failing.sh", f"ln -sfn {failing} {live}\n"
    )
    monkeypatch.setattr(codex_update, "restore_snapshot", lambda snapshot: "permission denied")

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "error"
    assert result.exit_code == codex_update.EXIT_UPDATE_FAILED
    assert "rollback FAILED: permission denied" in result.message


@posix_only
def test_safe_codex_update_reports_backup_failure_before_update(tmp_path):
    live = tmp_path / "bin" / "codex"
    backup_file = tmp_path / "backup-file"
    marker = tmp_path / "update-ran"
    live.parent.mkdir()
    _write_fake_codex(live, "0.142.4")
    backup_file.write_text("not a directory", encoding="utf-8")
    update = _write_update_script(tmp_path / "update.sh", f"printf x > {marker}\n")

    result = _update(live, update, backup_file)

    assert result.status == "error"
    assert "could not back up current codex" in result.message
    assert not marker.exists()


@posix_only
def test_safe_codex_update_keeps_successful_candidate(tmp_path):
    old = tmp_path / "codex-old"
    new = tmp_path / "codex-new"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(old, "0.142.4")
    _write_fake_codex(new, "0.144.1")
    live.symlink_to(old)
    update = _write_update_script(tmp_path / "replace-with-new.sh", f"ln -sfn {new} {live}\n")

    result = _update(live, update, tmp_path / "backups")

    assert result.status == "updated", result.message
    assert result.exit_code == 0
    assert live.resolve() == new
    assert "0.144.1" in result.message
    assert result.previous_version == "codex-cli 0.142.4"
    assert result.candidate_version == "codex-cli 0.144.1"


def test_safe_update_rejects_nonpositive_probe_timeout():
    with pytest.raises(ValueError):
        codex_update.safe_update(probe_timeout_s=0)


# ── default updater: Homebrew cask detection ──────────────────────────────────────
@posix_only
def test_default_codex_update_command_uses_brew_only_for_brew_cask_codex(tmp_path, monkeypatch):
    brew = tmp_path / "homebrew" / "bin" / "brew"
    brew.parent.mkdir(parents=True)
    brew.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = \"--prefix\" ]; then dirname \"$(dirname \"$0\")\"; exit 0; fi\n"
        "if [ \"${1:-}\" = \"list\" ]; then exit 0; fi\n",
        encoding="utf-8",
    )
    brew.chmod(0o755)
    monkeypatch.setattr(
        codex_update.shutil, "which", lambda name: str(brew) if name == "brew" else None
    )

    cask_codex = tmp_path / "homebrew" / "Caskroom" / "codex" / "0.144.1" / "codex"
    brew_codex = tmp_path / "homebrew" / "bin" / "codex"
    other_codex = tmp_path / "other" / "codex"
    cask_codex.parent.mkdir(parents=True)
    brew_codex.symlink_to(cask_codex)

    expected = [str(brew), "upgrade", "--cask", "codex"]
    assert codex_update.default_update_command(brew_codex) == expected
    assert codex_update.default_update_command(other_codex) == [str(other_codex), "update"]


def test_default_codex_update_command_does_not_treat_brew_prefix_binary_as_cask(
    tmp_path, monkeypatch
):
    brew = tmp_path / "homebrew" / "bin" / "brew"
    codex = tmp_path / "homebrew" / "bin" / "codex"
    brew.parent.mkdir(parents=True)
    brew.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = \"--prefix\" ]; then dirname \"$(dirname \"$0\")\"; exit 0; fi\n"
        "if [ \"${1:-}\" = \"list\" ]; then exit 1; fi\n",
        encoding="utf-8",
    )
    brew.chmod(0o755)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        codex_update.shutil, "which", lambda name: str(brew) if name == "brew" else None
    )

    assert codex_update.default_update_command(codex) == [str(codex), "update"]


def test_default_codex_update_command_without_brew_uses_codex_update(tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    monkeypatch.setattr(codex_update.shutil, "which", lambda name: None)

    assert codex_update.default_update_command(codex) == [str(codex), "update"]


@posix_only
def test_default_codex_update_command_handles_intel_homebrew_symlink(tmp_path, monkeypatch):
    prefix = tmp_path / "usr-local"
    real_brew = prefix / "Homebrew" / "bin" / "brew"
    brew_link = prefix / "bin" / "brew"
    cask_codex = prefix / "Caskroom" / "codex" / "0.144.1" / "codex"
    codex = prefix / "bin" / "codex"
    real_brew.parent.mkdir(parents=True)
    brew_link.parent.mkdir(parents=True)
    cask_codex.parent.mkdir(parents=True)
    real_brew.write_text(
        "#!/usr/bin/env bash\n"
        f"if [ \"${{1:-}}\" = \"--prefix\" ]; then printf '%s\\n' {prefix}; exit 0; fi\n"
        "if [ \"${1:-}\" = \"list\" ]; then exit 0; fi\n",
        encoding="utf-8",
    )
    real_brew.chmod(0o755)
    brew_link.symlink_to(real_brew)
    codex.symlink_to(cask_codex)
    monkeypatch.setattr(
        codex_update.shutil, "which", lambda name: str(brew_link) if name == "brew" else None
    )

    expected = [str(brew_link), "upgrade", "--cask", "codex"]
    assert codex_update.default_update_command(codex) == expected
