from __future__ import annotations

import sys
from types import ModuleType
from typing import Literal
from unittest.mock import patch

from voidcode.runtime.context_window import (
    ContextWindowPolicy,
    RuntimeContinuityState,
    assemble_provider_context,
    context_window_policy_from_payload,
    continuity_state_from_metadata_payload,
    continuity_summary_metadata,
    count_text_tokens,
    normalize_read_file_output,
    prepare_provider_context,
)
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
    assert "## Objective\nread sample.txt" in context.continuity_state.summary_text
    assert "Compacted 3 earlier tool results:" in context.continuity_state.summary_text
    assert '1. read_file ok content_preview="conte..."' in context.continuity_state.summary_text
    assert "... and 2 more" in context.continuity_state.summary_text


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
