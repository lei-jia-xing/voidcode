"""Unit tests for the cron plugin: state, tools, /loop, and fact-driven settlement."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.cron import create_plugin, cron_plugin
from voidcode.plugins.cron.cron_plugin import (
    DEFAULT_RECURRING_EXPIRY_SECONDS,
    DELETE_IN_FLIGHT_NOTE,
    MAX_SCHEDULES_PER_SESSION,
    MIN_EVERY_INTERVAL_SECONDS,
    SCHEMA_VERSION,
    CronCreateTool,
    CronDeleteTool,
    CronListTool,
    CronLoopCommand,
    CronState,
    PendingFire,
    ScheduleEntry,
    cron_observer,
)
from voidcode.plugins.cron.loop_command import DEFAULT_INTERVAL, USAGE_MESSAGE, loop_confirmation_message
from voidcode.plugins.cron.schedule_expr import next_fire_after, parse_schedule
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.hooks import InvocationFinishedFact, RunFinishedFact, RunRequestedFact, WakeupFiredFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import BudgetExhaustion
from voidcode.sdk.tools import ToolCall, ToolFailure, ToolSuccess

OWNER = "cron"
SESSION_ID = "session-1"
#: Pinned wall clock: the whole suite computes fire instants against this instant.
NOW_MS = 1_700_000_000_000
NOW_SECONDS = NOW_MS // 1000
DEFAULT_EXPIRY = NOW_MS + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron_plugin, "_clock_ms", lambda: NOW_MS)


@pytest.fixture
def services() -> FakeServices:
    return FakeServices(owner=OWNER)


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id=SESSION_ID,
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
    )


def _invoke_tool(tool: CronCreateTool | CronDeleteTool | CronListTool, services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]):
    call = ToolCall(tool_name=tool.definition.name, arguments=arguments, tool_call_id="call-1")
    return tool.invoke(call, context=_context(tmp_path), services=services)


def _create(services: FakeServices, tmp_path: Path, schedule: str, prompt: str = "keep building") -> str:
    result = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": schedule, "prompt": prompt})
    assert isinstance(result, ToolSuccess), result
    return str(json.loads(result.output.text)["id"])


def _stored(services: FakeServices) -> CronState:
    return cron_plugin._store(services).load()[0]


def _seed(payload: Mapping[str, object], *, version: int = 1) -> FakeServices:
    services = FakeServices(owner=OWNER)
    services.seed_state(payload, version=version)
    return services


def _entry(services: FakeServices, schedule_id: str) -> ScheduleEntry:
    return _stored(services).schedules[schedule_id]


def _pending(
    schedule_id: str,
    *,
    wakeup_id: str = "wakeup-fired",
    sequence: int = 7,
    prompt: str = "keep building",
    role: Literal["system", "user"] = "user",
) -> PendingFire:
    return PendingFire(
        wakeup_id=wakeup_id,
        fired_sequence=sequence,
        role=role,
        payload={"kind": "cron", "text": prompt, "schedule_id": schedule_id},
    )


def _schedule_payload(
    *,
    schedule_id: str,
    schedule_text: str = "every 10m",
    kind: str = "every",
    prompt: str = "keep building",
    expires_at: int | None = DEFAULT_EXPIRY,
    enabled: bool = True,
    wakeup_id: str | None = "wakeup-fired",
    next_fire_at: int | None = NOW_MS + 600_000,
    pending: PendingFire | None = None,
    last_finished_sequence: int = 0,
) -> dict[str, object]:
    if pending is not None:
        next_fire_at = None
        wakeup_id = pending.wakeup_id
    if not enabled:
        wakeup_id = None
        next_fire_at = None
        pending = None
    entry = ScheduleEntry(
        schedule_text=schedule_text,
        kind=kind,  # type: ignore[arg-type]
        prompt=prompt,
        enabled=enabled,
        expires_at=expires_at,
        wakeup_id=wakeup_id,
        next_fire_at=next_fire_at,
        pending=pending,
    )
    return CronState(schedules={schedule_id: entry}, last_finished_sequence=last_finished_sequence).to_payload()


def _fired_event(
    *,
    sequence: int,
    run_id: str = "run-claim",
    schedule_id: str,
    prompt: str = "keep building",
    wakeup_id: str = "wakeup-fired",
    owner: str = OWNER,
    payload: Mapping[str, object] | None = None,
) -> WakeupFiredFact:
    return WakeupFiredFact(
        session_id=SESSION_ID,
        event_sequence=sequence,
        run_id=run_id,
        revision_id="rev-1",
        logical_turn="turn-1",
        owner=owner,
        wakeup_id=wakeup_id,
        role="user",
        payload=payload or {"kind": "cron", "text": prompt, "schedule_id": schedule_id},
    )


def _run_requested(sequence: int, *, run_id: str = "run-1", resumed: bool = False) -> RunRequestedFact:
    return RunRequestedFact(
        session_id=SESSION_ID,
        run_id=run_id,
        revision_id="rev-1",
        logical_turn="turn-1",
        event_sequence=sequence,
        resumed=resumed,
    )


def _trigger(services: FakeServices, tmp_path: Path, *facts: object, trigger: object | None = None) -> None:
    """Seed the committed fact stream and invoke the observer's catch-up trigger."""
    services.facts = list(facts)
    chosen = trigger if trigger is not None else (facts[-1] if facts else _run_requested(9999))
    cron_observer(chosen, _context(tmp_path), services)  # type: ignore[arg-type]


def _run_finished(
    status: Literal["completed", "failed", "interrupted"] = "completed",
    *,
    sequence: int = 9,
    run_id: str = "run-resumed",
    stop_cause: Literal["completed", "error", "user_stop", "owner_cancel", "limit_reached", "shutdown"] | None = None,
    budget_exhaustion: BudgetExhaustion | None = None,
) -> RunFinishedFact:
    return RunFinishedFact(
        session_id=SESSION_ID,
        run_id=run_id,
        revision_id="rev-1",
        logical_turn="turn-1",
        event_sequence=sequence,
        status=status,
        stop_cause=stop_cause or ("completed" if status == "completed" else "error"),
        budget_exhaustion=budget_exhaustion,
    )


def _root_budget_exhausted() -> BudgetExhaustion:
    return BudgetExhaustion(
        dimension="root_actions",
        source="root_action_budget",
        limit=3,
        consumed=3,
        remaining=0,
        continue_with="extend_root_actions",
    )


# --- create ---


@pytest.mark.parametrize(
    ("schedule", "prompt"),
    [
        ("every 10m", "check the deploy"),
        ("every 60s", "tick"),
        ("every 6d", "slow"),
        ("once", "run now"),
        ("at 2026-12-01T09:00:00", "wake up"),
        ("cron */5 * * * *", "poll"),
    ],
)
def test_create_stages_the_first_fire_computed_by_the_expression(services: FakeServices, tmp_path: Path, schedule: str, prompt: str) -> None:
    result = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": schedule, "prompt": prompt})

    assert isinstance(result, ToolSuccess)
    payload = json.loads(result.output.text)
    schedule_id = str(payload["id"])
    expr = parse_schedule(schedule)
    expected_fire_ms = next_fire_after(expr, NOW_SECONDS) * 1000

    entry = _entry(services, schedule_id)
    assert entry.schedule_text == schedule
    assert entry.kind == expr.kind
    assert entry.prompt == prompt
    assert entry.enabled is True
    assert entry.next_fire_at == expected_fire_ms
    assert entry.pending is None
    assert payload["next_fire_at"] == expected_fire_ms
    assert payload["expires_at"] == entry.expires_at

    # The wakeup is staged with the state; no durable row exists before the result commit.
    assert len(services.staged_wakeups) == 1
    staged = services.staged_wakeups[0]
    assert staged["payload"] == {"kind": "cron", "text": prompt, "schedule_id": schedule_id}
    assert staged["due_at_ms"] == expected_fire_ms
    assert staged["role"] == "user"
    assert staged["idempotency_key"] == f"cron:{schedule_id}:{expected_fire_ms}"
    assert services.wakeups == {}
    assert [name for name, _payload in services.staged_events] == ["schedule_created"]
    event = services.staged_events[0][1]
    assert event["id"] == schedule_id
    assert event["kind"] == expr.kind
    assert event["next_fire_at"] == expected_fire_ms
    assert event["wakeup_id"] == entry.wakeup_id
    assert _stored(services).last_finished_sequence == 0


def test_default_expiry_applies_to_recurring_schedules_only(services: FakeServices, tmp_path: Path) -> None:
    for schedule, expected in (
        ("every 10m", DEFAULT_EXPIRY),
        ("cron 0 9 * * *", DEFAULT_EXPIRY),
        ("once", None),
        ("at 2026-12-01T09:00:00", None),
    ):
        schedule_id = _create(services, tmp_path, schedule)
        assert _entry(services, schedule_id).expires_at == expected, schedule


@pytest.mark.parametrize(
    ("schedule", "fragment"),
    [
        ("every 1s", "at least 1 minute"),
        ("every 59s", "at least 1 minute"),
        ("every 7d", "shorter than the 7-day recurring expiry"),
        ("every 8d", "shorter than the 7-day recurring expiry"),
        ("cron 0 0 1 1 *", "at or after the 7-day recurring expiry"),
    ],
)
def test_create_rejects_schedules_outside_the_recurring_policy(services: FakeServices, tmp_path: Path, schedule: str, fragment: str) -> None:
    services.seed_state(CronState().to_payload(), version=3)
    result = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": schedule, "prompt": "x"})

    assert isinstance(result, ToolFailure)
    assert fragment in result.error
    assert services.staged_wakeups == []
    assert services.staged_events == []
    assert services.read_state("session") == (CronState().to_payload(), 3)


@pytest.mark.parametrize("schedule", ["banana", "every 0s", "every 10 fortnights", "cron * * *", "at not-a-date", "cron 0 9 * * L"])
def test_invalid_schedule_is_rejected_without_side_effects(services: FakeServices, tmp_path: Path, schedule: str) -> None:
    services.seed_state(CronState().to_payload(), version=3)
    result = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": schedule, "prompt": "x"})

    assert isinstance(result, ToolFailure)
    assert result.error
    assert services.wakeups == {}
    assert services.staged_wakeups == []
    assert services.staged_events == []
    assert services.read_state("session") == (CronState().to_payload(), 3)


@pytest.mark.parametrize(
    "arguments",
    [
        {"prompt": "x"},
        {"schedule": "every 10m"},
        {"schedule": "   ", "prompt": "x"},
        {"schedule": "every 10m", "prompt": "  "},
    ],
)
def test_missing_or_blank_arguments_are_rejected(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]) -> None:
    result = _invoke_tool(CronCreateTool(), services, tmp_path, arguments)
    assert isinstance(result, ToolFailure)
    assert services.wakeups == {}
    assert services.staged_wakeups == []
    assert services.staged_events == []


def test_schedule_cap_is_enforced_per_session(services: FakeServices, tmp_path: Path) -> None:
    schedules = {
        f"sched-{index}": ScheduleEntry(
            schedule_text="every 10m",
            kind="every",
            prompt="x",
            enabled=True,
            expires_at=DEFAULT_EXPIRY,
            wakeup_id=f"wakeup-{index}",
            next_fire_at=NOW_MS + 10 * 60_000,
            pending=None,
        )
        for index in range(MAX_SCHEDULES_PER_SESSION)
    }
    services.seed_state(CronState(schedules=schedules).to_payload())

    result = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": "every 10m", "prompt": "x"})
    assert isinstance(result, ToolFailure)
    assert f"maximum {MAX_SCHEDULES_PER_SESSION} jobs" in result.error
    assert services.staged_wakeups == []
    assert len(_stored(services).schedules) == MAX_SCHEDULES_PER_SESSION


# --- delete and list ---


def test_delete_stages_cancel_with_the_state_removal(services: FakeServices, tmp_path: Path) -> None:
    kept = _create(services, tmp_path, "every 10m", "keep me")
    removed = _create(services, tmp_path, "every 1h", "drop me")
    armed = _entry(services, removed).wakeup_id
    assert armed is not None

    result = _invoke_tool(CronDeleteTool(), services, tmp_path, {"id": removed})

    assert isinstance(result, ToolSuccess)
    assert json.loads(result.output.text) == {"id": removed, "note": DELETE_IN_FLIGHT_NOTE}
    assert services.staged_wakeup_cancels == [armed]
    assert set(_stored(services).schedules) == {kept}
    assert services.wakeups == {}
    deleted = services.staged_events[-1]
    assert deleted[0] == "schedule_deleted"
    assert deleted[1]["id"] == removed
    assert deleted[1]["wakeup_id"] == armed
    assert deleted[1]["note"] == DELETE_IN_FLIGHT_NOTE


def test_delete_rejects_unknown_and_missing_ids(services: FakeServices, tmp_path: Path) -> None:
    assert isinstance(_invoke_tool(CronDeleteTool(), services, tmp_path, {"id": " "}), ToolFailure)
    unknown = _invoke_tool(CronDeleteTool(), services, tmp_path, {"id": "sched-nope"})
    assert isinstance(unknown, ToolFailure)
    assert unknown.error == "Schedule 'sched-nope' not found."


def test_list_reports_the_stored_armed_next_fire_and_pending_state(services: FakeServices, tmp_path: Path) -> None:
    every = _create(services, tmp_path, "every 10m", "poll")
    once = _create(services, tmp_path, "once", "now please")
    disabled = _create(services, tmp_path, "every 30m", "later")
    state = _stored(services)
    services.seed_state(
        replace(
            state,
            schedules={
                **state.schedules,
                disabled: replace(state.schedules[disabled], enabled=False, wakeup_id=None, next_fire_at=None, pending=None),
            },
        ).to_payload()
    )

    result = _invoke_tool(CronListTool(), services, tmp_path, {})
    assert isinstance(result, ToolSuccess)
    jobs = {job["id"]: job for job in json.loads(result.output.text)["jobs"]}
    assert jobs[every]["next_fire_at"] == NOW_MS + 600_000
    assert jobs[every]["status"] == "enabled"
    assert jobs[every]["pending_wakeup_id"] is None
    assert jobs[once]["next_fire_at"] == NOW_MS
    assert jobs[disabled]["next_fire_at"] is None
    assert jobs[disabled]["status"] == "disabled"

    schedule_id = every
    entry = _entry(services, schedule_id)
    _trigger(
        services,
        tmp_path,
        _fired_event(sequence=5, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id), prompt="poll"),
    )
    listed = _invoke_tool(CronListTool(), services, tmp_path, {})
    assert isinstance(listed, ToolSuccess)
    pending_jobs = {job["id"]: job for job in json.loads(listed.output.text)["jobs"]}
    assert pending_jobs[schedule_id]["status"] == "running"
    assert pending_jobs[schedule_id]["next_fire_at"] is None
    assert pending_jobs[schedule_id]["pending_wakeup_id"] == entry.wakeup_id


# --- wakeup facts and settlement ---


def test_wakeup_fired_records_pending_even_for_a_long_session(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)

    _trigger(
        services,
        tmp_path,
        _fired_event(sequence=10_000, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id)),
    )

    updated = _entry(services, schedule_id)
    assert updated.pending is not None
    assert updated.pending.wakeup_id == entry.wakeup_id
    assert updated.pending.fired_sequence == 10_000
    assert updated.pending.role == "user"
    assert updated.next_fire_at is None
    assert len(services.staged_wakeups) == 1  # only the creation arm
    assert services.staged_events == [("schedule_created", services.staged_events[0][1])]
    assert _stored(services).facts_cursor == 10_000


def test_duplicate_wakeup_delivery_is_idempotent(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)
    fact = _fired_event(sequence=7, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id))

    _trigger(services, tmp_path, fact)
    version = services.read_state("session")[1]
    pending = _entry(services, schedule_id).pending
    _trigger(services, tmp_path, fact)

    assert _entry(services, schedule_id).pending == pending
    assert services.read_state("session")[1] == version


def test_foreign_or_stale_firings_are_ignored(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)
    facts = (
        _fired_event(sequence=5, schedule_id="sched-unknown", wakeup_id="w"),
        _fired_event(sequence=6, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id), payload={"kind": "goal"}),
        _fired_event(sequence=7, schedule_id=schedule_id, wakeup_id="stale"),
        _fired_event(sequence=8, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id), owner="other"),
    )
    _trigger(services, tmp_path, *facts)

    assert _entry(services, schedule_id).pending is None
    assert all(name == "schedule_created" for name, _payload in services.staged_events)
    assert _stored(services).facts_cursor == 8


def test_terminal_run_rearms_a_recurring_schedule_even_with_a_new_run_id(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            schedule_text="every 10m",
            pending=_pending("sched-1", sequence=7),
        )
    )

    _trigger(services, tmp_path, _run_finished("completed", sequence=20, run_id="run-resumed"))

    expected_fire_ms = next_fire_after(parse_schedule("every 10m"), NOW_SECONDS) * 1000
    entry = _entry(services, "sched-1")
    assert entry.enabled is True
    assert entry.pending is None
    assert entry.next_fire_at == expected_fire_ms
    assert entry.wakeup_id is not None
    assert services.staged_wakeups[-1]["due_at_ms"] == expected_fire_ms
    assert services.staged_wakeups[-1]["payload"] == {"kind": "cron", "text": "keep building", "schedule_id": "sched-1"}
    assert services.staged_wakeups[-1]["idempotency_key"] == f"cron:sched-1:{expected_fire_ms}"
    assert services.staged_events == [
        (
            "schedule_fired",
            {
                "outcome": "rearmed",
                "id": "sched-1",
                "wakeup_id": "wakeup-fired",
                "fired_sequence": 7,
                "run_status": "completed",
                "next_fire_at": expected_fire_ms,
            },
        )
    ]
    stored = _stored(services)
    assert stored.last_finished_sequence == 20


def test_failed_recurring_run_still_rearms(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            schedule_text="cron 0 9 * * *",
            kind="cron",
            pending=_pending("sched-1", sequence=3),
        )
    )

    _trigger(services, tmp_path, _run_finished("failed", sequence=8))

    entry = _entry(services, "sched-1")
    assert entry.enabled is True
    assert entry.pending is None
    assert services.staged_wakeups[-1]["due_at_ms"] == next_fire_after(parse_schedule("cron 0 9 * * *"), NOW_SECONDS) * 1000
    assert services.staged_events[0][1]["outcome"] == "rearmed"
    assert services.staged_events[0][1]["run_status"] == "failed"


def test_once_schedule_deletes_on_success_and_disables_on_failure(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            schedule_text="once",
            kind="once",
            expires_at=None,
            pending=_pending("sched-1", sequence=4),
        )
    )

    _trigger(services, tmp_path, _run_finished("completed", sequence=9))

    assert _stored(services).schedules == {}
    assert services.staged_wakeups == []
    assert services.staged_events == [
        (
            "schedule_fired",
            {
                "outcome": "deleted",
                "id": "sched-1",
                "wakeup_id": "wakeup-fired",
                "fired_sequence": 4,
                "run_status": "completed",
            },
        )
    ]


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_once_schedule_is_disabled_when_its_run_fails(services: FakeServices, tmp_path: Path, status: Literal["failed", "interrupted"]) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            schedule_text="at 2026-12-01T09:00:00",
            kind="at",
            expires_at=None,
            pending=_pending("sched-1", sequence=4),
        )
    )

    _trigger(services, tmp_path, _run_finished(status, sequence=9))

    entry = _entry(services, "sched-1")
    assert entry.enabled is False
    assert entry.wakeup_id is None
    assert entry.next_fire_at is None
    assert entry.pending is None
    assert services.staged_wakeups == []
    assert services.staged_events[0][1]["outcome"] == "disabled"
    assert services.staged_events[0][1]["reason"] == "run_failed"
    assert services.staged_events[0][1]["run_status"] == status


def test_expired_recurring_schedule_is_deleted_instead_of_rearmed(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            expires_at=NOW_MS,
            pending=_pending("sched-1", sequence=4),
        )
    )

    _trigger(services, tmp_path, _run_finished("completed", sequence=9))

    assert _stored(services).schedules == {}
    assert services.staged_wakeups == []
    assert services.staged_events[0][1]["outcome"] == "expired"
    assert services.staged_events[0][1]["reason"] == "expiry"


def test_next_fire_beyond_expiry_retires_instead_of_arming(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            schedule_text="every 10m",
            expires_at=NOW_MS + 5 * 60_000,
            pending=_pending("sched-1", sequence=4),
        )
    )

    _trigger(services, tmp_path, _run_finished("completed", sequence=9))

    assert _stored(services).schedules == {}
    assert services.staged_wakeups == []
    assert services.staged_events[0][1]["outcome"] == "expired"


def test_limit_stop_disables_a_recurring_schedule_without_rearming(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            schedule_text="every 10m",
            pending=_pending("sched-1", sequence=4),
        )
    )

    _trigger(
        services,
        tmp_path,
        _run_finished("failed", sequence=9, stop_cause="limit_reached", budget_exhaustion=_root_budget_exhausted()),
    )

    entry = _entry(services, "sched-1")
    assert entry.enabled is False
    assert entry.wakeup_id is None
    assert entry.next_fire_at is None
    assert entry.pending is None
    assert services.staged_wakeups == []
    event = services.staged_events[0][1]
    assert event["outcome"] == "disabled"
    assert event["reason"] == "limit"
    assert event["budget_exhaustion"]["source"] == "root_action_budget"


def test_untyped_limit_stop_disables_a_recurring_schedule(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", pending=_pending("sched-1", sequence=4)))

    _trigger(services, tmp_path, _run_finished("interrupted", sequence=9, stop_cause="limit_reached"))

    assert _entry(services, "sched-1").enabled is False
    assert services.staged_wakeups == []
    assert services.staged_events[0][1]["reason"] == "limit"


def test_duplicate_terminal_delivery_does_not_duplicate_settlement(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", pending=_pending("sched-1", sequence=7)))
    fact = _run_finished("completed", sequence=20)

    _trigger(services, tmp_path, fact)
    first_version = services.read_state("session")[1]
    first_wakeups = len(services.staged_wakeups)
    first_events = len(services.staged_events)
    _trigger(services, tmp_path, fact)

    assert len(services.staged_wakeups) == first_wakeups
    assert len(services.staged_events) == first_events
    assert services.read_state("session")[1] == first_version


def test_run_finished_without_pending_only_advances_the_finish_sequence(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1"))
    _trigger(services, tmp_path, _run_finished("completed", sequence=20))

    stored = _stored(services)
    assert stored.last_finished_sequence == 20
    assert stored.schedules["sched-1"].pending is None
    assert stored.schedules["sched-1"].next_fire_at == NOW_MS + 600_000
    assert services.staged_events == []
    assert services.staged_wakeups == []


def test_late_wakeup_replay_pauses_visibly_instead_of_sticking_enabled(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        _schedule_payload(
            schedule_id="sched-1",
            last_finished_sequence=50,
            wakeup_id="wakeup-fired",
            next_fire_at=NOW_MS + 600_000,
        )
    )

    _trigger(services, tmp_path, _fired_event(sequence=40, schedule_id="sched-1"))

    entry = _entry(services, "sched-1")
    assert entry.enabled is False
    assert entry.wakeup_id is None
    assert entry.next_fire_at is None
    assert entry.pending is None
    event = services.staged_events[0][1]
    assert event["outcome"] == "disabled"
    assert event["reason"] == "settlement_missed"
    assert event["fired_sequence"] == 40


def test_catch_up_pages_more_than_200_facts_without_a_cap(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)
    facts: list[object] = [_run_requested(sequence) for sequence in range(1, 251)]
    facts.append(_fired_event(sequence=251, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id)))
    facts.append(_run_requested(252))
    facts.append(_run_finished("completed", sequence=253))

    _trigger(services, tmp_path, *facts)

    stored = _stored(services)
    assert stored.facts_cursor == 253
    assert stored.schedules[schedule_id].pending is None
    assert stored.schedules[schedule_id].next_fire_at == NOW_MS + 600_000
    assert services.staged_wakeups[-1]["due_at_ms"] == NOW_MS + 600_000
    assert [event["outcome"] for event in (payload for name, payload in services.staged_events if name == "schedule_fired")] == ["rearmed"]


def test_wakeup_and_terminal_in_one_catch_up_page_settle_without_a_live_run(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)
    facts = (
        _fired_event(sequence=5, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id)),
        _run_finished("completed", sequence=6, run_id="run-resumed"),
    )

    _trigger(services, tmp_path, *facts)

    stored = _stored(services)
    assert stored.schedules[schedule_id].pending is None
    assert stored.schedules[schedule_id].next_fire_at == NOW_MS + 600_000
    assert stored.facts_cursor == 6
    assert [event["outcome"] for name, event in services.staged_events if name == "schedule_fired"] == ["rearmed"]


def test_run_requested_trigger_recovers_a_lost_terminal_settlement(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)
    fire = _fired_event(sequence=10, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id))
    finish = _run_finished("completed", sequence=20)
    start = _run_requested(sequence=21)

    _trigger(services, tmp_path, fire, finish, start, trigger=start)

    stored = _stored(services)
    assert stored.schedules[schedule_id].pending is None
    assert stored.schedules[schedule_id].next_fire_at == NOW_MS + 600_000
    assert stored.facts_cursor == 21
    assert [event["outcome"] for name, event in services.staged_events if name == "schedule_fired"] == ["rearmed"]


def test_catch_up_does_not_advance_the_cursor_when_a_later_page_fails(services: FakeServices, tmp_path: Path) -> None:
    _create(services, tmp_path, "every 10m")
    facts = [_run_requested(sequence) for sequence in range(1, 206)]
    services.facts = list(facts)
    real_read = services.read_facts
    calls = {"n": 0}

    def _flaky_read(*, after_sequence, limit):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second page failed")
        return real_read(after_sequence=after_sequence, limit=limit)

    services.read_facts = _flaky_read  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="second page failed"):
        cron_observer(facts[-1], _context(tmp_path), services)
    assert _stored(services).facts_cursor == 0

    cron_observer(facts[-1], _context(tmp_path), services)
    assert _stored(services).facts_cursor == 205


def test_stored_v2_state_without_a_cursor_recovers_a_pending_settlement(services: FakeServices, tmp_path: Path) -> None:
    payload = _schedule_payload(schedule_id="sched-1", pending=_pending("sched-1", sequence=7))
    payload.pop("facts_cursor")
    services.seed_state(payload)
    fire = _fired_event(sequence=7, schedule_id="sched-1", wakeup_id="wakeup-fired")
    finish = _run_finished("completed", sequence=20)

    _trigger(services, tmp_path, fire, finish)

    stored = _stored(services)
    assert stored.facts_cursor == 20
    assert stored.schedules["sched-1"].pending is None
    assert stored.schedules["sched-1"].next_fire_at == NOW_MS + 600_000
    assert [event["outcome"] for name, event in services.staged_events if name == "schedule_fired"] == ["rearmed"]


def test_failed_catch_up_keeps_the_cursor_and_the_next_trigger_recovers(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)
    fire = _fired_event(sequence=10, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id))
    finish = _run_finished("completed", sequence=20)
    services.facts = [fire, finish]
    real_replace = services.replace_state
    calls = {"n": 0}

    def _flaky_replace(state, expected_version, scope="session", owner=None):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("state CAS conflict")
        return real_replace(state, expected_version, scope=scope, owner=owner)

    services.replace_state = _flaky_replace  # type: ignore[method-assign]
    before = _stored(services)
    with pytest.raises(RuntimeError, match="CAS conflict"):
        cron_observer(finish, _context(tmp_path), services)

    assert _stored(services) == before
    assert _stored(services).facts_cursor == 0
    # The real runtime discards a failed observer's staged work; emulate that before retrying.
    services.staged_wakeups.clear()
    services.staged_events.clear()

    cron_observer(finish, _context(tmp_path), services)

    recovered = _stored(services)
    assert recovered.facts_cursor == 20
    assert recovered.schedules[schedule_id].pending is None
    assert recovered.schedules[schedule_id].next_fire_at == NOW_MS + 600_000
    assert len(services.staged_wakeups) == 1
    assert [event["outcome"] for name, event in services.staged_events if name == "schedule_fired"] == ["rearmed"]


def test_failed_catch_up_read_keeps_the_cursor_and_the_next_trigger_recovers(services: FakeServices, tmp_path: Path) -> None:
    schedule_id = _create(services, tmp_path, "every 10m")
    entry = _entry(services, schedule_id)
    fire = _fired_event(sequence=10, schedule_id=schedule_id, wakeup_id=str(entry.wakeup_id))
    finish = _run_finished("completed", sequence=20)
    services.facts = [fire, finish]
    real_read = services.read_facts
    calls = {"n": 0}

    def _flaky_read(*, after_sequence, limit):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("facts read failed")
        return real_read(after_sequence=after_sequence, limit=limit)

    services.read_facts = _flaky_read  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="facts read failed"):
        cron_observer(finish, _context(tmp_path), services)

    assert _stored(services).facts_cursor == 0
    cron_observer(finish, _context(tmp_path), services)

    recovered = _stored(services)
    assert recovered.facts_cursor == 20
    assert recovered.schedules[schedule_id].pending is None
    assert recovered.schedules[schedule_id].next_fire_at == NOW_MS + 600_000
    assert [event["outcome"] for name, event in services.staged_events if name == "schedule_fired"] == ["rearmed"]


def test_stored_v2_state_without_a_cursor_starts_catch_up_from_zero() -> None:
    payload = _schedule_payload(schedule_id="sched-1")
    payload.pop("facts_cursor")

    parsed = CronState.from_payload(payload)

    assert parsed.facts_cursor == 0
    assert parsed.to_payload()["facts_cursor"] == 0


def test_observer_ignores_facts_it_does_not_settle(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1"))

    _trigger(
        services,
        tmp_path,
        InvocationFinishedFact(
            session_id=SESSION_ID,
            run_id="run-9",
            revision_id="rev-1",
            logical_turn="turn-1",
            event_sequence=3,
            tool_name="cron_create",
            tool_call_id="call-1",
            status="ok",
            owner=OWNER,
            effects=("session",),
        ),
    )

    assert services.staged_wakeups == []
    assert services.staged_events == []
    assert _stored(services).schedules


def test_cron_identity_follows_its_admitted_owner(services: FakeServices, tmp_path: Path) -> None:
    services.owner = "renamed-scheduler"
    services.seed_state(_schedule_payload(schedule_id="sched-1"))
    _trigger(
        services,
        tmp_path,
        replace(_fired_event(sequence=5, schedule_id="sched-1"), owner=services.owner),
    )
    assert _entry(services, "sched-1").pending is not None


# --- /loop ---


def _invoke_loop(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]):
    command = CronLoopCommand()
    invocation = CommandInvocation(context=_context(tmp_path), name=command.definition.name, arguments=arguments)
    return command.invoke(invocation, services)


@pytest.mark.parametrize(
    ("raw", "interval", "prompt"),
    [
        ("5m check the deploy", "5m", "check the deploy"),
        ("30m /standup 1", "30m", "/standup 1"),
        ("check the deploy", "10m", "check the deploy"),
        ("check the deploy every 20m", "20m", "check the deploy"),
        ("2 hours run the audit", "10m", "2 hours run the audit"),
    ],
)
def test_loop_parses_the_argument_and_stages_an_immediate_wakeup(
    services: FakeServices, tmp_path: Path, raw: str, interval: str, prompt: str
) -> None:
    result = _invoke_loop(services, tmp_path, {"input": raw})

    assert result.status == "ok"
    schedule_id = str(result.data["schedule_id"])  # type: ignore[index]
    entry = _entry(services, schedule_id)
    assert entry.schedule_text == f"every {interval}"
    assert entry.kind == "every"
    assert entry.prompt == prompt
    assert entry.expires_at == DEFAULT_EXPIRY
    # Fired immediately: the wakeup is due now instead of one interval later.
    assert entry.wakeup_id is not None
    assert entry.next_fire_at == NOW_MS
    assert services.staged_wakeups[-1]["due_at_ms"] == NOW_MS
    assert result.data["expires_at"] == DEFAULT_EXPIRY  # type: ignore[index]
    assert result.output == (
        f"Scheduled '{prompt}' on cadence 'every {interval}' "
        f"(job ID: {schedule_id}). "
        "Recurring tasks auto-expire after 7 days. "
        f"To cancel sooner, use /loop delete {schedule_id}. "
        "A run already in flight is not interrupted."
    )
    assert result.output == loop_confirmation_message(
        type("Summary", (), {"id": schedule_id, "prompt": prompt, "human_schedule": f"every {interval}"})()
    )
    assert [name for name, _payload in services.staged_events] == ["schedule_created"]


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"input": "30s too fast"}, "at least 1 minute"),
        ({"input": "8d too slow"}, "shorter than the 7-day recurring expiry"),
    ],
)
def test_loop_enforces_the_recurring_policy(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object], fragment: str) -> None:
    result = _invoke_loop(services, tmp_path, arguments)

    assert result.status == "error"
    assert fragment in str(result.error)
    assert services.staged_wakeups == []
    assert services.staged_events == []


@pytest.mark.parametrize("arguments", [{}, {"input": "   "}, {"input": "5m"}])
def test_loop_returns_the_usage_message_when_it_cannot_parse(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]) -> None:
    result = _invoke_loop(services, tmp_path, arguments)

    assert result.status == "ok"
    assert result.output == USAGE_MESSAGE
    assert services.staged_wakeups == []
    assert services.staged_events == []


def test_loop_preparation_does_not_schedule_and_shows_default_interval(services: FakeServices, tmp_path: Path) -> None:
    from voidcode.sdk.views import FormViewData, validate_form_submission

    before = services.get_state()
    prepared = CronLoopCommand().prepare_input(CommandInvocation(_context(tmp_path), "loop"), services)
    form = FormViewData.from_payload(prepared.data)
    assert DEFAULT_INTERVAL in form.fields[0].label
    assert "minimum 1m" in form.fields[0].label
    assert services.get_state() == before
    assert validate_form_submission(form, {"input": "5m check the deploy"}) == {"input": "5m check the deploy"}


def test_loop_receipt_can_cancel_its_schedule_and_return_empty_browser(services: FakeServices, tmp_path: Path) -> None:
    from voidcode.plugins.cron.cron_plugin import CronDeleteCommand
    from voidcode.sdk.views import MarkdownViewData

    created = _invoke_loop(services, tmp_path, {"input": "5m check deployment"})
    detail = MarkdownViewData.from_payload(created.presentation.data)
    cancel = next(action for action in detail.actions if action.action == "loop.delete")
    wakeup_id = _entry(services, str(created.data["schedule_id"])).wakeup_id  # type: ignore[index]
    result = CronDeleteCommand().invoke(CommandInvocation(_context(tmp_path), cancel.action, cancel.arguments), services)
    assert result.status == "ok"
    assert DELETE_IN_FLIGHT_NOTE in result.output
    assert services.staged_wakeup_cancels == [wakeup_id]
    assert _stored(services).schedules == {}
    empty = MarkdownViewData.from_payload(result.presentation.data)
    assert "No scheduled prompts" in empty.content
    assert empty.actions[0].prepare_input
    assert CronDeleteCommand().invoke(CommandInvocation(_context(tmp_path), cancel.action, cancel.arguments), services).status == "error"


def test_loop_browser_pages_without_mutation(services: FakeServices, tmp_path: Path) -> None:
    from voidcode.plugins.cron.cron_plugin import CronListCommand
    from voidcode.sdk.views import MarkdownViewData

    for index in range(12):
        _invoke_loop(services, tmp_path, {"input": f"5m check {index}"})
    before = services.get_state("session")
    listed = CronListCommand().invoke(CommandInvocation(_context(tmp_path), "loop.list"), services)
    view = MarkdownViewData.from_payload(listed.presentation.data)
    assert len([action for action in view.actions if action.action == "loop.delete"]) == 10
    next_page = next(action for action in view.actions if action.label == "Next schedules")
    page = CronListCommand().invoke(CommandInvocation(_context(tmp_path), next_page.action, next_page.arguments), services)
    assert len([action for action in MarkdownViewData.from_payload(page.presentation.data).actions if action.action == "loop.delete"]) == 2
    assert services.get_state("session") == before
    assert len(services.staged_wakeups) == 12


def test_loop_and_tool_share_schedule_capacity_guard(services: FakeServices, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron_plugin, "MAX_SCHEDULES_PER_SESSION", 1)
    assert _invoke_loop(services, tmp_path, {"input": "5m first"}).status == "ok"
    before = services.get_state("session")
    staged = len(services.staged_wakeups)
    refused = _invoke_loop(services, tmp_path, {"input": "5m second"})
    assert refused.status == "error" and "schedule limit reached" in refused.error
    refused_tool = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": "every 5m", "prompt": "third"})
    assert isinstance(refused_tool, ToolFailure)
    assert refused_tool.error == refused.error
    assert services.get_state("session") == before
    assert len(services.staged_wakeups) == staged


# --- staged write behavior and state errors ---


class _CASFakeServices(FakeServices):
    """A store whose state commit always loses its optimistic-concurrency check."""

    def replace_state(self, state, expected_version, scope="session", owner=None):  # type: ignore[override]
        raise RuntimeError("state CAS mismatch")


def test_state_cas_loss_after_create_stages_no_wakeup_row(tmp_path: Path) -> None:
    services = _CASFakeServices(owner=OWNER)
    with pytest.raises(RuntimeError, match="CAS mismatch"):
        _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": "every 10m", "prompt": "x"})

    assert services.wakeups == {}
    assert services.staged_events == []


def test_state_cas_loss_after_delete_keeps_the_existing_wakeup(tmp_path: Path) -> None:
    services = _CASFakeServices(owner=OWNER)
    schedule_id = "sched-1"
    services.seed_state(_schedule_payload(schedule_id=schedule_id, wakeup_id="wakeup-fired"))
    services.wakeups["wakeup-fired"] = {"status": "pending", "payload": {}}
    with pytest.raises(RuntimeError, match="CAS mismatch"):
        _invoke_tool(CronDeleteTool(), services, tmp_path, {"id": schedule_id})

    assert services.wakeups["wakeup-fired"]["status"] == "pending"
    assert services.staged_wakeup_cancels == []


def test_observer_rearm_is_staged_not_written_durably(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", pending=_pending("sched-1", sequence=7)))
    _trigger(services, tmp_path, _run_finished("completed", sequence=9))

    assert len(services.staged_wakeups) == 1
    assert services.wakeups == {}
    assert _entry(services, "sched-1").wakeup_id == "staged-wakeup-0"


def test_malformed_state_observer_stages_visible_error_without_raising(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(
        {
            "schema_version": SCHEMA_VERSION,
            "schedules": {
                "sched-1": {
                    "schedule_text": "every 10m",
                    "kind": "cron",
                    "prompt": "x",
                    "enabled": True,
                    "expires_at": DEFAULT_EXPIRY,
                    "wakeup_id": "w",
                    "next_fire_at": NOW_MS + 600_000,
                    "pending": None,
                }
            },
            "last_finished_sequence": 0,
        }
    )

    _trigger(services, tmp_path)

    assert [name for name, _payload in services.staged_events] == ["schedule_error"]
    error = services.staged_events[0][1]["error"]
    assert "kind" in str(error)
    assert len(str(error)) <= 500


def test_malformed_state_tools_fail_visibly(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state({"schema_version": SCHEMA_VERSION, "schedules": {}, "last_finished_sequence": 0} | {"schedules": {"sched-1": {"bad": True}}})

    listed = _invoke_tool(CronListTool(), services, tmp_path, {})
    assert isinstance(listed, ToolFailure)
    assert "Cron state is invalid" in listed.error
    created = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": "every 10m", "prompt": "x"})
    assert isinstance(created, ToolFailure)
    assert "Cron state is invalid" in created.error


# --- state document, registration ---


def test_state_document_is_strict() -> None:
    pending = _pending("sched-1")
    payload = CronState(
        schedules={"sched-1": ScheduleEntry("every 10m", "every", "keep building", True, DEFAULT_EXPIRY, pending.wakeup_id, None, pending)},
        last_finished_sequence=0,
    ).to_payload()
    assert payload["schema_version"] == SCHEMA_VERSION
    parsed = CronState.from_payload(payload)
    assert parsed.schedules["sched-1"].pending == pending

    with pytest.raises(ValueError, match="field 'bogus' is not supported"):
        cron_plugin._store(_seed({"bogus": 1})).load()
    with pytest.raises(ValueError, match="missing field"):
        cron_plugin._store(_seed({"schema_version": SCHEMA_VERSION})).load()
    with pytest.raises(ValueError, match=f"schema_version must be {SCHEMA_VERSION}"):
        CronState.from_payload({"schema_version": 1, "schedules": {}, "last_finished_sequence": 0})
    with pytest.raises(ValueError, match="last_finished_sequence must be a non-negative integer"):
        CronState.from_payload({"schema_version": SCHEMA_VERSION, "schedules": {}, "last_finished_sequence": -1})
    with pytest.raises(ValueError, match="schedules must be an object"):
        CronState.from_payload({"schema_version": SCHEMA_VERSION, "schedules": [], "last_finished_sequence": 0})
    with pytest.raises(ValueError, match="field 'bogus' is not supported"):
        CronState.from_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "schedules": {"sched-1": {**payload["schedules"]["sched-1"], "bogus": 1}},  # type: ignore[index]
                "last_finished_sequence": 0,
            }
        )
    with pytest.raises(ValueError, match="kind must be one of"):
        CronState.from_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "schedules": {"sched-1": {**payload["schedules"]["sched-1"], "kind": "hourly"}},  # type: ignore[index]
                "last_finished_sequence": 0,
            }
        )


def test_state_rejects_inconsistent_schedule_and_pending_fields() -> None:
    pending = _pending("sched-1")
    valid = _schedule_payload(schedule_id="sched-1", pending=pending)

    def _entry(payload: Mapping[str, object]) -> Mapping[str, object]:
        schedules = payload["schedules"]
        assert isinstance(schedules, Mapping)
        entry = schedules["sched-1"]
        assert isinstance(entry, Mapping)
        return entry

    base_entry = _entry(valid)
    with pytest.raises(ValueError, match="does not match schedule_text"):
        CronState.from_payload(
            {
                **valid,
                "schedules": {"sched-1": {**base_entry, "kind": "cron"}},
            }
        )
    with pytest.raises(ValueError, match="pending settlement must clear next_fire_at"):
        CronState.from_payload(
            {
                **valid,
                "schedules": {"sched-1": {**base_entry, "next_fire_at": NOW_MS + 600_000}},
            }
        )
    with pytest.raises(ValueError, match="pending wakeup does not match its armed wakeup"):
        CronState.from_payload(
            {
                **valid,
                "schedules": {"sched-1": {**base_entry, "wakeup_id": "other"}},
            }
        )
    with pytest.raises(ValueError, match="older than the last finished run"):
        CronState.from_payload({**valid, "last_finished_sequence": pending.fired_sequence})
    with pytest.raises(ValueError, match="one-shot schedules cannot expire"):
        CronState.from_payload(_schedule_payload(schedule_id="sched-1", schedule_text="once", kind="once", expires_at=DEFAULT_EXPIRY))
    with pytest.raises(ValueError, match="recurring schedules require expires_at"):
        CronState.from_payload(_schedule_payload(schedule_id="sched-1", expires_at=None))
    with pytest.raises(ValueError, match="at least 1 minute"):
        CronState.from_payload(_schedule_payload(schedule_id="sched-1", schedule_text="every 1s"))
    with pytest.raises(ValueError, match="at or after the 7-day recurring expiry"):
        CronState.from_payload(_schedule_payload(schedule_id="sched-1", expires_at=NOW_MS + 5 * 60_000, next_fire_at=NOW_MS + 10 * 60_000))
    disabled_with_wakeup = _schedule_payload(schedule_id="sched-1")
    disabled_with_wakeup["schedules"]["sched-1"]["enabled"] = False  # type: ignore[index]
    with pytest.raises(ValueError, match="disabled schedules cannot keep a wakeup"):
        CronState.from_payload(disabled_with_wakeup)
    disabled_pending = _schedule_payload(schedule_id="sched-1", pending=pending)
    disabled_pending["schedules"]["sched-1"]["enabled"] = False  # type: ignore[index]
    with pytest.raises(ValueError, match="cannot be disabled with a pending settlement"):
        CronState.from_payload(disabled_pending)


def test_plugin_registers_tools_commands_and_catch_up_fact_subscriptions() -> None:
    plugin = create_plugin(None)

    assert [tool.definition.name for tool in plugin.tools] == ["cron_create", "cron_delete", "cron_list"]
    assert [command.definition.name for command in plugin.commands] == ["loop", "loop.list", "loop.delete"]
    assert plugin.observer is cron_observer
    assert plugin.observed_facts == ("run_requested", "wakeup_fired", "run_finished")
    assert plugin.context is None
    assert plugin.providers == ()
    assert plugin.views == ()


def test_cron_factory_and_state_reject_coercion() -> None:
    assert create_plugin({"providers": {}}).commands
    with pytest.raises(ValueError, match="cron config does not support field: enabled"):
        create_plugin({"enabled": True})
    for version in (True, 1.0, "1"):
        with pytest.raises(ValueError, match="schema_version"):
            CronState.from_payload({"schema_version": version, "schedules": {}, "last_finished_sequence": 0})
    payload = _schedule_payload(schedule_id="bad")
    payload["schedules"]["bad"]["kind"] = []  # type: ignore[index]
    with pytest.raises(ValueError, match="kind must be one of"):
        CronState.from_payload(payload)


def test_loop_default_interval_is_documented_minimum_compatible() -> None:
    assert int(DEFAULT_INTERVAL.rstrip("m")) >= MIN_EVERY_INTERVAL_SECONDS // 60
