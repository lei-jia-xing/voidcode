from __future__ import annotations

from ..formatter import RuntimeFormatterPresetConfig
from .config import RuntimeHooksConfig, RuntimeHookSurface
from .executor import (
    HookExecutionEvent,
    HookExecutionOutcome,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)
from .plan import (
    HOOK_PAYLOAD_SCHEMA,
    HOOK_PLAN_REVISION,
    HOOK_PLAN_SCHEMA_VERSION,
    HookPlanBinding,
    HookPlanValidationError,
    ResolvedHookPlan,
    hook_plan_from_session_metadata,
    materialize_hook_plan,
)
from .presets import (
    HookPreset,
    HookPresetKind,
    HookPresetRef,
    get_builtin_hook_preset,
    is_builtin_hook_preset_ref,
    list_builtin_hook_presets,
    validate_hook_preset_refs,
)
from .typed import (
    ToolInputAction,
    ToolInputDecision,
    ToolInputEvent,
    ToolInputHandler,
    ToolInputHandlerRegistry,
    ToolInputHookOutcome,
    tool_input_arguments_sha256,
    tool_input_rewrite_metadata,
    validate_tool_input_schema,
)

__all__ = [
    "HOOK_PAYLOAD_SCHEMA",
    "HOOK_PLAN_REVISION",
    "HOOK_PLAN_SCHEMA_VERSION",
    "HookExecutionEvent",
    "HookExecutionOutcome",
    "HookExecutionRequest",
    "HookPlanBinding",
    "HookPlanValidationError",
    "HookPreset",
    "HookPresetKind",
    "HookPresetRef",
    "LifecycleHookExecutionRequest",
    "ResolvedHookPlan",
    "RuntimeFormatterPresetConfig",
    "RuntimeHookSurface",
    "RuntimeHooksConfig",
    "ToolInputAction",
    "ToolInputDecision",
    "ToolInputEvent",
    "ToolInputHandler",
    "ToolInputHandlerRegistry",
    "ToolInputHookOutcome",
    "tool_input_arguments_sha256",
    "tool_input_rewrite_metadata",
    "validate_tool_input_schema",
    "get_builtin_hook_preset",
    "hook_plan_from_session_metadata",
    "is_builtin_hook_preset_ref",
    "list_builtin_hook_presets",
    "materialize_hook_plan",
    "run_lifecycle_hooks",
    "run_tool_hooks",
    "validate_hook_preset_refs",
]
