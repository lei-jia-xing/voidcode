"""The catalog's context window sizes compaction; explicit overrides still win."""

from __future__ import annotations

from voidcode.core.transcript import ToolResultView
from voidcode.provider.model_catalog import static_catalog_metadata
from voidcode.runtime.config import RuntimeCompactionConfig
from voidcode.runtime.context.window import ContextWindowPolicy, prepare_provider_context
from voidcode.runtime.coordinators.stream_prep import StreamPrepCoordinator
from voidcode.tools.contracts import TextOutput

#: No per-result cap and nothing kept recent: every result is prunable.
_POLICY = ContextWindowPolicy(default_tool_result_chars=None, compaction=RuntimeCompactionConfig(keep_recent_tool_tokens=0))


def _tool_results(count: int = 20) -> tuple[ToolResultView, ...]:
    return tuple(ToolResultView(f"fixture-{index}", "read", {}, TextOutput("x" * 5_000), "ok") for index in range(count))


class _Selection:
    provider = "opencode-go"
    model = "minimax-m3"


class _Target:
    selection = _Selection()


class _Resolved:
    active_target = _Target()


class _Config:
    resolved_provider = _Resolved()


def test_small_context_model_compacts_earlier_than_a_large_one() -> None:
    """The catalog window decides the trigger: the same view is pruned against a
    small window and left byte-identical against a large one."""
    results = _tool_results()  # 20 x 5000 chars ~ 25k tokens
    prompt = "x" * 200_000  # ~50k tokens at the estimator's chars-per-token

    small = prepare_provider_context(
        prompt=prompt,
        tool_results=results,
        session_metadata={},
        policy=_POLICY,
        context_window=30_000,
        payload_bytes=0,
    )
    large = prepare_provider_context(
        prompt=prompt,
        tool_results=results,
        session_metadata={},
        policy=_POLICY,
        context_window=400_000,
        payload_bytes=0,
    )

    assert small.compacted is True
    assert small.dropped_tool_result_count > 0
    assert large.compacted is False
    assert [view.output.text for view in large.tool_results] == [result.output.text for result in results]


def test_the_budget_is_the_models_input_cap_from_the_catalog() -> None:
    metadata = static_catalog_metadata("opencode-go", "minimax-m3")
    assert metadata is not None and metadata.max_input_tokens is not None

    budget = StreamPrepCoordinator._context_budget_for(_Config())  # type: ignore[arg-type]

    assert budget == metadata.max_input_tokens


def test_a_model_the_catalog_does_not_describe_leaves_compaction_unsized() -> None:
    class _UnknownSelection:
        provider = "opencode-go"
        model = "not-in-any-catalog"

    class _UnknownTarget:
        selection = _UnknownSelection()

    class _UnknownResolved:
        active_target = _UnknownTarget()

    class _UnknownConfig:
        resolved_provider = _UnknownResolved()

    assert StreamPrepCoordinator._context_budget_for(_UnknownConfig()) is None  # type: ignore[arg-type]


def test_an_explicit_window_override_beats_the_catalog_budget() -> None:
    """`prepare_provider_context`'s own arguments stay authoritative: a caller that
    names a window is not second-guessed by the catalog."""
    prompt = "x" * 200_000
    results = _tool_results()

    explicit = prepare_provider_context(
        prompt=prompt,
        tool_results=results,
        session_metadata={},
        policy=_POLICY,
        context_window=400_000,
        payload_bytes=0,
    )

    assert explicit.compacted is False
    assert explicit.dropped_tool_result_count == 0
