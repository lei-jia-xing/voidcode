from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.functional_validators import BeforeValidator


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
BoundaryOptionalPositiveInt = Annotated[
    int | None, BeforeValidator(_parse_optional_boundary_positive_int)
]
BoundaryStringList = Annotated[tuple[str, ...], BeforeValidator(_parse_boundary_string_list)]
BoundaryStringMapping = Annotated[dict[str, str], BeforeValidator(_parse_boundary_string_mapping)]


class _ProviderPayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _OpenAIProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    organization: BoundaryOptionalString = None
    project: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None


class _AnthropicProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    version: BoundaryOptionalString = None
    beta_headers: BoundaryStringList = ()
    timeout_seconds: BoundaryOptionalTimeout = None


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


class _LiteLLMProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    auth_header: BoundaryOptionalString = None
    auth_scheme: BoundaryOptionalString = "bearer"
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)


class _SimplifiedProviderConfigPayload(_ProviderPayloadModel):
    api_key: BoundaryOptionalString = None
    api_key_env_var: BoundaryOptionalString = None
    base_url: BoundaryOptionalString = None
    discovery_base_url: BoundaryOptionalString = None
    timeout_seconds: BoundaryOptionalTimeout = None
    model_map: BoundaryStringMapping = Field(default_factory=dict)


class _ProviderConfigsPayload(_ProviderPayloadModel):
    openai: _OpenAIProviderConfigPayload | None = None
    anthropic: _AnthropicProviderConfigPayload | None = None
    google: _GoogleProviderConfigPayload | None = None
    copilot: _CopilotProviderConfigPayload | None = None
    litellm: _LiteLLMProviderConfigPayload | None = None
    glm: _SimplifiedProviderConfigPayload | None = None
    minimax: _SimplifiedProviderConfigPayload | None = None
    kimi: _SimplifiedProviderConfigPayload | None = None
    opencode_go: _SimplifiedProviderConfigPayload | None = Field(default=None, alias="opencode-go")
    qwen: _SimplifiedProviderConfigPayload | None = None
    custom: dict[str, _LiteLLMProviderConfigPayload] = Field(default_factory=dict)


class _ProviderFallbackPayload(_ProviderPayloadModel):
    preferred_model: BoundaryRequiredString
    fallback_models: BoundaryStringList = ()

# =============================================================================
# Simplified Provider Config for Chinese AI Providers
# Provides a unified, minimal configuration interface for GLM, MiniMax, Kimi, OpenCode Go, and Qwen
# =============================================================================


@dataclass(frozen=True, slots=True)
class SimplifiedProviderConfig:
    """Simplified provider configuration for Chinese AI providers.

    All providers follow a minimal auth pattern: API_KEY + optional BASE_URL.
    This reduces cognitive overhead and aligns with user expectations.
    """

    api_key: str | None = None
    api_key_env_var: str | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    timeout_seconds: float | None = None
    model_map: dict[str, str] = field(default_factory=dict)


_SIMPLIFIED_DEFAULTS: dict[str, tuple[str, str | None, dict[str, str]]] = {
    "glm": (
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/api/paas/v4",
        {
            "glm-4-flash": "glm-4-flash",
            "glm-4-plus": "glm-4-plus",
            "glm-4": "glm-4-flash",
            "glm-5": "glm-5",
            "glm-5-turbo": "glm-5-turbo",
        },
    ),
    "minimax": (
        "https://api.minimax.io",
        "",
        {
            "minimax-m2.7": "MiniMax-M2.7",
            "minimax-m2.5": "MiniMax-M2.5",
            "minimax-m2.1": "MiniMax-M2.1",
            "minimax-m2": "MiniMax-M2",
        },
    ),
    "kimi": (
        "https://api.moonshot.ai",
        "https://api.moonshot.ai/v1",
        {
            "kimi-k2.5": "kimi-k2.5",
            "kimi-k2": "kimi-k2",
            "kimi-k2-turbo": "kimi-k2-turbo-preview",
            "kimi-k2-thinking": "kimi-k2-thinking",
        },
    ),
    "opencode-go": (
        "https://opencode.ai/zen/go",
        "",
        {
            "glm-5": "glm-5",
            "glm-5.1": "glm-5.1",
            "kimi-k2.5": "kimi-k2.5",
            "kimi-k2.6": "kimi-k2.6",
            "mimo-v2-omni": "mimo-v2-omni",
            "mimo-v2-pro": "mimo-v2-pro",
            "mimo-v2.5": "mimo-v2.5",
            "mimo-v2.5-pro": "mimo-v2.5-pro",
            "minimax-m2.5": "minimax-m2.5",
            "minimax-m2.7": "minimax-m2.7",
            "qwen3.5-plus": "qwen3.5-plus",
            "qwen3.6-plus": "qwen3.6-plus",
        },
    ),
    "qwen": (
        "https://dashscope.aliyuncs.com/compatible-mode",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        {
            "qwen-plus": "qwen-plus",
            "qwen-max": "qwen-max",
            "qwen-flash": "qwen-flash",
            "qwen3.5-plus": "qwen3.5-plus",
            "qwen3.5-flash": "qwen3.5-flash",
            "qwq-plus": "qwq-plus",
        },
    ),
}


_SIMPLIFIED_PROVIDER_NAMES = frozenset(_SIMPLIFIED_DEFAULTS)


def simplified_defaults(provider_name: str) -> tuple[str, dict[str, str]]:
    default = _SIMPLIFIED_DEFAULTS.get(provider_name, ("", "", {}))
    return default[0], dict(default[2])


def simplified_discovery_base_url(provider_name: str) -> str | None:
    default = _SIMPLIFIED_DEFAULTS.get(provider_name)
    if default is None:
        return None
    return default[1]


def simplified_config_to_litellm(
    provider_name: str,
    config: SimplifiedProviderConfig | None,
) -> LiteLLMProviderConfig | None:
    if config is None:
        return None
    if provider_name not in _SIMPLIFIED_PROVIDER_NAMES:
        raise ValueError(f"Unknown simplified provider: {provider_name!r}")
    default_base_url, default_model_map = simplified_defaults(provider_name)
    default_discovery_base_url = simplified_discovery_base_url(provider_name)
    discovery_base_url = (
        config.discovery_base_url
        if config.discovery_base_url is not None
        else default_discovery_base_url
    )
    return LiteLLMProviderConfig(
        api_key=config.api_key,
        base_url=config.base_url if config.base_url else default_base_url,
        discovery_base_url=discovery_base_url,
        timeout_seconds=config.timeout_seconds,
        model_map=dict(config.model_map) if config.model_map else default_model_map,
    )


# =============================================================================
# Environment Variables for Chinese AI Providers
# =============================================================================

_GLM_API_KEY_ENV_VAR = "GLM_API_KEY"
_GLM_ZAI_API_KEY_ENV_VAR = "ZAI_API_KEY"
_GLM_ZHIPU_API_KEY_ENV_VAR = "ZHIPU_API_KEY"
_MINIMAX_API_KEY_ENV_VAR = "MINIMAX_API_KEY"
_KIMI_API_KEY_ENV_VAR = "KIMI_API_KEY"
_OPENCODE_API_KEY_ENV_VAR = "OPENCODE_API_KEY"


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


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    api_key: str | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    version: str | None = None
    beta_headers: tuple[str, ...] = ()
    timeout_seconds: float | None = None


type GoogleAuthMethod = Literal["api_key", "oauth", "service_account"]


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


@dataclass(frozen=True, slots=True)
class LiteLLMProviderConfig:
    api_key: str | None = None
    api_key_env_var: str | None = None
    base_url: str | None = None
    discovery_base_url: str | None = None
    auth_header: str | None = None
    auth_scheme: Literal["bearer", "token", "none"] = "bearer"
    timeout_seconds: float | None = None
    model_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderConfigs:
    openai: OpenAIProviderConfig | None = None
    anthropic: AnthropicProviderConfig | None = None
    google: GoogleProviderConfig | None = None
    copilot: CopilotProviderConfig | None = None
    litellm: LiteLLMProviderConfig | None = None
    glm: SimplifiedProviderConfig | None = None
    minimax: SimplifiedProviderConfig | None = None
    kimi: SimplifiedProviderConfig | None = None
    opencode_go: SimplifiedProviderConfig | None = None
    qwen: SimplifiedProviderConfig | None = None
    custom: dict[str, LiteLLMProviderConfig] = field(default_factory=dict)


_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
_GOOGLE_API_KEY_ENV_VAR = "GOOGLE_API_KEY"
_COPILOT_TOKEN_ENV_VAR = "GITHUB_COPILOT_TOKEN"
_LITELLM_API_KEY_ENV_VAR = "LITELLM_API_KEY"
_LITELLM_PROXY_API_KEY_ENV_VAR = "LITELLM_PROXY_API_KEY"
_LITELLM_BASE_URL_ENV_VAR = "LITELLM_BASE_URL"
_LITELLM_PROXY_URL_ENV_VAR = "LITELLM_PROXY_URL"

_VALID_GOOGLE_AUTH_METHODS: tuple[GoogleAuthMethod, ...] = (
    "api_key",
    "oauth",
    "service_account",
)
_VALID_COPILOT_AUTH_METHODS: tuple[CopilotAuthMethod, ...] = ("token", "oauth")
_VALID_LITELLM_AUTH_SCHEMES: tuple[Literal["bearer", "token", "none"], ...] = (
    "bearer",
    "token",
    "none",
)
_BUILTIN_PROVIDER_NAMES: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "copilot",
        "litellm",
        "opencode",
        "glm",
        "minimax",
        "kimi",
        "opencode-go",
        "qwen",
    }
)


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
        openai=(
            OpenAIProviderConfig(api_key=openai_key)
            if (openai_key := env.get(_OPENAI_API_KEY_ENV_VAR))
            else None
        ),
        anthropic=(
            AnthropicProviderConfig(api_key=anthropic_key)
            if (anthropic_key := env.get(_ANTHROPIC_API_KEY_ENV_VAR))
            else None
        ),
        google=(
            GoogleProviderConfig(
                auth=GoogleProviderAuthConfig(method="api_key", api_key=google_key)
            )
            if (google_key := env.get(_GOOGLE_API_KEY_ENV_VAR))
            else None
        ),
        copilot=(
            CopilotProviderConfig(
                auth=CopilotProviderAuthConfig(method="token", token=copilot_token)
            )
            if (copilot_token := env.get(_COPILOT_TOKEN_ENV_VAR))
            else None
        ),
        litellm=_litellm_provider_config_from_env(env),
        glm=_simplified_provider_config_from_env(
            env,
            _GLM_ZAI_API_KEY_ENV_VAR,
            _GLM_ZHIPU_API_KEY_ENV_VAR,
            _GLM_API_KEY_ENV_VAR,
        ),
        minimax=_simplified_provider_config_from_env(env, _MINIMAX_API_KEY_ENV_VAR),
        kimi=_simplified_provider_config_from_env(env, _KIMI_API_KEY_ENV_VAR),
        opencode_go=_simplified_provider_config_from_env(env, _OPENCODE_API_KEY_ENV_VAR),
        qwen=_simplified_provider_config_from_env(env, "DASHSCOPE_API_KEY"),
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
        openai=primary.openai or fallback.openai,
        anthropic=primary.anthropic or fallback.anthropic,
        google=primary.google or fallback.google,
        copilot=primary.copilot or fallback.copilot,
        litellm=primary.litellm or fallback.litellm,
        glm=primary.glm or fallback.glm,
        minimax=primary.minimax or fallback.minimax,
        kimi=primary.kimi or fallback.kimi,
        opencode_go=primary.opencode_go or fallback.opencode_go,
        qwen=primary.qwen or fallback.qwen,
        custom={**fallback.custom, **primary.custom},
    )


def _litellm_provider_config_from_env(env: Mapping[str, str]) -> LiteLLMProviderConfig | None:
    api_key = env.get(_LITELLM_API_KEY_ENV_VAR) or env.get(_LITELLM_PROXY_API_KEY_ENV_VAR)
    base_url = env.get(_LITELLM_BASE_URL_ENV_VAR) or env.get(_LITELLM_PROXY_URL_ENV_VAR)
    if api_key is None and base_url is None:
        return None
    return LiteLLMProviderConfig(api_key=api_key, base_url=base_url)


def _simplified_provider_config_from_env(
    env: Mapping[str, str],
    *api_key_env_vars: str,
) -> SimplifiedProviderConfig | None:
    for api_key_env_var in api_key_env_vars:
        api_key = env.get(api_key_env_var)
        if api_key is not None:
            return SimplifiedProviderConfig(api_key=api_key)
    return None


def _provider_configs_has_entries(providers: ProviderConfigs) -> bool:
    return any(
        (
            providers.openai,
            providers.anthropic,
            providers.google,
            providers.copilot,
            providers.litellm,
            providers.glm,
            providers.minimax,
            providers.kimi,
            providers.opencode_go,
            providers.qwen,
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
        raise ValueError(
            _format_provider_payload_validation_error(field_path=field_path, error=error)
        ) from exc


def parse_provider_configs_payload(
    raw_providers: object,
    *,
    source: str,
    env: Mapping[str, str] | None = None,
) -> ProviderConfigs | None:
    if raw_providers is None:
        return None
    payload = _validate_provider_payload_model(
        raw_providers,
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
        litellm=_parse_litellm_provider_config(
            payload.litellm,
            field_path=_nested_config_field(source, "litellm"),
            env=environment,
        ),
        glm=_parse_simplified_provider_config(
            payload.glm,
            field_path=_nested_config_field(source, "glm"),
            env=environment,
            api_key_env_var=_GLM_API_KEY_ENV_VAR,
        ),
        minimax=_parse_simplified_provider_config(
            payload.minimax,
            field_path=_nested_config_field(source, "minimax"),
            env=environment,
            api_key_env_var=_MINIMAX_API_KEY_ENV_VAR,
        ),
        kimi=_parse_simplified_provider_config(
            payload.kimi,
            field_path=_nested_config_field(source, "kimi"),
            env=environment,
            api_key_env_var=_KIMI_API_KEY_ENV_VAR,
        ),
        opencode_go=_parse_simplified_provider_config(
            payload.opencode_go,
            field_path=_nested_config_field(source, "opencode-go"),
            env=environment,
            api_key_env_var=_OPENCODE_API_KEY_ENV_VAR,
        ),
        qwen=_parse_simplified_provider_config(
            payload.qwen,
            field_path=_nested_config_field(source, "qwen"),
            env=environment,
            api_key_env_var="DASHSCOPE_API_KEY",
        ),
        custom=_parse_custom_litellm_provider_configs(
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
    if providers.litellm is not None:
        serialized["litellm"] = _serialize_litellm_provider_config(
            providers.litellm,
            include_secrets=include_secrets,
        )
    if providers.glm is not None:
        serialized["glm"] = _serialize_simplified_provider_config(
            providers.glm,
            include_secrets=include_secrets,
        )
    if providers.minimax is not None:
        serialized["minimax"] = _serialize_simplified_provider_config(
            providers.minimax,
            include_secrets=include_secrets,
        )
    if providers.kimi is not None:
        serialized["kimi"] = _serialize_simplified_provider_config(
            providers.kimi,
            include_secrets=include_secrets,
        )
    if providers.opencode_go is not None:
        serialized["opencode-go"] = _serialize_simplified_provider_config(
            providers.opencode_go,
            include_secrets=include_secrets,
        )
    if providers.qwen is not None:
        serialized["qwen"] = _serialize_simplified_provider_config(
            providers.qwen,
            include_secrets=include_secrets,
        )
    if providers.custom:
        custom_payload: dict[str, object] = {}
        for provider_name, custom_config in providers.custom.items():
            custom_payload[provider_name] = _serialize_litellm_provider_config(
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
        timeout_seconds=payload.timeout_seconds,
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

    raw_method = payload.method
    if raw_method not in _VALID_GOOGLE_AUTH_METHODS:
        allowed = ", ".join(_VALID_GOOGLE_AUTH_METHODS)
        raise ValueError(f"{_nested_config_field(field_path, 'method')} must be one of: {allowed}")
    method = raw_method

    api_key = payload.api_key
    if api_key is None:
        api_key = env.get(_GOOGLE_API_KEY_ENV_VAR)
    access_token = payload.access_token
    service_account_json_path = payload.service_account_json_path

    if method == "api_key":
        if api_key is None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'api_key')} "
                "must be provided when method is api_key"
            )
        if access_token is not None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'access_token')} "
                "must not be set when method is api_key"
            )
        if service_account_json_path is not None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'service_account_json_path')} "
                "must not be set when method is api_key"
            )
    elif method == "oauth":
        if access_token is None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'access_token')} "
                "must be provided when method is oauth"
            )
        if api_key is not None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'api_key')} "
                "must not be set when method is oauth"
            )
        if service_account_json_path is not None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'service_account_json_path')} "
                "must not be set when method is oauth"
            )
    elif service_account_json_path is None:
        raise ValueError(
            f"{_nested_config_field(field_path, 'service_account_json_path')} "
            "must be provided when method is service_account"
        )
    elif api_key is not None:
        raise ValueError(
            f"{_nested_config_field(field_path, 'api_key')} "
            "must not be set when method is service_account"
        )
    elif access_token is not None:
        raise ValueError(
            f"{_nested_config_field(field_path, 'access_token')} "
            "must not be set when method is service_account"
        )

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

    raw_method = payload.method
    if raw_method not in _VALID_COPILOT_AUTH_METHODS:
        allowed = ", ".join(_VALID_COPILOT_AUTH_METHODS)
        raise ValueError(f"{_nested_config_field(field_path, 'method')} must be one of: {allowed}")
    method = raw_method

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
                f"{_nested_config_field(field_path, 'token')} and "
                f"{_nested_config_field(field_path, 'token_env_var')} "
                "must not both be set"
            )
        if refresh_token is not None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'refresh_token')} "
                "must not be set when method is token"
            )
        if refresh_leeway_seconds is not None:
            raise ValueError(
                f"{_nested_config_field(field_path, 'refresh_leeway_seconds')} "
                "must not be set when method is token"
            )
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


def _parse_litellm_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> LiteLLMProviderConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _LiteLLMProviderConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_LiteLLMProviderConfigPayload,
        )
    )

    api_key = payload.api_key
    api_key_env_var = payload.api_key_env_var
    if api_key is None:
        if api_key_env_var is not None:
            api_key = env.get(api_key_env_var)
        else:
            api_key = env.get(_LITELLM_API_KEY_ENV_VAR) or env.get(_LITELLM_PROXY_API_KEY_ENV_VAR)

    base_url = payload.base_url
    if base_url is None:
        base_url = env.get(_LITELLM_BASE_URL_ENV_VAR) or env.get(_LITELLM_PROXY_URL_ENV_VAR)

    raw_auth_scheme = payload.auth_scheme
    auth_scheme: Literal["bearer", "token", "none"] = "bearer"
    if raw_auth_scheme is not None:
        if raw_auth_scheme not in _VALID_LITELLM_AUTH_SCHEMES:
            allowed = ", ".join(_VALID_LITELLM_AUTH_SCHEMES)
            raise ValueError(
                f"{_nested_config_field(field_path, 'auth_scheme')} must be one of: {allowed}"
            )
        auth_scheme = raw_auth_scheme

    return LiteLLMProviderConfig(
        api_key=api_key,
        api_key_env_var=api_key_env_var,
        base_url=base_url,
        discovery_base_url=payload.discovery_base_url,
        auth_header=payload.auth_header,
        auth_scheme=auth_scheme,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map,
    )


def _parse_simplified_provider_config(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
    api_key_env_var: str,
) -> SimplifiedProviderConfig | None:
    if raw_value is None:
        return None
    payload = (
        raw_value
        if isinstance(raw_value, _SimplifiedProviderConfigPayload)
        else _validate_provider_payload_model(
            raw_value,
            field_path=field_path,
            model_type=_SimplifiedProviderConfigPayload,
        )
    )

    api_key = payload.api_key
    api_key_env = payload.api_key_env_var
    if api_key is None:
        if api_key_env is not None:
            api_key = env.get(api_key_env)
        else:
            api_key = env.get(api_key_env_var)

    return SimplifiedProviderConfig(
        api_key=api_key,
        api_key_env_var=api_key_env,
        base_url=payload.base_url,
        discovery_base_url=payload.discovery_base_url,
        timeout_seconds=payload.timeout_seconds,
        model_map=payload.model_map,
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
    if provider.beta_headers:
        payload["beta_headers"] = list(provider.beta_headers)
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    return payload


def _serialize_google_provider_config(
    provider: GoogleProviderConfig,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if provider.auth is not None:
        payload["auth"] = _serialize_google_auth_config(
            provider.auth, include_secrets=include_secrets
        )
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


def _serialize_litellm_provider_config(
    provider: LiteLLMProviderConfig,
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
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.model_map:
        payload["model_map"] = dict(provider.model_map)
    return payload


def _serialize_simplified_provider_config(
    provider: SimplifiedProviderConfig,
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
    if provider.timeout_seconds is not None:
        payload["timeout_seconds"] = provider.timeout_seconds
    if provider.model_map:
        payload["model_map"] = dict(provider.model_map)
    return payload


def _parse_custom_litellm_provider_configs(
    raw_value: object,
    *,
    field_path: str,
    env: Mapping[str, str],
) -> dict[str, LiteLLMProviderConfig]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{field_path} must be an object when provided")

    payload = cast(dict[object, object], raw_value)
    parsed: dict[str, LiteLLMProviderConfig] = {}
    for raw_provider_name, provider_payload in payload.items():
        if not isinstance(raw_provider_name, str) or not raw_provider_name:
            raise ValueError(f"{field_path} keys must be non-empty strings")
        if raw_provider_name != raw_provider_name.strip():
            raise ValueError(
                f"{_nested_config_field(field_path, raw_provider_name)} "
                "must not have leading or trailing whitespace"
            )
        if "/" in raw_provider_name:
            raise ValueError(
                f"{_nested_config_field(field_path, raw_provider_name)} must not contain '/'"
            )
        normalized_provider_name = raw_provider_name.strip().lower()
        if normalized_provider_name in _BUILTIN_PROVIDER_NAMES:
            raise ValueError(
                f"{_nested_config_field(field_path, raw_provider_name)} "
                "must not collide with built-in provider names "
                f"(conflicts with '{normalized_provider_name}')"
            )

        parsed_config = _parse_litellm_provider_config(
            provider_payload,
            field_path=_nested_config_field(field_path, raw_provider_name),
            env=env,
        )
        if parsed_config is None:
            continue
        parsed[raw_provider_name] = parsed_config
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
