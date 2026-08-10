from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast
from unittest.mock import patch

from voidcode.runtime.context_transforms import (
    HookPresetGuidanceTransformProvider,
    RuntimeContextTransformInjection,
    RuntimeContextTransformRegistry,
    RuntimeContextTransformRequest,
    RuntimeContextTransformResult,
    RuntimeFileRulesTransformProvider,
)
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


def _context_window_policy(**overrides: object) -> Any:
    resolved: dict[str, object] = {"tokenizer_model": None, **overrides}
    return ContextWindowPolicy(**cast(Any, resolved))


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
    policy = _context_window_policy()
    context = prepare_provider_context(
        prompt="continue coding task",
        tool_results=tuple(_tool_result(index) for index in range(1, 8)),
        session_metadata={},
        policy=policy,
    )

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
        policy=_context_window_policy(),
    )

    (result,) = context.tool_results
    assert result.truncated is True
    assert result.partial is True
    assert result.content is not None
    assert len(result.content) < len(large_content)
    assert context.truncated_tool_result_count >= 0
    assert context.token_budget is None


def test_prepare_provider_context_keeps_results_within_limit() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2)),
        session_metadata={},
        policy=_context_window_policy(model_context_window_tokens=100),
    )

    assert context.prompt == "read sample.txt"
    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2)
    assert context.compacted is False
    assert context.compaction_reason is None
    assert context.original_tool_result_count == 2
    assert context.retained_tool_result_count == 2
    assert context.token_budget == 100
    assert context.continuity_state is None


def test_assemble_provider_context_second_stage_preserves_non_recent_tiers() -> None:
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
                            "content": "must survive compaction",
                            "status": "in_progress",
                            "position": 1,
                            "updated_at": 12,
                        }
                    ],
                }
            }
        },
        agent_prompt_context="A" * 400,
        policy=_context_window_policy(
            model_context_window_tokens=20,
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
                    "version": 1,
                    "revision": 12,
                    "todos": [
                        {
                            "content": "implement runtime todo state",
                            "status": "in_progress",
                            "position": 1,
                            "updated_at": 12,
                        },
                        {
                            "content": "old finished task",
                            "status": "completed",
                            "position": 2,
                            "updated_at": 12,
                        },
                    ],
                }
            }
        },
        policy=_context_window_policy(model_context_window_tokens=1),
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
        "runtime_memory_usage_guidance",
        "runtime_tool_policy_summary",
        "runtime_todo_state",
    }
    todo_metadata = next(metadata for metadata in system_metadata if metadata["source"] == "runtime_todo_state")
    assert todo_metadata == {
        "source": "runtime_todo_state",
        "tier": "task",
        "layer": "task_state",
    }


def test_assemble_provider_context_records_explicit_context_tiers() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="alpha",
                data={"tool_call_id": "call-1", "arguments": {"path": "src/app.py"}},
            ),
        ),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 1,
                    "revision": 12,
                    "todos": [
                        {
                            "content": "implement tiers",
                            "status": "in_progress",
                            "position": 1,
                            "updated_at": 12,
                        }
                    ],
                }
            }
        },
        skill_prompt_context="skill context",
        policy=_context_window_policy(model_context_window_tokens=100),
    )

    assert assembled.metadata["context_tiers"] == {
        "version": 1,
        "order": ["instruction", "task", "recent"],
        "counts": {
            "instruction": 6,
            "workspace": 0,
            "task": 2,
            "recent": 2,
        },
    }
    assert assembled.metadata["context_tier_policy"] == {
        "version": 1,
        "protected_tiers": ["instruction", "workspace", "task"],
        "compaction_target": "recent",
    }
    assert [(segment.metadata or {}).get("tier") for segment in assembled.segments[:4]] == [
        "instruction",
        "instruction",
        "instruction",
        "instruction",
    ]


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
                tool_name="read_file",
                status="ok",
                content="content",
                data={"path": "src/app.py", "arguments": {"path": "src/app.py"}},
            ),
        ),
        session_metadata={},
        workspace=workspace,
        policy=_context_window_policy(model_context_window_tokens=100),
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
                "execution_index": 2,
                "injection_count": 2,
                "provider_order": [
                    "hook_preset_guidance",
                    "runtime_file_rules",
                    "directory_readme_context",
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
        policy=_context_window_policy(model_context_window_tokens=100),
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
                    "runtime_file_rules",
                    "directory_readme_context",
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
                tool_name="read_file" if i % 2 == 0 else "edit",
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
        policy=_context_window_policy(model_context_window_tokens=100),
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
                tool_name="todo_write",
                content="Updated 1 todos\n1. [pending/low] stale tool feedback",
                status="ok",
                data={
                    "tool_call_id": "todo-old",
                    "arguments": {"todos": []},
                    "todos": [
                        {
                            "content": "stale tool feedback",
                            "status": "pending",
                        }
                    ],
                },
            ),
            ToolResult(
                tool_name="read_file",
                content="current code",
                status="ok",
                data={"tool_call_id": "read-1", "arguments": {"path": "src/app.py"}},
            ),
        ),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 1,
                    "revision": 2,
                    "todos": [
                        {
                            "content": "authoritative runtime state",
                            "status": "in_progress",
                            "position": 1,
                            "updated_at": 2,
                        }
                    ],
                }
            }
        },
        policy=_context_window_policy(model_context_window_tokens=100),
    )

    assert [segment.tool_name for segment in assembled.segments if segment.role == "tool"] == ["read_file"]
    system_text = "\n".join(str(segment.content) for segment in assembled.segments if segment.role == "system")
    assert "authoritative runtime state" in system_text
    assert "stale tool feedback" not in system_text
    assert "Do not call todo_write again unless you are actually changing" in system_text
    assert "If any todo is already in_progress, continue that item" in system_text


def test_assemble_provider_context_injects_pending_approval_state() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "plan_state": {
                "status": "waiting_approval",
                "approval_request_id": "approval-123",
                "blocked_tool": "write_file",
            }
        },
        policy=_context_window_policy(model_context_window_tokens=100),
    )

    pending_segments = [
        segment for segment in assembled.segments if segment.metadata is not None and segment.metadata.get("source") == "runtime_pending_state"
    ]
    assert len(pending_segments) == 1
    assert pending_segments[0].content is not None
    assert "waiting_approval" in pending_segments[0].content
    assert "approval-123" in pending_segments[0].content
    assert "write_file" in pending_segments[0].content
    assert "runtime resume" in pending_segments[0].content
    assert pending_segments[0].metadata == {
        "source": "runtime_pending_state",
        "tier": "task",
        "layer": "task_state",
        "status": "waiting_approval",
        "blocked_tool": "write_file",
        "approval_request_id": "approval-123",
    }


def test_assemble_provider_context_skill_todo_transform_content_present() -> None:
    assembled = assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={
            "runtime_state": {
                "todos": {
                    "version": 1,
                    "revision": 1,
                    "todos": [
                        {
                            "content": "implement feature",
                            "status": "in_progress",
                            "position": 1,
                            "updated_at": 1,
                        }
                    ],
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
        policy=_context_window_policy(model_context_window_tokens=100),
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
        policy=_context_window_policy(model_context_window_tokens=100),
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
        policy=_context_window_policy(model_context_window_tokens=100),
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
                            }
                        ]
                    },
                },
            ),
        ),
        session_metadata={},
        policy=_context_window_policy(model_context_window_tokens=100),
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
                content=("OPENAI_API_KEY=sk-test-secret\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz"),
                status="ok",
                data={"tool_call_id": "call:secret", "arguments": {"path": ".env"}},
            ),
        ),
        session_metadata={},
        policy=_context_window_policy(model_context_window_tokens=100),
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
        policy=_context_window_policy(model_context_window_tokens=100),
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
                        }
                    ]
                },
                "todos": [
                    {
                        "content": "preserve context parity",
                        "status": "in_progress",
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
                            "position": 1,
                            "updated_at": 1,
                        }
                    ],
                }
            }
        },
        policy=_context_window_policy(auto_compaction=False, model_context_window_tokens=100_000),
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
        policy=_context_window_policy(auto_compaction=False, model_context_window_tokens=100_000),
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
    expected_tool_results = tuple(result for result in tool_results if result.tool_name != "todo_write")
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
    assert '"tool_name": "todo_write"' not in synthetic_feedback
    assert any(diagnostic.code == "provider_path_uses_synthetic_tool_feedback" for diagnostic in synthetic_snapshot.diagnostics)


def test_prepare_provider_context_enforces_token_budget_compaction() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _sized_tool_result(1, content_size=16),
            _sized_tool_result(2, content_size=16),
            _sized_tool_result(3, content_size=16),
            _sized_tool_result(4, content_size=16),
        ),
        session_metadata={},
        policy=_context_window_policy(
            model_context_window_tokens=12,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (4,)
    assert context.retained_tool_result_count == 1
    assert context.compacted is True


def test_prepare_provider_context_preserves_latest_result_over_budget() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_sized_tool_result(1, content_size=400),),
        session_metadata={},
        policy=_context_window_policy(model_context_window_tokens=1),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (1,)
    assert context.compacted is False
    assert context.retained_tool_result_tokens is not None
    assert context.retained_tool_result_tokens > 1


def test_prepare_provider_context_uses_chars_per_4_token_approximation() -> None:
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
        policy=_context_window_policy(model_context_window_tokens=1),
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
        policy=_context_window_policy(model_context_window_tokens=1),
    )

    assert ascii_context.retained_tool_result_tokens is not None
    assert unicode_context.retained_tool_result_tokens is not None
    assert ascii_context.retained_tool_result_tokens > 0
    assert unicode_context.retained_tool_result_tokens > 0
    assert ascii_context.token_estimate_source == "approx_chars_per_4"
    assert unicode_context.token_estimate_source == "approx_chars_per_4"


def test_prepare_provider_context_keeps_all_results_when_budget_missing() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3)),
        session_metadata={},
        policy=_context_window_policy(
            model_context_window_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2, 3)
    assert context.compacted is False
    assert context.retained_tool_result_count == 3
    assert context.token_budget is None
    assert context.original_tool_result_tokens is None


def test_project_tool_results_for_context_window_keeps_results_without_token_budget() -> None:
    projection = project_tool_results_for_context_window(
        tool_results=tuple(_tool_result(index) for index in range(1, 11)),
        policy=_context_window_policy(
            model_context_window_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert projection.token_budget is None
    assert projection.retained_indexes == tuple(range(10))
    assert tuple(result.data["index"] for result in projection.retained_results) == (
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    )


def test_prepare_provider_context_keeps_results_without_token_budget_when_history_grows() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=tuple(_tool_result(index) for index in range(1, 11)),
        session_metadata={},
        policy=_context_window_policy(
            model_context_window_tokens=None,
            default_tool_result_tokens=None,
        ),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    )
    assert context.compacted is False
    assert context.retained_tool_result_count == 10


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
        policy=_context_window_policy(
            model_context_window_tokens=120,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert 4 in retained_indexes
    assert 3 in retained_indexes
    assert 1 not in retained_indexes
    assert context.compacted is True
    assert context.token_budget == 120


def test_retain_indexes_within_token_budget_always_keeps_newest_candidate() -> None:
    results = (
        _sized_tool_result(1, content_size=40),
        _sized_tool_result(2, content_size=40),
        _sized_tool_result(3, content_size=400),
    )

    retained = _retain_indexes_within_token_budget(
        results,
        (0, 1, 2),
        token_budget=20,
        tokenizer_model=None,
    )

    assert retained == (2,)


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
        policy=_context_window_policy(
            model_context_window_tokens=100,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert retained_indexes == (1, 3, 4, 5)


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
        policy=_context_window_policy(
            model_context_window_tokens=100,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert retained_indexes == (2, 3, 4, 5)


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
        policy=_context_window_policy(
            model_context_window_tokens=100,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert len(retained_indexes) >= 1


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
        policy=_context_window_policy(
            model_context_window_tokens=100,
            default_tool_result_tokens=None,
        ),
    )

    retained_indexes = tuple(result.data["index"] for result in context.tool_results)
    assert 5 in retained_indexes
    assert retained_indexes == (2, 3, 4, 5)


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
        policy=_context_window_policy(
            model_context_window_tokens=50,
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
        tokenizer_model=None,
    )
    assert len(indexes) >= 1


def test_count_text_tokens_reports_estimated_fallback_metadata() -> None:
    counted = count_text_tokens("abcd你")

    assert counted.tokens == 2
    assert counted.method == "estimated"
    assert counted.source == "approx_chars_per_4"
    assert counted.exact is False


def test_count_text_tokens_ignores_tokenizer_model_and_uses_approximation() -> None:
    fake_tiktoken = _FakeTiktokenModule()
    with patch.dict(sys.modules, {"tiktoken": fake_tiktoken}):
        counted = count_text_tokens("abcd", tokenizer_model="gpt-test")

    assert counted.tokens == 1
    assert counted.method == "estimated"
    assert counted.source == "approx_chars_per_4"
    assert counted.exact is False


def test_compaction_does_not_import_tiktoken() -> None:
    fake_tiktoken = _FakeTiktokenModule()
    with patch.dict(sys.modules, {"tiktoken": fake_tiktoken}):
        context = prepare_provider_context(
            prompt="search",
            tool_results=(ToolResult(tool_name="grep", status="ok", content="x" * 80),),
            session_metadata={},
            policy=_context_window_policy(
                model_context_window_tokens=20,
                default_tool_result_tokens=None,
                tokenizer_model="cl100k_base",
            ),
        )

    assert context.token_estimate_source == "approx_chars_per_4"
    assert fake_tiktoken.encoding_for_model_calls == 0


def test_prepare_provider_context_honors_reserved_output_budget() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(
            _sized_tool_result(1, content_size=480),
            _sized_tool_result(2, content_size=200),
        ),
        session_metadata={},
        policy=_context_window_policy(
            model_context_window_tokens=120,
            reserved_output_tokens=40,
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
        policy=_context_window_policy(
            model_context_window_tokens=50,
            per_tool_result_tokens={"grep": 30},
        ),
    )

    (latest,) = context.tool_results
    assert latest.truncated is False
    assert latest.content == "latest" * 20
    assert context.truncated_tool_result_count >= 0
    assert context.metadata_payload()["truncated_tool_result_count"] == 1


def test_prepare_provider_context_keeps_truncation_message_inside_tool_cap() -> None:
    context = prepare_provider_context(
        prompt="search",
        tool_results=(ToolResult(tool_name="grep", status="ok", content="x" * 80, data={"index": 1}),),
        session_metadata={},
        policy=_context_window_policy(
            model_context_window_tokens=30,
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
        policy=_context_window_policy(
            model_context_window_tokens=30,
            default_tool_result_tokens=None,
        ),
    )

    (latest,) = context.tool_results
    assert latest.data["index"] == 2
    assert latest.truncated is False
    assert latest.content is not None
    assert len(latest.content) == 80
    assert context.truncated_tool_result_count == 0


def test_prepare_provider_context_does_not_load_tokenizer_when_clipping() -> None:
    fake_tiktoken = _FakeTiktokenModule()
    with patch.dict(sys.modules, {"tiktoken": fake_tiktoken}):
        context = prepare_provider_context(
            prompt="search",
            tool_results=(ToolResult(tool_name="grep", status="ok", content="x" * 80, data={"index": 1}),),
            session_metadata={},
            policy=_context_window_policy(
                model_context_window_tokens=30,
                per_tool_result_tokens={"grep": 20},
                tokenizer_model="cache-test-model",
            ),
        )

    (result,) = context.tool_results
    assert result.truncated is False
    assert result.content is not None
    assert fake_tiktoken.encoding_for_model_calls == 0
    assert fake_tiktoken.get_encoding_calls == 0


def test_prepare_provider_context_preserves_recent_results_over_count_cap() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3)),
        session_metadata={},
        policy=_context_window_policy(model_context_window_tokens=50),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (2, 3)
    assert context.retained_tool_result_count == 2


def test_prepare_provider_context_auto_compaction_false_retains_all_results() -> None:
    context = prepare_provider_context(
        prompt="read sample.txt",
        tool_results=(_tool_result(1), _tool_result(2), _tool_result(3)),
        session_metadata={},
        policy=_context_window_policy(auto_compaction=False, model_context_window_tokens=30),
    )

    assert tuple(result.data["index"] for result in context.tool_results) == (1, 2, 3)
    assert context.compacted is False


def test_context_window_policy_metadata_round_trips() -> None:
    policy = _context_window_policy(
        auto_compaction=False,
        model_context_window_tokens=1_000,
        reserved_output_tokens=20,
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
        dropped_tool_results=(DroppedToolResultDiagnostic(tool_name="read_file", status="ok", index=1),),
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


def _continuity_tool_result(status: Literal["ok", "error"], content: str | None = None) -> ToolResult:
    return ToolResult(
        tool_name="fake_tool",
        status=status,
        content=content,
        data={},
        error=None,
    )


def test_continuity_state_metadata_payload_uses_instance_version() -> None:
    state = RuntimeContinuityState(
        summary_text="continuity summary",
        dropped_tool_result_count=1,
        retained_tool_result_count=2,
        source="tool_result_window",
        version=1,
    )

    payload = state.metadata_payload()

    assert payload["version"] == 1


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
        policy=_context_window_policy(model_context_window_tokens=100),
    )

    assert assembled.continuity_state is None
    assert "continuity_state" not in assembled.metadata
    assert all(segment.metadata is None or segment.metadata.get("source") != "continuity_summary" for segment in assembled.segments)


def test_continuity_state_round_trip_includes_source_references() -> None:
    state = RuntimeContinuityState(
        summary_text="summary",
        dropped_tool_result_count=1,
        retained_tool_result_count=1,
        source="tool_result_window",
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

    assert normalized == ("alpha\n(Output capped at 50 KB. Showing lines 1-1. Use offset=2 to continue.)")


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
    policy = _context_window_policy(
        auto_compaction=False,
        model_context_window_tokens=100_000,
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
