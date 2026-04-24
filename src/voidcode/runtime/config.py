from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..agent import AgentManifestId, get_builtin_agent_manifest, list_builtin_agent_manifests
from ..hook.config import FormatterCwdPolicy, RuntimeFormatterPresetConfig, RuntimeHooksConfig
from ..lsp import LspServerConfigOverride as RuntimeLspServerConfig
from ..lsp import derive_workspace_lsp_defaults, has_builtin_lsp_server_preset
from ..provider import config as provider_config
from .permission import PermissionDecision

RuntimeProviderFallbackConfig = provider_config.ProviderFallbackConfig
RuntimeProvidersConfig = provider_config.ProviderConfigs
parse_provider_fallback_payload = provider_config.parse_provider_fallback_payload
parse_provider_configs_payload = provider_config.parse_provider_configs_payload
provider_configs_from_env = provider_config.provider_configs_from_env
merge_provider_configs = provider_config.merge_provider_configs
serialize_provider_fallback_config = provider_config.serialize_provider_fallback_config
serialize_provider_configs = provider_config.serialize_provider_configs

RUNTIME_CONFIG_FILE_NAME = ".voidcode.json"
APPROVAL_MODE_ENV_VAR = "VOIDCODE_APPROVAL_MODE"
MODEL_ENV_VAR = "VOIDCODE_MODEL"
EXECUTION_ENGINE_ENV_VAR = "VOIDCODE_EXECUTION_ENGINE"
MAX_STEPS_ENV_VAR = "VOIDCODE_MAX_STEPS"
TOOL_TIMEOUT_ENV_VAR = "VOIDCODE_TOOL_TIMEOUT_SECONDS"
_VALID_APPROVAL_MODES = ("allow", "deny", "ask")
_VALID_TUI_COMMANDS = ("command_palette", "session_new", "session_resume")
type ExecutionEngineName = Literal["deterministic", "provider"]
type RuntimeAgentPresetId = AgentManifestId

_VALID_EXECUTION_ENGINES: tuple[ExecutionEngineName, ...] = ("deterministic", "provider")
_TOP_LEVEL_ENV_VARS = (
    APPROVAL_MODE_ENV_VAR,
    MODEL_ENV_VAR,
    EXECUTION_ENGINE_ENV_VAR,
    MAX_STEPS_ENV_VAR,
    TOOL_TIMEOUT_ENV_VAR,
)
_ENV_SETTINGS_LOCK = Lock()


class _EnvironmentRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    approval_mode: PermissionDecision | None = Field(
        default=None,
        validation_alias=APPROVAL_MODE_ENV_VAR,
    )
    model: str | None = Field(default=None, validation_alias=MODEL_ENV_VAR)
    execution_engine: ExecutionEngineName | None = Field(
        default=None,
        validation_alias=EXECUTION_ENGINE_ENV_VAR,
    )
    max_steps: int | None = Field(default=None, validation_alias=MAX_STEPS_ENV_VAR)
    tool_timeout_seconds: int | None = Field(
        default=None,
        validation_alias=TOOL_TIMEOUT_ENV_VAR,
    )

    @field_validator("approval_mode", mode="before")
    @classmethod
    def _validate_approval_mode(cls, value: object) -> PermissionDecision | None:
        return _parse_approval_mode(
            value,
            source=f"environment variable {APPROVAL_MODE_ENV_VAR}",
            allow_none=True,
        )

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"environment variable {MODEL_ENV_VAR} must be a non-empty string")
        return value

    @field_validator("execution_engine", mode="before")
    @classmethod
    def _validate_execution_engine(cls, value: object) -> ExecutionEngineName | None:
        return _parse_execution_engine(
            value,
            source=f"environment variable {EXECUTION_ENGINE_ENV_VAR}",
            allow_none=True,
        )

    @field_validator("max_steps", mode="before")
    @classmethod
    def _validate_max_steps(cls, value: object) -> int | None:
        return _parse_environment_max_steps(value)

    @field_validator("tool_timeout_seconds", mode="before")
    @classmethod
    def _validate_tool_timeout_seconds(cls, value: object) -> int | None:
        return _parse_environment_tool_timeout_seconds(value)


@dataclass(frozen=True, slots=True)
class RuntimeToolsBuiltinConfig:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeToolsConfig:
    builtin: RuntimeToolsBuiltinConfig | None = None
    paths: tuple[str, ...] = ()
    allowlist: tuple[str, ...] | None = None
    default: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSkillsConfig:
    enabled: bool | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeLspConfig:
    enabled: bool | None = None
    servers: Mapping[str, RuntimeLspServerConfig] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAcpConfig:
    enabled: bool | None = None
    transport: Literal["memory"] = "memory"
    handshake_request_type: str = "handshake"
    handshake_payload: dict[str, object] = field(default_factory=dict)


type McpTransport = Literal["stdio"]


@dataclass(frozen=True, slots=True)
class RuntimeMcpServerConfig:
    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeMcpConfig:
    enabled: bool | None = None
    servers: Mapping[str, RuntimeMcpServerConfig] | None = None
    request_timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTuiConfig:
    leader_key: str | None = None
    keymap: Mapping[str, str] | None = None
    preferences: RuntimeTuiPreferences | None = None


type RuntimeTuiThemeMode = Literal["auto", "light", "dark"]


@dataclass(frozen=True, slots=True)
class RuntimeTuiThemePreferences:
    name: str | None = None
    mode: RuntimeTuiThemeMode | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTuiReadingPreferences:
    wrap: bool | None = None
    sidebar_collapsed: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTuiPreferences:
    theme: RuntimeTuiThemePreferences | None = None
    reading: RuntimeTuiReadingPreferences | None = None


@dataclass(frozen=True, slots=True)
class EffectiveRuntimeTuiPreferences:
    theme: RuntimeTuiThemePreferences
    reading: RuntimeTuiReadingPreferences


@dataclass(frozen=True, slots=True)
class RuntimePlanConfig:
    provider: str | None = None
    module: str | None = None
    factory: str | None = None
    options: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAgentConfig:
    preset: RuntimeAgentPresetId
    prompt_profile: str | None = None
    model: str | None = None
    execution_engine: ExecutionEngineName | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    approval_mode: PermissionDecision = "ask"
    model: str | None = None
    execution_engine: ExecutionEngineName = "deterministic"
    max_steps: int = 4
    tool_timeout_seconds: int | None = None
    hooks: RuntimeHooksConfig | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    mcp: RuntimeMcpConfig | None = None
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    providers: RuntimeProvidersConfig | None = None
    plan: RuntimePlanConfig | None = None
    agent: RuntimeAgentConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfigOverrides:
    approval_mode: PermissionDecision | None = None
    model: str | None = None
    execution_engine: ExecutionEngineName | None = None
    max_steps: int | None = None
    tool_timeout_seconds: int | None = None
    tool_timeout_seconds_configured: bool = False
    hooks: RuntimeHooksConfig | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    mcp: RuntimeMcpConfig | None = None
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    providers: RuntimeProvidersConfig | None = None
    plan: RuntimePlanConfig | None = None
    agent: RuntimeAgentConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeWebSettings:
    provider: str | None = None
    provider_api_key: str | None = None
    provider_api_key_present: bool = False


_VALID_TUI_THEME_MODES: tuple[RuntimeTuiThemeMode, ...] = ("auto", "light", "dark")
_BUILTIN_TUI_THEME_DEFAULTS: dict[RuntimeTuiThemeMode, str] = {
    "auto": "textual-dark",
    "light": "textual-light",
    "dark": "textual-dark",
}
_BUILTIN_TEXTUAL_LIGHT_THEMES: frozenset[str] = frozenset(
    {"textual-light", "solarized-light", "atom-one-light"}
)
_BUILTIN_TEXTUAL_DARK_THEMES: frozenset[str] = frozenset(
    {
        "textual-dark",
        "nord",
        "gruvbox",
        "textual-ansi",
        "dracula",
        "tokyo-night",
        "monokai",
        "atom-one-dark",
    }
)


def runtime_config_path(workspace: Path) -> Path:
    return workspace / RUNTIME_CONFIG_FILE_NAME


def user_runtime_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "voidcode" / "config.json"
    return Path.home() / ".config" / "voidcode" / "config.json"


def load_global_tui_preferences(
    env: Mapping[str, str] | None = None,
) -> RuntimeTuiPreferences | None:
    environment: Mapping[str, str] = os.environ if env is None else env
    global_config = _load_user_config(environment)
    if global_config.tui is None:
        return None
    return global_config.tui.preferences


def load_global_web_settings(env: Mapping[str, str] | None = None) -> RuntimeWebSettings:
    environment: Mapping[str, str] = os.environ if env is None else env
    global_config = _load_user_config(environment)
    payload = _read_json_object(_user_runtime_config_path_from_env(environment))
    raw_web = payload.get("web")
    configured_provider: str | None = None
    if isinstance(raw_web, dict):
        raw_provider = cast(dict[str, object], raw_web).get("provider")
        if isinstance(raw_provider, str):
            configured_provider = raw_provider
    provider = configured_provider or _first_configured_provider_name(global_config.providers)
    return RuntimeWebSettings(
        provider=provider,
        provider_api_key_present=_provider_api_key_present(global_config.providers, provider),
    )


def load_workspace_tui_preferences(
    workspace: Path, env: Mapping[str, str] | None = None
) -> RuntimeTuiPreferences | None:
    environment: Mapping[str, str] = os.environ if env is None else env
    repo_local = _load_repo_local_config(workspace.resolve(), env=environment)
    if repo_local.tui is None:
        return None
    return repo_local.tui.preferences


def load_runtime_config(
    workspace: Path,
    *,
    approval_mode: PermissionDecision | None = None,
    model: str | None = None,
    execution_engine: ExecutionEngineName | None = None,
    max_steps: int | None = None,
    tool_timeout_seconds: int | None = None,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    resolved_workspace = workspace.resolve()
    environment: Mapping[str, str] = os.environ if env is None else env
    env_overrides = _load_environment_runtime_config(environment)
    global_config = _load_user_config(environment)
    repo_local = _load_repo_local_config(resolved_workspace, env=environment)
    resolved_tui = _resolve_tui_config(global_config.tui, repo_local.tui)
    resolved_lsp = repo_local.lsp or _derive_workspace_lsp_config(resolved_workspace)
    resolved_agent = _resolve_agent_config(repo_local.agent)

    resolved_providers = merge_provider_configs(
        repo_local.providers,
        merge_provider_configs(global_config.providers, provider_configs_from_env(environment)),
    )

    return RuntimeConfig(
        approval_mode=_resolve_approval_mode(
            explicit=approval_mode,
            repo_local=repo_local.approval_mode,
            environment=env_overrides.approval_mode,
        ),
        model=_resolve_model(
            explicit=model,
            repo_local=repo_local.model,
            environment=env_overrides.model,
        ),
        execution_engine=_resolve_execution_engine(
            explicit=execution_engine,
            repo_local=repo_local.execution_engine,
            environment=env_overrides.execution_engine,
        ),
        max_steps=_resolve_max_steps(
            explicit=max_steps,
            repo_local=repo_local.max_steps,
            environment=env_overrides.max_steps,
        ),
        tool_timeout_seconds=_resolve_tool_timeout_seconds(
            explicit=tool_timeout_seconds,
            repo_local=repo_local.tool_timeout_seconds,
            repo_local_configured=repo_local.tool_timeout_seconds_configured,
            environment=env_overrides.tool_timeout_seconds,
        ),
        hooks=repo_local.hooks,
        tools=repo_local.tools,
        skills=repo_local.skills,
        lsp=resolved_lsp,
        mcp=repo_local.mcp,
        tui=resolved_tui,
        provider_fallback=repo_local.provider_fallback,
        providers=resolved_providers,
        plan=repo_local.plan,
        agent=resolved_agent,
    )


def _derive_workspace_lsp_config(workspace: Path) -> RuntimeLspConfig | None:
    derived_servers = derive_workspace_lsp_defaults(workspace)
    if not derived_servers:
        return None
    return RuntimeLspConfig(enabled=True, servers=derived_servers)


def _load_repo_local_config(
    workspace: Path,
    *,
    env: Mapping[str, str],
) -> RuntimeConfigOverrides:
    config_path = runtime_config_path(workspace)
    if not config_path.exists():
        return RuntimeConfigOverrides()

    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime config file must contain valid JSON: {config_path}") from exc

    if not isinstance(raw_payload, dict):
        raise ValueError(f"runtime config file must contain a JSON object: {config_path}")

    payload = cast(dict[str, object], raw_payload)

    raw_model = payload.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        raise ValueError("runtime config field 'model' must be a string when provided")

    raw_execution_engine = payload.get("execution_engine")
    parsed_execution_engine = _parse_execution_engine(
        raw_execution_engine,
        source=f"runtime config field 'execution_engine' in {config_path}",
        allow_none=True,
    )

    parsed_max_steps = _parse_max_steps(
        payload.get("max_steps"),
        source=f"runtime config field 'max_steps' in {config_path}",
        allow_none=True,
    )

    tool_timeout_seconds_configured = "tool_timeout_seconds" in payload
    parsed_tool_timeout_seconds = _parse_tool_timeout_seconds(
        payload.get("tool_timeout_seconds"),
        source=f"runtime config field 'tool_timeout_seconds' in {config_path}",
        allow_none=True,
    )

    raw_hooks = payload.get("hooks")
    hooks = _parse_hooks_config(raw_hooks)

    raw_tools = payload.get("tools")
    tools = _parse_tools_config(raw_tools)

    raw_skills = payload.get("skills")
    skills = _parse_skills_config(raw_skills)

    raw_lsp = payload.get("lsp")
    lsp = _parse_lsp_config(raw_lsp)

    raw_mcp = payload.get("mcp")
    mcp = _parse_mcp_config(raw_mcp)

    raw_tui = payload.get("tui")
    tui = _parse_tui_config(raw_tui)

    raw_provider_fallback = payload.get("provider_fallback")
    provider_fallback = _parse_provider_fallback_config(raw_provider_fallback)

    raw_providers = payload.get("providers")
    providers = _parse_providers_config(raw_providers, env=env)

    raw_plan = payload.get("plan")
    plan = _parse_plan_config(raw_plan)
    raw_agent = payload.get("agent")
    agent = _parse_agent_config(raw_agent)

    raw_approval_mode = payload.get("approval_mode")
    parsed_approval_mode = _parse_approval_mode(
        raw_approval_mode,
        source=f"runtime config field 'approval_mode' in {config_path}",
        allow_none=True,
    )

    return RuntimeConfigOverrides(
        approval_mode=parsed_approval_mode,
        model=raw_model,
        execution_engine=parsed_execution_engine,
        max_steps=parsed_max_steps,
        tool_timeout_seconds=parsed_tool_timeout_seconds,
        tool_timeout_seconds_configured=tool_timeout_seconds_configured,
        hooks=hooks,
        tools=tools,
        skills=skills,
        lsp=lsp,
        mcp=mcp,
        tui=tui,
        provider_fallback=provider_fallback,
        providers=providers,
        plan=plan,
        agent=agent,
    )


def _load_user_config(env: Mapping[str, str]) -> RuntimeConfigOverrides:
    config_path = _user_runtime_config_path_from_env(env)
    if not config_path.exists():
        return RuntimeConfigOverrides()

    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime config file must contain valid JSON: {config_path}") from exc

    if not isinstance(raw_payload, dict):
        raise ValueError(f"runtime config file must contain a JSON object: {config_path}")

    payload = cast(dict[str, object], raw_payload)
    raw_tui = payload.get("tui")
    tui = _parse_tui_config(raw_tui)
    raw_providers = payload.get("providers")
    providers = _parse_providers_config(raw_providers, env=env)
    return RuntimeConfigOverrides(tui=tui, providers=providers)


def _user_runtime_config_path_from_env(env: Mapping[str, str]) -> Path:
    config_home = env.get("XDG_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "voidcode" / "config.json"
    return Path.home() / ".config" / "voidcode" / "config.json"


def _parse_hooks_config(raw_hooks: object) -> RuntimeHooksConfig | None:
    if raw_hooks is None:
        return None
    if not isinstance(raw_hooks, dict):
        raise ValueError("runtime config field 'hooks' must be an object when provided")

    hooks_payload = cast(dict[str, object], raw_hooks)
    enabled = hooks_payload.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("runtime config field 'hooks.enabled' must be a boolean when provided")

    pre_tool = _parse_command_list(hooks_payload.get("pre_tool"), field_path="hooks.pre_tool")
    post_tool = _parse_command_list(hooks_payload.get("post_tool"), field_path="hooks.post_tool")
    on_session_start = _parse_command_list(
        hooks_payload.get("on_session_start"),
        field_path="hooks.on_session_start",
    )
    on_session_end = _parse_command_list(
        hooks_payload.get("on_session_end"),
        field_path="hooks.on_session_end",
    )
    on_session_idle = _parse_command_list(
        hooks_payload.get("on_session_idle"),
        field_path="hooks.on_session_idle",
    )
    on_background_task_completed = _parse_command_list(
        hooks_payload.get("on_background_task_completed"),
        field_path="hooks.on_background_task_completed",
    )
    on_background_task_failed = _parse_command_list(
        hooks_payload.get("on_background_task_failed"),
        field_path="hooks.on_background_task_failed",
    )
    on_background_task_cancelled = _parse_command_list(
        hooks_payload.get("on_background_task_cancelled"),
        field_path="hooks.on_background_task_cancelled",
    )
    on_delegated_result_available = _parse_command_list(
        hooks_payload.get("on_delegated_result_available"),
        field_path="hooks.on_delegated_result_available",
    )
    formatter_presets: dict[str, RuntimeFormatterPresetConfig] = _parse_formatter_presets_config(
        hooks_payload.get("formatter_presets"),
        field_path="hooks.formatter_presets",
    )

    return RuntimeHooksConfig(
        enabled=enabled,
        pre_tool=pre_tool,
        post_tool=post_tool,
        on_session_start=on_session_start,
        on_session_end=on_session_end,
        on_session_idle=on_session_idle,
        on_background_task_completed=on_background_task_completed,
        on_background_task_failed=on_background_task_failed,
        on_background_task_cancelled=on_background_task_cancelled,
        on_delegated_result_available=on_delegated_result_available,
        formatter_presets=formatter_presets,
    )


def _parse_formatter_presets_config(
    raw_value: object, *, field_path: str
) -> dict[str, RuntimeFormatterPresetConfig]:
    parsed_presets = dict(RuntimeHooksConfig().formatter_presets)
    if raw_value is None:
        return parsed_presets
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    raw_presets = cast(dict[str, object], raw_value)
    for preset_name, raw_preset in raw_presets.items():
        builtin_preset = parsed_presets.get(preset_name)
        parsed_presets[preset_name] = _parse_formatter_preset_config(
            raw_preset,
            field_path=f"{field_path}.{preset_name}",
            base_preset=builtin_preset,
        )
    return parsed_presets


def _parse_formatter_preset_config(
    raw_value: object,
    *,
    field_path: str,
    base_preset: RuntimeFormatterPresetConfig | None = None,
) -> RuntimeFormatterPresetConfig:
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object")

    preset_payload = cast(dict[str, object], raw_value)
    raw_command = preset_payload.get("command")
    command = (
        _parse_string_list(raw_command, field_path=f"{field_path}.command")
        if "command" in preset_payload
        else (base_preset.command if base_preset is not None else ())
    )
    if not command:
        raise ValueError(
            f"runtime config field '{field_path}.command' must contain at least one string"
        )
    extensions = (
        _parse_string_list(preset_payload.get("extensions"), field_path=f"{field_path}.extensions")
        if "extensions" in preset_payload
        else (base_preset.extensions if base_preset is not None else ())
    )
    root_markers = (
        _parse_string_list(
            preset_payload.get("root_markers"),
            field_path=f"{field_path}.root_markers",
        )
        if "root_markers" in preset_payload
        else (base_preset.root_markers if base_preset is not None else ())
    )
    fallback_commands = (
        _parse_command_list(
            preset_payload.get("fallback_commands"),
            field_path=f"{field_path}.fallback_commands",
        )
        if "fallback_commands" in preset_payload
        else (base_preset.fallback_commands if base_preset is not None else ())
    )
    cwd_policy = _parse_formatter_cwd_policy(
        preset_payload.get("cwd_policy") if "cwd_policy" in preset_payload else None,
        field_path=f"{field_path}.cwd_policy",
        default=base_preset.cwd_policy if base_preset is not None else "nearest_root",
    )
    if base_preset is None and not extensions:
        raise ValueError(
            "runtime config field "
            f"'{field_path}.extensions' must contain at least one string "
            "for custom formatter presets"
        )
    return RuntimeFormatterPresetConfig(
        command=command,
        extensions=extensions,
        root_markers=root_markers,
        fallback_commands=fallback_commands,
        cwd_policy=cwd_policy,
    )


def _parse_formatter_cwd_policy(
    raw_value: object, *, field_path: str, default: FormatterCwdPolicy
) -> FormatterCwdPolicy:
    if raw_value is None:
        return default
    if raw_value not in ("workspace", "nearest_root", "file_directory"):
        raise ValueError(
            f"runtime config field '{field_path}' must be one of: "
            "workspace, nearest_root, file_directory"
        )
    return raw_value


class _RuntimeToolsBuiltinValidationModel(BaseModel):
    enabled: bool | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="tools.builtin.enabled")

    def to_runtime_config(self) -> RuntimeToolsBuiltinConfig:
        return RuntimeToolsBuiltinConfig(enabled=self.enabled)


class _RuntimeToolsValidationModel(BaseModel):
    builtin: _RuntimeToolsBuiltinValidationModel | None = None
    paths: tuple[str, ...] = ()
    allowlist: tuple[str, ...] | None = None
    default: tuple[str, ...] | None = None

    @field_validator("builtin", mode="before")
    @classmethod
    def _validate_builtin_shape(
        cls, value: object
    ) -> dict[str, object] | _RuntimeToolsBuiltinValidationModel | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'tools.builtin' must be an object when provided")
        return cast(dict[str, object], value)

    @field_validator("paths", mode="before")
    @classmethod
    def _validate_paths(cls, value: object) -> tuple[str, ...]:
        return _parse_string_list(value, field_path="tools.paths")

    @field_validator("allowlist", mode="before")
    @classmethod
    def _validate_allowlist(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _parse_string_list(value, field_path="tools.allowlist")

    @field_validator("default", mode="before")
    @classmethod
    def _validate_default(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _parse_string_list(value, field_path="tools.default")

    def to_runtime_config(self) -> RuntimeToolsConfig:
        return RuntimeToolsConfig(
            builtin=self.builtin.to_runtime_config() if self.builtin is not None else None,
            paths=self.paths,
            allowlist=self.allowlist,
            default=self.default,
        )


class _RuntimeSkillsValidationModel(BaseModel):
    enabled: bool | None = None
    paths: tuple[str, ...] = ()

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="skills.enabled")

    @field_validator("paths", mode="before")
    @classmethod
    def _validate_paths(cls, value: object) -> tuple[str, ...]:
        return _parse_string_list(value, field_path="skills.paths")

    def to_runtime_config(self) -> RuntimeSkillsConfig:
        return RuntimeSkillsConfig(enabled=self.enabled, paths=self.paths)


def _validation_context_field_path(info: ValidationInfo, *, default: str) -> str:
    context = info.context
    if isinstance(context, dict):
        typed_context = cast(dict[str, object], context)
        field_path = typed_context.get("field_path")
        if isinstance(field_path, str):
            return field_path
    return default


class _RuntimeMcpServerValidationModel(BaseModel):
    model_config = ConfigDict(validate_default=True)

    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("transport", mode="before")
    @classmethod
    def _validate_transport(cls, value: object, info: ValidationInfo) -> McpTransport:
        if value is None:
            return "stdio"
        field_path = _validation_context_field_path(info, default="mcp.servers")
        if value != "stdio":
            raise ValueError(f"runtime config field '{field_path}.transport' must be one of: stdio")
        return "stdio"

    @field_validator("command", mode="before")
    @classmethod
    def _validate_command(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        field_path = _validation_context_field_path(info, default="mcp.servers")
        command = _parse_string_list(value, field_path=f"{field_path}.command")
        if not command:
            raise ValueError(
                f"runtime config field '{field_path}.command' must contain at least one string"
            )
        return command

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env(cls, value: object, info: ValidationInfo) -> dict[str, str]:
        field_path = _validation_context_field_path(info, default="mcp.servers")
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field '{field_path}.env' must be an object")

        parsed_env: dict[str, str] = {}
        raw_env = cast(dict[object, object], value)
        for key, item in raw_env.items():
            if not isinstance(key, str):
                raise ValueError(f"runtime config field '{field_path}.env' keys must be strings")
            if not isinstance(item, str):
                raise ValueError(f"runtime config field '{field_path}.env.{key}' must be a string")
            parsed_env[key] = item
        return parsed_env

    def to_runtime_config(self) -> RuntimeMcpServerConfig:
        return RuntimeMcpServerConfig(
            transport=self.transport,
            command=self.command,
            env=self.env,
        )


class _RuntimeMcpValidationModel(BaseModel):
    enabled: bool | None = None
    servers: dict[str, _RuntimeMcpServerValidationModel] | None = None
    request_timeout_seconds: float | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="mcp.enabled")

    @field_validator("servers", mode="before")
    @classmethod
    def _validate_servers(cls, value: object) -> dict[str, _RuntimeMcpServerValidationModel] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'mcp.servers' must be an object when provided")

        parsed_servers: dict[str, _RuntimeMcpServerValidationModel] = {}
        raw_servers = cast(dict[object, object], value)
        for server_name, raw_server in raw_servers.items():
            if not isinstance(server_name, str):
                raise ValueError("runtime config field 'mcp.servers' keys must be strings")
            if not isinstance(raw_server, dict):
                raise ValueError(
                    f"runtime config field 'mcp.servers.{server_name}' must be an object"
                )
            parsed_servers[server_name] = _validate_runtime_config_model(
                _RuntimeMcpServerValidationModel,
                cast(dict[str, object], raw_server),
                context={"field_path": f"mcp.servers.{server_name}"},
            )
        return parsed_servers

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def _validate_request_timeout_seconds(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("runtime config field 'mcp.request_timeout_seconds' must be a number")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(
                "runtime config field 'mcp.request_timeout_seconds' must be a finite number"
            )
        if parsed <= 0:
            raise ValueError(
                "runtime config field 'mcp.request_timeout_seconds' must be greater than 0"
            )
        return parsed

    def to_runtime_config(self) -> RuntimeMcpConfig:
        return RuntimeMcpConfig(
            enabled=self.enabled,
            servers={
                server_name: server.to_runtime_config()
                for server_name, server in self.servers.items()
            }
            if self.servers is not None
            else None,
            request_timeout_seconds=self.request_timeout_seconds,
        )


class _RuntimeTuiValidationModel(BaseModel):
    leader_key: str | None = None
    keymap: dict[str, str] | None = None
    preferences: _RuntimeTuiPreferencesValidationModel | None = None

    @field_validator("preferences", mode="before")
    @classmethod
    def _validate_preferences(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(
                "runtime config field 'tui.preferences' must be an object when provided"
            )
        return cast(dict[str, object], value)

    @field_validator("leader_key", mode="before")
    @classmethod
    def _validate_leader_key(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("runtime config field 'tui.leader_key' must be a string when provided")
        return value

    @field_validator("keymap", mode="before")
    @classmethod
    def _validate_keymap(cls, value: object) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'tui.keymap' must be an object when provided")

        parsed_keymap: dict[str, str] = {}
        raw_keymap = cast(dict[object, object], value)
        for key, item in raw_keymap.items():
            if not isinstance(key, str):
                raise ValueError("runtime config field 'tui.keymap' keys must be strings")
            if not isinstance(item, str):
                raise ValueError("runtime config field 'tui.keymap' values must be strings")
            if item not in _VALID_TUI_COMMANDS:
                allowed = ", ".join(_VALID_TUI_COMMANDS)
                raise ValueError(
                    f"runtime config field 'tui.keymap' values must be one of: {allowed}"
                )
            parsed_keymap[key] = item

        return parsed_keymap

    def to_runtime_config(self) -> RuntimeTuiConfig:
        return RuntimeTuiConfig(
            leader_key=self.leader_key,
            keymap=self.keymap,
            preferences=(
                self.preferences.to_runtime_config() if self.preferences is not None else None
            ),
        )


class _RuntimeTuiThemePreferencesValidationModel(BaseModel):
    name: str | None = None
    mode: RuntimeTuiThemeMode | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                "runtime config field 'tui.preferences.theme.name' must be a string when provided"
            )
        return value

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object) -> RuntimeTuiThemeMode | None:
        if value is None:
            return None
        if value not in _VALID_TUI_THEME_MODES:
            allowed = ", ".join(_VALID_TUI_THEME_MODES)
            raise ValueError(
                f"runtime config field 'tui.preferences.theme.mode' must be one of: {allowed}"
            )
        return value

    def to_runtime_config(self) -> RuntimeTuiThemePreferences:
        return RuntimeTuiThemePreferences(name=self.name, mode=self.mode)


class _RuntimeTuiReadingPreferencesValidationModel(BaseModel):
    wrap: bool | None = None
    sidebar_collapsed: bool | None = None

    @field_validator("wrap", mode="before")
    @classmethod
    def _validate_wrap(cls, value: object) -> bool | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(
                "runtime config field 'tui.preferences.reading.wrap' must be a boolean when provided"  # noqa: E501
            )
        return value

    @field_validator("sidebar_collapsed", mode="before")
    @classmethod
    def _validate_sidebar_collapsed(cls, value: object) -> bool | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(
                "runtime config field 'tui.preferences.reading.sidebar_collapsed' must be a boolean when provided"  # noqa: E501
            )
        return value

    def to_runtime_config(self) -> RuntimeTuiReadingPreferences:
        return RuntimeTuiReadingPreferences(
            wrap=self.wrap,
            sidebar_collapsed=self.sidebar_collapsed,
        )


class _RuntimeTuiPreferencesValidationModel(BaseModel):
    theme: _RuntimeTuiThemePreferencesValidationModel | None = None
    reading: _RuntimeTuiReadingPreferencesValidationModel | None = None

    @field_validator("theme", mode="before")
    @classmethod
    def _validate_theme(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(
                "runtime config field 'tui.preferences.theme' must be an object when provided"
            )
        return cast(dict[str, object], value)

    @field_validator("reading", mode="before")
    @classmethod
    def _validate_reading(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(
                "runtime config field 'tui.preferences.reading' must be an object when provided"
            )
        return cast(dict[str, object], value)

    def to_runtime_config(self) -> RuntimeTuiPreferences:
        return RuntimeTuiPreferences(
            theme=self.theme.to_runtime_config() if self.theme is not None else None,
            reading=self.reading.to_runtime_config() if self.reading is not None else None,
        )


class _RuntimeConfigOutput(Protocol):
    def to_runtime_config(self) -> object: ...


def _validate_runtime_config_model[T: BaseModel](
    model_type: type[T], raw_value: dict[str, object], *, context: dict[str, object] | None = None
) -> T:
    try:
        return model_type.model_validate(raw_value, context=context)
    except ValidationError as exc:
        raise ValueError(_format_settings_validation_error(exc)) from exc


def _parse_tools_config(raw_tools: object) -> RuntimeToolsConfig | None:
    if raw_tools is None:
        return None
    if not isinstance(raw_tools, dict):
        raise ValueError("runtime config field 'tools' must be an object when provided")

    tools_payload = cast(dict[str, object], raw_tools)
    return cast(
        RuntimeToolsConfig | None,
        _parse_runtime_config_section(
            tools_payload,
            field_path="tools",
            model_type=_RuntimeToolsValidationModel,
        ),
    )


def _parse_skills_config(raw_skills: object) -> RuntimeSkillsConfig | None:
    if raw_skills is None:
        return None
    if not isinstance(raw_skills, dict):
        raise ValueError("runtime config field 'skills' must be an object when provided")

    skills_payload = cast(dict[str, object], raw_skills)
    return cast(
        RuntimeSkillsConfig | None,
        _parse_runtime_config_section(
            skills_payload,
            field_path="skills",
            model_type=_RuntimeSkillsValidationModel,
        ),
    )


def _parse_lsp_config(raw_lsp: object) -> RuntimeLspConfig | None:
    if raw_lsp is None:
        return None
    if not isinstance(raw_lsp, dict):
        raise ValueError("runtime config field 'lsp' must be an object when provided")

    lsp_payload = cast(dict[str, object], raw_lsp)
    enabled = _parse_optional_bool(lsp_payload.get("enabled"), field_path="lsp.enabled")
    servers = _parse_lsp_servers_config(lsp_payload.get("servers"), field_path="lsp.servers")
    return RuntimeLspConfig(enabled=enabled, servers=servers)


def _parse_lsp_servers_config(
    raw_value: object, *, field_path: str
) -> dict[str, RuntimeLspServerConfig] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    raw_servers = cast(dict[str, object], raw_value)
    parsed_servers: dict[str, RuntimeLspServerConfig] = {}
    for server_name, raw_server in raw_servers.items():
        parsed_servers[server_name] = _parse_lsp_server_config(
            raw_server,
            server_name=server_name,
            field_path=f"{field_path}.{server_name}",
        )
    return parsed_servers


def _parse_lsp_server_config(
    raw_value: object,
    *,
    server_name: str,
    field_path: str,
) -> RuntimeLspServerConfig:
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object")

    server_payload = cast(dict[str, object], raw_value)
    preset = server_payload.get("preset")
    uses_builtin_server_name = has_builtin_lsp_server_preset(server_name)
    if preset is not None:
        if not isinstance(preset, str) or not preset:
            raise ValueError(f"runtime config field '{field_path}.preset' must be a string")
        if not has_builtin_lsp_server_preset(preset):
            raise ValueError(
                f"runtime config field '{field_path}.preset' references unknown preset"
            )
    command = _parse_string_list(server_payload.get("command"), field_path=f"{field_path}.command")
    if not command and preset is None and not uses_builtin_server_name:
        raise ValueError(
            f"runtime config field '{field_path}.command' must contain at least one string"
        )
    languages = _parse_string_list(
        server_payload.get("languages"),
        field_path=f"{field_path}.languages",
    )
    extensions = _parse_string_list(
        server_payload.get("extensions"),
        field_path=f"{field_path}.extensions",
    )
    root_markers = _parse_string_list(
        server_payload.get("root_markers"),
        field_path=f"{field_path}.root_markers",
    )
    settings = _parse_object_container(
        server_payload.get("settings"),
        field_path=f"{field_path}.settings",
    )
    init_options = _parse_object_container(
        server_payload.get("init_options"),
        field_path=f"{field_path}.init_options",
    )
    return RuntimeLspServerConfig(
        preset=preset,
        command=command,
        languages=languages,
        extensions=extensions,
        root_markers=root_markers,
        settings=settings or {},
        init_options=init_options or {},
    )


def _parse_mcp_config(raw_mcp: object) -> RuntimeMcpConfig | None:
    if raw_mcp is None:
        return None
    if not isinstance(raw_mcp, dict):
        raise ValueError("runtime config field 'mcp' must be an object when provided")

    mcp_payload = cast(dict[str, object], raw_mcp)
    return cast(
        RuntimeMcpConfig | None,
        _parse_runtime_config_section(
            mcp_payload,
            field_path="mcp",
            model_type=_RuntimeMcpValidationModel,
        ),
    )


def _parse_tui_config(raw_tui: object) -> RuntimeTuiConfig | None:
    if raw_tui is None:
        return None
    if not isinstance(raw_tui, dict):
        raise ValueError("runtime config field 'tui' must be an object when provided")

    tui_payload = cast(dict[str, object], raw_tui)
    return cast(
        RuntimeTuiConfig | None,
        _parse_runtime_config_section(
            tui_payload,
            field_path="tui",
            model_type=_RuntimeTuiValidationModel,
        ),
    )


def _parse_plan_config(raw_plan: object) -> RuntimePlanConfig | None:
    if raw_plan is None:
        return None
    if not isinstance(raw_plan, dict):
        raise ValueError("runtime config field 'plan' must be an object when provided")

    payload = cast(dict[str, object], raw_plan)
    provider = payload.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise ValueError("runtime config field 'plan.provider' must be a non-empty string")

    module = payload.get("module")
    if module is not None and (not isinstance(module, str) or not module.strip()):
        raise ValueError("runtime config field 'plan.module' must be a non-empty string")

    factory = payload.get("factory")
    if factory is not None and (not isinstance(factory, str) or not factory.strip()):
        raise ValueError("runtime config field 'plan.factory' must be a non-empty string")

    options = payload.get("options")
    parsed_options: Mapping[str, object] | None = None
    if options is not None:
        if not isinstance(options, dict):
            raise ValueError("runtime config field 'plan.options' must be an object when provided")
        parsed_options = cast(dict[str, object], options)

    return RuntimePlanConfig(
        provider=provider,
        module=module,
        factory=factory,
        options=parsed_options,
    )


def _parse_agent_config(raw_agent: object) -> RuntimeAgentConfig | None:
    if raw_agent is None:
        return None
    if not isinstance(raw_agent, dict):
        raise ValueError("runtime config field 'agent' must be an object when provided")

    payload = cast(dict[str, object], raw_agent)
    if "preset" not in payload:
        builtin_manifests = list_builtin_agent_manifests()
        valid_ids = {manifest.id for manifest in builtin_manifests}
        payload_keys = set(payload)
        if payload_keys != valid_ids.intersection(payload_keys) or len(payload_keys) != 1:
            valid_presets = ", ".join(manifest.id for manifest in builtin_manifests)
            raise ValueError(
                "runtime config field 'agent' must declare exactly one built-in agent key: "
                f"{valid_presets}"
            )
        matching_id = next(iter(payload_keys))
        nested_payload = payload.get(matching_id)
        if not isinstance(nested_payload, dict):
            raise ValueError(
                f"runtime config field 'agent.{matching_id}' must be an object when provided"
            )
        payload = {"preset": matching_id, **cast(dict[str, object], nested_payload)}

    raw_preset = payload.get("preset")
    if not isinstance(raw_preset, str) or get_builtin_agent_manifest(raw_preset) is None:
        valid_presets = ", ".join(manifest.id for manifest in list_builtin_agent_manifests())
        raise ValueError(f"runtime config field 'agent.preset' must be one of: {valid_presets}")

    prompt_profile = payload.get("prompt_profile")
    if prompt_profile is not None and (
        not isinstance(prompt_profile, str) or not prompt_profile.strip()
    ):
        raise ValueError("runtime config field 'agent.prompt_profile' must be a non-empty string")

    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("runtime config field 'agent.model' must be a non-empty string")
    if "leader_mode" in payload:
        raise ValueError(
            "runtime config field 'agent.leader_mode' has been removed; "
            "use the default leader execution flow instead"
        )

    execution_engine = _parse_execution_engine(
        payload.get("execution_engine"),
        source="runtime config field 'agent.execution_engine'",
        allow_none=True,
    )

    provider_fallback = _parse_provider_fallback_config(payload.get("provider_fallback"))

    return RuntimeAgentConfig(
        preset=cast(RuntimeAgentPresetId, raw_preset),
        prompt_profile=prompt_profile.strip() if isinstance(prompt_profile, str) else None,
        model=model.strip() if isinstance(model, str) else None,
        execution_engine=execution_engine,
        tools=_parse_tools_config(payload.get("tools")),
        skills=_parse_skills_config(payload.get("skills")),
        provider_fallback=provider_fallback,
    )


def _resolve_agent_config(agent: RuntimeAgentConfig | None) -> RuntimeAgentConfig | None:
    if agent is None:
        return None
    manifest = get_builtin_agent_manifest(agent.preset)
    if manifest is not None:
        return RuntimeAgentConfig(
            preset=agent.preset,
            prompt_profile=agent.prompt_profile or manifest.prompt_profile,
            model=agent.model or manifest.model_preference,
            execution_engine=agent.execution_engine or manifest.execution_engine,
            tools=agent.tools,
            skills=agent.skills,
            provider_fallback=agent.provider_fallback,
        )
    return agent


def parse_runtime_plan_payload(raw_plan: object, *, source: str) -> RuntimePlanConfig | None:
    try:
        return _parse_plan_config(raw_plan)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def parse_runtime_agent_payload(raw_agent: object, *, source: str) -> RuntimeAgentConfig | None:
    try:
        return _resolve_agent_config(_parse_agent_config(raw_agent))
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def serialize_runtime_plan_config(plan: RuntimePlanConfig | None) -> dict[str, object] | None:
    if plan is None:
        return None
    payload: dict[str, object] = {}
    if plan.provider is not None:
        payload["provider"] = plan.provider
    if plan.module is not None:
        payload["module"] = plan.module
    if plan.factory is not None:
        payload["factory"] = plan.factory
    if plan.options is not None:
        payload["options"] = dict(plan.options)
    return payload


def serialize_runtime_agent_config(agent: RuntimeAgentConfig | None) -> dict[str, object] | None:
    if agent is None:
        return None
    payload: dict[str, object] = {"preset": agent.preset}
    if agent.prompt_profile is not None:
        payload["prompt_profile"] = agent.prompt_profile
    if agent.model is not None:
        payload["model"] = agent.model
    if agent.execution_engine is not None:
        payload["execution_engine"] = agent.execution_engine
    if agent.tools is not None:
        tools_payload: dict[str, object | None] = {
            "builtin": None
            if agent.tools.builtin is None
            else {"enabled": agent.tools.builtin.enabled},
            "paths": list(agent.tools.paths) if agent.tools.paths else None,
            "allowlist": (
                list(agent.tools.allowlist) if agent.tools.allowlist is not None else None
            ),
            "default": list(agent.tools.default) if agent.tools.default is not None else None,
        }
        payload["tools"] = {key: value for key, value in tools_payload.items() if value is not None}
    if agent.skills is not None:
        payload["skills"] = {
            "enabled": agent.skills.enabled,
            "paths": list(agent.skills.paths) if agent.skills.paths else None,
        }
    if agent.provider_fallback is not None:
        payload["provider_fallback"] = serialize_provider_fallback_config(agent.provider_fallback)
    return {key: value for key, value in payload.items() if value is not None}


def _parse_runtime_config_section[TModel: BaseModel](
    raw_value: object,
    *,
    field_path: str,
    model_type: type[TModel],
) -> object | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    validated_model = _validate_runtime_config_model(model_type, cast(dict[str, object], raw_value))
    return cast(_RuntimeConfigOutput, validated_model).to_runtime_config()


def _resolve_tui_config(
    global_tui: RuntimeTuiConfig | None, workspace_tui: RuntimeTuiConfig | None
) -> RuntimeTuiConfig:
    leader_key = (
        (workspace_tui.leader_key if workspace_tui is not None else None)
        or (global_tui.leader_key if global_tui is not None else None)
        or "alt+x"
    )
    keymap = (
        workspace_tui.keymap
        if workspace_tui is not None and workspace_tui.keymap is not None
        else (global_tui.keymap if global_tui is not None else None)
    )
    preferences = _merge_tui_preferences(
        global_tui.preferences if global_tui is not None else None,
        workspace_tui.preferences if workspace_tui is not None else None,
    )
    return RuntimeTuiConfig(leader_key=leader_key, keymap=keymap, preferences=preferences)


def _merge_tui_preferences(
    global_preferences: RuntimeTuiPreferences | None,
    workspace_preferences: RuntimeTuiPreferences | None,
) -> RuntimeTuiPreferences:
    global_theme = global_preferences.theme if global_preferences is not None else None
    workspace_theme = workspace_preferences.theme if workspace_preferences is not None else None
    global_reading = global_preferences.reading if global_preferences is not None else None
    workspace_reading = workspace_preferences.reading if workspace_preferences is not None else None
    return RuntimeTuiPreferences(
        theme=RuntimeTuiThemePreferences(
            name=(workspace_theme.name if workspace_theme is not None else None)
            or (global_theme.name if global_theme is not None else None)
            or _BUILTIN_TUI_THEME_DEFAULTS["auto"],
            mode=(workspace_theme.mode if workspace_theme is not None else None)
            or (global_theme.mode if global_theme is not None else None)
            or "auto",
        ),
        reading=RuntimeTuiReadingPreferences(
            wrap=(
                workspace_reading.wrap
                if workspace_reading is not None and workspace_reading.wrap is not None
                else (
                    global_reading.wrap
                    if global_reading is not None and global_reading.wrap is not None
                    else True
                )
            ),
            sidebar_collapsed=(
                workspace_reading.sidebar_collapsed
                if workspace_reading is not None and workspace_reading.sidebar_collapsed is not None
                else (
                    global_reading.sidebar_collapsed
                    if global_reading is not None and global_reading.sidebar_collapsed is not None
                    else False
                )
            ),
        ),
    )


def merge_runtime_tui_preferences(
    base_preferences: RuntimeTuiPreferences | None,
    override_preferences: RuntimeTuiPreferences | None,
) -> RuntimeTuiPreferences:
    return _merge_tui_preferences(base_preferences, override_preferences)


def effective_runtime_tui_preferences(
    preferences: RuntimeTuiPreferences | None,
) -> EffectiveRuntimeTuiPreferences:
    merged_preferences = _merge_tui_preferences(None, preferences)
    assert merged_preferences.theme is not None
    assert merged_preferences.reading is not None
    resolved_theme = _resolve_theme_preferences(merged_preferences.theme)
    return EffectiveRuntimeTuiPreferences(theme=resolved_theme, reading=merged_preferences.reading)


def _resolve_theme_preferences(
    theme_preferences: RuntimeTuiThemePreferences,
) -> RuntimeTuiThemePreferences:
    mode = theme_preferences.mode or "auto"
    name = theme_preferences.name or _BUILTIN_TUI_THEME_DEFAULTS[mode]
    if mode == "light" and name not in _BUILTIN_TEXTUAL_LIGHT_THEMES:
        name = _BUILTIN_TUI_THEME_DEFAULTS[mode]
    elif mode == "dark" and name not in _BUILTIN_TEXTUAL_DARK_THEMES:
        name = _BUILTIN_TUI_THEME_DEFAULTS[mode]
    elif mode == "auto" and name not in (
        _BUILTIN_TEXTUAL_LIGHT_THEMES | _BUILTIN_TEXTUAL_DARK_THEMES
    ):
        name = _BUILTIN_TUI_THEME_DEFAULTS[mode]
    return RuntimeTuiThemePreferences(name=name, mode=mode)


def save_workspace_tui_preferences(workspace: Path, preferences: RuntimeTuiPreferences) -> None:
    _save_tui_preferences(runtime_config_path(workspace.resolve()), preferences)


def save_global_tui_preferences(preferences: RuntimeTuiPreferences) -> None:
    _save_tui_preferences(user_runtime_config_path(), preferences)


def save_global_web_settings(settings: RuntimeWebSettings) -> None:
    provider = settings.provider.strip() if isinstance(settings.provider, str) else ""
    if settings.provider_api_key is not None and not provider:
        raise ValueError("provider is required when saving a provider API key")
    config_path = user_runtime_config_path()
    payload = _read_json_object(config_path)
    if provider:
        _validate_runtime_web_provider(provider)
        raw_web = payload.get("web")
        web_payload = dict(cast(dict[str, object], raw_web)) if isinstance(raw_web, dict) else {}
        web_payload["provider"] = provider
        payload["web"] = web_payload
        if settings.provider_api_key is not None:
            payload["providers"] = _set_provider_api_key_payload(
                raw_providers=payload.get("providers"),
                provider=provider,
                api_key=settings.provider_api_key,
            )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _save_tui_preferences(config_path: Path, preferences: RuntimeTuiPreferences) -> None:
    payload = _read_json_object(config_path)
    tui_payload = cast(
        dict[str, object], payload.get("tui") if isinstance(payload.get("tui"), dict) else {}
    )
    updated_tui_payload = dict(tui_payload)
    updated_tui_payload["preferences"] = serialize_runtime_tui_preferences(preferences)
    payload["tui"] = updated_tui_payload
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _validate_runtime_web_provider(provider: str) -> None:
    if not provider or provider != provider.strip() or "/" in provider:
        raise ValueError("provider must be a non-empty provider id without '/'")


def _first_configured_provider_name(providers: RuntimeProvidersConfig | None) -> str | None:
    if providers is None:
        return None
    ordered_candidates: tuple[tuple[str, object | None], ...] = (
        ("openai", providers.openai),
        ("anthropic", providers.anthropic),
        ("google", providers.google),
        ("copilot", providers.copilot),
        ("litellm", providers.litellm),
        ("glm", providers.glm),
        ("minimax", providers.minimax),
        ("kimi", providers.kimi),
        ("opencode-go", providers.opencode_go),
        ("qwen", providers.qwen),
    )
    for provider_name, configured_provider in ordered_candidates:
        if configured_provider is not None:
            return provider_name
    if providers.custom:
        return next(iter(providers.custom))
    return None


def _provider_api_key_present(
    providers: RuntimeProvidersConfig | None, provider: str | None
) -> bool:
    if providers is None or provider is None:
        return False
    if provider == "openai":
        return bool(providers.openai and providers.openai.api_key)
    if provider == "anthropic":
        return bool(providers.anthropic and providers.anthropic.api_key)
    if provider == "google":
        return bool(providers.google and providers.google.auth and providers.google.auth.api_key)
    if provider == "copilot":
        return bool(providers.copilot and providers.copilot.auth and providers.copilot.auth.token)
    if provider == "litellm":
        return bool(providers.litellm and providers.litellm.api_key)
    if provider == "glm":
        return bool(providers.glm and providers.glm.api_key)
    if provider == "minimax":
        return bool(providers.minimax and providers.minimax.api_key)
    if provider == "kimi":
        return bool(providers.kimi and providers.kimi.api_key)
    if provider == "opencode-go":
        return bool(providers.opencode_go and providers.opencode_go.api_key)
    if provider == "qwen":
        return bool(providers.qwen and providers.qwen.api_key)
    custom_provider = providers.custom.get(provider)
    return bool(custom_provider and custom_provider.api_key)


def _set_provider_api_key_payload(
    *, raw_providers: object, provider: str, api_key: str
) -> dict[str, object]:
    providers_payload = (
        dict(cast(dict[str, object], raw_providers)) if isinstance(raw_providers, dict) else {}
    )
    if provider in {"glm", "minimax", "kimi", "opencode-go", "qwen"}:
        nested = providers_payload.get(provider)
        nested_payload = dict(cast(dict[str, object], nested)) if isinstance(nested, dict) else {}
        nested_payload["api_key"] = api_key
        providers_payload[provider] = nested_payload
        return providers_payload
    if provider in {"openai", "anthropic", "litellm"}:
        nested = providers_payload.get(provider)
        nested_payload = dict(cast(dict[str, object], nested)) if isinstance(nested, dict) else {}
        nested_payload["api_key"] = api_key
        providers_payload[provider] = nested_payload
        return providers_payload
    if provider == "google":
        nested = providers_payload.get(provider)
        nested_payload = dict(cast(dict[str, object], nested)) if isinstance(nested, dict) else {}
        auth = nested_payload.get("auth")
        auth_payload = (
            dict(cast(dict[str, object], auth)) if isinstance(auth, dict) else {"method": "api_key"}
        )
        raw_method = auth_payload.get("method")
        method = raw_method if isinstance(raw_method, str) and raw_method else "api_key"
        auth_payload["method"] = method
        auth_payload["api_key"] = api_key
        nested_payload["auth"] = auth_payload
        providers_payload[provider] = nested_payload
        return providers_payload
    if provider == "copilot":
        nested = providers_payload.get(provider)
        nested_payload = dict(cast(dict[str, object], nested)) if isinstance(nested, dict) else {}
        auth = nested_payload.get("auth")
        auth_payload = (
            dict(cast(dict[str, object], auth)) if isinstance(auth, dict) else {"method": "token"}
        )
        raw_method = auth_payload.get("method")
        method = raw_method if isinstance(raw_method, str) and raw_method else "token"
        auth_payload["method"] = method
        auth_payload["token"] = api_key
        nested_payload["auth"] = auth_payload
        providers_payload[provider] = nested_payload
        return providers_payload
    custom = providers_payload.get("custom")
    custom_payload = dict(cast(dict[str, object], custom)) if isinstance(custom, dict) else {}
    nested = custom_payload.get(provider)
    nested_payload = dict(cast(dict[str, object], nested)) if isinstance(nested, dict) else {}
    nested_payload["api_key"] = api_key
    custom_payload[provider] = nested_payload
    providers_payload["custom"] = custom_payload
    return providers_payload


def _read_json_object(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime config file must contain valid JSON: {config_path}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError(f"runtime config file must contain a JSON object: {config_path}")
    return cast(dict[str, object], raw_payload)


def serialize_runtime_tui_preferences(preferences: RuntimeTuiPreferences) -> dict[str, object]:
    payload: dict[str, object] = {}
    if preferences.theme is not None:
        theme_payload: dict[str, object] = {}
        if preferences.theme.name is not None:
            theme_payload["name"] = preferences.theme.name
        if preferences.theme.mode is not None:
            theme_payload["mode"] = preferences.theme.mode
        if theme_payload:
            payload["theme"] = theme_payload
    if preferences.reading is not None:
        reading_payload: dict[str, object] = {}
        if preferences.reading.wrap is not None:
            reading_payload["wrap"] = preferences.reading.wrap
        if preferences.reading.sidebar_collapsed is not None:
            reading_payload["sidebar_collapsed"] = preferences.reading.sidebar_collapsed
        if reading_payload:
            payload["reading"] = reading_payload
    return payload


def _parse_provider_fallback_config(
    raw_provider_fallback: object,
) -> RuntimeProviderFallbackConfig | None:
    return parse_provider_fallback_payload(
        raw_provider_fallback,
        source="runtime config field 'provider_fallback'",
    )


def _parse_providers_config(
    raw_providers: object,
    *,
    env: Mapping[str, str],
) -> RuntimeProvidersConfig | None:
    return parse_provider_configs_payload(
        raw_providers,
        source="runtime config field 'providers'",
        env=env,
    )


def _parse_optional_bool(raw_value: object, *, field_path: str) -> bool | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be a boolean when provided")
    return raw_value


def _parse_object_container(raw_value: object, *, field_path: str) -> dict[str, object] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")
    return cast(dict[str, object], raw_value)


def _format_runtime_config_field_error(field_path: str) -> str:
    runtime_field_prefix = "runtime config field '"
    if field_path.startswith(runtime_field_prefix):
        if field_path.endswith("'"):
            return field_path
        if "'[" in field_path:
            base, suffix = field_path[len(runtime_field_prefix) :].split("'[", maxsplit=1)
            return f"{runtime_field_prefix}{base}[{suffix}'"
    return f"runtime config field '{field_path}'"


def _parse_string_list(raw_value: object, *, field_path: str) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise ValueError(
            f"{_format_runtime_config_field_error(field_path)} must be an array when provided"
        )

    raw_items = cast(list[object], raw_value)
    parsed_items: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, str):
            raise ValueError(
                f"{_format_runtime_config_field_error(f'{field_path}[{index}]')} must be a string"
            )
        parsed_items.append(item)
    return tuple(parsed_items)


def _parse_command_list(raw_value: object, *, field_path: str) -> tuple[tuple[str, ...], ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise ValueError(f"runtime config field '{field_path}' must be an array when provided")

    raw_commands = cast(list[object], raw_value)
    parsed_commands: list[tuple[str, ...]] = []
    for command_index, raw_command in enumerate(raw_commands):
        if not isinstance(raw_command, list):
            raise ValueError(
                f"runtime config field '{field_path}[{command_index}]' must be an array"
            )
        command_field_path = f"{field_path}[{command_index}]"
        parsed_command = _parse_string_list(
            cast(list[object], raw_command),
            field_path=command_field_path,
        )
        if not parsed_command:
            raise ValueError(
                f"runtime config field '{command_field_path}' must contain at least one string"
            )
        parsed_commands.append(parsed_command)
    return tuple(parsed_commands)


def _load_environment_runtime_config(env: Mapping[str, str] | None) -> RuntimeConfigOverrides:
    try:
        with _temporary_runtime_environment(env):
            settings = _EnvironmentRuntimeSettings()
    except ValidationError as exc:
        raise ValueError(_format_settings_validation_error(exc)) from exc

    return RuntimeConfigOverrides(
        approval_mode=settings.approval_mode,
        model=settings.model,
        execution_engine=settings.execution_engine,
        max_steps=settings.max_steps,
        tool_timeout_seconds=settings.tool_timeout_seconds,
    )


@contextmanager
def _temporary_runtime_environment(env: Mapping[str, str] | None):
    if env is None:
        yield
        return

    with _ENV_SETTINGS_LOCK:
        previous_values = {name: os.environ.get(name) for name in _TOP_LEVEL_ENV_VARS}
        try:
            for name in _TOP_LEVEL_ENV_VARS:
                if name in env:
                    os.environ[name] = env[name]
                else:
                    os.environ.pop(name, None)
            yield
        finally:
            for name, previous_value in previous_values.items():
                if previous_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous_value


def _format_settings_validation_error(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors():
        context = error.get("ctx")
        if isinstance(context, dict):
            original_error = context.get("error")
            if isinstance(original_error, ValueError):
                messages.append(str(original_error))
                continue
        messages.append(error["msg"])
    return "; ".join(messages)


def _resolve_approval_mode(
    *,
    explicit: PermissionDecision | None,
    repo_local: PermissionDecision | None,
    environment: str | None,
) -> PermissionDecision:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    parsed_environment = _parse_approval_mode(
        environment,
        source=f"environment variable {APPROVAL_MODE_ENV_VAR}",
        allow_none=True,
    )
    if parsed_environment is not None:
        return parsed_environment
    return "ask"


def _resolve_model(
    *, explicit: str | None, repo_local: str | None, environment: str | None
) -> str | None:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    if environment is not None:
        if not environment:
            raise ValueError(f"environment variable {MODEL_ENV_VAR} must be a non-empty string")
        return environment
    return None


def _resolve_execution_engine(
    *,
    explicit: ExecutionEngineName | None,
    repo_local: ExecutionEngineName | None,
    environment: ExecutionEngineName | None,
) -> ExecutionEngineName:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    if environment is not None:
        return environment
    return "deterministic"


def _resolve_max_steps(
    *, explicit: int | None, repo_local: int | None, environment: int | None
) -> int:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    if environment is not None:
        return environment
    return RuntimeConfig().max_steps


def _parse_approval_mode(
    raw_value: object,
    *,
    source: str,
    allow_none: bool,
) -> PermissionDecision | None:
    if raw_value is None and allow_none:
        return None
    if raw_value not in _VALID_APPROVAL_MODES:
        allowed = ", ".join(_VALID_APPROVAL_MODES)
        raise ValueError(f"{source} must be one of: {allowed}")
    return raw_value


def _parse_execution_engine(
    raw_value: object,
    *,
    source: str,
    allow_none: bool,
) -> ExecutionEngineName | None:
    if raw_value is None and allow_none:
        return None
    if raw_value not in _VALID_EXECUTION_ENGINES:
        allowed = ", ".join(_VALID_EXECUTION_ENGINES)
        raise ValueError(f"{source} must be one of: {allowed}")
    return raw_value


def _parse_max_steps(raw_value: object, *, source: str, allow_none: bool) -> int | None:
    if raw_value is None and allow_none:
        return None
    if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value < 1:
        raise ValueError(f"{source} must be an integer greater than or equal to 1")
    return raw_value


def _parse_environment_max_steps(raw_value: object) -> int | None:
    if raw_value is None:
        return None
    parsed_value = raw_value
    if isinstance(raw_value, str):
        try:
            parsed_value = int(raw_value)
        except ValueError as exc:
            raise ValueError(
                "environment variable "
                f"{MAX_STEPS_ENV_VAR} must be an integer greater than or equal to 1"
            ) from exc
    return _parse_max_steps(
        parsed_value,
        source=f"environment variable {MAX_STEPS_ENV_VAR}",
        allow_none=True,
    )


def _parse_tool_timeout_seconds(raw_value: object, *, source: str, allow_none: bool) -> int | None:
    if raw_value is None and allow_none:
        return None
    if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value < 1:
        raise ValueError(f"{source} must be an integer greater than or equal to 1")
    return raw_value


def _parse_environment_tool_timeout_seconds(raw_value: object) -> int | None:
    if raw_value is None:
        return None
    parsed_value = raw_value
    if isinstance(raw_value, str):
        try:
            parsed_value = int(raw_value)
        except ValueError as exc:
            raise ValueError(
                f"environment variable {TOOL_TIMEOUT_ENV_VAR} must be an integer "
                "greater than or equal to 1"
            ) from exc
    return _parse_tool_timeout_seconds(
        parsed_value,
        source=f"environment variable {TOOL_TIMEOUT_ENV_VAR}",
        allow_none=True,
    )


def _resolve_tool_timeout_seconds(
    *,
    explicit: int | None,
    repo_local: int | None,
    repo_local_configured: bool,
    environment: int | None,
) -> int | None:
    if explicit is not None:
        return _parse_tool_timeout_seconds(
            explicit,
            source="explicit runtime config override 'tool_timeout_seconds'",
            allow_none=True,
        )
    if repo_local_configured:
        return repo_local
    if environment is not None:
        return environment
    return None
