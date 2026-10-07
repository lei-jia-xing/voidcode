"""Bounded provider-context pruning.

Contract (``docs/contracts/runtime-config.md`` → ``context_window.compaction``):
when the estimated payload of one provider call exceeds the budget threshold, the
oldest prunable results have their content replaced by a bounded placeholder.
Messages and tool pairings are never removed, system/instruction sections are never touched,
protected results (todo/skill/rule surfaces) stay verbatim, and every count
reported on the window is real.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.core.turns import FinalTurn, ToolTurn
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.provider.config import ProviderConfigs, ProviderEndpointConfig
from voidcode.provider.protocol import ProviderTurnRequest, ProviderTurnResult
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.runtime.config import RuntimeCompactionConfig, RuntimeConfig, RuntimeContextWindowConfig, RuntimeMcpConfig
from voidcode.runtime.context.window import (
    CompactionBudget,
    ContextWindowPolicy,
    assemble_provider_context,
    prepare_provider_context,
)
from voidcode.runtime.context.window_policy import (
    context_window_config_from_policy,
    context_window_policy_from_config,
)
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.tools.contracts import ToolCall, ToolResult
from voidcode.tools.read import ReadTool

#: The pruning floors are production constants (20_000 tokens of reclaim, 50 per
#: result), so prunable fixtures are sized in tens of thousands of bytes: this is
#: 12_500 tokens per result.
_PRUNABLE_CHARS = 50_000


def _result(
    content: str,
    *,
    tool_name: str = "read",
    artifact_id: str | None = None,
    data: Mapping[str, object] | None = None,
) -> ToolResult:
    payload: dict[str, object] = dict(data or {})
    if artifact_id is not None:
        payload["artifact"] = {"artifact_id": artifact_id, "status": "available", "byte_count": len(content)}
    return ToolResult(tool_name=tool_name, status="ok", content=content, data=payload)


def _policy(**overrides: object) -> ContextWindowPolicy:
    arguments: dict[str, object] = {
        "default_tool_result_chars": None,
        "compaction": RuntimeCompactionConfig(keep_recent_tool_tokens=overrides.pop("keep_recent_tool_tokens", 500)),
    }
    arguments.update(overrides)
    return ContextWindowPolicy(**arguments)  # type: ignore[arg-type]


_PRUNE_PROMPT = "Summarize the workspace changes."


def _prepare(results: tuple[ToolResult, ...], **overrides: object) -> object:
    arguments: dict[str, object] = {
        "prompt": _PRUNE_PROMPT,
        "tool_results": results,
        "session_metadata": {},
        "policy": _policy(),
        "context_window": 1_000,
        "payload_bytes": 0,
    }
    arguments.update(overrides)
    return prepare_provider_context(**arguments)  # type: ignore[arg-type]


# --- real reduction, oldest first --------------------------------------------


def test_over_budget_prunes_the_oldest_content_and_keeps_the_recent_results() -> None:
    results = tuple(_result("x" * _PRUNABLE_CHARS) for _index in range(6))  # 6 x 50_000 chars ~ 75k tokens

    # Two results fit under the kept-recent budget; the four older ones are pruned.
    window = _prepare(results, policy=_policy(keep_recent_tool_tokens=26_000))

    assert window.compacted is True
    assert window.original_tool_result_count == 6
    assert window.retained_tool_result_count == 6  # pairing preserved: nothing is removed
    assert window.dropped_tool_result_count == 4
    assert window.pruned_savings_tokens > 0
    assert window.usage_tokens_before is not None and window.usage_tokens_after is not None
    assert window.usage_tokens_after < window.usage_tokens_before
    assert [view.pruned for view in window.tool_results] == [True, True, True, True, False, False]
    assert window.tool_results[-1].content == results[-1].content
    assert window.tool_results[0].content is not None and window.tool_results[0].content.startswith("[Runtime context pruning:")
    assert f"omitted_bytes={_PRUNABLE_CHARS}" in window.tool_results[0].content
    # Original scale is preserved on the view that replaced it.
    assert window.tool_results[0].original_content_chars == _PRUNABLE_CHARS
    assert window.continuity_state is not None and window.continuity_state.summary_text
    assert window.summary_anchor is not None


def test_data_payload_counts_toward_the_budget_and_is_replaced() -> None:
    """A read-style result carries its body in ``data``: it must be measured and pruned."""
    results = tuple(
        _result(
            f"Read 3000 line(s) from file-{index}.txt.",
            data={"lines": [f"line {line} " + "y" * 20 for line in range(3_000)], "path": f"file-{index}.txt"},
        )
        for index in range(6)
    )

    # One payload (~26k tokens once JSON-encoded) fits under the kept-recent budget.
    window = _prepare(results, policy=_policy(keep_recent_tool_tokens=35_000))

    assert window.compacted is True
    assert window.dropped_tool_result_count > 0
    pruned_view = window.tool_results[0]
    assert pruned_view.data["context_pruned"] is True
    assert "lines" not in pruned_view.data
    assert pruned_view.data["path"] == "file-0.txt"  # small scalars stay readable
    kept = [view for view in window.tool_results if not view.pruned]
    assert kept and "lines" in kept[-1].data


def test_under_budget_view_is_byte_identical() -> None:
    results = (_result("small"), _result("tiny"))

    window = _prepare(results, policy=_policy(keep_recent_tool_tokens=20_000), context_window=100_000)

    assert window.compacted is False
    assert window.compaction_reason is None
    assert window.dropped_tool_result_count == 0
    assert window.continuity_state is None
    assert [view.content for view in window.tool_results] == ["small", "tiny"]


def test_protected_results_are_never_pruned() -> None:
    results = (
        _result("t" * 4_000, tool_name="todo"),
        _result("s" * 4_000, tool_name="skill"),
        _result("r" * 4_000, tool_name="read", data={"path": "voidcode://rule/demo"}),
        _result("p" * _PRUNABLE_CHARS, tool_name="read", data={"path": "src/app.py"}),
        _result("q" * _PRUNABLE_CHARS, tool_name="read", data={"path": "src/other.py"}),
    )

    window = _prepare(results, policy=_policy(keep_recent_tool_tokens=1_000), context_window=100)

    assert window.compacted is True
    assert [view.pruned for view in window.tool_results] == [False, False, False, True, True]
    assert window.tool_results[0].content == "t" * 4_000
    assert window.tool_results[2].content == "r" * 4_000


def test_savings_below_the_floor_leave_the_view_untouched() -> None:
    # A single small result reclaims ~1k tokens, below the production savings floor.
    results = (_result("x" * 4_000),)

    window = _prepare(results, policy=_policy(keep_recent_tool_tokens=0), context_window=100)

    assert window.compacted is False
    assert window.dropped_tool_result_count == 0
    assert window.tool_results[0].content == "x" * 4_000
    # The overage is still reported instead of silently ignored.
    assert window.compaction_reason is not None
    assert window.compaction_reason.startswith("token_budget_exceeded:no_prunable_tool_content")


def test_unsized_model_is_reported_rather_than_silently_unbounded() -> None:
    results = (_result("x" * 4_000),)

    window = _prepare(results, context_window=None)

    assert window.compacted is False
    assert window.dropped_tool_result_count == 0
    assert window.compaction_reason is not None
    assert window.compaction_reason.startswith("compaction_unsized")


def test_small_window_first_turn_stays_under_threshold() -> None:
    """16k window recovers to threshold 13600, so an empty first turn never compacts."""
    window = _prepare((), context_window=16_000)

    assert window.compacted is False
    assert window.compaction_reason is None
    assert window.usage_tokens_before is not None and window.usage_tokens_before < 13_600


def test_hook_cancel_keeps_the_view_verbatim() -> None:
    from voidcode.runtime.context.window import BeforeCompactInput

    results = (_result("x" * 4_000),)

    window = _prepare(results, before_compact=BeforeCompactInput(cancel=True, reason="operator_hold"))

    assert window.compacted is False
    assert window.compaction_reason == "operator_hold"
    assert window.tool_results[0].content == "x" * 4_000


# --- determinism --------------------------------------------------------------


def test_identical_inputs_compile_identically() -> None:
    """The same event history compiles the same view, including through a recompile."""
    results = tuple(_result("x" * _PRUNABLE_CHARS) for _index in range(5))
    budget = CompactionBudget(context_window=1_000)

    first = _prepare(results, policy=_policy(keep_recent_tool_tokens=1_200))
    second = _prepare(results, policy=_policy(keep_recent_tool_tokens=1_200))

    assert [view.content for view in first.tool_results] == [view.content for view in second.tool_results]
    assert first.compaction_reason == second.compaction_reason
    assert (first.dropped_tool_result_count, first.pruned_savings_tokens) == (second.dropped_tool_result_count, second.pruned_savings_tokens)

    # Recompiling from the first view's session metadata (the resume/replay shape)
    # keeps the pruned set.
    assembled = assemble_provider_context(
        prompt="short prompt",
        tool_results=results,
        session_metadata={},
        policy=_policy(keep_recent_tool_tokens=0),
        compaction_budget=budget,
    )
    recompiled = assemble_provider_context(
        prompt="short prompt",
        tool_results=results,
        session_metadata=dict(assembled.metadata),
        policy=_policy(keep_recent_tool_tokens=0),
        compaction_budget=budget,
    )

    assert [segment.content for segment in assembled.segments] == [segment.content for segment in recompiled.segments]
    assert assembled.metadata["dropped_tool_result_count"] == recompiled.metadata["dropped_tool_result_count"]


# --- assembly: budget covers the whole payload, system sections survive --------


def test_budget_covers_system_sections_not_just_prompt_and_tools() -> None:
    """A large instruction payload must trigger pruning on its own."""
    results = (_result("x" * 100_000),)  # ~25k tokens: prunable alone, reclaim crosses the floor

    assembled = assemble_provider_context(
        prompt="short prompt",
        tool_results=results,
        session_metadata={},
        policy=_policy(keep_recent_tool_tokens=0),
        skill_prompt_context="s" * 8_000,
        compaction_budget=CompactionBudget(context_window=1_000),
    )

    window = assembled.context_window
    assert window is not None
    assert window.compacted is True
    tool_tokens = len(results[0].content or "") // 4
    assert window.usage_tokens_before > tool_tokens  # the skill body is in the estimate
    assert assembled.metadata["compacted"] is True
    assert assembled.metadata["dropped_tool_result_count"] == 1
    assert assembled.metadata["usage_tokens_estimated"] is True


def test_pruning_replaces_tool_content_without_dropping_system_or_pairing_segments() -> None:
    results = tuple(_result("x" * 40_000) for _index in range(4))

    assembled = assemble_provider_context(
        prompt="short prompt",
        tool_results=results,
        session_metadata={},
        policy=_policy(keep_recent_tool_tokens=0),
        skill_prompt_context="skill body " * 500,
        compaction_budget=CompactionBudget(context_window=1_000),
    )

    system_segments = [segment for segment in assembled.segments if segment.role == "system"]
    tool_segments = [segment for segment in assembled.segments if segment.role == "tool"]
    assert any("skill body" in (segment.content or "") for segment in system_segments)
    assert len(tool_segments) == len(results)
    pruned = [segment for segment in tool_segments if (segment.metadata or {}).get("pruned") is True]
    assert len(pruned) == assembled.context_window.dropped_tool_result_count
    assert all((segment.content or "").startswith("[Runtime context pruning:") for segment in pruned)
    assert (pruned[0].metadata or {}).get("original_content_chars") == 40_000


def test_pruned_result_with_an_artifact_produces_a_reference_segment() -> None:
    results = (
        _result("x" * _PRUNABLE_CHARS, artifact_id="artifact-1"),
        _result("y" * _PRUNABLE_CHARS, tool_name="grep"),
    )

    assembled = assemble_provider_context(
        prompt="short prompt",
        tool_results=results,
        session_metadata={},
        policy=_policy(keep_recent_tool_tokens=0),
        compaction_budget=CompactionBudget(context_window=1_000),
    )

    references = [segment for segment in assembled.segments if (segment.metadata or {}).get("source") == "runtime_context_artifact_reference"]
    assert len(references) == 1
    assert "voidcode://artifact/artifact-1" in (references[0].content or "")
    assert 'read(path="voidcode://artifact/artifact-1")' in (references[0].content or "")
    # The placeholder itself points at the recoverable artifact too.
    pruned = [segment for segment in assembled.segments if (segment.metadata or {}).get("pruned") is True]
    assert pruned and "artifact_id=artifact-1" in (pruned[0].content or "")


def test_policy_and_config_round_trip_keeps_every_compaction_knob() -> None:
    """The policy holds the config group itself, so no knob is copied or lost."""
    config = RuntimeContextWindowConfig(
        default_tool_result_chars=1_000,
        compaction=RuntimeCompactionConfig(
            enabled=False,
            threshold_tokens=9_000,
            reserve_tokens=42,
            keep_recent_tool_tokens=500,
        ),
    )

    policy = context_window_policy_from_config(config)

    assert policy.compaction == config.compaction
    assert context_window_config_from_policy(policy) == config


def test_compacted_summary_is_deterministic_text() -> None:
    window = _prepare((_result("x" * 100_000),))

    assert window.compacted is True
    assert window.continuity_state is not None and window.continuity_state.summary_text
    assert window.summary_anchor is not None


# --- model summary substitution ----------------------------------------------


def _projection_segment(assembled: object) -> object:
    for segment in assembled.segments:  # type: ignore[attr-defined]
        if isinstance(segment.metadata, dict) and segment.metadata.get("source") == "context_projection":
            return segment
    raise AssertionError("no context_projection segment")


def test_model_summary_override_replaces_the_text_but_not_the_counts() -> None:
    """An override swaps only the summary text; every counter stays the real pruning outcome."""
    results = tuple(_result("x" * _PRUNABLE_CHARS) for _index in range(6))
    budget = CompactionBudget(context_window=1_000)
    policy = _policy(keep_recent_tool_tokens=26_000)

    deterministic = assemble_provider_context(
        prompt=_PRUNE_PROMPT,
        tool_results=results,
        session_metadata={},
        policy=policy,
        compaction_budget=budget,
    )
    substituted = assemble_provider_context(
        prompt=_PRUNE_PROMPT,
        tool_results=results,
        session_metadata={},
        policy=policy,
        compaction_budget=budget,
        continuity_summary_override="  MODEL SUMMARY TEXT  ",
    )

    assert substituted.metadata["summary_kind"] == "model"
    assert deterministic.metadata["summary_kind"] == "deterministic"
    segment = _projection_segment(substituted)
    assert segment.content == "Runtime context projection:\nMODEL SUMMARY TEXT"
    # Counts describe the real pruning, never the substituted string.
    for key in ("original_tool_result_count", "retained_tool_result_count", "dropped_tool_result_count"):
        assert substituted.metadata[key] == deterministic.metadata[key]
    assert substituted.metadata["dropped_tool_result_count"] == 4
    # The anchor identifies the summary actually shown, so it moves with the text.
    assert substituted.metadata["summary_anchor"] != deterministic.metadata["summary_anchor"]


def test_blank_override_degrades_to_the_deterministic_summary() -> None:
    """An empty model response must not blank the continuity segment."""
    results = tuple(_result("x" * _PRUNABLE_CHARS) for _index in range(6))

    assembled = assemble_provider_context(
        prompt=_PRUNE_PROMPT,
        tool_results=results,
        session_metadata={},
        policy=_policy(keep_recent_tool_tokens=26_000),
        compaction_budget=CompactionBudget(context_window=1_000),
        continuity_summary_override="   ",
    )

    assert assembled.metadata["summary_kind"] == "deterministic"
    assert "## Progress Completed" in _projection_segment(assembled).content


# --- runtime seam: the opt-in model-generated summary -------------------------


_SUMMARY_PROMPT = "summarize the workspace"
_SUMMARY_TEXT = "MODEL SUMMARY: the parser is patched and the migration must be rerun."
_READ_COUNT = 30
_READ_LINES = 120
_CONTEXT_COMPACTED = "runtime.context_compacted"


def _big_workspace(tmp_path: Path) -> None:
    body = "".join(f"line {index:03d} " + "y" * 20 + "\n" for index in range(_READ_LINES))
    for index in range(_READ_COUNT):
        (tmp_path / f"big-{index}.txt").write_text(body, encoding="utf-8")


class _SummaryTurnProvider:
    """Turn provider whose only outcome is the summary answer (or a failure)."""

    def __init__(self, *, outcome: str | Exception) -> None:
        self.name = "session"
        self.outcome = outcome
        self.requests: list[ProviderTurnRequest] = []

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return ProviderTurnResult(output=self.outcome)


class _SummaryModelProvider:
    def __init__(self, *, outcome: str | Exception) -> None:
        self.name = "session"
        self.turn = _SummaryTurnProvider(outcome=outcome)

    def turn_provider(self) -> _SummaryTurnProvider:
        return self.turn


class _ReadThenDoneGraph:
    """Scripted graph: one read per step, then done; records the provider view."""

    def __init__(self, reads: int) -> None:
        self._reads = reads
        self.summary_segments: list[str | None] = []

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> FinalTurn | ToolTurn:
        _ = session
        assembled = request.assembled_context  # type: ignore[attr-defined]
        projection = [segment.content for segment in assembled.segments if (segment.metadata or {}).get("source") == "context_projection"]
        self.summary_segments.append(projection[0] if projection else None)
        if len(tool_results) < self._reads:
            return ToolTurn(calls=(ToolCall(tool_name="read", arguments={"path": f"big-{len(tool_results)}.txt"}),))
        return FinalTurn(output="done")


def _summary_runtime(
    tmp_path: Path,
    *,
    provider: _SummaryModelProvider,
    summary_enabled: bool,
    reads: int = _READ_COUNT,
    hooks: RuntimeHooksConfig | None = None,
) -> tuple[VoidCodeRuntime, _ReadThenDoneGraph]:
    graph = _ReadThenDoneGraph(reads)
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool()]),
        turn_producer=graph,  # type: ignore[arg-type]
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            execution_engine="provider",
            model="session/model",
            hooks=hooks,
            providers=ProviderConfigs(custom={"session": ProviderEndpointConfig()}),
            context_window=RuntimeContextWindowConfig(
                compaction=RuntimeCompactionConfig(
                    summary_enabled=summary_enabled,
                    threshold_tokens=2_000,
                    keep_recent_tool_tokens=100,
                )
            ),
        ),
        model_provider_registry=ModelProviderRegistry(providers={"session": provider}),  # type: ignore[arg-type]
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    return runtime, graph


def _compacted_kind(response: object) -> object:
    payloads = [event.payload for event in response.events if event.event_type == _CONTEXT_COMPACTED]  # type: ignore[attr-defined]
    assert payloads, "no runtime.context_compacted event"
    return payloads[-1]["summary_kind"]


def test_opt_in_summary_reaches_the_provider_view_as_model_kind(tmp_path: Path) -> None:
    """Gate on + a working call: the stub's text is what the provider receives."""
    _big_workspace(tmp_path)
    provider = _SummaryModelProvider(outcome=_SUMMARY_TEXT)
    runtime, graph = _summary_runtime(tmp_path, provider=provider, summary_enabled=True)

    response = runtime.run(RuntimeRequest(prompt=_SUMMARY_PROMPT, session_id="summary-on"))

    assert "line 000" in provider.turn.requests[0].prompt  # the discarded content, not the summary
    projection = [text for text in graph.summary_segments if text]
    assert projection and _SUMMARY_TEXT in projection[-1]
    assert _compacted_kind(response) == "model"


def test_failed_summary_keeps_the_deterministic_text_and_reports_fallback(tmp_path: Path) -> None:
    """Gate on + a failing call: the run completes on the deterministic projection."""
    _big_workspace(tmp_path)
    provider = _SummaryModelProvider(outcome=RuntimeError("summary provider exploded"))
    runtime, graph = _summary_runtime(tmp_path, provider=provider, summary_enabled=True)

    response = runtime.run(RuntimeRequest(prompt=_SUMMARY_PROMPT, session_id="summary-off"))

    assert provider.turn.requests, "the summary lane was attempted"
    projection = [text for text in graph.summary_segments if text]
    assert projection and "Runtime context projection:" in projection[-1]
    assert "MODEL SUMMARY" not in projection[-1]
    assert _compacted_kind(response) == "fallback"


def test_blank_summary_degrades_like_a_failure(tmp_path: Path) -> None:
    """Gate on + a whitespace-only answer: no override, deterministic text, fallback."""
    _big_workspace(tmp_path)
    provider = _SummaryModelProvider(outcome="   \n  ")
    runtime, graph = _summary_runtime(tmp_path, provider=provider, summary_enabled=True)

    response = runtime.run(RuntimeRequest(prompt=_SUMMARY_PROMPT, session_id="summary-blank"))

    assert provider.turn.requests, "the summary lane was attempted"
    projection = [text for text in graph.summary_segments if text]
    assert projection and "Runtime context projection:" in projection[-1]
    assert projection[-1].strip() != ""
    assert _compacted_kind(response) == "fallback"


def test_gate_off_spends_no_provider_call(tmp_path: Path) -> None:
    """Gate off: the provider is never asked, and the payload reports deterministic."""
    _big_workspace(tmp_path)
    provider = _SummaryModelProvider(outcome=_SUMMARY_TEXT)
    runtime, graph = _summary_runtime(tmp_path, provider=provider, summary_enabled=False)

    response = runtime.run(RuntimeRequest(prompt=_SUMMARY_PROMPT, session_id="summary-disabled"))

    assert provider.turn.requests == [], "gate off must not spend a provider call"
    projection = [text for text in graph.summary_segments if text]
    assert projection and _SUMMARY_TEXT not in projection[-1]
    assert _compacted_kind(response) == "deterministic"


def test_cancelling_before_compact_hook_skips_the_model_call(tmp_path: Path) -> None:
    """Hook cancel wins the ordering: the opt-in provider call is never made."""
    _big_workspace(tmp_path)
    stdout = json.dumps({"action": "cancel", "diagnostic": "operator_hold"})
    provider = _SummaryModelProvider(outcome=_SUMMARY_TEXT)
    runtime, _graph = _summary_runtime(
        tmp_path,
        provider=provider,
        summary_enabled=True,
        hooks=RuntimeHooksConfig(enabled=True, on_before_compact=(("echo", stdout),)),
    )

    response = runtime.run(RuntimeRequest(prompt=_SUMMARY_PROMPT, session_id="summary-hook-cancel"))

    assert provider.turn.requests == [], "a cancelling hook must skip the model call"
    assert [event for event in response.events if event.event_type == _CONTEXT_COMPACTED] == []
