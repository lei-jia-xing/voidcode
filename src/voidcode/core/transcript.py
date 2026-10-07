from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from ..security.json_values import own_json_object
from ..tools.contracts import AttachmentOutput, EmptyOutput, TextOutput, ToolDiagnostics, ToolFailure, ToolOutput

if TYPE_CHECKING:
    from .turns import ReportedCall

type MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolResultView:
    """An explicit model projection, with no reference to the canonical body."""

    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, object]
    output: ToolOutput
    status: Literal["ok", "error"]
    error: str | None = None
    diagnostics: ToolDiagnostics | None = None
    clipped: bool = False
    original_content_chars: int | None = None
    content_char_limit: int | None = None
    pruned: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))


def project_report(report: ReportedCall) -> ToolResultView:
    result = report.result
    return ToolResultView(
        tool_call_id=report.tool_call_id,
        tool_name=report.final_tool_name,
        arguments=report.authorized_arguments,
        output=result.output,
        status=result.status,
        error=result.error if isinstance(result, ToolFailure) else None,
        diagnostics=result.diagnostics if isinstance(result, ToolFailure) else None,
    )


def output_text(output: ToolOutput) -> str | None:
    if isinstance(output, TextOutput):
        return output.text
    if isinstance(output, (EmptyOutput, AttachmentOutput)):
        return output.presentation
    raise TypeError(f"unsupported tool output: {type(output).__name__}")


def tool_result_output(result: ToolResultView) -> str | None:
    return output_text(result.output)


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
    def tool_results(self) -> tuple[ToolResultView, ...]: ...

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
    def tool_results(self) -> tuple[ToolResultView, ...]: ...

    @property
    def continuity_state(self) -> object | None: ...

    @property
    def segments(self) -> tuple[ContextSegment, ...]: ...

    @property
    def metadata(self) -> dict[str, object]: ...
