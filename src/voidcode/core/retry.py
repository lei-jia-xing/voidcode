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
from collections.abc import Callable, Iterator

from tenacity import (
    AttemptManager,
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
)

from voidcode.sdk.provider import ProviderAbortSignal, ProviderExecutionError

#: Total provider calls per turn (the first call plus this many minus one retries).
MAX_ATTEMPTS: int = 4
#: Backoff when the failure carries no structured wait: uniform(0, 1s), (0, 2s), (0, 4s)…
_BACKOFF_BASE_SECONDS: float = 1.0
_BACKOFF_CAP_SECONDS: float = 30.0
#: A structured ``retry_after`` above this refuses the retry instead of stalling the run.
MAX_RETRY_AFTER_SECONDS: float = 300.0
#: How often a backoff re-checks the abort signal.
_ABORT_POLL_SECONDS: float = 0.05


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


def retry_wait_seconds(state: RetryCallState) -> float:
    """Seconds before the next attempt: structured hint first, jittered backoff otherwise."""
    exc = state.outcome.exception() if state.outcome is not None else None
    hint = exc.retry_after if isinstance(exc, ProviderExecutionError) else None
    if hint is not None:
        return max(0.0, hint)
    return random.uniform(0.0, min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2.0 ** (state.attempt_number - 1))))


def retry_sleep(abort_signal: ProviderAbortSignal | None, *, provider: str, model: str) -> Callable[[float], None]:
    """Tenacity ``sleep`` that polls the run's abort signal instead of blocking blind."""

    def _sleep(seconds: float) -> None:
        if abort_signal is None:
            threading.Event().wait(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if abort_signal.cancelled:
                raise ProviderExecutionError(
                    kind="cancelled",
                    provider_name=provider,
                    model_name=model,
                    message="provider turn cancelled while waiting to retry",
                    retryable=False,
                    fallback_allowed=False,
                )
            time.sleep(min(_ABORT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

    return _sleep


def retrying_turn(abort_signal: ProviderAbortSignal | None, *, provider: str, model: str) -> Iterator[AttemptManager]:
    """Tenacity attempts for one provider turn; the caller fills ``request.attempt``."""
    yield from Retrying(
        retry=retry_if_exception(should_retry),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=retry_wait_seconds,
        sleep=retry_sleep(abort_signal, provider=provider, model=model),
        reraise=True,
    )
