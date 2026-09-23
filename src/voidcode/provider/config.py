from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.functional_validators import BeforeValidator

from .naming import (
    BUILTIN_PROVIDER_IDS,
    PROVIDER_LABELS,
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
    for index, item in enumerate(value):
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
    for raw_key, raw_item in value.items():
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


type GoogleAuthMethod = Literal["api_key", "oauth", "service_account"]
type CopilotAuthMethod = Literal["token", "oauth"]
type EndpointAuthScheme = Literal["bearer", "token", "none"]
_VALID_GOOGLE_AUTH_METHODS: tuple[GoogleAuthMethod, ...] = ("api_key", "oauth", "service_account")
_VALID_COPILOT_AUTH_METHODS: tuple[CopilotAuthMethod, ...] = ("token", "oauth")
_VALID_ENDPOINT_AUTH_SCHEMES: tuple[EndpointAuthScheme, ...] = (
    "bearer",
    "token",
    "none",
)

# The wires VoidCode implements. An internal endpoint marker, never user config:
# it tells the discovery path which listing shape and credential headers the
# provider's host speaks, and per-model gateway routing reuses it.
type RoutedWire = Literal["openai-chat-completions", "anthropic-messages", "google-generative-ai"]


# The ``Field(...)`` range/minimum constraints below mirror what the paired
# ``BeforeValidator`` already enforces, so the JSON Schema generated from these
# boundary models (the shipped ``schema/voidcode.config.schema.json``) describes
# the values the parser really accepts instead of a looser shape.
# The range/length keywords below are published-contract metadata
# (``json_schema_extra``), not validation: the paired ``BeforeValidator`` keeps
# deciding acceptance and its messages, exactly as before these models were made
# public for schema generation.
BoundarySchemaNumber = Annotated[float, Field(json_schema_extra={"exclusiveMinimum": 0})]
BoundarySchemaNonnegativeNumber = Annotated[float, Field(json_schema_extra={"minimum": 0})]
BoundarySchemaPositiveInt = Annotated[int, Field(json_schema_extra={"minimum": 1})]
BoundarySchemaNonnegativeInt = Annotated[int, Field(json_schema_extra={"minimum": 0})]
BoundarySchemaString = Annotated[str, Field()]
#: The endpoint auth scheme publishes its enum inside the string branch so the
#: field also accepts an explicit null (which the parser normalises).
BoundaryAuthSchemeValue = Annotated[str, Field(json_schema_extra={"enum": list(_VALID_ENDPOINT_AUTH_SCHEMES)})]
_BoundaryAuthScheme = Annotated[BoundaryAuthSchemeValue | None, BeforeValidator(_parse_optional_boundary_string)]
#: ``_parse_boundary_string_mapping`` rejects an empty value, so only the map's
#: values publish a minimum; a plain provider string may be empty.
BoundarySchemaNonEmptyString = Annotated[str, Field(json_schema_extra={"minLength": 1})]
BoundaryOptionalString = Annotated[BoundarySchemaString | None, BeforeValidator(_parse_optional_boundary_string)]
BoundaryRequiredString = Annotated[str, BeforeValidator(_parse_required_boundary_string)]
BoundaryOptionalTimeout = Annotated[BoundarySchemaNumber | None, BeforeValidator(_parse_optional_boundary_timeout)]
BoundaryOptionalPositiveInt = Annotated[BoundarySchemaPositiveInt | None, BeforeValidator(_parse_optional_boundary_positive_int)]
BoundaryOptionalNonnegativeInt = Annotated[BoundarySchemaNonnegativeInt | None, BeforeValidator(_parse_optional_boundary_nonnegative_int)]
BoundaryOptionalNonnegativeFloat = Annotated[BoundarySchemaNonnegativeNumber | None, BeforeValidator(_parse_optional_boundary_nonnegative_float)]
BoundaryOptionalBool = Annotated[bool | None, BeforeValidator(_parse_optional_boundary_bool)]
BoundaryStringList = Annotated[tuple[str, ...], BeforeValidator(_parse_boundary_string_list)]
BoundaryStringMapping = Annotated[
    dict[str, BoundarySchemaNonEmptyString] | None,
    BeforeValidator(_parse_boundary_string_mapping),
    Field(json_schema_extra={"propertyNames": {"pattern": ".+"}}),
]

#: ``providers.custom`` keys may not shadow a built-in provider id (the parser
#: canonicalises and compares against ``BUILTIN_PROVIDER_IDS``) and may not
#: contain a ``/``; the pattern is derived from the same provider table.
#: The alternation is built from the label table's own ids (``[a-z0-9-]`` only in
#: this repo), so it needs no regex escaping.
_CUSTOM_PROVIDER_KEY_PATTERN = rf"^(?!(?:{'|'.join(PROVIDER_LABELS)})$)(?!.*[/]).+$"


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
    organization: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _AnthropicProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    version: BoundaryOptionalString = None
    beta_headers: BoundaryStringList | None = ()
    cache_retention: Literal["none", "short", "long"] = "none"
    timeout_seconds: BoundaryOptionalTimeout = None
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _GoogleProviderAuthConfigPayload(_ProviderPayloadModel):
    method: BoundaryRequiredString = Field(
        # ``_parse_google_auth_method`` owns the rejection.
        json_schema_extra={"enum": list(_VALID_GOOGLE_AUTH_METHODS)},
    )
    api_key: BoundaryOptionalString = None
    access_token: BoundaryOptionalString = None
    service_account_json_path: BoundaryOptionalString = None


class _GoogleProviderConfigPayload(_ProviderPayloadModel):
    auth: _GoogleProviderAuthConfigPayload | None = None
    base_url: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    region: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _CopilotProviderAuthConfigPayload(_ProviderPayloadModel):
    method: BoundaryRequiredString = Field(
        # ``_parse_copilot_auth_method`` owns the rejection.
        json_schema_extra={"enum": list(_VALID_COPILOT_AUTH_METHODS)},
    )
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
    auth_header: BoundaryOptionalString = None
    auth_scheme: _BoundaryAuthScheme = None
    ssl_verify: BoundaryOptionalBool = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class _OpenAICompatibleProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    ssl_verify: BoundaryOptionalBool = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)
    transient_retry: _ProviderTransientRetryConfigPayload | None = None


class ProviderConfigsPayload(_ProviderPayloadModel):
    openai: _OpenAIProviderConfigPayload | None = None
    anthropic: _AnthropicProviderConfigPayload | None = None
    kimi_coding: _AnthropicProviderConfigPayload | None = Field(default=None, alias="kimi-coding")
    minimax_cn: _AnthropicProviderConfigPayload | None = Field(default=None, alias="minimax-cn")
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
    custom: dict[str, _ProviderEndpointConfigPayload] = Field(
        default_factory=dict,
        json_schema_extra={"propertyNames": {"pattern": _CUSTOM_PROVIDER_KEY_PATTERN}},
    )


class _ProviderFallbackPayload(_ProviderPayloadModel):
    preferred_model: BoundaryRequiredString
    fallback_models: BoundaryStringList = ()


def _provider_config_fields() -> dict[str, tuple[str, str]]:
    """Canonical provider id -> (``ProviderConfigs`` field name, ``providers`` payload key).

    A payload field name and its ``ProviderConfigs`` field are one name -- the
    parser builds the config from the payload by keyword -- so the payload model
    is the one place the id -> field relationship is stated.
    """
    fields: dict[str, tuple[str, str]] = {}
    for field_name, model_field in ProviderConfigsPayload.model_fields.items():
        if field_name == "custom":
            continue
        payload_key = model_field.validation_alias if isinstance(model_field.validation_alias, str) else field_name
        fields[canonical_provider_id(payload_key)] = (field_name, payload_key)
    return fields


_PROVIDER_CONFIG_FIELD_ENTRIES: Mapping[str, tuple[str, str]] = _provider_config_fields()

#: Canonical built-in provider id -> the ``providers`` payload key that carries it.
_PROVIDER_CONFIG_PAYLOAD_KEYS: Mapping[str, str] = {
    provider_id: payload_key for provider_id, (_, payload_key) in _PROVIDER_CONFIG_FIELD_ENTRIES.items()
}

#: Canonical built-in provider id -> the ``ProviderConfigs`` field holding its
#: configuration. Custom providers live in the ``custom`` mapping instead.
PROVIDER_CONFIG_FIELDS: Mapping[str, str] = {provider_id: field_name for provider_id, (field_name, _) in _PROVIDER_CONFIG_FIELD_ENTRIES.items()}


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
    for raw_key, value in raw_value.items():
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
    ssl_verify: bool | None = None
    timeout_seconds: float | None = None
    model_map: dict[str, str] = field(default_factory=dict)
    transient_retry: ProviderTransientRetryConfig | None = None


# Default endpoint per provider. Only base URLs live here: the model list is
# always discovered from the provider's own host, so there is no second list to
# maintain and no listing URL to keep in step.
_OPENAI_COMPATIBLE_DEFAULTS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "zai": "https://api.z.ai/api/paas/v4",
    "zhipuai": "https://open.bigmodel.cn/api/paas/v4",
    "grok": "https://api.x.ai",
    "minimax": "https://api.minimax.io",
    "kimi": "https://api.moonshot.ai",
    "opencode-go": "https://opencode.ai/zen/go",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.ai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "mistral": "https://api.mistral.ai/v1",
}


_OPENAI_COMPATIBLE_PROVIDER_NAMES = frozenset(_OPENAI_COMPATIBLE_DEFAULTS)


def openai_compatible_default_base_url(provider_name: str) -> str:
    return _OPENAI_COMPATIBLE_DEFAULTS.get(provider_name, "")


def openai_compatible_endpoint_config(
    provider_name: str,
    config: OpenAICompatibleProviderConfig | None,
) -> ProviderEndpointConfig:
    if provider_name not in _OPENAI_COMPATIBLE_PROVIDER_NAMES:
        raise ValueError(f"Unknown OpenAI-compatible provider: {provider_name!r}")
    default_base_url = openai_compatible_default_base_url(provider_name)
    if config is None:
        # An unconfigured provider still has exactly one endpoint: its own
        # vendor default.
        return ProviderEndpointConfig(base_url=default_base_url)
    return ProviderEndpointConfig(
        api_key=config.api_key,
        api_key_env_var=config.api_key_env_var,
        base_url=config.base_url if config.base_url else default_base_url,
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
    organization: str | None = None
    project: str | None = None
    timeout_seconds: float | None = None
    transient_retry: ProviderTransientRetryConfig | None = None


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    api_key: str | None = None
    base_url: str | None = None
    version: str | None = None
    beta_headers: tuple[str, ...] = ()
    cache_retention: Literal["none", "short", "long"] = "none"
    timeout_seconds: float | None = None
    transient_retry: ProviderTransientRetryConfig | None = None
    beta_headers_explicit: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.beta_headers and not self.beta_headers_explicit:
            object.__setattr__(self, "beta_headers_explicit", True)


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


#: Any configuration entry a built-in provider id can resolve to. Every member
#: carries a ``base_url`` and a ``transient_retry``, which is what the readers of
#: an entry actually ask it for.
type ProviderConfigEntry = (
    OpenAIProviderConfig
    | AnthropicProviderConfig
    | GoogleProviderConfig
    | CopilotProviderConfig
    | ProviderEndpointConfig
    | OpenAICompatibleProviderConfig
)


@dataclass(frozen=True, slots=True)
class ProviderEndpointConfig:
    api_key: str | None = None
    api_key_env_var: str | None = None
    base_url: str | None = None
    auth_header: str | None = None
    auth_scheme: EndpointAuthScheme = "bearer"
    ssl_verify: bool | None = None
    timeout_seconds: float | None = None
    model_map: dict[str, str] = field(default_factory=dict)
    transient_retry: ProviderTransientRetryConfig | None = None
    openai_organization: str | None = None
    openai_project: str | None = None
    wire: RoutedWire = "openai-chat-completions"
    cache_retention: Literal["none", "short", "long"] = "none"
    auth_scheme_explicit: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.auth_scheme != "bearer" and not self.auth_scheme_explicit:
            object.__setattr__(self, "auth_scheme_explicit", True)


@dataclass(frozen=True, slots=True)
class ProviderConfigs:
    openai: OpenAIProviderConfig | None = None
    anthropic: AnthropicProviderConfig | None = None
    kimi_coding: AnthropicProviderConfig | None = None
    minimax_cn: AnthropicProviderConfig | None = None
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

    def entry(self, provider_name: str) -> ProviderConfigEntry | None:
        """The configuration entry of one built-in provider id, ``None`` if it is not built-in.

        Provider ids are case-insensitive: ``MiniMax`` reads the ``minimax``
        entry. An id nothing declares, and a provider under ``custom``, return
        ``None``: the entry's field name is derived from the payload model, so
        there is no second list of provider ids to keep in step.
        """
        field_name = PROVIDER_CONFIG_FIELDS.get(canonical_provider_id(provider_name))
        return None if field_name is None else getattr(self, field_name)


_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
# Distinct from ``KIMI_API_KEY``/``MINIMAX_API_KEY`` on purpose: those feed the
# OpenAI wire at ``api.moonshot.ai``/``api.minimax.io``, and one variable must
# never send one host's credential to another host.
_KIMI_CODING_API_KEY_ENV_VAR = "KIMI_CODING_API_KEY"
_MINIMAX_CN_API_KEY_ENV_VAR = "MINIMAX_CN_API_KEY"
_GOOGLE_API_KEY_ENV_VAR = "GOOGLE_API_KEY"
_COPILOT_TOKEN_ENV_VAR = "GITHUB_COPILOT_TOKEN"
_ENDPOINT_API_KEY_ENV_VAR = "ENDPOINT_API_KEY"
_ENDPOINT_BASE_URL_ENV_VAR = "ENDPOINT_BASE_URL"
_OPENROUTER_API_KEY_ENV_VAR = "OPENROUTER_API_KEY"


# =============================================================================
# Provider wires
# =============================================================================

type _ProviderShape = Literal[
    "openai",
    "anthropic",
    "google",
    "copilot",
    "endpoint",
    "named_endpoint",
    "openai_compatible",
]


@dataclass(frozen=True, slots=True)
class _ProviderWire:
    """How one built-in provider is wired into the provider-config passes.

    ``field_name`` names the provider in both ``ProviderConfigsPayload`` and
    ``ProviderConfigs`` (the parser builds one from the other by keyword), and
    ``payload_key`` is the spelling a config file uses for it. ``env_vars`` are
    the environment variables that configure the provider, credential first: an
    ``openai_compatible`` provider reads the rest as fallbacks, the generic
    ``endpoint`` provider takes its base URL from the second, and every other
    shape takes only the first.
    """

    field_name: str
    payload_key: str
    shape: _ProviderShape
    env_vars: tuple[str, ...]


def _provider_wires() -> dict[str, _ProviderWire]:
    """Canonical provider id -> its wire, in ``providers`` payload order.

    The id -> field/payload-key half is derived from the payload model, so the
    one hand-written part is the shape and the credential environment variables
    below: adding a vendor is its payload field plus one entry here.
    """
    details: dict[str, tuple[_ProviderShape, tuple[str, ...]]] = {
        "openai": ("openai", (_OPENAI_API_KEY_ENV_VAR,)),
        "anthropic": ("anthropic", (_ANTHROPIC_API_KEY_ENV_VAR,)),
        "kimi-coding": ("anthropic", (_KIMI_CODING_API_KEY_ENV_VAR,)),
        "minimax-cn": ("anthropic", (_MINIMAX_CN_API_KEY_ENV_VAR,)),
        "google": ("google", (_GOOGLE_API_KEY_ENV_VAR,)),
        "copilot": ("copilot", (_COPILOT_TOKEN_ENV_VAR,)),
        "endpoint": ("endpoint", (_ENDPOINT_API_KEY_ENV_VAR, _ENDPOINT_BASE_URL_ENV_VAR)),
        "opencode": ("named_endpoint", (_OPENCODE_API_KEY_ENV_VAR,)),
        "openrouter": ("named_endpoint", (_OPENROUTER_API_KEY_ENV_VAR,)),
        "deepseek": ("openai_compatible", (_DEEPSEEK_API_KEY_ENV_VAR,)),
        "zai": ("openai_compatible", (_ZAI_API_KEY_ENV_VAR,)),
        "zhipuai": ("openai_compatible", (_ZHIPU_API_KEY_ENV_VAR, _ZAI_API_KEY_ENV_VAR)),
        "grok": ("openai_compatible", (_XAI_API_KEY_ENV_VAR,)),
        "minimax": ("openai_compatible", (_MINIMAX_API_KEY_ENV_VAR,)),
        "kimi": ("openai_compatible", (_KIMI_API_KEY_ENV_VAR,)),
        "opencode-go": ("openai_compatible", (_OPENCODE_API_KEY_ENV_VAR,)),
        "qwen": ("openai_compatible", (_DASHSCOPE_API_KEY_ENV_VAR,)),
        "groq": ("openai_compatible", (_GROQ_API_KEY_ENV_VAR,)),
        "together": ("openai_compatible", (_TOGETHER_API_KEY_ENV_VAR,)),
        "fireworks": ("openai_compatible", (_FIREWORKS_API_KEY_ENV_VAR,)),
        "mistral": ("openai_compatible", (_MISTRAL_API_KEY_ENV_VAR,)),
    }
    return {
        provider_id: _ProviderWire(field_name, payload_key, *details[provider_id])
        for provider_id, (field_name, payload_key) in _PROVIDER_CONFIG_FIELD_ENTRIES.items()
    }


#: Canonical provider id -> its wire. The keys are the payload model's own
#: provider ids, so this table cannot drift from the config surface.
_PROVIDER_WIRES: Mapping[str, _ProviderWire] = _provider_wires()


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


def _provider_configs_from_entries(
    entries: Mapping[str, ProviderConfigEntry | None],
    *,
    custom: dict[str, ProviderEndpointConfig] | None = None,
) -> ProviderConfigs:
    """A ``ProviderConfigs`` holding ``entries``, keyed by ``ProviderConfigs`` field name.

    The field name comes from the provider wire, so nothing here lists provider
    ids. ``ProviderConfigs`` is frozen, hence ``object.__setattr__``; ``custom``
    is assigned on its own because it is a map rather than one provider's entry.
    """
    providers = ProviderConfigs()
    for field_name, entry in entries.items():
        object.__setattr__(providers, field_name, entry)
    if custom is not None:
        object.__setattr__(providers, "custom", custom)
    return providers


def _provider_config_from_env(wire: _ProviderWire, env: Mapping[str, str]) -> ProviderConfigEntry | None:
    """The config ``wire``'s credential environment variables imply, ``None`` when unset."""
    match wire.shape:
        case "endpoint":
            return _endpoint_provider_config_from_env(
                env,
                api_key_env_var=wire.env_vars[0],
                base_url_env_var=wire.env_vars[1],
            )
        case "named_endpoint":
            api_key = env.get(wire.env_vars[0])
            return None if not api_key else ProviderEndpointConfig(api_key=api_key)
        case "openai":
            api_key = env.get(wire.env_vars[0])
            return None if not api_key else OpenAIProviderConfig(api_key=api_key)
        case "anthropic":
            api_key = env.get(wire.env_vars[0])
            return None if not api_key else AnthropicProviderConfig(api_key=api_key)
        case "google":
            api_key = env.get(wire.env_vars[0])
            if not api_key:
                return None
            return GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="api_key", api_key=api_key))
        case "copilot":
            token = env.get(wire.env_vars[0])
            if not token:
                return None
            return CopilotProviderConfig(auth=CopilotProviderAuthConfig(method="token", token=token))
        case "openai_compatible":
            return _openai_compatible_provider_config_from_env(env, *wire.env_vars)


def provider_configs_from_env(env: Mapping[str, str]) -> ProviderConfigs | None:
    """Build provider config from credential environment variables alone.

    This keeps first-run provider setup lightweight: setting VOIDCODE_MODEL plus
    the provider's standard API-key environment variable is enough for runtime
    provider resolution without requiring a .voidcode.json providers block.
    """
    providers = _provider_configs_from_entries({wire.field_name: _provider_config_from_env(wire, env) for wire in _PROVIDER_WIRES.values()})
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
    return _provider_configs_from_entries(
        {
            wire.field_name: _MERGE_PROVIDER_CONFIG_BY_SHAPE[wire.shape](
                getattr(primary, wire.field_name),
                getattr(fallback, wire.field_name),
            )
            for wire in _PROVIDER_WIRES.values()
        },
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
        ssl_verify=_prefer_primary(primary.ssl_verify, fallback.ssl_verify),
        timeout_seconds=_prefer_primary(primary.timeout_seconds, fallback.timeout_seconds),
        model_map={**fallback.model_map, **primary.model_map},
        transient_retry=_prefer_primary(primary.transient_retry, fallback.transient_retry),
    )


#: How two entries of one shape merge, primary winning. Each merge function
#: takes its own config type, so the table is heterogeneous and is called with
#: entries that came from ``getattr`` on the two ``ProviderConfigs``.
_MERGE_PROVIDER_CONFIG_BY_SHAPE: Mapping[_ProviderShape, Callable[..., ProviderConfigEntry | None]] = {
    "openai": _merge_openai_provider_config,
    "anthropic": _merge_anthropic_provider_config,
    "google": _merge_google_provider_config,
    "copilot": _merge_copilot_provider_config,
    "endpoint": _merge_endpoint_provider_config,
    "named_endpoint": _merge_endpoint_provider_config,
    "openai_compatible": _merge_openai_compatible_provider_config,
}


def _endpoint_provider_config_from_env(
    env: Mapping[str, str],
    *,
    api_key_env_var: str,
    base_url_env_var: str,
) -> ProviderEndpointConfig | None:
    api_key = env.get(api_key_env_var)
    base_url = env.get(base_url_env_var)
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
    return any(getattr(providers, wire.field_name) for wire in _PROVIDER_WIRES.values()) or bool(providers.custom)


def _runtime_config_field_name(field_path: str) -> str | None:
    runtime_field_prefix = "runtime config field '"
    if field_path.startswith(runtime_field_prefix) and field_path.endswith("'"):
        return field_path[len(runtime_field_prefix) : -1]
    return None


def _append_config_field_suffix(field_path: str, suffix: str) -> str:
    runtime_field_name = _runtime_config_field_name(field_path)
    if runtime_field_name is not None:
        return format_runtime_config_field_error(f"{runtime_field_name}{suffix}")
    return f"{field_path}{suffix}"


def _extend_config_field_path(field_path: str, loc: tuple[object, ...]) -> str:
    extended = field_path
    for item in loc:
        if isinstance(item, int):
            extended = _append_config_field_suffix(extended, f"[{item}]")
            continue
        extended = _nested_config_field(extended, str(item))
    return extended


def _format_provider_payload_validation_error(
    *,
    field_path: str,
    error: dict[str, object],
    object_when_provided: bool = True,
) -> str:
    loc = tuple(cast(tuple[object, ...], error.get("loc", ())))
    error_type = error.get("type", "")
    target = _extend_config_field_path(field_path, loc)
    if error_type in {"model_type", "dict_type"}:
        suffix = " when provided" if object_when_provided else ""
        return f"{target} must be an object{suffix}"
    if error_type == "extra_forbidden":
        return f"{target} is not supported"
    # Imported lazily: ``provider.errors`` imports ``provider.protocol``, which
    # reaches back through ``model_catalog`` to this module.
    from .errors import validation_reason_from_error

    reason = validation_reason_from_error(error)
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


def _parse_provider_config_entry(
    wire: _ProviderWire,
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> ProviderConfigEntry | None:
    """Parse one ``providers.<id>`` block through its shape's parser."""
    match wire.shape:
        case "openai":
            return _parse_openai_provider_config(raw_value, field_path=field_path, env=env)
        case "anthropic":
            return _parse_anthropic_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                api_key_env_var=wire.env_vars[0],
            )
        case "google":
            return _parse_google_provider_config(raw_value, field_path=field_path, env=env)
        case "copilot":
            return _parse_copilot_provider_config(raw_value, field_path=field_path, env=env)
        case "endpoint" | "named_endpoint":
            return _parse_endpoint_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                default_api_key_env_var=wire.env_vars[0],
            )
        case "openai_compatible":
            return _parse_openai_compatible_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                api_key_env_var=wire.env_vars[0],
                fallback_api_key_env_vars=wire.env_vars[1:],
            )


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
        model_type=ProviderConfigsPayload,
    )

    environment: Mapping[str, str] = {} if env is None else env

    return _provider_configs_from_entries(
        {
            wire.field_name: _parse_provider_config_entry(
                wire,
                getattr(payload, wire.field_name),
                field_path=_nested_config_field(source, wire.payload_key),
                env=environment,
            )
            for wire in _PROVIDER_WIRES.values()
        },
        custom=_parse_custom_endpoint_provider_configs(
            payload.custom,
            field_path=_nested_config_field(source, "custom"),
            env=environment,
        ),
    )


def _serialize_provider_config_entry(
    entry: ProviderConfigEntry,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    """Serialize one provider's config through its own shape's serializer."""
    match entry:
        case OpenAIProviderConfig():
            return _serialize_openai_provider_config(entry, include_secrets=include_secrets)
        case AnthropicProviderConfig():
            return _serialize_anthropic_provider_config(entry, include_secrets=include_secrets)
        case GoogleProviderConfig():
            return _serialize_google_provider_config(entry, include_secrets=include_secrets)
        case CopilotProviderConfig():
            return _serialize_copilot_provider_config(entry, include_secrets=include_secrets)
        case ProviderEndpointConfig():
            return _serialize_endpoint_provider_config(entry, include_secrets=include_secrets)
        case OpenAICompatibleProviderConfig():
            return _serialize_openai_compatible_provider_config(entry, include_secrets=include_secrets)


def serialize_provider_configs(
    providers: ProviderConfigs | None,
    *,
    include_secrets: bool = False,
) -> dict[str, object] | None:
    if providers is None:
        return None
    serialized: dict[str, object] = {}
    for wire in _PROVIDER_WIRES.values():
        entry = getattr(providers, wire.field_name)
        if entry is None:
            continue
        serialized[wire.payload_key] = _serialize_provider_config_entry(
            entry,
            include_secrets=include_secrets,
        )
    if providers.custom:
        serialized["custom"] = {
            provider_name: _serialize_endpoint_provider_config(
                custom_config,
                include_secrets=include_secrets,
            )
            for provider_name, custom_config in providers.custom.items()
        }
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
    api_key_env_var: str,
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
        api_key = env.get(api_key_env_var)
    return AnthropicProviderConfig(
        api_key=api_key,
        base_url=payload.base_url,
        version=payload.version,
        beta_headers=payload.beta_headers or (),
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
        auth_header=payload.auth_header,
        auth_scheme=auth_scheme,
        auth_scheme_explicit=raw_auth_scheme is not None,
        ssl_verify=payload.ssl_verify,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map or {},
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
        ssl_verify=payload.ssl_verify,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map or {},
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

    payload = raw_value
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


def format_runtime_config_field_error(field_path: str) -> str:
    runtime_field_prefix = "runtime config field '"
    if field_path.startswith(runtime_field_prefix):
        if field_path.endswith("'"):
            return field_path
        if "'[" in field_path:
            base, suffix = field_path[len(runtime_field_prefix) :].split("'[", maxsplit=1)
            return f"{runtime_field_prefix}{base}[{suffix}'"
    return f"runtime config field '{field_path}'"
