"""Drift guard for the package version: ``harness_cli.__version__`` (what ``--version`` prints) and
``pyproject.toml``'s ``[project] version`` (what PyPI and ship's release gate see) are two literals
with no runtime link. A release that bumped only pyproject (with ``__init__`` left behind)
shipped a ``--version`` that disagreed with the package; this pins them together, and pins the
PyPI distribution name the README tells people to install."""

from __future__ import annotations

import re
from pathlib import Path

from harness_cli import __version__

PYPROJECT = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")


def _project_field(name: str) -> str:
    table = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", PYPROJECT, re.MULTILINE | re.DOTALL).group(1)
    return re.search(rf'^{name}\s*=\s*"([^"]+)"', table, re.MULTILINE).group(1)


def test_package_version_matches_pyproject() -> None:
    assert __version__ == _project_field("version")


def test_distribution_name_is_the_pypi_one() -> None:
    assert _project_field("name") == "hyper-harness"
