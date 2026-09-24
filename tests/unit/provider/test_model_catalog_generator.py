"""Offline checks for the dev-time model-catalog generator.

The generator is a script, not an importable module, so it is loaded by path;
nothing here touches the network (the upstream fetch lives in ``main``).
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import fields
from pathlib import Path

import pytest

from voidcode.provider.model_catalog import ProviderModelMetadata
from voidcode.provider.reasoning_effort import CANONICAL_EFFORTS

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "generate_model_catalog.py"
_spec = importlib.util.spec_from_file_location("voidcode_generate_model_catalog", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Upstream advertises discrete effort values: the ladder order wins and
        # tokens that are not levels (``none``) are dropped.
        (
            {"reasoning_options": [{"type": "effort", "values": ["none", "high", "low", "max"]}]},
            {"supports_reasoning_effort": True, "supported_effort_levels": ["low", "high", "max"]},
        ),
        # Toggle and budget models expose a reasoning control but no ladder.
        ({"reasoning_options": [{"type": "toggle"}]}, {"supports_reasoning_effort": True}),
        (
            {"reasoning_options": [{"type": "budget_tokens", "min": 1024}]},
            {"supports_reasoning_effort": True},
        ),
        # An effort option whose values hold no level still means "capable".
        ({"reasoning_options": [{"type": "effort", "values": ["none"]}]}, {"supports_reasoning_effort": True}),
        # No options: an explicit False, which is what makes the runtime refuse.
        ({"reasoning_options": []}, {"supports_reasoning_effort": False}),
        ({}, {"supports_reasoning_effort": False}),
        ({"reasoning_options": None}, {"supports_reasoning_effort": False}),
    ],
)
def test_reasoning_effort_fields(raw: dict[str, object], expected: dict[str, object]) -> None:
    assert generator._reasoning_effort_fields(raw) == expected


def test_emitted_levels_are_the_runtime_ladder_in_ladder_order() -> None:
    """Levels are emitted in the order the runtime clamps against, so a shuffle
    upstream or a non-ladder token cannot reorder or leak into the catalog."""
    shuffled = ["default", "max", "none", *reversed(CANONICAL_EFFORTS)]
    fields_ = generator._reasoning_effort_fields({"reasoning_options": [{"type": "effort", "values": shuffled}]})
    assert fields_["supported_effort_levels"] == list(CANONICAL_EFFORTS)


def test_model_entry_only_emits_loader_known_fields() -> None:
    """The loader reads a fixed name set; anything else is silently ignored."""
    entry = generator._model_entry(
        {
            "limit": {"context": 128000, "output": 8192, "input": 100000},
            "cost": {"input": 1, "output": 2},
            "tool_call": True,
            "reasoning": True,
            "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
            "modalities": {"input": ["text", "image"], "output": ["text"]},
            "name": "Test Model",
        },
        "openai-completions",
    )
    assert entry is not None
    known = {model_field.name for model_field in fields(ProviderModelMetadata) if model_field.init}
    assert set(entry) <= known
    assert entry["supports_reasoning_effort"] is True
    assert entry["supported_effort_levels"] == ["low", "high"]
    assert entry["supports_vision"] is True
    assert entry["api"] == "openai-completions"
    assert entry["display_name"] == "Test Model"
    assert entry["modalities_output"] == ["text"]
    assert entry["max_input_tokens"] == 100000


def test_model_api_prefers_the_route_pin_then_the_npm_hint_then_the_provider_wire() -> None:
    """OMP's precedence: per-id pins win over upstream metadata, which wins over
    the provider's own wire."""
    # ``opencode-go/minimax-m3`` is pinned to chat-completions although upstream
    # hints ``@ai-sdk/anthropic``.
    assert generator._model_api("opencode-go", "minimax-m3", {"provider": {"npm": "@ai-sdk/anthropic"}}) == "openai-completions"
    # No pin, an ``@ai-sdk/openai`` hint: the hint decides.
    assert generator._model_api("opencode-go", "grok-4.6", {"provider": {"npm": "@ai-sdk/openai"}}) == "openai-responses"
    # Neither: the provider table's own wire.
    assert generator._model_api("opencode-go", "glm-5.3", {}) == "openai-completions"
    # The hint is read for any provider, not only the gateways.
    assert generator._model_api("openai", "gpt-5.4", {"provider": {"npm": "@ai-sdk/openai"}}) == "openai-responses"


def _upstream_model(*reasoning_options: dict[str, object]) -> dict[str, object]:
    return {
        "limit": {"context": 200000, "output": 8192},
        "tool_call": True,
        "reasoning": True,
        "modalities": {"input": ["text"]},
        "reasoning_options": list(reasoning_options),
    }


def test_authored_default_effort_is_applied_to_the_named_model_only() -> None:
    payload = {
        "github-copilot": {
            "models": {
                "kimi-k3": _upstream_model({"type": "effort", "values": ["low", "high", "max"]}),
                "kimi-k2.7-code": _upstream_model({"type": "effort", "values": ["low", "high"]}),
            }
        }
    }

    entries = generator._provider_catalog_entries("github-copilot", ("github-copilot",), payload)

    assert entries["kimi-k3"]["default_reasoning_effort"] == "max"
    assert "default_reasoning_effort" not in entries["kimi-k2.7-code"]


def test_shipped_catalog_carries_exactly_the_authored_default_efforts() -> None:
    catalog = json.loads(generator.OUTPUT_PATH.read_text(encoding="utf-8"))
    shipped = {
        (provider_id, model_id)
        for provider_id, models in catalog.items()
        for model_id, entry in models.items()
        if entry.get("default_reasoning_effort") is not None
    }

    # Upstream may drop a model between regenerations, so an authored pair that
    # no longer exists is fine; a default nobody authored is not.
    assert shipped <= set(generator.DEFAULT_REASONING_EFFORT)
    assert shipped
    for provider_id, model_id in shipped:
        assert catalog[provider_id][model_id]["default_reasoning_effort"] == generator.DEFAULT_REASONING_EFFORT[(provider_id, model_id)]
