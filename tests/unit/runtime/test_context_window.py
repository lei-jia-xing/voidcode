from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Literal, cast
from unittest.mock import patch

from voidcode.runtime.context_window import (
    ContextWindowPolicy,
    DroppedToolResultDiagnostic,
    RuntimeAssembledContext,
    RuntimeContextSegment,
    RuntimeContinuityState,
    assemble_provider_context,
    context_window_policy_from_payload,
    continuity_state_from_metadata_payload,
    continuity_summary_metadata,
    count_text_tokens,
    normalize_read_file_output,
    prepare_provider_context,
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


def test_context_window_policy_default_retains_more_tool_results_before_compaction() -> None:
    policy = ContextWindowPolicy()
    context = prepare_provider_context(
        prompt="continue coding task",
        tool_results=tuple(_tool_result(index) for index in range(1, 8)),
        session_metadata={},
        policy=policy,
    )

    assert policy.max_tool_results == 8
    assert context.compacted is False
    assert context.retained_tool_result_count == 7


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
    tool_data = tool_segment.metadata["data"]
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


def test_provider_context_policy_marks_blocking_diagnostics() -> None:
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
                content="x" * 12,
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
        oversized_tool_feedback_chars=5,
        diagnostic_policy_mode="block",
    )

    assert snapshot.policy_decision is not None
    assert snapshot.policy_decision.blocked is True
    assert snapshot.policy_decision.action == "block"
    assert set(snapshot.policy_decision.blocking_diagnostic_codes) >= {
        "missing_tool_result",
        "orphan_tool_result",
        "oversized_tool_feedback",
    }
    blocking_codes = {
        diagnostic.code for diagnostic in snapshot.diagnostics if diagnostic.policy_blocking
    }
    assert {
        "missing_tool_result",
        "orphan_tool_result",
        "oversized_tool_feedback",
    } <= blocking_codes


def test_provider_context_policy_off_keeps_diagnostics_debug_only() -> None:
    assembled = RuntimeAssembledContext(
        prompt="continue",
        tool_results=(),
        continuity_state=None,
        metadata={},
        segments=(
            RuntimeContextSegment(role="user", content="continue"),
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
        available_tool_count=3,
        diagnostic_policy_mode="off",
    )

    assert snapshot.policy_decision is not None
    assert snapshot.policy_decision.action == "ignored"
    assert snapshot.policy_decision.blocked is False
    assert any(diagnostic.code == "orphan_tool_result" for diagnostic in snapshot.diagnostics)


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
    continuity_payload = context.metadata_payload()["continuity_state"]
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
            _sized_tool_result(1, content_size=160),
            _sized_tool_result(2, content_size=32),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            max_context_ratio=0.1,
            model_context_window_tokens=500,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (2,)
    assert context.token_budget == 50
    assert context.retained_tool_result_tokens is not None
    assert context.retained_tool_result_tokens <= 50


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
        policy=ContextWindowPolicy(max_tool_results=2),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (2, 3)
    assert context.token_budget is None
    assert context.original_tool_result_tokens is None


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
            _sized_tool_result(1, content_size=240),
            _sized_tool_result(2, content_size=20),
        ),
        session_metadata={},
        policy=ContextWindowPolicy(
            model_context_window_tokens=120,
            reserved_output_tokens=40,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (2,)
    assert context.token_budget == 80
    assert context.reserved_output_tokens == 40
    assert context.metadata_payload()["reserved_output_tokens"] == 40


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
        reserved_output_tokens=20,
        recent_tool_result_count=2,
        default_tool_result_tokens=30,
        per_tool_result_tokens={"grep": 10},
        tokenizer_model="gpt-4o",
    )

    parsed = context_window_policy_from_payload(policy.metadata_payload())

    assert parsed == policy


def test_continuity_summary_metadata_is_derived_from_state() -> None:
    first = RuntimeContinuityState(
        summary_text="one",
        dropped_tool_result_count=1,
        retained_tool_result_count=3,
    )
    second = RuntimeContinuityState(
        summary_text="one",
        dropped_tool_result_count=2,
        retained_tool_result_count=3,
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
            ToolResult(tool_name="list", status="ok", content="secret.txt", data={}),
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
