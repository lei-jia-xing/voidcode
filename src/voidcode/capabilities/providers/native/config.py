from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError
from pydantic.functional_validators import BeforeValidator

from .naming import (
    BUILTIN_PROVIDER_IDS,
    UnknownProviderIdError,
    canonical_provider_id,
)
from .provider_table import PROVIDER_TABLE_BY_ID

type GoogleAuthMethod = Literal["api_key", "oauth", "service_account"]
type CopilotAuthMethod = Literal["token", "oauth"]
type EndpointAuthScheme = Literal["bearer", "token", "none"]
# The wires VoidCode implements. An internal endpoint marker, never user config:
# it identifies the request protocol used by per-model gateway routing.
type RoutedWire = Literal["openai-chat-completions", "anthropic-messages", "google-generative-ai"]


BoundaryOptionalString = StrictStr | None
BoundaryOptionalTimeout = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)] | None
BoundaryOptionalBool = StrictBool | None
BoundaryStringList = Annotated[list[StrictStr], Field(strict=True), BeforeValidator(lambda value: [] if value is None else value)]
NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
BoundaryStringMapping = Annotated[
    dict[NonEmptyString, NonEmptyString], Field(strict=True), BeforeValidator(lambda value: {} if value is None else value)
]


class _ProviderPayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class _OpenAIProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = Field(default=None, repr=False)
    base_url: BoundaryOptionalString = None
    organization: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None


class _AnthropicProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = Field(default=None, repr=False)
    base_url: BoundaryOptionalString = None
    version: BoundaryOptionalString = None
    beta_headers: BoundaryStringList = Field(default_factory=list)
    cache_retention: Literal["none", "short", "long"] = "short"
    timeout_seconds: BoundaryOptionalTimeout = None


class _GoogleProviderAuthConfigPayload(_ProviderPayloadModel):
    method: GoogleAuthMethod
    api_key: BoundaryOptionalString = Field(default=None, repr=False)
    access_token: BoundaryOptionalString = Field(default=None, repr=False)
    service_account_json_path: BoundaryOptionalString = None


class _GoogleProviderConfigPayload(_ProviderPayloadModel):
    auth: _GoogleProviderAuthConfigPayload | None = None
    base_url: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    region: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None


class _CopilotProviderAuthConfigPayload(_ProviderPayloadModel):
    method: CopilotAuthMethod
    token: BoundaryOptionalString = Field(default=None, repr=False)
    token_env_var: BoundaryOptionalString = None


class _CopilotProviderConfigPayload(_ProviderPayloadModel):
    auth: _CopilotProviderAuthConfigPayload | None = None
    base_url: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None


class _ProviderEndpointConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = Field(default=None, repr=False)
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    auth_header: BoundaryOptionalString = None
    auth_scheme: EndpointAuthScheme | None = None
    ssl_verify: BoundaryOptionalBool = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)


class _OpenAICompatibleProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = Field(default=None, repr=False)
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
    custom: dict[str, _ProviderEndpointConfigPayload] = Field(default_factory=dict)


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
    _ProviderEndpointConfigPayload: "endpoint",
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

    api_key: str | None = field(default=None, repr=False)
    api_key_env_var: str | None = None
    base_url: str | None = None
    ssl_verify: bool | None = None
    timeout_seconds: float | None = None
    model_map: dict[str, str] = field(default_factory=dict)


# Default endpoint per provider, read from the provider table. Only base URLs
# live there; model metadata comes from the generated models.dev catalog.
# The providers on this contract are the table rows whose config shape is
# ``openai_compatible`` -- a named endpoint, OpenAI itself, Copilot and the
# Anthropic-/Google-wire vendors resolve their own default elsewhere.
_OPENAI_COMPATIBLE_PROVIDER_NAMES = frozenset(provider_id for provider_id, wire in PROVIDER_WIRES.items() if wire.shape == "openai_compatible")


def openai_compatible_endpoint_config(
    provider_name: str,
    config: OpenAICompatibleProviderConfig | None,
) -> ProviderEndpointConfig:
    if provider_name not in _OPENAI_COMPATIBLE_PROVIDER_NAMES:
        raise ValueError(f"Unknown OpenAI-compatible provider: {provider_name!r}")
    default_base_url = PROVIDER_TABLE_BY_ID[provider_name].default_base_url
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
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    organization: str | None = None
    project: str | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    version: str | None = None
    beta_headers: tuple[str, ...] = ()
    cache_retention: Literal["none", "short", "long"] = "short"
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class GoogleProviderAuthConfig:
    method: GoogleAuthMethod
    api_key: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
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
    token: str | None = field(default=None, repr=False)
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
    api_key: str | None = field(default=None, repr=False)
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


def _extend_config_field_path(field_path: str, loc: tuple[object, ...]) -> str:
    for item in loc:
        field_path += f"[{item}]" if isinstance(item, int) else f".{item}"
    return field_path


def _format_provider_payload_validation_error(
    *,
    field_path: str,
    error: Mapping[str, object],
) -> str:
    raw_loc = error.get("loc", ())
    loc = tuple(raw_loc) if isinstance(raw_loc, (list, tuple)) else ()
    error_type = error.get("type", "")
    target = _extend_config_field_path(field_path, loc)
    if error_type in {"model_type", "dict_type"}:
        return f"{target} must be an object when provided"
    if error_type == "extra_forbidden":
        return f"{target} is not supported"
    return f"{target} {error['msg']}"


def _parse_provider_config_entry(
    wire: _ProviderWire,
    payload: ProviderConfigsPayload,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> ProviderConfigEntry | None:
    """Resolve one already validated provider block through its shape's parser."""
    raw_value = getattr(payload, wire.field_name)
    if raw_value is None:
        return None
    match wire.shape:
        case "openai":
            return _parse_openai_provider_config(
                raw_value,
                env=env,
                api_key_env_var=wire.env_vars[0],
            )
        case "anthropic":
            return _parse_anthropic_provider_config(
                raw_value,
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
        case "endpoint":
            return _parse_endpoint_provider_config(
                raw_value,
                env=env,
                default_api_key_env_var=wire.env_vars[0],
                default_base_url_env_var=wire.env_vars[1] if len(wire.env_vars) > 1 else None,
            )
        case "openai_compatible":
            return _parse_openai_compatible_provider_config(
                raw_value,
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
    try:
        payload = ProviderConfigsPayload.model_validate(_canonicalize_provider_config_payload_keys(raw_providers, field_path=source))
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        raise ValueError(_format_provider_payload_validation_error(field_path=source, error=error)) from exc

    environment: Mapping[str, str] = {} if env is None else env

    return _provider_configs_from_entries(
        {
            wire.field_name: _parse_provider_config_entry(
                wire,
                payload,
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
    payload: _OpenAIProviderConfigPayload,
    *,
    env: Mapping[str, str],
    api_key_env_var: str,
) -> OpenAIProviderConfig:
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
    payload: _AnthropicProviderConfigPayload,
    *,
    env: Mapping[str, str],
    api_key_env_var: str,
) -> AnthropicProviderConfig:
    api_key = payload.api_key
    if api_key is None:
        api_key = env.get(api_key_env_var)
    return AnthropicProviderConfig(
        api_key=api_key,
        base_url=payload.base_url,
        version=payload.version,
        beta_headers=tuple(payload.beta_headers),
        cache_retention=payload.cache_retention,
        timeout_seconds=payload.timeout_seconds,
    )


def _parse_google_provider_config(
    payload: _GoogleProviderConfigPayload,
    *,
    field_path: str,
    env: Mapping[str, str],
    api_key_env_var: str,
) -> GoogleProviderConfig:
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
    payload: _GoogleProviderAuthConfigPayload | None,
    *,
    field_path: str,
    env: Mapping[str, str],
    api_key_env_var: str,
) -> GoogleProviderAuthConfig | None:
    if payload is None:
        return None

    method = payload.method

    api_key = payload.api_key
    if method == "api_key" and api_key is None:
        api_key = env.get(api_key_env_var)
    access_token = payload.access_token
    service_account_json_path = payload.service_account_json_path

    credentials = {"api_key": api_key, "access_token": access_token, "service_account_json_path": service_account_json_path}
    required_field = {"api_key": "api_key", "oauth": "access_token", "service_account": "service_account_json_path"}[method]
    if credentials[required_field] is None:
        raise ValueError(f"{_nested_config_field(field_path, required_field)} must be provided when method is {method}")
    for name, value in credentials.items():
        if name != required_field and value is not None:
            raise ValueError(f"{_nested_config_field(field_path, name)} must not be set when method is {method}")

    return GoogleProviderAuthConfig(
        method=method,
        api_key=api_key,
        access_token=access_token,
        service_account_json_path=service_account_json_path,
    )


def _parse_copilot_provider_config(
    payload: _CopilotProviderConfigPayload,
    *,
    field_path: str,
    env: Mapping[str, str],
    token_env_var: str,
) -> CopilotProviderConfig:
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
    payload: _CopilotProviderAuthConfigPayload | None,
    *,
    field_path: str,
    env: Mapping[str, str],
    default_token_env_var: str,
) -> CopilotProviderAuthConfig | None:
    if payload is None:
        return None

    method = payload.method

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
    payload: _ProviderEndpointConfigPayload,
    *,
    env: Mapping[str, str],
    default_api_key_env_var: str,
    default_base_url_env_var: str | None = None,
) -> ProviderEndpointConfig:
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

    return ProviderEndpointConfig(
        api_key=api_key,
        api_key_env_var=api_key_env_var,
        base_url=base_url,
        auth_header=payload.auth_header,
        auth_scheme=payload.auth_scheme or "bearer",
        ssl_verify=payload.ssl_verify,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map,
    )


def _parse_openai_compatible_provider_config(
    payload: _OpenAICompatibleProviderConfigPayload,
    *,
    env: Mapping[str, str],
    api_key_env_var: str,
    fallback_api_key_env_vars: tuple[str, ...] = (),
) -> OpenAICompatibleProviderConfig:
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
        model_map=payload.model_map,
    )


def _parse_custom_endpoint_provider_configs(
    payload: dict[str, _ProviderEndpointConfigPayload],
    *,
    field_path: str,
    env: Mapping[str, str],
) -> dict[str, ProviderEndpointConfig]:
    parsed: dict[str, ProviderEndpointConfig] = {}
    spelled: dict[str, str] = {}
    for raw_provider_name, provider_payload in payload.items():
        if not raw_provider_name:
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
            env=env,
            # A custom provider is an endpoint-shaped gateway: it reads the
            # generic ENDPOINT_* variables, like the built-in ``endpoint``.
            default_api_key_env_var=PROVIDER_TABLE_BY_ID["endpoint"].env_vars[0],
            default_base_url_env_var=PROVIDER_TABLE_BY_ID["endpoint"].env_vars[1],
        )
        parsed[normalized_provider_name] = parsed_config
    return parsed


def _nested_config_field(source: str, nested: str) -> str:
    return f"{source}.{nested}"
