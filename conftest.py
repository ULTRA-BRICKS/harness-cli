"""Pytest path setup for harness-cli (standalone repo).

Puts the repo root on sys.path so a test can ``import harness_cli`` from a clean checkout
without an install step, matching the sibling ecosystem CLIs (task-cli, dev-cli).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
