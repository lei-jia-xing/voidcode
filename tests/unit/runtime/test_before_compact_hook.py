"""before_compact hook wiring: executor outcome → BeforeCompactInput → seam.

Only cancel reaches the seam (with the hook reason); guidance and hook errors
fail open: the seam is untouched and compaction proceeds as configured.
"""

from __future__ import annotations

from typing import Literal

from voidcode.core.transcript import ToolResultView
from voidcode.runtime import EventEnvelope, RuntimeStreamChunk, SessionRef, SessionState
from voidcode.runtime.config import RuntimeCompactionConfig
from voidcode.runtime.context.window import (
    ContextWindowPolicy,
    prepare_provider_context,
)
from voidcode.runtime.hook_runtime import RuntimeHookOutcome, before_compact_input_from_hook_outcome
from voidcode.tools.contracts import TextOutput


def _result(content: str, tool_name: str = "read") -> ToolResultView:
    return ToolResultView("fixture-call", tool_name, {}, TextOutput(content), "ok")


def _over_budget_results() -> tuple[ToolResultView, ToolResultView]:
    # Sized so the reclaim crosses the production savings floor (20_000 tokens).
    return (_result("x" * 60_000), _result("y" * 60_000))


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
        policy=ContextWindowPolicy(default_tool_result_chars=None, compaction=RuntimeCompactionConfig(keep_recent_tool_tokens=0)),
        context_window=100,
        payload_bytes=0,
        before_compact=before_compact,
    )
    assert window.compacted is False
    assert window.compaction_reason == "operator_hold"


def test_non_cancel_guidance_leaves_the_seam_untouched() -> None:
    assert before_compact_input_from_hook_outcome(_outcome(payloads=({"guidance": "keep the deploy notes"},))) is None
    assert before_compact_input_from_hook_outcome(_outcome(payloads=({"guidance": "g" * 10_000},))) is None


def test_hook_error_fails_open_and_compaction_proceeds() -> None:
    assert before_compact_input_from_hook_outcome(_outcome(failed_error="boom")) is None
    assert before_compact_input_from_hook_outcome(_outcome(payloads=({"note": "not json-shaped"},))) is None
    window = prepare_provider_context(
        prompt="Summarize the workspace changes.",
        tool_results=_over_budget_results(),
        session_metadata={},
        policy=ContextWindowPolicy(default_tool_result_chars=None, compaction=RuntimeCompactionConfig(keep_recent_tool_tokens=0)),
        context_window=100,
        payload_bytes=0,
        before_compact=None,
    )
    assert window.compacted is True
