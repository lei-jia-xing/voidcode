from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .config import (
    CopilotProviderConfig,
    GoogleProviderConfig,
    ProviderConfigs,
)

type ProviderAuthProvider = Literal["openai", "anthropic", "google", "copilot"]
type ProviderErrorKind = Literal[
    "rate_limit", "context_limit", "invalid_model", "transient_failure"
]


@dataclass(frozen=True, slots=True)
class ProviderAuthMethod:
    id: str
    label: str
    requires_callback: bool = False


@dataclass(frozen=True, slots=True)
class ProviderAuthMethodsResponse:
    provider: ProviderAuthProvider
    methods: tuple[ProviderAuthMethod, ...]
    default_method: str


@dataclass(frozen=True, slots=True)
class ProviderAuthMaterial:
    provider: ProviderAuthProvider
    method: str
    headers: Mapping[str, str]
    metadata: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ProviderAuthCallback:
    state: str
    instructions: str


@dataclass(frozen=True, slots=True)
class ProviderAuthAuthorizeRequest:
    provider: ProviderAuthProvider
    method: str | None = None
    payload: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ProviderAuthAuthorizeResult:
    provider: ProviderAuthProvider
    method: str
    status: Literal["authorized", "needs_callback"]
    material: ProviderAuthMaterial | None = None
    callback: ProviderAuthCallback | None = None


@dataclass(frozen=True, slots=True)
class ProviderAuthCallbackRequest:
    provider: ProviderAuthProvider
    method: str
    state: str
    payload: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ProviderAuthResolutionError(ValueError):
    provider: str
    code: Literal[
        "unsupported_provider",
        "unsupported_method",
        "missing_credentials",
        "invalid_payload",
        "invalid_state",
        "callback_not_supported",
        "invalid_credentials",
    ]
    provider_error_kind: ProviderErrorKind
    message: str

    def __str__(self) -> str:
        return self.message


_OPENAI_METHODS: tuple[ProviderAuthMethod, ...] = (
    ProviderAuthMethod(id="api_key", label="API Key"),
)
_ANTHROPIC_METHODS: tuple[ProviderAuthMethod, ...] = (
    ProviderAuthMethod(id="api_key", label="API Key"),
)
_GOOGLE_METHODS: tuple[ProviderAuthMethod, ...] = (
    ProviderAuthMethod(id="api_key", label="API Key"),
    ProviderAuthMethod(id="oauth", label="OAuth", requires_callback=True),
    ProviderAuthMethod(id="service_account", label="Service Account"),
)
_COPILOT_METHODS: tuple[ProviderAuthMethod, ...] = (
    ProviderAuthMethod(id="token", label="Token"),
    ProviderAuthMethod(id="oauth", label="OAuth", requires_callback=True),
)


class ProviderAuthResolver:
    def __init__(
        self,
        *,
        providers: ProviderConfigs | None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._providers = providers or ProviderConfigs()
        self._env: Mapping[str, str] = {} if env is None else env

    def methods(self, provider: ProviderAuthProvider) -> ProviderAuthMethodsResponse:
        if provider == "openai":
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_OPENAI_METHODS,
                default_method="api_key",
            )
        if provider == "anthropic":
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_ANTHROPIC_METHODS,
                default_method="api_key",
            )
        if provider == "google":
            configured = None
            if self._providers.google is not None and self._providers.google.auth is not None:
                configured = self._providers.google.auth.method
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_GOOGLE_METHODS,
                default_method=configured or "api_key",
            )
        if provider == "copilot":
            configured = None
            if self._providers.copilot is not None and self._providers.copilot.auth is not None:
                configured = self._providers.copilot.auth.method
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_COPILOT_METHODS,
                default_method=configured or "token",
            )
        raise self._error(
            provider=provider,
            code="unsupported_provider",
            kind="invalid_model",
            message=f"provider auth provider '{provider}' is not supported",
        )

    def authorize(self, request: ProviderAuthAuthorizeRequest) -> ProviderAuthAuthorizeResult:
        if request.provider == "openai":
            return self._authorize_openai(request)
        if request.provider == "anthropic":
            return self._authorize_anthropic(request)
        if request.provider == "google":
            return self._authorize_google(request)
        if request.provider == "copilot":
            return self._authorize_copilot(request)
        raise self._error(
            provider=request.provider,
            code="unsupported_provider",
            kind="invalid_model",
            message=f"provider auth provider '{request.provider}' is not supported",
        )

    def callback(self, request: ProviderAuthCallbackRequest) -> ProviderAuthMaterial:
        expected_state = self._callback_state(request.provider, request.method)
        if request.state != expected_state:
            raise self._error(
                provider=request.provider,
                code="invalid_state",
                kind="invalid_model",
                message=(
                    f"provider auth callback state for provider '{request.provider}' "
                    f"and method '{request.method}' is invalid"
                ),
            )

        payload = {} if request.payload is None else dict(request.payload)
        if request.provider == "google" and request.method == "oauth":
            token = self._required_payload_str(
                payload,
                key="access_token",
                field_path="provider auth callback payload.access_token",
                provider=request.provider,
            )
            return self._bearer_material(request.provider, request.method, token)
        if request.provider == "copilot" and request.method == "oauth":
            token = self._required_payload_str(
                payload,
                key="token",
                field_path="provider auth callback payload.token",
                provider=request.provider,
            )
            refresh = self._optional_payload_str(
                payload,
                key="refresh_token",
                field_path="provider auth callback payload.refresh_token",
                provider=request.provider,
            )
            metadata: dict[str, str] = {}
            if refresh is not None:
                metadata["refresh_token"] = refresh
            return ProviderAuthMaterial(
                provider=request.provider,
                method=request.method,
                headers={"Authorization": f"Bearer {token}"},
                metadata=metadata,
            )

        raise self._error(
            provider=request.provider,
            code="callback_not_supported",
            kind="invalid_model",
            message=(
                f"provider auth callback is not supported for provider '{request.provider}' "
                f"method '{request.method}'"
            ),
        )

    def _authorize_openai(
        self, request: ProviderAuthAuthorizeRequest
    ) -> ProviderAuthAuthorizeResult:
        method = self._resolve_method(
            request, default_method="api_key", allowed_methods={"api_key"}
        )
        provider_config = self._providers.openai
        payload = {} if request.payload is None else dict(request.payload)
        token = self._resolve_api_key(
            payload=payload,
            field_name="api_key",
            config_value=None if provider_config is None else provider_config.api_key,
            provider="openai",
        )
        return ProviderAuthAuthorizeResult(
            provider="openai",
            method=method,
            status="authorized",
            material=self._bearer_material("openai", method, token),
        )

    def _authorize_anthropic(
        self, request: ProviderAuthAuthorizeRequest
    ) -> ProviderAuthAuthorizeResult:
        method = self._resolve_method(
            request, default_method="api_key", allowed_methods={"api_key"}
        )
        provider_config = self._providers.anthropic
        payload = {} if request.payload is None else dict(request.payload)
        token = self._resolve_api_key(
            payload=payload,
            field_name="api_key",
            config_value=None if provider_config is None else provider_config.api_key,
            provider="anthropic",
        )
        return ProviderAuthAuthorizeResult(
            provider="anthropic",
            method=method,
            status="authorized",
            material=ProviderAuthMaterial(
                provider="anthropic",
                method=method,
                headers={"x-api-key": token},
                metadata={},
            ),
        )

    def _authorize_google(
        self, request: ProviderAuthAuthorizeRequest
    ) -> ProviderAuthAuthorizeResult:
        provider_config: GoogleProviderConfig | None = self._providers.google
        configured_method = None
        if provider_config is not None and provider_config.auth is not None:
            configured_method = provider_config.auth.method
        method = self._resolve_method(
            request,
            default_method=configured_method or "api_key",
            allowed_methods={"api_key", "oauth", "service_account"},
            configured_method=configured_method,
        )
        payload = {} if request.payload is None else dict(request.payload)
        auth = None if provider_config is None else provider_config.auth

        if method == "api_key":
            token = self._resolve_api_key(
                payload=payload,
                field_name="api_key",
                config_value=None if auth is None else auth.api_key,
                provider="google",
            )
            return ProviderAuthAuthorizeResult(
                provider="google",
                method=method,
                status="authorized",
                material=ProviderAuthMaterial(
                    provider="google",
                    method=method,
                    headers={},
                    metadata={"api_key": token},
                ),
            )

        if method == "service_account":
            service_account_path = self._optional_payload_str(
                payload,
                key="service_account_json_path",
                field_path="provider auth authorize payload.service_account_json_path",
                provider="google",
            )
            if service_account_path is None and auth is not None:
                service_account_path = auth.service_account_json_path
            if service_account_path is None:
                raise self._error(
                    provider="google",
                    code="missing_credentials",
                    kind="invalid_model",
                    message=(
                        "provider auth field 'google.service_account_json_path' "
                        "must be provided for google service_account auth"
                    ),
                )
            return ProviderAuthAuthorizeResult(
                provider="google",
                method=method,
                status="authorized",
                material=ProviderAuthMaterial(
                    provider="google",
                    method=method,
                    headers={},
                    metadata={"service_account_json_path": service_account_path},
                ),
            )

        access_token = self._optional_payload_str(
            payload,
            key="access_token",
            field_path="provider auth authorize payload.access_token",
            provider="google",
        )
        if access_token is None and auth is not None:
            access_token = auth.access_token
        if access_token is not None:
            return ProviderAuthAuthorizeResult(
                provider="google",
                method=method,
                status="authorized",
                material=self._bearer_material("google", method, access_token),
            )
        return ProviderAuthAuthorizeResult(
            provider="google",
            method=method,
            status="needs_callback",
            callback=ProviderAuthCallback(
                state=self._callback_state("google", "oauth"),
                instructions="exchange Google OAuth code for access_token and call callback",
            ),
        )

    def _authorize_copilot(
        self, request: ProviderAuthAuthorizeRequest
    ) -> ProviderAuthAuthorizeResult:
        provider_config: CopilotProviderConfig | None = self._providers.copilot
        configured_method = None
        if provider_config is not None and provider_config.auth is not None:
            configured_method = provider_config.auth.method
        method = self._resolve_method(
            request,
            default_method=configured_method or "token",
            allowed_methods={"token", "oauth"},
            configured_method=configured_method,
        )
        payload = {} if request.payload is None else dict(request.payload)
        auth = None if provider_config is None else provider_config.auth

        token = self._optional_payload_str(
            payload,
            key="token",
            field_path="provider auth authorize payload.token",
            provider="copilot",
        )
        if token is None and auth is not None and auth.token is not None:
            token = auth.token
        if token is None and auth is not None and auth.token_env_var is not None:
            token = self._env.get(auth.token_env_var)

        if method == "token":
            if token is None:
                raise self._error(
                    provider="copilot",
                    code="missing_credentials",
                    kind="invalid_model",
                    message=(
                        "provider auth field 'copilot.token' "
                        "must be provided for copilot token auth"
                    ),
                )
            return ProviderAuthAuthorizeResult(
                provider="copilot",
                method=method,
                status="authorized",
                material=self._bearer_material("copilot", method, token),
            )

        refresh = self._optional_payload_str(
            payload,
            key="refresh_token",
            field_path="provider auth authorize payload.refresh_token",
            provider="copilot",
        )
        if refresh is None and auth is not None:
            refresh = auth.refresh_token
        if token is None:
            return ProviderAuthAuthorizeResult(
                provider="copilot",
                method=method,
                status="needs_callback",
                callback=ProviderAuthCallback(
                    state=self._callback_state("copilot", "oauth"),
                    instructions="exchange Copilot OAuth code for token and call callback",
                ),
            )

        metadata: dict[str, str] = {}
        if refresh is not None:
            metadata["refresh_token"] = refresh
        return ProviderAuthAuthorizeResult(
            provider="copilot",
            method=method,
            status="authorized",
            material=ProviderAuthMaterial(
                provider="copilot",
                method=method,
                headers={"Authorization": f"Bearer {token}"},
                metadata=metadata,
            ),
        )

    def _resolve_api_key(
        self,
        *,
        payload: dict[str, object],
        field_name: str,
        config_value: str | None,
        provider: ProviderAuthProvider,
    ) -> str:
        payload_value = self._optional_payload_str(
            payload,
            key=field_name,
            field_path=f"provider auth authorize payload.{field_name}",
            provider=provider,
        )
        if payload_value is not None:
            return payload_value
        if config_value is not None:
            return config_value
        raise self._error(
            provider=provider,
            code="missing_credentials",
            kind="invalid_model",
            message=(
                f"provider auth field '{provider}.{field_name}' "
                f"must be provided for {provider} api_key auth"
            ),
        )

    def _resolve_method(
        self,
        request: ProviderAuthAuthorizeRequest,
        *,
        default_method: str,
        allowed_methods: set[str],
        configured_method: str | None = None,
    ) -> str:
        method = request.method or default_method
        if method not in allowed_methods:
            allowed = ", ".join(sorted(allowed_methods))
            raise self._error(
                provider=request.provider,
                code="unsupported_method",
                kind="invalid_model",
                message=(
                    f"provider auth method '{method}' for provider '{request.provider}' "
                    f"must be one of: {allowed}"
                ),
            )
        if (
            configured_method is not None
            and request.method is not None
            and request.method != configured_method
        ):
            raise self._error(
                provider=request.provider,
                code="invalid_payload",
                kind="invalid_model",
                message=(
                    f"provider auth method '{request.method}' for provider '{request.provider}' "
                    f"must match configured method '{configured_method}'"
                ),
            )
        return method

    def _optional_payload_str(
        self,
        payload: dict[str, object],
        *,
        key: str,
        field_path: str,
        provider: ProviderAuthProvider,
    ) -> str | None:
        raw = payload.get(key)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise self._error(
                provider=provider,
                code="invalid_payload",
                kind="invalid_model",
                message=f"{field_path} must be a string when provided",
            )
        return raw

    def _required_payload_str(
        self,
        payload: dict[str, object],
        *,
        key: str,
        field_path: str,
        provider: ProviderAuthProvider,
    ) -> str:
        value = self._optional_payload_str(
            payload,
            key=key,
            field_path=field_path,
            provider=provider,
        )
        if value is None:
            raise self._error(
                provider=provider,
                code="missing_credentials",
                kind="invalid_model",
                message=f"{field_path} must be provided",
            )
        return value

    @staticmethod
    def _callback_state(provider: str, method: str) -> str:
        return f"voidcode:{provider}:{method}:callback"

    @staticmethod
    def _bearer_material(
        provider: ProviderAuthProvider, method: str, token: str
    ) -> ProviderAuthMaterial:
        return ProviderAuthMaterial(
            provider=provider,
            method=method,
            headers={"Authorization": f"Bearer {token}"},
            metadata={},
        )

    @staticmethod
    def _error(
        *,
        provider: str,
        code: Literal[
            "unsupported_provider",
            "unsupported_method",
            "missing_credentials",
            "invalid_payload",
            "invalid_state",
            "callback_not_supported",
            "invalid_credentials",
        ],
        kind: ProviderErrorKind,
        message: str,
    ) -> ProviderAuthResolutionError:
        return ProviderAuthResolutionError(
            provider=provider,
            code=code,
            provider_error_kind=kind,
            message=message,
        )


def provider_auth_error_to_execution_kind(error: ProviderAuthResolutionError) -> ProviderErrorKind:
    return error.provider_error_kind
