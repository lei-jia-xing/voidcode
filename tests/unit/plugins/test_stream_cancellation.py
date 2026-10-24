"""Cancellation has the same meaning for transport and queue timeouts."""

from collections.abc import Iterator
from threading import Event

import pytest

from voidcode.capabilities.providers.native._wire_common import iter_stream_with_timeout
from voidcode.sdk.provider import ProviderExecutionError


@pytest.mark.parametrize("cancelled", [False, True])
def test_transport_error_after_partial_output_preserves_caller_intent(cancelled: bool) -> None:
    release = Event()
    abort = Event()
    failure = TimeoutError("transport read timed out")

    def transport() -> Iterator[object]:
        yield "partial"
        assert release.wait(timeout=5), "test did not release the transport"
        raise failure

    stream = iter_stream_with_timeout(transport(), timeout_seconds=5, provider_name="test", model_name="test-model", aborted=abort.is_set)
    assert next(stream) == "partial"
    if cancelled:
        abort.set()
    release.set()
    if cancelled:
        with pytest.raises(ProviderExecutionError) as exc:
            list(stream)
        assert exc.value.kind == "cancelled"
        assert exc.value.retryable is False
        assert exc.value.fallback_allowed is False
    else:
        with pytest.raises(TimeoutError) as exc:
            list(stream)
        assert exc.value is failure


def test_queue_timeout_closes_stream_once_and_releases_worker() -> None:
    release = Event()
    stopped = Event()
    closed = []

    class Stream:
        def __iter__(self):
            return self

        def __next__(self):
            try:
                assert release.wait(timeout=5)
                raise StopIteration
            finally:
                stopped.set()

        def close(self):
            closed.append(1)
            release.set()

    with pytest.raises(ProviderExecutionError, match="first-event timeout"):
        list(iter_stream_with_timeout(Stream(), timeout_seconds=0.01, provider_name="test", model_name="model"))
    assert stopped.wait(timeout=5)
    assert closed == [1]


@pytest.mark.parametrize("after_partial", [False, True])
def test_pending_abort_does_not_wait_for_the_network_timeout(after_partial):
    from threading import Timer
    from time import monotonic

    abort = Event()
    release = Event()
    stopped = Event()
    closed = []

    class Stream:
        sent = False

        def __iter__(self):
            return self

        def __next__(self):
            if after_partial and not self.sent:
                self.sent = True
                return "partial"
            try:
                assert release.wait(timeout=5)
                raise StopIteration
            finally:
                stopped.set()

        def close(self):
            closed.append(1)
            release.set()

    stream = iter_stream_with_timeout(Stream(), timeout_seconds=5, provider_name="test", model_name="model", aborted=abort.is_set)
    if after_partial:
        assert next(stream) == "partial"
    timer = Timer(0.05, abort.set)
    started = monotonic()
    timer.start()
    try:
        with pytest.raises(ProviderExecutionError) as caught:
            list(stream)
        assert caught.value.kind == "cancelled"
        assert monotonic() - started < 1
        assert stopped.wait(timeout=1)
        assert closed == [1]
    finally:
        timer.cancel()
        release.set()
