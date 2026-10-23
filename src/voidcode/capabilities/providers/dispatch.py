"""Registered provider entry points for the ``providers`` plugin.

The core selects one registered provider name and hands it a turn request.
This module owns the policy behind that name: which native adapter serves the
configured provider, with which endpoint and credential, the gateway's own
per-model wire routing, and the catalog cost of the reported usage.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from threading import Lock

from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    StreamableTurnProvider,
    TurnProvider,
)

from .native.anthropic_native import AnthropicMessagesProvider
from .native.config import (
    PROVIDER_CONFIG_PAYLOAD_KEYS,
    PROVIDER_WIRES,
    AnthropicProviderConfig,
    CopilotProviderConfig,
    GoogleProviderConfig,
    OpenAICompatibleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    parse_provider_configs_payload,
)
from .native.copilot import GithubCopilotModelProvider
from .native.endpoint import OpenAIEndpointProvider
from .native.google import GoogleModelProvider
from .native.naming import UnknownProviderIdError, canonical_provider_id
from .native.openai import OpenAIModelProvider
from .native.openai_native import OpenAIChatCompletionsProvider
from .native.opencode import OpenCodeZenModelProvider
from .native.opencode_go import OpenCodeGoModelProvider
from .native.openrouter import OpenRouterModelProvider
from .native.provider_table import PROVIDER_TABLE
from .pricing import price_event, price_result

#: Provider names this plugin registers with the plugin host: every built-in
#: provider the shipped table declares. The configured name is the selection and
#: the core never learns which vendor policy answers it.
DECLARED_PROVIDER_NAMES: tuple[str, ...] = tuple(sorted(row.id for row in PROVIDER_TABLE))

_SELECTION_KEYS = frozenset({"wire"})

#: Vendors whose configuration nests its credential under ``auth``. The core's
#: provider entry carries a flat credential + endpoint, so the plugin maps that
#: pair into the vendor's own shape before the shape parser validates it.
_NESTED_CREDENTIAL_PROVIDERS = frozenset({"google", "github-copilot"})

_CREDENTIAL_FIELD_BY_PROVIDER: Mapping[str, str] = {"google": "api_key", "github-copilot": "token"}


def _entry_payload(provider_name: str, entry: Mapping[str, object]) -> dict[str, object]:
    """One configured provider entry in the vendor's own configuration shape."""
    payload = {key: value for key, value in entry.items() if key not in _SELECTION_KEYS}
    if provider_name not in _NESTED_CREDENTIAL_PROVIDERS or "auth" in payload:
        return payload
    credential = payload.pop("api_key", None)
    auth: dict[str, object] = {"method": "api_key" if provider_name == "google" else "token"}
    if credential is not None:
        auth[_CREDENTIAL_FIELD_BY_PROVIDER[provider_name]] = credential
    payload["auth"] = auth
    return payload


def provider_configs(config: Mapping[str, object]) -> ProviderConfigs:
    """Build the typed provider configuration from the core-injected section.

    The core's provider entries select a registered provider name through
    ``wire``; every remaining field is the vendor's own configuration, parsed by
    the same shape rules a configuration file used. An id the product does not
    declare resolves as a custom endpoint.
    """
    raw = config.get("providers")
    payload: dict[str, object] = {}
    custom: dict[str, object] = {}
    if isinstance(raw, Mapping):
        for raw_name, raw_entry in raw.items():
            name = canonical_provider_id(str(raw_name))
            entry = _entry_payload(name, raw_entry) if isinstance(raw_entry, Mapping) else {}
            payload_key = PROVIDER_CONFIG_PAYLOAD_KEYS.get(name)
            if payload_key is None:
                custom[name] = entry
            else:
                payload[payload_key] = entry
    if custom:
        payload["custom"] = custom
    parsed = parse_provider_configs_payload(payload, source="providers config")
    return parsed if parsed is not None else ProviderConfigs()


def _entry_for(configs: ProviderConfigs, provider_name: str) -> object | None:
    entry = configs.entry(provider_name)
    if entry is not None:
        return entry
    return configs.custom.get(canonical_provider_id(provider_name))


def _materialize(provider_name: str, configuration: object | None) -> TurnProvider:
    """Build the adapter a configured provider id runs on.

    Mirrors the product's provider dispatch: the named gateways and first-party
    vendors keep their own endpoint resolution, every remaining built-in id runs
    on the wire its table row declares, and an unknown id is only reachable as a
    declared custom endpoint.
    """
    if configuration is not None and not isinstance(
        configuration,
        (
            OpenAIProviderConfig,
            AnthropicProviderConfig,
            GoogleProviderConfig,
            CopilotProviderConfig,
            ProviderEndpointConfig,
            OpenAICompatibleProviderConfig,
        ),
    ):
        raise ValueError(f"provider configuration has the wrong type for {provider_name!r}")
    entry = configuration
    match provider_name:
        case "opencode-zen" if entry is None or isinstance(entry, ProviderEndpointConfig):
            return OpenCodeZenModelProvider(config=entry).turn_provider()
        case "openai" if entry is None or isinstance(entry, OpenAIProviderConfig):
            return OpenAIModelProvider(config=entry).turn_provider()
        case "google" if entry is None or isinstance(entry, GoogleProviderConfig):
            return GoogleModelProvider(config=entry).turn_provider()
        case "github-copilot" if entry is None or isinstance(entry, CopilotProviderConfig):
            return GithubCopilotModelProvider(config=entry).turn_provider()
        case "endpoint" if entry is None or isinstance(entry, ProviderEndpointConfig):
            return OpenAIEndpointProvider(name=provider_name, config=entry).turn_provider()
        case "openrouter" if entry is None or isinstance(entry, ProviderEndpointConfig):
            return OpenRouterModelProvider(config=entry).turn_provider()
        case "opencode-go" if entry is None or isinstance(entry, OpenAICompatibleProviderConfig):
            return OpenCodeGoModelProvider(config=entry).turn_provider()
        case "opencode-zen" | "openai" | "google" | "github-copilot" | "endpoint" | "openrouter" | "opencode-go":
            raise ValueError(f"provider configuration has the wrong type for {provider_name!r}")
    wire = PROVIDER_WIRES.get(provider_name)
    if wire is not None:
        if wire.shape == "anthropic" and (entry is None or isinstance(entry, AnthropicProviderConfig)):
            return AnthropicMessagesProvider(name=provider_name, config=entry)
        if wire.shape == "openai_compatible" and (entry is None or isinstance(entry, OpenAICompatibleProviderConfig)):
            return OpenAIChatCompletionsProvider(name=provider_name, config=_endpoint_of(provider_name, entry))
        raise ValueError(f"provider configuration has the wrong type for {provider_name!r}")
    if isinstance(entry, ProviderEndpointConfig):
        return OpenAIEndpointProvider(name=provider_name, config=entry).turn_provider()
    raise UnknownProviderIdError(provider_name)


def _endpoint_of(provider_name: str, configuration: OpenAICompatibleProviderConfig | None) -> ProviderEndpointConfig:
    from .native.config import openai_compatible_endpoint_config

    return openai_compatible_endpoint_config(provider_name, configuration)


class RegisteredProvider:
    """One provider name the core can select; requests route by their own provider id."""

    def __init__(self, name: str, configs: ProviderConfigs) -> None:
        self._name = name
        self._configs = configs
        self._providers: dict[str, TurnProvider] = {}
        self._lock = Lock()

    @property
    def name(self) -> str:
        return self._name

    def _provider_for(self, provider_name: str) -> TurnProvider:
        canonical = canonical_provider_id(provider_name)
        with self._lock:
            provider = self._providers.get(canonical)
            if provider is None:
                try:
                    provider = _materialize(canonical, _entry_for(self._configs, canonical))
                except UnknownProviderIdError as exc:
                    raise ProviderExecutionError(
                        kind="not_configured",
                        provider_name=canonical,
                        model_name="",
                        message=str(exc),
                    ) from None
                self._providers[canonical] = provider
            return provider

    def _selected(self, request: ProviderTurnRequest) -> TurnProvider:
        provider_name = request.provider_name
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name="unknown",
                model_name=request.model_name or "",
                message="request.provider_name is required; no silent fallback allowed",
            )
        return self._provider_for(provider_name)

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        return price_result(request, self._selected(request).propose_turn(request))

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider = self._selected(request)
        if not isinstance(provider, StreamableTurnProvider):
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=request.provider_name or self._name,
                model_name=request.model_name or "",
                message=f"provider '{request.provider_name or self._name}' does not support streaming",
                retryable=False,
                fallback_allowed=False,
            )
        for event in provider.stream_turn(request):
            yield price_event(request, event)

    def close(self) -> None:
        with self._lock:
            providers = tuple(self._providers.values())
            self._providers.clear()
        with ExitStack() as stack:
            for provider in providers:
                closer = getattr(provider, "close", None)
                if callable(closer):
                    stack.callback(closer)


def build_registered_providers(config: Mapping[str, object]) -> tuple[RegisteredProvider, ...]:
    """One registered entry point per declared provider name."""
    configs = provider_configs(config)
    return tuple(RegisteredProvider(name, configs) for name in DECLARED_PROVIDER_NAMES)
