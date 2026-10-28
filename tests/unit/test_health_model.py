"""health.model reports the admitted model and its supported thinking levels."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.bootstrap import open_core
from voidcode.sdk.client import HealthStatus
from voidcode.transport.projection import model_payload
from voidcode.tui.local_client import CoreLocalClient


@pytest.mark.parametrize(
    ("raw", "effort", "expected_effort", "expected_levels"),
    [
        ("openai/gpt-5.2", "max", "xhigh", ("off", "low", "medium", "high", "xhigh")),
        ("openai/gpt-5", "max", "high", ("minimal", "low", "medium", "high")),
        ("openai/gpt-5.2", "high", "high", ("off", "low", "medium", "high", "xhigh")),
        ("openai/gpt-5.2", "off", "off", ("off", "low", "medium", "high", "xhigh")),
        ("openai/gpt-5.2", "none", "off", ("off", "low", "medium", "high", "xhigh")),
        ("openai/gpt-5", "off", None, ("minimal", "low", "medium", "high")),
        ("anthropic/claude-opus-4-6", "xhigh", "high", ("low", "medium", "high", "max")),
        ("anthropic/claude-sonnet-4-5", "high", "high", ("minimal", "low", "medium", "high", "xhigh", "max")),
        ("google/gemini-3-flash-preview", "xhigh", "high", ("minimal", "low", "medium", "high")),
        ("zai/glm-4.6", "high", None, ()),
        ("qwen/qwen-plus", "high", None, ()),
        ("openai/gpt-4o", "max", None, ()),
        ("openai/gpt-4", "max", None, ()),
        ("endpoint/anything", "max", None, ()),
        ("openai/gpt-5.2", None, None, ("off", "low", "medium", "high", "xhigh")),
        ("openai/gpt-5.2", 20000, None, ("off", "low", "medium", "high", "xhigh")),
        ("openai/gpt-5.2", "bogus", None, ("off", "low", "medium", "high", "xhigh")),
        (None, "max", None, ()),
        ("not-a-reference", "max", None, ()),
    ],
)
def test_supported_level_selection_and_hiding(
    raw: str | None, effort: str | int | None, expected_effort: str | None, expected_levels: tuple[str, ...]
) -> None:
    section = model_payload(raw=raw, configured_effort=effort)
    assert section["reasoning_effort"] == expected_effort
    assert tuple(section["reasoning_levels"]) == expected_levels
    assert "budget" not in json.dumps(section)
    assert section["provider"] == (raw.split("/")[0] if raw is not None and "/" in raw else None)


def test_health_model_reads_admitted_config_not_the_edited_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "openai/gpt-5.2", "reasoning_effort": "max"},
                "providers": {"openai": {"api_key": "dummy", "base_url": "http://127.0.0.1:9/v1"}},
                "plugins": {},
            }
        ),
        encoding="utf-8",
    )
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "openai/gpt-5", "reasoning_effort": "low"},
                    "providers": {"openai": {"api_key": "dummy", "base_url": "http://127.0.0.1:9/v1"}},
                    "plugins": {},
                }
            ),
            encoding="utf-8",
        )
        model = CoreLocalClient(core, workspace=workspace).health().model
        assert model is not None
        assert (model.raw, model.reasoning_effort) == ("openai/gpt-5.2", "xhigh")
        assert model.reasoning_levels == ("off", "low", "medium", "high", "xhigh")


def test_health_rejects_unknown_model_keys() -> None:
    with pytest.raises(ValueError):
        HealthStatus.from_payload(
            {
                "status": "ok",
                "stage": "serving",
                "pid": 1,
                "workspace": "w",
                "revision_id": "r",
                "launch_protocol": 1,
                "core_version": "v",
                "web_bundle": None,
                "generation": 1,
                "active_upgrade": None,
                "model": {"raw": None, "bogus": 1},
            }
        )
