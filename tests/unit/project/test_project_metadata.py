"""Tests for project metadata stored in pyproject.toml."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from typing import cast

from .._paths import REPO_ROOT


def test_pyproject_matches_expected_metadata() -> None:
    pyproject_path = REPO_ROOT / "pyproject.toml"

    assert pyproject_path.exists(), "pyproject.toml must exist"

    pyproject_data = cast(
        Mapping[str, object],
        tomllib.loads(pyproject_path.read_text(encoding="utf-8")),
    )
    project_data = cast(Mapping[str, object], pyproject_data["project"])

    assert project_data["name"] == "voidcode"
    assert isinstance(project_data["version"], str)
    assert project_data["version"]
    assert project_data["description"]
