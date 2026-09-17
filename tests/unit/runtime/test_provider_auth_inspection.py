from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from voidcode.provider.auth import ProviderAuthResolver
from voidcode.provider.config import (
    CopilotProviderAuthConfig,
    CopilotProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    OpenAICompatibleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
)
from voidcode.provider.protocol import ProviderExecutionError
from voidcode.runtime.execution.provider_fallback import (
    DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG,
    ProviderFallbackDecision,
    decide_provider_error_policy,
)
from voidcode.runtime.provider_inspection import RuntimeProviderAuthInspector, RuntimeProviderEndpointInspector


def _inspector(
    providers: ProviderConfigs | None,
    *,
    env: dict[str, str] | None = None,
) -> RuntimeProviderAuthInspector:
    environment = {} if env is None else env
    return RuntimeProviderAuthInspector(
        providers=providers,
        resolver=ProviderAuthResolver(providers=providers, env=environment),
        env=environment,
    )


def _endpoint_inspector(providers: ProviderConfigs | None) -> RuntimeProviderEndpointInspector:
    return RuntimeProviderEndpointInspector(providers=providers)


def test_provider_auth_inspector_reports_configured_builtin_and_custom_providers() -> None:
    inspector = _inspector(
        ProviderConfigs(
            openai=OpenAIProviderConfig(),
            custom={"local": ProviderEndpointConfig()},
        )
    )

    assert inspector.is_configured("openai") is True
    assert inspector.is_configured("anthropic") is False
    assert inspector.is_configured("local") is True


def test_provider_auth_inspector_reports_endpoint_provider_configuration() -> None:
    configured = _inspector(ProviderConfigs(endpoint=ProviderEndpointConfig(api_key="endpoint-key")))
    unconfigured = _inspector(ProviderConfigs())

    assert configured.is_configured("endpoint") is True
    assert unconfigured.is_configured("endpoint") is False


def test_provider_auth_inspector_maps_missing_credentials_to_missing_auth() -> None:
    presence = _inspector(ProviderConfigs(openai=OpenAIProviderConfig())).presence("openai")

    assert presence.present is False
    assert presence.failure_kind == "missing_auth"
    assert presence.message is not None
    assert "openai.api_key" in presence.message


def test_provider_auth_inspector_checks_google_oauth_without_callback_allocation() -> None:
    providers = ProviderConfigs(google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="oauth", access_token="token")))
    resolver = ProviderAuthResolver(providers=providers, env={})
    inspector = RuntimeProviderAuthInspector(providers=providers, resolver=resolver, env={})

    presence = inspector.presence("google")

    assert presence.present is True
    assert resolver._pending_callback_states == {}


def test_provider_auth_inspector_reads_copilot_oauth_token_environment() -> None:
    providers = ProviderConfigs(copilot=CopilotProviderConfig(auth=CopilotProviderAuthConfig(method="oauth", token_env_var="COPILOT_TEST_TOKEN")))

    presence = _inspector(providers, env={"COPILOT_TEST_TOKEN": "token"}).presence("copilot")

    assert presence.present is True


def test_provider_auth_inspector_preserves_invalid_provider_failure() -> None:
    presence = _inspector(ProviderConfigs()).presence("unknown")

    assert presence.present is False
    assert presence.failure_kind == "invalid_model"
    assert presence.message is not None


def test_provider_endpoint_inspector_reports_a_configured_base_url() -> None:
    facts = _endpoint_inspector(
        ProviderConfigs(deepseek=OpenAICompatibleProviderConfig(api_key="deepseek-key", base_url="https://deepseek-proxy.example.test/v1"))
    ).facts("deepseek")

    assert facts.as_payload() == {
        "base_url": "https://deepseek-proxy.example.test/v1",
        "source": "config",
        "discovery_base_url": None,
    }


def test_provider_endpoint_inspector_reports_a_provider_default() -> None:
    facts = _endpoint_inspector(ProviderConfigs(openai=OpenAIProviderConfig(api_key="sk-openai"))).facts("openai")

    assert facts.as_payload() == {
        "base_url": "https://api.openai.com/v1",
        "source": "provider_default",
        "discovery_base_url": "https://api.openai.com",
    }


def test_provider_endpoint_inspector_reports_the_unconfigured_endpoint() -> None:
    """An unconfigured provider names the host it would call, never a neighbour's."""
    facts = _endpoint_inspector(ProviderConfigs()).facts("deepseek")

    assert facts.as_payload() == {
        "base_url": "https://api.deepseek.com/v1",
        "source": "provider_default",
        "discovery_base_url": "",
    }


def test_provider_endpoint_inspector_reports_the_generic_endpoint_default() -> None:
    facts = _endpoint_inspector(ProviderConfigs()).facts("endpoint")

    assert facts.as_payload() == {
        "base_url": "http://127.0.0.1:4000/v1",
        "source": "endpoint_default",
        "discovery_base_url": "http://127.0.0.1:4000",
    }


def test_provider_endpoint_inspector_reads_a_custom_provider_base_url() -> None:
    facts = _endpoint_inspector(ProviderConfigs(custom={"local": ProviderEndpointConfig(base_url="http://127.0.0.1:8123")})).facts("local")

    assert facts.base_url == "http://127.0.0.1:8123/v1"
    assert facts.source == "config"


def test_not_configured_provider_error_is_recoverable_by_fallback_only() -> None:
    """A provider with no endpoint of its own is skippable, not retryable.

    Runtime recovery reads these flags when a provider error carries none, so
    ``not_configured`` must default to "fall back to the next target" -- retrying
    an endpoint-less provider can only fail again.
    """
    decision = decide_provider_error_policy(
        error=ProviderExecutionError(
            kind="not_configured",
            provider_name="acme-gateway",
            model_name="acme-chat",
            message="provider 'acme-gateway' has no endpoint configured",
        ),
        current_provider_attempt=0,
        provider_retry_attempt=0,
        transient_retry_config=DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG,
        fallback_target_provider="openai",
        fallback_target_model="gpt-4o",
        background_rate_limit_retry=False,
    )

    assert isinstance(decision, ProviderFallbackDecision)
    assert decision.to_provider == "openai"
    assert decision.to_model == "gpt-4o"


def test_provider_inspect_command_reports_the_resolved_endpoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``provider inspect`` publishes the endpoint the provider would call, and its origin."""
    cli = importlib.import_module("voidcode.cli.app")
    runtime_gateway = importlib.import_module("voidcode.cli.runtime_gateway")
    contracts = importlib.import_module("voidcode.runtime.contracts")
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "model": "deepseek/deepseek-chat",
                "providers": {"deepseek": {"base_url": "https://deepseek-proxy.example.test/v1"}},
            }
        ),
        encoding="utf-8",
    )
    inspect_result = contracts.ProviderInspectResult(
        summary=contracts.ProviderSummary(name="deepseek", label="DeepSeek", configured=True),
        models=contracts.ProviderModelsResult(provider="deepseek", configured=True),
        validation=contracts.ProviderValidationResult(provider="deepseek", configured=True, ok=True, status="ok", message="ok"),
    )

    with patch.object(runtime_gateway, "VoidCodeRuntime", autospec=True) as runtime_class:
        runtime_class.return_value.inspect_provider.return_value = inspect_result
        exit_code = cli.main(["provider", "inspect", "deepseek", "--workspace", str(tmp_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["endpoint"] == {
        "base_url": "https://deepseek-proxy.example.test/v1",
        "source": "config",
        "discovery_base_url": None,
    }
