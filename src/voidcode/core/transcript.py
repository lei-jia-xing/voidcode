from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from ..tools.contracts import ToolDiagnostics, ToolResult, ToolResultStatus

type MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolResultView:
    """Isolated model-facing rendering; never mutates the authoritative result."""

    result: ToolResult
    content: str | None
    clipped: bool = False
    original_content_chars: int | None = None
    content_char_limit: int | None = None
    pruned: bool = False
    _isolated_data: dict[str, object] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        isolated_result = deepcopy(self.result)
        object.__setattr__(self, "result", isolated_result)
        object.__setattr__(self, "_isolated_data", deepcopy(isolated_result.data))

    @property
    def tool_name(self) -> str:
        return self.result.tool_name

    @property
    def status(self) -> ToolResultStatus:
        return self.result.status

    @property
    def data(self) -> dict[str, object]:
        return self._isolated_data

    @property
    def error(self) -> str | None:
        return self.result.error

    @property
    def truncated(self) -> bool:
        return self.pruned or self.clipped or self.result.truncated

    @property
    def partial(self) -> bool:
        return True if self.pruned or self.clipped else self.result.partial

    @property
    def reference(self) -> str | None:
        return self.result.reference

    @property
    def source(self) -> str | None:
        return self.result.source

    @property
    def diagnostics(self) -> ToolDiagnostics | None:
        return self.result.diagnostics

    def __getattr__(self, name: str) -> object:
        return getattr(self.result, name)


def tool_result_output(result: ToolResult | ToolResultView) -> str | None:
    if result.tool_name == "read" and result.status == "ok" and not (isinstance(result, ToolResultView) and (result.pruned or result.clipped)):
        raw_content = result.data.get("raw_content")
        if isinstance(raw_content, str):
            return raw_content
    return result.content


@dataclass(frozen=True, slots=True)
class ContextSegment:
    role: MessageRole
    content: str | None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, object] | None = None
    metadata: dict[str, object] | None = None


@runtime_checkable
class ContextWindow(Protocol):
    @property
    def prompt(self) -> str: ...

    @property
    def tool_results(self) -> tuple[ToolResult | ToolResultView, ...]: ...

    @property
    def compacted(self) -> bool: ...

    @property
    def retained_tool_result_count(self) -> int: ...

    @property
    def continuity_state(self) -> object | None: ...


@runtime_checkable
class AssembledContext(Protocol):
    """This turn's model-facing transcript, not an append-only history store."""

    @property
    def prompt(self) -> str: ...

    @property
    def tool_results(self) -> tuple[ToolResult | ToolResultView, ...]: ...

    @property
    def continuity_state(self) -> object | None: ...

    @property
    def segments(self) -> tuple[ContextSegment, ...]: ...

    @property
    def metadata(self) -> dict[str, object]: ...
