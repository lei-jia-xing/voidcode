from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.functional_validators import BeforeValidator

from .naming import (
    BUILTIN_PROVIDER_IDS,
    PROVIDER_LABELS,
    UnknownProviderIdError,
    canonical_provider_id,
)
from .provider_table import PROVIDER_TABLE_BY_ID


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


class _ProviderPayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _OpenAIProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    organization: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None


class _AnthropicProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    version: BoundaryOptionalString = None
    beta_headers: BoundaryStringList | None = ()
    cache_retention: Literal["none", "short", "long"] = "short"
    timeout_seconds: BoundaryOptionalTimeout = None


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


class _CopilotProviderAuthConfigPayload(_ProviderPayloadModel):
    method: BoundaryRequiredString = Field(
        # ``_parse_copilot_auth_method`` owns the rejection.
        json_schema_extra={"enum": list(_VALID_COPILOT_AUTH_METHODS)},
    )
    token: BoundaryOptionalString = None
    token_env_var: BoundaryOptionalString = None


class _CopilotProviderConfigPayload(_ProviderPayloadModel):
    auth: _CopilotProviderAuthConfigPayload | None = None
    base_url: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None


class _ProviderEndpointConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    auth_header: BoundaryOptionalString = None
    auth_scheme: _BoundaryAuthScheme = None
    ssl_verify: BoundaryOptionalBool = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)


class _OpenAICompatibleProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    ssl_verify: BoundaryOptionalBool = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)


class ProviderConfigsPayload(_ProviderPayloadModel):
    openai: _OpenAIProviderConfigPayload | None = None
    anthropic: _AnthropicProviderConfigPayload | None = None
    kimi_code: _AnthropicProviderConfigPayload | None = Field(default=None, alias="kimi-code")
    minimax_cn: _AnthropicProviderConfigPayload | None = Field(default=None, alias="minimax-cn")
    google: _GoogleProviderConfigPayload | None = None
    github_copilot: _CopilotProviderConfigPayload | None = Field(default=None, alias="github-copilot")
    endpoint: _ProviderEndpointConfigPayload | None = None
    opencode_zen: _ProviderEndpointConfigPayload | None = Field(default=None, alias="opencode-zen")
    openrouter: _ProviderEndpointConfigPayload | None = None
    deepseek: _OpenAICompatibleProviderConfigPayload | None = None
    zai: _OpenAICompatibleProviderConfigPayload | None = None
    zhipuai: _OpenAICompatibleProviderConfigPayload | None = None
    xai: _OpenAICompatibleProviderConfigPayload | None = None
    minimax: _OpenAICompatibleProviderConfigPayload | None = None
    moonshot: _OpenAICompatibleProviderConfigPayload | None = None
    opencode_go: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="opencode-go")
    qwen: _OpenAICompatibleProviderConfigPayload | None = None
    groq: _OpenAICompatibleProviderConfigPayload | None = None
    together: _OpenAICompatibleProviderConfigPayload | None = None
    fireworks: _OpenAICompatibleProviderConfigPayload | None = None
    mistral: _OpenAICompatibleProviderConfigPayload | None = None
    aiand: _OpenAICompatibleProviderConfigPayload | None = None
    alibaba_token_plan: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="alibaba-token-plan")
    baseten: _OpenAICompatibleProviderConfigPayload | None = None
    cline_pass: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="cline-pass")
    coreweave: _OpenAICompatibleProviderConfigPayload | None = None
    gmi_cloud: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="gmi-cloud")
    huggingface: _OpenAICompatibleProviderConfigPayload | None = None
    kilo: _OpenAICompatibleProviderConfigPayload | None = None
    novita: _OpenAICompatibleProviderConfigPayload | None = None
    nvidia: _OpenAICompatibleProviderConfigPayload | None = None
    venice: _OpenAICompatibleProviderConfigPayload | None = None
    wafer_serverless: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="wafer-serverless")
    xiaomi: _OpenAICompatibleProviderConfigPayload | None = None
    xiaomi_token_plan_ams: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="xiaomi-token-plan-ams")
    xiaomi_token_plan_cn: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="xiaomi-token-plan-cn")
    xiaomi_token_plan_sgp: _OpenAICompatibleProviderConfigPayload | None = Field(default=None, alias="xiaomi-token-plan-sgp")
    zenmux: _OpenAICompatibleProviderConfigPayload | None = None
    custom: dict[str, _ProviderEndpointConfigPayload] = Field(
        default_factory=dict,
        json_schema_extra={"propertyNames": {"pattern": _CUSTOM_PROVIDER_KEY_PATTERN}},
    )


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
PROVIDER_CONFIG_PAYLOAD_KEYS: Mapping[str, str] = {
    provider_id: payload_key for provider_id, (_, payload_key) in _PROVIDER_CONFIG_FIELD_ENTRIES.items()
}

#: Canonical built-in provider id -> the ``ProviderConfigs`` field holding its
#: configuration. Custom providers live in the ``custom`` mapping instead.
PROVIDER_CONFIG_FIELDS: Mapping[str, str] = {provider_id: field_name for provider_id, (field_name, _) in _PROVIDER_CONFIG_FIELD_ENTRIES.items()}


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


#: Which config shape a ``providers.<id>`` payload field belongs to. The payload
#: model *is* the shape -- it decides which config dataclass the parser builds --
#: so the classification is read off the field's own annotation rather than a
#: second list of provider ids.
_SHAPE_BY_PAYLOAD_MODEL: Mapping[type[BaseModel], _ProviderShape] = {
    _OpenAIProviderConfigPayload: "openai",
    _AnthropicProviderConfigPayload: "anthropic",
    _GoogleProviderConfigPayload: "google",
    _CopilotProviderConfigPayload: "copilot",
    _ProviderEndpointConfigPayload: "named_endpoint",
    _OpenAICompatibleProviderConfigPayload: "openai_compatible",
}


def _payload_model_type(annotation: object) -> type[BaseModel]:
    for argument in get_args(annotation):
        if isinstance(argument, type) and issubclass(argument, BaseModel):
            return argument
    raise ValueError(f"provider payload field is not a single config model: {annotation!r}")


def _provider_wires() -> dict[str, _ProviderWire]:
    """Canonical provider id -> its wire, in ``providers`` payload order.

    The id -> field/payload-key half is derived from the payload model, the
    shape from that field's own config model, and the credential environment
    variables from ``provider_table.json``. A payload field with no table row
    raises here instead of resolving to a provider with no endpoint.
    """
    wires: dict[str, _ProviderWire] = {}
    for provider_id, (field_name, payload_key) in _PROVIDER_CONFIG_FIELD_ENTRIES.items():
        shape = _SHAPE_BY_PAYLOAD_MODEL[_payload_model_type(ProviderConfigsPayload.model_fields[field_name].annotation)]
        if provider_id == "endpoint":
            # The generic gateway is the one ``named_endpoint`` that owns the
            # generic ``ENDPOINT_*`` variables and its own default host.
            shape = "endpoint"
        wires[provider_id] = _ProviderWire(field_name, payload_key, shape, PROVIDER_TABLE_BY_ID[provider_id].env_vars)
    return wires


#: Canonical provider id -> its wire. The keys are the payload model's own
#: provider ids and the values come from the provider table, so this mapping
#: cannot drift from either the config surface or the vendor facts.
PROVIDER_WIRES: Mapping[str, _ProviderWire] = _provider_wires()


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
    payload_key = PROVIDER_CONFIG_PAYLOAD_KEYS.get(canonical_key)
    if payload_key is None:
        raise ValueError(f"{_nested_config_field(field_path, raw_key)}: {UnknownProviderIdError(raw_key).message}")
    return payload_key


# =============================================================================
# OpenAI-compatible provider configuration
# Provides one typed configuration shape for named OpenAI-compatible providers.
# =============================================================================


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


# Default endpoint per provider, read from the provider table. Only base URLs
# live there: the model list is always discovered from the provider's own host,
# so there is no second list to maintain and no listing URL to keep in step.
# The providers on this contract are the table rows whose config shape is
# ``openai_compatible`` -- a named endpoint, OpenAI itself, Copilot and the
# Anthropic-/Google-wire vendors resolve their own default elsewhere.
_OPENAI_COMPATIBLE_PROVIDER_NAMES = frozenset(provider_id for provider_id, wire in PROVIDER_WIRES.items() if wire.shape == "openai_compatible")


def openai_compatible_default_base_url(provider_name: str) -> str:
    if provider_name not in _OPENAI_COMPATIBLE_PROVIDER_NAMES:
        return ""
    return PROVIDER_TABLE_BY_ID[provider_name].default_base_url


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
# Provider Config Classes
# =============================================================================


@dataclass(frozen=True, slots=True)
class OpenAIProviderConfig:
    api_key: str | None = None
    base_url: str | None = None
    organization: str | None = None
    project: str | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    api_key: str | None = None
    base_url: str | None = None
    version: str | None = None
    beta_headers: tuple[str, ...] = ()
    cache_retention: Literal["none", "short", "long"] = "short"
    timeout_seconds: float | None = None


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


@dataclass(frozen=True, slots=True)
class CopilotProviderAuthConfig:
    method: CopilotAuthMethod
    token: str | None = None
    token_env_var: str | None = None


@dataclass(frozen=True, slots=True)
class CopilotProviderConfig:
    auth: CopilotProviderAuthConfig | None = None
    base_url: str | None = None
    timeout_seconds: float | None = None


#: Any configuration entry a built-in provider id can resolve to. Every member
#: carries a ``base_url``, which is what the readers of
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
    openai_organization: str | None = None
    openai_project: str | None = None
    wire: RoutedWire = "openai-chat-completions"
    cache_retention: Literal["none", "short", "long"] = "none"


@dataclass(frozen=True, slots=True)
class ProviderConfigs:
    openai: OpenAIProviderConfig | None = None
    anthropic: AnthropicProviderConfig | None = None
    kimi_code: AnthropicProviderConfig | None = None
    minimax_cn: AnthropicProviderConfig | None = None
    google: GoogleProviderConfig | None = None
    github_copilot: CopilotProviderConfig | None = None
    endpoint: ProviderEndpointConfig | None = None
    opencode_zen: ProviderEndpointConfig | None = None
    openrouter: ProviderEndpointConfig | None = None
    deepseek: OpenAICompatibleProviderConfig | None = None
    zai: OpenAICompatibleProviderConfig | None = None
    zhipuai: OpenAICompatibleProviderConfig | None = None
    xai: OpenAICompatibleProviderConfig | None = None
    minimax: OpenAICompatibleProviderConfig | None = None
    moonshot: OpenAICompatibleProviderConfig | None = None
    opencode_go: OpenAICompatibleProviderConfig | None = None
    qwen: OpenAICompatibleProviderConfig | None = None
    groq: OpenAICompatibleProviderConfig | None = None
    together: OpenAICompatibleProviderConfig | None = None
    fireworks: OpenAICompatibleProviderConfig | None = None
    mistral: OpenAICompatibleProviderConfig | None = None
    aiand: OpenAICompatibleProviderConfig | None = None
    alibaba_token_plan: OpenAICompatibleProviderConfig | None = None
    baseten: OpenAICompatibleProviderConfig | None = None
    cline_pass: OpenAICompatibleProviderConfig | None = None
    coreweave: OpenAICompatibleProviderConfig | None = None
    gmi_cloud: OpenAICompatibleProviderConfig | None = None
    huggingface: OpenAICompatibleProviderConfig | None = None
    kilo: OpenAICompatibleProviderConfig | None = None
    novita: OpenAICompatibleProviderConfig | None = None
    nvidia: OpenAICompatibleProviderConfig | None = None
    venice: OpenAICompatibleProviderConfig | None = None
    wafer_serverless: OpenAICompatibleProviderConfig | None = None
    xiaomi: OpenAICompatibleProviderConfig | None = None
    xiaomi_token_plan_ams: OpenAICompatibleProviderConfig | None = None
    xiaomi_token_plan_cn: OpenAICompatibleProviderConfig | None = None
    xiaomi_token_plan_sgp: OpenAICompatibleProviderConfig | None = None
    zenmux: OpenAICompatibleProviderConfig | None = None
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
    error: Mapping[str, object],
    object_when_provided: bool = True,
) -> str:
    raw_loc = error.get("loc", ())
    loc = tuple(raw_loc) if isinstance(raw_loc, (list, tuple)) else ()
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
        first = exc.errors(include_url=False)[0]
        error = first if isinstance(first, Mapping) else {}
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
            return _parse_openai_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                api_key_env_var=wire.env_vars[0],
            )
        case "anthropic":
            return _parse_anthropic_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                api_key_env_var=wire.env_vars[0],
            )
        case "google":
            return _parse_google_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                api_key_env_var=wire.env_vars[0],
            )
        case "copilot":
            return _parse_copilot_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                token_env_var=wire.env_vars[0],
            )
        case "endpoint" | "named_endpoint":
            return _parse_endpoint_provider_config(
                raw_value,
                field_path=field_path,
                env=env,
                default_api_key_env_var=wire.env_vars[0],
                default_base_url_env_var=wire.env_vars[1] if len(wire.env_vars) > 1 else None,
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
            for wire in PROVIDER_WIRES.values()
        },
        custom=_parse_custom_endpoint_provider_configs(
            payload.custom,
            field_path=_nested_config_field(source, "custom"),
            env=environment,
        ),
    )


def _parse_openai_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    api_key_env_var: str,
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
        api_key = env.get(api_key_env_var)
    return OpenAIProviderConfig(
        api_key=api_key,
        base_url=payload.base_url,
        organization=payload.organization,
        project=payload.project,
        timeout_seconds=payload.timeout_seconds,
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
        timeout_seconds=payload.timeout_seconds,
    )


def _parse_google_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    api_key_env_var: str,
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
        api_key_env_var=api_key_env_var,
    )
    return GoogleProviderConfig(
        auth=auth,
        base_url=payload.base_url,
        project=payload.project,
        region=payload.region,
        timeout_seconds=payload.timeout_seconds,
    )


def _parse_google_auth_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    api_key_env_var: str,
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
        api_key = env.get(api_key_env_var)
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
    token_env_var: str,
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
        default_token_env_var=token_env_var,
    )
    return CopilotProviderConfig(
        auth=auth,
        base_url=payload.base_url,
        timeout_seconds=payload.timeout_seconds,
    )


def _parse_copilot_auth_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    default_token_env_var: str,
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
        token = env.get(default_token_env_var)
    if token is None and token_env_var is None:
        raise ValueError(f"{field_path} requires token or token_env_var")
    if token is not None and token_env_var is not None:
        raise ValueError(f"{field_path} token and token_env_var must not both be set")

    return CopilotProviderAuthConfig(
        method=method,
        token=token,
        token_env_var=token_env_var,
    )


def _parse_endpoint_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    default_api_key_env_var: str,
    default_base_url_env_var: str | None = None,
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
        else:
            api_key = env.get(default_api_key_env_var)

    base_url = payload.base_url
    if base_url is None and default_base_url_env_var is not None:
        base_url = env.get(default_base_url_env_var)

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
        ssl_verify=payload.ssl_verify,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map or {},
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
    )


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
            # A custom provider is an endpoint-shaped gateway: it reads the
            # generic ENDPOINT_* variables, like the built-in ``endpoint``.
            default_api_key_env_var=PROVIDER_TABLE_BY_ID["endpoint"].env_vars[0],
            default_base_url_env_var=PROVIDER_TABLE_BY_ID["endpoint"].env_vars[1],
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
