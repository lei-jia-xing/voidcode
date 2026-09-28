"""Error/retry/timeout semantics: the two acceptance scenarios, the retry hints, the watchdogs."""

from __future__ import annotations

import time

import pytest

from voidcode.provider._wire_common import (
    iter_stream_with_timeout,
)
from voidcode.provider.errors import parse_provider_api_error, parse_provider_stream_error
from voidcode.provider.openai_native import _done_reason
from voidcode.provider.protocol import ProviderExecutionError


def test_a_429_with_a_retry_after_header_carries_the_wait_and_no_retry_verdict() -> None:
    parsed = parse_provider_api_error({"status_code": 429, "message": "Too Many Requests", "headers": {"retry-after": "7"}})

    assert parsed.kind == "rate_limit"
    # A usage/limit answer does not decide retryability at parse time: the runtime's
    # rate-limit lane owns it (see tests/unit/runtime/test_provider_rate_limit_lane.py).
    assert parsed.retryable is None
    assert parsed.fallback_allowed is True
    assert parsed.retry_after == 7.0


def test_a_generic_400_is_terminal() -> None:
    """A permanent client error must not be retried, whatever kind its message matched."""
    parsed = parse_provider_api_error({"status_code": 400, "code": "invalid_request_error", "message": "Invalid request"})

    assert parsed.kind == "transient_failure"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is True
    assert parsed.retry_after is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_every_4xx_except_408_and_429_is_terminal(status: int) -> None:
    assert parse_provider_api_error({"status_code": status, "message": "nope"}).retryable is False


@pytest.mark.parametrize("status", [408, 500, 502, 503])
def test_408_and_5xx_stay_retryable(status: int) -> None:
    assert parse_provider_api_error({"status_code": status, "message": "later"}).retryable is True


def test_a_402_is_a_usage_limit_error_not_a_transient_failure() -> None:
    parsed = parse_provider_api_error({"status_code": 402, "message": "Payment Required"})

    assert parsed.kind == "rate_limit"
    # Terminal for the provider lane (a 4xx that is not 408/429); the runtime routes
    # the rate_limit kind to its own quota lane rather than the provider retry lane.
    assert parsed.retryable is False


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after": "7"}, 7.0),
        ({"x-ratelimit-reset-ms": "2500"}, 2.5),
        ({"x-ratelimit-reset": "3"}, 3.0),
        # The longest parsed value wins across every header name.
        ({"retry-after-ms": "1500", "retry-after": "7", "x-ratelimit-reset": "2"}, 7.0),
        # An already-elapsed reset counter is not a hint.
        ({"x-ratelimit-reset": str(int(time.time()) - 3600)}, None),
    ],
)
def test_retry_after_header_names(headers: dict[str, str], expected: float | None) -> None:
    parsed = parse_provider_api_error({"status_code": 429, "headers": headers}).retry_after

    if expected is None:
        assert parsed is None
        return
    assert parsed == pytest.approx(expected, abs=1.0)


def test_a_reset_counter_large_enough_to_be_an_epoch_is_a_target_time() -> None:
    """OMP's `parseResetHeader` reads a big `x-ratelimit-reset*` counter as an absolute
    epoch (`utils/retry-after.ts:104-125`), never as a delta of that many seconds."""
    reset_at = int(time.time()) + 30
    parsed = parse_provider_api_error({"status_code": 429, "headers": {"x-ratelimit-reset": str(reset_at)}}).retry_after

    assert parsed is not None
    assert 0 < parsed <= 31


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Please retry in 12s", 12.0),
        ('{"retryDelay": "2500ms"}', 2.5),
        ("try again in 3 minutes", 180.0),
        ("resets in 45s", 45.0),
        ("reset after 1h2m3s", 3600.0),  # parsed 3723s, capped at voidcode's hour
        # "Resets in 2hr 15min": the compound remainder the generic pattern truncates
        # (the generic pattern alone would read only the "2hr").
        ("Resets in 2hr 15min", 3600.0),  # parsed 8100s, capped at voidcode's hour
        ("retry-after-ms=7200000", 3600.0),  # parsed 7200s, capped at voidcode's hour
        # An elapsed `reset at` stamp is not a hint.
        ("Your limit will reset at 2020-01-01T00:00:00Z", None),
    ],
)
def test_retry_after_body_patterns(message: str, expected: float | None) -> None:
    parsed = parse_provider_api_error({"status_code": 429, "message": message}).retry_after

    if expected is None:
        assert parsed is None
        return
    assert parsed == pytest.approx(expected, abs=1.0)


def test_a_reset_at_stamp_settles_only_when_no_relative_signal_exists() -> None:
    """A ``reset at`` wall clock that omits its zone is read as UTC (voidcode ships no
    per-provider reset timezone) and, like OMP's, resolves only when the message
    carries no unambiguous relative wait (``utils/fetch-retry.ts:135-165,241``)."""
    naive = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 120))
    aware = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 120))

    only_naive = parse_provider_api_error({"status_code": 429, "message": f"reset at {naive}"})
    naive_with_relative = parse_provider_api_error({"status_code": 429, "message": f"reset at {naive} - please retry in 3s"})
    only_aware = parse_provider_api_error({"status_code": 429, "message": f"Your limit will reset at {aware}"})

    assert only_naive.retry_after == pytest.approx(120.0, abs=2.0)
    assert naive_with_relative.retry_after == 3.0
    assert only_aware.retry_after == pytest.approx(120.0, abs=2.0)


def test_a_chinese_reset_at_stamp_is_read_the_same_way() -> None:
    naive = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 90))
    parsed = parse_provider_api_error({"status_code": 429, "message": f"账户额度将在 {naive} 重置"})

    assert parsed.retry_after == pytest.approx(90.0, abs=2.0)


def test_a_body_hint_competes_with_the_headers_instead_of_losing_to_them() -> None:
    parsed = parse_provider_api_error({"status_code": 429, "headers": {"retry-after": "7"}, "message": "Please retry in 12s"})

    assert parsed.retry_after == 12.0


def test_a_zero_retry_hint_survives_as_retry_now() -> None:
    assert parse_provider_api_error({"status_code": 429, "headers": {"retry-after": "0"}}).retry_after == 0.0


def test_a_non_positive_retry_after_ms_header_is_not_a_hint_but_its_body_form_is() -> None:
    """The millisecond *header* is a plain positive delta -- OMP's
    ``parseRetryAfterMsHeader`` drops anything <= 0 (``utils/retry-after.ts:79-85``) --
    while the ``retry-after-ms=0`` *body* spelling stays an explicit retry-now."""
    assert parse_provider_api_error({"status_code": 429, "headers": {"retry-after-ms": "0"}}).retry_after is None
    assert parse_provider_api_error({"status_code": 429, "headers": {"retry-after-ms": "-5"}}).retry_after is None
    assert parse_provider_api_error({"status_code": 429, "message": "retry-after-ms=0"}).retry_after == 0.0


def test_an_unknown_stop_reason_is_a_success() -> None:
    """OMP maps an unrecognized stop reason to a normal stop; voidcode keeps that."""
    assert _done_reason("something_new") == "unknown"
    assert _done_reason("") == "stop"
    assert _done_reason("length") == "length"


def test_usage_rides_the_voidcode_envelope_not_an_omp_event_shape() -> None:
    """Usage stays on ``ProviderStreamEvent``'s own field (a documented non-goal to copy
    OMP's accumulating-message shape)."""
    parsed = parse_provider_stream_error({"status_code": 500, "message": "boom"})
    assert parsed.retryable is True


def _never_yielding_stream() -> object:
    def generator():
        time.sleep(5)
        yield {"chunk": 1}

    return generator()


def test_the_first_event_watchdog_reports_the_missing_first_event() -> None:
    with pytest.raises(ProviderExecutionError) as failure:
        list(
            iter_stream_with_timeout(
                _never_yielding_stream(),  # type: ignore[arg-type]
                timeout_seconds=0.05,
                provider_name="p",
                model_name="m",
                first_event_timeout_seconds=0.05,
            )
        )

    assert failure.value.kind == "transient_failure"
    assert failure.value.retryable is True
    assert failure.value.message == "provider stream first-event timeout exceeded"


def test_a_first_token_slower_than_the_idle_gap_still_arrives() -> None:
    """The first-event budget is floored at the idle timeout
    (``utils/idle-iterator.ts:90``): a shorter ``first_event_timeout_seconds``
    never cuts the first token off, and a longer one extends only that first wait."""

    def slow_first_token():
        time.sleep(0.3)
        yield {"chunk": 1}

    assert list(
        iter_stream_with_timeout(
            slow_first_token(),  # type: ignore[arg-type]
            timeout_seconds=2.0,
            provider_name="p",
            model_name="m",
            first_event_timeout_seconds=0.05,
        )
    ) == [{"chunk": 1}]


def test_the_idle_watchdog_fires_between_chunks() -> None:
    def slow_generator():
        yield {"chunk": 1}
        time.sleep(5)
        yield {"chunk": 2}

    with pytest.raises(ProviderExecutionError) as failure:
        list(
            iter_stream_with_timeout(
                slow_generator(),  # type: ignore[arg-type]
                timeout_seconds=0.05,
                provider_name="p",
                model_name="m",
            )
        )

    assert failure.value.message == "provider stream chunk timeout exceeded"


def test_a_caller_abort_during_a_wait_is_cancelled_not_a_timeout() -> None:
    with pytest.raises(ProviderExecutionError) as failure:
        list(
            iter_stream_with_timeout(
                _never_yielding_stream(),  # type: ignore[arg-type]
                timeout_seconds=0.05,
                provider_name="p",
                model_name="m",
                aborted=lambda: True,
            )
        )

    assert failure.value.kind == "cancelled"
    assert failure.value.retryable is False
    assert failure.value.fallback_allowed is False
