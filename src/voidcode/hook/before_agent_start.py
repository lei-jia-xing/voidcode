from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from ..security.json_values import own_json_object

_MAX_HANDLER_NAMES = 32
_MAX_TRACE_TEXT_CHARS = 240


@dataclass(frozen=True, slots=True)
class BeforeAgentStartRequest:
    original_prompt: str
    effective_prompt: str
    command: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.original_prompt, str) or not isinstance(self.effective_prompt, str):
            raise ValueError("before-agent-start prompts must be strings")
        if self.command is not None:
            if not isinstance(self.command, Mapping):
                raise ValueError("before-agent-start command context must be an object")
            object.__setattr__(self, "command", MappingProxyType(own_json_object(self.command)))


@dataclass(frozen=True, slots=True)
class BeforeAgentStartTransform:
    prompt: str

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("before-agent-start transform prompt must be non-empty")


@dataclass(frozen=True, slots=True)
class BeforeAgentStartBlock:
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("before-agent-start block reason must be non-empty")
        object.__setattr__(self, "reason", self.reason.strip()[:_MAX_TRACE_TEXT_CHARS])


BeforeAgentStartDecision = BeforeAgentStartTransform | BeforeAgentStartBlock


class BeforeAgentStartHandler(Protocol):
    def __call__(self, request: BeforeAgentStartRequest, /) -> BeforeAgentStartDecision: ...


@dataclass(frozen=True, slots=True)
class BeforeAgentStartDeclaration:
    name: str
    version: str
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("before-agent-start handler name must be non-empty")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("before-agent-start handler version must be non-empty")
        if type(self.priority) is not int:
            raise ValueError("before-agent-start handler priority must be an integer")


@dataclass(frozen=True, slots=True)
class BeforeAgentStartBinding:
    declaration: BeforeAgentStartDeclaration
    handler: BeforeAgentStartHandler

    def __post_init__(self) -> None:
        if not isinstance(self.declaration, BeforeAgentStartDeclaration) or not callable(self.handler):
            raise ValueError("before-agent-start binding requires a declaration and callable handler")


@dataclass(frozen=True, slots=True)
class BeforeAgentStartOutcome:
    prompt: str
    handlers: tuple[BeforeAgentStartDeclaration, ...]
    changed: bool
    blocked_reason: str | None = None
    failed_handler: str | None = None

    def trace_payload(self) -> dict[str, object]:
        return {
            "status": "blocked" if self.blocked_reason is not None else ("failed" if self.failed_handler else "ready"),
            "changed": self.changed,
            "handlers": [
                {
                    "name": item.name[:_MAX_TRACE_TEXT_CHARS],
                    "version": item.version[:_MAX_TRACE_TEXT_CHARS],
                }
                for item in self.handlers[:_MAX_HANDLER_NAMES]
            ],
            "omitted_handler_count": max(0, len(self.handlers) - _MAX_HANDLER_NAMES),
            **({"blocked_reason": self.blocked_reason[:_MAX_TRACE_TEXT_CHARS]} if self.blocked_reason is not None else {}),
            **({"failed_handler": self.failed_handler[:_MAX_TRACE_TEXT_CHARS]} if self.failed_handler is not None else {}),
        }


class BeforeAgentStartRegistry:
    """Ordered trusted callbacks for the one runtime request-preparation phase."""

    def __init__(
        self,
        bindings: Iterable[BeforeAgentStartBinding] = (),
        *,
        declarations: Iterable[BeforeAgentStartDeclaration] | None = None,
    ) -> None:
        bound = tuple(bindings)
        declared = tuple(declarations) if declarations is not None else tuple(binding.declaration for binding in bound)
        by_name: dict[str, BeforeAgentStartDeclaration] = {}
        for declaration in declared:
            if type(declaration) is not BeforeAgentStartDeclaration:
                raise ValueError("before-agent-start declarations must be typed records")
            if declaration.name in by_name:
                raise ValueError(f"duplicate before-agent-start handler name: {declaration.name}")
            by_name[declaration.name] = declaration
        bound_by_name: dict[str, BeforeAgentStartBinding] = {}
        for binding in bound:
            name = binding.declaration.name
            if name in bound_by_name or by_name.get(name) != binding.declaration:
                raise ValueError(f"before-agent-start binding does not match unique declaration: {name}")
            bound_by_name[name] = binding
        self._declarations = MappingProxyType(by_name)
        self._bindings = tuple(
            bound_by_name[item.name]
            for item in sorted(declared, key=lambda declaration: (declaration.priority, declaration.name))
            if item.name in bound_by_name
        )

    @property
    def declarations(self) -> Mapping[str, BeforeAgentStartDeclaration]:
        return self._declarations

    @property
    def bindings(self) -> tuple[BeforeAgentStartBinding, ...]:
        return self._bindings

    def register(self, declaration: BeforeAgentStartDeclaration) -> None:
        if type(declaration) is not BeforeAgentStartDeclaration:
            raise ValueError("before-agent-start declarations must be typed records")
        if declaration.name in self._declarations:
            raise ValueError(f"before-agent-start handler name is already declared: {declaration.name}")
        declarations = dict(self._declarations)
        declarations[declaration.name] = declaration
        self._declarations = MappingProxyType(declarations)

    def bind_handler(self, binding: BeforeAgentStartBinding) -> None:
        name = binding.declaration.name
        if self._declarations.get(name) != binding.declaration:
            raise ValueError(f"before-agent-start binding does not match declaration: {name}")
        bound = {item.declaration.name: item for item in self._bindings}
        bound[name] = binding
        self._bindings = tuple(
            bound[item.name]
            for item in sorted(self._declarations.values(), key=lambda declaration: (declaration.priority, declaration.name))
            if item.name in bound
        )

    def apply(self, request: BeforeAgentStartRequest) -> BeforeAgentStartOutcome:
        if len(self._bindings) != len(self._declarations):
            bound_names = {binding.declaration.name for binding in self._bindings}
            declared = tuple(sorted(self._declarations.values(), key=lambda declaration: (declaration.priority, declaration.name)))
            missing = next(item for item in declared if item.name not in bound_names)
            return BeforeAgentStartOutcome(
                prompt=request.effective_prompt,
                handlers=declared,
                changed=False,
                failed_handler=missing.name,
            )
        current = request
        changed = False
        declarations: list[BeforeAgentStartDeclaration] = []
        for binding in self._bindings:
            declarations.append(binding.declaration)
            try:
                decision = binding.handler(current)
                if isinstance(decision, BeforeAgentStartBlock):
                    return BeforeAgentStartOutcome(
                        prompt=current.effective_prompt,
                        handlers=tuple(declarations),
                        changed=changed,
                        blocked_reason=decision.reason,
                    )
                if not isinstance(decision, BeforeAgentStartTransform):
                    raise ValueError("before-agent-start handler returned an invalid decision")
                changed = changed or decision.prompt != current.effective_prompt
                current = BeforeAgentStartRequest(
                    original_prompt=request.original_prompt,
                    effective_prompt=decision.prompt,
                    command=request.command,
                )
            except Exception:
                return BeforeAgentStartOutcome(
                    prompt=current.effective_prompt,
                    handlers=tuple(declarations),
                    changed=changed,
                    failed_handler=binding.declaration.name,
                )
        return BeforeAgentStartOutcome(prompt=current.effective_prompt, handlers=tuple(declarations), changed=changed)
