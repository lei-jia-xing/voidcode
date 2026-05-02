from __future__ import annotations

from pathlib import Path

from voidcode.provider.config import (
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderFallbackConfig,
)
from voidcode.provider.model_catalog import ProviderModelCatalog, ProviderModelMetadata
from voidcode.provider.registry import ModelProviderRegistry
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


def test_provider_readiness_preserves_invalid_provider_status(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="unknown-provider/demo",
            execution_engine="provider",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    assert readiness.provider == "unknown-provider"
    assert readiness.model == "demo"
    assert readiness.configured is False
    assert readiness.ok is False
    assert readiness.auth_present is False
    assert readiness.status == "invalid_model"
    assert "not supported" in readiness.guidance


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
    assert readiness.reasoning_controls["status"] == "not_requested"


def test_provider_readiness_reports_forwarded_reasoning_effort_controls(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="glm/glm-5",
            execution_engine="provider",
            reasoning_effort="high",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    controls = readiness.reasoning_controls
    assert controls["reasoning_effort_requested"] is True
    assert controls["status"] == "forwarded"
    assert controls["forwarded"] is True
    assert controls["provider_parameter"] == "reasoning_effort"


def test_provider_readiness_reports_forwarded_opencode_go_reasoning_effort(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="opencode-go/glm-5",
            execution_engine="provider",
            reasoning_effort="high",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    assert readiness.reasoning_controls["status"] == "unsupported"
    assert readiness.reasoning_controls["forwarded"] is False
    assert readiness.reasoning_controls["reason"] == "model_metadata_disallows_reasoning_effort"


def test_provider_readiness_marks_streaming_unsupported_as_not_ready(tmp_path: Path) -> None:
    registry = ModelProviderRegistry.with_defaults()
    registry.model_catalog = {
        "openai": ProviderModelCatalog(
            provider="openai",
            models=("batch-only",),
            refreshed=True,
            model_metadata={
                "batch-only": ProviderModelMetadata(
                    context_window=8_192,
                    max_output_tokens=1_024,
                    supports_streaming=False,
                )
            },
        )
    }
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="openai/batch-only",
            execution_engine="provider",
            providers=ProviderConfigs(openai=OpenAIProviderConfig(api_key="test-key")),
        ),
        model_provider_registry=registry,
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    assert readiness.ok is False
    assert readiness.status == "streaming_unsupported"
    assert readiness.streaming_supported is False


def test_provider_readiness_does_not_allocate_oauth_callback_state(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="google/gemini-2.5-pro",
            execution_engine="provider",
            providers=ProviderConfigs(
                google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="oauth"))
            ),
        ),
    )
    try:
        first = runtime.provider_readiness()
        second = runtime.provider_readiness()
        pending_states = runtime.provider_auth_resolver._pending_callback_states  # pyright: ignore[reportPrivateUsage]
    finally:
        runtime.__exit__(None, None, None)

    assert first.ok is False
    assert first.status == "missing_auth"
    assert second.status == "missing_auth"
    assert pending_states == {}
