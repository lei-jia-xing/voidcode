from __future__ import annotations

from typing import cast

from voidcode.runtime.context_continuity import verified_checkpoint_session_metadata


def test_verified_checkpoint_session_metadata_recovers_continuity_only_delta() -> None:
    checkpoint_metadata = cast(
        dict[str, object],
        {
            "runtime_config": {"execution_engine": "provider"},
            "context_window": {"compacted": True},
            "runtime_state": {
                "run_id": "run-1",
                "continuity": {
                    "version": 2,
                    "summary_text": "checkpoint summary",
                    "dropped_tool_result_count": 1,
                    "retained_tool_result_count": 1,
                    "source": "tool_result_window",
                    "distillation_source": "deterministic",
                },
                "continuity_summary": {"anchor": "continuity:abc"},
            },
        },
    )
    stored_metadata = cast(
        dict[str, object],
        {
            "runtime_config": {"execution_engine": "provider"},
            "runtime_state": {"run_id": "run-1"},
        },
    )

    assert (
        verified_checkpoint_session_metadata(
            checkpoint_metadata=checkpoint_metadata,
            stored_metadata=stored_metadata,
        )
        == checkpoint_metadata
    )


def test_verified_checkpoint_session_metadata_rejects_non_context_delta() -> None:
    checkpoint_metadata = cast(
        dict[str, object],
        {
            "runtime_config": {"execution_engine": "provider"},
            "runtime_state": {"continuity": {"summary_text": "checkpoint"}},
        },
    )
    stored_metadata = cast(
        dict[str, object],
        {
            "runtime_config": {"execution_engine": "deterministic"},
        },
    )

    assert (
        verified_checkpoint_session_metadata(
            checkpoint_metadata=checkpoint_metadata,
            stored_metadata=stored_metadata,
        )
        is None
    )
