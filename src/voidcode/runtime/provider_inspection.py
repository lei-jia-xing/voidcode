from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from ..provider.auth import (
    ProviderAuthAuthorizeRequest,
    ProviderAuthResolutionError,
    ProviderAuthResolver,
)
from ..provider.config import PROVIDER_CONFIG_FIELDS, ProviderConfigEntry, ProviderConfigs, ProviderEndpointConfig
from ..provider.errors import guidance_for_provider_error_kind
from ..provider.naming import canonical_provider_id
from ..provider.openai_native import normalize_openai_base_url
from ..provider.provider_config import DEFAULT_ENDPOINT_BASE_URL
from ..provider.registry import ModelProviderRegistry
from .config import MODEL_ENV_VAR, RUNTIME_CONFIG_FILE_NAME
from .contracts import (
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
)


def provider_config_entry(providers: ProviderConfigs | None, provider_name: str) -> ProviderConfigEntry | None:
    """Return the raw ``provider_name`` config entry, or ``None`` when unconfigured.

    Provider ids are case-insensitive: ``MiniMax`` reads the ``minimax`` entry.
    """
    if providers is None:
        return None
    entry = providers.entry(provider_name)
    if entry is not None:
        return entry
    return providers.custom.get(canonical_provider_id(provider_name))


def provider_credentials_config_path(provider_name: str) -> str:
    """Runtime config path that holds one provider's credentials."""
    canonical_name = canonical_provider_id(provider_name)
    field_name = PROVIDER_CONFIG_FIELDS.get(canonical_name)
    if field_name is None:
        return f"providers.custom.{canonical_name}.api_key"
    return f"providers.{field_name}.api_key"


def missing_model_guidance() -> str:
    """First-run remediation for a workspace with no model configured."""
    return (
        "No model is configured. Set "
        f'"model": "<provider>/<model>" in {RUNTIME_CONFIG_FILE_NAME} '
        f"(or the {MODEL_ENV_VAR} environment variable), for example "
        '"model": "openai/gpt-4o".'
    )


def unconfigured_provider_guidance(provider_name: str | None) -> str:
    """First-run remediation for a provider that has no credentials at all."""
    if provider_name is None:
        return f'No provider is configured. Add a providers entry in {RUNTIME_CONFIG_FILE_NAME} or set {MODEL_ENV_VAR} to "<provider>/<model>".'
    return (
        f"Provider '{provider_name}' has no credentials. Set {provider_credentials_config_path(provider_name)} "
        f"in {RUNTIME_CONFIG_FILE_NAME} (or the provider's credentials environment variable), then rerun."
    )


def missing_credentials_guidance(provider_name: str | None) -> str:
    """First-run remediation for a provider whose credentials are incomplete."""
    if provider_name is None:
        return guidance_for_provider_error_kind("missing_auth")
    return (
        f"Provider '{provider_name}' is missing credentials. Set {provider_credentials_config_path(provider_name)} "
        f"in {RUNTIME_CONFIG_FILE_NAME} (or the provider's credentials environment variable), then rerun."
    )


@dataclass(frozen=True, slots=True)
class ProviderAuthPresence:
    present: bool | None
    failure_kind: str | None = None
    message: str | None = None

    def as_tuple(self) -> tuple[bool | None, str | None, str | None]:
        return self.present, self.failure_kind, self.message


class RuntimeProviderAuthInspector:
    """Inspect configured provider auth without owning validation or refresh flows."""

    def __init__(
        self,
        *,
        providers: ProviderConfigs | None,
        resolver: ProviderAuthResolver,
        env: Mapping[str, str],
    ) -> None:
        self._providers = providers
        self._resolver = resolver
        self._env = env

    def is_configured(self, provider_name: str) -> bool:
        return provider_config_entry(self._providers, provider_name) is not None

    def presence(self, provider_name: str | None) -> ProviderAuthPresence:
        if provider_name is None:
            return ProviderAuthPresence(present=None)
        oauth_presence = self._oauth_presence(provider_name)
        if oauth_presence is not None:
            return oauth_presence
        try:
            result = self._resolver.authorize(ProviderAuthAuthorizeRequest(provider=provider_name))
        except ProviderAuthResolutionError as exc:
            return ProviderAuthPresence(
                present=False,
                failure_kind=("missing_auth" if exc.code == "missing_credentials" else exc.provider_error_kind),
                message=str(exc),
            )
        return ProviderAuthPresence(present=result.status == "authorized")

    def _oauth_presence(self, provider_name: str) -> ProviderAuthPresence | None:
        providers = self._providers
        if providers is None:
            return None
        if provider_name == "google":
            config = providers.google
            auth = None if config is None else config.auth
            if auth is None or auth.method != "oauth":
                return None
            if auth.access_token:
                return ProviderAuthPresence(present=True)
            return ProviderAuthPresence(
                present=False,
                failure_kind="missing_auth",
                message=("provider auth field 'google.access_token' must be provided for google oauth auth"),
            )
        if provider_name == "copilot":
            config = providers.copilot
            auth = None if config is None else config.auth
            if auth is None or auth.method != "oauth":
                return None
            if auth.token or (auth.token_env_var and self._env.get(auth.token_env_var)):
                return ProviderAuthPresence(present=True)
            return ProviderAuthPresence(
                present=False,
                failure_kind="missing_auth",
                message=("provider auth field 'copilot.token' must be provided for copilot oauth auth"),
            )
        return None


type ProviderEndpointSource = Literal["config", "provider_default", "endpoint_default"]


@dataclass(frozen=True, slots=True)
class ProviderEndpointFacts:
    """The endpoint one provider resolves to, and where that endpoint came from."""

    base_url: str | None
    source: ProviderEndpointSource

    def as_payload(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "source": self.source,
        }


class RuntimeProviderEndpointInspector:
    """Report the endpoint a provider turn would use, before anything is sent.

    Resolution goes through the same registry and the same base-URL
    normalization the wire adapter applies, so ``provider inspect`` and the
    request cannot disagree about which host a provider talks to.
    """

    def __init__(self, *, providers: ProviderConfigs | None) -> None:
        self._providers = providers
        self._registry = ModelProviderRegistry.with_defaults(provider_configs=providers)

    def facts(self, provider_name: str) -> ProviderEndpointFacts:
        endpoint_config = self._registry.provider_config(provider_name)
        return ProviderEndpointFacts(
            base_url=self._base_url(endpoint_config),
            source=self._source(provider_name, endpoint_config),
        )

    @staticmethod
    def _base_url(endpoint_config: ProviderEndpointConfig | None) -> str | None:
        if endpoint_config is None or not endpoint_config.base_url:
            return None
        if endpoint_config.wire == "anthropic-messages":
            # The Anthropic wire owns its own path conventions; reporting it as
            # an OpenAI chat base URL would name an endpoint nothing calls.
            return endpoint_config.base_url
        return normalize_openai_base_url(endpoint_config.base_url)

    def _source(self, provider_name: str, endpoint_config: ProviderEndpointConfig | None) -> ProviderEndpointSource:
        entry = provider_config_entry(self._providers, provider_name)
        configured = None if entry is None else entry.base_url
        if configured is not None and configured.strip():
            return "config"
        if endpoint_config is not None and endpoint_config.base_url == DEFAULT_ENDPOINT_BASE_URL:
            # Nothing configured: the generic endpoint provider's documented
            # local gateway is the only host the runtime will call.
            return "endpoint_default"
        return "provider_default"


@dataclass(frozen=True, slots=True)
class ProviderReadinessFacts:
    provider: str | None
    model: str | None
    configured: bool
    auth_present: bool | None
    auth_failure_kind: str | None = None
    auth_message: str | None = None
    streaming_configured: bool | None = None
    streaming_supported: bool | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    fallback_chain: tuple[str, ...] = ()
    reasoning_controls: dict[str, object] = field(default_factory=dict)


class RuntimeProviderReadinessProjector:
    """Project resolved provider facts into the stable readiness contract."""

    @staticmethod
    def project(facts: ProviderReadinessFacts) -> ProviderReadinessResult:
        status = "ready"
        ok = facts.configured and facts.auth_present is not False
        guidance = "Provider/model configuration is ready enough to run."
        if facts.provider is None or facts.model is None:
            status = "missing_model"
            ok = False
            guidance = missing_model_guidance()
        elif facts.auth_present is False and facts.auth_failure_kind == "invalid_model":
            status = facts.auth_failure_kind
            ok = False
            guidance = facts.auth_message or guidance_for_provider_error_kind("invalid_model")
        elif not facts.configured:
            status = "unconfigured"
            ok = False
            guidance = unconfigured_provider_guidance(facts.provider)
        elif facts.auth_present is False:
            status = facts.auth_failure_kind or "missing_auth"
            ok = False
            guidance = facts.auth_message or missing_credentials_guidance(facts.provider)
        elif facts.streaming_supported is False:
            status = "streaming_unsupported"
            ok = False
            guidance = guidance_for_provider_error_kind("unsupported_feature")
        return ProviderReadinessResult(
            provider=facts.provider,
            model=facts.model,
            configured=facts.configured,
            ok=ok,
            status=status,
            guidance=guidance,
            auth_present=facts.auth_present,
            streaming_configured=facts.streaming_configured,
            streaming_supported=facts.streaming_supported,
            context_window=facts.context_window,
            max_output_tokens=facts.max_output_tokens,
            fallback_chain=facts.fallback_chain,
            reasoning_controls=facts.reasoning_controls,
        )


@dataclass(frozen=True, slots=True)
class ProviderValidationFacts:
    provider: str
    configured: bool
    auth_present: bool | None
    auth_failure_kind: str | None = None
    auth_message: str | None = None
    models: ProviderModelsResult | None = None


class RuntimeProviderValidationProjector:
    """Project auth and model-discovery facts into the validation contract."""

    @staticmethod
    def project(facts: ProviderValidationFacts) -> ProviderValidationResult:
        models = facts.models
        if not facts.configured:
            return ProviderValidationResult(
                provider=facts.provider,
                configured=False,
                ok=False,
                status="unconfigured",
                message="Provider is not configured.",
                source=None if models is None else models.source,
                last_error=None if models is None else models.last_error,
                discovery_mode=None if models is None else models.discovery_mode,
                failure_kind="missing_auth",
                guidance=unconfigured_provider_guidance(facts.provider),
            )
        if facts.auth_present is False:
            return ProviderValidationResult(
                provider=facts.provider,
                configured=True,
                ok=False,
                status=facts.auth_failure_kind or "missing_auth",
                message=facts.auth_message or "Provider authentication is missing.",
                failure_kind=facts.auth_failure_kind or "missing_auth",
                guidance=facts.auth_message or missing_credentials_guidance(facts.provider),
            )
        if models is None:
            raise ValueError("provider validation requires model discovery facts")
        if models.last_refresh_status == "failed":
            return ProviderValidationResult(
                provider=facts.provider,
                configured=True,
                ok=False,
                status="failed",
                message=models.last_error or "Provider credential validation failed.",
                source=models.source,
                last_error=models.last_error,
                discovery_mode=models.discovery_mode,
                failure_kind="transient_failure",
                guidance=guidance_for_provider_error_kind("transient_failure"),
            )
        status = models.last_refresh_status or "unavailable"
        ok = status == "ok"
        return ProviderValidationResult(
            provider=facts.provider,
            configured=True,
            ok=ok,
            status=status,
            message=("Remote provider validation succeeded." if ok else "Provider credentials are configured; remote validation is unavailable."),
            source=models.source,
            last_error=models.last_error,
            discovery_mode=models.discovery_mode,
            guidance=("Provider model discovery succeeded." if ok else "Credentials are present, but remote validation could not confirm readiness."),
        )


class ProviderSummaryProjector:
    """Build stable provider summary contracts from runtime-resolved facts."""

    @staticmethod
    def project_one(
        provider_name: str,
        *,
        current_provider: str | None,
        label_for: Callable[[str], str],
        is_configured: Callable[[str], bool],
    ) -> ProviderSummary:
        return ProviderSummary(
            name=provider_name,
            label=label_for(provider_name),
            configured=is_configured(provider_name),
            current=provider_name == current_provider,
        )

    def project_all(
        self,
        provider_names: Iterable[str],
        *,
        current_provider: str | None,
        label_for: Callable[[str], str],
        is_configured: Callable[[str], bool],
    ) -> tuple[ProviderSummary, ...]:
        return tuple(
            sorted(
                (
                    self.project_one(
                        provider_name,
                        current_provider=current_provider,
                        label_for=label_for,
                        is_configured=is_configured,
                    )
                    for provider_name in provider_names
                ),
                key=lambda item: item.name,
            )
        )


__all__ = [
    "ProviderAuthPresence",
    "ProviderEndpointFacts",
    "ProviderReadinessFacts",
    "ProviderSummaryProjector",
    "ProviderValidationFacts",
    "RuntimeProviderAuthInspector",
    "RuntimeProviderEndpointInspector",
    "RuntimeProviderReadinessProjector",
    "RuntimeProviderValidationProjector",
    "missing_credentials_guidance",
    "missing_model_guidance",
    "provider_config_entry",
    "provider_credentials_config_path",
    "unconfigured_provider_guidance",
]
