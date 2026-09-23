from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal, Protocol

import jsonschema

from ..security.shell_policy import non_interactive_shell_env
from ..tools.contracts import ToolCall, ToolDefinition, ToolDiagnostics

ToolInputAction = Literal["unchanged", "rewrite", "block", "diagnostic"]
_MAX_HANDLER_NAMES = 32
_MAX_DIAGNOSTICS = 32
_MAX_ARGUMENT_KEYS = 64


@dataclass(frozen=True, slots=True)
class ToolInputEvent:
    """Read-only input exposed to a typed pre-tool handler."""

    session_id: str
    tool_call: ToolCall
    tool: ToolDefinition
    sequence: int
    session_status: str
    mode: str
    read_only: bool
    is_resume: bool = False


@dataclass(frozen=True, slots=True)
class UnchangedDecision:
    """Handler leaves the tool call untouched."""

    action: Literal["unchanged"] = "unchanged"


@dataclass(frozen=True, slots=True)
class RewriteDecision:
    """Handler replaces the tool arguments; the payload is part of the type."""

    arguments: Mapping[str, object]
    action: Literal["rewrite"] = "rewrite"

    def __post_init__(self) -> None:
        if not all(isinstance(key, str) and key for key in self.arguments):
            raise ValueError("tool input rewrite argument keys must be non-empty strings")
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass(frozen=True, slots=True)
class BlockDecision:
    """Handler blocks the tool call; the reason is part of the type."""

    reason: str
    action: Literal["block"] = "block"

    def __post_init__(self) -> None:
        if not _safe_text(self.reason):
            raise ValueError("tool input block must provide a reason")
        object.__setattr__(self, "reason", _safe_text(self.reason))


@dataclass(frozen=True, slots=True)
class DiagnosticDecision:
    """Handler reports a diagnostic without changing the call."""

    diagnostic: str
    action: Literal["diagnostic"] = "diagnostic"

    def __post_init__(self) -> None:
        if not _safe_text(self.diagnostic):
            raise ValueError("tool input diagnostic must provide diagnostic text")
        object.__setattr__(self, "diagnostic", _safe_text(self.diagnostic))


# The deliberately small result vocabulary for typed input handlers.
type ToolInputDecision = UnchangedDecision | RewriteDecision | BlockDecision | DiagnosticDecision


class ToolInputHandler(Protocol):
    """Callable contract for pure typed tool-input canonicalization/policy."""

    def __call__(self, event: ToolInputEvent, /) -> ToolInputDecision: ...


@dataclass(frozen=True, slots=True)
class ToolInputHandlerBinding:
    name: str
    handler: ToolInputHandler
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool input handler name must be non-empty")
        if isinstance(self.priority, bool):
            raise ValueError("tool input handler priority must be an integer")


@dataclass(frozen=True, slots=True)
class ToolInputHookOutcome:
    tool_call: ToolCall
    action: ToolInputAction = "unchanged"
    diagnostics: tuple[str, ...] = ()
    handler_names: tuple[str, ...] = ()
    blocked_reason: str | None = None


class ToolInputHandlerRegistry:
    """Stable, runtime-injected composition of typed pre-tool handlers."""

    def __init__(self, bindings: Iterable[ToolInputHandlerBinding] = ()) -> None:
        indexed = tuple(enumerate(bindings))
        names: set[str] = set()
        for _index, binding in indexed:
            if binding.name in names:
                raise ValueError(f"duplicate tool input handler name: {binding.name}")
            names.add(binding.name)
        # Explicit priority is the primary order; Python's stable sort keeps
        # registration order for equal priorities.
        self._bindings = tuple(binding for _index, binding in sorted(indexed, key=lambda item: item[1].priority))

    @property
    def bindings(self) -> tuple[ToolInputHandlerBinding, ...]:
        return self._bindings

    def apply(self, *, event: ToolInputEvent) -> ToolInputHookOutcome:
        original_arguments = deepcopy(dict(event.tool_call.arguments))
        current_call = replace(event.tool_call, arguments=deepcopy(original_arguments))
        diagnostics: list[str] = []
        names: list[str] = []
        changed = False

        for binding in self._bindings:
            if len(names) < _MAX_HANDLER_NAMES:
                names.append(binding.name)
            elif len(names) == _MAX_HANDLER_NAMES:
                names.append("[additional handlers omitted]")
            # Both call and definition are snapshots. A handler cannot mutate
            # the runtime's call or published schema through a nested dict.
            handler_call = replace(current_call, arguments=deepcopy(dict(current_call.arguments)))
            handler_tool = replace(event.tool, input_schema=deepcopy(event.tool.input_schema))
            current_event = replace(event, tool_call=handler_call, tool=handler_tool)
            try:
                decision = binding.handler(current_event)
            except Exception as exc:
                return ToolInputHookOutcome(
                    tool_call=current_call,
                    action="block",
                    diagnostics=tuple(diagnostics),
                    handler_names=tuple(names),
                    blocked_reason=_safe_text(f"tool input handler '{binding.name}' failed: {exc}"),
                )
            if decision.action == "diagnostic":
                if len(diagnostics) < _MAX_DIAGNOSTICS:
                    diagnostics.append(decision.diagnostic)
                elif len(diagnostics) == _MAX_DIAGNOSTICS:
                    diagnostics.append("[additional diagnostics omitted]")
                continue
            if decision.action == "unchanged":
                continue
            if decision.action == "rewrite":
                next_arguments = deepcopy(dict(decision.arguments))
                changed = changed or next_arguments != current_call.arguments
                current_call = replace(current_call, arguments=next_arguments)
                continue
            return ToolInputHookOutcome(
                tool_call=current_call,
                action="block",
                diagnostics=tuple(diagnostics),
                handler_names=tuple(names),
                blocked_reason=decision.reason,
            )

        action: ToolInputAction = "rewrite" if changed else ("diagnostic" if diagnostics else "unchanged")
        return ToolInputHookOutcome(
            tool_call=current_call,
            action=action,
            diagnostics=tuple(diagnostics),
            handler_names=tuple(names),
        )


def _shell_non_interactive_env_handler(event: ToolInputEvent, /) -> ToolInputDecision:
    if event.tool_call.tool_name != "shell_exec":
        return UnchangedDecision()
    command = event.tool_call.arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return UnchangedDecision()
    keys = tuple(non_interactive_shell_env(command))
    if not keys:
        return UnchangedDecision()
    return DiagnosticDecision(diagnostic=f"shell non-interactive env injected: {', '.join(keys)}")


def builtin_tool_input_handler_registry() -> ToolInputHandlerRegistry:
    """Return the production builtin registry (shell env policy as composed hook)."""
    # ponytail: diagnostic-only on purpose; Popen env merge stays in
    # ShellExecTool/hook executor (one line each). Add a typed env channel
    # only if a handler ever needs to change keys, not just announce them.
    return ToolInputHandlerRegistry((ToolInputHandlerBinding(name="shell-non-interactive-env", handler=_shell_non_interactive_env_handler),))


def validate_tool_input_schema(tool: ToolDefinition, arguments: Mapping[str, object]) -> None:
    """Apply the advertised schema gate without replacing tool self-validation."""

    raw_schema = dict(tool.input_schema)
    if not raw_schema:
        return
    if "type" in raw_schema or "properties" in raw_schema or "$schema" in raw_schema:
        schema = raw_schema
        schema.setdefault("type", "object")
    else:
        required = raw_schema.pop("required", None)
        schema: dict[str, object] = {
            "type": "object",
            "properties": raw_schema,
            "additionalProperties": True,
        }
        if isinstance(required, list) and all(isinstance(item, str) for item in required):
            schema["required"] = required
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(dict(arguments)),
        key=lambda error: tuple(error.path),
    )
    if errors:
        error = errors[0]
        location = error.json_path.removeprefix("$").lstrip(".") or "arguments"
        raise ValueError(f"{tool.name} input schema validation failed at {location}: {error.message}")


def tool_input_rewrite_metadata(*, original: ToolCall, outcome: ToolInputHookOutcome) -> dict[str, object]:
    """Build a bounded, non-authoritative rewrite trace for runtime events."""

    return {
        "original_sha256": _arguments_sha256(original.arguments),
        "final_sha256": _arguments_sha256(outcome.tool_call.arguments),
        "original_argument_keys": _bounded_keys(original.arguments),
        "final_argument_keys": _bounded_keys(outcome.tool_call.arguments),
        "handler_names": list(outcome.handler_names[:_MAX_HANDLER_NAMES]),
        "diagnostics": list(outcome.diagnostics[:_MAX_DIAGNOSTICS]),
        "action": outcome.action,
    }


def _arguments_sha256(arguments: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(dict(arguments), ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=repr).encode()
    except Exception:
        encoded = repr(sorted(arguments)).encode()
    return hashlib.sha256(encoded).hexdigest()


def tool_input_arguments_sha256(arguments: Mapping[str, object]) -> str:
    return _arguments_sha256(arguments)


def _bounded_keys(arguments: Mapping[str, object]) -> list[str]:
    keys = sorted(arguments)
    if len(keys) > _MAX_ARGUMENT_KEYS:
        return [*keys[:_MAX_ARGUMENT_KEYS], "[additional argument keys omitted]"]
    return keys


def _safe_text(value: object | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    diagnostic = ToolDiagnostics(summary=value)
    return diagnostic.summary or ""


__all__ = [
    "BlockDecision",
    "DiagnosticDecision",
    "RewriteDecision",
    "ToolInputAction",
    "ToolInputDecision",
    "ToolInputEvent",
    "ToolInputHandler",
    "ToolInputHandlerRegistry",
    "ToolInputHookOutcome",
    "UnchangedDecision",
    "builtin_tool_input_handler_registry",
    "tool_input_arguments_sha256",
    "tool_input_rewrite_metadata",
    "validate_tool_input_schema",
]
