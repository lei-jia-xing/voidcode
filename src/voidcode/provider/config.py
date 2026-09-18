from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.functional_validators import BeforeValidator

from .naming import (
    BUILTIN_PROVIDER_IDS,
    UnknownProviderIdError,
    canonical_provider_id,
)


def _parse_optional_boundary_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string when provided")
    return value


def _parse_required_boundary_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("must be a string")
    return value


def _parse_optional_boundary_timeout(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError("must be a number greater than 0 when provided")
    return float(value)


def _parse_optional_boundary_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("must be an integer greater than or equal to 1 when provided")
    return value


def _parse_optional_boundary_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("must be an integer greater than or equal to 0 when provided")
    return value


def _parse_optional_boundary_nonnegative_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise ValueError("must be a number greater than or equal to 0 when provided")
    return float(value)


def _parse_optional_boundary_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("must be a boolean when provided")
    return value


def _parse_boundary_string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("must be an array when provided")
    parsed_items: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str):
            raise ValueError(f"[{index}] must be a string")
        parsed_items.append(item)
    return tuple(parsed_items)


def _parse_boundary_string_mapping(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("must be an object when provided")
    mapping: dict[str, str] = {}
    for raw_key, raw_item in cast(dict[object, object], value).items():
        if not isinstance(raw_key, str):
            raise ValueError(" keys must be strings")
        if not raw_key:
            raise ValueError(" keys must not be empty")
        if not isinstance(raw_item, str):
            raise ValueError(f".{raw_key} must be a string")
        if not raw_item:
            raise ValueError(f".{raw_key} must not be empty")
        mapping[raw_key] = raw_item
    return mapping


BoundaryOptionalString = Annotated[str | None, BeforeValidator(_parse_optional_boundary_string)]
BoundaryRequiredString = Annotated[str, BeforeValidator(_parse_required_boundary_string)]
BoundaryOptionalTimeout = Annotated[float | None, BeforeValidator(_parse_optional_boundary_timeout)]
BoundaryOptionalPositiveInt = Annotated[int | None, BeforeValidator(_parse_optional_boundary_positive_int)]
BoundaryOptionalNonnegativeInt = Annotated[int | None, BeforeValidator(_parse_optional_boundary_nonnegative_int)]
BoundaryOptionalNonnegativeFloat = Annotated[float | None, BeforeValidator(_parse_optional_boundary_nonnegative_float)]
BoundaryOptionalBool = Annotated[bool | None, BeforeValidator(_parse_optional_boundary_bool)]
BoundaryStringList = Annotated[tuple[str, ...], BeforeValidator(_parse_boundary_string_list)]
BoundaryStringMapping = Annotated[dict[str, str], BeforeValidator(_parse_boundary_string_mapping)]


def _prefer_primary[T](primary: T | None, fallback: T | None) -> T | None:
    return primary if primary is not None else fallback


class _ProviderPayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ProviderTransientRetryConfigPayload(_ProviderPayloadModel):
    max_retries: BoundaryOptionalNonnegativeInt = None
    base_delay_ms: BoundaryOptionalNonnegativeFloat = None
    max_delay_ms: BoundaryOptionalNonnegativeFloat = None
    jitter: BoundaryOptionalBool = None


class _OpenAIProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    organization: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _AnthropicProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    version: BoundaryOptionalString = None
    beta_headers: BoundaryStringList = ()
    cache_retention: Literal["none", "short", "long"] = "none"
    timeout_seconds: BoundaryOptionalTimeout = None
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _GoogleProviderAuthConfigPayload(_ProviderPayloadModel):
    method: BoundaryRequiredString
    api_key: BoundaryOptionalString = None
    access_token: BoundaryOptionalString = None
    service_account_json_path: BoundaryOptionalString = None


class _GoogleProviderConfigPayload(_ProviderPayloadModel):
    auth: _GoogleProviderAuthConfigPayload | None = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    region: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _CopilotProviderAuthConfigPayload(_ProviderPayloadModel):
    method: BoundaryRequiredString
    token: BoundaryOptionalString = None
    token_env_var: BoundaryOptionalString = None
    refresh_token: BoundaryOptionalString = None
    refresh_leeway_seconds: BoundaryOptionalPositiveInt = None


class _CopilotProviderConfigPayload(_ProviderPayloadModel):
    auth: _CopilotProviderAuthConfigPayload | None = None
    base_url: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _ProviderEndpointConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    auth_header: BoundaryOptionalString = None
    auth_scheme: BoundaryOptionalString = None
    ssl_verify: BoundaryOptionalBool = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _OpenAICompatibleProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    ssl_verify: BoundaryOptionalBool = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _ProviderConfigsPayload(_ProviderPayloadModel):
    openai: _OpenAIProviderConfigPayload | None = None
    anthropic: _AnthropicProviderConfigPayload | None = None
    google: _GoogleProviderConfigPayload | None = None
    copilot: _CopilotProviderConfigPayload | None = None
    endpoint: _ProviderEndpointConfigPayload | None = None
    opencode: _ProviderEndpointConfigPayload | None = None
    openrouter: _ProviderEndpointConfigPayload | None = None
    deepseek: _OpenAICompatibleProviderConfigPayload | None = None
    zai: _OpenAICompatibleProviderConfigPayload | None = None
    zhipuai: _OpenAICompatibleProviderConfigPayload | None = None
    grok: _OpenAICompatibleProviderConfigPayload | None = None
    minimax: _OpenAICompatibleProviderConfigPayload | None = None
    kimi: _OpenAICompatibleProviderConfigPayload | None = None
    opencode_go: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="opencode-go")
    qwen: _OpenAICompatibleProviderConfigPayload | None = None
    groq: _OpenAICompatibleProviderConfigPayload | None = None
    together: _OpenAICompatibleProviderConfigPayload | None = None
    fireworks: _OpenAICompatibleProviderConfigPayload | None = None
    mistral: _OpenAICompatibleProviderConfigPayload | None = None
    custom: dict[str, _ProviderEndpointConfigPayload] = Field(default_factory=dict)


class _ProviderFallbackPayload(_ProviderPayloadModel):
    preferred_model: BoundaryRequiredString
    fallback_models: BoundaryStringList = ()


def _provider_config_payload_keys() -> dict[str, str]:
    """Canonical provider id -> the ``providers`` payload key that carries it."""
    keys: dict[str, str] = {}
    for field_name, model_field in _ProviderConfigsPayload.model_fields.items():
        if field_name == "custom":
            continue
        payload_key = model_field.validation_alias if isinstance(model_field.validation_alias, str) else field_name
        keys[canonical_provider_id(payload_key)] = payload_key
    return keys


_PROVIDER_CONFIG_PAYLOAD_KEYS: Mapping[str, str] = _provider_config_payload_keys()

# Provider keys renamed after a session was persisted. The persisted parsing
# boundary migrates these; live config still rejects them, pointing at the new key.
_RENAMED_PROVIDER_KEY_REPLACEMENTS: Mapping[str, str] = {"litellm": "providers.endpoint"}


def _canonicalize_provider_config_payload_keys(
    raw_value: object,
    *,
    field_path: str,
) -> object:
    """Accept ``providers.<id>`` keys case-insensitively and canonicalise them.

    ``providers.MiniMax`` and ``providers.minimax`` are one entry, spelled either
    way. A key that is neither a built-in provider id nor ``custom`` fails loudly
    instead of being read as something else.
    """
    if not isinstance(raw_value, dict):
        # Not an object: leave the object-ness error to the payload model.
        return raw_value
    canonicalized: dict[str, object] = {}
    spelled: dict[str, str] = {}
    for raw_key, value in cast(dict[object, object], raw_value).items():
        if not isinstance(raw_key, str) or not raw_key:
            raise ValueError(f"{field_path} keys must be non-empty strings")
        payload_key = _canonical_provider_config_key(raw_key, field_path=field_path)
        previous = spelled.get(payload_key)
        if previous is not None and previous != raw_key:
            raise ValueError(
                f"{_nested_config_field(field_path, raw_key)} duplicates "
                f"{_nested_config_field(field_path, previous)}: provider ids are case-insensitive"
            )
        spelled[payload_key] = raw_key
        canonicalized[payload_key] = value
    return canonicalized


def _canonical_provider_config_key(raw_key: str, *, field_path: str) -> str:
    canonical_key = canonical_provider_id(raw_key)
    if canonical_key == "custom":
        return "custom"
    payload_key = _PROVIDER_CONFIG_PAYLOAD_KEYS.get(canonical_key)
    if payload_key is None:
        replacement = _RENAMED_PROVIDER_KEY_REPLACEMENTS.get(canonical_key)
        if replacement is not None:
            raise ValueError(f"{_nested_config_field(field_path, raw_key)} is not supported; use '{replacement}' instead")
        raise ValueError(f"{_nested_config_field(field_path, raw_key)}: {UnknownProviderIdError(raw_key).message}")
    return payload_key


# =============================================================================
# OpenAI-compatible provider configuration
# Provides one typed configuration shape for named OpenAI-compatible providers.
# =============================================================================


@dataclass(frozen=True, slots=True)
class ProviderTransientRetryConfig:
    max_retries: int = 3
    base_delay_ms: float = 1000.0
    max_delay_ms: float = 10_000.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")
        if self.base_delay_ms < 0:
            raise ValueError("base_delay_ms must be greater than or equal to 0")
        if self.max_delay_ms < 0:
            raise ValueError("max_delay_ms must be greater than or equal to 0")
        if self.max_delay_ms < self.base_delay_ms:
            raise ValueError("max_delay_ms must be greater than or equal to base_delay_ms")


DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG = ProviderTransientRetryConfig()


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProviderConfig:
    """Configuration shared by named OpenAI-compatible providers.

    Providers use a minimal API_KEY plus optional BASE_URL contract.
    """

    api_key: str | None = None
    api_key_env_var: str | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    ssl_verify: bool | None = None
    timeout_seconds: float | None = None
    model_map: dict[str, str] = field(default_factory=dict)
    transient_retry: ProviderTransientRetryConfig | None = None


# Default endpoint per provider. Only base URLs live here: the model list is
# always discovered from the provider so there is no second list to maintain.
_OPENAI_COMPATIBLE_DEFAULTS: dict[str, tuple[str, str | None]] = {
    "deepseek": (
        "https://api.deepseek.com",
        "https://api.deepseek.com",
    ),
    "zai": (
        "https://api.z.ai/api/paas/v4",
        "https://api.z.ai/api/paas/v4",
    ),
    "zhipuai": (
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/api/paas/v4",
    ),
    "grok": (
        "https://api.x.ai",
        "https://api.x.ai",
    ),
    "minimax": (
        "https://api.minimax.io",
        "",
    ),
    "kimi": (
        "https://api.moonshot.ai",
        "https://api.moonshot.ai/v1",
    ),
    "opencode-go": (
        "https://opencode.ai/zen/go",
        "",
    ),
    "qwen": (
        "https://dashscope.aliyuncs.com/compatible-mode",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "groq": (
        "https://api.groq.com/openai/v1",
        "https://api.groq.com/openai/v1",
    ),
    "together": (
        "https://api.together.ai/v1",
        "https://api.together.ai/v1",
    ),
    "fireworks": (
        "https://api.fireworks.ai/inference/v1",
        "",
    ),
    "mistral": (
        "https://api.mistral.ai/v1",
        "https://api.mistral.ai/v1",
    ),
}


_OPENAI_COMPATIBLE_PROVIDER_NAMES = frozenset(_OPENAI_COMPATIBLE_DEFAULTS)
_OPENAI_COMPATIBLE_PROVIDERS_USING_BASE_URL_DISCOVERY = frozenset({"deepseek"})


def openai_compatible_default_base_url(provider_name: str) -> str:
    default = _OPENAI_COMPATIBLE_DEFAULTS.get(provider_name)
    return "" if default is None else default[0]


def openai_compatible_discovery_base_url(provider_name: str) -> str | None:
    default = _OPENAI_COMPATIBLE_DEFAULTS.get(provider_name)
    if default is None:
        return None
    return default[1]


def openai_compatible_endpoint_config(
    provider_name: str,
    config: OpenAICompatibleProviderConfig | None,
) -> ProviderEndpointConfig:
    if provider_name not in _OPENAI_COMPATIBLE_PROVIDER_NAMES:
        raise ValueError(f"Unknown OpenAI-compatible provider: {provider_name!r}")
    if config is None:
        # An unconfigured provider still has exactly one endpoint: its own
        # vendor default. Discovery stays off -- nobody asked us to list this
        # vendor's models, and an unauthenticated listing must not go out.
        return ProviderEndpointConfig(
            base_url=openai_compatible_default_base_url(provider_name),
            discovery_base_url="",
        )
    default_base_url = openai_compatible_default_base_url(provider_name)
    default_discovery_base_url = openai_compatible_discovery_base_url(provider_name)
    if config.discovery_base_url is not None:
        discovery_base_url = config.discovery_base_url
    elif config.base_url is not None and default_discovery_base_url == "":
        # An empty default marks a provider with no public model listing.
        discovery_base_url = ""
    elif config.base_url is not None and provider_name in _OPENAI_COMPATIBLE_PROVIDERS_USING_BASE_URL_DISCOVERY:
        discovery_base_url = None
    else:
        discovery_base_url = default_discovery_base_url
    return ProviderEndpointConfig(
        api_key=config.api_key,
        base_url=config.base_url if config.base_url else default_base_url,
        discovery_base_url=discovery_base_url,
        ssl_verify=config.ssl_verify,
        timeout_seconds=config.timeout_seconds,
        model_map=dict(config.model_map),
    )


# =============================================================================
# Provider environment variables
# =============================================================================

_ZAI_API_KEY_ENV_VAR = "ZAI_API_KEY"
_ZHIPU_API_KEY_ENV_VAR = "ZHIPU_API_KEY"
_DEEPSEEK_API_KEY_ENV_VAR = "DEEPSEEK_API_KEY"
_XAI_API_KEY_ENV_VAR = "XAI_API_KEY"
_MINIMAX_API_KEY_ENV_VAR = "MINIMAX_API_KEY"
_KIMI_API_KEY_ENV_VAR = "KIMI_API_KEY"
_OPENCODE_API_KEY_ENV_VAR = "OPENCODE_API_KEY"
_DASHSCOPE_API_KEY_ENV_VAR = "DASHSCOPE_API_KEY"
_GROQ_API_KEY_ENV_VAR = "GROQ_API_KEY"
_TOGETHER_API_KEY_ENV_VAR = "TOGETHER_API_KEY"
_FIREWORKS_API_KEY_ENV_VAR = "FIREWORKS_API_KEY"
_MISTRAL_API_KEY_ENV_VAR = "MISTRAL_API_KEY"


# =============================================================================
# Provider Config Classes
# =============================================================================


@dataclass(frozen=True, slots=True)
class OpenAIProviderConfig:
    api_key: str | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    organization: str | None = None
    project: str | None = None
    timeout_seconds: float | None = None
    transient_retry: ProviderTransientRetryConfig | None = None


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    api_key: str | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    version: str | None = None
    beta_headers: tuple[str, ...] = ()
    cache_retention: Literal["none", "short", "long"] = "none"
    timeout_seconds: float | None = None
    transient_retry: ProviderTransientRetryConfig | None = None
    beta_headers_explicit: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.beta_headers and not self.beta_headers_explicit:
            object.__setattr__(self, "beta_headers_explicit", True)


type GoogleAuthMethod = Literal["api_key", "oauth", "service_account"]
type EndpointAuthScheme = Literal["bearer", "token", "none"]


@dataclass(frozen=True, slots=True)
class GoogleProviderAuthConfig:
    method: GoogleAuthMethod
    api_key: str | None = None
    access_token: str | None = None
    service_account_json_path: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleProviderConfig:
    auth: GoogleProviderAuthConfig | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    project: str | None = None
    region: str | None = None
    timeout_seconds: float | None = None
    transient_retry: ProviderTransientRetryConfig | None = None


type CopilotAuthMethod = Literal["token", "oauth"]


@dataclass(frozen=True, slots=True)
class CopilotProviderAuthConfig:
    method: CopilotAuthMethod
    token: str | None = None
    token_env_var: str | None = None
    refresh_token: str | None = None
    refresh_leeway_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class CopilotProviderConfig:
    auth: CopilotProviderAuthConfig | None = None
    base_url: str | None = None
    timeout_seconds: float | None = None
    transient_retry: ProviderTransientRetryConfig | None = None


@dataclass(frozen=True, slots=True)
class ProviderEndpointConfig:
    api_key: str | None = None
    api_key_env_var: str | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    auth_header: str | None = None
    auth_scheme: EndpointAuthScheme = "bearer"
    ssl_verify: bool | None = None
    timeout_seconds: float | None = None
    model_map: dict[str, str] = field(default_factory=dict)
    transient_retry: ProviderTransientRetryConfig | None = None
    openai_organization: str | None = None
    openai_project: str | None = None
    anthropic_version: str | None = None
    anthropic_beta_headers: tuple[str, ...] = ()
    anthropic_messages_compatible: bool = False
    cache_retention: Literal["none", "short", "long"] = "none"
    auth_scheme_explicit: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.auth_scheme != "bearer" and not self.auth_scheme_explicit:
            object.__setattr__(self, "auth_scheme_explicit", True)


@dataclass(frozen=True, slots=True)
class ProviderConfigs:
    openai: OpenAIProviderConfig | None = None
    anthropic: AnthropicProviderConfig | None = None
    google: GoogleProviderConfig | None = None
    copilot: CopilotProviderConfig | None = None
    endpoint: ProviderEndpointConfig | None = None
    opencode: ProviderEndpointConfig | None = None
    openrouter: ProviderEndpointConfig | None = None
    deepseek: OpenAICompatibleProviderConfig | None = None
    zai: OpenAICompatibleProviderConfig | None = None
    zhipuai: OpenAICompatibleProviderConfig | None = None
    grok: OpenAICompatibleProviderConfig | None = None
    minimax: OpenAICompatibleProviderConfig | None = None
    kimi: OpenAICompatibleProviderConfig | None = None
    opencode_go: OpenAICompatibleProviderConfig | None = None
    qwen: OpenAICompatibleProviderConfig | None = None
    groq: OpenAICompatibleProviderConfig | None = None
    together: OpenAICompatibleProviderConfig | None = None
    fireworks: OpenAICompatibleProviderConfig | None = None
    mistral: OpenAICompatibleProviderConfig | None = None
    custom: dict[str, ProviderEndpointConfig] = field(default_factory=dict)


_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
_GOOGLE_API_KEY_ENV_VAR = "GOOGLE_API_KEY"
_COPILOT_TOKEN_ENV_VAR = "GITHUB_COPILOT_TOKEN"
_ENDPOINT_API_KEY_ENV_VAR = "ENDPOINT_API_KEY"
_ENDPOINT_BASE_URL_ENV_VAR = "ENDPOINT_BASE_URL"
_OPENROUTER_API_KEY_ENV_VAR = "OPENROUTER_API_KEY"

_VALID_GOOGLE_AUTH_METHODS: tuple[GoogleAuthMethod, ...] = (
    "api_key",
    "oauth",
    "service_account",
)
_VALID_COPILOT_AUTH_METHODS: tuple[CopilotAuthMethod, ...] = ("token", "oauth")
_VALID_ENDPOINT_AUTH_SCHEMES: tuple[EndpointAuthScheme, ...] = (
    "bearer",
    "token",
    "none",
)


def _parse_google_auth_method(raw_method: str, *, field_path: str) -> GoogleAuthMethod:
    if raw_method == "api_key":
        return "api_key"
    if raw_method == "oauth":
        return "oauth"
    if raw_method == "service_account":
        return "service_account"
    allowed = ", ".join(_VALID_GOOGLE_AUTH_METHODS)
    raise ValueError(f"{_nested_config_field(field_path, 'method')} must be one of: {allowed}")


def _parse_copilot_auth_method(raw_method: str, *, field_path: str) -> CopilotAuthMethod:
    if raw_method == "token":
        return "token"
    if raw_method == "oauth":
        return "oauth"
    allowed = ", ".join(_VALID_COPILOT_AUTH_METHODS)
    raise ValueError(f"{_nested_config_field(field_path, 'method')} must be one of: {allowed}")


def _parse_endpoint_auth_scheme(raw_scheme: str, *, field_path: str) -> EndpointAuthScheme:
    if raw_scheme == "bearer":
        return "bearer"
    if raw_scheme == "token":
        return "token"
    if raw_scheme == "none":
        return "none"
    allowed = ", ".join(_VALID_ENDPOINT_AUTH_SCHEMES)
    raise ValueError(f"{_nested_config_field(field_path, 'auth_scheme')} must be one of: {allowed}")


@dataclass(frozen=True, slots=True)
class ProviderFallbackConfig:
    preferred_model: str
    fallback_models: tuple[str, ...] = ()


def provider_configs_from_env(env: Mapping[str, str]) -> ProviderConfigs | None:
    """Build provider config from credential environment variables alone.

    This keeps first-run provider setup lightweight: setting VOIDCODE_MODEL plus
    the provider's standard API-key environment variable is enough for runtime
    provider resolution without requiring a .voidcode.json providers block.
    """
    providers = ProviderConfigs(
        openai=(OpenAIProviderConfig(api_key=openai_key) if (openai_key := env.get(_OPENAI_API_KEY_ENV_VAR)) else None),
        anthropic=(AnthropicProviderConfig(api_key=anthropic_key) if (anthropic_key := env.get(_ANTHROPIC_API_KEY_ENV_VAR)) else None),
        google=(
            GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="api_key", api_key=google_key))
            if (google_key := env.get(_GOOGLE_API_KEY_ENV_VAR))
            else None
        ),
        copilot=(
            CopilotProviderConfig(auth=CopilotProviderAuthConfig(method="token", token=copilot_token))
            if (copilot_token := env.get(_COPILOT_TOKEN_ENV_VAR))
            else None
        ),
        endpoint=_endpoint_provider_config_from_env(env),
        opencode=(ProviderEndpointConfig(api_key=opencode_key) if (opencode_key := env.get(_OPENCODE_API_KEY_ENV_VAR)) else None),
        openrouter=(ProviderEndpointConfig(api_key=openrouter_key) if (openrouter_key := env.get(_OPENROUTER_API_KEY_ENV_VAR)) else None),
        deepseek=_openai_compatible_provider_config_from_env(env, _DEEPSEEK_API_KEY_ENV_VAR),
        zai=_openai_compatible_provider_config_from_env(env, _ZAI_API_KEY_ENV_VAR),
        zhipuai=_openai_compatible_provider_config_from_env(
            env,
            _ZHIPU_API_KEY_ENV_VAR,
            _ZAI_API_KEY_ENV_VAR,
        ),
        grok=_openai_compatible_provider_config_from_env(
            env,
            _XAI_API_KEY_ENV_VAR,
        ),
        minimax=_openai_compatible_provider_config_from_env(env, _MINIMAX_API_KEY_ENV_VAR),
        kimi=_openai_compatible_provider_config_from_env(env, _KIMI_API_KEY_ENV_VAR),
        opencode_go=_openai_compatible_provider_config_from_env(env, _OPENCODE_API_KEY_ENV_VAR),
        qwen=_openai_compatible_provider_config_from_env(env, _DASHSCOPE_API_KEY_ENV_VAR),
        groq=_openai_compatible_provider_config_from_env(env, _GROQ_API_KEY_ENV_VAR),
        together=_openai_compatible_provider_config_from_env(env, _TOGETHER_API_KEY_ENV_VAR),
        fireworks=_openai_compatible_provider_config_from_env(env, _FIREWORKS_API_KEY_ENV_VAR),
        mistral=_openai_compatible_provider_config_from_env(env, _MISTRAL_API_KEY_ENV_VAR),
    )
    if _provider_configs_has_entries(providers):
        return providers
    return None


def merge_provider_configs(
    primary: ProviderConfigs | None,
    fallback: ProviderConfigs | None,
) -> ProviderConfigs | None:
    """Merge provider configs, preserving primary values over fallback values."""
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return ProviderConfigs(
        openai=_merge_openai_provider_config(primary.openai, fallback.openai),
        anthropic=_merge_anthropic_provider_config(primary.anthropic, fallback.anthropic),
        google=_merge_google_provider_config(primary.google, fallback.google),
        copilot=_merge_copilot_provider_config(primary.copilot, fallback.copilot),
        endpoint=_merge_endpoint_provider_config(primary.endpoint, fallback.endpoint),
        opencode=_merge_endpoint_provider_config(primary.opencode, fallback.opencode),
        openrouter=_merge_endpoint_provider_config(primary.openrouter, fallback.openrouter),
        deepseek=_merge_openai_compatible_provider_config(primary.deepseek, fallback.deepseek),
        zai=_merge_openai_compatible_provider_config(primary.zai, fallback.zai),
        zhipuai=_merge_openai_compatible_provider_config(primary.zhipuai, fallback.zhipuai),
        grok=_merge_openai_compatible_provider_config(primary.grok, fallback.grok),
        minimax=_merge_openai_compatible_provider_config(primary.minimax, fallback.minimax),
        kimi=_merge_openai_compatible_provider_config(primary.kimi, fallback.kimi),
        opencode_go=_merge_openai_compatible_provider_config(
            primary.opencode_go,
            fallback.opencode_go,
        ),
        qwen=_merge_openai_compatible_provider_config(primary.qwen, fallback.qwen),
        groq=_merge_openai_compatible_provider_config(primary.groq, fallback.groq),
        together=_merge_openai_compatible_provider_config(primary.together, fallback.together),
        fireworks=_merge_openai_compatible_provider_config(primary.fireworks, fallback.fireworks),
        mistral=_merge_openai_compatible_provider_config(primary.mistral, fallback.mistral),
        custom={**fallback.custom, **primary.custom},
    )


def _merge_openai_provider_config(
    primary: OpenAIProviderConfig | None,
    fallback: OpenAIProviderConfig | None,
) -> OpenAIProviderConfig | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return OpenAIProviderConfig(
        api_key=_prefer_primary(primary.api_key, fallback.api_key),
        base_url=_prefer_primary(primary.base_url, fallback.base_url),
        discovery_base_url=_prefer_primary(primary.discovery_base_url, fallback.discovery_base_url),
        organization=_prefer_primary(primary.organization, fallback.organization),
        project=_prefer_primary(primary.project, fallback.project),
        timeout_seconds=_prefer_primary(primary.timeout_seconds, fallback.timeout_seconds),
        transient_retry=_prefer_primary(primary.transient_retry, fallback.transient_retry),
    )


def _merge_anthropic_provider_config(
    primary: AnthropicProviderConfig | None,
    fallback: AnthropicProviderConfig | None,
) -> AnthropicProviderConfig | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return AnthropicProviderConfig(
        api_key=_prefer_primary(primary.api_key, fallback.api_key),
        base_url=_prefer_primary(primary.base_url, fallback.base_url),
        discovery_base_url=_prefer_primary(primary.discovery_base_url, fallback.discovery_base_url),
        version=_prefer_primary(primary.version, fallback.version),
        beta_headers=(primary.beta_headers if primary.beta_headers_explicit else fallback.beta_headers),
        cache_retention=(primary.cache_retention if primary.cache_retention != "none" else fallback.cache_retention),
        beta_headers_explicit=primary.beta_headers_explicit or fallback.beta_headers_explicit,
        timeout_seconds=_prefer_primary(primary.timeout_seconds, fallback.timeout_seconds),
        transient_retry=_prefer_primary(primary.transient_retry, fallback.transient_retry),
    )


def _merge_google_provider_config(
    primary: GoogleProviderConfig | None,
    fallback: GoogleProviderConfig | None,
) -> GoogleProviderConfig | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return GoogleProviderConfig(
        auth=_prefer_primary(primary.auth, fallback.auth),
        base_url=_prefer_primary(primary.base_url, fallback.base_url),
        discovery_base_url=_prefer_primary(primary.discovery_base_url, fallback.discovery_base_url),
        project=_prefer_primary(primary.project, fallback.project),
        region=_prefer_primary(primary.region, fallback.region),
        timeout_seconds=_prefer_primary(primary.timeout_seconds, fallback.timeout_seconds),
        transient_retry=_prefer_primary(primary.transient_retry, fallback.transient_retry),
    )


def _merge_copilot_provider_config(
    primary: CopilotProviderConfig | None,
    fallback: CopilotProviderConfig | None,
) -> CopilotProviderConfig | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return CopilotProviderConfig(
        auth=_prefer_primary(primary.auth, fallback.auth),
        base_url=_prefer_primary(primary.base_url, fallback.base_url),
        timeout_seconds=_prefer_primary(primary.timeout_seconds, fallback.timeout_seconds),
        transient_retry=_prefer_primary(primary.transient_retry, fallback.transient_retry),
    )


def _merge_endpoint_provider_config(
    primary: ProviderEndpointConfig | None,
    fallback: ProviderEndpointConfig | None,
) -> ProviderEndpointConfig | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return ProviderEndpointConfig(
        api_key=_prefer_primary(primary.api_key, fallback.api_key),
        api_key_env_var=_prefer_primary(primary.api_key_env_var, fallback.api_key_env_var),
        base_url=_prefer_primary(primary.base_url, fallback.base_url),
        discovery_base_url=_prefer_primary(primary.discovery_base_url, fallback.discovery_base_url),
        auth_header=_prefer_primary(primary.auth_header, fallback.auth_header),
        auth_scheme=(primary.auth_scheme if primary.auth_scheme_explicit else fallback.auth_scheme),
        auth_scheme_explicit=primary.auth_scheme_explicit or fallback.auth_scheme_explicit,
        ssl_verify=_prefer_primary(primary.ssl_verify, fallback.ssl_verify),
        timeout_seconds=_prefer_primary(primary.timeout_seconds, fallback.timeout_seconds),
        model_map={**fallback.model_map, **primary.model_map},
        transient_retry=_prefer_primary(primary.transient_retry, fallback.transient_retry),
    )


def _merge_openai_compatible_provider_config(
    primary: OpenAICompatibleProviderConfig | None,
    fallback: OpenAICompatibleProviderConfig | None,
) -> OpenAICompatibleProviderConfig | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return OpenAICompatibleProviderConfig(
        api_key=_prefer_primary(primary.api_key, fallback.api_key),
        api_key_env_var=_prefer_primary(primary.api_key_env_var, fallback.api_key_env_var),
        base_url=_prefer_primary(primary.base_url, fallback.base_url),
        discovery_base_url=_prefer_primary(primary.discovery_base_url, fallback.discovery_base_url),
        ssl_verify=_prefer_primary(primary.ssl_verify, fallback.ssl_verify),
        timeout_seconds=_prefer_primary(primary.timeout_seconds, fallback.timeout_seconds),
        model_map={**fallback.model_map, **primary.model_map},
        transient_retry=_prefer_primary(primary.transient_retry, fallback.transient_retry),
    )


def _endpoint_provider_config_from_env(env: Mapping[str, str]) -> ProviderEndpointConfig | None:
    api_key = env.get(_ENDPOINT_API_KEY_ENV_VAR)
    base_url = env.get(_ENDPOINT_BASE_URL_ENV_VAR)
    if api_key is None and base_url is None:
        return None
    return ProviderEndpointConfig(api_key=api_key, base_url=base_url)


def _openai_compatible_provider_config_from_env(
    env: Mapping[str, str],
    *api_key_env_vars: str,
) -> OpenAICompatibleProviderConfig | None:
    for api_key_env_var in api_key_env_vars:
        api_key = env.get(api_key_env_var)
        if api_key is not None:
            return OpenAICompatibleProviderConfig(api_key=api_key)
    return None


def _provider_configs_has_entries(providers: ProviderConfigs) -> bool:
    return any(
        (
            providers.openai,
            providers.anthropic,
            providers.google,
            providers.copilot,
            providers.endpoint,
            providers.opencode,
            providers.openrouter,
            providers.deepseek,
            providers.zai,
            providers.zhipuai,
            providers.grok,
            providers.minimax,
            providers.kimi,
            providers.opencode_go,
            providers.qwen,
            providers.groq,
            providers.together,
            providers.fireworks,
            providers.mistral,
            providers.custom,
        )
    )


def _runtime_config_field_name(field_path: str) -> str | None:
    runtime_field_prefix = "runtime config field '"
    if field_path.startswith(runtime_field_prefix) and field_path.endswith("'"):
        return field_path[len(runtime_field_prefix) : -1]
    return None


def _append_config_field_suffix(field_path: str, suffix: str) -> str:
    runtime_field_name = _runtime_config_field_name(field_path)
    if runtime_field_name is not None:
        return _format_runtime_config_field_error(f"{runtime_field_name}{suffix}")
    return f"{field_path}{suffix}"


def _extend_config_field_path(field_path: str, loc: tuple[object, ...]) -> str:
    extended = field_path
    for item in loc:
        if isinstance(item, int):
            extended = _append_config_field_suffix(extended, f"[{item}]")
            continue
        extended = _nested_config_field(extended, str(item))
    return extended


def _validation_reason_from_error(error: dict[str, object]) -> str:
    error_type = cast(str, error.get("type", ""))
    if error_type == "value_error":
        context = error.get("ctx")
        if isinstance(context, dict):
            nested_error = cast(dict[str, object], context).get("error")
            if isinstance(nested_error, ValueError):
                return str(nested_error)
    return cast(str, error.get("msg", "is invalid"))


def _format_provider_payload_validation_error(
    *,
    field_path: str,
    error: dict[str, object],
    object_when_provided: bool = True,
) -> str:
    loc = tuple(cast(tuple[object, ...], error.get("loc", ())))
    error_type = cast(str, error.get("type", ""))
    target = _extend_config_field_path(field_path, loc)
    if error_type in {"model_type", "dict_type"}:
        suffix = " when provided" if object_when_provided else ""
        return f"{target} must be an object{suffix}"
    if error_type == "extra_forbidden":
        return f"{target} is not supported"
    reason = _validation_reason_from_error(error)
    if reason.startswith("[") or reason.startswith("."):
        if " " in reason:
            suffix, nested_reason = reason.split(" ", maxsplit=1)
            return f"{_append_config_field_suffix(target, suffix)} {nested_reason}"
        return _append_config_field_suffix(target, reason)
    if reason.startswith(" keys"):
        return f"{target}{reason}"
    return f"{target} {reason}"


def _validate_provider_payload_model[TModel: BaseModel](
    raw_value: object,
    *,
    field_path: str,
    model_type: type[TModel],
) -> TModel:
    try:
        return model_type.model_validate(raw_value)
    except ValidationError as exc:
        error = cast(dict[str, object], exc.errors(include_url=False)[0])
        raise ValueError(_format_provider_payload_validation_error(field_path=field_path, error=error)) from exc


def parse_provider_configs_payload(
    raw_providers: object,
    *,
    source: str,
    env: Mapping[str, str] | None = None,
) -> ProviderConfigs | None:
    if raw_providers is None:
        return None
    payload = _validate_provider_payload_model(
        _canonicalize_provider_config_payload_keys(raw_providers, field_path=source),
        field_path=source,
        model_type=_ProviderConfigsPayload,
    )

    environment: Mapping[str, str] = {} if env is None else env

    return ProviderConfigs(
        openai=_parse_openai_provider_config(
            payload.openai,
            field_path=_nested_config_field(source, "openai"),
            env=environment,
        ),
        anthropic=_parse_anthropic_provider_config(
            payload.anthropic,
            field_path=_nested_config_field(source, "anthropic"),
            env=environment,
        ),
        google=_parse_google_provider_config(
            payload.google,
            field_path=_nested_config_field(source, "google"),
            env=environment,
        ),
        copilot=_parse_copilot_provider_config(
            payload.copilot,
            field_path=_nested_config_field(source, "copilot"),
            env=environment,
        ),
        endpoint=_parse_endpoint_provider_config(
            payload.endpoint,
            field_path=_nested_config_field(source, "endpoint"),
            env=environment,
        ),
        opencode=_parse_endpoint_provider_config(
            payload.opencode,
            field_path=_nested_config_field(source, "opencode"),
            env=environment,
            default_api_key_env_var=_OPENCODE_API_KEY_ENV_VAR,
        ),
        openrouter=_parse_endpoint_provider_config(
            payload.openrouter,
            field_path=_nested_config_field(source, "openrouter"),
            env=environment,
            default_api_key_env_var=_OPENROUTER_API_KEY_ENV_VAR,
        ),
        deepseek=_parse_openai_compatible_provider_config(
            payload.deepseek,
            field_path=_nested_config_field(source, "deepseek"),
            env=environment,
            api_key_env_var=_DEEPSEEK_API_KEY_ENV_VAR,
        ),
        zai=_parse_openai_compatible_provider_config(
            payload.zai,
            field_path=_nested_config_field(source, "zai"),
            env=environment,
            api_key_env_var=_ZAI_API_KEY_ENV_VAR,
        ),
        zhipuai=_parse_openai_compatible_provider_config(
            payload.zhipuai,
            field_path=_nested_config_field(source, "zhipuai"),
            env=environment,
            api_key_env_var=_ZHIPU_API_KEY_ENV_VAR,
            fallback_api_key_env_vars=(_ZAI_API_KEY_ENV_VAR,),
        ),
        grok=_parse_openai_compatible_provider_config(
            payload.grok,
            field_path=_nested_config_field(source, "grok"),
            env=environment,
            api_key_env_var=_XAI_API_KEY_ENV_VAR,
        ),
        minimax=_parse_openai_compatible_provider_config(
            payload.minimax,
            field_path=_nested_config_field(source, "minimax"),
            env=environment,
            api_key_env_var=_MINIMAX_API_KEY_ENV_VAR,
        ),
        kimi=_parse_openai_compatible_provider_config(
            payload.kimi,
            field_path=_nested_config_field(source, "kimi"),
            env=environment,
            api_key_env_var=_KIMI_API_KEY_ENV_VAR,
        ),
        opencode_go=_parse_openai_compatible_provider_config(
            payload.opencode_go,
            field_path=_nested_config_field(source, "opencode-go"),
            env=environment,
            api_key_env_var=_OPENCODE_API_KEY_ENV_VAR,
        ),
        qwen=_parse_openai_compatible_provider_config(
            payload.qwen,
            field_path=_nested_config_field(source, "qwen"),
            env=environment,
            api_key_env_var=_DASHSCOPE_API_KEY_ENV_VAR,
        ),
        groq=_parse_openai_compatible_provider_config(
            payload.groq,
            field_path=_nested_config_field(source, "groq"),
            env=environment,
            api_key_env_var=_GROQ_API_KEY_ENV_VAR,
        ),
        together=_parse_openai_compatible_provider_config(
            payload.together,
            field_path=_nested_config_field(source, "together"),
            env=environment,
            api_key_env_var=_TOGETHER_API_KEY_ENV_VAR,
        ),
        fireworks=_parse_openai_compatible_provider_config(
            payload.fireworks,
            field_path=_nested_config_field(source, "fireworks"),
            env=environment,
            api_key_env_var=_FIREWORKS_API_KEY_ENV_VAR,
        ),
        mistral=_parse_openai_compatible_provider_config(
            payload.mistral,
            field_path=_nested_config_field(source, "mistral"),
            env=environment,
            api_key_env_var=_MISTRAL_API_KEY_ENV_VAR,
        ),
        custom=_parse_custom_endpoint_provider_configs(
            payload.custom,
            field_path=_nested_config_field(source, "custom"),
            env=environment,
        ),
    )


def serialize_provider_configs(
    providers: ProviderConfigs | None,
    *,
    include_secrets: bool = False,
) -> dict[str, object] | None:
    if providers is None:
        return None
    serialized: dict[str, object] = {}
    if providers.openai is not None:
        serialized["openai"] = _serialize_openai_provider_config(
            providers.openai,
            include_secrets=include_secrets,
        )
    if providers.anthropic is not None:
        serialized["anthropic"] = _serialize_anthropic_provider_config(
            providers.anthropic,
            include_secrets=include_secrets,
        )
    if providers.google is not None:
        serialized["google"] = _serialize_google_provider_config(
            providers.google,
            include_secrets=include_secrets,
        )
    if providers.copilot is not None:
        serialized["copilot"] = _serialize_copilot_provider_config(
            providers.copilot,
            include_secrets=include_secrets,
        )
    if providers.endpoint is not None:
        serialized["endpoint"] = _serialize_endpoint_provider_config(
            providers.endpoint,
            include_secrets=include_secrets,
        )
    if providers.opencode is not None:
        serialized["opencode"] = _serialize_endpoint_provider_config(
            providers.opencode,
            include_secrets=include_secrets,
        )
    if providers.openrouter is not None:
        serialized["openrouter"] = _serialize_endpoint_provider_config(
            providers.openrouter,
            include_secrets=include_secrets,
        )
    if providers.deepseek is not None:
        serialized["deepseek"] = _serialize_openai_compatible_provider_config(
            providers.deepseek,
            include_secrets=include_secrets,
        )
    if providers.zai is not None:
        serialized["zai"] = _serialize_openai_compatible_provider_config(
            providers.zai,
            include_secrets=include_secrets,
        )
    if providers.zhipuai is not None:
        serialized["zhipuai"] = _serialize_openai_compatible_provider_config(
            providers.zhipuai,
            include_secrets=include_secrets,
        )
    if providers.grok is not None:
        serialized["grok"] = _serialize_openai_compatible_provider_config(
            providers.grok,
            include_secrets=include_secrets,
        )
    if providers.minimax is not None:
        serialized["minimax"] = _serialize_openai_compatible_provider_config(
            providers.minimax,
            include_secrets=include_secrets,
        )
    if providers.kimi is not None:
        serialized["kimi"] = _serialize_openai_compatible_provider_config(
            providers.kimi,
            include_secrets=include_secrets,
        )
    if providers.opencode_go is not None:
        serialized["opencode-go"] = _serialize_openai_compatible_provider_config(
            providers.opencode_go,
            include_secrets=include_secrets,
        )
    if providers.qwen is not None:
        serialized["qwen"] = _serialize_openai_compatible_provider_config(
            providers.qwen,
            include_secrets=include_secrets,
        )
    if providers.groq is not None:
        serialized["groq"] = _serialize_openai_compatible_provider_config(
            providers.groq,
            include_secrets=include_secrets,
        )
    if providers.together is not None:
        serialized["together"] = _serialize_openai_compatible_provider_config(
            providers.together,
            include_secrets=include_secrets,
        )
    if providers.fireworks is not None:
        serialized["fireworks"] = _serialize_openai_compatible_provider_config(
            providers.fireworks,
            include_secrets=include_secrets,
        )
    if providers.mistral is not None:
        serialized["mistral"] = _serialize_openai_compatible_provider_config(
            providers.mistral,
            include_secrets=include_secrets,
        )
    if providers.custom:
        custom_payload: dict[str, object] = {}
        for provider_name, custom_config in providers.custom.items():
            custom_payload[provider_name] = _serialize_endpoint_provider_config(
                custom_config,
                include_secrets=include_secrets,
            )
        serialized["custom"] = custom_payload
    return serialized


def parse_provider_fallback_payload(
    raw_provider_fallback: object,
    *,
    source: str,
) -> ProviderFallbackConfig | None:
    if raw_provider_fallback is None:
        return None
    payload = _validate_provider_payload_model(
        raw_provider_fallback,
        field_path=source,
        model_type=_ProviderFallbackPayload,
    )
    preferred_model = payload.preferred_model
    fallback_models = payload.fallback_models
    ordered_models = (preferred_model, *fallback_models)
    if len(set(ordered_models)) != len(ordered_models):
        raise ValueError("provider fallback chain must not contain duplicate models")
    return ProviderFallbackConfig(
        preferred_model=preferred_model,
        fallback_models=fallback_models,
    )


def serialize_provider_fallback_config(
    provider_fallback: ProviderFallbackConfig | None,
) -> dict[str, object] | None:
    if provider_fallback is None:
        return None
    return {
        "preferred_model": provider_fallback.preferred_model,
        "fallback_models": list(provider_fallback.fallback_models),
    }


def _parse_openai_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> OpenAIProviderConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _OpenAIProviderConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_OpenAIProviderConfigPayload,
        )
    )

    api_key = payload.api_key
    if api_key is None:
        api_key = env.get(_OPENAI_API_KEY_ENV_VAR)
    return OpenAIProviderConfig(
        api_key=api_key,
        base_url=payload.base_url,
        discovery_base_url=payload.discovery_base_url,
        organization=payload.organization,
        project=payload.project,
        timeout_seconds=payload.timeout_seconds,
        transient_retry=_parse_transient_retry_config(
            payload.transient_retry,
            field_path=_nested_config_field(field_path, "transient_retry"),
        ),
    )


def _parse_anthropic_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> AnthropicProviderConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _AnthropicProviderConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_AnthropicProviderConfigPayload,
        )
    )

    api_key = payload.api_key
    if api_key is None:
        api_key = env.get(_ANTHROPIC_API_KEY_ENV_VAR)
    return AnthropicProviderConfig(
        api_key=api_key,
        base_url=payload.base_url,
        discovery_base_url=payload.discovery_base_url,
        version=payload.version,
        beta_headers=payload.beta_headers,
        cache_retention=payload.cache_retention,
        beta_headers_explicit="beta_headers" in payload.model_fields_set,
        timeout_seconds=payload.timeout_seconds,
        transient_retry=_parse_transient_retry_config(
            payload.transient_retry,
            field_path=_nested_config_field(field_path, "transient_retry"),
        ),
    )


def _parse_google_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> GoogleProviderConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _GoogleProviderConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_GoogleProviderConfigPayload,
        )
    )

    auth = _parse_google_auth_config(
        payload.auth,
        field_path=_nested_config_field(field_path, "auth"),
        env=env,
    )
    return GoogleProviderConfig(
        auth=auth,
        base_url=payload.base_url,
        discovery_base_url=payload.discovery_base_url,
        project=payload.project,
        region=payload.region,
        timeout_seconds=payload.timeout_seconds,
        transient_retry=_parse_transient_retry_config(
            payload.transient_retry,
            field_path=_nested_config_field(field_path, "transient_retry"),
        ),
    )


def _parse_google_auth_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> GoogleProviderAuthConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _GoogleProviderAuthConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_GoogleProviderAuthConfigPayload,
        )
    )

    method = _parse_google_auth_method(payload.method, field_path=field_path)

    api_key = payload.api_key
    if api_key is None:
        api_key = env.get(_GOOGLE_API_KEY_ENV_VAR)
    access_token = payload.access_token
    service_account_json_path = payload.service_account_json_path

    if method == "api_key":
        if api_key is None:
            raise ValueError(f"{_nested_config_field(field_path, 'api_key')} must be provided when method is api_key")
        if access_token is not None:
            raise ValueError(f"{_nested_config_field(field_path, 'access_token')} must not be set when method is api_key")
        if service_account_json_path is not None:
            raise ValueError(f"{_nested_config_field(field_path, 'service_account_json_path')} must not be set when method is api_key")
    elif method == "oauth":
        if access_token is None:
            raise ValueError(f"{_nested_config_field(field_path, 'access_token')} must be provided when method is oauth")
        if api_key is not None:
            raise ValueError(f"{_nested_config_field(field_path, 'api_key')} must not be set when method is oauth")
        if service_account_json_path is not None:
            raise ValueError(f"{_nested_config_field(field_path, 'service_account_json_path')} must not be set when method is oauth")
    elif service_account_json_path is None:
        raise ValueError(f"{_nested_config_field(field_path, 'service_account_json_path')} must be provided when method is service_account")
    elif api_key is not None:
        raise ValueError(f"{_nested_config_field(field_path, 'api_key')} must not be set when method is service_account")
    elif access_token is not None:
        raise ValueError(f"{_nested_config_field(field_path, 'access_token')} must not be set when method is service_account")

    return GoogleProviderAuthConfig(
        method=method,
        api_key=api_key,
        access_token=access_token,
        service_account_json_path=service_account_json_path,
    )


def _parse_copilot_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> CopilotProviderConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _CopilotProviderConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_CopilotProviderConfigPayload,
        )
    )

    auth = _parse_copilot_auth_config(
        payload.auth,
        field_path=_nested_config_field(field_path, "auth"),
        env=env,
    )
    return CopilotProviderConfig(
        auth=auth,
        base_url=payload.base_url,
        timeout_seconds=payload.timeout_seconds,
        transient_retry=_parse_transient_retry_config(
            payload.transient_retry,
            field_path=_nested_config_field(field_path, "transient_retry"),
        ),
    )


def _parse_copilot_auth_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> CopilotProviderAuthConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _CopilotProviderAuthConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_CopilotProviderAuthConfigPayload,
        )
    )

    method = _parse_copilot_auth_method(payload.method, field_path=field_path)

    token = payload.token
    token_env_var = payload.token_env_var
    if token is None and token_env_var is None:
        token = env.get(_COPILOT_TOKEN_ENV_VAR)
        if token is not None:
            token_env_var = _COPILOT_TOKEN_ENV_VAR
    refresh_token = payload.refresh_token
    refresh_leeway_seconds = payload.refresh_leeway_seconds

    if method == "token":
        if token is None and token_env_var is None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'token')} or "
                f"{_nested_config_field(field_path, 'token_env_var')} "
                "must be provided when method is token"
            )
        if token is not None and token_env_var is not None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'token')} and {_nested_config_field(field_path, 'token_env_var')} must not both be set"
            )
        if refresh_token is not None:
            raise ValueError(f"{_nested_config_field(field_path, 'refresh_token')} must not be set when method is token")
        if refresh_leeway_seconds is not None:
            raise ValueError(f"{_nested_config_field(field_path, 'refresh_leeway_seconds')} must not be set when method is token")
    else:
        if token is None and token_env_var is None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'token')} or "
                f"{_nested_config_field(field_path, 'token_env_var')} "
                "must be provided when method is oauth"
            )

    return CopilotProviderAuthConfig(
        method=method,
        token=token,
        token_env_var=token_env_var,
        refresh_token=refresh_token,
        refresh_leeway_seconds=refresh_leeway_seconds,
    )


def _parse_endpoint_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    default_api_key_env_var: str | None = None,
) -> ProviderEndpointConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _ProviderEndpointConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_ProviderEndpointConfigPayload,
        )
    )

    api_key = payload.api_key
    api_key_env_var = payload.api_key_env_var
    if api_key is None:
        if api_key_env_var is not None:
            api_key = env.get(api_key_env_var)
        elif default_api_key_env_var is not None:
            api_key = env.get(default_api_key_env_var)
        else:
            api_key = env.get(_ENDPOINT_API_KEY_ENV_VAR)

    base_url = payload.base_url
    if base_url is None:
        base_url = env.get(_ENDPOINT_BASE_URL_ENV_VAR)

    raw_auth_scheme = payload.auth_scheme
    auth_scheme: EndpointAuthScheme = "bearer"
    if raw_auth_scheme is not None:
        auth_scheme = _parse_endpoint_auth_scheme(raw_auth_scheme, field_path=field_path)

    return ProviderEndpointConfig(
        api_key=api_key,
        api_key_env_var=api_key_env_var,
        base_url=base_url,
        discovery_base_url=payload.discovery_base_url,
        auth_header=payload.auth_header,
        auth_scheme=auth_scheme,
        auth_scheme_explicit=raw_auth_scheme is not None,
        ssl_verify=payload.ssl_verify,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map,
        transient_retry=_parse_transient_retry_config(
            payload.transient_retry,
            field_path=_nested_config_field(field_path, "transient_retry"),
        ),
    )


def _parse_openai_compatible_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    api_key_env_var: str,
    fallback_api_key_env_vars: tuple[str, ...] = (),
) -> OpenAICompatibleProviderConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _OpenAICompatibleProviderConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_OpenAICompatibleProviderConfigPayload,
        )
    )

    api_key = payload.api_key
    api_key_env = payload.api_key_env_var
    if api_key is None:
        if api_key_env is not None:
            api_key = env.get(api_key_env)
        else:
            for candidate_env_var in (api_key_env_var, *fallback_api_key_env_vars):
                api_key = env.get(candidate_env_var)
                if api_key is not None:
                    break

    return OpenAICompatibleProviderConfig(
        api_key=api_key,
        api_key_env_var=api_key_env,
        base_url=payload.base_url,
        discovery_base_url=payload.discovery_base_url,
        ssl_verify=payload.ssl_verify,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map,
        transient_retry=_parse_transient_retry_config(
            payload.transient_retry,
            field_path=_nested_config_field(field_path, "transient_retry"),
        ),
    )


def _parse_transient_retry_config(
    raw_value: object,
    *,
    field_path: str,
) -> ProviderTransientRetryConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _ProviderTransientRetryConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_ProviderTransientRetryConfigPayload,
        )
    )
    base_delay_ms = DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG.base_delay_ms if payload.base_delay_ms is None else payload.base_delay_ms
    max_delay_ms = DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG.max_delay_ms if payload.max_delay_ms is None else payload.max_delay_ms
    if max_delay_ms < base_delay_ms:
        raise ValueError(f"{_nested_config_field(field_path, 'max_delay_ms')} must be greater than or equal to base_delay_ms")
    return ProviderTransientRetryConfig(
        max_retries=(DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG.max_retries if payload.max_retries is None else payload.max_retries),
        base_delay_ms=base_delay_ms,
        max_delay_ms=max_delay_ms,
        jitter=(DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG.jitter if payload.jitter is None else payload.jitter),
    )


def _serialize_openai_provider_config(
    provider: OpenAIProviderConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if include_secrets and provider.api_key is not None:
        payload["api_key"] = provider.api_key
    if provider.base_url is not None:
        payload["base_url"] = provider.base_url
    if provider.discovery_base_url is not None:
        payload["discovery_base_url"] = provider.discovery_base_url
    if provider.organization is not None:
        payload["organization"] = provider.organization
    if provider.project is not None:
        payload["project"] = provider.project
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.transient_retry is not None:
        payload["transient_retry"] = _serialize_transient_retry_config(provider.transient_retry)
    return payload


def _serialize_anthropic_provider_config(
    provider: AnthropicProviderConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if include_secrets and provider.api_key is not None:
        payload["api_key"] = provider.api_key
    if provider.base_url is not None:
        payload["base_url"] = provider.base_url
    if provider.discovery_base_url is not None:
        payload["discovery_base_url"] = provider.discovery_base_url
    if provider.version is not None:
        payload["version"] = provider.version
    if provider.beta_headers or provider.beta_headers_explicit:
        payload["beta_headers"] = list(provider.beta_headers)
    if provider.cache_retention != "none":
        payload["cache_retention"] = provider.cache_retention
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.transient_retry is not None:
        payload["transient_retry"] = _serialize_transient_retry_config(provider.transient_retry)
    return payload


def _serialize_google_provider_config(
    provider: GoogleProviderConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if provider.auth is not None:
        payload["auth"] = _serialize_google_auth_config(provider.auth, include_secrets=include_secrets)
    if provider.base_url is not None:
        payload["base_url"] = provider.base_url
    if provider.discovery_base_url is not None:
        payload["discovery_base_url"] = provider.discovery_base_url
    if provider.project is not None:
        payload["project"] = provider.project
    if provider.region is not None:
        payload["region"] = provider.region
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.transient_retry is not None:
        payload["transient_retry"] = _serialize_transient_retry_config(provider.transient_retry)
    return payload


def _serialize_google_auth_config(
    auth: GoogleProviderAuthConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {"method": auth.method}
    if include_secrets and auth.api_key is not None:
        payload["api_key"] = auth.api_key
    if include_secrets and auth.access_token is not None:
        payload["access_token"] = auth.access_token
    if auth.service_account_json_path is not None:
        payload["service_account_json_path"] = auth.service_account_json_path
    return payload


def _serialize_copilot_provider_config(
    provider: CopilotProviderConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if provider.auth is not None:
        payload["auth"] = _serialize_copilot_auth_config(
            provider.auth,
            include_secrets=include_secrets,
        )
    if provider.base_url is not None:
        payload["base_url"] = provider.base_url
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.transient_retry is not None:
        payload["transient_retry"] = _serialize_transient_retry_config(provider.transient_retry)
    return payload


def _serialize_copilot_auth_config(
    auth: CopilotProviderAuthConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {"method": auth.method}
    if include_secrets and auth.token is not None:
        payload["token"] = auth.token
    if auth.token_env_var is not None:
        payload["token_env_var"] = auth.token_env_var
    if include_secrets and auth.refresh_token is not None:
        payload["refresh_token"] = auth.refresh_token
    if auth.refresh_leeway_seconds is not None:
        payload["refresh_leeway_seconds"] = auth.refresh_leeway_seconds
    return payload


def _serialize_endpoint_provider_config(
    provider: ProviderEndpointConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if include_secrets and provider.api_key is not None:
        payload["api_key"] = provider.api_key
    if provider.api_key_env_var is not None:
        payload["api_key_env_var"] = provider.api_key_env_var
    if provider.base_url is not None:
        payload["base_url"] = provider.base_url
    if provider.discovery_base_url is not None:
        payload["discovery_base_url"] = provider.discovery_base_url
    if provider.auth_header is not None:
        payload["auth_header"] = provider.auth_header
    payload["auth_scheme"] = provider.auth_scheme
    if provider.ssl_verify is not None:
        payload["ssl_verify"] = provider.ssl_verify
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.model_map:
        payload["model_map"] = dict(provider.model_map)
    if provider.transient_retry is not None:
        payload["transient_retry"] = _serialize_transient_retry_config(provider.transient_retry)
    return payload


def _serialize_openai_compatible_provider_config(
    provider: OpenAICompatibleProviderConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if include_secrets and provider.api_key is not None:
        payload["api_key"] = provider.api_key
    if provider.api_key_env_var is not None:
        payload["api_key_env_var"] = provider.api_key_env_var
    if provider.base_url is not None:
        payload["base_url"] = provider.base_url
    if provider.discovery_base_url is not None:
        payload["discovery_base_url"] = provider.discovery_base_url
    if provider.ssl_verify is not None:
        payload["ssl_verify"] = provider.ssl_verify
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.model_map:
        payload["model_map"] = dict(provider.model_map)
    if provider.transient_retry is not None:
        payload["transient_retry"] = _serialize_transient_retry_config(provider.transient_retry)
    return payload


def _serialize_transient_retry_config(
    retry_config: ProviderTransientRetryConfig,
) -> dict[str, object]:
    return {
        "max_retries": retry_config.max_retries,
        "base_delay_ms": retry_config.base_delay_ms,
        "max_delay_ms": retry_config.max_delay_ms,
        "jitter": retry_config.jitter,
    }


def _parse_custom_endpoint_provider_configs(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> dict[str, ProviderEndpointConfig]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{field_path} must be an object when provided")

    payload = cast(dict[object, object], raw_value)
    parsed: dict[str, ProviderEndpointConfig] = {}
    spelled: dict[str, str] = {}
    for raw_provider_name, provider_payload in payload.items():
        if not isinstance(raw_provider_name, str) or not raw_provider_name:
            raise ValueError(f"{field_path} keys must be non-empty strings")
        if "/" in raw_provider_name:
            raise ValueError(f"{_nested_config_field(field_path, raw_provider_name)} must not contain '/'")
        # A custom provider id is a provider id: it is trimmed, canonicalised to
        # lowercase, and may not shadow a built-in provider.
        normalized_provider_name = canonical_provider_id(raw_provider_name)
        if not normalized_provider_name:
            raise ValueError(f"{field_path} keys must be non-empty strings")
        if normalized_provider_name in BUILTIN_PROVIDER_IDS:
            raise ValueError(
                f"{_nested_config_field(field_path, raw_provider_name)} "
                "must not collide with built-in provider names "
                f"(conflicts with '{normalized_provider_name}')"
            )
        previous = spelled.get(normalized_provider_name)
        if previous is not None and previous != raw_provider_name:
            raise ValueError(
                f"{_nested_config_field(field_path, raw_provider_name)} duplicates "
                f"{_nested_config_field(field_path, previous)}: provider ids are case-insensitive"
            )
        spelled[normalized_provider_name] = raw_provider_name

        parsed_config = _parse_endpoint_provider_config(
            provider_payload,
            field_path=_nested_config_field(field_path, raw_provider_name),
            env=env,
        )
        if parsed_config is None:
            continue
        parsed[normalized_provider_name] = parsed_config
    return parsed


def _nested_config_field(source: str, nested: str) -> str:
    runtime_field_prefix = "runtime config field '"
    if source.startswith(runtime_field_prefix) and source.endswith("'"):
        base_field = source[len(runtime_field_prefix) : -1]
        return f"runtime config field '{base_field}.{nested}'"
    return f"{source}.{nested}"


def _format_runtime_config_field_error(field_path: str) -> str:
    runtime_field_prefix = "runtime config field '"
    if field_path.startswith(runtime_field_prefix):
        if field_path.endswith("'"):
            return field_path
        if "'[" in field_path:
            base, suffix = field_path[len(runtime_field_prefix) :].split("'[", maxsplit=1)
            return f"{runtime_field_prefix}{base}[{suffix}'"
    return f"runtime config field '{field_path}'"
