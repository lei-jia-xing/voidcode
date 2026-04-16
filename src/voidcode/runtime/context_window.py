from __future__ import annotations

from dataclasses import dataclass

from ..tools.contracts import ToolResult


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    max_tool_results: int = 4

    def __post_init__(self) -> None:
        if self.max_tool_results < 0:
            raise ValueError("max_tool_results must be >= 0")


@dataclass(frozen=True, slots=True)
class RuntimeContextWindow:
    prompt: str
    tool_results: tuple[ToolResult, ...] = ()
    compacted: bool = False
    compaction_reason: str | None = None
    original_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    max_tool_result_count: int = 0

    def metadata_payload(self) -> dict[str, object]:
        return {
            "compacted": self.compacted,
            "compaction_reason": self.compaction_reason,
            "original_tool_result_count": self.original_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
            "max_tool_result_count": self.max_tool_result_count,
        }


def prepare_single_agent_context(
    *,
    prompt: str,
    tool_results: tuple[ToolResult, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
) -> RuntimeContextWindow:
    _ = session_metadata
    effective_policy = policy or ContextWindowPolicy()
    original_count = len(tool_results)

    if effective_policy.max_tool_results == 0:
        retained_results: tuple[ToolResult, ...] = ()
    else:
        retained_results = tool_results[-effective_policy.max_tool_results :]

    retained_count = len(retained_results)
    compacted = retained_count < original_count

    return RuntimeContextWindow(
        prompt=prompt,
        tool_results=retained_results,
        compacted=compacted,
        compaction_reason="tool_result_window" if compacted else None,
        original_tool_result_count=original_count,
        retained_tool_result_count=retained_count,
        max_tool_result_count=effective_policy.max_tool_results,
    )
