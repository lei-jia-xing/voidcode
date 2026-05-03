from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any, Literal, cast
from unittest.mock import patch

from voidcode.runtime.context_window import (
    ContextWindowPolicy,
    DroppedToolResultDiagnostic,
    RuntimeAssembledContext,
    RuntimeContextSegment,
    RuntimeContinuityState,
    _retain_indexes_within_token_budget,
    assemble_provider_context,
    context_window_policy_from_payload,
    continuity_state_from_metadata_payload,
    continuity_summary_metadata,
    count_text_tokens,
    normalize_read_file_output,
    prepare_provider_context,
    project_tool_results_for_context_window,
)
from voidcode.runtime.provider_context import inspect_provider_context
from voidcode.tools.contracts import ToolResult


class _FakeEncoding:
    def encode(self, value: str, *, disallowed_special: tuple[object, ...]) -> list[str]:
        _ = disallowed_special
        return list(value)


class _FakeTiktokenModule(ModuleType):
    def __init__(self) -> None:
        super().__init__("tiktoken")
        self.encoding_for_model_calls = 0
        self.get_encoding_calls = 0
        self._encoding = _FakeEncoding()

    def encoding_for_model(self, model: str) -> _FakeEncoding:
        _ = model
        self.encoding_for_model_calls += 1
        return self._encoding

    def get_encoding(self, name: str) -> _FakeEncoding:
        _ = name
        self.get_encoding_calls += 1
        return self._encoding


def _tool_result(index: int) -> ToolResult:
    return ToolResult(
        tool_name="read_file",
        content=f"content-{index}",
        status="ok",
        data={"index": index},
    )


def _sized_tool_result(index: int, *, content_size: int) -> ToolResult:
    return ToolResult(
        tool_name="read_file",
        content=f"content-{index}-" + ("x" * content_size),
        status="ok",
        data={"index": index, "path": f"sample-{index}.txt"},
    )


def _shell_tool_result(index: int, *, command: str, content: str = "ok") -> ToolResult:
    return ToolResult(
        tool_name="shell_exec",
        content=content,
        status="ok",
        data={"index": index, "command": command},
    )


def test_context_window_policy_default_retains_more_tool_results_before_compaction() -> None:
    policy = ContextWindowPolicy()
    context = prepare_provider_context(
        prompt="continue coding task",
        tool_results=tuple(_tool_result(index) for index in range(1, 8)),
        session_metadata={},
        policy=policy,
    )

    assert policy.max_tool_results == 8
    assert policy.recent_tool_result_tokens == 3_000
    assert policy.default_tool_result_tokens == 1_500
    assert context.compacted is False
    assert context.retained_tool_result_count == 7


def test_prepare_provider_context_default_policy_truncates_large_tool_results() -> None:
    large_content = "x" * 20_000

    context = prepare_provider_context(
        prompt="inspect large file",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content=large_content,
                data={"path": "large.txt"},
            ),
        ),
        session_metadata={},
    )

    (result,) = context.tool_results
    assert result.truncated is True
    assert result.partial is True
    assert result.content is not None
    assert len(result.content) < len(large_content)
    assert context.truncated_tool_result_count == 1
    assert context.token_budget is None


def test_prepare_provider_context_keeps_results_within_limit() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2)),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=3),
    )

    assert context.prompt == "read sample.txt"
    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2)
    assert context.compacted is False
    assert context.compaction_reason is None
    assert context.original_tool_result_count == 2
    assert context.retained_tool_result_count == 2
    assert context.max_tool_result_count == 3
    assert context.continuity_state is None


def test_assemble_provider_context_injects_active_runtime_todos() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 1,
                    "revision": 12,
                    "todos": [
                        {
                            "content": "implement runtime todo state",
                            "status": "in_progress",
                            "priority": "high",
                            "position": 1,
                            "updated_at": 12,
                        },
                        {
                            "content": "old finished task",
                            "status": "completed",
                            "priority": "low",
                            "position": 2,
                            "updated_at": 12,
                        },
                    ],
                }
            }
        },
        policy=ContextWindowPolicy(max_tool_results=0),
    )

    system_segments = [
        segment.content for segment in assembled.segments if segment.role == "system"
    ]

    assert any(
        isinstance(content, str)
        and "Runtime-managed todo state is active" in content
        and "implement runtime todo state" in content
        and "old finished task" not in content
        for content in system_segments
    )
    assert [segment.metadata for segment in assembled.segments if segment.role == "system"] == [
        {"source": "runtime_todo_state"}
    ]


def test_provider_context_inspector_reports_synthetic_feedback_mode() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content="hello",
                status="ok",
                data={
                    "tool_call_id": "call:1",
                    "arguments": {"path": "sample.txt", "api_key": "secret"},
                    "path": "sample.txt",
                },
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=4),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="opencode-go",
        model="glm-5",
        execution_engine="provider",
        available_tool_count=3,
        tool_feedback_mode="synthetic_user_message",
    )

    assert snapshot.provider == "opencode-go"
    assert snapshot.provider_messages[-1].source == "provider_synthetic_tool_feedback"
    assert snapshot.provider_messages[-1].role == "user"
    synthetic_content = snapshot.provider_messages[-1].content or ""
    assert "Completed tool calls for current request" in synthetic_content
    assert "api_key" not in synthetic_content
    assert "secret" not in synthetic_content
    assert any(
        diagnostic.code == "provider_path_uses_synthetic_tool_feedback"
        for diagnostic in snapshot.diagnostics
    )


def test_provider_context_inspector_strips_sentinels_from_provider_messages() -> None:
    raw_todo_content = "Secret todo content should not appear in provider messages"
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="todo_write",
                content="Updated todos",
                status="ok",
                data={
                    "tool_call_id": "call:todo",
                    "arguments": {
                        "todos": [
                            {
                                "content": raw_todo_content,
                                "status": "pending",
                                "priority": "high",
                            }
                        ]
                    },
                },
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=4),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=3,
    )

    tool_call = snapshot.provider_messages[-2].tool_calls[0]
    function = cast(dict[str, object], tool_call["function"])
    provider_arguments = function["arguments"]
    assert isinstance(provider_arguments, str)
    assert raw_todo_content not in provider_arguments
    assert '"content": ""' in provider_arguments
    assert '"omitted": true' not in provider_arguments
    assert '"byte_count"' not in provider_arguments


def test_provider_context_inspector_redacts_secret_text_from_tool_output() -> None:
    assembled = assemble_provider_context(
        prompt="inspect env",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content=(
                    "OPENAI_API_KEY=sk-test-secret\n"
                    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
                ),
                status="ok",
                data={"tool_call_id": "call:secret", "arguments": {"path": ".env"}},
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=4),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=3,
    )
    tool_segment = snapshot.segments[-1]
    tool_message = snapshot.provider_messages[-1]

    assert tool_segment.content == "OPENAI_API_KEY=[redacted]\nAuthorization: Bearer [redacted]"
    assert "sk-test-secret" not in (tool_message.content or "")
    assert "abcdefghijklmnopqrstuvwxyz" not in (tool_message.content or "")
    assert tool_message.tool_call_id == "call_secret"


def test_provider_context_inspector_redacts_tool_error_and_data_fields() -> None:
    assembled = assemble_provider_context(
        prompt="inspect failure",
        tool_results=(
            ToolResult(
                tool_name="web_fetch",
                status="error",
                error="request failed with access_token=tool-secret-token",
                data={
                    "tool_call_id": "call:error",
                    "arguments": {"url": "https://example.com"},
                    "headers": {"authorization": "Bearer nested-secret-token"},
                    "access_token": "data-secret-token",
                },
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=4),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=3,
    )
    tool_segment = snapshot.segments[-1]
    tool_message_content = snapshot.provider_messages[-1].content or ""

    assert "tool-secret-token" not in tool_message_content
    assert "nested-secret-token" not in tool_message_content
    assert "data-secret-token" not in tool_message_content
    assert "authorization" not in tool_message_content.lower()
    assert tool_segment.metadata["error"] == "request failed with access_token=[redacted]"
    tool_data = cast(dict[str, object], tool_segment.metadata["data"])
    assert isinstance(tool_data, dict)
    assert "headers" in tool_data
    assert tool_data["headers"] == {}


def test_provider_context_inspector_reports_tool_pairing_problems() -> None:
    assembled = RuntimeAssembledContext(
        prompt="continue",
        tool_results=(),
        continuity_state=None,
        metadata={},
        segments=(
            RuntimeContextSegment(role="user", content="continue"),
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id="missing-result",
                tool_name="read_file",
                tool_arguments={"path": "sample.txt"},
            ),
            RuntimeContextSegment(
                role="tool",
                content="orphan",
                tool_call_id="orphan-result",
                tool_name="grep",
                metadata={"status": "ok", "data": {}},
            ),
        ),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=0,
    )
    diagnostic_codes = {diagnostic.code for diagnostic in snapshot.diagnostics}

    assert "missing_tool_result" in diagnostic_codes
    assert "orphan_tool_result" in diagnostic_codes
    assert "provider_requires_tools_schema" in diagnostic_codes


def test_provider_context_inspector_reports_duplicate_tool_result_ids() -> None:
    assembled = RuntimeAssembledContext(
        prompt="continue",
        tool_results=(),
        continuity_state=None,
        metadata={},
        segments=(
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id="duplicate-result",
                tool_name="read_file",
            ),
            RuntimeContextSegment(
                role="tool",
                content="first",
                tool_call_id="duplicate-result",
                tool_name="read_file",
                metadata={"status": "ok", "data": {}},
            ),
            RuntimeContextSegment(
                role="tool",
                content="second",
                tool_call_id="duplicate-result",
                tool_name="read_file",
                metadata={"status": "ok", "data": {}},
            ),
        ),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=1,
    )

    duplicate = [
        diagnostic
        for diagnostic in snapshot.diagnostics
        if diagnostic.code == "duplicate_tool_call_id"
    ]
    assert len(duplicate) == 1
    assert duplicate[0].details == {"tool_call_ids": ["duplicate-result"]}


def test_provider_context_inspector_reports_oversized_retained_tool_feedback() -> None:
    assembled = RuntimeAssembledContext(
        prompt="continue",
        tool_results=(),
        continuity_state=None,
        metadata={},
        segments=(
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id="large-result",
                tool_name="read_file",
            ),
            RuntimeContextSegment(
                role="tool",
                content="x" * 32,
                tool_call_id="large-result",
                tool_name="read_file",
                metadata={"status": "ok", "data": {}},
            ),
        ),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=1,
        oversized_tool_feedback_chars=8,
    )

    oversized = [
        diagnostic
        for diagnostic in snapshot.diagnostics
        if diagnostic.code == "oversized_tool_feedback"
    ]
    assert len(oversized) == 1
    assert oversized[0].details == {"content_chars": 32, "threshold_chars": 8}


def test_provider_context_parity_matrix_preserves_tool_shapes_across_debug_messages() -> None:
    raw_read_content = "\n".join(
        [
            "<path>sample.txt</path>",
            "<type>file</type>",
            "<content>",
            "1: alpha",
            "2: beta",
            "(End of file - total 2 lines)",
            "</content>",
        ]
    )
    tool_results = (
        ToolResult(
            tool_name="read_file",
            status="ok",
            content=raw_read_content,
            data={
                "tool_call_id": "read-1",
                "arguments": {"path": "sample.txt"},
                "path": "sample.txt",
                "type": "file",
            },
        ),
        ToolResult(
            tool_name="shell_exec",
            status="ok",
            content="line-1\n[truncated: .voidcode/tool-output/shell_exec-abc.txt]",
            data={
                "tool_call_id": "shell-1",
                "arguments": {"command": "python script.py"},
                "command": "python script.py",
                "exit_code": 0,
                "output_path": ".voidcode/tool-output/shell_exec-abc.txt",
            },
            truncated=True,
            partial=True,
            reference=".voidcode/tool-output/shell_exec-abc.txt",
        ),
        ToolResult(
            tool_name="grep",
            status="ok",
            content="Found 2 match(es) for 'alpha' in src\nsrc/a.py:1: alpha",
            data={
                "tool_call_id": "grep-1",
                "arguments": {"pattern": "alpha", "path": "src"},
                "pattern": "alpha",
                "match_count": 2,
                "matches": [{"file": "src/a.py", "line": 1, "text": "alpha"}],
            },
        ),
        ToolResult(
            tool_name="todo_write",
            status="ok",
            content="Updated 1 todos\n1. [in_progress/high] preserve context parity",
            data={
                "tool_call_id": "todo-1",
                "arguments": {
                    "todos": [
                        {
                            "content": "preserve context parity",
                            "status": "in_progress",
                            "priority": "high",
                        }
                    ]
                },
                "todos": [
                    {
                        "content": "preserve context parity",
                        "status": "in_progress",
                        "priority": "high",
                    }
                ],
                "summary": {"total": 1, "in_progress": 1},
            },
        ),
        ToolResult(
            tool_name="task",
            status="ok",
            content="Background task launched.\n\nBackground Task ID: bg_123",
            data={
                "tool_call_id": "task-1",
                "arguments": {"prompt": "inspect child"},
                "task_id": "bg_123",
                "child_session_id": "child-session",
            },
            reference="session:child-session",
        ),
        ToolResult(
            tool_name="background_output",
            status="ok",
            content="Task Result\n\nTask ID: bg_123\nSummary: child done",
            data={
                "tool_call_id": "background-1",
                "arguments": {"task_id": "bg_123"},
                "task_id": "bg_123",
                "child_session_id": "child-session",
                "summary_output": "child done",
            },
            reference="session:child-session",
        ),
    )
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=tool_results,
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 1,
                    "revision": 1,
                    "todos": [
                        {
                            "content": "preserve context parity",
                            "status": "in_progress",
                            "priority": "high",
                            "position": 1,
                            "updated_at": 1,
                        }
                    ],
                }
            }
        },
        policy=ContextWindowPolicy(auto_compaction=False, max_tool_result_tokens=100_000),
    )

    standard_snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=6,
    )
    synthetic_tool_results = (tool_results[0], tool_results[-1])
    synthetic_assembled = assemble_provider_context(
        prompt="continue",
        tool_results=synthetic_tool_results,
        session_metadata={},
        policy=ContextWindowPolicy(auto_compaction=False, max_tool_result_tokens=100_000),
    )
    synthetic_snapshot = inspect_provider_context(
        assembled_context=synthetic_assembled,
        provider="opencode-go",
        model="minimax-m2.7",
        execution_engine="provider",
        available_tool_count=6,
        tool_feedback_mode="synthetic_user_message",
    )

    tool_segments = [segment for segment in standard_snapshot.segments if segment.role == "tool"]
    tool_messages = [
        message for message in standard_snapshot.provider_messages if message.role == "tool"
    ]
    assert [segment.tool_name for segment in tool_segments] == [
        result.tool_name for result in tool_results
    ]
    assert len(tool_messages) == len(tool_results)
    for result, segment, message in zip(tool_results, tool_segments, tool_messages, strict=True):
        assert segment.content == result.content
        assert segment.metadata["status"] == result.status
        assert segment.metadata["reference"] == result.reference
        assert message.content is not None
        payload = json.loads(message.content)
        assert payload["tool_name"] == result.tool_name
        assert payload["status"] == result.status
        assert payload["content"] == result.content
        assert payload["reference"] == result.reference
        assert "tool_call_id" not in payload["data"]
        assert "arguments" not in payload["data"]

    todo_system_segments = [
        segment
        for segment in standard_snapshot.segments
        if segment.role == "system" and segment.source == "runtime_todo_state"
    ]
    assert len(todo_system_segments) == 1
    assert "preserve context parity" in (todo_system_segments[0].content or "")
    synthetic_feedback = synthetic_snapshot.provider_messages[-1].content or ""
    assert synthetic_snapshot.provider_messages[-1].source == "provider_synthetic_tool_feedback"
    for result in synthetic_tool_results:
        assert synthetic_feedback.count(f'"tool_name": "{result.tool_name}"') == 1
    assert "1: alpha" in synthetic_feedback
    assert "child done" in synthetic_feedback
    assert any(
        diagnostic.code == "provider_path_uses_synthetic_tool_feedback"
        for diagnostic in synthetic_snapshot.diagnostics
    )


def test_provider_context_inspector_reports_continuity_distillation_source() -> None:
    assembled = RuntimeAssembledContext(
        prompt="continue",
        tool_results=(),
        continuity_state=None,
        metadata={
            "continuity_state": {
                "summary_text": "summary",
                "dropped_tool_result_count": 1,
                "retained_tool_result_count": 1,
                "source": "tool_result_window",
                "distillation_source": "model_assisted",
            }
        },
        segments=(RuntimeContextSegment(role="user", content="continue"),),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=0,
    )

    matched = [
        diagnostic
        for diagnostic in snapshot.diagnostics
        if diagnostic.code == "continuity_distillation_source"
    ]
    assert len(matched) == 1
    assert matched[0].details == {"distillation_source": "model_assisted"}


def test_prepare_provider_context_compacts_old_results_and_reports_metadata() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3), _tool_result(4)),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=2),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (3, 4)
    assert context.compacted is True
    assert context.compaction_reason == "tool_result_window"
    assert context.original_tool_result_count == 4
    assert context.retained_tool_result_count == 2
    assert context.max_tool_result_count == 2
    assert context.continuity_state is not None
    assert context.continuity_state.dropped_tool_result_count == 2
    assert context.continuity_state.retained_tool_result_count == 2
    assert context.continuity_state.source == "tool_result_window"
    assert len(context.continuity_state.dropped_tool_results) == 2
    assert context.continuity_state.dropped_tool_results[0].metadata_payload() == {
        "tool_name": "read_file",
        "status": "ok",
        "index": 1,
        "estimated_tokens": context.continuity_state.dropped_tool_results[0].estimated_tokens,
    }
    assert context.continuity_state.summary_text is not None
    assert "Compacted 2 earlier tool results:" in context.continuity_state.summary_text
    assert 'content_preview="content-1"' in context.continuity_state.summary_text
    assert 'content_preview="content-2"' in context.continuity_state.summary_text
    assert context.summary_anchor is not None
    assert context.summary_anchor.startswith("continuity:")
    assert context.summary_source == {"tool_result_start": 0, "tool_result_end": 2}
    continuity_payload = cast(dict[str, Any], context.metadata_payload()["continuity_state"])
    assert isinstance(continuity_payload, dict)
    assert continuity_payload["summary_text"] == context.continuity_state.summary_text
    assert continuity_payload["objective"] == "read sample.txt"
    assert continuity_payload["current_goal"] == "read sample.txt"
    assert continuity_payload["dropped_tool_result_count"] == 2
    assert continuity_payload["retained_tool_result_count"] == 2
    assert continuity_payload["source"] == "tool_result_window"
    assert continuity_payload["version"] == 2
    assert continuity_payload["dropped_tool_results"] == [
        item.metadata_payload() for item in context.continuity_state.dropped_tool_results
    ]
    assert context.metadata_payload()["summary_anchor"] == context.summary_anchor
    assert context.metadata_payload()["summary_source"] == context.summary_source


def test_prepare_provider_context_uses_explicit_continuity_preview_policy() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3), _tool_result(4)),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            continuity_preview_items=1,
            continuity_preview_chars=5,
        ),
    )

    assert context.continuity_state is not None
    summary_text = context.continuity_state.summary_text
    assert summary_text is not None
    assert "## Objective\nread sample.txt" in summary_text
    assert "Compacted 3 earlier tool results:" in summary_text
    assert '1. read_file ok content_preview="conte..."' in summary_text
    assert "... and 2 more" in summary_text


def test_prepare_provider_context_compacts_by_absolute_token_budget() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _sized_tool_result(1, content_size=240),
            _sized_tool_result(2, content_size=40),
            _sized_tool_result(3, content_size=40),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_result_tokens=90),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (2, 3)
    assert context.compacted is True
    assert context.compaction_reason == "tool_result_window"
    assert context.token_budget == 90
    assert context.token_estimate_source == "unicode_aware_chars"
    assert context.original_tool_result_tokens is not None
    assert context.retained_tool_result_tokens is not None
    assert context.dropped_tool_result_tokens is not None
    assert context.retained_tool_result_tokens <= 90
    assert context.dropped_tool_result_tokens > 0
    assert context.continuity_state is not None
    assert (
        context.continuity_state.original_tool_result_tokens == context.original_tool_result_tokens
    )
    assert (
        context.continuity_state.retained_tool_result_tokens == context.retained_tool_result_tokens
    )
    assert context.continuity_state.dropped_tool_result_tokens == context.dropped_tool_result_tokens
    assert context.continuity_state.token_budget == 90
    assert context.metadata_payload()["token_budget"] == 90


def test_prepare_provider_context_derives_budget_from_context_ratio() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _sized_tool_result(1, content_size=320),
            _sized_tool_result(2, content_size=160),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_result_tokens=None,
            max_context_ratio=0.1,
            model_context_window_tokens=500,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert context.token_budget == 50
    assert context.retained_tool_result_tokens is not None
    assert context.compacted is True
    assert context.original_tool_result_count == 2
    assert context.retained_tool_result_count >= 1
    assert context.dropped_tool_result_tokens is not None
    assert context.dropped_tool_result_tokens > 0


def test_prepare_provider_context_enforces_count_cap_with_token_budget() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _sized_tool_result(1, content_size=16),
            _sized_tool_result(2, content_size=16),
            _sized_tool_result(3, content_size=16),
            _sized_tool_result(4, content_size=16),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=2,
            max_tool_result_tokens=1_000,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (3, 4)
    assert context.max_tool_result_count == 2
    assert context.retained_tool_result_count == 2
    assert context.compacted is True


def test_prepare_provider_context_preserves_latest_result_over_budget() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_sized_tool_result(1, content_size=400),),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_result_tokens=1),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (1,)
    assert context.compacted is False
    assert context.retained_tool_result_tokens is not None
    assert context.retained_tool_result_tokens > 1


def test_prepare_provider_context_uses_unicode_aware_token_estimates() -> None:
    ascii_context = prepare_provider_context(
        prompt="read ascii.txt",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content="a" * 80,
                status="ok",
                data={"path": "ascii.txt"},
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_result_tokens=1),
    )
    unicode_context = prepare_provider_context(
        prompt="read unicode.txt",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content="你" * 80,
                status="ok",
                data={"path": "unicode.txt"},
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_result_tokens=1),
    )

    assert ascii_context.retained_tool_result_tokens is not None
    assert unicode_context.retained_tool_result_tokens is not None
    assert unicode_context.retained_tool_result_tokens > ascii_context.retained_tool_result_tokens
    assert unicode_context.token_estimate_source == "unicode_aware_chars"


def test_prepare_provider_context_keeps_count_policy_when_budget_missing() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3)),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=2,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (2, 3)
    assert context.token_budget is None
    assert context.original_tool_result_tokens is None


def test_prepare_provider_context_retains_recent_tail_without_scoring() -> None:
    context = prepare_provider_context(
        prompt="fix failing tests",
        tool_results=(
            _tool_result(1),
            ToolResult(
                tool_name="read_file",
                status="error",
                error="missing file",
                data={"index": 2, "path": "missing.py"},
            ),
            _shell_tool_result(3, command="run project verification"),
            _tool_result(4),
            _tool_result(5),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (3, 4, 5)
    assert context.compacted is True
    assert context.continuity_state is not None
    dropped_tool_names = [
        item.metadata_payload()["tool_name"]
        for item in context.continuity_state.dropped_tool_results
    ]
    assert dropped_tool_names == ["read_file", "read_file"]
    dropped_index_positions = [
        item.metadata_payload()["index"] for item in context.continuity_state.dropped_tool_results
    ]
    assert dropped_index_positions == [1, 2]


def test_prepare_provider_context_token_budget_prefers_recent_candidates() -> None:
    context = prepare_provider_context(
        prompt="verify fix",
        tool_results=(
            _sized_tool_result(1, content_size=160),
            _shell_tool_result(2, command="run project verification", content="passed"),
            _sized_tool_result(3, content_size=160),
            _tool_result(4),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=120,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert 4 in retained_indexes
    assert 3 in retained_indexes
    assert 1 not in retained_indexes
    assert context.compacted is True
    assert context.token_budget == 120


def test_prepare_provider_context_can_disable_importance_retention() -> None:
    context = prepare_provider_context(
        prompt="debug",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="error",
                error="not found",
                data={"index": 1, "path": "missing.py"},
            ),
            _shell_tool_result(2, command="run project verification", content="ok"),
            _tool_result(3),
            _tool_result(4),
            _tool_result(5),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            importance_retention=False,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (3, 4, 5)


def test_prepare_provider_context_older_todo_and_task_do_not_displace_newer_reads() -> None:
    context = prepare_provider_context(
        prompt="finish task",
        tool_results=(
            ToolResult(
                tool_name="todo_write",
                content="Updated todos",
                status="ok",
                data={"index": 1},
            ),
            ToolResult(
                tool_name="task",
                content="Background task launched.",
                status="ok",
                data={"index": 2, "task_id": "bg_1"},
            ),
            _tool_result(3),
            _tool_result(4),
            _tool_result(5),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert retained_indexes == (3, 4, 5)


def test_prepare_provider_context_older_write_edit_do_not_displace_newer_reads() -> None:
    context = prepare_provider_context(
        prompt="fix code",
        tool_results=(
            ToolResult(
                tool_name="write_file",
                content="written",
                status="ok",
                data={"index": 1, "path": "src/app.py"},
            ),
            ToolResult(
                tool_name="edit",
                content="edited",
                status="ok",
                data={"index": 2, "path": "src/utils.py"},
            ),
            _tool_result(3),
            _tool_result(4),
            _tool_result(5),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert retained_indexes == (3, 4, 5)


def test_prepare_provider_context_older_error_does_not_displace_newer_results() -> None:
    context = prepare_provider_context(
        prompt="debug",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="error",
                error="not found",
                data={"index": 1, "path": "missing.py"},
            ),
            _tool_result(2),
            _tool_result(3),
            _tool_result(4),
            _tool_result(5),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert retained_indexes == (3, 4, 5)


def test_prepare_provider_context_importance_tie_breaker_prefers_newer() -> None:
    context = prepare_provider_context(
        prompt="read files",
        tool_results=(
            _tool_result(1),
            _tool_result(2),
            _tool_result(3),
            _tool_result(4),
            _tool_result(5),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert 5 in retained_indexes
    assert retained_indexes == (3, 4, 5)


def test_prepare_provider_context_protected_recent_always_kept() -> None:
    context = prepare_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="write_file",
                content="important write",
                status="ok",
                data={"index": 1, "path": "src/app.py"},
            ),
            ToolResult(
                tool_name="read_file",
                status="error",
                error="missing",
                data={"index": 2, "path": "src/missing.py"},
            ),
            _tool_result(3),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=2,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert 3 in retained_indexes


def test_retain_indexes_within_token_budget_protects_recent() -> None:
    results = (
        _tool_result(1),
        _tool_result(2),
        _tool_result(3),
    )
    indexes = _retain_indexes_within_token_budget(
        results,
        candidate_indexes=(0, 1, 2),
        token_budget=100,
        protected_recent_count=1,
        tokenizer_model=None,
    )
    assert len(indexes) >= 1


def test_context_window_policy_importance_retention_is_compatibility_only() -> None:
    policy = ContextWindowPolicy(importance_retention=False)
    parsed = context_window_policy_from_payload(policy.metadata_payload())
    assert parsed.importance_retention is False

    default_policy = ContextWindowPolicy()
    default_parsed = context_window_policy_from_payload(default_policy.metadata_payload())
    assert default_parsed.importance_retention is False

    legacy_payload = default_policy.metadata_payload()
    legacy_payload["importance_retention"] = True
    legacy_parsed = context_window_policy_from_payload(legacy_payload)
    assert legacy_parsed.importance_retention is True


def test_count_text_tokens_reports_estimated_fallback_metadata() -> None:
    counted = count_text_tokens("abcd你")

    assert counted.tokens == 2
    assert counted.method == "estimated"
    assert counted.source == "unicode_aware_chars"
    assert counted.exact is False


def test_count_text_tokens_reports_tiktoken_metadata() -> None:
    fake_tiktoken = _FakeTiktokenModule()
    with patch.dict(sys.modules, {"tiktoken": fake_tiktoken}):
        counted = count_text_tokens("abcd", tokenizer_model="gpt-test")

    assert counted.tokens == 4
    assert counted.method == "tiktoken"
    assert counted.source == "tiktoken:gpt-test"
    assert counted.exact is True


def test_prepare_provider_context_honors_reserved_output_budget() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _sized_tool_result(1, content_size=480),
            _sized_tool_result(2, content_size=200),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_result_tokens=None,
            model_context_window_tokens=120,
            reserved_output_tokens=40,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert context.token_budget == 80
    assert context.reserved_output_tokens == 40
    assert context.metadata_payload()["reserved_output_tokens"] == 40
    assert context.compacted is True
    assert context.original_tool_result_count == 2
    assert context.retained_tool_result_count >= 1


def test_prepare_provider_context_truncates_old_tool_outputs_by_tool_policy() -> None:
    context = prepare_provider_context(
        prompt="search",
        tool_results=(
            ToolResult(tool_name="grep", status="ok", content="x" * 200, data={"index": 1}),
            ToolResult(tool_name="grep", status="ok", content="latest" * 20, data={"index": 2}),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=2,
            per_tool_result_tokens={"grep": 30},
            recent_tool_result_count=1,
        ),
    )

    first, latest = context.tool_results
    assert first.truncated is True
    assert first.partial is True
    assert first.data["context_window_truncated"] is True
    assert "Tool output truncated by context window policy" in (first.content or "")
    assert latest.truncated is False
    assert latest.content == "latest" * 20
    assert context.truncated_tool_result_count == 1
    assert context.metadata_payload()["truncated_tool_result_count"] == 1


def test_prepare_provider_context_keeps_truncation_message_inside_tool_cap() -> None:
    context = prepare_provider_context(
        prompt="search",
        tool_results=(
            ToolResult(tool_name="grep", status="ok", content="x" * 80, data={"index": 1}),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            minimum_retained_tool_results=0,
            recent_tool_result_count=0,
            per_tool_result_tokens={"grep": 1},
        ),
    )

    (result,) = context.tool_results
    assert result.truncated is True
    assert result.content is not None
    assert len(result.content) <= 4


def test_prepare_provider_context_applies_recent_tool_result_token_cap() -> None:
    context = prepare_provider_context(
        prompt="search",
        tool_results=(
            ToolResult(tool_name="grep", status="ok", content="older", data={"index": 1}),
            ToolResult(tool_name="grep", status="ok", content="x" * 80, data={"index": 2}),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            recent_tool_result_count=1,
            recent_tool_result_tokens=5,
            default_tool_result_tokens=None,
        ),
    )

    (latest,) = context.tool_results
    assert latest.data["index"] == 2
    assert latest.truncated is True
    assert latest.content is not None
    assert len(latest.content) <= 20
    assert context.truncated_tool_result_count == 1


def test_prepare_provider_context_reuses_tokenizer_encoding_when_clipping() -> None:
    fake_tiktoken = _FakeTiktokenModule()
    with patch.dict(sys.modules, {"tiktoken": fake_tiktoken}):
        context = prepare_provider_context(
            prompt="search",
            tool_results=(
                ToolResult(tool_name="grep", status="ok", content="x" * 80, data={"index": 1}),
            ),
            session_metadata={},
            policy=ContextWindowPolicy(
                max_tool_results=1,
                minimum_retained_tool_results=0,
                recent_tool_result_count=0,
                per_tool_result_tokens={"grep": 20},
                tokenizer_model="cache-test-model",
            ),
        )

    (result,) = context.tool_results
    assert result.truncated is True
    assert result.content is not None
    assert fake_tiktoken.encoding_for_model_calls == 1
    assert fake_tiktoken.get_encoding_calls == 0


def test_prepare_provider_context_preserves_recent_results_over_count_cap() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3)),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=1, recent_tool_result_count=2),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (2, 3)
    assert context.retained_tool_result_count == 2


def test_prepare_provider_context_auto_compaction_false_retains_all_results() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3)),
        session_metadata={},
        policy=ContextWindowPolicy(auto_compaction=False, max_tool_results=1),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2, 3)
    assert context.compacted is False


def test_context_window_policy_metadata_round_trips() -> None:
    policy = ContextWindowPolicy(
        auto_compaction=False,
        max_tool_results=6,
        max_tool_result_tokens=200,
        model_context_window_tokens=1_000,
        reserved_output_tokens=20,
        recent_tool_result_count=2,
        default_tool_result_tokens=30,
        per_tool_result_tokens={"grep": 10},
        tokenizer_model="gpt-4o",
        continuity_distillation_enabled=True,
        continuity_distillation_max_input_items=9,
        continuity_distillation_max_input_chars=1024,
    )

    parsed = context_window_policy_from_payload(policy.metadata_payload())

    assert parsed == policy


def test_continuity_summary_metadata_is_derived_from_state() -> None:
    first = RuntimeContinuityState(
        summary_text="one",
        dropped_tool_result_count=1,
        retained_tool_result_count=3,
        dropped_tool_results=(
            DroppedToolResultDiagnostic(tool_name="read_file", status="ok", index=1),
        ),
    )
    second = RuntimeContinuityState(
        summary_text="one",
        dropped_tool_result_count=2,
        retained_tool_result_count=3,
        dropped_tool_results=(
            DroppedToolResultDiagnostic(tool_name="read_file", status="ok", index=1),
            DroppedToolResultDiagnostic(tool_name="read_file", status="ok", index=2),
        ),
    )

    first_anchor, first_source = continuity_summary_metadata(first)
    second_anchor, second_source = continuity_summary_metadata(second)

    assert first_anchor is not None
    assert second_anchor is not None
    assert first_anchor != second_anchor
    assert first_source == {"tool_result_start": 0, "tool_result_end": 1}
    assert second_source == {"tool_result_start": 0, "tool_result_end": 2}


def _continuity_tool_result(
    status: Literal["ok", "error"], content: str | None = None
) -> ToolResult:
    return ToolResult(
        tool_name="fake_tool",
        status=status,
        content=content,
        data={},
        error=None,
    )


def test_prepare_provider_context_continuity_metadata_includes_version() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _continuity_tool_result("ok", content="dropped"),
            _continuity_tool_result("ok", content="retained"),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=1),
    )

    assert context.continuity_state is not None
    payload = context.continuity_state.metadata_payload()
    assert payload.get("version") == 2
    assert payload.get("source") == "tool_result_window"
    assert payload.get("dropped_tool_result_count") == 1
    assert payload.get("retained_tool_result_count") == 1
    assert payload.get("objective") == "read sample.txt"


def test_continuity_state_metadata_payload_uses_instance_version() -> None:
    state = RuntimeContinuityState(
        summary_text="legacy summary",
        dropped_tool_result_count=1,
        retained_tool_result_count=2,
        source="tool_result_window",
        version=1,
    )

    payload = state.metadata_payload()

    assert payload["version"] == 1


def test_continuity_state_from_metadata_payload_accepts_explicit_v1_fallback() -> None:
    payload: dict[str, object] = {
        "version": 1,
        "summary_text": "v1 summary",
        "dropped_tool_result_count": 1,
        "retained_tool_result_count": 2,
        "source": "tool_result_window",
    }

    state = continuity_state_from_metadata_payload(payload)

    assert state is not None
    assert state.version == 1
    assert state.summary_text == "v1 summary"
    assert state.distillation_source == "deterministic"


def test_continuity_state_from_metadata_payload_defaults_missing_version_to_v1() -> None:
    payload: dict[str, object] = {
        "summary_text": "implicit legacy summary",
        "dropped_tool_result_count": 1,
        "retained_tool_result_count": 2,
        "source": "tool_result_window",
    }

    state = continuity_state_from_metadata_payload(payload)

    assert state is not None
    assert state.version == 1
    assert state.summary_text == "implicit legacy summary"


def test_continuity_state_from_metadata_payload_rejects_unknown_version_safely() -> None:
    payload: dict[str, object] = {
        "version": 99,
        "summary_text": "future summary",
        "dropped_tool_result_count": 1,
        "retained_tool_result_count": 2,
        "source": "tool_result_window",
    }

    assert continuity_state_from_metadata_payload(payload) is None


def test_continuity_state_from_metadata_payload_rejects_malformed_version_safely() -> None:
    payload: dict[str, object] = {
        "version": "2",
        "summary_text": "malformed summary",
        "dropped_tool_result_count": 1,
        "retained_tool_result_count": 2,
        "source": "tool_result_window",
    }

    assert continuity_state_from_metadata_payload(payload) is None


def test_assemble_provider_context_ignores_malformed_prior_continuity_metadata() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(_tool_result(1),),
        session_metadata={
            "runtime_state": {
                "continuity": {
                    "version": "bad",
                    "summary_text": "must not be trusted as transcript truth",
                    "dropped_tool_result_count": 1,
                    "retained_tool_result_count": 1,
                    "source": "tool_result_window",
                }
            }
        },
        policy=ContextWindowPolicy(max_tool_results=4),
    )

    assert assembled.continuity_state is None
    assert "continuity_state" not in assembled.metadata
    assert all(
        segment.metadata is None or segment.metadata.get("source") != "continuity_summary"
        for segment in assembled.segments
    )


def test_assemble_provider_context_reconstructs_projection_metadata_from_prior_continuity() -> None:
    continuity = RuntimeContinuityState(
        summary_text="Prior compact projection only",
        objective="ship context continuity",
        current_goal="resume safely",
        dropped_tool_result_count=2,
        retained_tool_result_count=1,
        source="tool_result_window",
        dropped_tool_results=(
            DroppedToolResultDiagnostic(tool_name="read_file", status="ok", index=1),
            DroppedToolResultDiagnostic(tool_name="grep", status="ok", index=2),
        ),
        version=2,
    )

    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(_tool_result(3),),
        session_metadata={"runtime_state": {"continuity": continuity.metadata_payload()}},
        policy=ContextWindowPolicy(max_tool_results=4),
    )

    assert assembled.continuity_state == continuity
    assert assembled.metadata["continuity_state"] == continuity.metadata_payload()
    assert isinstance(assembled.metadata["summary_anchor"], str)
    assert assembled.metadata["summary_source"] == {"tool_result_start": 0, "tool_result_end": 2}
    continuity_segments = [
        segment
        for segment in assembled.segments
        if segment.metadata == {"source": "continuity_summary"}
    ]
    assert len(continuity_segments) == 1
    assert "Prior compact projection only" in (continuity_segments[0].content or "")
    retained_tool_segments = [segment for segment in assembled.segments if segment.role == "tool"]
    assert len(retained_tool_segments) == 1
    assert retained_tool_segments[0].content == "content-3"


def test_prepare_provider_context_dropped_tool_diagnostics_omit_raw_content() -> None:
    context = prepare_provider_context(
        prompt="write secret.txt",
        tool_results=(
            ToolResult(
                tool_name="write_file",
                status="ok",
                content="RAW SECRET CONTENT SHOULD NOT BE COPIED",
                data={
                    "tool_call_id": "write-1",
                    "arguments": {"path": "secret.txt", "content": "hidden"},
                    "path": "secret.txt",
                },
            ),
            ToolResult(tool_name="glob", status="ok", content="secret.txt", data={}),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=1),
    )

    assert context.continuity_state is not None
    payload = context.continuity_state.metadata_payload()
    raw_dropped = payload["dropped_tool_results"]
    assert isinstance(raw_dropped, list)
    dropped = cast(list[object], raw_dropped)
    assert dropped == [
        {
            "tool_name": "write_file",
            "status": "ok",
            "index": 1,
            "tool_call_id": "write-1",
            "path": "secret.txt",
            "estimated_tokens": context.continuity_state.dropped_tool_results[0].estimated_tokens,
        }
    ]
    assert "RAW SECRET CONTENT" not in str(dropped)


def test_prepare_provider_context_dropped_diagnostics_use_prepared_results() -> None:
    context = prepare_provider_context(
        prompt="search",
        tool_results=(
            ToolResult(tool_name="grep", status="ok", content="x" * 200, data={"index": 1}),
            ToolResult(tool_name="grep", status="ok", content="latest", data={"index": 2}),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            recent_tool_result_count=1,
            per_tool_result_tokens={"grep": 30},
        ),
    )

    assert context.continuity_state is not None
    (diagnostic,) = context.continuity_state.dropped_tool_results
    assert diagnostic.truncated is True
    assert diagnostic.partial is True
    assert diagnostic.metadata_payload()["truncated"] is True


def test_continuity_state_metadata_payload_round_trips_v2_fields() -> None:
    state = RuntimeContinuityState(
        summary_text="summary",
        objective="ship feature",
        current_goal="fix tests",
        verbatim_user_constraints=("Never drop raw output",),
        progress_completed=("Implemented digest",),
        blockers_open_questions=("Need review",),
        key_decisions=("Use runtime-owned summary",),
        relevant_files_commands_errors=("src/voidcode/runtime/context_window.py",),
        verification_state=("pytest passed",),
        delegated_task_summaries=("task_id=task-1 child_session_id=session-1",),
        recent_tail=("background_output ok",),
        dropped_tool_result_count=2,
        retained_tool_result_count=1,
        source="tool_result_window",
        distillation_source="deterministic",
        distillation_error=None,
        fact_reference_count=0,
        dropped_tool_results=(
            DroppedToolResultDiagnostic(
                tool_name="read_file",
                status="ok",
                index=1,
                tool_call_id="read-1",
                path="sample.txt",
                estimated_tokens=12,
            ),
        ),
        version=2,
    )

    restored = continuity_state_from_metadata_payload(state.metadata_payload())

    assert restored == state


def test_prepare_provider_context_continuity_state_exposes_distillation_source_metadata() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _continuity_tool_result("ok", content="dropped"),
            _continuity_tool_result("ok", content="retained"),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(max_tool_results=1),
    )

    assert context.continuity_state is not None
    payload = context.continuity_state.metadata_payload()
    assert payload["distillation_source"] == "deterministic"
    assert payload["fact_reference_count"] == 0


def test_prepare_provider_context_uses_model_assisted_distillation_candidate_when_enabled() -> None:
    candidate = {
        "objective_current_goal": "Ship continuity distillation",
        "verbatim_user_constraints": ["Do not override user instructions"],
        "completed_progress": ["Mapped runtime continuity flow"],
        "blockers_open_questions": ["Need integration tests"],
        "key_decisions_with_rationale": [
            {
                "text": "Use deterministic fallback",
                "rationale": "Preserve resilience",
                "refs": [{"kind": "event", "id": "event:42"}],
            }
        ],
        "relevant_files_commands_errors": [
            {
                "text": "src/voidcode/runtime/context_window.py",
                "kind": "file",
                "refs": [{"kind": "session", "id": "session:distill"}],
            }
        ],
        "verification_state": {
            "status": "pending",
            "details": ["tests not run"],
            "refs": [{"kind": "tool", "id": "tool:pytest"}],
        },
        "next_steps": ["Run tests"],
        "source_references": [{"kind": "session", "id": "session:distill"}],
    }

    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _continuity_tool_result("ok", content="drop-me"),
            _continuity_tool_result("ok", content="keep-me"),
        ),
        session_metadata={"runtime_state": {"distillation_candidate": candidate}},
        policy=ContextWindowPolicy(max_tool_results=1, continuity_distillation_enabled=True),
    )

    assert context.continuity_state is not None
    assert context.continuity_state.distillation_source == "model_assisted"
    assert context.continuity_state.current_goal == "Ship continuity distillation"
    assert context.continuity_state.fact_reference_count == 3
    assert context.continuity_state.source_references == (
        "session:session:distill",
        "event:event:42",
        "tool:tool:pytest",
    )


def test_prepare_provider_context_distillation_references_are_deduplicated() -> None:
    candidate = {
        "objective_current_goal": "Goal",
        "verbatim_user_constraints": ["constraint"],
        "completed_progress": ["progress"],
        "blockers_open_questions": ["blocker"],
        "key_decisions_with_rationale": [
            {
                "text": "decision",
                "rationale": "why",
                "refs": [{"kind": "event", "id": "event:1"}],
            }
        ],
        "relevant_files_commands_errors": [
            {
                "text": "file",
                "kind": "file",
                "refs": [{"kind": "event", "id": "event:1"}],
            }
        ],
        "verification_state": {
            "status": "pending",
            "details": ["pending"],
            "refs": [{"kind": "event", "id": "event:1"}],
        },
        "next_steps": ["next"],
        "source_references": [{"kind": "event", "id": "event:1"}],
    }

    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _continuity_tool_result("ok", content="drop-me"),
            _continuity_tool_result("ok", content="keep-me"),
        ),
        session_metadata={"runtime_state": {"distillation_candidate": candidate}},
        policy=ContextWindowPolicy(max_tool_results=1, continuity_distillation_enabled=True),
    )

    assert context.continuity_state is not None
    assert context.continuity_state.source_references == ("event:event:1",)
    assert context.continuity_state.fact_reference_count == 1


def test_prepare_provider_context_invalid_distillation_candidate_falls_back_safely() -> None:
    invalid_candidate = {
        "objective_current_goal": "",
        "verbatim_user_constraints": ["x"],
    }

    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _continuity_tool_result("ok", content="drop-me"),
            _continuity_tool_result("ok", content="keep-me"),
        ),
        session_metadata={"runtime_state": {"distillation_candidate": invalid_candidate}},
        policy=ContextWindowPolicy(max_tool_results=1, continuity_distillation_enabled=True),
    )

    assert context.continuity_state is not None
    assert context.continuity_state.distillation_source == "fallback_after_model_error"
    assert context.continuity_state.distillation_error is not None
    assert "failed schema validation" in context.continuity_state.distillation_error


def test_prepare_provider_context_distillation_candidate_ignores_raw_oversized_fields() -> None:
    large_data_uri = "data:image/png;base64," + ("A" * 8000)
    candidate = {
        "objective_current_goal": "Goal",
        "verbatim_user_constraints": ["constraint"],
        "completed_progress": ["progress"],
        "blockers_open_questions": ["blocker"],
        "key_decisions_with_rationale": [
            {
                "text": "decision",
                "rationale": "why",
                "refs": [{"kind": "event", "id": "event:1"}],
            }
        ],
        "relevant_files_commands_errors": [
            {
                "text": "cmd",
                "kind": "command",
                "refs": [{"kind": "tool", "id": "tool:1"}],
            }
        ],
        "verification_state": {
            "status": "pending",
            "details": ["pending"],
            "refs": [{"kind": "session", "id": "session:1"}],
        },
        "next_steps": ["next"],
        "source_references": [{"kind": "session", "id": "session:1"}],
        "raw_output": large_data_uri,
    }

    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content=large_data_uri,
                data={"data_uri": large_data_uri},
            ),
            _continuity_tool_result("ok", content="keep-me"),
        ),
        session_metadata={"runtime_state": {"distillation_candidate": candidate}},
        policy=ContextWindowPolicy(max_tool_results=1, continuity_distillation_enabled=True),
    )

    assert context.continuity_state is not None
    assert context.continuity_state.distillation_source == "model_assisted"
    assert "data:image" not in (context.continuity_state.summary_text or "")


def test_continuity_state_round_trip_includes_source_references() -> None:
    state = RuntimeContinuityState(
        summary_text="summary",
        dropped_tool_result_count=1,
        retained_tool_result_count=1,
        source="tool_result_window",
        distillation_source="model_assisted",
        fact_reference_count=2,
        source_references=("tool:call-1", "event:file:src/a.py"),
    )

    restored = continuity_state_from_metadata_payload(state.metadata_payload())
    assert restored is not None
    assert restored.source_references == ("tool:call-1", "event:file:src/a.py")


def test_normalize_read_file_output_preserves_showing_lines_footer() -> None:
    content = "\n".join(
        [
            "<path>sample.txt</path>",
            "<type>file</type>",
            "<content>",
            "10: alpha",
            "11: beta",
            "(Showing lines 10-11 of 20. Use offset=12 to continue.)",
            "</content>",
        ]
    )

    normalized = normalize_read_file_output(content)

    assert normalized == ("alpha\nbeta\n(Showing lines 10-11 of 20. Use offset=12 to continue.)")


def test_normalize_read_file_output_preserves_output_capped_footer() -> None:
    content = "\n".join(
        [
            "<path>sample.txt</path>",
            "<type>file</type>",
            "<content>",
            "1: alpha",
            "(Output capped at 50 KB. Showing lines 1-1. Use offset=2 to continue.)",
            "</content>",
        ]
    )

    normalized = normalize_read_file_output(content)

    assert normalized == (
        "alpha\n(Output capped at 50 KB. Showing lines 1-1. Use offset=2 to continue.)"
    )


def test_context_window_token_estimate_counts_raw_read_file_content() -> None:
    raw_content = "\n".join(
        [
            "<path>sample.txt</path>",
            "<type>file</type>",
            "<content>",
            "1: alpha",
            "2: beta",
            "(End of file - total 2 lines)",
            "</content>",
        ]
    )
    stripped_content = normalize_read_file_output(raw_content)
    policy = ContextWindowPolicy(
        auto_compaction=False,
        max_tool_result_tokens=100_000,
    )

    raw_context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(ToolResult(tool_name="read_file", status="ok", content=raw_content),),
        session_metadata={},
        policy=policy,
    )
    stripped_context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(ToolResult(tool_name="read_file", status="ok", content=stripped_content),),
        session_metadata={},
        policy=policy,
    )

    assert raw_context.original_tool_result_tokens is not None
    assert stripped_context.original_tool_result_tokens is not None
    assert raw_context.original_tool_result_tokens > stripped_context.original_tool_result_tokens


def test_artifact_placeholder_contract_projects_bounded_reference_for_omitted_output() -> None:
    raw_output = "artifact payload line\n" * 1_000
    artifact: dict[str, object] = {
        "artifact_id": "artifact_111111111111111111111111",
        "producer": "voidcode.tool_output.v1",
        "tool_call_id": "call-artifact-1",
        "tool_name": "shell_exec",
        "kind": "content",
        "byte_count": len(raw_output.encode("utf-8")),
        "line_count": 1_000,
        "status": "available",
    }
    assembled = assemble_provider_context(
        prompt="summarize artifact output",
        tool_results=(
            ToolResult(
                tool_name="shell_exec",
                status="ok",
                content=raw_output,
                data={
                    "index": 1,
                    "tool_call_id": "call-artifact-1",
                    "artifact": artifact,
                    "artifact_id": artifact["artifact_id"],
                    "artifact_status": "available",
                    "original_byte_count": artifact["byte_count"],
                    "original_line_count": artifact["line_count"],
                },
                truncated=True,
                partial=True,
                reference=f"artifact:{artifact['artifact_id']}",
            ),
            _tool_result(2),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
            importance_retention=False,
        ),
    )

    placeholder_segments = [
        segment
        for segment in assembled.segments
        if segment.metadata is not None
        and segment.metadata.get("source") == "runtime_context_artifact_reference"
    ]

    assert len(placeholder_segments) == 1
    placeholder = placeholder_segments[0]
    assert placeholder.role == "system"
    assert placeholder.tool_call_id is None
    assert placeholder.tool_name is None
    assert placeholder.content is not None
    assert len(placeholder.content) < 1_000
    assert "artifact_111111111111111111111111" in placeholder.content
    assert "call-artifact-1" in placeholder.content
    assert "byte_count=22000" in placeholder.content
    assert raw_output[:200] not in placeholder.content
    assert "artifact payload line\nartifact payload line" not in placeholder.content


def test_artifact_placeholder_contract_is_not_projected_as_fake_tool_result() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="grep",
                status="ok",
                content="RAW OMITTED MATCH\n" * 500,
                data={
                    "index": 1,
                    "tool_call_id": "grep-artifact-1",
                    "artifact_id": "artifact_222222222222222222222222",
                    "artifact": {
                        "artifact_id": "artifact_222222222222222222222222",
                        "producer": "voidcode.tool_output.v1",
                        "tool_call_id": "grep-artifact-1",
                        "tool_name": "grep",
                        "kind": "content",
                        "byte_count": 9_000,
                        "line_count": 500,
                        "status": "available",
                    },
                },
                truncated=True,
                partial=True,
                reference="artifact:artifact_222222222222222222222222",
            ),
            _tool_result(2),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
            importance_retention=False,
        ),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=3,
    )

    assert [segment.role for segment in assembled.segments].count("assistant") == 1
    assert [segment.role for segment in assembled.segments].count("tool") == 1
    assert all(message.tool_call_id != "grep-artifact-1" for message in snapshot.provider_messages)
    assert all(
        message.source != "provider_native_tool_result" or message.tool_call_id != "grep-artifact-1"
        for message in snapshot.provider_messages
    )
    assert any(
        message.role == "system"
        and message.source == "runtime_context_artifact_reference"
        and message.content is not None
        and "artifact_222222222222222222222222" in message.content
        for message in snapshot.provider_messages
    )


def test_artifact_placeholder_contract_provider_context_omits_raw_unbounded_output() -> None:
    raw_output = "unbounded artifact payload " * 2_000
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content=raw_output,
                data={
                    "index": 1,
                    "tool_call_id": "read-artifact-1",
                    "artifact_id": "artifact_333333333333333333333333",
                    "artifact": {
                        "artifact_id": "artifact_333333333333333333333333",
                        "producer": "voidcode.tool_output.v1",
                        "tool_call_id": "read-artifact-1",
                        "tool_name": "read_file",
                        "kind": "content",
                        "byte_count": len(raw_output.encode("utf-8")),
                        "line_count": 1,
                        "status": "available",
                    },
                },
                truncated=True,
                partial=True,
                reference="artifact:artifact_333333333333333333333333",
            ),
            _tool_result(2),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
            importance_retention=False,
        ),
    )
    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="opencode-go",
        model="glm-5",
        execution_engine="provider",
        available_tool_count=3,
        tool_feedback_mode="synthetic_user_message",
    )
    provider_text = "\n".join(message.content or "" for message in snapshot.provider_messages)

    assert "artifact_333333333333333333333333" in provider_text
    assert "read-artifact-1" in provider_text
    assert len(provider_text) < 4_000
    assert raw_output[:1_000] not in provider_text
    assert "unbounded artifact payload unbounded artifact payload" not in provider_text


def test_compact_projection_provider_context_preserves_message_invariants() -> None:
    raw_output = "omitted artifact payload " * 1_000
    assembled = assemble_provider_context(
        prompt="continue compact work",
        tool_results=(
            ToolResult(
                tool_name="shell_exec",
                status="ok",
                content=raw_output,
                data={
                    "tool_call_id": "artifact-call-1",
                    "artifact_id": "artifact_444444444444444444444444",
                    "artifact": {
                        "artifact_id": "artifact_444444444444444444444444",
                        "producer": "voidcode.tool_output.v1",
                        "tool_call_id": "artifact-call-1",
                        "tool_name": "shell_exec",
                        "kind": "content",
                        "byte_count": len(raw_output.encode("utf-8")),
                        "line_count": 1,
                        "status": "available",
                    },
                },
                truncated=True,
                partial=True,
                reference="artifact:artifact_444444444444444444444444",
            ),
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="retained content",
                data={"tool_call_id": "retained-call-1", "arguments": {"path": "sample.txt"}},
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=1,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=2,
    )
    diagnostic_codes = {diagnostic.code for diagnostic in snapshot.diagnostics}

    assert "missing_tool_result" not in diagnostic_codes
    assert "orphan_tool_result" not in diagnostic_codes
    assert "duplicate_tool_call_id" not in diagnostic_codes
    assert "compact_projection_wrong_role" not in diagnostic_codes
    compact_segments = [
        segment
        for segment in snapshot.segments
        if segment.source in {"continuity_summary", "runtime_context_artifact_reference"}
    ]
    assert compact_segments
    assert all(segment.role == "system" for segment in compact_segments)
    assert all(segment.tool_call_id is None for segment in compact_segments)
    assert all(segment.tool_name is None for segment in compact_segments)
    assert "omitted artifact payload omitted artifact payload" not in "\n".join(
        message.content or "" for message in snapshot.provider_messages
    )


def test_provider_context_inspector_reports_compact_projection_wrong_role() -> None:
    assembled = RuntimeAssembledContext(
        prompt="continue",
        tool_results=(),
        continuity_state=None,
        metadata={},
        segments=(
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id="fake-summary-call",
                tool_name="continuity_summary",
                metadata={"source": "continuity_summary"},
            ),
            RuntimeContextSegment(
                role="tool",
                content="Runtime artifact reference for omitted tool output",
                tool_call_id="fake-summary-call",
                tool_name="continuity_summary",
                metadata={"source": "runtime_context_artifact_reference", "status": "ok"},
            ),
        ),
    )

    snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=1,
    )

    wrong_role = [
        diagnostic
        for diagnostic in snapshot.diagnostics
        if diagnostic.code == "compact_projection_wrong_role"
    ]
    assert len(wrong_role) == 2
    assert {diagnostic.details["role"] for diagnostic in wrong_role} == {"assistant", "tool"}


# ── Projection contract tests ───────────────────────────────────────────────
# These encode the desired runtime context pipeline before production
# implementation: persisted truth stays complete, provider context receives
# bounded derived projection, score-driven retention is not the core policy,
# and there are no command-name-specific verification heuristics.


def test_projection_contract_continuity_state_preserves_full_dropped_record() -> None:
    """Continuity state must record every dropped result with correct tool_name,
    status, and index, preserving the complete session truth even when the
    provider only sees a bounded projection."""
    context = prepare_provider_context(
        prompt="inspect failing tests",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="content-1",
                data={"index": 1, "path": "src/a.py"},
            ),
            ToolResult(
                tool_name="shell_exec",
                status="error",
                error="exit code 1",
                data={"index": 2, "command": "pytest tests/"},
            ),
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="content-3",
                data={"index": 3, "path": "src/b.py"},
            ),
            ToolResult(
                tool_name="edit",
                status="ok",
                content="patched",
                data={"index": 4, "path": "src/b.py"},
            ),
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="content-5",
                data={"index": 5, "path": "src/c.py"},
            ),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=2,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert context.continuity_state is not None
    assert context.continuity_state.dropped_tool_result_count == 3
    assert context.continuity_state.retained_tool_result_count == 2
    assert len(context.continuity_state.dropped_tool_results) == 3

    # Every dropped result has a complete diagnostic entry
    for diagnostic in context.continuity_state.dropped_tool_results:
        payload = diagnostic.metadata_payload()
        assert isinstance(payload["tool_name"], str) and payload["tool_name"]
        assert isinstance(payload["status"], str) and payload["status"]
        assert isinstance(payload["index"], int) and payload["index"] >= 1


def test_projection_contract_structural_equivalence_across_shell_commands() -> None:
    """Two shell_exec results with equivalent content and status but different
    command names must receive the same retention treatment within a count-limited
    policy. There is no command-name-specific verification taxonomy."""
    # Identical structure: same content size, same status — only command differs
    shell_a = ToolResult(
        tool_name="shell_exec",
        content="x" * 40,
        status="ok",
        data={"index": 1, "command": "pytest tests/unit/test_sample.py -q"},
    )
    shell_b = ToolResult(
        tool_name="shell_exec",
        content="x" * 40,
        status="ok",
        data={"index": 2, "command": "echo hello world"},
    )

    context_a = prepare_provider_context(
        prompt="run tests",
        tool_results=(
            _tool_result(0),
            _tool_result(0),
            shell_a,
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=2,
            recent_tool_result_count=2,
            importance_retention=False,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )
    context_b = prepare_provider_context(
        prompt="run tests",
        tool_results=(
            _tool_result(0),
            _tool_result(0),
            shell_b,
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=2,
            recent_tool_result_count=2,
            importance_retention=False,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    # Both shell results should be retained equally — no command-name bias
    indices_a = [r.data["index"] for r in context_a.tool_results]
    indices_b = [r.data["index"] for r in context_b.tool_results]
    assert 1 in indices_a
    assert 2 in indices_b
    assert context_a.retained_tool_result_count == context_b.retained_tool_result_count


def test_projection_contract_importance_retention_equals_recency() -> None:
    """When importance_retention=True, retention behavior must match
    importance_retention=False: both must select purely by recency, not by
    tool-name or command-name scoring.

    This encodes the architectural decision that score-driven retention is
    NOT the core policy; importance_retention is compatibility-only recency
    behavior."""
    results: tuple[ToolResult, ...] = (
        ToolResult(
            tool_name="read_file",
            status="error",
            error="file not found",
            data={"index": 1, "path": "missing.py"},
        ),
        _tool_result(2),
        _tool_result(3),
        _tool_result(4),
        _tool_result(5),
    )

    policy_base = ContextWindowPolicy(
        max_tool_results=3,
        recent_tool_result_count=1,
        max_tool_result_tokens=None,
        recent_tool_result_tokens=None,
        default_tool_result_tokens=None,
    )

    # With importance_retention=True — the compat field
    context_importance = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=policy_base.max_tool_results,
            recent_tool_result_count=policy_base.recent_tool_result_count,
            max_tool_result_tokens=policy_base.max_tool_result_tokens,
            recent_tool_result_tokens=policy_base.recent_tool_result_tokens,
            default_tool_result_tokens=policy_base.default_tool_result_tokens,
            importance_retention=True,
        ),
    )

    # With importance_retention=False — explicit recency-only
    context_recency = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=policy_base.max_tool_results,
            recent_tool_result_count=policy_base.recent_tool_result_count,
            max_tool_result_tokens=policy_base.max_tool_result_tokens,
            recent_tool_result_tokens=policy_base.recent_tool_result_tokens,
            default_tool_result_tokens=policy_base.default_tool_result_tokens,
            importance_retention=False,
        ),
    )

    # Contract: both policies must produce identical retained results.
    importance_indices = tuple(result.data["index"] for result in context_importance.tool_results)
    recency_indices = tuple(result.data["index"] for result in context_recency.tool_results)
    assert importance_indices == recency_indices, (
        "importance_retention=True must behave identically to "
        "importance_retention=False (recency-only). "
        f"Got importance={importance_indices}, recency={recency_indices}"
    )


def test_projection_contract_score_driven_tool_name_bias_not_acceptable() -> None:
    """Tool name categories (write_file, todo_write, shell_exec, etc.) must
    not influence retention priority. A write_file result at position 2 must
    not displace a read_file result at position 3 just because write_file
    had a higher score."""
    results: tuple[ToolResult, ...] = (
        _tool_result(1),
        ToolResult(
            tool_name="write_file",
            content="written",
            status="ok",
            data={"index": 2, "path": "src/app.py"},
        ),
        _tool_result(3),
        _tool_result(4),
        _tool_result(5),
    )

    context = prepare_provider_context(
        prompt="fix code",
        tool_results=results,
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=None,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
            importance_retention=True,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)

    # Contract: retention should follow the ordered tail, not tool-name scoring.
    # The most recent three results (3, 4, 5) should be retained, with index 5
    # always included as the protected recent. Index 2 (write_file) should NOT
    # jump ahead of index 3 or 4 just because its tool name scores higher.
    assert 5 in retained_indexes, "most recent result (protected) must always be retained"
    assert retained_indexes == (3, 4, 5), (
        "retention must be recency-based, not tool-name scored. "
        f"Got {retained_indexes}, expected (3, 4, 5)"
    )


def test_projection_contract_token_budget_uses_recency_not_scoring() -> None:
    """When a token budget constrains selection, the ranking must prefer recent
    results over scoring by tool name. Older high-scoring results must not
    displace newer results within budget."""
    context = prepare_provider_context(
        prompt="verify fix",
        tool_results=(
            _sized_tool_result(1, content_size=160),
            ToolResult(
                tool_name="shell_exec",
                content="passed",
                status="ok",
                data={"index": 2, "command": "python -m pytest"},
            ),
            _sized_tool_result(3, content_size=160),
            _tool_result(4),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_tool_results=3,
            recent_tool_result_count=1,
            max_tool_result_tokens=120,
            recent_tool_result_tokens=None,
            default_tool_result_tokens=None,
            importance_retention=True,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)

    # Contract: index 4 (most recent) is always retained as protected.
    # The remaining slots should be filled from newest to oldest within budget,
    # not based on shell_exec scoring higher than read_file.
    # Current scoring: shell_exec(index 2) gets +30, potentially displacing
    # newer read_file results. This must not happen.
    assert 4 in retained_indexes, "most recent result must always be retained"
    # Newest results (3, 4) should be preferred. index 1 should not survive
    # over index 3 just because it's more recent than 2 (which gets scored higher).
    assert 3 in retained_indexes, (
        f"newer results should be preferred over tool-name scoring. Got {retained_indexes}"
    )
    # Older index 1 should be dropped; it's not protected and not recent enough
    assert 1 not in retained_indexes, (
        f"oldest result (index 1) must be dropped in favor of newer results. Got {retained_indexes}"
    )


def test_projection_contract_default_policy_does_not_require_importance_retention() -> None:
    """The default ContextWindowPolicy must NOT depend on importance_retention
    for correct behavior. A policy with importance_retention defaults produces
    the same projection result as a policy where importance_retention is
    explicitly False, for identical input."""
    default_policy = ContextWindowPolicy(
        max_tool_results=3,
        recent_tool_result_count=1,
        max_tool_result_tokens=None,
        recent_tool_result_tokens=None,
        default_tool_result_tokens=None,
    )
    explicit_policy = ContextWindowPolicy(
        max_tool_results=3,
        recent_tool_result_count=1,
        max_tool_result_tokens=None,
        recent_tool_result_tokens=None,
        default_tool_result_tokens=None,
        importance_retention=False,
    )

    results: tuple[ToolResult, ...] = (
        _tool_result(1),
        ToolResult(
            tool_name="read_file",
            status="error",
            error="missing",
            data={"index": 2, "path": "missing.py"},
        ),
        _tool_result(3),
        _tool_result(4),
        _tool_result(5),
    )

    context_default = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=results,
        session_metadata={},
        policy=default_policy,
    )
    context_explicit = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=results,
        session_metadata={},
        policy=explicit_policy,
    )

    default_indices = tuple(r.data["index"] for r in context_default.tool_results)
    explicit_indices = tuple(r.data["index"] for r in context_explicit.tool_results)
    assert default_indices == explicit_indices, (
        "default policy must produce same results as importance_retention=False. "
        f"Got default={default_indices}, explicit={explicit_indices}"
    )


def test_projection_contract_projection_preserves_original_count_in_metadata() -> None:
    """The RuntimeContextWindow metadata must record the original tool result
    count and retained count, so consumers can distinguish the projection
    from the full truth."""
    projection = project_tool_results_for_context_window(
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3), _tool_result(4)),
        policy=ContextWindowPolicy(max_tool_results=2),
    )

    assert len(projection.prepared_results) == 4  # full set after truncation only
    assert len(projection.retained_results) == 2  # bounded projection
    assert len(projection.dropped_results) == 2  # what was excluded
    assert len(projection.retained_indexes) == 2
    assert len(projection.dropped_indexes) == 2
    assert set(projection.retained_indexes) | set(projection.dropped_indexes) == {0, 1, 2, 3}
    assert projection.truncated_count >= 0
