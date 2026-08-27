from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

import jsonschema

from ..runtime.context_window import ToolResultView
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
class ToolInputDecision:
    """The deliberately small result vocabulary for typed input handlers."""

    action: ToolInputAction
    arguments: Mapping[str, object] | None = None
    reason: str | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.action not in {"unchanged", "rewrite", "block", "diagnostic"}:
            raise ValueError(f"unsupported tool input handler action: {self.action}")
        if self.action == "rewrite":
            if not isinstance(self.arguments, Mapping):
                raise ValueError("tool input rewrite must provide an arguments mapping")
            if not all(isinstance(key, str) and key for key in self.arguments):
                raise ValueError("tool input rewrite argument keys must be non-empty strings")
            object.__setattr__(self, "arguments", dict(self.arguments))
        elif self.arguments is not None:
            raise ValueError(f"tool input handler action {self.action} cannot provide arguments")
        if self.action == "block" and not _safe_text(self.reason):
            raise ValueError("tool input block must provide a reason")
        if self.action != "block" and self.reason is not None:
            raise ValueError(f"tool input handler action {self.action} cannot provide a block reason")
        if self.action == "diagnostic" and not _safe_text(self.diagnostic):
            raise ValueError("tool input diagnostic must provide diagnostic text")
        if self.action != "diagnostic" and self.diagnostic is not None:
            raise ValueError(f"tool input handler action {self.action} cannot provide diagnostic text")
        if self.diagnostic is not None:
            object.__setattr__(self, "diagnostic", _safe_text(self.diagnostic))
        if self.reason is not None:
            object.__setattr__(self, "reason", _safe_text(self.reason))


class ToolInputHandler(Protocol):
    """Callable contract for pure typed tool-input canonicalization/policy."""

    def __call__(self, event: ToolInputEvent, /) -> ToolInputDecision: ...


@dataclass(frozen=True, slots=True)
class ToolInputHandlerBinding:
    name: str
    handler: ToolInputHandler
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool input handler name must be non-empty")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("tool input handler priority must be an integer")


@dataclass(frozen=True, slots=True)
class ToolInputHookOutcome:
    tool_call: ToolCall
    action: ToolInputAction = "unchanged"
    diagnostics: tuple[str, ...] = ()
    handler_names: tuple[str, ...] = ()
    blocked_reason: str | None = None
    _original_arguments: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    @property
    def changed(self) -> bool:
        return self.tool_call.arguments != self._original_arguments


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

    @classmethod
    def empty(cls) -> ToolInputHandlerRegistry:
        return cls()

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
                    _original_arguments=original_arguments,
                )
            if not isinstance(decision, ToolInputDecision):
                return ToolInputHookOutcome(
                    tool_call=current_call,
                    action="block",
                    diagnostics=tuple(diagnostics),
                    handler_names=tuple(names),
                    blocked_reason=_safe_text(f"tool input handler '{binding.name}' returned an invalid decision"),
                    _original_arguments=original_arguments,
                )
            if decision.action == "diagnostic":
                assert decision.diagnostic is not None
                if len(diagnostics) < _MAX_DIAGNOSTICS:
                    diagnostics.append(decision.diagnostic)
                elif len(diagnostics) == _MAX_DIAGNOSTICS:
                    diagnostics.append("[additional diagnostics omitted]")
                continue
            if decision.action == "unchanged":
                continue
            if decision.action == "rewrite":
                assert decision.arguments is not None
                next_arguments = deepcopy(dict(decision.arguments))
                changed = changed or next_arguments != current_call.arguments
                current_call = replace(current_call, arguments=next_arguments)
                continue
            assert decision.action == "block"
            assert decision.reason is not None
            return ToolInputHookOutcome(
                tool_call=current_call,
                action="block",
                diagnostics=tuple(diagnostics),
                handler_names=tuple(names),
                blocked_reason=decision.reason,
                _original_arguments=original_arguments,
            )

        action: ToolInputAction = "rewrite" if changed else ("diagnostic" if diagnostics else "unchanged")
        return ToolInputHookOutcome(
            tool_call=current_call,
            action=action,
            diagnostics=tuple(diagnostics),
            handler_names=tuple(names),
            _original_arguments=original_arguments,
        )


def builtin_tool_input_handler_registry() -> ToolInputHandlerRegistry:
    """Return intentionally empty production builtin registry."""

    return ToolInputHandlerRegistry.empty()


def compose_tool_input_handler_registry(
    builtin_bindings: Iterable[ToolInputHandlerBinding] = (),
    configured_bindings: Iterable[ToolInputHandlerBinding] = (),
) -> ToolInputHandlerRegistry:
    """Compose builtin and explicit bindings through one stable registry."""

    return ToolInputHandlerRegistry((*builtin_bindings, *configured_bindings))


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


ToolResultHandlerAction = Literal["unchanged", "rewrite", "error"]


@dataclass(frozen=True, slots=True)
class ToolResultHandlerDecision:
    """Provider-visible result transformation; authority fields are absent."""

    action: Literal["unchanged", "rewrite"]
    content: str | None = None
    error: str | None = None
    diagnostics: ToolDiagnostics | None = None

    def __post_init__(self) -> None:
        if self.action not in {"unchanged", "rewrite"}:
            raise ValueError(f"unsupported tool result handler action: {self.action}")
        if self.action == "unchanged" and any(value is not None for value in (self.content, self.error, self.diagnostics)):
            raise ValueError("unchanged result decision cannot provide view fields")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("result content must be a string or null")
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("result error must be a string or null")


class ToolResultHandler(Protocol):
    def __call__(self, result: ToolResultView, /) -> ToolResultHandlerDecision: ...


@dataclass(frozen=True, slots=True)
class ToolResultHandlerBinding:
    name: str
    handler: ToolResultHandler
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool result handler name must be non-empty")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("tool result handler priority must be an integer")


@dataclass(frozen=True, slots=True)
class ToolResultHandlerOutcome:
    view: ToolResultView
    action: ToolResultHandlerAction = "unchanged"
    handler_names: tuple[str, ...] = ()
    failure_reason: str | None = None


class ToolResultHandlerRegistry:
    """Stable priority-ordered composition of safe result views."""

    def __init__(self, bindings: Iterable[ToolResultHandlerBinding] = ()) -> None:
        indexed = tuple(enumerate(bindings))
        names: set[str] = set()
        for _index, binding in indexed:
            if binding.name in names:
                raise ValueError(f"duplicate tool result handler name: {binding.name}")
            names.add(binding.name)
        self._bindings = tuple(binding for _index, binding in sorted(indexed, key=lambda item: item[1].priority))

    @classmethod
    def empty(cls) -> ToolResultHandlerRegistry:
        return cls()

    @property
    def bindings(self) -> tuple[ToolResultHandlerBinding, ...]:
        return self._bindings

    def apply(self, *, result: ToolResultView) -> ToolResultHandlerOutcome:
        source = ToolResultView(result=deepcopy(result.result), content=deepcopy(result.content))
        current = source
        names: list[str] = []
        for binding in self._bindings:
            if len(names) < _MAX_HANDLER_NAMES:
                names.append(binding.name)
            try:
                decision = binding.handler(deepcopy(current))
            except Exception:
                return ToolResultHandlerOutcome(
                    view=source, action="error", handler_names=tuple(names), failure_reason=f"handler '{binding.name}' failed"
                )
            if not isinstance(decision, ToolResultHandlerDecision):
                return ToolResultHandlerOutcome(
                    view=source, action="error", handler_names=tuple(names), failure_reason=f"handler '{binding.name}' returned invalid decision"
                )
            if decision.action == "unchanged":
                continue
            try:
                transformed = replace(
                    current.result, content=deepcopy(decision.content), error=deepcopy(decision.error), diagnostics=deepcopy(decision.diagnostics)
                )
            except Exception as exc:
                return ToolResultHandlerOutcome(
                    view=source, action="error", handler_names=tuple(names), failure_reason=f"handler '{binding.name}' produced invalid view: {exc}"
                )
            current = ToolResultView(result=transformed, content=transformed.content)
        return ToolResultHandlerOutcome(view=current, action="rewrite" if current != source else "unchanged", handler_names=tuple(names))


def tool_result_handler_metadata(*, original: ToolResultView, outcome: ToolResultHandlerOutcome) -> dict[str, object]:
    """Return bounded, text-free provenance for a provider-view transform."""
    return {
        "surface": "typed_result",
        "action": outcome.action,
        "handler_names": list(outcome.handler_names[:_MAX_HANDLER_NAMES]),
        "failure": outcome.failure_reason,
        "original_content_sha256": _result_field_sha256(original.content),
        "final_content_sha256": _result_field_sha256(outcome.view.content),
        "original_error_sha256": _result_field_sha256(original.error),
        "final_error_sha256": _result_field_sha256(outcome.view.error),
        "diagnostic_changed": original.diagnostics != outcome.view.diagnostics,
    }


def _result_field_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=repr).encode()
    return hashlib.sha256(encoded).hexdigest()


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
    "ToolInputAction",
    "ToolInputDecision",
    "ToolInputEvent",
    "ToolInputHandler",
    "ToolInputHandlerRegistry",
    "ToolInputHookOutcome",
    "ToolResultHandler",
    "ToolResultHandlerAction",
    "ToolResultHandlerBinding",
    "ToolResultHandlerDecision",
    "ToolResultHandlerOutcome",
    "ToolResultHandlerRegistry",
    "builtin_tool_input_handler_registry",
    "compose_tool_input_handler_registry",
    "tool_input_arguments_sha256",
    "tool_input_rewrite_metadata",
    "tool_result_handler_metadata",
    "validate_tool_input_schema",
]
