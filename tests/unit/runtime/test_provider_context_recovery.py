"""Context-limit recovery: prune once, promote, or fail resumably.

Contract (``docs/contracts/runtime-config.md`` → context_limit recovery): a
provider ``context_limit`` answer is a runtime-owned lane. The runtime first
rebuilds the provider view with a recovery budget (the whole view must fit the
catalog window minus reserve) and retries the same call once per turn, announces
it with ``runtime.provider_context_recovery``, and otherwise promotes through the
existing fallback chain or fails in a way that ``sessions resume`` can retry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from voidcode.core.turns import TurnPlan
from voidcode.provider.config import ProviderConfigs, ProviderEndpointConfig, ProviderFallbackConfig
from voidcode.provider.protocol import ProviderExecutionError
from voidcode.runtime.config import RuntimeCompactionConfig, RuntimeConfig, RuntimeContextWindowConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.events import RUNTIME_PROVIDER_CONTEXT_RECOVERY, RUNTIME_PROVIDER_FALLBACK
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall
from voidcode.tools.read import ReadTool

SESSION_ID = "context-limit-session"
FILE_CHARS = 400_000
PROMPT = "summarize the workspace"


class _OverflowScript:
    """Scripted steps; an ``overflow`` entry raises a provider context_limit."""

    def __init__(
        self,
        script: list[TurnPlan | str],
        *,
        repeat_last: bool = False,
        provider: str = "session",
        model: str = "model",
    ) -> None:
        self._script = list(script)
        self.calls: list[Any] = []
        self._repeat_last = repeat_last
        self._provider = provider
        self._model = model

    def produce(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> TurnPlan:
        _ = session
        self.calls.append(request.assembled_context)
        if not self._script:
            raise AssertionError(f"script exhausted after {len(self.calls)} calls")
        nxt = self._script[0] if self._repeat_last and len(self._script) == 1 else self._script.pop(0)
        if nxt == "overflow":
            raise ProviderExecutionError(
                kind="context_limit",
                provider_name=self._provider,
                model_name=self._model,
                message="prompt is too long: 250000 tokens > 200000 maximum",
            )
        return nxt  # type: ignore[return-value]


#: Enough read payload that pruning clears the policy's savings floor (the
#: ``min_savings_tokens`` knob is no longer a config key).
READ_COUNT = 30
READ_LINES = 120


def _workspace(tmp_path: Path) -> None:
    body = "".join(f"line {index:03d} " + "y" * 20 + "\n" for index in range(READ_LINES))
    for index in range(READ_COUNT):
        (tmp_path / f"big-{index}.txt").write_text(body, encoding="utf-8")


def _reads() -> list[TurnPlan]:
    return [TurnPlan(tool_calls=(ToolCall(tool_name="read", arguments={"path": f"big-{index}.txt"}),)) for index in range(READ_COUNT)]


def _runtime(tmp_path: Path, graph: _OverflowScript, *, threshold: int = 2_000) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool()]),
        turn_producer=graph,
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            execution_engine="provider",
            model="session/model",
            providers=ProviderConfigs(custom={"session": ProviderEndpointConfig()}),
            context_window=RuntimeContextWindowConfig(
                compaction=RuntimeCompactionConfig(
                    threshold_tokens=threshold,
                    keep_recent_tool_tokens=100,
                )
            ),
        ),
        permission_policy=PermissionPolicy(mode="yolo"),
    )


def _chain_runtime(
    tmp_path: Path,
    graph: _OverflowScript,
    *,
    model: str,
    fallback_models: tuple[str, ...],
) -> VoidCodeRuntime:
    """Provider-engine runtime whose chain is declared through the real fallback config."""
    return VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool()]),
        turn_producer=graph,
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            execution_engine="provider",
            model=model,
            provider_fallback=ProviderFallbackConfig(preferred_model=model, fallback_models=fallback_models),
            context_window=RuntimeContextWindowConfig(
                compaction=RuntimeCompactionConfig(
                    threshold_tokens=2_000,
                    keep_recent_tool_tokens=100,
                )
            ),
        ),
        permission_policy=PermissionPolicy(mode="yolo"),
    )


def _events(chunks: list[Any], event_type: str) -> list[Any]:
    return [chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == event_type]


def test_promotion_prefers_a_strictly_larger_window_candidate(tmp_path: Path) -> None:
    """Chain order is not enough: a same-or-smaller first candidate is skipped."""
    _workspace(tmp_path)
    graph = _OverflowScript(
        [
            *_reads(),
            "overflow",
            "overflow",
        ],
        repeat_last=True,
        provider="google",
        model="gemini-2.5-flash",
    )

    chunks = list(
        _chain_runtime(
            tmp_path,
            graph,
            model="google/gemini-2.5-flash",  # 983k effective window
            fallback_models=("openai/gpt-4o", "openai/gpt-4.1"),  # 111k then 1.01M
        ).run_stream(RuntimeRequest(prompt=PROMPT, session_id=SESSION_ID))
    )

    promote = [event for event in _events(chunks, RUNTIME_PROVIDER_CONTEXT_RECOVERY) if event.payload["mode"] == "promote"]
    assert len(promote) == 1
    payload = promote[0].payload
    assert payload["promotion_reason"] == "larger_window"
    assert payload["candidate_count"] == 2
    assert payload["window_tokens_before"] is not None and payload["window_tokens_after"] is not None
    assert payload["window_tokens_after"] > payload["window_tokens_before"]
    fallback = _events(chunks, RUNTIME_PROVIDER_FALLBACK)
    assert [(event.payload["from_model"], event.payload["to_model"]) for event in fallback] == [("gemini-2.5-flash", "gpt-4.1")]
    assert [chunk.session.status for chunk in chunks][-1] == "failed"


def test_promotion_without_a_larger_candidate_keeps_the_chain_order_target(tmp_path: Path) -> None:
    """No strictly larger candidate: the existing chain order applies and the event says so."""
    _workspace(tmp_path)
    graph = _OverflowScript(
        [
            *_reads(),
            "overflow",
            "overflow",
        ],
        repeat_last=True,
        provider="openai",
        model="gpt-4.1",
    )

    chunks = list(
        _chain_runtime(
            tmp_path,
            graph,
            model="openai/gpt-4.1",  # 1.01M
            fallback_models=("openai/gpt-4o",),  # 111k: smaller, never claimed as an upgrade
        ).run_stream(RuntimeRequest(prompt=PROMPT, session_id=SESSION_ID))
    )

    promote = [event for event in _events(chunks, RUNTIME_PROVIDER_CONTEXT_RECOVERY) if event.payload["mode"] == "promote"]
    assert len(promote) == 1
    payload = promote[0].payload
    assert payload["promotion_reason"] == "no_larger_candidate"
    assert payload["window_tokens_before"] is not None and payload["window_tokens_after"] is not None
    assert payload["window_tokens_after"] < payload["window_tokens_before"]
    fallback = _events(chunks, RUNTIME_PROVIDER_FALLBACK)
    # Existing behaviour preserved for this lane: the chain-order next target.
    assert [(event.payload["from_model"], event.payload["to_model"]) for event in fallback] == [("gpt-4.1", "gpt-4o")]


def test_promotion_without_catalog_metadata_falls_back_without_crashing(tmp_path: Path) -> None:
    """A model the catalog does not describe cannot be compared; the run still ends."""
    _workspace(tmp_path)
    graph = _OverflowScript(
        [
            *_reads(),
            "overflow",
            "overflow",
        ],
        repeat_last=True,
    )

    chunks = list(_runtime(tmp_path, graph).run_stream(RuntimeRequest(prompt=PROMPT, session_id=SESSION_ID)))

    promote = [event for event in _events(chunks, RUNTIME_PROVIDER_CONTEXT_RECOVERY) if event.payload["mode"] == "promote"]
    assert len(promote) == 1
    payload = promote[0].payload
    assert payload["window_tokens_before"] is None
    assert payload["window_tokens_after"] is None
    assert payload["promotion_reason"] in {"no_larger_candidate", "unavailable"}
    assert chunks[-1].session.status == "failed"


def test_context_limit_prunes_and_retries_the_same_call(tmp_path: Path) -> None:
    _workspace(tmp_path)
    graph = _OverflowScript(
        [
            *_reads(),
            "overflow",
            TurnPlan(output="recovered", is_finished=True),
        ]
    )

    chunks = list(_runtime(tmp_path, graph).run_stream(RuntimeRequest(prompt=PROMPT, session_id=SESSION_ID)))

    recovery = _events(chunks, RUNTIME_PROVIDER_CONTEXT_RECOVERY)
    assert [(event.payload["mode"], event.payload["outcome"]) for event in recovery] == [("prune", "retry")]
    assert recovery[0].payload["reason"] == "context_limit"
    assert recovery[0].payload["provider"] == "session" and recovery[0].payload["model"] == "model"
    assert recovery[0].payload["dropped_tool_result_count"] > 0
    assert recovery[0].payload["usage_tokens_after"] < recovery[0].payload["usage_tokens_before"]
    assert recovery[0].payload["usage_tokens_estimated"] is True
    # The retried (last) call received the pruned view: pairing preserved, content replaced.
    retried_tool_segments = [segment for segment in graph.calls[-1].segments if segment.role == "tool"]
    assert len(retried_tool_segments) == READ_COUNT
    assert all((segment.metadata or {}).get("pruned") is True for segment in retried_tool_segments)
    assert chunks[-1].session.status == "completed"
    assert chunks[-1].output == "recovered"


def test_recovery_flag_tracks_the_max_winner_not_a_constant(tmp_path: Path) -> None:
    """A fresh high anchor wins the recovery decision and the flag reports it."""
    from voidcode.provider.protocol import ProviderTokenUsage

    _workspace(tmp_path)
    graph = _OverflowScript(
        [
            TurnPlan(
                tool_calls=(ToolCall(tool_name="read", arguments={"path": "big-0.txt"}),),
                provider_usage=ProviderTokenUsage(input_tokens=90_000, output_tokens=100),
            ),
            "overflow",
            TurnPlan(output="recovered", is_finished=True),
        ]
    )

    chunks = list(_runtime(tmp_path, graph).run_stream(RuntimeRequest(prompt=PROMPT, session_id=SESSION_ID)))

    recovery = _events(chunks, RUNTIME_PROVIDER_CONTEXT_RECOVERY)
    assert [(event.payload["mode"], event.payload["outcome"]) for event in recovery] == [("prune", "retry")]
    assert recovery[0].payload["measured_anchor_tokens"] == 90_100
    assert recovery[0].payload["usage_tokens_estimated"] is False
    assert chunks[-1].session.status == "completed"


def test_a_small_overshoot_is_still_pruned_by_recovery(tmp_path: Path) -> None:
    """Recovery ignores the routine savings floor: a tiny reclaim must still retry."""
    _workspace(tmp_path)
    graph = _OverflowScript(
        [
            TurnPlan(tool_calls=(ToolCall(tool_name="read", arguments={"path": "big-0.txt"}),)),
            "overflow",
            TurnPlan(output="recovered", is_finished=True),
        ]
    )

    chunks = list(_runtime(tmp_path, graph).run_stream(RuntimeRequest(prompt=PROMPT, session_id=SESSION_ID)))

    recovery = _events(chunks, RUNTIME_PROVIDER_CONTEXT_RECOVERY)
    assert [(event.payload["mode"], event.payload["outcome"]) for event in recovery] == [("prune", "retry")]
    # The reclaim here is far below the routine floor (DEFAULT_MIN_SAVINGS_TOKENS).
    assert recovery[0].payload["dropped_tool_result_count"] == 1
    assert chunks[-1].session.status == "completed"


def test_repeated_context_limit_recovers_once_then_fails_resumably(tmp_path: Path) -> None:
    _workspace(tmp_path)
    # Every later provider call overflows too: the runtime may try the promotion
    # path before giving up, and the assertions below only pin the invariants.
    graph = _OverflowScript(
        [
            *_reads(),
            "overflow",
            "overflow",
        ],
        repeat_last=True,
    )

    chunks = list(_runtime(tmp_path, graph).run_stream(RuntimeRequest(prompt=PROMPT, session_id=SESSION_ID)))

    recovery = _events(chunks, RUNTIME_PROVIDER_CONTEXT_RECOVERY)
    # One pruning retry per turn; the second failure is a promotion decision.
    assert [event.payload["mode"] for event in recovery] == ["prune", "promote"]
    assert recovery[0].payload["outcome"] == "retry"
    failed = _events(chunks, "runtime.failed")
    assert len(failed) == 1
    assert failed[0].payload["provider_error_kind"] == "context_limit"
    assert failed[0].payload["resumable"] is True
    assert failed[0].payload["context_limit_recovery"] == "prune"
    assert "resume" in str(failed[0].payload["guidance"])
    assert chunks[-1].session.status == "failed"

    # The failure stays resumable: a checkpoint exists and resume re-runs the turn.
    checkpoint = SqliteSessionStore().load_resume_checkpoint(workspace=tmp_path, session_id=SESSION_ID)
    assert checkpoint is not None

    resumed_graph = _OverflowScript([TurnPlan(output="resumed after shrinking", is_finished=True)])
    resumed = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool()]),
        turn_producer=resumed_graph,
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            execution_engine="provider",
            model="session/model",
            providers=ProviderConfigs(custom={"session": ProviderEndpointConfig()}),
        ),
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    response = resumed.resume(SESSION_ID)

    assert response.session.status == "completed"
