from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
from ..lsp import LspServerConfigOverride as RuntimeLspServerConfig
from ..lsp import has_builtin_lsp_server_preset
from ..provider import config as provider_config
from .permission import PermissionDecision

RuntimeProviderFallbackConfig = provider_config.ProviderFallbackConfig
RuntimeProvidersConfig = provider_config.ProviderConfigs
parse_provider_fallback_payload = provider_config.parse_provider_fallback_payload
parse_provider_configs_payload = provider_config.parse_provider_configs_payload
serialize_provider_fallback_config = provider_config.serialize_provider_fallback_config
serialize_provider_configs = provider_config.serialize_provider_configs

RUNTIME_CONFIG_FILE_NAME = ".voidcode.json"
APPROVAL_MODE_ENV_VAR = "VOIDCODE_APPROVAL_MODE"
MODEL_ENV_VAR = "VOIDCODE_MODEL"
EXECUTION_ENGINE_ENV_VAR = "VOIDCODE_EXECUTION_ENGINE"
MAX_STEPS_ENV_VAR = "VOIDCODE_MAX_STEPS"
_VALID_APPROVAL_MODES = ("allow", "deny", "ask")
_VALID_TUI_COMMANDS = ("command_palette", "session_new", "session_resume")

type ExecutionEngineName = Literal["deterministic", "single_agent"]

_VALID_EXECUTION_ENGINES: tuple[ExecutionEngineName, ...] = ("deterministic", "single_agent")
_TOP_LEVEL_ENV_VARS = (
    APPROVAL_MODE_ENV_VAR,
    MODEL_ENV_VAR,
    EXECUTION_ENGINE_ENV_VAR,
    MAX_STEPS_ENV_VAR,
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


@dataclass(frozen=True, slots=True)
class RuntimeToolsBuiltinConfig:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeToolsConfig:
    builtin: RuntimeToolsBuiltinConfig | None = None
    paths: tuple[str, ...] = ()


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


@dataclass(frozen=True, slots=True)
class RuntimeTuiConfig:
    leader_key: str = "alt+x"
    keymap: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RuntimePlanConfig:
    provider: str | None = None
    module: str | None = None
    factory: str | None = None
    options: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    approval_mode: PermissionDecision = "ask"
    model: str | None = None
    execution_engine: ExecutionEngineName = "deterministic"
    max_steps: int = 4
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


@dataclass(frozen=True, slots=True)
class RuntimeConfigOverrides:
    approval_mode: PermissionDecision | None = None
    model: str | None = None
    execution_engine: ExecutionEngineName | None = None
    max_steps: int | None = None
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


def runtime_config_path(workspace: Path) -> Path:
    return workspace / RUNTIME_CONFIG_FILE_NAME


def load_runtime_config(
    workspace: Path,
    *,
    approval_mode: PermissionDecision | None = None,
    model: str | None = None,
    execution_engine: ExecutionEngineName | None = None,
    max_steps: int | None = None,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    resolved_workspace = workspace.resolve()
    environment: Mapping[str, str] = os.environ if env is None else env
    env_overrides = _load_environment_runtime_config(environment)
    repo_local = _load_repo_local_config(resolved_workspace, env=environment)

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
        hooks=repo_local.hooks,
        tools=repo_local.tools,
        skills=repo_local.skills,
        lsp=repo_local.lsp,
        acp=repo_local.acp,
        mcp=repo_local.mcp,
        tui=repo_local.tui,
        provider_fallback=repo_local.provider_fallback,
        providers=repo_local.providers,
        plan=repo_local.plan,
    )


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

    raw_hooks = payload.get("hooks")
    hooks = _parse_hooks_config(raw_hooks)

    raw_tools = payload.get("tools")
    tools = _parse_tools_config(raw_tools)

    raw_skills = payload.get("skills")
    skills = _parse_skills_config(raw_skills)

    raw_lsp = payload.get("lsp")
    lsp = _parse_lsp_config(raw_lsp)

    raw_acp = payload.get("acp")
    acp = _parse_acp_config(raw_acp)

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
        hooks=hooks,
        tools=tools,
        skills=skills,
        lsp=lsp,
        acp=acp,
        mcp=mcp,
        tui=tui,
        provider_fallback=provider_fallback,
        providers=providers,
        plan=plan,
    )


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
    formatter_presets: dict[str, RuntimeFormatterPresetConfig] = _parse_formatter_presets_config(
        hooks_payload.get("formatter_presets"),
        field_path="hooks.formatter_presets",
    )

    return RuntimeHooksConfig(
        enabled=enabled,
        pre_tool=pre_tool,
        post_tool=post_tool,
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
        parsed_presets[preset_name] = _parse_formatter_preset_config(
            raw_preset,
            field_path=f"{field_path}.{preset_name}",
        )
    return parsed_presets


def _parse_formatter_preset_config(
    raw_value: object, *, field_path: str
) -> RuntimeFormatterPresetConfig:
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object")

    preset_payload = cast(dict[str, object], raw_value)
    command = _parse_string_list(preset_payload.get("command"), field_path=f"{field_path}.command")
    if not command:
        raise ValueError(
            f"runtime config field '{field_path}.command' must contain at least one string"
        )
    return RuntimeFormatterPresetConfig(command=command)


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

    def to_runtime_config(self) -> RuntimeToolsConfig:
        return RuntimeToolsConfig(
            builtin=self.builtin.to_runtime_config() if self.builtin is not None else None,
            paths=self.paths,
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


class _RuntimeAcpValidationModel(BaseModel):
    enabled: bool | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="acp.enabled")

    def to_runtime_config(self) -> RuntimeAcpConfig:
        return RuntimeAcpConfig(enabled=self.enabled)


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

    def to_runtime_config(self) -> RuntimeMcpConfig:
        return RuntimeMcpConfig(
            enabled=self.enabled,
            servers={
                server_name: server.to_runtime_config()
                for server_name, server in self.servers.items()
            }
            if self.servers is not None
            else None,
        )


class _RuntimeTuiValidationModel(BaseModel):
    leader_key: str = "alt+x"
    keymap: dict[str, str] | None = None

    @field_validator("leader_key", mode="before")
    @classmethod
    def _validate_leader_key(cls, value: object) -> str:
        if value is None:
            return "alt+x"
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
        return RuntimeTuiConfig(leader_key=self.leader_key, keymap=self.keymap)


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
    return _parse_runtime_config_section(
        tools_payload,
        field_path="tools",
        model_type=_RuntimeToolsValidationModel,
    )


def _parse_skills_config(raw_skills: object) -> RuntimeSkillsConfig | None:
    if raw_skills is None:
        return None
    if not isinstance(raw_skills, dict):
        raise ValueError("runtime config field 'skills' must be an object when provided")

    skills_payload = cast(dict[str, object], raw_skills)
    return _parse_runtime_config_section(
        skills_payload,
        field_path="skills",
        model_type=_RuntimeSkillsValidationModel,
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
    if preset is not None:
        if not isinstance(preset, str) or not preset:
            raise ValueError(f"runtime config field '{field_path}.preset' must be a string")
        if not has_builtin_lsp_server_preset(preset):
            raise ValueError(
                f"runtime config field '{field_path}.preset' references unknown preset"
            )
    command = _parse_string_list(server_payload.get("command"), field_path=f"{field_path}.command")
    if not command and preset is None and not has_builtin_lsp_server_preset(server_name):
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


def _parse_acp_config(raw_acp: object) -> RuntimeAcpConfig | None:
    if raw_acp is None:
        return None
    if not isinstance(raw_acp, dict):
        raise ValueError("runtime config field 'acp' must be an object when provided")

    acp_payload = cast(dict[str, object], raw_acp)
    return _parse_runtime_config_section(
        acp_payload,
        field_path="acp",
        model_type=_RuntimeAcpValidationModel,
    )


def _parse_mcp_config(raw_mcp: object) -> RuntimeMcpConfig | None:
    if raw_mcp is None:
        return None
    if not isinstance(raw_mcp, dict):
        raise ValueError("runtime config field 'mcp' must be an object when provided")

    mcp_payload = cast(dict[str, object], raw_mcp)
    return _parse_runtime_config_section(
        mcp_payload,
        field_path="mcp",
        model_type=_RuntimeMcpValidationModel,
    )


def _parse_tui_config(raw_tui: object) -> RuntimeTuiConfig | None:
    if raw_tui is None:
        return None
    if not isinstance(raw_tui, dict):
        raise ValueError("runtime config field 'tui' must be an object when provided")

    tui_payload = cast(dict[str, object], raw_tui)
    return _parse_runtime_config_section(
        tui_payload,
        field_path="tui",
        model_type=_RuntimeTuiValidationModel,
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


def parse_runtime_plan_payload(raw_plan: object, *, source: str) -> RuntimePlanConfig | None:
    try:
        return _parse_plan_config(raw_plan)
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


def _parse_runtime_config_section[TConfig, TModel: BaseModel](
    raw_value: object,
    *,
    field_path: str,
    model_type: type[TModel],
) -> TConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    validated_model = _validate_runtime_config_model(model_type, cast(dict[str, object], raw_value))
    return cast(
        TConfig,
        cast(_RuntimeConfigOutput, validated_model).to_runtime_config(),
    )


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
