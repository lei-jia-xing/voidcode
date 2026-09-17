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
    registry = ModelProviderRegistry.with_defaults()
    registry.model_catalog = {
        "openai": ProviderModelCatalog(
            provider="openai",
            models=("gpt-4o", "gpt-4o-mini"),
            refreshed=True,
            model_metadata={
                "gpt-4o": ProviderModelMetadata(
                    context_window=128_000,
                    max_output_tokens=16_384,
                    supports_streaming=True,
                ),
                "gpt-4o-mini": ProviderModelMetadata(
                    context_window=128_000,
                    max_output_tokens=16_384,
                    supports_streaming=True,
                ),
            },
        )
    }
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
        model_provider_registry=registry,
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


def test_provider_readiness_reports_model_metadata_forwarded_reasoning_effort(
    tmp_path: Path,
) -> None:
    # Old promise: the payload claimed `provider_parameter = "reasoning_effort"`,
    # which is false for the binary providers (zai/zhipuai send
    # `extra_body.thinking.type`). New promise: it reports the model's own
    # capability and the layer that decided, and claims no wire field.
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="zai/glm-5",
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
    assert controls["supports_reasoning_effort"] is True
    assert controls["capability_source"] == "model_metadata"
    assert "provider_parameter" not in controls


def test_provider_readiness_keeps_the_provider_fallback_reason_for_unknown_models(
    tmp_path: Path,
) -> None:
    # A single-upstream host whose API does not take the field: the provider-level
    # fallback decides, and the reason must say so instead of blaming model metadata.
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="qwen/not-in-any-catalog",
            execution_engine="provider",
            reasoning_effort="high",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    controls = readiness.reasoning_controls
    assert controls["status"] == "unsupported"
    assert controls["forwarded"] is False
    assert controls["supports_reasoning_effort"] is False
    assert controls["capability_source"] == "provider_default"
    assert controls["reason"] == "provider_default_disallows_reasoning_effort"


def test_provider_readiness_reports_unverified_forward_when_capability_is_unknown(
    tmp_path: Path,
) -> None:
    # Neither the model catalog nor the provider allowlist knows this target, so
    # the hint is forwarded best-effort but must not be reported as verified.
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="custom-provider/some-model",
            execution_engine="provider",
            reasoning_effort="high",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    controls = readiness.reasoning_controls
    assert controls["status"] == "forwarded_unverified"
    assert controls["forwarded"] is True
    assert controls["reason"] == "model_capability_unknown"
    assert controls["supports_reasoning_effort"] is None
    assert controls["capability_source"] == "unknown"


def test_provider_readiness_reports_the_shipped_catalog_for_the_gateway_model(
    tmp_path: Path,
) -> None:
    # The gateway's model is in the shipped catalog, so the model's own capability
    # (levels low/high/max) answers - the gateway name no longer decides.
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="opencode-go/deepseek-v4.1-flash",
            execution_engine="provider",
            reasoning_effort="medium",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    controls = readiness.reasoning_controls
    assert controls["status"] == "forwarded"
    assert controls["forwarded"] is True
    assert controls["supports_reasoning_effort"] is True
    assert controls["capability_source"] == "model_metadata"


def test_provider_readiness_forwards_an_uncatalogued_gateway_model_unverified(
    tmp_path: Path,
) -> None:
    # A gateway model the shipped catalog does not describe still forwards; the
    # gateway serves many upstreams, so its name is not a capability verdict.
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="opencode-go/not-in-any-catalog",
            execution_engine="provider",
            reasoning_effort="high",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    controls = readiness.reasoning_controls
    assert controls["status"] == "forwarded_unverified"
    assert controls["forwarded"] is True
    assert controls["supports_reasoning_effort"] is None
    assert controls["capability_source"] == "unknown"
    assert controls["reason"] == "model_capability_unknown"


def test_provider_readiness_prefers_model_metadata_over_the_provider_allowlist(
    tmp_path: Path,
) -> None:
    # `kimi` is on the provider-level denylist, but the shipped catalog says
    # kimi-k2.6 takes minimal..high: the model wins.
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            model="kimi/kimi-k2.6",
            execution_engine="provider",
            reasoning_effort="high",
        ),
    )
    try:
        readiness = runtime.provider_readiness()
    finally:
        runtime.__exit__(None, None, None)

    controls = readiness.reasoning_controls
    assert controls["status"] == "forwarded"
    assert controls["forwarded"] is True
    assert controls["capability_source"] == "model_metadata"
    assert controls["supports_reasoning_effort"] is True


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
            providers=ProviderConfigs(google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="oauth"))),
        ),
    )
    try:
        first = runtime.provider_readiness()
        second = runtime.provider_readiness()
        pending_states = runtime.provider_auth_resolver._pending_callback_states
    finally:
        runtime.__exit__(None, None, None)

    assert first.ok is False
    assert first.status == "missing_auth"
    assert second.status == "missing_auth"
    assert pending_states == {}
