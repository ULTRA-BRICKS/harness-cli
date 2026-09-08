"""Pytest path setup for harness-cli (standalone repo).

Puts the repo root on sys.path so a test can ``import harness_cli`` from a clean checkout
without an install step, matching the sibling ecosystem CLIs (task-cli, dev-cli).

Autouse: unit tests never hit the real network, macOS keychain, or process env tokens.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _hermetic_limits_side_effects(monkeypatch):
    monkeypatch.setenv("HARNESS_LIMITS_KEYCHAIN", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)

    def _blocked(*args, **kwargs):
        raise RuntimeError("urllib.request.urlopen blocked in unit tests")

    monkeypatch.setattr("urllib.request.urlopen", _blocked)
