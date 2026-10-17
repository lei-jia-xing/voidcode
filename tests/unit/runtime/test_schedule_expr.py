from __future__ import annotations

import datetime

import pytest

from voidcode.runtime.background.schedule_expr import (
    ScheduleExpr,
    human_schedule,
    next_fire_after,
    parse_schedule,
)


def test_parse_schedule_every() -> None:
    expr_s = parse_schedule("every 30s")
    assert expr_s.kind == "every"
    assert expr_s.interval_seconds == 30

    expr_m = parse_schedule("every 10m")
    assert expr_m.kind == "every"
    assert expr_m.interval_seconds == 600

    expr_h = parse_schedule("every 2h")
    assert expr_h.kind == "every"
    assert expr_h.interval_seconds == 7200

    expr_d = parse_schedule("every 1d")
    assert expr_d.kind == "every"
    assert expr_d.interval_seconds == 86400

    # Case insensitive
    expr_upper = parse_schedule("EVERY 5M")
    assert expr_upper.interval_seconds == 300


def test_parse_schedule_every_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("every 0m")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("every -5s")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("every 10w")


def test_parse_schedule_at() -> None:
    expr = parse_schedule("at 2026-06-15T12:00:00")
    assert expr.kind == "at"
    assert expr.at_epoch is not None

    dt = datetime.datetime.fromtimestamp(expr.at_epoch).astimezone()
    assert dt.year == 2026
    assert dt.month == 6
    assert dt.day == 15
    assert dt.hour == 12

    # With timezone
    expr_utc = parse_schedule("at 2026-06-15T12:00:00Z")
    assert expr_utc.kind == "at"
    assert expr_utc.at_epoch is not None


def test_parse_schedule_at_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("at not-a-date")


def test_parse_schedule_cron() -> None:
    expr = parse_schedule("cron */5 * * * *")
    assert expr.kind == "cron"
    assert expr.cron_fields == "*/5 * * * *"

    expr2 = parse_schedule("cron 0 0 1 1 *")
    assert expr2.cron_fields == "0 0 1 1 *"

    # Range and comma list
    expr3 = parse_schedule("cron 0,30 9-17 * * 1-5")
    assert expr3.cron_fields == "0,30 9-17 * * 1-5"


def test_parse_schedule_cron_rejects_invalid() -> None:
    # Not 5 fields
    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron * * * *")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron * * * * * *")

    # Unsupported dialect characters: L, W, ?, #, @
    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron ? * * * *")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron 0 0 L * *")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron @hourly")

    # Out of bounds
    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron 60 * * * *")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron * 24 * * *")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron * * 32 * *")

    with pytest.raises(ValueError, match="Accepted grammar"):
        parse_schedule("cron * * * 13 *")


def test_parse_schedule_once() -> None:
    expr = parse_schedule("once")
    assert expr.kind == "once"
    assert expr.interval_seconds is None
    assert expr.at_epoch is None

    expr_caps = parse_schedule("ONCE")
    assert expr_caps.kind == "once"


def test_parse_schedule_invalid_grammar_message() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_schedule("hourly")
    assert "Accepted grammar: 'every <N><s|m|h|d>', 'at <ISO-8601>', 'cron <min> <hour> <day> <month> <dow>', or 'once'" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        parse_schedule("")
    assert "Accepted grammar" in str(excinfo.value)


def test_next_fire_after_once() -> None:
    expr = ScheduleExpr(kind="once")
    assert next_fire_after(expr, 1000) == 1000


def test_next_fire_after_every() -> None:
    expr = ScheduleExpr(kind="every", interval_seconds=300)
    assert next_fire_after(expr, 1000) == 1300


def test_next_fire_after_at() -> None:
    expr = ScheduleExpr(kind="at", at_epoch=2000)
    assert next_fire_after(expr, 1000) == 2000
    assert next_fire_after(expr, 2500) == 2000


def test_next_fire_after_cron_boundaries() -> None:
    expr = ScheduleExpr(kind="cron", cron_fields="*/5 * * * *")

    # Fixed base timestamp: 2026-01-01 12:00:00 local time
    dt_base = datetime.datetime(2026, 1, 1, 12, 0, 0).astimezone()
    t0 = int(dt_base.timestamp())

    # Exactly on boundary 12:00:00 -> next is 12:05:00
    next_t = next_fire_after(expr, t0)
    dt_next = datetime.datetime.fromtimestamp(next_t).astimezone()
    assert dt_next.hour == 12
    assert dt_next.minute == 5
    assert dt_next.second == 0

    # 1 second before boundary 12:04:59 -> next is 12:05:00
    next_59 = next_fire_after(expr, t0 + 299)
    assert next_59 == next_t

    # Exactly on 12:05:00 -> next is 12:10:00
    next_from_5 = next_fire_after(expr, next_t)
    dt_next_5 = datetime.datetime.fromtimestamp(next_from_5).astimezone()
    assert dt_next_5.hour == 12
    assert dt_next_5.minute == 10


def test_next_fire_after_cron_dow_sunday() -> None:
    # 0 and 7 are both Sunday
    expr_0 = ScheduleExpr(kind="cron", cron_fields="0 10 * * 0")
    expr_7 = ScheduleExpr(kind="cron", cron_fields="0 10 * * 7")

    # Friday 2026-01-02 12:00:00
    dt_fri = datetime.datetime(2026, 1, 2, 12, 0, 0).astimezone()
    t_fri = int(dt_fri.timestamp())

    t0_res = next_fire_after(expr_0, t_fri)
    t7_res = next_fire_after(expr_7, t_fri)

    # Next Sunday is 2026-01-04
    dt_res = datetime.datetime.fromtimestamp(t0_res).astimezone()
    assert dt_res.day == 4
    assert dt_res.hour == 10
    assert dt_res.minute == 0
    assert t0_res == t7_res


def test_human_schedule() -> None:
    assert human_schedule(ScheduleExpr(kind="once")) == "once"
    assert human_schedule(ScheduleExpr(kind="every", interval_seconds=30)) == "every 30s"
    assert human_schedule(ScheduleExpr(kind="every", interval_seconds=600)) == "every 10m"
    assert human_schedule(ScheduleExpr(kind="every", interval_seconds=7200)) == "every 2h"
    assert human_schedule(ScheduleExpr(kind="every", interval_seconds=86400)) == "every 1d"

    dt = datetime.datetime(2026, 1, 2, 15, 4, 0).astimezone()
    assert human_schedule(ScheduleExpr(kind="at", at_epoch=int(dt.timestamp()))) == "at 2026-01-02T15:04"

    assert human_schedule(ScheduleExpr(kind="cron", cron_fields="*/5 * * * *")) == "cron */5 * * * *"
