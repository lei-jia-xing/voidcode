"""Hooks plugin: argv pre-tool gate, post-tool note and session lifecycle hooks.

The user's ``voidcode.json`` argv commands for the four surfaces the core
offers:

- ``pre_tool`` can only refuse: a ``cancel`` action, a non-zero exit, malformed
  stdout or a timeout denies the call, and the hook's own diagnostic is what the
  model reads instead of a result.
- ``post_tool`` can only append: the guidance a hook command prints becomes the
  note on the successful result the model reads. A broken note never fails a call
  that already succeeded.
- ``session_start`` / ``session_end`` run for their side effects only and
  are never injected into the prompt.
- Hook leases are read-only, so this plugin touches no state and stages no events.

Which tools a surface applies to is config-driven (``pre_tool_match`` /
``post_tool_match``); the registration matches ``("*",)`` because every tool
must reach the plugin to be filtered.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from voidcode.sdk.hooks import RunLifecycle, ToolIntent, ToolOutcome
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

from .hook_config import RuntimeHooksConfig
from .hook_executor import (
    HookExecutionOutcome,
    HookExecutionPolicy,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)

#: Legacy argv-hook recursion guard; set on every hook command's environment.
RECURSION_ENV_VAR = "VOIDCODE_RUNNING_TOOL_HOOK"
#: Default per-command timeout, matching the legacy ``hooks.timeout_seconds`` default.
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Bound on hook text handed back to the core.
MAX_NOTE_CHARACTERS = 2000
_TRUNCATION_SUFFIX = "[truncated]"
_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "enabled",
        "timeout_seconds",
        "pre_tool",
        "pre_tool_match",
        "post_tool",
        "post_tool_match",
        "session_start",
        "session_end",
        # The core injects the resolved providers map every factory receives; it is not ours.
        "providers",
    }
)


def _optional_bool(raw: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"hooks config field '{key}' must be a boolean")
    return value


def _timeout_seconds(raw: Mapping[str, object]) -> float:
    value = raw.get("timeout_seconds")
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("hooks config field 'timeout_seconds' must be a positive number")
    return float(value)


def _command_list(value: object, *, field: str) -> tuple[tuple[str, ...], ...]:
    """Parse ``[[argv...], ...]``: every command is a non-empty array of non-empty strings."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"hooks config field '{field}' must be an array of commands")
    commands: list[tuple[str, ...]] = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or not entry:
            raise ValueError(f"hooks config field '{field}' must contain non-empty string arrays")
        command: list[str] = []
        for item in entry:
            if not isinstance(item, str) or not item:
                raise ValueError(f"hooks config field '{field}' must contain non-empty string arrays")
            command.append(item)
        commands.append(tuple(command))
    return tuple(commands)


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"hooks config field '{field}' must be an array of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"hooks config field '{field}' must contain non-empty strings")
        items.append(item)
    return tuple(items)


def parse_hooks_config(config: Mapping[str, object] | None) -> RuntimeHooksConfig:
    """Build the ported argv-hook config for the available surfaces, rejecting everything else."""
    if config is None:
        return RuntimeHooksConfig()
    unknown = sorted(str(key) for key in config if key not in _CONFIG_KEYS)
    if unknown:
        raise ValueError(f"hooks config field '{unknown[0]}' is not supported")
    return RuntimeHooksConfig(
        enabled=_optional_bool(config, "enabled", default=True),
        timeout_seconds=_timeout_seconds(config),
        pre_tool=_command_list(config.get("pre_tool"), field="pre_tool"),
        pre_tool_match=_string_tuple(config.get("pre_tool_match"), field="pre_tool_match"),
        post_tool=_command_list(config.get("post_tool"), field="post_tool"),
        post_tool_match=_string_tuple(config.get("post_tool_match"), field="post_tool_match"),
        on_session_start=_command_list(config.get("session_start"), field="session_start"),
        on_session_end=_command_list(config.get("session_end"), field="session_end"),
    )


def _declared_texts(outcome: HookExecutionOutcome, *keys: str) -> list[str]:
    """Every non-empty ``keys`` string the hook commands declared, in execution order."""
    texts: list[str] = []
    for event in outcome.events:
        for key in keys:
            value = event.payload.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def _bounded(text: str) -> str | None:
    if not text:
        return None
    if len(text) > MAX_NOTE_CHARACTERS:
        # ponytail: plain prefix cap, per-item trimming if a hook ever needs the tail too.
        return text[:MAX_NOTE_CHARACTERS] + _TRUNCATION_SUFFIX
    return text


def _cancel_reason(outcome: HookExecutionOutcome, *, default: str) -> str:
    """First non-empty diagnostic/guidance/reason a cancelling hook command carried."""
    for key in ("diagnostic", "guidance", "reason"):
        texts = _declared_texts(outcome, key)
        if texts:
            return texts[0]
    return default


def _note(outcome: HookExecutionOutcome) -> str | None:
    """Guidance to append to a successful result; a cancelling hook's diagnostic otherwise."""
    texts = _declared_texts(outcome, "guidance")
    if not texts and outcome.action == "cancel":
        # An advisory surface keeps a cancel's diagnostic and guidance: hand it over as the note.
        texts = _declared_texts(outcome, "diagnostic")
    return _bounded("\n".join(texts))


def _lifecycle_note(outcome: HookExecutionOutcome) -> str | None:
    """A lifecycle note is the only record there is: guidance and diagnostics alike."""
    return _bounded("\n".join(_declared_texts(outcome, "guidance", "diagnostic")))


class ToolHooks:
    """The configured argv hook commands for the tool surfaces and the session lifecycle."""

    def __init__(self, config: RuntimeHooksConfig) -> None:
        self._config = config

    def pre_tool(self, intent: ToolIntent, context: ToolContext, services: PluginServices) -> str | None:
        _ = services  # the lease is read-only: this plugin reads nothing
        if not self._active(self._config.pre_tool):
            return None
        outcome = self._run(context, tool_name=intent.tool_name, phase="pre")
        if outcome.failed_error is not None:
            # Fail closed: a broken gate must never read as permission.
            return outcome.failed_error
        if outcome.action == "cancel":
            return f"tool '{intent.tool_name}' blocked: {_cancel_reason(outcome, default='cancelled by pre-tool hook')}"
        return None

    def post_tool(self, outcome: ToolOutcome, context: ToolContext, services: PluginServices) -> str | None:
        _ = services
        if not self._active(self._config.post_tool):
            return None
        execution = self._run(context, tool_name=outcome.tool_name, phase="post")
        if execution.failed_error is not None:
            # Fail open: a broken note must not fail a write that already succeeded.
            return None
        return _note(execution)

    def lifecycle(self, lifecycle: RunLifecycle, context: ToolContext, services: PluginServices) -> str | None:
        """Run the start/end argv commands for their side effects and report what they said."""
        _ = services
        commands = self._config.on_session_start if lifecycle.phase == "start" else self._config.on_session_end
        if not self._active(commands):
            return None
        outcome = self._run_lifecycle(context, lifecycle)
        if outcome.failed_error is not None:
            # Lifecycle surfaces are fail-open and the note is the only record: report the failure.
            return _bounded(outcome.failed_error)
        return _lifecycle_note(outcome)

    def _active(self, commands: tuple[tuple[str, ...], ...]) -> bool:
        """A surface with no enabled commands is a fast no-op, never a subprocess run."""
        return self._config.enabled is True and bool(commands)

    def _run(self, context: ToolContext, *, tool_name: str, phase: Literal["pre", "post"]) -> HookExecutionOutcome:
        return run_tool_hooks(
            HookExecutionRequest(
                hooks=self._config,
                workspace=context.workspace,
                session_id=context.session_id,
                tool_name=tool_name,
                phase=phase,
                recursion_env_var=RECURSION_ENV_VAR,
                environment=os.environ,
                policy=HookExecutionPolicy(),
            )
        )

    def _run_lifecycle(self, context: ToolContext, lifecycle: RunLifecycle) -> HookExecutionOutcome:
        payload: dict[str, object] = {
            "phase": lifecycle.phase,
            "run_id": lifecycle.run_id,
            "logical_turn": lifecycle.logical_turn,
        }
        if lifecycle.phase == "end":
            # Legacy ``session_end`` payload key, plus the failure a failed run reports.
            payload["session_status"] = lifecycle.status
            if lifecycle.error is not None:
                payload["error"] = lifecycle.error
        return run_lifecycle_hooks(
            LifecycleHookExecutionRequest(
                hooks=self._config,
                workspace=context.workspace,
                session_id=context.session_id,
                surface="session_start" if lifecycle.phase == "start" else "session_end",
                recursion_env_var=RECURSION_ENV_VAR,
                environment=os.environ,
                payload=payload,
                policy=HookExecutionPolicy(),
            )
        )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_NOTE_CHARACTERS",
    "RECURSION_ENV_VAR",
    "ToolHooks",
    "parse_hooks_config",
]
