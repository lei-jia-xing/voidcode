from __future__ import annotations

from dataclasses import dataclass, field

from voidcode.core.transcript import ContextSegment
from voidcode.runtime.context.provider import inspect_provider_context
from voidcode.tools.contracts import ToolResult


@dataclass(frozen=True)
class _Assembled:
    segments: tuple[ContextSegment, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    continuity_state: object | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def _diagnostic_codes(metadata: dict[str, object]) -> tuple[str, ...]:
    snapshot = inspect_provider_context(
        assembled_context=_Assembled(metadata=metadata),
        provider="anthropic",
        model="claude-test",
        execution_engine="direct",
        available_tool_count=0,
    )
    return tuple(diagnostic.code for diagnostic in snapshot.diagnostics)


def test_dropped_count_is_read_from_the_nested_projection() -> None:
    codes = _diagnostic_codes(
        {
            "dropped_tool_result_count": 0,
            "projection": {"dropped_tool_result_count": 3, "retained_tool_result_count": 1},
        }
    )
    assert "tool_feedback_not_retained" in codes


def test_absent_projection_keeps_the_diagnostics_quiet() -> None:
    assert _diagnostic_codes({"dropped_tool_result_count": 3}) == ()


def test_context_transform_diagnostics_keep_declared_and_request_policies_distinct() -> None:
    snapshot = inspect_provider_context(
        assembled_context=_Assembled(
            metadata={
                "context_transforms": {
                    "version": 2,
                    "failure_policy": "warn",
                    "applied": [
                        {
                            "provider_id": "mode_guidance",
                            "provider_version": "8",
                            "scope": "provider_context",
                            "failure_policy": "block",
                            "status": "error",
                            "priority": 1,
                            "execution_index": 1,
                            "sources": [],
                            "diagnostics": ["provider unavailable"],
                            "error": "provider unavailable",
                        }
                    ],
                }
            }
        ),
        provider="anthropic",
        model="claude-test",
        execution_engine="direct",
        available_tool_count=0,
    )

    diagnostic = snapshot.diagnostics[0]
    assert diagnostic.code == "context_transform_trace"
    assert diagnostic.severity == "warning"
    assert diagnostic.details == {
        "status": "error",
        "sources": [],
        "provider_version": "8",
        "scope": "provider_context",
        "failure_policy": "block",
        "request_failure_policy": "warn",
        "execution_index": 1,
        "priority": 1,
        "diagnostics": ["provider unavailable"],
        "error": "provider unavailable",
    }
