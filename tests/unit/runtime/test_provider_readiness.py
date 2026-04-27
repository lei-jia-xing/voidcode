from __future__ import annotations

from pathlib import Path

from voidcode.provider.config import OpenAIProviderConfig, ProviderConfigs, ProviderFallbackConfig
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.service import VoidCodeRuntime


def test_provider_readiness_reports_missing_auth(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="openai/gpt-4o",
            execution_engine="provider",
            providers=ProviderConfigs(openai=OpenAIProviderConfig()),
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    assert readiness.provider == "openai"
    assert readiness.model == "gpt-4o"
    assert readiness.configured is True
    assert readiness.ok is False
    assert readiness.status == "missing_auth"
    assert readiness.auth_present is False
    assert "openai.api_key" in readiness.guidance


def test_provider_readiness_includes_fallback_and_context_metadata(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="openai/gpt-4o",
            execution_engine="provider",
            providers=ProviderConfigs(openai=OpenAIProviderConfig(api_key="test-key")),
            provider_fallback=ProviderFallbackConfig(
                preferred_model="openai/gpt-4o",
                fallback_models=("openai/gpt-4o-mini",),
            ),
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    assert readiness.ok is True
    assert readiness.auth_present is True
    assert readiness.context_window == 128_000
    assert readiness.max_output_tokens == 16_384
    assert readiness.streaming_supported is True
    assert readiness.fallback_chain == ("openai/gpt-4o", "openai/gpt-4o-mini")
