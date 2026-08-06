from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from ..provider.auth import (
    ProviderAuthAuthorizeRequest,
    ProviderAuthResolutionError,
    ProviderAuthResolver,
)
from ..provider.config import ProviderConfigs
from ..provider.errors import guidance_for_provider_error_kind
from .contracts import (
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
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
        providers = self._providers
        if providers is None:
            return False
        configured = {
            "openai": providers.openai,
            "anthropic": providers.anthropic,
            "google": providers.google,
            "copilot": providers.copilot,
            "litellm": providers.litellm,
            "deepseek": providers.deepseek,
            "glm": providers.glm,
            "grok": providers.grok,
            "minimax": providers.minimax,
            "kimi": providers.kimi,
            "opencode": providers.opencode,
            "opencode-go": providers.opencode_go,
            "qwen": providers.qwen,
        }
        if provider_name in configured:
            return configured[provider_name] is not None
        return provider_name in providers.custom

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
            guidance = "Configure a provider/model, for example model: 'openai/gpt-4o'."
        elif facts.auth_present is False and facts.auth_failure_kind == "invalid_model":
            status = facts.auth_failure_kind
            ok = False
            guidance = facts.auth_message or guidance_for_provider_error_kind("invalid_model")
        elif not facts.configured:
            status = "unconfigured"
            ok = False
            guidance = "Add provider credentials in environment variables or .voidcode.json."
        elif facts.auth_present is False:
            status = facts.auth_failure_kind or "missing_auth"
            ok = False
            guidance = facts.auth_message or guidance_for_provider_error_kind("missing_auth")
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
                guidance="Add provider credentials in environment variables or .voidcode.json.",
            )
        if facts.auth_present is False:
            return ProviderValidationResult(
                provider=facts.provider,
                configured=True,
                ok=False,
                status=facts.auth_failure_kind or "missing_auth",
                message=facts.auth_message or "Provider authentication is missing.",
                failure_kind=facts.auth_failure_kind or "missing_auth",
                guidance=facts.auth_message or guidance_for_provider_error_kind("missing_auth"),
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
        status = models.last_refresh_status or "ok"
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
    "ProviderReadinessFacts",
    "ProviderSummaryProjector",
    "ProviderValidationFacts",
    "RuntimeProviderAuthInspector",
    "RuntimeProviderReadinessProjector",
    "RuntimeProviderValidationProjector",
]
