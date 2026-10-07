from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal, Protocol

import jsonschema

from ..security.json_values import json_wire_object
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
class ToolInputHandlerDeclaration:
    name: str
    version: str
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool input handler name must be non-empty")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("tool input handler version must be non-empty")
        if type(self.priority) is not int:
            raise ValueError("tool input handler priority must be an integer")

    def to_payload(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version, "priority": self.priority}


@dataclass(frozen=True, slots=True)
class ToolInputHandlerBinding:
    declaration: ToolInputHandlerDeclaration
    handler: ToolInputHandler

    def __post_init__(self) -> None:
        if not isinstance(self.declaration, ToolInputHandlerDeclaration) or not callable(self.handler):
            raise ValueError("tool input binding requires a declaration and callable handler")


@dataclass(frozen=True, slots=True)
class ToolInputHookOutcome:
    tool_call: ToolCall
    action: ToolInputAction = "unchanged"
    diagnostics: tuple[str, ...] = ()
    handlers: tuple[ToolInputHandlerDeclaration, ...] = ()
    omitted_handler_count: int = 0
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if len(self.handlers) > _MAX_HANDLER_NAMES or any(not isinstance(handler, ToolInputHandlerDeclaration) for handler in self.handlers):
            raise ValueError("input outcome requires at most 32 real handler declarations")
        if type(self.omitted_handler_count) is not int or self.omitted_handler_count < 0:
            raise ValueError("input outcome omitted_handler_count must be a nonnegative integer")

    def metadata_payload(self) -> dict[str, object]:
        return {
            "version": 2,
            "action": self.action,
            "handlers": [handler.to_payload() for handler in self.handlers],
            "omitted_handler_count": self.omitted_handler_count,
            "diagnostics": list(self.diagnostics),
            **({"reason": self.blocked_reason} if self.blocked_reason is not None else {}),
        }


class ToolInputHandlerRegistry:
    """One declaration catalogue with genuinely bound pre-tool handlers."""

    def __init__(
        self,
        bindings: Iterable[ToolInputHandlerBinding] = (),
        *,
        declarations: Iterable[ToolInputHandlerDeclaration] | None = None,
    ) -> None:
        bound = tuple(bindings)
        declared = tuple(declarations) if declarations is not None else tuple(binding.declaration for binding in bound)
        by_name: dict[str, ToolInputHandlerDeclaration] = {}
        for declaration in declared:
            if type(declaration) is not ToolInputHandlerDeclaration:
                raise ValueError("input declarations must be pure ToolInputHandlerDeclaration records")
            if declaration.name in by_name:
                raise ValueError(f"duplicate tool input handler name: {declaration.name}")
            by_name[declaration.name] = declaration
        bound_by_name: dict[str, ToolInputHandlerBinding] = {}
        for binding in bound:
            name = binding.declaration.name
            if name in bound_by_name or by_name.get(name) != binding.declaration:
                raise ValueError(f"tool input binding does not match unique declaration: {name}")
            bound_by_name[name] = binding
        self._declarations = MappingProxyType(by_name)
        # Stable ties preserve the declaration registration order.
        self._bindings = tuple(
            bound_by_name[declaration.name] for declaration in sorted(declared, key=lambda item: item.priority) if declaration.name in bound_by_name
        )

    @classmethod
    def from_declarations(cls, declarations: Iterable[ToolInputHandlerDeclaration]) -> ToolInputHandlerRegistry:
        return cls(declarations=declarations)

    @property
    def declarations(self) -> Mapping[str, ToolInputHandlerDeclaration]:
        return self._declarations

    @property
    def bindings(self) -> tuple[ToolInputHandlerBinding, ...]:
        return self._bindings

    def bind(self, materialize: Callable[[str], ToolInputHandlerBinding]) -> ToolInputHandlerRegistry:
        if len(self._bindings) == len(self._declarations):
            return self
        existing = {binding.declaration.name: binding for binding in self._bindings}
        bound: list[ToolInputHandlerBinding] = []
        for name, declaration in self._declarations.items():
            binding = existing[name] if name in existing else materialize(name)
            if not isinstance(binding, ToolInputHandlerBinding) or binding.declaration != declaration:
                raise ValueError(f"materialized input handler does not match declaration: {name}")
            bound.append(binding)
        return ToolInputHandlerRegistry(bound, declarations=self._declarations.values())

    def apply(self, *, event: ToolInputEvent) -> ToolInputHookOutcome:
        if len(self._bindings) != len(self._declarations):
            raise RuntimeError("tool input handlers must be bound after activation before dispatch")
        original_arguments = json_wire_object(event.tool_call.arguments)
        current_call = replace(event.tool_call, arguments=original_arguments)
        diagnostics: list[str] = []
        handlers: list[ToolInputHandlerDeclaration] = []
        omitted_handler_count = 0
        changed = False

        for binding in self._bindings:
            if len(handlers) < _MAX_HANDLER_NAMES:
                handlers.append(binding.declaration)
            else:
                omitted_handler_count += 1
            # Both call and definition are snapshots. A handler cannot mutate
            # the runtime's call or published schema through a nested dict.
            handler_call = replace(current_call, arguments=json_wire_object(current_call.arguments))
            handler_tool = replace(event.tool, input_schema=json_wire_object(event.tool.input_schema))
            current_event = replace(event, tool_call=handler_call, tool=handler_tool)
            try:
                decision = binding.handler(current_event)
                if not isinstance(decision, UnchangedDecision | RewriteDecision | BlockDecision | DiagnosticDecision):
                    raise ValueError("tool input handler returned an invalid decision")
                next_arguments = json_wire_object(decision.arguments) if isinstance(decision, RewriteDecision) else None
            except Exception as exc:
                return ToolInputHookOutcome(
                    tool_call=current_call,
                    action="block",
                    diagnostics=tuple(diagnostics),
                    handlers=tuple(handlers),
                    omitted_handler_count=omitted_handler_count,
                    blocked_reason=_safe_text(f"tool input handler '{binding.declaration.name}' failed: {exc}"),
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
                assert next_arguments is not None
                changed = changed or next_arguments != current_call.arguments
                current_call = replace(current_call, arguments=next_arguments)
                continue
            return ToolInputHookOutcome(
                tool_call=current_call,
                action="block",
                diagnostics=tuple(diagnostics),
                handlers=tuple(handlers),
                omitted_handler_count=omitted_handler_count,
                blocked_reason=decision.reason,
            )

        action: ToolInputAction = "rewrite" if changed else ("diagnostic" if diagnostics else "unchanged")
        return ToolInputHookOutcome(
            tool_call=current_call,
            action=action,
            diagnostics=tuple(diagnostics),
            handlers=tuple(handlers),
            omitted_handler_count=omitted_handler_count,
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


_BUILTIN_INPUT_HANDLERS = ((ToolInputHandlerDeclaration(name="shell-non-interactive-env", version="1"), _shell_non_interactive_env_handler),)


def builtin_tool_input_handler_declarations() -> tuple[ToolInputHandlerDeclaration, ...]:
    return tuple(declaration for declaration, _handler in _BUILTIN_INPUT_HANDLERS)


def materialize_builtin_tool_input_handler(name: str) -> ToolInputHandlerBinding:
    for declaration, handler in _BUILTIN_INPUT_HANDLERS:
        if declaration.name == name:
            # Diagnostic only: actual Popen env merging stays in shell/argv owners.
            return ToolInputHandlerBinding(declaration=declaration, handler=handler)
    raise ValueError(f"unknown builtin tool input handler: {name}")


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
        **outcome.metadata_payload(),
    }


def _arguments_sha256(arguments: Mapping[str, object]) -> str:
    encoded = json.dumps(json_wire_object(arguments), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
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
    "ToolInputHandlerBinding",
    "ToolInputHandlerDeclaration",
    "ToolInputHandlerRegistry",
    "ToolInputHookOutcome",
    "UnchangedDecision",
    "builtin_tool_input_handler_declarations",
    "materialize_builtin_tool_input_handler",
    "tool_input_arguments_sha256",
    "tool_input_rewrite_metadata",
    "validate_tool_input_schema",
]
