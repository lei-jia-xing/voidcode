from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

from ..provider.protocol import ProviderAbortSignal


@dataclass(frozen=True, slots=True)
class RuntimeToolInvocationContext:
    session_id: str
    parent_session_id: str | None = None
    delegation_depth: int = 0
    remaining_spawn_budget: int | None = None
    abort_signal: ProviderAbortSignal | None = None


_CURRENT_RUNTIME_TOOL_CONTEXT: ContextVar[RuntimeToolInvocationContext | None] = ContextVar(
    "voidcode_runtime_tool_context",
    default=None,
)


@contextmanager
def bind_runtime_tool_context(
    context: RuntimeToolInvocationContext,
) -> Iterator[None]:
    token: Token[RuntimeToolInvocationContext | None] = _CURRENT_RUNTIME_TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_RUNTIME_TOOL_CONTEXT.reset(token)


def current_runtime_tool_context() -> RuntimeToolInvocationContext | None:
    return _CURRENT_RUNTIME_TOOL_CONTEXT.get()


def require_runtime_tool_context(tool_name: str) -> RuntimeToolInvocationContext:
    context = current_runtime_tool_context()
    if context is None:
        raise RuntimeError(f"{tool_name} requires an active runtime tool invocation context")
    return context
