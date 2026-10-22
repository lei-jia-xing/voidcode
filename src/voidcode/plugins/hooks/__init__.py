"""Hooks plugin package: user argv hooks for the four surfaces the core offers."""

from .hook_config import RuntimeHooksConfig, hook_tool_matches
from .hook_executor import (
    HookExecutionEvent,
    HookExecutionOutcome,
    HookExecutionPolicy,
    HookExecutionRequest,
    HookPolicyDecision,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)
from .hooks_plugin import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_NOTE_CHARACTERS,
    RECURSION_ENV_VAR,
    ToolHooks,
    create_plugin,
    parse_hooks_config,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "HookExecutionEvent",
    "HookExecutionOutcome",
    "HookExecutionPolicy",
    "HookExecutionRequest",
    "HookPolicyDecision",
    "LifecycleHookExecutionRequest",
    "MAX_NOTE_CHARACTERS",
    "RECURSION_ENV_VAR",
    "RuntimeHooksConfig",
    "ToolHooks",
    "create_plugin",
    "hook_tool_matches",
    "parse_hooks_config",
    "run_lifecycle_hooks",
    "run_tool_hooks",
]
