"""One validated compare-and-swap API owns model-section rewrites."""

from pathlib import Path

import pytest

from voidcode.config import ModelConfig, load_config, update_model_section


def _write_config(workspace: Path, payload: str) -> None:
    (workspace / "voidcode.toml").write_text(payload, encoding="utf-8")


def test_update_model_preserves_other_sections_and_round_trips(tmp_path: Path) -> None:
    _write_config(tmp_path, 'schema_version = 1\n\n[model]\nmodel = "old/model"\n\n[policy]\napproval_mode = "write"\n')
    config = update_model_section(tmp_path, "model", "openai/gpt-5.2")
    assert config.model == ModelConfig(model="openai/gpt-5.2")
    stored = load_config(tmp_path)
    assert stored.model.model == "openai/gpt-5.2"
    assert stored.policy.approval_mode == "write"


def test_update_creates_a_missing_config_with_schema_version(tmp_path: Path) -> None:
    config = update_model_section(tmp_path, "reasoning_effort", "high")
    assert config.model.reasoning_effort == "high"
    assert load_config(tmp_path).model.reasoning_effort == "high"


def test_invalid_values_leave_the_file_byte_identical(tmp_path: Path) -> None:
    original = 'schema_version = 1\n\n[model]\nmodel = "old/model"\n'
    _write_config(tmp_path, original)
    before = (tmp_path / "voidcode.toml").read_bytes()
    with pytest.raises(ValueError):
        update_model_section(tmp_path, "model", "")
    with pytest.raises(ValueError):
        update_model_section(tmp_path, "reasoning_effort", "ultra")
    with pytest.raises(ValueError):
        update_model_section(tmp_path, "reasoning_effort", -1)
    assert (tmp_path / "voidcode.toml").read_bytes() == before


def test_already_invalid_config_is_refused_before_any_write(tmp_path: Path) -> None:
    original = "schema_version = 1\nmodel = []\nunknown = true\n"
    _write_config(tmp_path, original)
    before = (tmp_path / "voidcode.toml").read_bytes()
    with pytest.raises(ValueError):
        update_model_section(tmp_path, "model", "openai/gpt-5.2")
    assert (tmp_path / "voidcode.toml").read_bytes() == before
