"""Provider-turn retry owned by the runtime, not the adapters.

Adapters classify failures (``ProviderExecutionError.retryable/retry_after``) and set
``max_retries=0`` on their own SDK clients; this module is the single executor. It
retries only a failure the adapter explicitly opted in (``retryable is True``),
waits the structured ``retry_after`` hint when present, otherwise an exponential
backoff with jitter, and stops after a bounded number of attempts or when the run's
abort signal fires. Cancellation raises the run's own
``ProviderExecutionError(kind="cancelled")`` so the caller's except-chain keeps its
shape: cancelled during backoff reads exactly like provider-reported cancellation.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable

from voidcode.sdk.provider import ProviderAbortSignal, ProviderExecutionError

#: Total provider calls per turn (the first call plus this many minus one retries).
MAX_ATTEMPTS: int = 4
#: Backoff when the failure carries no structured wait: uniform(0, 1s), (0, 2s), (0, 4s)…
_BACKOFF_BASE_SECONDS: float = 1.0
_BACKOFF_CAP_SECONDS: float = 30.0
#: A structured ``retry_after`` above this refuses the retry instead of stalling the run.
MAX_RETRY_AFTER_SECONDS: float = 300.0


def should_retry(exc: BaseException) -> bool:
    """Retry an explicit adapter opt-in, or a rate limit the adapter left undecided.

    ``retryable is None`` is undecided, never consent — except ``rate_limit``, whose
    lane the runtime owns: the adapter classifies it ``None`` precisely so this policy
    decides. An over-long structured wait refuses the retry instead of stalling the run.
    """
    if not isinstance(exc, ProviderExecutionError):
        return False
    if exc.retryable is False:
        return False
    if exc.retryable is None and exc.kind != "rate_limit":
        return False
    hint = exc.retry_after
    return hint is None or hint <= MAX_RETRY_AFTER_SECONDS


def _retry_delay_seconds(exc: ProviderExecutionError, attempt: int) -> float:
    """Seconds before the next attempt: structured hint first, jittered backoff otherwise."""
    if exc.retry_after is not None:
        return max(0.0, exc.retry_after)
    return random.uniform(0.0, min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2.0 ** (attempt - 1))))


def _cancelled_error(provider: str, model: str) -> ProviderExecutionError:
    return ProviderExecutionError(
        kind="cancelled",
        provider_name=provider,
        model_name=model,
        message="provider turn cancelled while waiting to retry",
        retryable=False,
        fallback_allowed=False,
    )


def _backoff_or_raise(abort_signal: ProviderAbortSignal | None, delay: float, *, provider: str, model: str) -> None:
    """One abort-aware backoff: a single blocking wait, never a poll."""
    if abort_signal is None:
        time.sleep(delay)
        return
    on_cancel = getattr(abort_signal, "on_cancel", None)
    if on_cancel is None:
        if abort_signal.cancelled:
            raise _cancelled_error(provider, model)
        time.sleep(delay)
        if abort_signal.cancelled:
            raise _cancelled_error(provider, model)
        return
    done = threading.Event()
    unregister = on_cancel(done.set)
    try:
        if done.wait(delay):
            raise _cancelled_error(provider, model)
    finally:
        unregister()


def run_with_retry[T](call: Callable[[int], T], abort_signal: ProviderAbortSignal | None, *, provider: str, model: str) -> T:
    """Run one provider turn, retrying adapter-approved failures with abort-aware backoff."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call(attempt)
        except ProviderExecutionError as exc:
            if not should_retry(exc) or attempt >= MAX_ATTEMPTS:
                raise
            _backoff_or_raise(abort_signal, _retry_delay_seconds(exc, attempt), provider=provider, model=model)
    raise AssertionError("unreachable: retry loop always returns or raises")
