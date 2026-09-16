from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast
from unittest.mock import patch

import pytest

from voidcode.runtime.context.provider import inspect_provider_context
from voidcode.runtime.context.transforms import (
    HookPresetGuidanceTransformProvider,
    RuntimeContextTransformInjection,
    RuntimeContextTransformRegistry,
    RuntimeContextTransformRequest,
    RuntimeContextTransformResult,
    RuntimeFileRulesTransformProvider,
)
from voidcode.runtime.context.window import (
    ContextProjection,
    ContextWindowPolicy,
    DroppedToolResultDiagnostic,
    RuntimeAssembledContext,
    RuntimeContextSegment,
    ToolResultView,
    assemble_provider_context,
    continuity_state_from_metadata_payload,
    continuity_summary_metadata,
    normalize_read_output,
    prepare_provider_context,
)
from voidcode.runtime.context.window_policy import (
    context_window_config_from_policy,
    context_window_policy_from_config,
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


def _context_window_policy(**overrides: object) -> Any:
    overrides.pop("auto_compaction", None)
    return ContextWindowPolicy(**cast(Any, overrides))


def _tool_result(index: int) -> ToolResult:
    return ToolResult(
        tool_name="read",
        content=f"content-{index}",
        status="ok",
        data={"index": index},
    )


def _sized_tool_result(index: int, *, content_size: int) -> ToolResult:
    return ToolResult(
        tool_name="read",
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


def test_context_window_policy_default_uses_character_cap() -> None:
    policy = _context_window_policy()
    context = prepare_provider_context(
        prompt="continue coding task",
        tool_results=tuple(_tool_result(index) for index in range(1, 8)),
        session_metadata={},
        policy=policy,
    )

    assert policy.default_tool_result_chars == 6_000
    assert context.compacted is False
    assert context.retained_tool_result_count == 7


def test_prepare_provider_context_default_policy_truncates_large_tool_results() -> None:
    large_content = "x" * 20_000

    context = prepare_provider_context(
        prompt="inspect large file",
        tool_results=(
            ToolResult(
                tool_name="read",
                status="ok",
                content=large_content,
                data={"path": "large.txt"},
            ),
        ),
        session_metadata={},
        policy=_context_window_policy(),
    )

    (result,) = context.tool_results
    assert result.truncated is True
    assert result.partial is True
    assert result.content is not None
    assert len(result.content) < len(large_content)
    assert context.truncated_tool_result_count >= 0


def test_prepare_provider_context_keeps_results_within_limit() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2)),
        session_metadata={},
        policy=_context_window_policy(default_tool_result_chars=100),
    )

    assert context.prompt == "read sample.txt"
    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2)
    assert context.compacted is False
    assert context.compaction_reason is None
    assert context.original_tool_result_count == 2
    assert context.retained_tool_result_count == 2
    assert context.continuity_state is None


def test_assemble_provider_context_second_stage_preserves_non_recent_tiers() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 2,
                    "revision": 12,
                    "phases": [{"name": "Tasks", "tasks": [{"content": "must survive compaction", "status": "in_progress"}]}],
                    "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 1},
                }
            }
        },
        agent_prompt_context="A" * 400,
        policy=_context_window_policy(
            default_tool_result_chars=20,
        ),
    )

    assert any((segment.metadata or {}).get("source") == "runtime_todo_state" for segment in assembled.segments)
    assert any((segment.metadata or {}).get("source") == "runtime_instruction_precedence" for segment in assembled.segments)


def test_assemble_provider_context_injects_active_runtime_todos() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 2,
                    "revision": 12,
                    "phases": [
                        {
                            "name": "Tasks",
                            "tasks": [
                                {"content": "implement runtime todo state", "status": "in_progress"},
                                {"content": "old finished task", "status": "completed"},
                            ],
                        }
                    ],
                    "summary": {"total": 2, "pending": 0, "in_progress": 1, "completed": 1, "abandoned": 0, "blocked": 0, "active": 1},
                }
            }
        },
        policy=_context_window_policy(default_tool_result_chars=1),
    )
    system_segments = [segment.content for segment in assembled.segments if segment.role == "system"]
    assert any(
        isinstance(content, str)
        and "Runtime-managed todo state is active" in content
        and "implement runtime todo state" in content
        and "old finished task" not in content
        for content in system_segments
    )
    system_metadata = [segment.metadata for segment in assembled.segments if segment.role == "system" and segment.metadata is not None]
    assert {metadata["source"] for metadata in system_metadata} >= {
        "runtime_base_safety",
        "runtime_instruction_precedence",
        "runtime_tool_policy_summary",
        "runtime_todo_state",
    }
    todo_metadata = next(metadata for metadata in system_metadata if metadata["source"] == "runtime_todo_state")
    assert todo_metadata == {"source": "runtime_todo_state", "tier": "task", "layer": "task_state"}


def test_assemble_provider_context_keeps_todo_result_without_active_todo_state() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(ToolResult(tool_name="todo", content="todo result", status="ok", data={}),),
        session_metadata={},
    )

    assert any(segment.tool_name == "todo" and segment.role == "tool" for segment in assembled.segments)


def test_assemble_provider_context_bounds_replayed_tool_content() -> None:
    replayed = RuntimeContextSegment(
        role="tool",
        content="x" * 200,
        tool_name="read",
        tool_call_id="replayed-read",
        metadata={"source": "replayed_conversation", "tier": "recent"},
    )
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={},
        replayed_conversation_segments=(replayed,),
        policy=_context_window_policy(default_tool_result_chars=10),
    )

    replayed_tools = [
        segment
        for segment in assembled.segments
        if segment.metadata and segment.metadata.get("source") == "replayed_conversation" and segment.role == "tool"
    ]
    assert len(replayed_tools) == 1
    assert replayed_tools[0].metadata is not None
    assert replayed_tools[0].metadata["truncated"] is True
    assert replayed_tools[0].metadata["partial"] is True
    assert len(replayed_tools[0].content or "") < 200


def test_assemble_provider_context_records_explicit_context_tiers() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="read",
                status="ok",
                content="alpha",
                data={"tool_call_id": "call-1", "arguments": {"path": "src/app.py"}},
            ),
        ),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 2,
                    "revision": 12,
                    "phases": [{"name": "Tasks", "tasks": [{"content": "implement tiers", "status": "in_progress"}]}],
                    "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 1},
                }
            }
        },
        skill_prompt_context="skill context",
        policy=_context_window_policy(default_tool_result_chars=100),
    )
    assert assembled.metadata["context_tiers"] == {
        "version": 1,
        "order": ["instruction", "task", "recent"],
        "counts": {"instruction": 5, "workspace": 0, "task": 2, "recent": 2},
    }
    assert assembled.metadata["context_tier_policy"] == {
        "version": 1,
        "protected_tiers": ["instruction", "workspace", "task"],
        "compaction_target": "recent",
    }
    assert [(segment.metadata or {}).get("tier") for segment in assembled.segments[:4]] == ["instruction"] * 4


def test_assemble_provider_context_injects_file_rules_from_tool_paths(tmp_path: Any) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("Project rules", encoding="utf-8")
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "AGENTS.md").write_text("Runtime rules", encoding="utf-8")

    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="read",
                status="ok",
                content="content",
                data={"path": "src/app.py", "arguments": {"path": "src/app.py"}},
            ),
        ),
        session_metadata={},
        workspace=workspace,
        policy=_context_window_policy(default_tool_result_chars=100),
    )

    rule_segments = [
        segment for segment in assembled.segments if segment.metadata is not None and segment.metadata.get("source") == "runtime_file_rules"
    ]
    assert [(segment.metadata or {})["path"] for segment in rule_segments] == [
        "AGENTS.md",
        "src/AGENTS.md",
    ]
    assert "Project rules" in (rule_segments[0].content or "")
    assert "Runtime rules" in (rule_segments[1].content or "")
    assert assembled.metadata["context_transforms"] == {
        "version": 1,
        "failure_policy": "warn",
        "applied": [
            {
                "provider_id": "runtime_file_rules",
                "status": "ok",
                "priority": 200,
                "execution_index": 3,
                "injection_count": 2,
                "provider_order": [
                    "hook_preset_guidance",
                    "mode_guidance",
                    "runtime_file_rules",
                ],
                "sources": ["runtime_file_rules"],
            }
        ],
    }


def test_assemble_provider_context_tracks_hook_preset_guidance_transform() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={},
        hook_preset_context="Resolved agent hook preset guidance.",
        policy=_context_window_policy(default_tool_result_chars=100),
    )

    hook_segments = [
        segment for segment in assembled.segments if segment.metadata is not None and segment.metadata.get("source") == "hook_preset_guidance"
    ]
    assert len(hook_segments) == 1
    assert hook_segments[0].content == "Resolved agent hook preset guidance."
    assert assembled.metadata["context_transforms"] == {
        "version": 1,
        "failure_policy": "warn",
        "applied": [
            {
                "provider_id": "hook_preset_guidance",
                "status": "ok",
                "priority": 100,
                "execution_index": 1,
                "injection_count": 1,
                "provider_order": [
                    "hook_preset_guidance",
                    "mode_guidance",
                    "runtime_file_rules",
                ],
                "sources": ["hook_preset_guidance"],
            }
        ],
    }


def test_context_transform_registry_combines_multiple_providers(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("Project rules", encoding="utf-8")
    registry = RuntimeContextTransformRegistry(
        providers=(
            HookPresetGuidanceTransformProvider(),
            RuntimeFileRulesTransformProvider(),
        )
    )

    result = registry.build_result(
        RuntimeContextTransformRequest(
            workspace=workspace,
            tool_results=(),
            hook_preset_context="Resolved agent hook preset guidance.",
        )
    )

    assert [injection.metadata["source"] for injection in result.injections] == [
        "hook_preset_guidance",
        "runtime_file_rules",
    ]
    assert result.metadata_payload() == {
        "version": 1,
        "failure_policy": "warn",
        "applied": [
            {
                "provider_id": "hook_preset_guidance",
                "status": "ok",
                "priority": 100,
                "execution_index": 1,
                "injection_count": 1,
                "provider_order": ["hook_preset_guidance", "runtime_file_rules"],
                "sources": ["hook_preset_guidance"],
            },
            {
                "provider_id": "runtime_file_rules",
                "status": "ok",
                "priority": 200,
                "execution_index": 2,
                "injection_count": 1,
                "provider_order": ["hook_preset_guidance", "runtime_file_rules"],
                "sources": ["runtime_file_rules"],
            },
        ],
    }


def test_context_transform_registry_orders_providers_by_priority(tmp_path: Path) -> None:
    class HighPriorityRulesProvider(RuntimeFileRulesTransformProvider):
        priority = 50

    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("Project rules", encoding="utf-8")
    registry = RuntimeContextTransformRegistry(
        providers=(
            HookPresetGuidanceTransformProvider(),
            HighPriorityRulesProvider(),
        )
    )

    result = registry.build_result(
        RuntimeContextTransformRequest(
            workspace=workspace,
            tool_results=(),
            hook_preset_context="Resolved agent hook preset guidance.",
        )
    )

    assert [trace.provider_id for trace in result.traces] == [
        "runtime_file_rules",
        "hook_preset_guidance",
    ]
    assert [trace.execution_index for trace in result.traces] == [1, 2]
    assert [trace.priority for trace in result.traces] == [50, 100]


def test_assemble_provider_context_uses_full_tool_history_for_rules_not_compacted_window(
    tmp_path: Path,
) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("Project rules", encoding="utf-8")
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "AGENTS.md").write_text("Runtime rules", encoding="utf-8")

    # 8 results: 6 path-bearing, then 2 non-path. Compaction retains 4.
    # Full tool history (not compacted window) must still inject rules.
    results: list[ToolResult] = []
    for i in range(6):
        results.append(
            ToolResult(
                tool_name="read" if i % 2 == 0 else "edit",
                status="ok",
                content="content",
                data={"path": "src/module.py"},
            )
        )
    results.append(ToolResult(tool_name="web_search", status="ok", content="sr1", data={}))
    results.append(ToolResult(tool_name="web_search", status="ok", content="sr2", data={}))

    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=tuple(results),
        session_metadata={},
        workspace=workspace,
        policy=_context_window_policy(default_tool_result_chars=100),
    )

    rule_segments = [
        segment for segment in assembled.segments if segment.metadata is not None and segment.metadata.get("source") == "runtime_file_rules"
    ]
    paths = [(segment.metadata or {}).get("path") for segment in rule_segments]
    assert "AGENTS.md" in paths
    assert "src/AGENTS.md" in paths


def test_assemble_provider_context_uses_runtime_todos_as_single_authority() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="todo",
                content="Updated 1 todos\n1. [pending/low] stale tool feedback",
                status="ok",
                data={
                    "tool_call_id": "todo-old",
                    "arguments": {"op": "view"},
                    "phases": [{"name": "Tasks", "tasks": [{"content": "stale tool feedback", "status": "pending"}]}],
                },
            ),
            ToolResult(
                tool_name="read",
                content="current code",
                status="ok",
                data={"tool_call_id": "read-1", "arguments": {"path": "src/app.py"}},
            ),
        ),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 2,
                    "revision": 2,
                    "phases": [{"name": "Tasks", "tasks": [{"content": "authoritative runtime state", "status": "in_progress"}]}],
                    "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 1},
                }
            }
        },
        policy=_context_window_policy(default_tool_result_chars=100),
    )
    assert [segment.tool_name for segment in assembled.segments if segment.role == "tool"] == ["read"]
    system_text = "\n".join(str(segment.content) for segment in assembled.segments if segment.role == "system")
    assert "authoritative runtime state" in system_text
    assert "stale tool feedback" not in system_text
    assert "Current active todos:" in system_text


def test_assemble_provider_context_injects_pending_approval_state() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "plan_state": {
                "status": "waiting_approval",
                "approval_request_id": "approval-123",
                "blocked_tool": "write",
            }
        },
        policy=_context_window_policy(default_tool_result_chars=100),
    )

    pending_segments = [
        segment for segment in assembled.segments if segment.metadata is not None and segment.metadata.get("source") == "runtime_pending_state"
    ]
    assert len(pending_segments) == 1
    assert pending_segments[0].content is not None
    assert "waiting_approval" in pending_segments[0].content
    assert "approval-123" in pending_segments[0].content
    assert "write" in pending_segments[0].content
    assert "runtime resume" in pending_segments[0].content
    assert pending_segments[0].metadata == {
        "source": "runtime_pending_state",
        "tier": "task",
        "layer": "task_state",
        "status": "waiting_approval",
        "blocked_tool": "write",
        "approval_request_id": "approval-123",
    }


def test_assemble_provider_context_skill_todo_transform_content_present() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 2,
                    "revision": 1,
                    "phases": [{"name": "Tasks", "tasks": [{"content": "implement feature", "status": "in_progress"}]}],
                    "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 1},
                }
            }
        },
        skill_prompt_context="skill guidance text",
        context_transform_result=RuntimeContextTransformResult(
            injections=(
                RuntimeContextTransformInjection(
                    role="system",
                    content="transform injected",
                    metadata={"source": "transform_test", "tier": "workspace"},
                ),
            )
        ),
        policy=_context_window_policy(default_tool_result_chars=100),
    )

    skill_segments = [s for s in assembled.segments if s.metadata is not None and s.metadata.get("source") == "skill_prompt"]
    assert len(skill_segments) == 1
    assert "skill guidance text" in (skill_segments[0].content or "")

    todo_segments = [s for s in assembled.segments if s.metadata is not None and s.metadata.get("source") == "runtime_todo_state"]
    assert len(todo_segments) >= 1
    todo_text = "\n".join(str(s.content) for s in todo_segments if s.content)
    assert "implement feature" in todo_text

    transform_segments = [s for s in assembled.segments if s.metadata is not None and s.metadata.get("source") == "transform_test"]
    assert len(transform_segments) == 1
    assert "transform injected" in (transform_segments[0].content or "")

    tiers = cast(dict[str, object], assembled.metadata.get("context_tiers", {}))
    counts = cast(dict[str, int], tiers.get("counts", {}))
    assert counts.get("task", 0) >= 1
    assert counts.get("instruction", 0) >= 1
    assert counts.get("workspace", 0) >= 1


def test_assemble_provider_context_injects_pending_question_state() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "plan_state": {
                "status": "waiting_question",
                "blocked_tool": "question",
            }
        },
        policy=_context_window_policy(default_tool_result_chars=100),
    )

    pending_segments = [
        segment for segment in assembled.segments if segment.metadata is not None and segment.metadata.get("source") == "runtime_pending_state"
    ]
    assert len(pending_segments) == 1
    assert pending_segments[0].content is not None
    assert "waiting_question" in pending_segments[0].content
    assert "pending question" in pending_segments[0].content
    assert "question" in pending_segments[0].content
    assert pending_segments[0].metadata == {
        "source": "runtime_pending_state",
        "tier": "task",
        "layer": "task_state",
        "status": "waiting_question",
        "blocked_tool": "question",
    }


def test_provider_context_inspector_reports_synthetic_feedback_mode() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="read",
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
        policy=_context_window_policy(default_tool_result_chars=100),
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
    assert any(diagnostic.code == "provider_path_uses_synthetic_tool_feedback" for diagnostic in snapshot.diagnostics)


def test_provider_context_inspector_strips_sentinels_from_provider_messages() -> None:
    raw_todo_content = "Secret todo content should not appear in provider messages"
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="todo",
                content="Updated todos",
                status="ok",
                data={
                    "tool_call_id": "call:todo",
                    "arguments": {
                        "op": "init",
                        "items": [raw_todo_content],
                    },
                },
            ),
        ),
        session_metadata={},
        policy=_context_window_policy(default_tool_result_chars=100),
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
    assert '"items": [""]' in provider_arguments
    assert '"omitted": true' not in provider_arguments
    assert '"byte_count"' not in provider_arguments


def test_provider_context_inspector_redacts_secret_text_from_tool_output() -> None:
    assembled = assemble_provider_context(
        prompt="inspect env",
        tool_results=(
            ToolResult(
                tool_name="read",
                content=("OPENAI_API_KEY=sk-test-secret\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz"),
                status="ok",
                data={"tool_call_id": "call:secret", "arguments": {"path": ".env"}},
            ),
        ),
        session_metadata={},
        policy=_context_window_policy(default_tool_result_chars=100),
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
        policy=_context_window_policy(default_tool_result_chars=100),
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
                tool_name="read",
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
                tool_name="read",
            ),
            RuntimeContextSegment(
                role="tool",
                content="first",
                tool_call_id="duplicate-result",
                tool_name="read",
                metadata={"status": "ok", "data": {}},
            ),
            RuntimeContextSegment(
                role="tool",
                content="second",
                tool_call_id="duplicate-result",
                tool_name="read",
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

    duplicate = [diagnostic for diagnostic in snapshot.diagnostics if diagnostic.code == "duplicate_tool_call_id"]
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
                tool_name="read",
            ),
            RuntimeContextSegment(
                role="tool",
                content="x" * 32,
                tool_call_id="large-result",
                tool_name="read",
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

    oversized = [diagnostic for diagnostic in snapshot.diagnostics if diagnostic.code == "oversized_tool_feedback"]
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
            tool_name="read",
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
            tool_name="todo",
            status="ok",
            content="Todo view applied.",
            data={
                "tool_call_id": "todo-1",
                "arguments": {"op": "view"},
                "phases": [{"name": "Tasks", "tasks": [{"content": "preserve context parity", "status": "in_progress"}]}],
                "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 1},
                "op": "view",
                "mutated": False,
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
            tool_name="task",
            status="ok",
            content="Task Result\n\nTask ID: bg_123\nSummary: child done",
            data={
                "tool_call_id": "background-1",
                "arguments": {"operation": "output", "task_id": "bg_123"},
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
                    "version": 2,
                    "revision": 1,
                    "phases": [{"name": "Tasks", "tasks": [{"content": "preserve context parity", "status": "in_progress"}]}],
                    "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 1},
                }
            }
        },
        policy=_context_window_policy(auto_compaction=False, default_tool_result_chars=100_000),
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
        policy=_context_window_policy(auto_compaction=False, default_tool_result_chars=100_000),
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
    tool_messages = [message for message in standard_snapshot.provider_messages if message.role == "tool"]
    expected_tool_results = tuple(result for result in tool_results if result.tool_name != "todo")
    assert [segment.tool_name for segment in tool_segments] == [result.tool_name for result in expected_tool_results]
    assert len(tool_messages) == len(expected_tool_results)
    for result, segment, message in zip(expected_tool_results, tool_segments, tool_messages, strict=True):
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

    todo_system_segments = [segment for segment in standard_snapshot.segments if segment.role == "system" and segment.source == "runtime_todo_state"]
    assert len(todo_system_segments) == 1
    assert "preserve context parity" in (todo_system_segments[0].content or "")
    synthetic_feedback = synthetic_snapshot.provider_messages[-1].content or ""
    assert synthetic_snapshot.provider_messages[-1].source == "provider_synthetic_tool_feedback"
    for result in synthetic_tool_results:
        assert synthetic_feedback.count(f'"tool_name": "{result.tool_name}"') == 1
    assert "1: alpha" in synthetic_feedback
    assert "child done" in synthetic_feedback
    assert '"tool_name": "todo"' not in synthetic_feedback
    assert any(diagnostic.code == "provider_path_uses_synthetic_tool_feedback" for diagnostic in synthetic_snapshot.diagnostics)


def test_prepare_provider_context_truncates_old_tool_outputs_by_tool_policy() -> None:
    context = prepare_provider_context(
        prompt="search",
        tool_results=(
            ToolResult(tool_name="grep", status="ok", content="x" * 200, data={"index": 1}),
            ToolResult(tool_name="grep", status="ok", content="latest" * 20, data={"index": 2}),
        ),
        session_metadata={},
        policy=_context_window_policy(
            default_tool_result_chars=50,
            per_tool_result_chars={"grep": 30},
        ),
    )

    older, latest = context.tool_results
    assert older.truncated is True
    assert older.content is not None
    assert len(older.content) < 200
    assert latest.truncated is True
    assert latest.content is not None
    assert len(latest.content) <= 30
    assert context.retained_tool_result_count == 2
    assert context.compacted is False
    assert context.truncated_tool_result_count == 2
    assert context.metadata_payload()["truncated_tool_result_count"] == 2


def test_prepare_provider_context_keeps_truncation_message_inside_tool_cap() -> None:
    context = prepare_provider_context(
        prompt="search",
        tool_results=(ToolResult(tool_name="grep", status="ok", content="x" * 80, data={"index": 1}),),
        session_metadata={},
        policy=_context_window_policy(
            default_tool_result_chars=30,
            per_tool_result_chars={"grep": 1},
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
        policy=_context_window_policy(
            default_tool_result_chars=30,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2)
    (older, latest) = context.tool_results
    assert latest.data["index"] == 2
    assert latest.truncated is True
    assert latest.content is not None
    assert len(latest.content) <= 30
    assert older.content == "older"
    assert context.truncated_tool_result_count == 1
    assert context.compacted is False


def test_prepare_provider_context_does_not_load_tokenizer_when_clipping() -> None:
    fake_tiktoken = _FakeTiktokenModule()
    with patch.dict(sys.modules, {"tiktoken": fake_tiktoken}):
        context = prepare_provider_context(
            prompt="search",
            tool_results=(ToolResult(tool_name="grep", status="ok", content="x" * 80, data={"index": 1}),),
            session_metadata={},
            policy=_context_window_policy(
                default_tool_result_chars=30,
                per_tool_result_chars={"grep": 20},
            ),
        )

    (result,) = context.tool_results
    assert result.truncated is True
    assert result.content is not None
    assert len(result.content) <= 20
    assert fake_tiktoken.encoding_for_model_calls == 0
    assert fake_tiktoken.get_encoding_calls == 0


def test_context_window_policy_metadata_round_trips() -> None:
    policy = _context_window_policy(
        auto_compaction=False,
        default_tool_result_chars=30,
        per_tool_result_chars={"grep": 10},
    )

    parsed = context_window_policy_from_config(
        context_window_config_from_policy(policy),
        resolved_provider=None,
    )

    assert parsed == policy


def test_continuity_summary_metadata_is_derived_from_state() -> None:
    first = ContextProjection(
        summary_text="one",
        dropped_tool_result_count=1,
        retained_tool_result_count=3,
        dropped_tool_results=(DroppedToolResultDiagnostic(tool_name="read", status="ok", index=1),),
    )
    second = ContextProjection(
        summary_text="one",
        dropped_tool_result_count=2,
        retained_tool_result_count=3,
        dropped_tool_results=(
            DroppedToolResultDiagnostic(tool_name="read", status="ok", index=1),
            DroppedToolResultDiagnostic(tool_name="read", status="ok", index=2),
        ),
    )

    first_anchor, first_source = continuity_summary_metadata(first)
    second_anchor, second_source = continuity_summary_metadata(second)

    assert first_anchor is not None
    assert second_anchor is not None
    assert first_anchor != second_anchor
    assert first_source == {"tool_result_start": 0, "tool_result_end": 1}
    assert second_source == {"tool_result_start": 0, "tool_result_end": 2}


def test_assemble_provider_context_omits_matching_continuity_objective_only() -> None:
    prompt = "fix the failing test"
    summary_text = "## Objective\nfix the failing test\n\n## Progress Completed\n- kept"
    state = ContextProjection(
        projection_id="continuity:fixture",
        summary_text=summary_text,
        objective=prompt,
        progress_completed=("kept",),
        dropped_tool_result_count=1,
        retained_tool_result_count=1,
        source="tool_result_window",
    )

    assembled = assemble_provider_context(
        prompt=prompt,
        tool_results=(),
        session_metadata={},
        preserved_continuity_state=state,
    )

    provider_summary = next(segment.content for segment in assembled.segments if (segment.metadata or {}).get("source") == "context_projection")
    assert "## Objective" not in provider_summary
    assert "## Progress Completed\n- kept" in provider_summary
    assert assembled.metadata["projection"] == state.metadata_payload()
    assert state.summary_text == summary_text


def test_assemble_provider_context_retains_nonmatching_continuity_objective() -> None:
    prompt = "fix the failing test"
    summary_text = "## Objective\ncontinue the prior task\n\n## Progress Completed\n- kept"
    state = ContextProjection(
        projection_id="continuity:fixture",
        summary_text=summary_text,
        objective="continue the prior task",
        progress_completed=("kept",),
        dropped_tool_result_count=1,
        retained_tool_result_count=1,
        source="tool_result_window",
    )

    assembled = assemble_provider_context(
        prompt=prompt,
        tool_results=(),
        session_metadata={},
        preserved_continuity_state=state,
    )

    provider_summary = next(segment.content for segment in assembled.segments if (segment.metadata or {}).get("source") == "context_projection")
    assert "## Objective\ncontinue the prior task" in provider_summary
    assert assembled.metadata["projection"] == state.metadata_payload()
    assert state.summary_text == summary_text


def _continuity_tool_result(status: Literal["ok", "error"], content: str | None = None) -> ToolResult:
    return ToolResult(
        tool_name="fake_tool",
        status=status,
        content=content,
        data={},
        error=None,
    )


def test_continuity_state_metadata_payload_uses_instance_version() -> None:
    state = ContextProjection(
        summary_text="continuity summary",
        dropped_tool_result_count=1,
        retained_tool_result_count=2,
        source="tool_result_window",
        version=4,
    )

    payload = state.metadata_payload()

    assert payload["version"] == 4


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


def test_assemble_provider_context_rejects_legacy_continuity_metadata() -> None:
    with pytest.raises(ValueError, match="persisted runtime_state field 'continuity' is not supported"):
        assemble_provider_context(
            prompt="continue",
            tool_results=(_tool_result(1),),
            session_metadata={
                "runtime_state": {
                    "continuity": {
                        "version": "bad",
                        "summary_text": "must not be trusted as transcript truth",
                    }
                }
            },
            policy=_context_window_policy(default_tool_result_chars=100),
        )


def test_continuity_state_round_trip_includes_source_references() -> None:
    state = ContextProjection(
        summary_text="summary",
        dropped_tool_result_count=1,
        retained_tool_result_count=1,
        source="tool_result_window",
        source_references=("tool:call-1", "event:file:src/a.py"),
    )

    restored = continuity_state_from_metadata_payload(state.metadata_payload())
    assert restored is not None
    assert restored.source_references == ("tool:call-1", "event:file:src/a.py")


def test_normalize_read_output_preserves_showing_lines_footer() -> None:
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

    normalized = normalize_read_output(content)

    assert normalized == ("alpha\nbeta\n(Showing lines 10-11 of 20. Use offset=12 to continue.)")


def test_normalize_read_output_preserves_output_capped_footer() -> None:
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

    normalized = normalize_read_output(content)

    assert normalized == ("alpha\n(Output capped at 50 KB. Showing lines 1-1. Use offset=2 to continue.)")


def test_truncation_renders_through_view_with_character_cap() -> None:
    original = ToolResult(
        tool_name="read",
        status="ok",
        content="x" * 20_000,
        data={"path": "large.txt"},
    )
    context = prepare_provider_context(
        prompt="inspect large file",
        tool_results=(original,),
        session_metadata={},
        policy=_context_window_policy(),
    )

    (view,) = context.tool_results
    assert isinstance(view, ToolResultView)
    # Persisted truth (the original ToolResult) is never rebuilt or mutated.
    assert original.content == "x" * 20_000
    assert original.truncated is False
    assert original.partial is False
    assert "context_window_truncated" not in original.data
    assert view.result is not original
    # The view renders the clipped content with observable truncation state.
    assert view.clipped is True
    assert view.truncated is True
    assert view.partial is True
    assert view.content is not None
    assert len(view.content) < 20_000
    assert "[Tool output truncated by character limit" in view.content
    assert view.original_content_chars == 20_000
    assert view.content_char_limit == 6_000
    assert view.data["path"] == "large.txt"
    # No internal metadata markers leak into the rendered data.
    assert "context_window_truncated" not in view.data
    assert "context_window_original_content_chars" not in view.data
    assert "context_window_content_char_limit" not in view.data


def test_identity_view_preserves_source_truncation_flags() -> None:
    source = ToolResult(
        tool_name="shell_exec",
        status="ok",
        content="line-1\n[truncated: .voidcode/tool-output/shell_exec-abc.txt]",
        data={"exit_code": 0},
        truncated=True,
        partial=True,
        reference=".voidcode/tool-output/shell_exec-abc.txt",
    )
    context = prepare_provider_context(
        prompt="run script",
        tool_results=(source,),
        session_metadata={},
        policy=_context_window_policy(),
    )

    (view,) = context.tool_results
    assert isinstance(view, ToolResultView)
    assert view.clipped is False
    assert view.truncated is True
    assert view.partial is True
    assert view.reference == source.reference
    assert view.content == source.content


def test_truncated_tool_result_data_has_no_context_window_markers() -> None:
    context = prepare_provider_context(
        prompt="inspect large file",
        tool_results=(ToolResult(tool_name="read", status="ok", content="x" * 20_000, data={"path": "large.txt"}),),
        session_metadata={},
        policy=_context_window_policy(),
    )

    (result,) = context.tool_results
    for key in (
        "context_window_truncated",
        "context_window_original_content_tokens",
        "context_window_content_token_limit",
    ):
        assert key not in result.data


def test_provider_messages_exclude_context_window_markers() -> None:
    assembled = assemble_provider_context(
        prompt="inspect large file",
        tool_results=(ToolResult(tool_name="read", status="ok", content="x" * 20_000, data={"path": "large.txt"}),),
        session_metadata={},
        policy=_context_window_policy(),
    )

    for mode in ("standard", "synthetic_user_message"):
        snapshot = inspect_provider_context(
            assembled_context=assembled,
            provider="opencode-go" if mode == "synthetic_user_message" else "openai",
            model="minimax-m2.7" if mode == "synthetic_user_message" else "gpt-4o",
            execution_engine="provider",
            available_tool_count=1,
            tool_feedback_mode=mode,
        )
        assert snapshot.provider_messages
        for message in snapshot.provider_messages:
            assert "context_window_" not in (message.content or "")
    # Standard tool-role rendering exposes the clipped view content.
    standard_snapshot = inspect_provider_context(
        assembled_context=assembled,
        provider="openai",
        model="gpt-4o",
        execution_engine="provider",
        available_tool_count=1,
        tool_feedback_mode="standard",
    )
    tool_message = next(message for message in standard_snapshot.provider_messages if message.role == "tool")
    assert len(tool_message.content or "") < 20_000
    assert tool_message.content_truncated is True
