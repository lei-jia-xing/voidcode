"""Behavior tests for the cron plugin: schedule expressions and /loop parsing."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import pytest

from voidcode.plugins.cron import (
    DEFAULT_INTERVAL,
    USAGE_MESSAGE,
    ScheduleExpr,
    human_schedule,
    loop_confirmation_message,
    next_fire_after,
    parse_loop_command,
    parse_schedule,
)

UTC = datetime.UTC


def _local_epoch(*parts: int) -> int:
    """Epoch seconds for a wall-clock time in the machine's local timezone."""
    return int(datetime.datetime(*parts).astimezone().timestamp())


def _local_iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- accept table


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("once", ScheduleExpr(kind="once")),
        ("  ONCE  ", ScheduleExpr(kind="once")),
        ("every 30s", ScheduleExpr(kind="every", interval_seconds=30)),
        ("every 5m", ScheduleExpr(kind="every", interval_seconds=300)),
        ("every 2h", ScheduleExpr(kind="every", interval_seconds=7200)),
        ("every 1d", ScheduleExpr(kind="every", interval_seconds=86400)),
        ("EVERY 15M", ScheduleExpr(kind="every", interval_seconds=900)),
        ("  every   7m  ", ScheduleExpr(kind="every", interval_seconds=420)),
        ("cron */15 * * * *", ScheduleExpr(kind="cron", cron_fields="*/15 * * * *")),
        ("cron 0 9-17 * * 1-5", ScheduleExpr(kind="cron", cron_fields="0 9-17 * * 1-5")),
        ("cron 0 9,17 * * 1,3,5", ScheduleExpr(kind="cron", cron_fields="0 9,17 * * 1,3,5")),
        ("cron 5/10 * * * *", ScheduleExpr(kind="cron", cron_fields="5/10 * * * *")),
        ("cron 0 0-23/2 * * *", ScheduleExpr(kind="cron", cron_fields="0 0-23/2 * * *")),
        ("cron * * * * 7", ScheduleExpr(kind="cron", cron_fields="* * * * 7")),
        ("cron 0 0 1 1 *", ScheduleExpr(kind="cron", cron_fields="0 0 1 1 *")),
        ("  CRON   0   9  *  *  *  ", ScheduleExpr(kind="cron", cron_fields="0 9 * * *")),
    ],
)
def test_parse_schedule_accepts(text: str, expected: ScheduleExpr) -> None:
    assert parse_schedule(text) == expected


def test_parse_schedule_at_is_utc_exact() -> None:
    expr = parse_schedule("at 2030-01-01T12:00:00+00:00")
    assert expr.kind == "at"
    assert expr.at_epoch == int(datetime.datetime(2030, 1, 1, 12, 0, tzinfo=UTC).timestamp())


def test_parse_schedule_at_naive_is_local() -> None:
    expr = parse_schedule("at 2030-01-01T12:00:00")
    assert expr.at_epoch == _local_epoch(2030, 1, 1, 12, 0, 0)


# ---------------------------------------------------------------- reject table


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "sometimes",
        "every",
        "every 5",
        "every 5x",
        "every 0m",
        "every 5 m",
        "every5m",
        "every 5m extra",
        "at",
        "at not-a-date",
        "at 2030-13-01T00:00:00",
        "cron",
        "cron 0 9 * *",
        "cron 0 9 * * * *",
        "cron 0 9 L * *",
        "cron 0 9 l * *",
        "cron 0 9 15W * *",
        "cron 0 9 ? * *",
        "cron 0 9 * * 1#2",
        "cron @ 9 * * *",
        "cron 60 * * * *",
        "cron * 24 * * *",
        "cron * * 0 * *",
        "cron * * 32 * *",
        "cron * * * 0 *",
        "cron * * * 13 *",
        "cron * * * * 8",
        "cron 5-1 * * * *",
        "cron */0 * * * *",
        "cron */x * * * *",
        "cron x * * * *",
        "cron 1,,2 * * * *",
        "cron 1/2/3 * * * *",
    ],
)
def test_parse_schedule_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_schedule(text)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "Schedule expression cannot be empty."),
        ("sometimes", "Invalid schedule 'sometimes'."),
        ("every 0m", "Interval amount must be greater than 0: 'every 0m'."),
        ("at nope", "Invalid ISO-8601 timestamp in schedule 'at nope'"),
        ("cron 0 9 * *", "Cron expression must have exactly 5 fields, got 4 in 'cron 0 9 * *'."),
        ("cron 0 9 L * *", "Cron dialect does not support 'L' in 'cron 0 9 L * *'."),
        ("cron 0 9 ? * *", "Cron dialect does not support '?' in 'cron 0 9 ? * *'."),
        ("cron @ 9 * * *", "Cron dialect does not support '@' in 'cron @ 9 * * *'."),
        ("cron * * * * 1#2", "Cron dialect does not support '#' in 'cron * * * * 1#2'."),
        ("cron 0 9 15W * *", "Cron dialect does not support 'W' in 'cron 0 9 15W * *'."),
        ("cron 60 * * * *", "Value out of bounds (0-59) in minute: 60"),
        ("cron * * * * 8", "Value out of bounds (0-7) in day-of-week: 8"),
        ("cron 1,,2 * * * *", "Empty element in minute"),
        ("cron 5-1 * * * *", "Range out of bounds (0-59) in minute: '5-1'"),
        ("cron */0 * * * *", "Step in minute must be a positive integer: '0'"),
    ],
)
def test_parse_schedule_error_text(text: str, fragment: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_schedule(text)
    message = str(exc.value)
    assert fragment in message
    assert "Accepted grammar: 'every <N><s|m|h|d>', 'at <ISO-8601>', 'cron <min> <hour> <day> <month> <dow>', or 'once'" in message


# ---------------------------------------------------------- next_fire_after


def test_next_fire_after_once_returns_after_epoch() -> None:
    assert next_fire_after(ScheduleExpr(kind="once"), 1_700_000_000) == 1_700_000_000


def test_next_fire_after_every_adds_interval() -> None:
    expr = parse_schedule("every 15m")
    assert next_fire_after(expr, 1000) == 1900


def test_next_fire_after_at_fires_once_even_when_past() -> None:
    expr = parse_schedule("at 2020-01-01T00:00:00+00:00")
    assert expr.at_epoch is not None
    now = _local_epoch(2026, 1, 1, 0, 0, 0)
    assert expr.at_epoch < now
    assert next_fire_after(expr, now) == expr.at_epoch


def test_next_fire_after_cron_aligns_to_minute() -> None:
    expr = parse_schedule("cron */15 * * * *")
    after = _local_epoch(2026, 3, 10, 8, 30, 45)
    result = next_fire_after(expr, after)
    assert _local_iso(result) == "2026-03-10T08:45:00"
    assert result % 60 == 0


def test_next_fire_after_cron_is_strictly_after() -> None:
    expr = parse_schedule("cron */15 * * * *")
    after = _local_epoch(2026, 3, 10, 8, 45, 0)
    assert _local_iso(next_fire_after(expr, after)) == "2026-03-10T09:00:00"


def test_next_fire_after_cron_dom_or_dow_when_both_restricted() -> None:
    expr = parse_schedule("cron 0 0 1 * 1")
    after = _local_epoch(2026, 3, 2, 0, 0, 0)  # Monday
    result = next_fire_after(expr, after)
    # OR semantics: the next Monday (2026-03-09) wins over the next 1st of month.
    assert _local_iso(result) == "2026-03-09T00:00:00"


def test_next_fire_after_cron_dom_only_and_dow_only() -> None:
    after = _local_epoch(2026, 3, 2, 0, 0, 0)  # Monday
    assert _local_iso(next_fire_after(parse_schedule("cron 0 0 15 * *"), after)) == "2026-03-15T00:00:00"
    assert _local_iso(next_fire_after(parse_schedule("cron 0 0 * * 6"), after)) == "2026-03-07T00:00:00"


def test_next_fire_after_cron_sunday_is_zero_and_seven() -> None:
    after = _local_epoch(2026, 3, 2, 0, 0, 0)  # Monday
    as_zero = next_fire_after(parse_schedule("cron 0 12 * * 0"), after)
    as_seven = next_fire_after(parse_schedule("cron 0 12 * * 7"), after)
    assert as_zero == as_seven
    assert _local_iso(as_zero) == "2026-03-08T12:00:00"
    assert datetime.datetime.fromtimestamp(as_zero).astimezone().weekday() == 6


def test_next_fire_after_cron_gives_up_after_five_year_bound() -> None:
    expr = parse_schedule("cron 0 0 31 2 *")  # Feb 31 never exists
    with pytest.raises(ValueError, match="No matching fire time found within 5 years"):
        next_fire_after(expr, _local_epoch(2026, 1, 1, 0, 0, 0))


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        (ScheduleExpr(kind="every"), "Invalid interval_seconds for 'every' schedule"),
        (ScheduleExpr(kind="every", interval_seconds=0), "Invalid interval_seconds for 'every' schedule"),
        (ScheduleExpr(kind="at"), "Missing at_epoch for 'at' schedule"),
        (ScheduleExpr(kind="cron"), "Missing cron_fields for 'cron' schedule"),
        (ScheduleExpr(kind="bogus"), "Unknown schedule kind: 'bogus'"),  # type: ignore[arg-type]
    ],
)
def test_next_fire_after_invalid_expr(expr: ScheduleExpr, message: str) -> None:
    with pytest.raises(ValueError, match=message.replace("'", "'")):
        next_fire_after(expr, 1000)


# ------------------------------------------------------------ human_schedule


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (ScheduleExpr(kind="once"), "once"),
        (ScheduleExpr(kind="every", interval_seconds=30), "every 30s"),
        (ScheduleExpr(kind="every", interval_seconds=90), "every 90s"),
        (ScheduleExpr(kind="every", interval_seconds=120), "every 2m"),
        (ScheduleExpr(kind="every", interval_seconds=7200), "every 2h"),
        (ScheduleExpr(kind="every", interval_seconds=86400), "every 1d"),
        (ScheduleExpr(kind="every", interval_seconds=90000), "every 25h"),
        (ScheduleExpr(kind="every", interval_seconds=0), "every 0s"),
        (ScheduleExpr(kind="every"), "every 0s"),
        (ScheduleExpr(kind="cron", cron_fields="0 9 * * 1-5"), "cron 0 9 * * 1-5"),
        (ScheduleExpr(kind="cron"), "cron "),
        (ScheduleExpr(kind="at"), "at <unknown>"),
    ],
)
def test_human_schedule(expr: ScheduleExpr, expected: str) -> None:
    assert human_schedule(expr) == expected


def test_human_schedule_at_omits_seconds_when_zero() -> None:
    assert human_schedule(ScheduleExpr(kind="at", at_epoch=_local_epoch(2026, 5, 4, 12, 34, 0))) == "at 2026-05-04T12:34"
    assert human_schedule(ScheduleExpr(kind="at", at_epoch=_local_epoch(2026, 5, 4, 12, 34, 56))) == "at 2026-05-04T12:34:56"


# -------------------------------------------------------- parse_loop_command


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", (None, None)),
        ("   ", (None, None)),
        ("5m /babysit-prs", ("5m", "/babysit-prs")),
        ("30m check the deploy", ("30m", "check the deploy")),
        ("1H /standup 1", ("1h", "/standup 1")),
        ("2d audit the repo", ("2d", "audit the repo")),
        ("15s ping", ("15s", "ping")),
        ("15m   spaced   prompt  ", ("15m", "spaced   prompt")),
        ("5m", (None, None)),
        ("5m   ", (None, None)),
        ("check the deploy", ("10m", "check the deploy")),
        ("check the deploy every 20m", ("20m", "check the deploy")),
        ("check the deploy every 20 minutes", ("20m", "check the deploy")),
        ("check the deploy every 2hr", ("2h", "check the deploy")),
        ("check the deploy every 1days", ("1d", "check the deploy")),
        ("check the deploy every 3 seconds", ("3s", "check the deploy")),
        ("check the deploy EVERY 45 MIN", ("45m", "check the deploy")),
        ("every 20m", (None, None)),
        ("check the deploy every 20 fortnights", ("10m", "check the deploy every 20 fortnights")),
        ("check the deploy every", ("10m", "check the deploy every")),
        ("5m check the deploy every 20m", ("5m", "check the deploy every 20m")),
        ("5x check", ("10m", "5x check")),
        ("5 m check", ("10m", "5 m check")),
    ],
)
def test_parse_loop_command(args: str, expected: tuple[str | None, str | None]) -> None:
    assert parse_loop_command(args) == expected


def test_loop_default_interval_and_usage_message() -> None:
    assert DEFAULT_INTERVAL == "10m"
    assert parse_loop_command("") == (None, None)
    assert USAGE_MESSAGE.startswith("Usage: /loop [interval] <prompt>")
    assert "If no interval is specified, defaults to 10m." in USAGE_MESSAGE
    assert "Intervals: Ns, Nm, Nh, Nd (e.g. 5m, 30m, 2h, 1d). Minimum granularity is 1 minute." in USAGE_MESSAGE
    assert "/loop check the deploy          (defaults to 10m)" in USAGE_MESSAGE


@dataclass(frozen=True)
class _Summary:
    prompt: str
    human_schedule: str
    id: str


def test_loop_confirmation_message_text() -> None:
    message = loop_confirmation_message(_Summary(prompt="check the deploy", human_schedule="every 10m", id="abc123"))
    assert message == (
        "Scheduled 'check the deploy' on cadence 'every 10m' (job ID: abc123). "
        "Recurring tasks auto-expire after 7 days. "
        "To cancel sooner, use `cron_delete` or run `voidcode cron remove abc123`."
    )


def test_loop_confirmation_message_uses_parsed_schedule() -> None:
    interval, prompt = parse_loop_command("30m check the deploy")
    assert interval is not None and prompt is not None
    expr = parse_schedule(f"every {interval}")
    message = loop_confirmation_message(_Summary(prompt=prompt, human_schedule=human_schedule(expr), id="job-7"))
    assert message.startswith("Scheduled 'check the deploy' on cadence 'every 30m' (job ID: job-7).")
    assert message.endswith("use `cron_delete` or run `voidcode cron remove job-7`.")
