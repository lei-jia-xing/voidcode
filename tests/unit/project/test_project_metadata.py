"""Tests for project metadata stored in pyproject.toml."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast


def test_pyproject_matches_expected_metadata() -> None:
    pyproject_path = Path(__file__).resolve().parents[3] / "pyproject.toml"

    assert pyproject_path.exists(), "pyproject.toml must exist"

    pyproject_data = cast(
        Mapping[str, object],
        tomllib.loads(pyproject_path.read_text(encoding="utf-8")),
    )
    project_data = cast(Mapping[str, object], pyproject_data["project"])

    assert project_data["name"] == "voidcode"
    assert project_data["version"] == "0.1.0"
    assert project_data["description"]
