from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from .config import (
    AnthropicProviderConfig,
    CopilotProviderConfig,
    GoogleProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderConfigs,
    ProviderConfigsPayload,
    ProviderEndpointConfig,
    _AnthropicProviderConfigPayload,
    _OpenAICompatibleProviderConfigPayload,
    _ProviderEndpointConfigPayload,
)
from .naming import canonical_provider_id

type ProviderAuthProvider = str
type ProviderErrorKind = Literal[
    "missing_auth",
    "rate_limit",
    "context_limit",
    "invalid_model",
    "transient_failure",
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

    def __post_init__(self) -> None:
        # ``MiniMax`` and ``minimax`` are the same provider id: canonicalise once,
        # at the boundary, so every branch below compares one spelling.
        object.__setattr__(self, "provider", canonical_provider_id(self.provider))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", canonical_provider_id(self.provider))


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


_OPENAI_METHODS: tuple[ProviderAuthMethod, ...] = (ProviderAuthMethod(id="api_key", label="API Key"),)
_ANTHROPIC_METHODS: tuple[ProviderAuthMethod, ...] = (ProviderAuthMethod(id="api_key", label="API Key"),)
_GOOGLE_METHODS: tuple[ProviderAuthMethod, ...] = (
    ProviderAuthMethod(id="api_key", label="API Key"),
    ProviderAuthMethod(id="oauth", label="OAuth", requires_callback=True),
    ProviderAuthMethod(id="service_account", label="Service Account"),
)
_COPILOT_METHODS: tuple[ProviderAuthMethod, ...] = (
    ProviderAuthMethod(id="token", label="Token"),
    ProviderAuthMethod(id="oauth", label="OAuth", requires_callback=True),
)
_ENDPOINT_AUTH_METHODS: tuple[ProviderAuthMethod, ...] = (
    ProviderAuthMethod(id="api_key", label="API Key"),
    ProviderAuthMethod(id="none", label="No Auth"),
)
_OPENAI_COMPATIBLE_AUTH_METHODS: tuple[ProviderAuthMethod, ...] = (ProviderAuthMethod(id="api_key", label="API Key"),)


def _endpoint_default_method(config: ProviderEndpointConfig) -> str:
    """Default auth method for an endpoint-shaped provider: its configured credential decides."""
    if config.auth_scheme == "none" or config.api_key is None:
        return "none"
    return "api_key"


def _provider_id_field_map(payload_type: type) -> Mapping[str, str]:
    """Canonical provider id -> payload/``ProviderConfigs`` field name for one payload shape."""
    return {
        canonical_provider_id(model_field.validation_alias if isinstance(model_field.validation_alias, str) else field_name): field_name
        for field_name, model_field in ProviderConfigsPayload.model_fields.items()
        if model_field.annotation == (payload_type | None)
    }


#: Endpoint-shaped built-ins: canonical provider id -> ``ProviderConfigs`` field.
ENDPOINT_SHAPED_BUILTIN_FIELDS: Mapping[str, str] = _provider_id_field_map(_ProviderEndpointConfigPayload)
ENDPOINT_SHAPED_BUILTIN_IDS: frozenset[str] = frozenset(ENDPOINT_SHAPED_BUILTIN_FIELDS)

#: OpenAI-compatible built-ins: canonical provider id -> ``ProviderConfigs`` field.
OPENAI_COMPATIBLE_BUILTIN_FIELDS: Mapping[str, str] = _provider_id_field_map(_OpenAICompatibleProviderConfigPayload)

#: Anthropic-wire built-ins: canonical provider id -> ``ProviderConfigs`` field.
ANTHROPIC_COMPATIBLE_BUILTIN_FIELDS: Mapping[str, str] = _provider_id_field_map(_AnthropicProviderConfigPayload)


class ProviderAuthResolver:
    def __init__(
        self,
        *,
        providers: ProviderConfigs | None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._providers = providers or ProviderConfigs()
        self._env: Mapping[str, str] = {} if env is None else env
        self._pending_callback_states: dict[str, tuple[str, str]] = {}

    def _custom_provider_config(self, provider: str) -> ProviderEndpointConfig | None:
        return self._providers.custom.get(provider)

    def _endpoint_provider_config(self, provider: str) -> ProviderEndpointConfig | None:
        """Config for the endpoint-shaped built-ins, by provider id.

        ``None`` means "not one of these provider ids": an endpoint-shaped
        built-in whose config block is absent still answers here, with an empty
        config, so it is reported as missing credentials instead of falling
        through to the custom-provider branch.
        """
        field_name = ENDPOINT_SHAPED_BUILTIN_FIELDS.get(provider)
        if field_name is None:
            return None
        return getattr(self._providers, field_name) or ProviderEndpointConfig()

    def _openai_compatible_provider_config(self, provider: str) -> OpenAICompatibleProviderConfig | None:
        """Config for a known OpenAI-compatible provider, empty when unconfigured.

        ``None`` means "not one of these provider ids": a built-in provider whose
        config block is absent still answers here, with an empty config, so it is
        reported as missing credentials instead of as an unsupported provider.
        """
        field_name = OPENAI_COMPATIBLE_BUILTIN_FIELDS.get(provider)
        if field_name is None:
            return None
        return getattr(self._providers, field_name) or OpenAICompatibleProviderConfig()

    def _anthropic_wire_provider_config(self, provider: str) -> AnthropicProviderConfig | None:
        """Config for a known Anthropic-wire provider, empty when unconfigured.

        ``None`` means "not one of these provider ids": a built-in provider whose
        config block is absent still answers here, with an empty config, so it is
        reported as missing credentials instead of as an unsupported provider.
        """
        field_name = ANTHROPIC_COMPATIBLE_BUILTIN_FIELDS.get(provider)
        if field_name is None:
            return None
        return getattr(self._providers, field_name) or AnthropicProviderConfig()

    def methods(self, provider: ProviderAuthProvider) -> ProviderAuthMethodsResponse:
        provider = canonical_provider_id(provider)
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
        endpoint_config = self._endpoint_provider_config(provider)
        if endpoint_config is not None:
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_ENDPOINT_AUTH_METHODS,
                default_method=_endpoint_default_method(endpoint_config),
            )
        if self._openai_compatible_provider_config(provider) is not None:
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_OPENAI_COMPATIBLE_AUTH_METHODS,
                default_method="api_key",
            )
        if self._anthropic_wire_provider_config(provider) is not None:
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_ANTHROPIC_METHODS,
                default_method="api_key",
            )
        custom_config = self._custom_provider_config(provider)
        if custom_config is not None:
            return ProviderAuthMethodsResponse(
                provider=provider,
                methods=_ENDPOINT_AUTH_METHODS,
                default_method=_endpoint_default_method(custom_config),
            )
        raise self._error(
            provider=provider,
            code="unsupported_provider",
            kind="invalid_model",
            message=f"provider auth provider '{provider}' is not supported",
        )

    def authorize(self, request: ProviderAuthAuthorizeRequest) -> ProviderAuthAuthorizeResult:
        if request.provider == "openai":
            openai_config = self._providers.openai
            return self._authorize_api_key(
                request=request,
                provider_name="openai",
                config_api_key=None if openai_config is None else openai_config.api_key,
            )
        if request.provider == "anthropic":
            anthropic_config = self._providers.anthropic
            return self._authorize_api_key(
                request=request,
                provider_name="anthropic",
                config_api_key=None if anthropic_config is None else anthropic_config.api_key,
                api_key_header="x-api-key",
            )
        if request.provider == "google":
            return self._authorize_google(request)
        if request.provider == "copilot":
            return self._authorize_copilot(request)
        endpoint_config = self._endpoint_provider_config(request.provider)
        if endpoint_config is not None:
            return self._authorize_endpoint_compatible(
                request=request,
                provider_name=request.provider,
                provider_config=endpoint_config,
            )
        compatible_config = self._openai_compatible_provider_config(request.provider)
        if compatible_config is not None:
            return self._authorize_api_key(
                request=request,
                provider_name=request.provider,
                config_api_key=compatible_config.api_key,
            )
        anthropic_wire_config = self._anthropic_wire_provider_config(request.provider)
        if anthropic_wire_config is not None:
            return self._authorize_api_key(
                request=request,
                provider_name=request.provider,
                config_api_key=anthropic_wire_config.api_key,
                api_key_header="x-api-key",
            )
        custom_config = self._custom_provider_config(request.provider)
        if custom_config is not None:
            return self._authorize_endpoint_compatible(
                request=request,
                provider_name=request.provider,
                provider_config=custom_config,
            )
        raise self._error(
            provider=request.provider,
            code="unsupported_provider",
            kind="invalid_model",
            message=f"provider auth provider '{request.provider}' is not supported",
        )

    def _authorize_endpoint_compatible(
        self,
        *,
        request: ProviderAuthAuthorizeRequest,
        provider_name: str,
        provider_config: ProviderEndpointConfig | None,
    ) -> ProviderAuthAuthorizeResult:
        payload = {} if request.payload is None else dict(request.payload)
        method = request.method
        if method is None:
            method = "none" if provider_config is None else _endpoint_default_method(provider_config)
        if method not in {"api_key", "none"}:
            raise self._error(
                provider=provider_name,
                code="unsupported_method",
                kind="invalid_model",
                message=(f"provider auth method '{method}' for provider '{provider_name}' must be one of: api_key, none"),
            )

        if method == "none":
            return ProviderAuthAuthorizeResult(
                provider=provider_name,
                method=method,
                status="authorized",
                material=ProviderAuthMaterial(
                    provider=provider_name,
                    method=method,
                    headers={},
                    metadata={},
                ),
            )

        token = self._optional_payload_str(
            payload,
            key="api_key",
            field_path="provider auth authorize payload.api_key",
            provider=provider_name,
        )
        if token is None and provider_config is not None:
            token = provider_config.api_key
        if token is None:
            raise self._error(
                provider=provider_name,
                code="missing_credentials",
                kind="missing_auth",
                message=(f"provider auth field '{provider_name}.api_key' must be provided for {provider_name} api_key auth"),
            )
        return ProviderAuthAuthorizeResult(
            provider=provider_name,
            method=method,
            status="authorized",
            material=self._api_key_material(provider_name, method, token),
        )

    def _authorize_api_key(
        self,
        *,
        request: ProviderAuthAuthorizeRequest,
        provider_name: str,
        config_api_key: str | None,
        api_key_header: str = "Authorization",
    ) -> ProviderAuthAuthorizeResult:
        method = self._resolve_method(request, default_method="api_key", allowed_methods={"api_key"})
        payload = {} if request.payload is None else dict(request.payload)
        token = self._resolve_api_key(
            payload=payload,
            field_name="api_key",
            config_value=config_api_key,
            provider=provider_name,
        )
        return ProviderAuthAuthorizeResult(
            provider=provider_name,
            method=method,
            status="authorized",
            material=self._api_key_material(provider_name, method, token, header_name=api_key_header),
        )

    def callback(self, request: ProviderAuthCallbackRequest) -> ProviderAuthMaterial:
        callback_supported = (request.provider == "google" and request.method == "oauth") or (
            request.provider == "copilot" and request.method == "oauth"
        )
        if not callback_supported:
            raise self._error(
                provider=request.provider,
                code="callback_not_supported",
                kind="invalid_model",
                message=(f"provider auth callback is not supported for provider '{request.provider}' method '{request.method}'"),
            )

        if not self._validate_callback_state(request.state, request.provider, request.method):
            raise self._error(
                provider=request.provider,
                code="invalid_state",
                kind="invalid_model",
                message=(f"provider auth callback state for provider '{request.provider}' and method '{request.method}' is invalid"),
            )
        self._pending_callback_states.pop(request.state, None)

        payload = {} if request.payload is None else dict(request.payload)
        if request.provider == "google" and request.method == "oauth":
            token = self._required_payload_str(
                payload,
                key="access_token",
                field_path="provider auth callback payload.access_token",
                provider=request.provider,
            )
            return self._api_key_material(request.provider, request.method, token)
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
            message=(f"provider auth callback is not supported for provider '{request.provider}' method '{request.method}'"),
        )

    def _authorize_google(self, request: ProviderAuthAuthorizeRequest) -> ProviderAuthAuthorizeResult:
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
                    kind="missing_auth",
                    message=("provider auth field 'google.service_account_json_path' must be provided for google service_account auth"),
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
                material=self._api_key_material("google", method, access_token),
            )
        return ProviderAuthAuthorizeResult(
            provider="google",
            method=method,
            status="needs_callback",
            callback=ProviderAuthCallback(
                state=self._new_callback_state("google", "oauth"),
                instructions="exchange Google OAuth code for access_token and call callback",
            ),
        )

    def _authorize_copilot(self, request: ProviderAuthAuthorizeRequest) -> ProviderAuthAuthorizeResult:
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
                    kind="missing_auth",
                    message=("provider auth field 'copilot.token' must be provided for copilot token auth"),
                )
            return ProviderAuthAuthorizeResult(
                provider="copilot",
                method=method,
                status="authorized",
                material=self._api_key_material("copilot", method, token),
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
                    state=self._new_callback_state("copilot", "oauth"),
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
            kind="missing_auth",
            message=(f"provider auth field '{provider}.{field_name}' must be provided for {provider} api_key auth"),
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
                message=(f"provider auth method '{method}' for provider '{request.provider}' must be one of: {allowed}"),
            )
        if configured_method is not None and request.method is not None and request.method != configured_method:
            raise self._error(
                provider=request.provider,
                code="invalid_payload",
                kind="invalid_model",
                message=(
                    f"provider auth method '{request.method}' for provider '{request.provider}' must match configured method '{configured_method}'"
                ),
            )
        return method

    def _optional_payload_str(
        self,
        payload: dict[str, object],
        *,
        key: str,
        field_path: str,
        provider: str,
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
        provider: str,
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
                kind="missing_auth",
                message=f"{field_path} must be provided",
            )
        return value

    def _new_callback_state(self, provider: str, method: str) -> str:
        state = f"voidcode:{provider}:{method}:callback:{uuid4().hex}"
        self._pending_callback_states[state] = (provider, method)
        return state

    def _validate_callback_state(self, state: str, provider: str, method: str) -> bool:
        expected = self._pending_callback_states.get(state)
        return expected == (provider, method)

    @staticmethod
    def _api_key_material(
        provider: str,
        method: str,
        token: str,
        *,
        header_name: str = "Authorization",
    ) -> ProviderAuthMaterial:
        """Auth material carrying one token: a bearer header unless another header is named."""
        headers = {"Authorization": f"Bearer {token}"} if header_name == "Authorization" else {header_name: token}
        return ProviderAuthMaterial(
            provider=provider,
            method=method,
            headers=headers,
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
