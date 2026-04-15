from __future__ import annotations

import time

import pytest

from voidcode.provider.protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    normalize_provider_stream_event,
    wrap_provider_stream,
)


class _AbortSignal:
    def __init__(self, *, cancelled: bool) -> None:
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def test_normalize_provider_stream_event_enforces_required_fields() -> None:
    with pytest.raises(ValueError, match="requires text"):
        _ = normalize_provider_stream_event(ProviderStreamEvent(kind="delta"))

    with pytest.raises(ValueError, match="requires error"):
        _ = normalize_provider_stream_event(ProviderStreamEvent(kind="error", channel="error"))


def test_wrap_provider_stream_appends_done_when_missing() -> None:
    events = wrap_provider_stream(
        iter((ProviderStreamEvent(kind="delta", text="hello"),)),
        provider_name="openai",
        model_name="gpt-5",
        abort_signal=None,
        chunk_timeout_seconds=0.5,
    )

    chunks = list(events)
    assert chunks[0] == ProviderStreamEvent(kind="delta", text="hello")
    assert chunks[1] == ProviderStreamEvent(kind="done", done_reason="completed")


def test_wrap_provider_stream_emits_cancelled_when_abort_is_pre_set() -> None:
    chunks = list(
        wrap_provider_stream(
            iter((ProviderStreamEvent(kind="delta", text="ignored"),)),
            provider_name="openai",
            model_name="gpt-5",
            abort_signal=_AbortSignal(cancelled=True),
            chunk_timeout_seconds=0.5,
        )
    )

    assert [chunk.kind for chunk in chunks] == ["error", "done"]
    assert chunks[0].error_kind == "cancelled"
    assert chunks[1].done_reason == "cancelled"


def test_wrap_provider_stream_maps_chunk_timeout_to_transient_failure() -> None:
    def _slow_events():
        yield ProviderStreamEvent(kind="delta", text="a")
        time.sleep(0.02)
        yield ProviderStreamEvent(kind="delta", text="b")

    with pytest.raises(ProviderExecutionError, match="chunk timeout") as exc_info:
        _ = list(
            wrap_provider_stream(
                _slow_events(),
                provider_name="openai",
                model_name="gpt-5",
                abort_signal=None,
                chunk_timeout_seconds=0.001,
            )
        )

    assert exc_info.value.kind == "transient_failure"
