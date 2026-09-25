"""before_compact hook wiring: executor outcome → BeforeCompactInput → seam.

S2 (cancel): a cancelling hook skips compaction with the hook reason.
S1 (custom summary): hook guidance reaches the projector input, bounded by
the seam preview cap. Hook errors fail open: the seam is untouched.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.executor import LifecycleHookExecutionRequest, run_lifecycle_hooks
from voidcode.runtime import EventEnvelope, RuntimeStreamChunk, SessionRef, SessionState
from voidcode.runtime.context.window import (
    ContextWindowPolicy,
    prepare_provider_context,
)
from voidcode.runtime.hook_runtime import RuntimeHookOutcome, before_compact_input_from_hook_outcome
from voidcode.tools.contracts import ToolResult


def _result(content: str, tool_name: str = "read") -> ToolResult:
    return ToolResult(tool_name=tool_name, status="ok", content=content)


def _over_budget_results() -> tuple[ToolResult, ToolResult]:
    return (_result("x" * 4000), _result("y" * 4000))


def _session() -> SessionState:
    return SessionState(session=SessionRef(id="session-1"), status="running", turn=1)


def _outcome(
    *,
    action: Literal["continue", "cancel"] = "continue",
    failed_error: str | None = None,
    payloads: tuple[dict[str, object], ...] = (),
) -> RuntimeHookOutcome:
    session = _session()
    chunks = tuple(
        RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=index,
                event_type="runtime.before_compact",
                source="runtime",
                payload=dict(payload),
            ),
        )
        for index, payload in enumerate(payloads, start=1)
    )
    collected: list[str] = []
    for payload in payloads:
        guidance = payload.get("guidance")
        if isinstance(guidance, str) and guidance.strip():
            collected.append(guidance)
    return RuntimeHookOutcome(
        chunks=chunks,
        last_sequence=len(payloads),
        failed_error=failed_error,
        action=action,
        guidance=tuple(collected),
    )


def test_cancelling_hook_skips_compaction_with_reason() -> None:
    before_compact = before_compact_input_from_hook_outcome(
        _outcome(action="cancel", payloads=({"action": "cancel", "diagnostic": "operator_hold"},)),
    )
    assert before_compact is not None
    assert before_compact.cancel is True
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(keep_recent_tool_tokens=0, min_savings_tokens=1),
        context_window=100,
        payload_bytes=0,
        before_compact=before_compact,
    )
    assert window.compacted is False
    assert window.compaction_reason == "operator_hold"


def test_guidance_hook_reaches_summary_input_bounded() -> None:
    seen: dict[str, object] = {}

    def _projector(facts: Mapping[str, object]) -> str:
        seen.update(facts)
        return "model summary"

    before_compact = before_compact_input_from_hook_outcome(
        _outcome(payloads=({"guidance": "keep the deploy notes"},)),
    )
    assert before_compact is not None
    assert before_compact.cancel is False
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(summary_strategy="model_assisted", keep_recent_tool_tokens=0, min_savings_tokens=1),
        summary_projector=_projector,
        context_window=100,
        payload_bytes=0,
        before_compact=before_compact,
    )
    assert window.compacted is True
    assert seen.get("custom_summary") == "keep the deploy notes"

    oversized = before_compact_input_from_hook_outcome(
        _outcome(payloads=({"guidance": "g" * 10_000},)),
    )
    assert oversized is not None
    assert oversized.custom_summary is not None
    assert len(oversized.custom_summary) <= 240


def test_hook_error_fails_open_and_compaction_proceeds() -> None:
    assert before_compact_input_from_hook_outcome(_outcome(failed_error="boom")) is None
    assert before_compact_input_from_hook_outcome(_outcome(payloads=({"note": "not json-shaped"},))) is None
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(keep_recent_tool_tokens=0, min_savings_tokens=1),
        context_window=100,
        payload_bytes=0,
        before_compact=None,
    )
    assert window.compacted is True


def test_executor_cancel_action_parses_for_before_compact(tmp_path: Path) -> None:
    stdout = json.dumps({"action": "cancel", "diagnostic": "operator_hold"})
    hooks = RuntimeHooksConfig(on_before_compact=(("echo", stdout),))
    outcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="session-1",
            surface="before_compact",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=0,
        )
    )
    assert outcome.action == "cancel"
    assert outcome.events and outcome.events[0].event_type == "runtime.before_compact"


def test_guidance_last_wins_and_prior_items_become_extra_context() -> None:
    """Design §3: last non-empty guidance wins the summary; the rest is extra context."""
    before_compact = before_compact_input_from_hook_outcome(
        _outcome(payloads=({"guidance": "first note"}, {"guidance": "final summary"})),
    )
    assert before_compact is not None
    assert before_compact.custom_summary == "final summary"
    assert before_compact.extra_context == ("first note",)


def test_extra_context_is_bounded_and_carried_to_projector_input() -> None:
    seen: dict[str, object] = {}

    def _projector(facts: Mapping[str, object]) -> str:
        seen.update(facts)
        return "model summary"

    before_compact = before_compact_input_from_hook_outcome(
        _outcome(payloads=({"guidance": "x" * 5_000}, {"guidance": "tail"})),
    )
    assert before_compact is not None
    assert sum(len(item) for item in before_compact.extra_context) <= 2000

    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(summary_strategy="model_assisted", keep_recent_tool_tokens=0, min_savings_tokens=1),
        summary_projector=_projector,
        context_window=100,
        payload_bytes=0,
        before_compact=before_compact,
    )
    assert window.compacted is True
    assert seen.get("custom_summary") == "tail"
    assert "x" * 100 in str(seen.get("hook_extra_context"))


def test_single_guidance_item_leaves_extra_context_empty() -> None:
    before_compact = before_compact_input_from_hook_outcome(
        _outcome(payloads=({"guidance": "only"},)),
    )
    assert before_compact is not None
    assert before_compact.custom_summary == "only"
    assert before_compact.extra_context == ()
