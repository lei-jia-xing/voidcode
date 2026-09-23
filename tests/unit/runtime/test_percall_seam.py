"""Per-call rewrite seam in assemble_provider_context: history untouched, markers wire-only."""

from __future__ import annotations

from copy import deepcopy

from voidcode.hook.percall import (
    PerCallChain,
    PerCallHandlerBinding,
    PerCallMessage,
    PerCallRewriteDecision,
    RewritePerCall,
    percall_cache_prefix,
)
from voidcode.runtime.context.window import (
    RuntimeContextSegment,
    assemble_provider_context,
)
from voidcode.tools.contracts import ToolResult


def _inject_ephemeral(messages: tuple[PerCallMessage, ...]) -> PerCallRewriteDecision:
    return RewritePerCall(
        messages=(*messages, PerCallMessage(role="system", content="per-call-only-hint", per_call=True)),
    )


_INJECTING_CHAIN = PerCallChain(
    bindings=(PerCallHandlerBinding(name="test-injector", handler=_inject_ephemeral),),
)


def _replayed() -> tuple[RuntimeContextSegment, ...]:
    return (
        RuntimeContextSegment(role="user", content="earlier question", metadata={"source": "replayed_conversation"}),
        RuntimeContextSegment(role="assistant", content="earlier answer", metadata={"source": "replayed_conversation"}),
    )


def test_percall_history_inputs_deep_equal_after_assemble() -> None:
    replayed = _replayed()
    replayed_snapshot = deepcopy(replayed)
    results = (ToolResult(tool_name="read", status="ok", content="file body"),)
    results_snapshot = deepcopy(results)
    assemble_provider_context(
        prompt="current question",
        tool_results=results,
        session_metadata={},
        replayed_conversation_segments=replayed,
        percall_chain=_INJECTING_CHAIN,
    )
    assert replayed == replayed_snapshot
    assert [r.content for r in results] == [r.content for r in results_snapshot]


def test_percall_markers_never_in_persisted_segments_or_metadata() -> None:
    assembled = assemble_provider_context(
        prompt="current question",
        tool_results=(),
        session_metadata={},
        percall_chain=_INJECTING_CHAIN,
    )
    persisted_text = "\n".join(segment.content or "" for segment in assembled.segments)
    assert "per-call-only-hint" not in persisted_text
    assert "per-call-only-hint" not in repr(assembled.metadata)
    assert "per-call-only-hint" not in repr(assembled.tool_results)
    wire_text = "\n".join(segment.content or "" for segment in assembled.percall_wire_segments)
    assert "per-call-only-hint" in wire_text
    assert all((segment.metadata or {}).get("per_call") is True for segment in assembled.percall_wire_segments)


def test_percall_markers_excluded_from_cache_prefix() -> None:
    assembled = assemble_provider_context(
        prompt="current question",
        tool_results=(),
        session_metadata={},
        percall_chain=_INJECTING_CHAIN,
    )
    prefix = assembled.metadata["percall_cache_prefix"]
    assert isinstance(prefix, str) and prefix
    bound = tuple(PerCallMessage(role=segment.role, content=segment.content or "") for segment in assembled.segments)
    assert prefix == percall_cache_prefix(bound)


def test_empty_chain_is_noop_wire() -> None:
    assembled = assemble_provider_context(
        prompt="current question",
        tool_results=(),
        session_metadata={},
    )
    assert assembled.percall_wire_segments == ()
