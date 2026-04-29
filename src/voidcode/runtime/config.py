from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..agent import AgentManifestId, get_builtin_agent_manifest, list_builtin_agent_manifests
from ..agent.prompts import has_builtin_prompt_profile
from ..hook.config import FormatterCwdPolicy, RuntimeFormatterPresetConfig, RuntimeHooksConfig
from ..lsp import LspServerConfigOverride as RuntimeLspServerConfig
from ..lsp import derive_workspace_lsp_defaults, has_builtin_lsp_server_preset
from ..provider import config as provider_config
from .permission import PermissionDecision
from .task import supported_subagent_categories

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
DEFAULT_EXECUTION_ENGINE: ExecutionEngineName = "provider"
type RuntimeAgentPresetId = AgentManifestId
type RuntimeAgentPromptSource = Literal["builtin"]

_VALID_EXECUTION_ENGINES: tuple[ExecutionEngineName, ...] = ("deterministic", "provider")
_TOP_LEVEL_ENV_VARS = (
    APPROVAL_MODE_ENV_VAR,
    MODEL_ENV_VAR,
    EXECUTION_ENGINE_ENV_VAR,
    MAX_STEPS_ENV_VAR,
    TOOL_TIMEOUT_ENV_VAR,
)
_ENV_SETTINGS_LOCK = Lock()
_REPO_CONFIG_KEYS = frozenset(
    {
        "$schema",
        "approval_mode",
        "model",
        "execution_engine",
        "max_steps",
        "tool_timeout_seconds",
        "hooks",
        "tools",
        "skills",
        "context_window",
        "lsp",
        "background_task",
        "mcp",
        "tui",
        "provider_fallback",
        "providers",
        "agent",
        "agents",
        "categories",
    }
)
_USER_CONFIG_KEYS = frozenset({"$schema", "tui", "web", "providers"})
_HOOKS_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "timeout_seconds",
        "pre_tool",
        "post_tool",
        "on_session_start",
        "on_session_end",
        "on_session_idle",
        "on_background_task_completed",
        "on_background_task_failed",
        "on_background_task_cancelled",
        "on_delegated_result_available",
        "on_context_pressure",
        "formatter_presets",
    }
)
_CONTEXT_WINDOW_CONFIG_KEYS = frozenset(
    {
        "version",
        "auto_compaction",
        "max_tool_results",
        "max_tool_result_tokens",
        "max_context_ratio",
        "model_context_window_tokens",
        "reserved_output_tokens",
        "minimum_retained_tool_results",
        "recent_tool_result_count",
        "recent_tool_result_tokens",
        "default_tool_result_tokens",
        "per_tool_result_tokens",
        "tokenizer_model",
        "continuity_preview_items",
        "continuity_preview_chars",
        "context_pressure_threshold",
        "context_pressure_cooldown_steps",
    }
)
_FORMATTER_PRESET_CONFIG_KEYS = frozenset(
    {"command", "extensions", "root_markers", "fallback_commands", "cwd_policy"}
)
_TOOLS_CONFIG_KEYS = frozenset({"builtin", "allowlist", "default"})
_TOOLS_BUILTIN_CONFIG_KEYS = frozenset({"enabled"})
_SKILLS_CONFIG_KEYS = frozenset({"enabled", "paths"})
_LSP_CONFIG_KEYS = frozenset({"enabled", "servers"})
_LSP_SERVER_CONFIG_KEYS = frozenset(
    {"preset", "command", "languages", "extensions", "root_markers", "settings", "init_options"}
)
_MCP_CONFIG_KEYS = frozenset({"enabled", "servers", "request_timeout_seconds"})
_MCP_SERVER_CONFIG_KEYS = frozenset({"transport", "command", "env", "scope"})
_TUI_CONFIG_KEYS = frozenset({"leader_key", "keymap", "preferences"})
_TUI_PREFERENCES_CONFIG_KEYS = frozenset({"theme", "reading"})
_TUI_THEME_CONFIG_KEYS = frozenset({"name", "mode"})
_TUI_READING_CONFIG_KEYS = frozenset({"wrap", "sidebar_collapsed"})
_AGENT_CONFIG_KEYS = frozenset(
    {
        "preset",
        "prompt_profile",
        "prompt_materialization",
        "prompt_ref",
        "prompt_source",
        "hook_refs",
        "model",
        "execution_engine",
        "tools",
        "skills",
        "provider_fallback",
        "fallback_models",
    }
)


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
    allowlist: tuple[str, ...] | None = None
    default: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSkillsConfig:
    enabled: bool | None = None
    paths: tuple[str, ...] = ()


def _empty_context_window_tool_limits() -> dict[str, int]:
    return {}


@dataclass(frozen=True, slots=True)
class RuntimeContextWindowConfig:
    auto_compaction: bool = True
    max_tool_results: int = 4
    max_tool_result_tokens: int | None = None
    max_context_ratio: float | None = None
    model_context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    minimum_retained_tool_results: int = 1
    recent_tool_result_count: int = 1
    recent_tool_result_tokens: int | None = None
    default_tool_result_tokens: int | None = None
    per_tool_result_tokens: Mapping[str, int] = field(
        default_factory=_empty_context_window_tool_limits
    )
    tokenizer_model: str | None = None
    continuity_preview_items: int = 3
    continuity_preview_chars: int = 80
    context_pressure_threshold: float = 0.7
    context_pressure_cooldown_steps: int = 3


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


def _empty_background_task_concurrency_map() -> dict[str, int]:
    return {}


@dataclass(frozen=True, slots=True)
class RuntimeBackgroundTaskConfig:
    default_concurrency: int = 5
    provider_concurrency: Mapping[str, int] = field(
        default_factory=_empty_background_task_concurrency_map
    )
    model_concurrency: Mapping[str, int] = field(
        default_factory=_empty_background_task_concurrency_map
    )


type McpTransport = Literal["stdio"]
type RuntimeMcpServerScope = Literal["runtime", "session"]


@dataclass(frozen=True, slots=True)
class RuntimeMcpServerConfig:
    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    scope: RuntimeMcpServerScope = "runtime"


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
class RuntimeAgentConfig:
    preset: RuntimeAgentPresetId
    prompt_profile: str | None = None
    prompt_ref: str | None = None
    prompt_source: RuntimeAgentPromptSource | None = None
    hook_refs: tuple[str, ...] = ()
    model: str | None = None
    execution_engine: ExecutionEngineName | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeCategoryConfig:
    model: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    approval_mode: PermissionDecision = "ask"
    model: str | None = None
    execution_engine: ExecutionEngineName = DEFAULT_EXECUTION_ENGINE
    max_steps: int | None = None
    tool_timeout_seconds: int | None = None
    hooks: RuntimeHooksConfig | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    context_window: RuntimeContextWindowConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    background_task: RuntimeBackgroundTaskConfig = field(
        default_factory=RuntimeBackgroundTaskConfig
    )
    mcp: RuntimeMcpConfig | None = None
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    providers: RuntimeProvidersConfig | None = None
    agent: RuntimeAgentConfig | None = None
    agents: Mapping[str, RuntimeAgentConfig] | None = None
    categories: Mapping[str, RuntimeCategoryConfig] | None = None


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
    context_window: RuntimeContextWindowConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    background_task: RuntimeBackgroundTaskConfig | None = None
    mcp: RuntimeMcpConfig | None = None
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    providers: RuntimeProvidersConfig | None = None
    agent: RuntimeAgentConfig | None = None
    agents: Mapping[str, RuntimeAgentConfig] | None = None
    categories: Mapping[str, RuntimeCategoryConfig] | None = None


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
    providers = merge_provider_configs(
        global_config.providers, provider_configs_from_env(environment)
    )
    payload = _read_json_object(_user_runtime_config_path_from_env(environment))
    raw_web = payload.get("web")
    configured_provider: str | None = None
    if isinstance(raw_web, dict):
        raw_provider = cast(dict[str, object], raw_web).get("provider")
        if isinstance(raw_provider, str):
            configured_provider = raw_provider
    provider = configured_provider or _first_configured_provider_name(providers)
    return RuntimeWebSettings(
        provider=provider,
        provider_api_key_present=_provider_api_key_present(providers, provider),
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

    env_providers = provider_configs_from_env(environment)
    resolved_providers = merge_provider_configs(
        repo_local.providers,
        merge_provider_configs(env_providers, global_config.providers),
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
        context_window=repo_local.context_window,
        lsp=resolved_lsp,
        background_task=repo_local.background_task or RuntimeBackgroundTaskConfig(),
        mcp=repo_local.mcp,
        tui=resolved_tui,
        provider_fallback=repo_local.provider_fallback,
        providers=resolved_providers,
        agent=resolved_agent,
        agents=repo_local.agents,
        categories=repo_local.categories,
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
    _reject_unknown_config_keys(payload, allowed_keys=_REPO_CONFIG_KEYS, field_path="")

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

    raw_context_window = payload.get("context_window")
    context_window = _parse_context_window_config(raw_context_window)

    raw_lsp = payload.get("lsp")
    lsp = _parse_lsp_config(raw_lsp)

    raw_mcp = payload.get("mcp")
    mcp = _parse_mcp_config(raw_mcp)

    raw_background_task = payload.get("background_task")
    background_task = _parse_background_task_config(raw_background_task)

    raw_tui = payload.get("tui")
    tui = _parse_tui_config(raw_tui)

    raw_provider_fallback = payload.get("provider_fallback")
    provider_fallback = _parse_provider_fallback_config(raw_provider_fallback)

    raw_providers = payload.get("providers")
    providers = _parse_providers_config(raw_providers, env=env)

    raw_agent = payload.get("agent")
    agent = _parse_agent_config(raw_agent, hooks=hooks)
    raw_agents = payload.get("agents")
    agents = _parse_agents_config(raw_agents, hooks=hooks)
    raw_categories = payload.get("categories")
    categories = _parse_categories_config(raw_categories)

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
        context_window=context_window,
        lsp=lsp,
        background_task=background_task,
        mcp=mcp,
        tui=tui,
        provider_fallback=provider_fallback,
        providers=providers,
        agent=agent,
        agents=agents,
        categories=categories,
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
    _reject_unknown_config_keys(payload, allowed_keys=_USER_CONFIG_KEYS, field_path="")
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
    _reject_unknown_config_keys(
        hooks_payload,
        allowed_keys=_HOOKS_CONFIG_KEYS,
        field_path="hooks",
    )
    enabled = hooks_payload.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("runtime config field 'hooks.enabled' must be a boolean when provided")
    timeout_seconds = _parse_hook_timeout_seconds(
        hooks_payload.get("timeout_seconds"),
        source="runtime config field 'hooks.timeout_seconds'",
    )

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
    on_context_pressure = _parse_command_list(
        hooks_payload.get("on_context_pressure"),
        field_path="hooks.on_context_pressure",
    )
    formatter_presets: dict[str, RuntimeFormatterPresetConfig] = _parse_formatter_presets_config(
        hooks_payload.get("formatter_presets"),
        field_path="hooks.formatter_presets",
    )

    return RuntimeHooksConfig(
        enabled=enabled,
        timeout_seconds=timeout_seconds,
        pre_tool=pre_tool,
        post_tool=post_tool,
        on_session_start=on_session_start,
        on_session_end=on_session_end,
        on_session_idle=on_session_idle,
        on_background_task_completed=on_background_task_completed,
        on_background_task_failed=on_background_task_failed,
        on_background_task_cancelled=on_background_task_cancelled,
        on_delegated_result_available=on_delegated_result_available,
        on_context_pressure=on_context_pressure,
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
    _reject_unknown_config_keys(
        preset_payload,
        allowed_keys=_FORMATTER_PRESET_CONFIG_KEYS,
        field_path=field_path,
    )
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


def _reject_unknown_config_keys(
    payload: Mapping[str, object], *, allowed_keys: frozenset[str], field_path: str
) -> None:
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if not unknown_keys:
        return
    first_key = unknown_keys[0]
    full_path = f"{field_path}.{first_key}" if field_path else first_key
    raise ValueError(f"runtime config field '{full_path}' is not supported")


def _parse_hook_timeout_seconds(raw_value: object, *, source: str) -> float | None:
    if raw_value is None:
        return RuntimeHooksConfig().timeout_seconds
    if not isinstance(raw_value, int | float) or isinstance(raw_value, bool) or raw_value < 1:
        raise ValueError(f"{source} must be a number greater than or equal to 1")
    return float(raw_value)


class _RuntimeToolsBuiltinValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="tools.builtin.enabled")

    def to_runtime_config(self) -> RuntimeToolsBuiltinConfig:
        return RuntimeToolsBuiltinConfig(enabled=self.enabled)


class _RuntimeToolsValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    builtin: _RuntimeToolsBuiltinValidationModel | None = None
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
            allowlist=self.allowlist,
            default=self.default,
        )


class _RuntimeSkillsValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def _parse_optional_positive_int(value: object, *, field_path: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be an integer when provided")
    if value < 1:
        raise ValueError(f"runtime config field '{field_path}' must be greater than or equal to 1")
    return value


def _parse_concurrency_limit(value: object, *, field_path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be an integer")
    if value < 1:
        raise ValueError(f"runtime config field '{field_path}' must be greater than or equal to 1")
    return value


def _parse_concurrency_map(value: object, *, field_path: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")
    parsed: dict[str, int] = {}
    for raw_key, raw_limit in cast(dict[object, object], value).items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"runtime config field '{field_path}' keys must be non-empty strings")
        parsed[raw_key] = _parse_concurrency_limit(
            raw_limit,
            field_path=f"{field_path}.{raw_key}",
        )
    return parsed


def _parse_background_task_config(raw_value: object) -> RuntimeBackgroundTaskConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError("runtime config field 'background_task' must be an object when provided")
    payload = cast(dict[str, object], raw_value)
    default_concurrency = RuntimeBackgroundTaskConfig().default_concurrency
    if "default_concurrency" in payload:
        default_concurrency = _parse_concurrency_limit(
            payload.get("default_concurrency"),
            field_path="background_task.default_concurrency",
        )
    return RuntimeBackgroundTaskConfig(
        default_concurrency=default_concurrency,
        provider_concurrency=_parse_concurrency_map(
            payload.get("provider_concurrency"),
            field_path="background_task.provider_concurrency",
        ),
        model_concurrency=_parse_concurrency_map(
            payload.get("model_concurrency"),
            field_path="background_task.model_concurrency",
        ),
    )


class _RuntimeContextWindowValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    version: int = 1
    auto_compaction: bool = True
    max_tool_results: int = 4
    max_tool_result_tokens: int | None = None
    max_context_ratio: float | None = None
    model_context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    minimum_retained_tool_results: int = 1
    recent_tool_result_count: int = 1
    recent_tool_result_tokens: int | None = None
    default_tool_result_tokens: int | None = None
    per_tool_result_tokens: dict[str, int] = Field(default_factory=dict)
    tokenizer_model: str | None = None
    continuity_preview_items: int = 3
    continuity_preview_chars: int = 80
    context_pressure_threshold: float = 0.7
    context_pressure_cooldown_steps: int = 3

    @field_validator("auto_compaction", mode="before")
    @classmethod
    def _validate_auto_compaction(cls, value: object) -> bool:
        parsed = _parse_optional_bool(value, field_path="context_window.auto_compaction")
        return True if parsed is None else parsed

    @field_validator("version", mode="before")
    @classmethod
    def _validate_version(cls, value: object) -> int:
        if value is None:
            return 1
        if value != 1:
            raise ValueError("runtime config field 'context_window.version' must be 1")
        return 1

    @field_validator(
        "max_tool_results",
        "minimum_retained_tool_results",
        "recent_tool_result_count",
        mode="before",
    )
    @classmethod
    def _validate_positive_int(cls, value: object, info: ValidationInfo) -> int:
        field_name = info.field_name or "unknown"
        field_path = f"context_window.{field_name}"
        if value is None:
            defaults = RuntimeContextWindowConfig()
            return cast(int, getattr(defaults, field_name))
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"runtime config field '{field_path}' must be an integer")
        if value < 1:
            raise ValueError(
                f"runtime config field '{field_path}' must be greater than or equal to 1"
            )
        return value

    @field_validator(
        "max_tool_result_tokens",
        "model_context_window_tokens",
        "recent_tool_result_tokens",
        "default_tool_result_tokens",
        "continuity_preview_items",
        "continuity_preview_chars",
        mode="before",
    )
    @classmethod
    def _validate_optional_positive_int(cls, value: object, info: ValidationInfo) -> int | None:
        field_name = info.field_name or "unknown"
        field_path = f"context_window.{field_name}"
        parsed = _parse_optional_positive_int(value, field_path=field_path)
        if parsed is None and field_name in {
            "continuity_preview_items",
            "continuity_preview_chars",
        }:
            defaults = RuntimeContextWindowConfig()
            return cast(int, getattr(defaults, field_name))
        return parsed

    @field_validator("reserved_output_tokens", mode="before")
    @classmethod
    def _validate_reserved_output_tokens(cls, value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                "runtime config field 'context_window.reserved_output_tokens' must be an integer"
            )
        if value < 1:
            raise ValueError(
                "runtime config field 'context_window.reserved_output_tokens' must be "
                "greater than or equal to 1"
            )
        return value

    @field_validator("max_context_ratio", mode="before")
    @classmethod
    def _validate_max_context_ratio(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(
                "runtime config field 'context_window.max_context_ratio' must be a number"
            )
        parsed = float(value)
        if not 0 < parsed <= 1:
            raise ValueError(
                "runtime config field 'context_window.max_context_ratio' must be greater "
                "than 0 and less than or equal to 1"
            )
        return parsed

    @field_validator("context_pressure_threshold", mode="before")
    @classmethod
    def _validate_context_pressure_threshold(cls, value: object) -> float:
        if value is None:
            return 0.7
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(
                "runtime config field 'context_window.context_pressure_threshold' must be a number"
            )
        parsed = float(value)
        if not 0 < parsed <= 1:
            raise ValueError(
                "runtime config field 'context_window.context_pressure_threshold' must be greater "
                "than 0 and less than or equal to 1"
            )
        return parsed

    @field_validator("context_pressure_cooldown_steps", mode="before")
    @classmethod
    def _validate_context_pressure_cooldown_steps(cls, value: object) -> int:
        if value is None:
            return 3
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                "runtime config field 'context_window.context_pressure_cooldown_steps' "
                "must be an integer"
            )
        if value < 1:
            raise ValueError(
                "runtime config field 'context_window.context_pressure_cooldown_steps' "
                "must be greater than or equal to 1"
            )
        return value

    @field_validator("per_tool_result_tokens", mode="before")
    @classmethod
    def _validate_per_tool_result_tokens(cls, value: object) -> dict[str, int]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(
                "runtime config field 'context_window.per_tool_result_tokens' must be an object"
            )
        parsed: dict[str, int] = {}
        for raw_key, raw_limit in cast(dict[object, object], value).items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError(
                    "runtime config field 'context_window.per_tool_result_tokens' keys "
                    "must be non-empty strings"
                )
            limit = _parse_optional_positive_int(
                raw_limit,
                field_path=f"context_window.per_tool_result_tokens.{raw_key}",
            )
            assert limit is not None
            parsed[raw_key] = limit
        return parsed

    @field_validator("tokenizer_model", mode="before")
    @classmethod
    def _validate_tokenizer_model(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "runtime config field 'context_window.tokenizer_model' must be a non-empty string"
            )
        return value.strip()

    def to_runtime_config(self) -> RuntimeContextWindowConfig:
        return RuntimeContextWindowConfig(
            auto_compaction=self.auto_compaction,
            max_tool_results=self.max_tool_results,
            max_tool_result_tokens=self.max_tool_result_tokens,
            max_context_ratio=self.max_context_ratio,
            model_context_window_tokens=self.model_context_window_tokens,
            reserved_output_tokens=self.reserved_output_tokens,
            minimum_retained_tool_results=self.minimum_retained_tool_results,
            recent_tool_result_count=self.recent_tool_result_count,
            recent_tool_result_tokens=self.recent_tool_result_tokens,
            default_tool_result_tokens=self.default_tool_result_tokens,
            per_tool_result_tokens=dict(self.per_tool_result_tokens),
            tokenizer_model=self.tokenizer_model,
            continuity_preview_items=self.continuity_preview_items,
            continuity_preview_chars=self.continuity_preview_chars,
            context_pressure_threshold=self.context_pressure_threshold,
            context_pressure_cooldown_steps=self.context_pressure_cooldown_steps,
        )


def _validation_context_field_path(info: ValidationInfo, *, default: str) -> str:
    context = info.context
    if isinstance(context, dict):
        typed_context = cast(dict[str, object], context)
        field_path = typed_context.get("field_path")
        if isinstance(field_path, str):
            return field_path
    return default


class _RuntimeMcpServerValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    scope: RuntimeMcpServerScope = "runtime"

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

    @field_validator("scope", mode="before")
    @classmethod
    def _validate_scope(cls, value: object, info: ValidationInfo) -> RuntimeMcpServerScope:
        if value is None:
            return "runtime"
        field_path = _validation_context_field_path(info, default="mcp.servers")
        if value not in ("runtime", "session"):
            raise ValueError(
                f"runtime config field '{field_path}.scope' must be one of: runtime, session"
            )
        return value

    def to_runtime_config(self) -> RuntimeMcpServerConfig:
        return RuntimeMcpServerConfig(
            transport=self.transport,
            command=self.command,
            env=self.env,
            scope=self.scope,
        )


class _RuntimeMcpValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
            _reject_unknown_config_keys(
                cast(dict[str, object], raw_server),
                allowed_keys=_MCP_SERVER_CONFIG_KEYS,
                field_path=f"mcp.servers.{server_name}",
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
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

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
        base_field_path = None
        if context is not None:
            raw_base_field_path = context.get("field_path")
            if isinstance(raw_base_field_path, str):
                base_field_path = raw_base_field_path
        message = _format_settings_validation_error(exc, field_path=base_field_path)
        raise ValueError(message) from exc


def _parse_tools_config(
    raw_tools: object,
    *,
    field_path: str = "tools",
) -> RuntimeToolsConfig | None:
    if raw_tools is None:
        return None
    if not isinstance(raw_tools, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    tools_payload = cast(dict[str, object], raw_tools)
    _reject_unknown_config_keys(
        tools_payload,
        allowed_keys=_TOOLS_CONFIG_KEYS,
        field_path=field_path,
    )
    raw_builtin = tools_payload.get("builtin")
    if isinstance(raw_builtin, dict):
        _reject_unknown_config_keys(
            cast(dict[str, object], raw_builtin),
            allowed_keys=_TOOLS_BUILTIN_CONFIG_KEYS,
            field_path=f"{field_path}.builtin",
        )
    return cast(
        RuntimeToolsConfig | None,
        _parse_runtime_config_section(
            tools_payload,
            field_path=field_path,
            model_type=_RuntimeToolsValidationModel,
            context={"field_path": field_path},
        ),
    )


def _parse_skills_config(raw_skills: object) -> RuntimeSkillsConfig | None:
    if raw_skills is None:
        return None
    if not isinstance(raw_skills, dict):
        raise ValueError("runtime config field 'skills' must be an object when provided")

    skills_payload = cast(dict[str, object], raw_skills)
    _reject_unknown_config_keys(
        skills_payload,
        allowed_keys=_SKILLS_CONFIG_KEYS,
        field_path="skills",
    )
    return cast(
        RuntimeSkillsConfig | None,
        _parse_runtime_config_section(
            skills_payload,
            field_path="skills",
            model_type=_RuntimeSkillsValidationModel,
        ),
    )


def _parse_context_window_config(raw_context_window: object) -> RuntimeContextWindowConfig | None:
    if raw_context_window is None:
        return None
    if not isinstance(raw_context_window, dict):
        raise ValueError("runtime config field 'context_window' must be an object when provided")

    context_window_payload = cast(dict[str, object], raw_context_window)
    _reject_unknown_config_keys(
        context_window_payload,
        allowed_keys=_CONTEXT_WINDOW_CONFIG_KEYS,
        field_path="context_window",
    )
    return cast(
        RuntimeContextWindowConfig | None,
        _parse_runtime_config_section(
            context_window_payload,
            field_path="context_window",
            model_type=_RuntimeContextWindowValidationModel,
        ),
    )


def _parse_lsp_config(raw_lsp: object) -> RuntimeLspConfig | None:
    if raw_lsp is None:
        return None
    if not isinstance(raw_lsp, dict):
        raise ValueError("runtime config field 'lsp' must be an object when provided")

    lsp_payload = cast(dict[str, object], raw_lsp)
    _reject_unknown_config_keys(lsp_payload, allowed_keys=_LSP_CONFIG_KEYS, field_path="lsp")
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
    _reject_unknown_config_keys(
        server_payload,
        allowed_keys=_LSP_SERVER_CONFIG_KEYS,
        field_path=field_path,
    )
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
    _reject_unknown_config_keys(mcp_payload, allowed_keys=_MCP_CONFIG_KEYS, field_path="mcp")
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
    _reject_unknown_config_keys(tui_payload, allowed_keys=_TUI_CONFIG_KEYS, field_path="tui")
    return cast(
        RuntimeTuiConfig | None,
        _parse_runtime_config_section(
            tui_payload,
            field_path="tui",
            model_type=_RuntimeTuiValidationModel,
        ),
    )


def _parse_agent_config(
    raw_agent: object,
    *,
    hooks: RuntimeHooksConfig | None = None,
) -> RuntimeAgentConfig | None:
    if raw_agent is None:
        return None
    if not isinstance(raw_agent, dict):
        raise ValueError("runtime config field 'agent' must be an object when provided")

    payload = cast(dict[str, object], raw_agent)
    _reject_unknown_config_keys(payload, allowed_keys=_AGENT_CONFIG_KEYS, field_path="agent")
    if "preset" not in payload:
        raise ValueError("runtime config field 'agent.preset' is required")

    raw_preset = payload.get("preset")
    if not isinstance(raw_preset, str) or get_builtin_agent_manifest(raw_preset) is None:
        valid_presets = ", ".join(manifest.id for manifest in list_builtin_agent_manifests())
        raise ValueError(f"runtime config field 'agent.preset' must be one of: {valid_presets}")

    prompt_profile = payload.get("prompt_profile")
    if prompt_profile is not None and (
        not isinstance(prompt_profile, str) or not prompt_profile.strip()
    ):
        raise ValueError("runtime config field 'agent.prompt_profile' must be a non-empty string")

    prompt_ref = payload.get("prompt_ref")
    if prompt_ref is not None and (not isinstance(prompt_ref, str) or not prompt_ref.strip()):
        raise ValueError("runtime config field 'agent.prompt_ref' must be a non-empty string")

    prompt_source = payload.get("prompt_source")
    if prompt_source is not None and prompt_source != "builtin":
        raise ValueError("runtime config field 'agent.prompt_source' must be one of: builtin")
    if prompt_source is not None and prompt_ref is None:
        raise ValueError("runtime config field 'agent.prompt_ref' is required with prompt_source")
    normalized_prompt_ref = prompt_ref.strip() if isinstance(prompt_ref, str) else None
    if normalized_prompt_ref is not None and not has_builtin_prompt_profile(normalized_prompt_ref):
        raise ValueError(
            "runtime config field 'agent.prompt_ref' references unknown prompt profile"
        )
    normalized_prompt_source = "builtin" if normalized_prompt_ref is not None else None

    hook_refs = _parse_agent_hook_refs(payload.get("hook_refs"), hooks=hooks)

    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("runtime config field 'agent.model' must be a non-empty string")
    execution_engine = _parse_execution_engine(
        payload.get("execution_engine"),
        source="runtime config field 'agent.execution_engine'",
        allow_none=True,
    )

    provider_fallback = _parse_agent_provider_fallback_config(payload, model=model)

    return RuntimeAgentConfig(
        preset=cast(RuntimeAgentPresetId, raw_preset),
        prompt_profile=(
            prompt_profile.strip() if isinstance(prompt_profile, str) else normalized_prompt_ref
        ),
        prompt_ref=normalized_prompt_ref,
        prompt_source=cast(RuntimeAgentPromptSource, normalized_prompt_source),
        hook_refs=hook_refs,
        model=model.strip() if isinstance(model, str) else None,
        execution_engine=execution_engine,
        tools=_parse_tools_config(payload.get("tools"), field_path="agent.tools"),
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
            prompt_ref=agent.prompt_ref,
            prompt_source=agent.prompt_source,
            hook_refs=agent.hook_refs,
            model=agent.model or manifest.model_preference,
            execution_engine=agent.execution_engine or manifest.execution_engine,
            tools=agent.tools,
            skills=agent.skills,
            provider_fallback=agent.provider_fallback,
        )
    return agent


def _parse_agent_hook_refs(
    raw_hook_refs: object,
    *,
    hooks: RuntimeHooksConfig | None,
) -> tuple[str, ...]:
    hook_refs = _parse_string_list(raw_hook_refs, field_path="agent.hook_refs")
    if not hook_refs:
        return ()
    available_hook_refs = set((hooks or RuntimeHooksConfig()).formatter_presets)
    for hook_ref in hook_refs:
        if hook_ref not in available_hook_refs:
            valid_refs = ", ".join(sorted(available_hook_refs))
            raise ValueError(
                "runtime config field 'agent.hook_refs' references unknown hook preset: "
                f"{hook_ref}; valid presets are: {valid_refs}"
            )
    return hook_refs


def _parse_agent_provider_fallback_config(
    payload: Mapping[str, object],
    *,
    model: object,
) -> RuntimeProviderFallbackConfig | None:
    raw_provider_fallback = payload.get("provider_fallback")
    raw_fallback_models = payload.get("fallback_models")
    if raw_provider_fallback is not None and raw_fallback_models is not None:
        raise ValueError(
            "runtime config field 'agent.fallback_models' cannot be combined with "
            "'agent.provider_fallback'"
        )
    if raw_fallback_models is None:
        return _parse_provider_fallback_config(raw_provider_fallback)
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            "runtime config field 'agent.model' is required when 'agent.fallback_models' "
            "is provided"
        )
    return parse_provider_fallback_payload(
        {
            "preferred_model": model.strip(),
            "fallback_models": raw_fallback_models,
        },
        source="runtime config field 'agent.provider_fallback'",
    )


_AGENTS_MAP_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def _parse_agents_config(
    raw_agents: object,
    *,
    hooks: RuntimeHooksConfig | None = None,
) -> Mapping[str, RuntimeAgentConfig] | None:
    if raw_agents is None:
        return None
    if not isinstance(raw_agents, dict):
        raise ValueError("runtime config field 'agents' must be an object when provided")

    raw_payload = cast(dict[object, object], raw_agents)
    parsed: dict[str, RuntimeAgentConfig] = {}
    for key, value in raw_payload.items():
        if not isinstance(key, str) or not _AGENTS_MAP_KEY_PATTERN.fullmatch(key):
            raise ValueError("runtime config field 'agents' keys must match '^[a-z][a-z0-9_-]*$'")
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field 'agents.{key}' must be an object when provided")

        entry_payload = dict(cast(dict[str, object], value))
        is_builtin_key = get_builtin_agent_manifest(key) is not None
        if "preset" not in entry_payload:
            if is_builtin_key:
                entry_payload["preset"] = key
            else:
                valid_presets = ", ".join(
                    manifest.id for manifest in list_builtin_agent_manifests()
                )
                raise ValueError(
                    f"runtime config field 'agents.{key}.preset' must be one of: {valid_presets}"
                )

        try:
            parsed_entry = _parse_agent_config(entry_payload, hooks=hooks)
        except ValueError as exc:
            message = (
                str(exc).replace("agent.", f"agents.{key}.").replace("'agent'", f"'agents.{key}'")
            )
            raise ValueError(message) from exc

        resolved_entry = _resolve_agent_config(parsed_entry)
        if resolved_entry is None:
            raise ValueError(f"runtime config field 'agents.{key}' must resolve to a valid agent")
        parsed[key] = resolved_entry
    return parsed


def _parse_categories_config(
    raw_categories: object,
) -> Mapping[str, RuntimeCategoryConfig] | None:
    if raw_categories is None:
        return None
    if not isinstance(raw_categories, dict):
        raise ValueError("runtime config field 'categories' must be an object when provided")

    supported_categories = set(supported_subagent_categories())
    raw_payload = cast(dict[object, object], raw_categories)
    parsed: dict[str, RuntimeCategoryConfig] = {}
    for key, value in raw_payload.items():
        if not isinstance(key, str) or not key:
            raise ValueError("runtime config field 'categories' keys must be non-empty strings")
        if key not in supported_categories:
            valid_categories = ", ".join(sorted(supported_categories))
            raise ValueError(
                f"runtime config field 'categories.{key}' uses unsupported task category; "
                f"valid categories are: {valid_categories}"
            )
        if not isinstance(value, dict):
            raise ValueError(
                f"runtime config field 'categories.{key}' must be an object when provided"
            )
        category_payload = cast(dict[str, object], value)
        model = category_payload.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError(
                f"runtime config field 'categories.{key}.model' must be a non-empty string"
            )
        parsed[key] = RuntimeCategoryConfig(model=model.strip() if isinstance(model, str) else None)
    return parsed


def serialize_runtime_categories_config(
    categories: Mapping[str, RuntimeCategoryConfig] | None,
) -> dict[str, object] | None:
    if categories is None:
        return None
    serialized: dict[str, object] = {}
    for category_name, category in categories.items():
        payload: dict[str, object] = {}
        if category.model is not None:
            payload["model"] = category.model
        serialized[category_name] = payload
    return serialized


def serialize_runtime_agents_config(
    agents: Mapping[str, RuntimeAgentConfig] | None,
) -> dict[str, object] | None:
    if agents is None:
        return None
    serialized: dict[str, object] = {}
    for agent_id, agent in agents.items():
        entry = serialize_runtime_agent_config(agent)
        if entry is not None:
            serialized[agent_id] = entry
    return serialized


def parse_runtime_agent_payload(
    raw_agent: object,
    *,
    source: str,
    hooks: RuntimeHooksConfig | None = None,
) -> RuntimeAgentConfig | None:
    try:
        return _resolve_agent_config(_parse_agent_config(raw_agent, hooks=hooks))
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def parse_runtime_agents_payload(
    raw_agents: object,
    *,
    source: str,
    hooks: RuntimeHooksConfig | None = None,
) -> Mapping[str, RuntimeAgentConfig] | None:
    try:
        return _parse_agents_config(raw_agents, hooks=hooks)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def parse_runtime_categories_payload(
    raw_categories: object,
    *,
    source: str,
) -> Mapping[str, RuntimeCategoryConfig] | None:
    try:
        return _parse_categories_config(raw_categories)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def parse_runtime_context_window_payload(
    raw_context_window: object,
    *,
    source: str,
) -> RuntimeContextWindowConfig | None:
    try:
        return _parse_context_window_config(raw_context_window)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def serialize_runtime_context_window_config(
    context_window: RuntimeContextWindowConfig | None,
) -> dict[str, object] | None:
    if context_window is None:
        return None
    payload: dict[str, object] = {
        "version": 1,
        "auto_compaction": context_window.auto_compaction,
        "max_tool_results": context_window.max_tool_results,
        "minimum_retained_tool_results": context_window.minimum_retained_tool_results,
        "recent_tool_result_count": context_window.recent_tool_result_count,
        "continuity_preview_items": context_window.continuity_preview_items,
        "continuity_preview_chars": context_window.continuity_preview_chars,
        "context_pressure_threshold": context_window.context_pressure_threshold,
        "context_pressure_cooldown_steps": context_window.context_pressure_cooldown_steps,
    }
    if context_window.max_tool_result_tokens is not None:
        payload["max_tool_result_tokens"] = context_window.max_tool_result_tokens
    if context_window.max_context_ratio is not None:
        payload["max_context_ratio"] = context_window.max_context_ratio
    if context_window.model_context_window_tokens is not None:
        payload["model_context_window_tokens"] = context_window.model_context_window_tokens
    if context_window.reserved_output_tokens is not None:
        payload["reserved_output_tokens"] = context_window.reserved_output_tokens
    if context_window.recent_tool_result_tokens is not None:
        payload["recent_tool_result_tokens"] = context_window.recent_tool_result_tokens
    if context_window.default_tool_result_tokens is not None:
        payload["default_tool_result_tokens"] = context_window.default_tool_result_tokens
    if context_window.per_tool_result_tokens:
        payload["per_tool_result_tokens"] = dict(context_window.per_tool_result_tokens)
    if context_window.tokenizer_model is not None:
        payload["tokenizer_model"] = context_window.tokenizer_model
    return payload


def serialize_runtime_background_task_config(
    background_task: RuntimeBackgroundTaskConfig,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "default_concurrency": background_task.default_concurrency,
    }
    if background_task.provider_concurrency:
        payload["provider_concurrency"] = dict(background_task.provider_concurrency)
    if background_task.model_concurrency:
        payload["model_concurrency"] = dict(background_task.model_concurrency)
    return payload


def serialize_runtime_agent_config(agent: RuntimeAgentConfig | None) -> dict[str, object] | None:
    if agent is None:
        return None
    payload: dict[str, object] = {"preset": agent.preset}
    manifest = get_builtin_agent_manifest(agent.preset)
    if agent.prompt_profile is not None:
        payload["prompt_profile"] = agent.prompt_profile
    if manifest is not None and manifest.prompt_materialization is not None:
        prompt_materialization_profile = (
            agent.prompt_profile or manifest.prompt_materialization.profile
        )
        payload["prompt_materialization"] = manifest.prompt_materialization.to_payload(
            profile=prompt_materialization_profile,
        )
    if agent.prompt_ref is not None:
        payload["prompt_ref"] = agent.prompt_ref
    if agent.prompt_source is not None:
        payload["prompt_source"] = agent.prompt_source
    if agent.hook_refs:
        payload["hook_refs"] = list(agent.hook_refs)
    if agent.model is not None:
        payload["model"] = agent.model
    if agent.execution_engine is not None:
        payload["execution_engine"] = agent.execution_engine
    if agent.tools is not None:
        tools_payload: dict[str, object | None] = {
            "builtin": None
            if agent.tools.builtin is None
            else {"enabled": agent.tools.builtin.enabled},
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
    context: dict[str, object] | None = None,
) -> object | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    validation_context: dict[str, object] = (
        {"field_path": field_path} if context is None else context
    )
    validated_model = _validate_runtime_config_model(
        model_type,
        cast(dict[str, object], raw_value),
        context=validation_context,
    )
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
        ("deepseek", providers.deepseek),
        ("glm", providers.glm),
        ("grok", providers.grok),
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
    if provider == "deepseek":
        return bool(providers.deepseek and providers.deepseek.api_key)
    if provider == "glm":
        return bool(providers.glm and providers.glm.api_key)
    if provider == "grok":
        return bool(providers.grok and providers.grok.api_key)
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
    if provider in {"deepseek", "glm", "grok", "minimax", "kimi", "opencode-go", "qwen"}:
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


def _format_settings_validation_error(
    exc: ValidationError,
    *,
    field_path: str | None = None,
) -> str:
    messages: list[str] = []
    for error in exc.errors():
        if error.get("type") == "extra_forbidden":
            loc = error.get("loc")
            loc_parts = tuple(str(part) for part in loc)
            base_path = field_path or ""
            full_path = ".".join(part for part in (base_path, *loc_parts) if part)
            if full_path:
                messages.append(f"runtime config field '{full_path}' is not supported")
                continue
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
    return DEFAULT_EXECUTION_ENGINE


def _resolve_max_steps(
    *, explicit: int | None, repo_local: int | None, environment: int | None
) -> int | None:
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
