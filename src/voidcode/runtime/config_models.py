"""Single definition source for the runtime configuration data boundary.

Every configuration input surface the runtime accepts is described here as a
Pydantic payload model plus the raw-value validators those models share:

* the workspace ``.voidcode.json`` file (:class:`RuntimeConfigPayload`),
* the user-level config file (:class:`UserConfigPayload`),
* environment-derived settings (:class:`EnvironmentRuntimeSettings`),
* request-metadata overrides and the persisted session ``runtime_config``
  snapshot (the same section payloads, plus :class:`PersistedAgentPayload`).

``runtime/config_schema.py`` generates the shipped
``schema/voidcode.config.schema.json`` from these models, so the published
artifact cannot drift from the boundary it documents (see
``tests/unit/runtime/test_config_schema.py`` and
``scripts/generate_config_schema.py``).

Ownership note for maintainers: **these models own shape, not policy.** They
declare which keys exist, the type/range/enum each value may take, and they
coerce nothing the loader did not already coerce. They do not choose between
sources and they do not interpret values. Precedence
(environment -> user -> repo -> request -> persisted session), merge rules,
default-vs-unset resolution, which values are recovery-critical and persisted
into the session snapshot, plus provider and policy semantics, all stay in
their current owners: ``runtime/config.py``, ``runtime/config_materializer.py``,
``runtime/policy.py`` and ``provider/config.py``. If a rule picks between
sources or decides what a value means, it does not belong in this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..formatter import FormatterCwdPolicy
from ..hook.config import RuntimeHooksConfig
from ..mcp.builtin import get_builtin_mcp_descriptor
from ..provider.config import ProviderConfigsPayload
from ..provider.reasoning_effort import ALL_EFFORTS, normalize_reasoning_effort
from .permission import PermissionDecision
from .policy import runtime_policy_allowed_hook_scopes

#: The ``runtime_internal`` sub-object of a persisted agent payload.
AGENT_RUNTIME_INTERNAL_CONFIG_KEY = "runtime_internal"
#: Serializes environment mutation while the settings surface reads ``os.environ``.
ENV_SETTINGS_LOCK = Lock()

#: Every environment variable that feeds the environment settings surface.
TOP_LEVEL_ENV_VARS: tuple[str, ...] = (
    "VOIDCODE_APPROVAL_MODE",
    "VOIDCODE_MODEL",
    "VOIDCODE_EXECUTION_ENGINE",
    "VOIDCODE_TOOL_TIMEOUT_SECONDS",
    "VOIDCODE_REASONING_EFFORT",
)

APPROVAL_MODE_ENV_VAR = "VOIDCODE_APPROVAL_MODE"
MODEL_ENV_VAR = "VOIDCODE_MODEL"
EXECUTION_ENGINE_ENV_VAR = "VOIDCODE_EXECUTION_ENGINE"
TOOL_TIMEOUT_ENV_VAR = "VOIDCODE_TOOL_TIMEOUT_SECONDS"
REASONING_EFFORT_ENV_VAR = "VOIDCODE_REASONING_EFFORT"

VALID_APPROVAL_MODES: tuple[PermissionDecision, ...] = ("allow", "deny", "ask")
VALID_TUI_COMMANDS = ("command_palette", "session_new", "session_resume")
type TuiCommand = Literal["command_palette", "session_new", "session_resume"]

type ExecutionEngineName = Literal["deterministic", "provider"]
VALID_EXECUTION_ENGINES: tuple[ExecutionEngineName, ...] = ("deterministic", "provider")
type RuntimeProviderContextDiagnosticMode = Literal["off", "warn", "block"]
type RuntimeContextTransformFailureMode = Literal["ignore", "warn", "block"]
type RuntimeAgentPresetId = str
type RuntimeAgentPromptSource = Literal["builtin", "custom_markdown"]
type McpTransport = Literal["stdio", "remote-http"]
type RuntimeMcpServerScope = Literal["runtime", "session"]
type RuntimeTuiThemeMode = Literal["auto", "light", "dark"]
type RuntimeSummaryStrategy = Literal["deterministic", "model_assisted"]
#: Canonical reasoning-effort hint values, from the provider's own ladder. The
#: enum is schema metadata: ``parse_reasoning_effort`` owns the rejection and its
#: contract message, so it is declared here rather than as a Literal constraint.
type ReasoningEffort = Annotated[str, Field(json_schema_extra={"enum": list(ALL_EFFORTS)})]
type RuntimeHookFailureMode = Literal["warn", "fail"]
#: Policy hook event scopes, from the policy owner's own table.
type HookEventScope = Annotated[str, Field(json_schema_extra={"enum": list(runtime_policy_allowed_hook_scopes())})]

#: A hook/fallback command must be a non-empty argv array; the loader rejects an
#: empty command with its own message, so the inner minimum stays a contract
#: declaration rather than a second validation source.
type CommandList = tuple[Annotated[tuple[str, ...], Field(min_length=1)], ...]

#: A string item whose minimum length is published as metadata: the paired
#: validator owns rejection, the artifact only describes it.
type NonEmptyItem = Annotated[str, Field(json_schema_extra={"minLength": 1})]

VALID_TUI_THEME_MODES: tuple[RuntimeTuiThemeMode, ...] = ("auto", "light", "dark")

#: Agent/agent-map keys are lower-case ids; the same pattern covers manifest ids.
AGENT_PRESET_ID_PATTERN = r"^[a-z][a-z0-9_-]*$"
_AGENT_ID_RE = re.compile(AGENT_PRESET_ID_PATTERN)

#: Context-window schema version accepted by the loader.
CONTEXT_WINDOW_VERSION = 2
#: Top-level config schema version accepted by the loader. Additive-only:
#: absent (or explicit null) backfills to 1; anything else is rejected.
CONFIG_SCHEMA_VERSION = 1
DEFAULT_HOOK_TIMEOUT_SECONDS: float = cast(float, RuntimeHooksConfig().timeout_seconds)


# ---------------------------------------------------------------------------
# Raw-value validators shared by the payload models and the environment surface
# ---------------------------------------------------------------------------


def _format_runtime_config_field_error(field_path: str) -> str:
    runtime_field_prefix = "runtime config field '"
    if field_path.startswith(runtime_field_prefix):
        if field_path.endswith("'"):
            return field_path
        if "'[" in field_path:
            base, suffix = field_path[len(runtime_field_prefix) :].split("'[", maxsplit=1)
            return f"{runtime_field_prefix}{base}[{suffix}'"
    return f"runtime config field '{field_path}'"


def reject_unknown_config_keys(payload: Mapping[str, object], *, allowed_keys: frozenset[str], field_path: str) -> None:
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if not unknown_keys:
        return
    first_key = unknown_keys[0]
    full_path = f"{field_path}.{first_key}" if field_path else first_key
    raise ValueError(f"runtime config field '{full_path}' is not supported")


def config_model_keys(model_type: type[BaseModel]) -> frozenset[str]:
    """The input keys ``model_type`` accepts, derived from the model itself.

    Payload models are declared with the config-file key as the field name (or
    as its ``validation_alias``, e.g. ``$schema`` and ``opencode-go``), so the
    accepted key set needs no hand-maintained whitelist beside the model.
    """
    keys: set[str] = set()
    for name, model_field in model_type.model_fields.items():
        alias = model_field.validation_alias
        keys.add(alias if isinstance(alias, str) else name)
    return frozenset(keys)


def _validation_context_field_path(info: ValidationInfo, *, default: str) -> str:
    context = info.context
    if isinstance(context, dict):
        field_path = cast(dict[str, object], context).get("field_path")
        if isinstance(field_path, str):
            return field_path
    return default


def _config_field_source(info: ValidationInfo, field_name: str) -> str:
    """``runtime config field '<name>'`` plus the config file, when known.

    The workspace config path is part of the message contract for the top-level
    scalars (``... in /path/.voidcode.json``); callers that load the workspace
    file pass it through the validation context.
    """
    base = f"runtime config field '{field_name}'"
    context = info.context
    if isinstance(context, dict):
        config_file = cast(dict[str, object], context).get("config_file")
        if isinstance(config_file, str):
            return f"{base} in {config_file}"
    return base


def _parse_optional_bool(raw_value: object, *, field_path: str) -> bool | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be a boolean when provided")
    return raw_value


def _parse_non_null_optional_bool(raw_value: object, *, field_path: str) -> bool | None:
    """Optional boolean whose explicit ``null`` is rejected (key absent means unset)."""
    if not isinstance(raw_value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be a boolean when provided")
    return raw_value


def _parse_string_list(raw_value: object, *, field_path: str) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise ValueError(f"{_format_runtime_config_field_error(field_path)} must be an array when provided")

    raw_items = cast(list[object], raw_value)
    parsed_items: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, str):
            raise ValueError(f"{_format_runtime_config_field_error(f'{field_path}[{index}]')} must be a string")
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
            raise ValueError(f"runtime config field '{field_path}[{command_index}]' must be an array")
        command_field_path = f"{field_path}[{command_index}]"
        parsed_command = _parse_string_list(
            cast(list[object], raw_command),
            field_path=command_field_path,
        )
        if not parsed_command:
            raise ValueError(f"runtime config field '{command_field_path}' must contain at least one string")
        parsed_commands.append(parsed_command)
    return tuple(parsed_commands)


def _parse_optional_positive_int(value: object, *, field_path: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be an integer when provided")
    if value < 1:
        raise ValueError(f"runtime config field '{field_path}' must be greater than or equal to 1")
    return value


def _parse_non_null_positive_int(value: object, *, field_path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be an integer")
    if value < 1:
        raise ValueError(f"runtime config field '{field_path}' must be greater than or equal to 1")
    return value


def _parse_concurrency_limit(value: object, *, field_path: str) -> int:
    return _parse_non_null_positive_int(value, field_path=field_path)


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


def _parse_hook_timeout_seconds(raw_value: object, *, source: str) -> float:
    if raw_value is None:
        return DEFAULT_HOOK_TIMEOUT_SECONDS
    if not isinstance(raw_value, int | float) or isinstance(raw_value, bool) or raw_value < 1:
        raise ValueError(f"{source} must be a number greater than or equal to 1")
    return float(raw_value)


def _parse_formatter_cwd_policy(raw_value: object, *, field_path: str, default: FormatterCwdPolicy) -> FormatterCwdPolicy:
    if raw_value is None:
        return default
    if raw_value == "workspace":
        return "workspace"
    if raw_value == "nearest_root":
        return "nearest_root"
    if raw_value == "file_directory":
        return "file_directory"
    raise ValueError(f"runtime config field '{field_path}' must be one of: workspace, nearest_root, file_directory")


def _parse_permission_decision(value: object, *, source: str) -> PermissionDecision:
    if value == "allow":
        return "allow"
    if value == "deny":
        return "deny"
    if value == "ask":
        return "ask"
    allowed = ", ".join(VALID_APPROVAL_MODES)
    raise ValueError(f"{source} must be one of: {allowed}")


def _parse_execution_engine_name(value: object, *, source: str) -> ExecutionEngineName:
    if value == "deterministic":
        return "deterministic"
    if value == "provider":
        return "provider"
    allowed = ", ".join(VALID_EXECUTION_ENGINES)
    raise ValueError(f"{source} must be one of: {allowed}")


def parse_approval_mode(raw_value: object, *, source: str, allow_none: bool) -> PermissionDecision | None:
    if raw_value is None and allow_none:
        return None
    return _parse_permission_decision(raw_value, source=source)


def parse_execution_engine(raw_value: object, *, source: str, allow_none: bool) -> ExecutionEngineName | None:
    if raw_value is None and allow_none:
        return None
    return _parse_execution_engine_name(raw_value, source=source)


def parse_tool_timeout_seconds(raw_value: object, *, source: str, allow_none: bool) -> int | None:
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
            raise ValueError(f"environment variable {TOOL_TIMEOUT_ENV_VAR} must be an integer greater than or equal to 1") from exc
    return parse_tool_timeout_seconds(
        parsed_value,
        source=f"environment variable {TOOL_TIMEOUT_ENV_VAR}",
        allow_none=True,
    )


def parse_reasoning_effort(raw_value: object, *, allow_none: bool) -> str | None:
    if raw_value is None and allow_none:
        return None
    return normalize_reasoning_effort(raw_value)


def _parse_environment_reasoning_effort(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, str) and not raw_value:
        return None
    return parse_reasoning_effort(raw_value, allow_none=True)


def _parse_provider_context_diagnostic_mode(value: object) -> RuntimeProviderContextDiagnosticMode:
    if value is None:
        return "warn"
    if value == "off":
        return "off"
    if value == "warn":
        return "warn"
    if value == "block":
        return "block"
    raise ValueError("runtime config field 'context_window.provider_context_diagnostics' must be one of: off, warn, block")


def _parse_context_transform_failure_mode(value: object) -> RuntimeContextTransformFailureMode:
    if value is None:
        return "warn"
    if value == "ignore":
        return "ignore"
    if value == "warn":
        return "warn"
    if value == "block":
        return "block"
    raise ValueError("runtime config field 'context_window.context_transform_failure_policy' must be one of: ignore, warn, block")


def _parse_runtime_mcp_server_scope(value: object, *, field_path: str) -> RuntimeMcpServerScope:
    if value is None:
        return "runtime"
    if value == "runtime":
        return "runtime"
    if value == "session":
        return "session"
    raise ValueError(f"runtime config field '{field_path}.scope' must be one of: runtime, session")


def _parse_runtime_tui_theme_mode(value: object) -> RuntimeTuiThemeMode | None:
    if value is None:
        return None
    if value == "auto":
        return "auto"
    if value == "light":
        return "light"
    if value == "dark":
        return "dark"
    allowed = ", ".join(VALID_TUI_THEME_MODES)
    raise ValueError(f"runtime config field 'tui.preferences.theme.mode' must be one of: {allowed}")


def format_environment_validation_error(
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


def validate_config_model[T: BaseModel](
    model_type: type[T],
    raw_value: dict[str, object],
    *,
    context: dict[str, object] | None = None,
) -> T:
    """Validate a payload against its model, raising the loader's ``ValueError``."""
    try:
        return model_type.model_validate(raw_value, context=context)
    except ValidationError as exc:
        base_field_path = None
        if context is not None:
            raw_base_field_path = context.get("field_path")
            if isinstance(raw_base_field_path, str):
                base_field_path = raw_base_field_path
        message = format_environment_validation_error(exc, field_path=base_field_path)
        raise ValueError(message) from exc


def validate_config_section[T: BaseModel](
    model_type: type[T],
    raw_value: object,
    *,
    field_path: str,
    shape_message: str | None = None,
) -> T:
    """Validate a nested section object, keeping the absolute field path.

    ``shape_message`` overrides the wording of the "not an object" error for the
    call sites whose HEAD sentence had no ``when provided`` suffix.
    """
    if not isinstance(raw_value, dict):
        raise ValueError(shape_message or f"runtime config field '{field_path}' must be an object when provided")
    return validate_config_model(
        model_type,
        cast(dict[str, object], raw_value),
        context={"field_path": field_path},
    )


class _PayloadModel(BaseModel):
    """Base class for every config-file payload object: closed and shape-only."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Environment-derived settings
# ---------------------------------------------------------------------------


class EnvironmentRuntimeSettings(BaseSettings):
    """Environment input surface (``VOIDCODE_*`` variables)."""

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
    tool_timeout_seconds: int | None = Field(
        default=None,
        validation_alias=TOOL_TIMEOUT_ENV_VAR,
    )
    reasoning_effort: str | None = Field(
        default=None,
        validation_alias=REASONING_EFFORT_ENV_VAR,
    )

    @field_validator("approval_mode", mode="before")
    @classmethod
    def _validate_approval_mode(cls, value: object) -> PermissionDecision | None:
        return parse_approval_mode(
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
        return parse_execution_engine(
            value,
            source=f"environment variable {EXECUTION_ENGINE_ENV_VAR}",
            allow_none=True,
        )

    @field_validator("tool_timeout_seconds", mode="before")
    @classmethod
    def _validate_tool_timeout_seconds(cls, value: object) -> int | None:
        return _parse_environment_tool_timeout_seconds(value)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _validate_reasoning_effort(cls, value: object) -> str | None:
        return _parse_environment_reasoning_effort(value)


# ---------------------------------------------------------------------------
# permission
# ---------------------------------------------------------------------------


class PermissionRulePayload(_PayloadModel):
    tool: str = Field(
        default="*",
        min_length=1,
        description="Tool name glob, for example read, grep, or shell_exec.",
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        description="Workspace-relative or canonical path glob for filesystem-related tool calls.",
    )
    command: str | None = Field(
        default=None,
        min_length=1,
        description="Shell command glob used by shell_exec rules.",
    )
    decision: PermissionDecision

    @field_validator("tool", mode="before")
    @classmethod
    def _validate_tool(cls, value: object, info: ValidationInfo) -> str:
        field_path = _validation_context_field_path(info, default="permission.rules")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"runtime config field '{field_path}.tool' must be a non-empty string")
        return value

    @field_validator("path", "command", mode="before")
    @classmethod
    def _validate_optional_glob(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        field_path = _validation_context_field_path(info, default="permission.rules")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"runtime config field '{field_path}.{info.field_name}' must be a non-empty string")
        return value

    @model_validator(mode="before")
    @classmethod
    def _validate_decision(cls, payload: object, info: ValidationInfo) -> object:
        if not isinstance(payload, dict):
            return payload
        field_path = _validation_context_field_path(info, default="permission.rules")
        raw_payload = cast(dict[str, object], payload)
        if "decision" not in raw_payload:
            raise ValueError(f"runtime config field '{field_path}.decision' is required")
        parsed_decision = parse_approval_mode(
            raw_payload["decision"],
            source=f"runtime config field '{field_path}.decision'",
            allow_none=False,
        )
        assert parsed_decision is not None
        return {**raw_payload, "decision": parsed_decision}


class PermissionPayload(_PayloadModel):
    external_directory_read: dict[str, PermissionDecision] | None = Field(
        default_factory=dict,
        json_schema_extra={"propertyNames": {"minLength": 1}},
    )
    external_directory_write: dict[str, PermissionDecision] | None = Field(
        default_factory=dict,
        json_schema_extra={"propertyNames": {"minLength": 1}},
    )
    rules: tuple[PermissionRulePayload, ...] | None = Field(
        default=None,
        description=(
            "Ordered runtime permission rules for tool/path/command matches. "
            "First matching rule applies after hard tool allowlist and "
            "external-directory gates."
        ),
    )

    @field_validator("external_directory_read", "external_directory_write", mode="before")
    @classmethod
    def _validate_rule_map(cls, value: object, info: ValidationInfo) -> dict[str, PermissionDecision]:
        field_path = f"permission.{info.field_name}"
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field '{field_path}' must be an object when provided")
        parsed: dict[str, PermissionDecision] = {}
        for raw_pattern, raw_decision in cast(dict[object, object], value).items():
            if not isinstance(raw_pattern, str) or not raw_pattern.strip():
                raise ValueError(f"runtime config field '{field_path}' keys must be non-empty strings")
            parsed_decision = parse_approval_mode(
                raw_decision,
                source=f"runtime config field '{field_path}.{raw_pattern}'",
                allow_none=False,
            )
            assert parsed_decision is not None
            parsed[raw_pattern] = parsed_decision
        return parsed

    @field_validator("rules", mode="before")
    @classmethod
    def _validate_rules(cls, value: object) -> tuple[PermissionRulePayload, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ValueError("runtime config field 'permission.rules' must be an array when provided")
        parsed_rules: list[PermissionRulePayload] = []
        for index, raw_rule in enumerate(cast(list[object], value)):
            parsed_rules.append(
                validate_config_section(
                    PermissionRulePayload,
                    raw_rule,
                    field_path=f"permission.rules[{index}]",
                    shape_message=f"runtime config field 'permission.rules[{index}]' must be an object",
                )
            )
        return tuple(parsed_rules)


# ---------------------------------------------------------------------------
# policy (shape only: runtime/policy.py stays the semantic owner)
# ---------------------------------------------------------------------------


class PolicyToolPolicyPayload(_PayloadModel):
    allow: tuple[NonEmptyItem, ...] | None = None
    deny: tuple[NonEmptyItem, ...] | None = None
    default: str | None = Field(default=None, min_length=1)

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _validate_entries(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        return _parse_string_list(value, field_path=f"policy.tool_policy.{info.field_name}")


class PolicyDelegationPolicyPayload(_PayloadModel):
    allow: tuple[NonEmptyItem, ...] | None = None
    deny: tuple[NonEmptyItem, ...] | None = None
    default: str | None = Field(default=None, min_length=1)

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _validate_entries(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        return _parse_string_list(value, field_path=f"policy.delegation_policy.{info.field_name}")


class PolicyHookPolicyPayload(_PayloadModel):
    #: The scope enum comes from the policy table the loader validates against;
    #: ``runtime/policy.py`` still raises its own message first.
    allowed_event_scopes: tuple[HookEventScope, ...] | None = None
    #: Unknown actions are filtered by ``runtime/policy.py`` rather than rejected,
    #: so no enum is published for this key.
    actions: tuple[str, ...] | None = None

    @field_validator("allowed_event_scopes", "actions", mode="before")
    @classmethod
    def _validate_entries(cls, value: object, info: ValidationInfo) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _parse_string_list(value, field_path=f"policy.hook_policy.{info.field_name}")


class PolicyPromptActivationPayload(_PayloadModel):
    enabled: bool | None = None
    profile_refs: tuple[NonEmptyItem, ...] | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="policy.prompt_activation.enabled")

    @field_validator("profile_refs", mode="before")
    @classmethod
    def _validate_profile_refs(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _parse_string_list(value, field_path="policy.prompt_activation.profile_refs")


class PolicyPayload(_PayloadModel):
    #: ``version`` is required; the remaining keys reject an explicit ``null`` in
    #: the published contract (``runtime/policy.py`` requires the version and
    #: validates each section as an object), which the generator drops from the
    #: artifact -- see ``_NON_NULLABLE_POLICY_KEYS``.
    enabled: bool | None = None
    version: Literal["v1"]
    tool_policy: PolicyToolPolicyPayload | None = None
    delegation_policy: PolicyDelegationPolicyPayload | None = None
    hook_policy: PolicyHookPolicyPayload | None = None
    prompt_activation: PolicyPromptActivationPayload | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="policy.enabled")


# ---------------------------------------------------------------------------
# formatter and hooks
# ---------------------------------------------------------------------------


def validate_formatter_preset_map(
    raw_presets: object,
    *,
    field_path: str,
) -> dict[str, FormatterPresetPayload] | None:
    """Validate a ``{preset name: preset}`` map; the loader merges built-in presets."""
    if raw_presets is None:
        return None
    if not isinstance(raw_presets, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")
    parsed_presets: dict[str, FormatterPresetPayload] = {}
    for preset_name, raw_preset in cast(dict[object, object], raw_presets).items():
        if not isinstance(preset_name, str):
            raise ValueError(f"runtime config field '{field_path}' keys must be strings")
        parsed_presets[preset_name] = validate_config_section(
            FormatterPresetPayload,
            raw_preset,
            field_path=f"{field_path}.{preset_name}",
            shape_message=f"runtime config field '{field_path}.{preset_name}' must be an object",
        )
    return parsed_presets


# Shape-only preset: built-in preset defaults are merged by the loader.
# ``command``/``extensions`` require at least one entry only for presets that are
# not built-ins, so the minimum is declared on the artifact while the per-preset
# rule stays in ``voidcode.runtime.config``.
class FormatterPresetPayload(_PayloadModel):
    command: tuple[str, ...] | None = Field(default=None, json_schema_extra={"items": {"type": "string"}, "minItems": 1})
    #: No minimum: ``extensions`` may be empty for a builtin preset, which the
    #: loader fills from the builtin table (only a custom preset needs entries).
    extensions: tuple[str, ...] | None = None
    root_markers: tuple[str, ...] | None = None
    fallback_commands: CommandList | None = None
    cwd_policy: Literal["workspace", "nearest_root", "file_directory"] | None = None

    @field_validator("command", "extensions", "root_markers", mode="before")
    @classmethod
    def _validate_strings(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        field_path = _validation_context_field_path(info, default="formatter_presets")
        return _parse_string_list(value, field_path=f"{field_path}.{info.field_name}")

    @field_validator("fallback_commands", mode="before")
    @classmethod
    def _validate_commands(cls, value: object, info: ValidationInfo) -> tuple[tuple[str, ...], ...]:
        field_path = _validation_context_field_path(info, default="formatter_presets")
        return _parse_command_list(value, field_path=f"{field_path}.fallback_commands")

    @field_validator("cwd_policy", mode="before")
    @classmethod
    def _validate_cwd_policy(cls, value: object, info: ValidationInfo) -> str | None:
        field_path = _validation_context_field_path(info, default="formatter_presets")
        return _parse_formatter_cwd_policy(value, field_path=f"{field_path}.cwd_policy", default="nearest_root")


class FormatterPayload(_PayloadModel):
    enabled: bool | None = None
    format_on_write: bool | None = Field(
        default=None,
        description=(
            "Opt-in auto-format after edit/write. Off by default. "
            "'enabled' is kept as the existing alias and maps onto the "
            "same format-on-write switch (it no longer disables all hooks)."
        ),
    )
    languages: dict[str, FormatterPresetPayload] | None = None

    @field_validator("enabled", "format_on_write", mode="before")
    @classmethod
    def _validate_bool(cls, value: object, info: ValidationInfo) -> bool | None:
        return _parse_optional_bool(value, field_path=f"formatter.{info.field_name}")

    @field_validator("languages", mode="before")
    @classmethod
    def _validate_languages(cls, value: object) -> dict[str, FormatterPresetPayload]:
        return validate_formatter_preset_map(value, field_path="formatter.languages") or {}


#: Hook event command slots, in artifact order.
HOOK_COMMAND_FIELDS: tuple[str, ...] = (
    "pre_tool",
    "post_tool",
    "on_session_start",
    "on_session_end",
    "on_session_idle",
    "on_background_task_registered",
    "on_background_task_started",
    "on_background_task_progress",
    "on_background_task_completed",
    "on_background_task_failed",
    "on_background_task_cancelled",
    "on_background_task_interrupted",
    "on_background_task_notification_enqueued",
    "on_background_task_result_read",
    "on_delegated_result_available",
    "on_turn_progress",
    "on_stuck_detected",
    "on_approval_requested",
    "on_question_asked",
    "on_before_compact",
)


class HooksPayload(_PayloadModel):
    enabled: bool = Field(default=True)
    timeout_seconds: float | None = Field(default=DEFAULT_HOOK_TIMEOUT_SECONDS, ge=1)
    failure_mode: RuntimeHookFailureMode = "warn"
    pre_tool: CommandList | None = None
    pre_tool_match: tuple[str, ...] | None = None
    post_tool: CommandList | None = None
    post_tool_match: tuple[str, ...] | None = None
    on_session_start: CommandList | None = None
    on_session_end: CommandList | None = None
    on_session_idle: CommandList | None = None
    on_background_task_registered: CommandList | None = None
    on_background_task_started: CommandList | None = None
    on_background_task_progress: CommandList | None = None
    on_background_task_completed: CommandList | None = None
    on_background_task_failed: CommandList | None = None
    on_background_task_cancelled: CommandList | None = None
    on_background_task_interrupted: CommandList | None = None
    on_background_task_notification_enqueued: CommandList | None = None
    on_background_task_result_read: CommandList | None = None
    on_delegated_result_available: CommandList | None = None
    on_turn_progress: CommandList | None = None
    on_stuck_detected: CommandList | None = None
    on_approval_requested: CommandList | None = None
    on_question_asked: CommandList | None = None
    on_before_compact: CommandList | None = None
    formatter_presets: dict[str, FormatterPresetPayload] | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool:
        if value is None:
            raise ValueError("runtime config field 'hooks.enabled' must be a boolean when provided")
        parsed = _parse_optional_bool(value, field_path="hooks.enabled")
        assert parsed is not None
        return parsed

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _validate_timeout_seconds(cls, value: object) -> float:
        # ``null`` is normalised to the default, so the artifact accepts it too.
        return _parse_hook_timeout_seconds(value, source="runtime config field 'hooks.timeout_seconds'")

    @field_validator("failure_mode", mode="before")
    @classmethod
    def _validate_failure_mode(cls, value: object) -> RuntimeHookFailureMode:
        if value not in ("warn", "fail"):
            raise ValueError("runtime config field 'hooks.failure_mode' must be warn or fail")
        return cast(RuntimeHookFailureMode, value)

    @field_validator(*HOOK_COMMAND_FIELDS, mode="before")
    @classmethod
    def _validate_command_list(cls, value: object, info: ValidationInfo) -> tuple[tuple[str, ...], ...]:
        if value is None:
            return ()
        return _parse_command_list(value, field_path=f"hooks.{info.field_name}")

    @field_validator("formatter_presets", mode="before")
    @classmethod
    def _validate_formatter_presets(cls, value: object) -> dict[str, FormatterPresetPayload]:
        return validate_formatter_preset_map(value, field_path="hooks.formatter_presets") or {}

    @field_validator("pre_tool_match", "post_tool_match", mode="before")
    @classmethod
    def _validate_tool_match(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        patterns = _parse_string_list(value, field_path=f"hooks.{info.field_name}")
        # An empty glob matches nothing, which would silently disable the hook;
        # an omitted or empty list is the documented "match every tool" form.
        for index, pattern in enumerate(patterns):
            if not pattern.strip():
                raise ValueError(f"runtime config field 'hooks.{info.field_name}[{index}]' must be a non-empty string")
        return patterns


# ---------------------------------------------------------------------------
# tools, skills, context window
# ---------------------------------------------------------------------------


class ToolsBuiltinPayload(_PayloadModel):
    enabled: bool | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="tools.builtin.enabled")


class ToolsLocalPayload(_PayloadModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "description": (
                "Opt-in workspace-local custom tool manifest discovery. Runtime executes "
                "discovered command tools through the normal registry, allowlist, and "
                "permission path."
            )
        },
    )

    enabled: bool | None = None
    path: str | None = Field(
        default=".voidcode/tools",
        min_length=1,
        description="Workspace-relative directory containing *.json tool manifests.",
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object, info: ValidationInfo) -> bool | None:
        field_path = _validation_context_field_path(info, default="tools.local")
        return _parse_optional_bool(value, field_path=f"{field_path}.enabled")

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object, info: ValidationInfo) -> str:
        field_path = _validation_context_field_path(info, default="tools.local")
        if value is None:
            return ".voidcode/tools"
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"runtime config field '{field_path}.path' must be a non-empty string when provided")
        if Path(value).is_absolute():
            raise ValueError(f"runtime config field '{field_path}.path' must be workspace-relative")
        if ".." in Path(value).parts:
            raise ValueError(f"runtime config field '{field_path}.path' must not contain '..'")
        return value.strip()


class ToolsPayload(_PayloadModel):
    builtin: ToolsBuiltinPayload | None = None
    local: ToolsLocalPayload | None = None
    allowlist: tuple[str, ...] | None = None
    default: tuple[str, ...] | None = None
    essential_only: bool | None = Field(
        default=None,
        description=(
            "Essential/discoverable tool split: when true, only the essential "
            "tool set (plus allowlist-required tools) is sent top-level to the "
            "provider; the rest stay registered and reachable on demand."
        ),
    )

    @field_validator("builtin", mode="before")
    @classmethod
    def _validate_builtin_shape(cls, value: object) -> dict[str, object] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'tools.builtin' must be an object when provided")
        return cast(dict[str, object], value)

    @field_validator("local", mode="before")
    @classmethod
    def _validate_local_shape(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        field_path = _validation_context_field_path(info, default="tools")
        return validate_config_section(ToolsLocalPayload, value, field_path=f"{field_path}.local")

    @field_validator("allowlist", "default", mode="before")
    @classmethod
    def _validate_string_list(cls, value: object, info: ValidationInfo) -> tuple[str, ...] | None:
        if value is None:
            return None
        field_path = _validation_context_field_path(info, default="tools")
        return _parse_string_list(value, field_path=f"{field_path}.{info.field_name}")

    @field_validator("essential_only", mode="before")
    @classmethod
    def _validate_essential_only(cls, value: object, info: ValidationInfo) -> bool | None:
        if value is None:
            return None
        field_path = _validation_context_field_path(info, default="tools")
        return _parse_optional_bool(value, field_path=f"{field_path}.essential_only")


class AgentToolsPayload(_PayloadModel):
    builtin: ToolsBuiltinPayload | None = None
    allowlist: tuple[str, ...] | None = None
    default: tuple[str, ...] | None = None
    essential_only: bool | None = None

    @field_validator("builtin", mode="before")
    @classmethod
    def _validate_builtin_shape(cls, value: object) -> dict[str, object] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'agent.tools.builtin' must be an object when provided")
        return cast(dict[str, object], value)

    @field_validator("allowlist", "default", mode="before")
    @classmethod
    def _validate_string_list(cls, value: object, info: ValidationInfo) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _parse_string_list(value, field_path=f"agent.tools.{info.field_name}")

    @field_validator("essential_only", mode="before")
    @classmethod
    def _validate_essential_only(cls, value: object) -> bool | None:
        if value is None:
            return None
        return _parse_optional_bool(value, field_path="agent.tools.essential_only")


class SkillsPayload(_PayloadModel):
    enabled: bool | None = None
    paths: tuple[str, ...] | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="skills.enabled")

    @field_validator("paths", mode="before")
    @classmethod
    def _validate_paths(cls, value: object) -> tuple[str, ...]:
        return _parse_string_list(value, field_path="skills.paths")


class ContextWindowPayload(_PayloadModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    version: Literal[2] | None = 2
    default_tool_result_chars: int | None = Field(default=6_000, ge=1)
    per_tool_result_chars: dict[str, Annotated[int, Field(ge=1)]] | None = None
    provider_context_diagnostics: RuntimeProviderContextDiagnosticMode | None = Field(
        default="warn",
        description=(
            "Runtime policy for provider-context diagnostics before provider "
            "execution. 'warn' emits bounded metadata, 'block' fails selected "
            "high-severity diagnostics before the provider call, and 'off' keeps "
            "diagnostics debug-only."
        ),
    )
    provider_context_oversized_feedback_chars: int | None = Field(
        default=8_000,
        ge=1,
        description="Character threshold for oversized retained tool feedback diagnostics.",
    )
    context_transform_failure_policy: RuntimeContextTransformFailureMode | None = Field(
        default="warn",
        description=(
            "Policy for failed context transform providers. "
            "'ignore' keeps failures as debug metadata only, "
            "'warn' surfaces warning diagnostics without "
            "blocking provider execution, "
            "and 'block' turns transform failures into "
            "blocking provider-context diagnostics."
        ),
    )
    summary_strategy: RuntimeSummaryStrategy | None = "deterministic"

    @field_validator("version", mode="before")
    @classmethod
    def _validate_version(cls, value: object) -> int:
        if value is None:
            return CONTEXT_WINDOW_VERSION
        if value != CONTEXT_WINDOW_VERSION:
            raise ValueError(f"runtime config field 'context_window.version' must be {CONTEXT_WINDOW_VERSION}")
        return CONTEXT_WINDOW_VERSION

    @field_validator("default_tool_result_chars", mode="before")
    @classmethod
    def _validate_default_chars(cls, value: object, info: ValidationInfo) -> int | None:
        return _parse_optional_positive_int(value, field_path=f"context_window.{info.field_name}")

    @field_validator("provider_context_diagnostics", mode="before")
    @classmethod
    def _validate_provider_context_diagnostics(cls, value: object) -> RuntimeProviderContextDiagnosticMode:
        return _parse_provider_context_diagnostic_mode(value)

    @field_validator("provider_context_oversized_feedback_chars", mode="before")
    @classmethod
    def _validate_provider_context_oversized_feedback_chars(cls, value: object) -> int:
        if value is None:
            return 8_000
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("runtime config field 'context_window.provider_context_oversized_feedback_chars' must be greater than or equal to 1")
        return value

    @field_validator("context_transform_failure_policy", mode="before")
    @classmethod
    def _validate_context_transform_failure_policy(cls, value: object) -> RuntimeContextTransformFailureMode:
        return _parse_context_transform_failure_mode(value)

    @field_validator("per_tool_result_chars", mode="before")
    @classmethod
    def _validate_per_tool_result_chars(cls, value: object) -> dict[str, int]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'context_window.per_tool_result_chars' must be an object")
        parsed: dict[str, int] = {}
        for raw_key, raw_limit in cast(dict[object, object], value).items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError("runtime config field 'context_window.per_tool_result_chars' keys must be non-empty strings")
            limit = _parse_optional_positive_int(raw_limit, field_path=f"context_window.per_tool_result_chars.{raw_key}")
            assert limit is not None
            parsed[raw_key] = limit
        return parsed

    @field_validator("summary_strategy", mode="before")
    @classmethod
    def _validate_summary_strategy(cls, value: object) -> object:
        # ``None`` means unset; an invalid value keeps pydantic's Literal message,
        # which is the message HEAD produced for this field.
        return "deterministic" if value is None else value


# ---------------------------------------------------------------------------
# lsp and mcp
# ---------------------------------------------------------------------------


class LspServerPayload(_PayloadModel):
    preset: str | None = Field(default=None, min_length=1)
    command: tuple[str, ...] | None = None
    languages: tuple[str, ...] | None = None
    extensions: tuple[str, ...] | None = None
    root_markers: tuple[str, ...] | None = None
    settings: dict[str, object] | None = None
    init_options: dict[str, object] | None = None

    @field_validator("preset", mode="before")
    @classmethod
    def _validate_preset(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        field_path = _validation_context_field_path(info, default="lsp.servers")
        if not isinstance(value, str) or not value:
            raise ValueError(f"runtime config field '{field_path}.preset' must be a string")
        return value

    @field_validator("command", "languages", "extensions", "root_markers", mode="before")
    @classmethod
    def _validate_strings(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        if value is None:
            return ()
        field_path = _validation_context_field_path(info, default="lsp.servers")
        return _parse_string_list(value, field_path=f"{field_path}.{info.field_name}")

    @field_validator("settings", "init_options", mode="before")
    @classmethod
    def _validate_object(cls, value: object, info: ValidationInfo) -> dict[str, object]:
        if value is None:
            return {}
        field_path = _validation_context_field_path(info, default="lsp.servers")
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field '{field_path}.{info.field_name}' must be an object when provided")
        return cast(dict[str, object], value)


class LspPayload(_PayloadModel):
    enabled: bool | None = None
    diagnostics_on_write: bool | None = Field(
        default=False,
        description=("Opt-in automatic LSP diagnostics after edit/write. Off by default. Does not gate the explicit 'lsp' tool."),
    )
    servers: dict[str, LspServerPayload] | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="lsp.enabled")

    @field_validator("diagnostics_on_write", mode="before")
    @classmethod
    def _validate_diagnostics_on_write(cls, value: object) -> bool:
        parsed = _parse_optional_bool(value, field_path="lsp.diagnostics_on_write")
        return parsed if parsed is not None else False

    @field_validator("servers", mode="before")
    @classmethod
    def _validate_servers(cls, value: object) -> dict[str, LspServerPayload] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'lsp.servers' must be an object when provided")
        parsed_servers: dict[str, LspServerPayload] = {}
        for server_name, raw_server in cast(dict[object, object], value).items():
            if not isinstance(server_name, str):
                raise ValueError("runtime config field 'lsp.servers' keys must be strings")
            parsed_servers[server_name] = validate_config_section(
                LspServerPayload,
                raw_server,
                field_path=f"lsp.servers.{server_name}",
                shape_message=f"runtime config field 'lsp.servers.{server_name}' must be an object",
            )
        return parsed_servers


class McpServerPayload(_PayloadModel):
    # A stdio server needs an argv and a remote-http server needs a url, but the
    # rule cannot be published conditionally: a builtin server name may omit both
    # (the descriptor fills them in), so only the loader can decide.
    model_config = ConfigDict(extra="forbid", validate_default=True)

    transport: McpTransport | None = "stdio"
    command: tuple[str, ...] | None = Field(default=None, json_schema_extra={"items": {"type": "string"}, "minItems": 1})
    env: dict[str, str] | None = None
    scope: RuntimeMcpServerScope | None = Field(
        default="runtime",
        description=("Runtime-scoped servers are shared by the runtime; session-scoped servers are isolated per session."),
    )
    url: str | None = Field(
        default=None,
        min_length=1,
        json_schema_extra={"format": "uri"},
        description="Remote HTTP MCP endpoint URL. Required when transport is remote-http.",
    )

    @field_validator("transport", mode="before")
    @classmethod
    def _validate_transport(cls, value: object, info: ValidationInfo) -> McpTransport:
        if value is None:
            return "stdio"
        field_path = _validation_context_field_path(info, default="mcp.servers")
        if value not in ("stdio", "remote-http"):
            raise ValueError(f"runtime config field '{field_path}.transport' must be one of: stdio, remote-http")
        return cast(McpTransport, value)

    @field_validator("command", mode="before")
    @classmethod
    def _validate_command(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        field_path = _validation_context_field_path(info, default="mcp.servers")
        if value is None:
            return ()
        if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            return cast(tuple[str, ...], value)
        return _parse_string_list(value, field_path=f"{field_path}.command")

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env(cls, value: object, info: ValidationInfo) -> dict[str, str]:
        field_path = _validation_context_field_path(info, default="mcp.servers")
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field '{field_path}.env' must be an object")

        parsed_env: dict[str, str] = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError(f"runtime config field '{field_path}.env' keys must be strings")
            if not isinstance(item, str):
                raise ValueError(f"runtime config field '{field_path}.env.{key}' must be a string")
            parsed_env[key] = item
        return parsed_env

    @field_validator("scope", mode="before")
    @classmethod
    def _validate_scope(cls, value: object, info: ValidationInfo) -> RuntimeMcpServerScope:
        field_path = _validation_context_field_path(info, default="mcp.servers")
        return _parse_runtime_mcp_server_scope(value, field_path=field_path)

    @field_validator("url", mode="before")
    @classmethod
    def _validate_url(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            field_path = _validation_context_field_path(info, default="mcp.servers")
            raise ValueError(f"runtime config field '{field_path}.url' must be a non-empty string")
        return value.strip()

    def model_post_init(self, __context: object) -> None:
        """A transport that cannot start is a config error, not a runtime one.

        The message keeps the loader's historical wording (the field path was
        never threaded into this model, so the server name is not available here;
        the parent validator reports the missing-key variant with the real path).
        """
        if self.transport == "stdio" and not self.command:
            raise ValueError("MCP server 'unknown' using stdio transport requires a command")
        if self.transport == "remote-http" and not self.url:
            raise ValueError("MCP server 'unknown' using remote-http transport requires a url")


def _merge_builtin_mcp_server_defaults(server_name: str, raw_server: dict[str, object]) -> dict[str, object]:
    """Fill a builtin MCP server's defaults into its shorthand payload."""
    descriptor = get_builtin_mcp_descriptor(server_name)
    if descriptor is None:
        return dict(raw_server)
    merged = dict(raw_server)
    if "transport" not in merged:
        merged["transport"] = "stdio" if "command" in merged else descriptor.transport
    if descriptor.command and merged.get("transport") == "stdio":
        merged.setdefault("command", list(descriptor.command))
    if descriptor.url is not None and merged.get("transport") == "remote-http":
        merged.setdefault("url", descriptor.url)
    if descriptor.scope:
        merged.setdefault("scope", descriptor.scope)
    return merged


class McpPayload(_PayloadModel):
    enabled: bool | None = None
    servers: dict[str, McpServerPayload] | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("enabled", mode="before")
    @classmethod
    def _validate_enabled(cls, value: object) -> bool | None:
        return _parse_optional_bool(value, field_path="mcp.enabled")

    @field_validator("servers", mode="before")
    @classmethod
    def _validate_servers(cls, value: object) -> dict[str, McpServerPayload] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'mcp.servers' must be an object when provided")

        parsed_servers: dict[str, McpServerPayload] = {}
        for server_name, raw_server in cast(dict[object, object], value).items():
            if not isinstance(server_name, str):
                raise ValueError("runtime config field 'mcp.servers' keys must be strings")
            if not isinstance(raw_server, dict):
                raise ValueError(f"runtime config field 'mcp.servers.{server_name}' must be an object")
            field_path = f"mcp.servers.{server_name}"
            # A builtin server name may omit transport/command/url: the descriptor
            # supplies them, and the merge happens before the required-key checks
            # below so a shorthand entry validates like the fully written one.
            server_payload = _merge_builtin_mcp_server_defaults(
                server_name,
                cast(dict[str, object], raw_server),
            )
            transport = server_payload.get("transport")
            if transport is None:
                transport = "stdio"
            if transport == "stdio" and "command" not in server_payload:
                raise ValueError(f"runtime config field '{field_path}.command' is required when transport is stdio")
            if transport == "remote-http" and "url" not in server_payload:
                raise ValueError(f"runtime config field '{field_path}.url' is required when transport is remote-http")
            parsed_servers[server_name] = validate_config_section(McpServerPayload, server_payload, field_path=field_path)
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
            raise ValueError("runtime config field 'mcp.request_timeout_seconds' must be a finite number")
        if parsed <= 0:
            raise ValueError("runtime config field 'mcp.request_timeout_seconds' must be greater than 0")
        return parsed


# ---------------------------------------------------------------------------
# tui
# ---------------------------------------------------------------------------


class TuiThemePreferencesPayload(_PayloadModel):
    name: str | None = None
    mode: RuntimeTuiThemeMode | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("runtime config field 'tui.preferences.theme.name' must be a string when provided")
        return value

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object) -> RuntimeTuiThemeMode | None:
        return _parse_runtime_tui_theme_mode(value)


class TuiReadingPreferencesPayload(_PayloadModel):
    wrap: bool | None = None
    sidebar_collapsed: bool | None = None

    @field_validator("wrap", mode="before")
    @classmethod
    def _validate_wrap(cls, value: object) -> bool | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError("runtime config field 'tui.preferences.reading.wrap' must be a boolean when provided")
        return value

    @field_validator("sidebar_collapsed", mode="before")
    @classmethod
    def _validate_sidebar_collapsed(cls, value: object) -> bool | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError("runtime config field 'tui.preferences.reading.sidebar_collapsed' must be a boolean when provided")
        return value


class TuiPreferencesPayload(_PayloadModel):
    theme: TuiThemePreferencesPayload | None = None
    reading: TuiReadingPreferencesPayload | None = None

    @field_validator("theme", mode="before")
    @classmethod
    def _validate_theme(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'tui.preferences.theme' must be an object when provided")
        return cast(dict[str, object], value)

    @field_validator("reading", mode="before")
    @classmethod
    def _validate_reading(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'tui.preferences.reading' must be an object when provided")
        return cast(dict[str, object], value)


class TuiPayload(_PayloadModel):
    leader_key: str | None = None
    keymap: dict[str, TuiCommand] | None = None
    preferences: TuiPreferencesPayload | None = None

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
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError("runtime config field 'tui.keymap' keys must be strings")
            if not isinstance(item, str):
                raise ValueError("runtime config field 'tui.keymap' values must be strings")
            if item not in VALID_TUI_COMMANDS:
                allowed = ", ".join(VALID_TUI_COMMANDS)
                raise ValueError(f"runtime config field 'tui.keymap' values must be one of: {allowed}")
            parsed_keymap[key] = item
        return parsed_keymap

    @field_validator("preferences", mode="before")
    @classmethod
    def _validate_preferences(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'tui.preferences' must be an object when provided")
        return cast(dict[str, object], value)


# ---------------------------------------------------------------------------
# background tasks
# ---------------------------------------------------------------------------


class BackgroundTaskPayload(_PayloadModel):
    default_concurrency: int = Field(default=5, ge=1)
    provider_concurrency: dict[str, Annotated[int, Field(ge=1)]] | None = None
    model_concurrency: dict[str, Annotated[int, Field(ge=1)]] | None = None
    delegated_reminders_enabled: bool | None = True
    delegated_reminder_cooldown_seconds: int = Field(default=300, ge=1)

    @field_validator("default_concurrency", mode="before")
    @classmethod
    def _validate_default_concurrency(cls, value: object) -> int:
        return _parse_concurrency_limit(value, field_path="background_task.default_concurrency")

    @field_validator("provider_concurrency", "model_concurrency", mode="before")
    @classmethod
    def _validate_concurrency_map(cls, value: object, info: ValidationInfo) -> dict[str, int]:
        return _parse_concurrency_map(value, field_path=f"background_task.{info.field_name}")

    @field_validator("delegated_reminders_enabled", mode="before")
    @classmethod
    def _validate_delegated_reminders_enabled(cls, value: object) -> bool:
        if value is None:
            return True
        parsed = _parse_optional_bool(value, field_path="background_task.delegated_reminders_enabled")
        assert parsed is not None
        return parsed

    @field_validator("delegated_reminder_cooldown_seconds", mode="before")
    @classmethod
    def _validate_delegated_reminder_cooldown_seconds(cls, value: object) -> int:
        return _parse_concurrency_limit(value, field_path="background_task.delegated_reminder_cooldown_seconds")


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


class AgentRuntimeInternalPayload(_PayloadModel):
    """Runtime-owned agent provenance recorded in the persisted session snapshot."""

    prompt_materialization: dict[str, object] | None = None
    prompt_ref: str | None = None
    prompt_source: RuntimeAgentPromptSource | None = None
    manifest_source_scope: str | None = None
    manifest_source_path: str | None = None
    manifest_tool_allowlist: tuple[str, ...] = ()
    manifest_skill_refs: tuple[str, ...] = ()
    manifest_hook_refs: tuple[str, ...] = ()

    @field_validator("prompt_materialization", mode="before")
    @classmethod
    def _validate_prompt_materialization(cls, value: object) -> dict[str, object] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'agent.runtime_internal.prompt_materialization' must be an object when provided")
        return cast(dict[str, object], value)

    @field_validator("manifest_tool_allowlist", "manifest_skill_refs", "manifest_hook_refs", mode="before")
    @classmethod
    def _validate_string_list(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        if value is None:
            return ()
        return _parse_string_list(value, field_path=f"agent.runtime_internal.{info.field_name}")


class AgentMcpBindingPayload(_PayloadModel):
    #: Declarative MCP profile/server binding intent for an agent. Runtime MCP
    #: config, lifecycle, approval, and tool allowlists remain authoritative.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "description": (
                "Declarative MCP profile/server binding intent for this agent. Runtime "
                "MCP config, lifecycle, approval, and tool allowlists remain authoritative."
            )
        },
    )

    profile: str | None = Field(default=None, min_length=1)
    servers: tuple[NonEmptyItem, ...] | None = None

    @field_validator("profile", mode="before")
    @classmethod
    def _validate_profile(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("runtime config field 'agent.mcp_binding.profile' must be a non-empty string")
        return value.strip()

    @field_validator("servers", mode="before")
    @classmethod
    def _validate_servers(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        return _parse_string_list(value, field_path="agent.mcp_binding.servers")


class AgentPayload(_PayloadModel):
    #: Preset validity is registry-dependent, so the value stays untyped here and
    #: ``_agent_config_from_payload`` keeps raising the loader's preset message.
    preset: object = Field(
        default=None,
        json_schema_extra={"type": ["string", "null"], "pattern": AGENT_PRESET_ID_PATTERN},
    )
    prompt_profile: str | None = Field(
        default=None,
        json_schema_extra={"minLength": 1},
        description="Prompt/profile selection for this agent entry.",
    )
    prompt: str | None = Field(
        default=None,
        json_schema_extra={"minLength": 1},
        description="Explicit prompt/profile text for this agent config entry.",
    )
    prompt_append: str | None = Field(
        default=None,
        json_schema_extra={"minLength": 1},
        description="Additional local guidance appended to the resolved base prompt.",
    )
    hook_refs: tuple[str, ...] | None = None
    context_transform_refs: tuple[str, ...] | None = None
    model: str | None = Field(default=None, json_schema_extra={"minLength": 1})
    tools: AgentToolsPayload | None = None
    skills: SkillsPayload | None = None
    mcp_binding: AgentMcpBindingPayload | None = None
    #: Untyped on purpose (see ``RuntimeConfigPayload.fallback_models``).
    fallback_models: object = Field(
        default=None,
        json_schema_extra={"type": ["array", "null"], "items": {"type": "string"}},
        description="Agent-scoped fallback model chain; requires agent.model.",
    )

    @field_validator("preset", mode="before")
    @classmethod
    def _validate_preset(cls, value: object) -> object:
        return value

    @field_validator("prompt_profile", "prompt", "prompt_append", "model", mode="before")
    @classmethod
    def _validate_optional_text(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        field_path = _validation_context_field_path(info, default="agent")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"runtime config field '{field_path}.{info.field_name}' must be a non-empty string")
        return value.strip()

    @field_validator("hook_refs", "context_transform_refs", mode="before")
    @classmethod
    def _validate_ref_list(cls, value: object, info: ValidationInfo) -> tuple[str, ...]:
        if value is None:
            return ()
        # the path comes from the validation context, so an ``agents.<key>`` entry
        # reports under its own key (HEAD's contract)
        field_path = _validation_context_field_path(info, default="agent")
        return _parse_string_list(value, field_path=f"{field_path}.{info.field_name}")

    @field_validator("tools", "skills", "mcp_binding", mode="before")
    @classmethod
    def _validate_section_shape(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        field_path = _validation_context_field_path(info, default="agent")
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field '{field_path}.{info.field_name}' must be an object when provided")
        return cast(dict[str, object], value)


class PersistedAgentPayload(AgentPayload):
    """Agent payload of the persisted session snapshot (adds runtime-owned keys)."""

    execution_engine: ExecutionEngineName | None = None
    runtime_internal: AgentRuntimeInternalPayload | None = None

    @field_validator("runtime_internal", mode="before")
    @classmethod
    def _validate_runtime_internal(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'agent.runtime_internal' must be an object when provided")
        return cast(dict[str, object], value)

    @field_validator("execution_engine", mode="before")
    @classmethod
    def _validate_execution_engine(cls, value: object) -> ExecutionEngineName | None:
        return parse_execution_engine(
            value,
            source="runtime config field 'agent.execution_engine'",
            allow_none=True,
        )


# ---------------------------------------------------------------------------
# top-level surfaces
# ---------------------------------------------------------------------------


#: Object-valued sections of the workspace config file.
SECTION_FIELDS: tuple[str, ...] = (
    "permission",
    "policy",
    "hooks",
    "formatter",
    "tools",
    "skills",
    "context_window",
    "lsp",
    "mcp",
    "tui",
    "providers",
    "background_task",
    "agent",
)


class RuntimeConfigPayload(_PayloadModel):
    """The workspace ``.voidcode.json`` input surface."""

    schema_ref: str | None = Field(
        default=None,
        alias="$schema",
        description="JSON Schema reference for editor support.",
    )
    config_schema_version: Literal[1] | None = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="Top-level config schema version. Currently only 1 is supported; omit to default to 1.",
    )
    approval_mode: PermissionDecision | None = Field(
        default=None,
        description="Default approval policy for tool execution.",
    )
    permission: PermissionPayload | None = None
    policy: PolicyPayload | None = None
    model: str | None = Field(
        default=None,
        description="Provider/model identifier in `provider/model` form.",
    )
    execution_engine: ExecutionEngineName | None = Field(
        default=None,
        description="Execution engine used when no request or environment override is set.",
    )
    # Item shape is published as contract metadata only; provider/config.py owns
    # the chain's rejection and messages.
    fallback_models: tuple[Annotated[object, Field(json_schema_extra={"type": "string"})], ...] | None = Field(
        default=None,
        description="Ordered fallback models tried after the primary model.",
    )
    tool_timeout_seconds: int | None = Field(default=None, ge=1, description="Timeout applied to each tool execution.")
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description=(
            "Optional runtime-owned reasoning-effort hint forwarded to the active "
            "provider when supported (for example, 'low', 'medium', 'high'). Runtime "
            "rejects this hint when the resolved model explicitly does not support "
            "reasoning effort."
        ),
    )
    hooks: HooksPayload | None = Field(
        default=None,
        description="Runtime-managed lifecycle hooks (pre/post tool, session, background).",
    )
    formatter: FormatterPayload | None = Field(
        default=None,
        description="Formatting behavior exposed as a top-level user-facing capability.",
    )
    tools: ToolsPayload | None = None
    skills: SkillsPayload | None = None
    context_window: ContextWindowPayload | None = None
    lsp: LspPayload | None = None
    mcp: McpPayload | None = None
    tui: TuiPayload | None = None
    providers: ProviderConfigsPayload | None = Field(
        default=None,
        description="Provider-level configuration. Credential fields are sensitive; prefer environment variables for secrets.",
    )
    background_task: BackgroundTaskPayload | None = Field(
        default=None,
        description="Background task queue and concurrency limits.",
    )
    agent: AgentPayload | None = None
    agents: dict[str, AgentPayload] | None = Field(
        default=None,
        json_schema_extra={"propertyNames": {"pattern": AGENT_PRESET_ID_PATTERN}},
    )

    @field_validator("config_schema_version", mode="before")
    @classmethod
    def _validate_config_schema_version(cls, value: object) -> int:
        if value is None:
            return CONFIG_SCHEMA_VERSION
        if isinstance(value, bool) or not isinstance(value, int) or value != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"runtime config field 'config_schema_version' must be {CONFIG_SCHEMA_VERSION}")
        return CONFIG_SCHEMA_VERSION

    @field_validator("schema_ref", mode="before")
    @classmethod
    def _validate_schema_ref(cls, value: object) -> str | None:
        # ``$schema`` is an editor hint the loader never reads; HEAD ignored any
        # value here, so a non-string is ignored rather than rejected.
        return value if isinstance(value, str) else None

    @field_validator(*SECTION_FIELDS, mode="before")
    @classmethod
    def _validate_section_shape(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field '{info.field_name}' must be an object when provided")
        return value

    @field_validator("agents", mode="before")
    @classmethod
    def _validate_agents_shape(cls, value: object) -> dict[str, AgentPayload] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'agents' must be an object when provided")
        parsed: dict[str, AgentPayload] = {}
        for raw_key, entry in cast(dict[object, object], value).items():
            if not isinstance(raw_key, str):
                raise ValueError("runtime config field 'agents' keys must be strings")
            # A per-entry context path makes a defect report under the entry key
            # (``agents.worker.model``), matching the loader's contract.
            parsed[raw_key] = validate_config_section(AgentPayload, entry, field_path=f"agents.{raw_key}")
        return parsed

    @field_validator("approval_mode", mode="before")
    @classmethod
    def _validate_approval_mode(cls, value: object, info: ValidationInfo) -> PermissionDecision | None:
        return parse_approval_mode(value, source=_config_field_source(info, "approval_mode"), allow_none=True)

    @field_validator("execution_engine", mode="before")
    @classmethod
    def _validate_execution_engine(cls, value: object, info: ValidationInfo) -> ExecutionEngineName | None:
        return parse_execution_engine(value, source=_config_field_source(info, "execution_engine"), allow_none=True)

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("runtime config field 'model' must be a string when provided")
        return value

    @field_validator("tool_timeout_seconds", mode="before")
    @classmethod
    def _validate_tool_timeout_seconds(cls, value: object, info: ValidationInfo) -> int | None:
        return parse_tool_timeout_seconds(value, source=_config_field_source(info, "tool_timeout_seconds"), allow_none=True)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _validate_reasoning_effort(cls, value: object) -> str | None:
        return parse_reasoning_effort(value, allow_none=True)


class UserConfigPayload(_PayloadModel):
    """The user-level ``config.json`` input surface."""

    schema_ref: str | None = Field(default=None, alias="$schema")
    tui: TuiPayload | None = None
    #: ``web`` is read opaquely by the web-settings surface today (``load_global_web_settings``),
    #: so it stays untyped here: any value is accepted exactly as HEAD accepted it.
    web: object | None = None
    providers: ProviderConfigsPayload | None = None
    #: User-global hook commands, concatenated before repo-local commands per surface.
    hooks: HooksPayload | None = None

    @field_validator("tui", "providers", mode="before")
    @classmethod
    def _validate_user_section_shape(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field '{info.field_name}' must be an object when provided")
        return value

    @field_validator("hooks", mode="before")
    @classmethod
    def _validate_user_hooks_shape(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("runtime config field 'hooks' must be an object when provided")
        return value

    @field_validator("schema_ref", mode="before")
    @classmethod
    def _validate_schema_ref(cls, value: object) -> str | None:
        # ``$schema`` is an editor hint the loader never reads; HEAD ignored any
        # value here, so a non-string is ignored rather than rejected.
        return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# published JSON Schema definition names
# ---------------------------------------------------------------------------

#: Generated definition key (payload class name) -> published ``$defs`` name.
#: The names are part of the shipped artifact's external contract, so they are
#: declared explicitly instead of following the class names.
SCHEMA_DEFINITION_NAMES: Mapping[str, str] = {
    # value-type aliases shared by several properties
    "PermissionDecision": "permissionDecision",
    "ExecutionEngineName": "executionEngine",
    "RuntimeProviderContextDiagnosticMode": "providerContextDiagnosticMode",
    "RuntimeContextTransformFailureMode": "contextTransformFailureMode",
    "RuntimeSummaryStrategy": "summaryStrategy",
    "RuntimeHookFailureMode": "hookFailureMode",
    "McpTransport": "mcpTransport",
    "RuntimeMcpServerScope": "mcpServerScope",
    "RuntimeTuiThemeMode": "tuiThemeMode",
    "CommandList": "commandList",
    "TuiCommand": "tuiCommand",
    "HookEventScope": "hookEventScope",
    "NonEmptyItem": "nonEmptyItem",
    "ReasoningEffort": "reasoningEffort",
    # runtime-owned payload models
    "PermissionPayload": "permissionConfig",
    "PermissionRulePayload": "patternPermissionRule",
    "PolicyPayload": "runtimePolicyConfig",
    "PolicyToolPolicyPayload": "runtimePolicyToolPolicyConfig",
    "PolicyDelegationPolicyPayload": "runtimePolicyDelegationPolicyConfig",
    "PolicyHookPolicyPayload": "runtimePolicyHookPolicyConfig",
    "PolicyPromptActivationPayload": "runtimePolicyPromptActivationConfig",
    "HooksPayload": "hooksConfig",
    "FormatterPayload": "formatterConfig",
    "FormatterPresetPayload": "formatterPresetConfig",
    "ToolsPayload": "runtimeToolsConfig",
    "AgentToolsPayload": "agentToolsConfig",
    "ToolsBuiltinPayload": "toolsBuiltinConfig",
    "ToolsLocalPayload": "localToolsConfig",
    "SkillsPayload": "skillsConfig",
    "ContextWindowPayload": "contextWindowConfig",
    "LspPayload": "lspConfig",
    "LspServerPayload": "lspServerConfig",
    "McpPayload": "mcpConfig",
    "McpServerPayload": "mcpServerConfig",
    "TuiPayload": "tuiConfig",
    "TuiPreferencesPayload": "tuiPreferencesConfig",
    "TuiThemePreferencesPayload": "tuiThemePreferencesConfig",
    "TuiReadingPreferencesPayload": "tuiReadingPreferencesConfig",
    "BackgroundTaskPayload": "backgroundTaskConfig",
    "AgentPayload": "agentConfig",
    "AgentMcpBindingPayload": "agentMcpBindingConfig",
    "ProviderConfigsPayload": "providersConfig",
    "_ProviderTransientRetryConfigPayload": "providerTransientRetryConfig",
    "_OpenAIProviderConfigPayload": "openaiProviderConfig",
    "_AnthropicProviderConfigPayload": "anthropicProviderConfig",
    "_GoogleProviderAuthConfigPayload": "googleProviderAuthConfig",
    "_GoogleProviderConfigPayload": "googleProviderConfig",
    "_CopilotProviderAuthConfigPayload": "copilotProviderAuthConfig",
    "_CopilotProviderConfigPayload": "copilotProviderConfig",
    "_ProviderEndpointConfigPayload": "endpointProviderConfig",
    "_OpenAICompatibleProviderConfigPayload": "openAICompatibleProviderConfig",
}

#: Shapes folded back into a shared ``$defs`` entry so the artifact does not
#: repeat one identical object in 17 hook slots (see ``config_schema``).
SHARED_SCHEMA_DEFINITIONS: Mapping[str, tuple[object, str | None]] = {
    "commandList": (
        {"items": {"items": {"type": "string"}, "minItems": 1, "type": "array"}, "type": "array"},
        "Array of commands; each command is a non-empty argv array of strings. Commands are executed directly without an implicit shell.",
    ),
    "permissionRules": (
        {
            "additionalProperties": {"enum": ["allow", "deny", "ask"], "type": "string"},
            "propertyNames": {"minLength": 1},
            "type": "object",
        },
        "Ordered path-glob permission map. First matching pattern applies.",
    ),
}
