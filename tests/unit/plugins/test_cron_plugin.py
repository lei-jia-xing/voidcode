"""Unit tests for the cron plugin: state, tools, /loop command, and terminal rescheduling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.cron import cron_plugin
from voidcode.plugins.cron.cron_plugin import (
    DEFAULT_RECURRING_EXPIRY_SECONDS,
    MAX_SCHEDULES_PER_WORKSPACE,
    SCHEMA_VERSION,
    CronCreateTool,
    CronDeleteTool,
    CronListTool,
    CronLoopCommand,
    CronState,
    ScheduleEntry,
    create_plugin,
    cron_observer,
)
from voidcode.plugins.cron.loop_command import USAGE_MESSAGE, loop_confirmation_message
from voidcode.plugins.cron.schedule_expr import next_fire_after, parse_schedule
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.hooks import FactPage, InvocationFinishedFact, RunFinishedFact, WakeupFiredFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import ToolCall, ToolFailure, ToolSuccess

OWNER = "cron"
SESSION_ID = "session-1"
#: Pinned wall clock: the whole suite computes fire instants against this instant.
NOW_MS = 1_700_000_000_000
NOW_SECONDS = NOW_MS // 1000


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron_plugin, "_clock_ms", lambda: NOW_MS)


@pytest.fixture
def services() -> FakeServices:
    return FakeServices(owner=OWNER, schema_version=SCHEMA_VERSION)


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
    assert isinstance(result, ToolSuccess)
    return str(json.loads(result.output.text)["id"])


def _stored(services: FakeServices) -> CronState:
    payload, _version = services.get_state("session")
    return CronState.from_payload(payload)


def _entry(services: FakeServices, schedule_id: str) -> ScheduleEntry:
    return _stored(services).schedules[schedule_id]


def _fired_event(*, sequence: int, run_id: str, schedule_id: str, prompt: str = "keep building", wakeup_id: str = "wakeup-fired") -> WakeupFiredFact:
    return WakeupFiredFact(
        session_id=SESSION_ID,
        event_sequence=sequence,
        run_id=run_id,
        revision_id="rev-1",
        logical_turn="turn-1",
        owner=OWNER,
        wakeup_id=wakeup_id,
        role="user",
        payload={"kind": "cron", "text": prompt, "schedule_id": schedule_id},
    )


def _run_finished(status: Literal["completed", "failed", "interrupted"], *, run_id: str) -> RunFinishedFact:
    return RunFinishedFact(
        session_id=SESSION_ID,
        run_id=run_id,
        revision_id="rev-1",
        logical_turn="turn-1",
        event_sequence=9,
        status=status,
        stop_cause="completed" if status == "completed" else "error",
    )


def _schedule_payload(
    *,
    schedule_id: str,
    schedule_text: str,
    kind: str,
    expires_at: int | None,
    enabled: bool = True,
    wakeup_id: str | None = "wakeup-fired",
) -> dict[str, object]:
    return CronState(
        schedules={
            schedule_id: ScheduleEntry(
                schedule_text=schedule_text,
                kind=kind,  # type: ignore[arg-type]
                prompt="keep building",
                enabled=enabled,
                expires_at=expires_at,
                wakeup_id=wakeup_id,
            )
        }
    ).to_payload()


# --- create ---


@pytest.mark.parametrize(
    ("schedule", "prompt"),
    [
        ("every 10m", "check the deploy"),
        ("every 1s", "tick"),
        ("once", "run now"),
        ("at 2026-12-01T09:00:00", "wake up"),
        ("cron */5 * * * *", "poll"),
    ],
)
def test_create_arms_the_first_fire_computed_by_the_expression(services: FakeServices, tmp_path: Path, schedule: str, prompt: str) -> None:
    schedule_id = _create(services, tmp_path, schedule, prompt)
    expr = parse_schedule(schedule)
    expected_fire_ms = next_fire_after(expr, NOW_SECONDS) * 1000

    entry = _entry(services, schedule_id)
    assert entry.schedule_text == schedule
    assert entry.kind == expr.kind
    assert entry.prompt == prompt
    assert entry.enabled is True

    assert services.pending() == {
        entry.wakeup_id: {
            "payload": {"kind": "cron", "text": prompt, "schedule_id": schedule_id},
            "due_at_ms": expected_fire_ms,
            "role": "user",
            "idempotency_key": f"cron:{schedule_id}:{expected_fire_ms}",
            "status": "pending",
        }
    }
    assert [name for name, _payload in services.staged_events] == ["schedule_created"]
    event = services.staged_events[0][1]
    assert event["id"] == schedule_id
    assert event["kind"] == expr.kind
    assert event["next_fire_at"] == expected_fire_ms
    assert event["wakeup_id"] == entry.wakeup_id
    assert _stored(services).cursor_sequence == 0


def test_default_expiry_applies_to_recurring_schedules_only(services: FakeServices, tmp_path: Path) -> None:
    for schedule, expected in (
        ("every 10m", NOW_MS + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000),
        ("cron 0 9 * * *", NOW_MS + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000),
        ("once", None),
        ("at 2026-12-01T09:00:00", None),
    ):
        schedule_id = _create(services, tmp_path, schedule)
        assert _entry(services, schedule_id).expires_at == expected, schedule


@pytest.mark.parametrize("schedule", ["banana", "every 0s", "every 10 fortnights", "cron * * *", "at not-a-date", "cron 0 9 * * L"])
def test_invalid_schedule_is_rejected_without_side_effects(services: FakeServices, tmp_path: Path, schedule: str) -> None:
    services.seed_state(CronState().to_payload(), version=3)
    result = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": schedule, "prompt": "x"})

    assert isinstance(result, ToolFailure)
    assert result.error
    assert services.wakeups == {}
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
    assert services.staged_events == []


def test_schedule_cap_is_enforced_per_session(services: FakeServices, tmp_path: Path) -> None:
    schedules = {
        f"sched-{index}": ScheduleEntry(
            schedule_text="every 10m",
            kind="every",
            prompt="x",
            enabled=True,
            expires_at=NOW_MS + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000,
            wakeup_id=f"wakeup-{index}",
        )
        for index in range(MAX_SCHEDULES_PER_WORKSPACE)
    }
    services.seed_state(CronState(schedules=schedules).to_payload())

    result = _invoke_tool(CronCreateTool(), services, tmp_path, {"schedule": "every 10m", "prompt": "x"})
    assert isinstance(result, ToolFailure)
    assert f"maximum {MAX_SCHEDULES_PER_WORKSPACE} jobs" in result.error
    assert services.wakeups == {}
    assert len(_stored(services).schedules) == MAX_SCHEDULES_PER_WORKSPACE


# --- delete and list ---


def test_delete_cancels_the_armed_wakeup_and_removes_the_schedule(services: FakeServices, tmp_path: Path) -> None:
    kept = _create(services, tmp_path, "every 10m", "keep me")
    removed = _create(services, tmp_path, "every 1h", "drop me")
    armed = _entry(services, removed).wakeup_id
    assert armed is not None and armed in services.pending()

    result = _invoke_tool(CronDeleteTool(), services, tmp_path, {"id": removed})

    assert isinstance(result, ToolSuccess)
    assert json.loads(result.output.text) == {"id": removed}
    assert services.wakeups[armed]["status"] == "cancelled"
    assert set(_stored(services).schedules) == {kept}


def test_delete_rejects_unknown_and_missing_ids(services: FakeServices, tmp_path: Path) -> None:
    assert isinstance(_invoke_tool(CronDeleteTool(), services, tmp_path, {"id": " "}), ToolFailure)
    unknown = _invoke_tool(CronDeleteTool(), services, tmp_path, {"id": "sched-nope"})
    assert isinstance(unknown, ToolFailure)
    assert unknown.error == "Schedule 'sched-nope' not found."


def test_list_reports_every_schedule(services: FakeServices, tmp_path: Path) -> None:
    every = _create(services, tmp_path, "every 10m", "poll")
    once = _create(services, tmp_path, "once", "now please")
    disabled = _create(services, tmp_path, "every 30m", "later")
    state = _stored(services)
    services.seed_state(
        replace(
            state,
            schedules={
                **state.schedules,
                disabled: replace(state.schedules[disabled], enabled=False, wakeup_id=None),
            },
        ).to_payload()
    )

    result = _invoke_tool(CronListTool(), services, tmp_path, {})
    assert isinstance(result, ToolSuccess)
    jobs = json.loads(result.output.text)["jobs"]
    assert jobs == [
        {
            "id": every,
            "schedule_text": "every 10m",
            "human_schedule": "every 10m",
            "next_fire_at": next_fire_after(parse_schedule("every 10m"), NOW_SECONDS),
            "enabled": True,
            "expires_at": NOW_MS + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000,
        },
        {
            "id": once,
            "schedule_text": "once",
            "human_schedule": "once",
            "next_fire_at": NOW_SECONDS,
            "enabled": True,
            "expires_at": None,
        },
        {
            "id": disabled,
            "schedule_text": "every 30m",
            "human_schedule": "every 30m",
            "next_fire_at": None,
            "enabled": False,
            "expires_at": NOW_MS + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000,
        },
    ]


# --- observation ---


def test_observer_rearms_a_recurring_schedule_after_a_fired_completed_run(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS + 86_400_000))
    services.events.append(_fired_event(sequence=7, run_id="run-9", schedule_id="sched-1"))

    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)

    expected_fire_ms = next_fire_after(parse_schedule("every 10m"), NOW_SECONDS) * 1000
    entry = _entry(services, "sched-1")
    assert entry.enabled is True
    assert entry.wakeup_id is not None
    assert services.pending()[entry.wakeup_id]["due_at_ms"] == expected_fire_ms
    assert services.pending()[entry.wakeup_id]["payload"] == {"kind": "cron", "text": "keep building", "schedule_id": "sched-1"}
    assert services.pending()[entry.wakeup_id]["role"] == "user"
    assert services.pending()[entry.wakeup_id]["idempotency_key"] == f"cron:sched-1:{expected_fire_ms}"
    assert services.staged_events == [
        ("schedule_fired", {"id": "sched-1", "outcome": "rearmed", "run_status": "completed", "next_fire_at": expected_fire_ms})
    ]
    assert _stored(services).cursor_sequence == 7


def test_observer_rearms_recurring_schedule_after_a_failed_run_too(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="cron 0 9 * * *", kind="cron", expires_at=NOW_MS + 86_400_000))
    services.events.append(_fired_event(sequence=3, run_id="run-9", schedule_id="sched-1"))

    cron_observer(_run_finished("failed", run_id="run-9"), _context(tmp_path), services)

    entry = _entry(services, "sched-1")
    assert entry.enabled is True
    assert entry.wakeup_id is not None
    assert services.pending()[entry.wakeup_id]["due_at_ms"] == next_fire_after(parse_schedule("cron 0 9 * * *"), NOW_SECONDS) * 1000
    assert services.staged_events[0][1]["outcome"] == "rearmed"
    assert services.staged_events[0][1]["run_status"] == "failed"


def test_once_schedule_deletes_on_success_and_disables_on_failure(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="once", kind="once", expires_at=None))
    services.events.append(_fired_event(sequence=4, run_id="run-9", schedule_id="sched-1"))

    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)

    assert _stored(services).schedules == {}
    assert services.pending() == {}
    assert services.staged_events == [("schedule_fired", {"id": "sched-1", "outcome": "deleted", "run_status": "completed"})]


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_once_schedule_is_disabled_when_its_run_fails(services: FakeServices, tmp_path: Path, status: Literal["failed", "interrupted"]) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="at 2026-12-01T09:00:00", kind="at", expires_at=None))
    services.events.append(_fired_event(sequence=4, run_id="run-9", schedule_id="sched-1"))

    cron_observer(_run_finished(status, run_id="run-9"), _context(tmp_path), services)

    entry = _entry(services, "sched-1")
    assert entry.enabled is False
    assert entry.wakeup_id is None
    assert services.pending() == {}
    assert services.staged_events == [("schedule_fired", {"id": "sched-1", "outcome": "disabled", "run_status": status})]


def test_expired_recurring_schedule_is_deleted_instead_of_rearmed(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS))
    services.events.append(_fired_event(sequence=4, run_id="run-9", schedule_id="sched-1"))

    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)

    assert _stored(services).schedules == {}
    assert services.wakeups == {}
    assert services.staged_events == [("schedule_fired", {"id": "sched-1", "outcome": "expired", "run_status": "completed"})]


def test_untracked_or_foreign_firings_are_ignored(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS + 86_400_000))
    # A wakeup this plugin does not track, a foreign wakeup kind, and a wakeup of another run.
    services.events.append(_fired_event(sequence=5, run_id="run-9", schedule_id="sched-unknown"))
    services.events.append(replace(_fired_event(sequence=6, run_id="run-9", schedule_id="sched-1"), payload={"kind": "goal", "goal_id": "g-1"}))
    services.events.append(_fired_event(sequence=7, run_id="run-other", schedule_id="sched-1"))

    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)

    assert services.wakeups == {}
    assert services.staged_events == []
    entry = _entry(services, "sched-1")
    assert entry.wakeup_id == "wakeup-fired"
    # The cursor still advances past what was read, so nothing is re-examined forever.
    assert _stored(services).cursor_sequence == 7


def test_disabled_schedule_is_never_rearmed(services: FakeServices, tmp_path: Path) -> None:
    payload = _schedule_payload(schedule_id="sched-1", schedule_text="once", kind="once", expires_at=None, enabled=False, wakeup_id=None)
    services.seed_state(payload)
    services.events.append(_fired_event(sequence=4, run_id="run-9", schedule_id="sched-1"))

    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)

    assert services.wakeups == {}
    assert services.staged_events == []
    assert _stored(services).schedules["sched-1"].enabled is False


def test_observer_ignores_facts_it_does_not_settle(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS + 86_400_000))

    cron_observer(
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
        _context(tmp_path),
        services,
    )

    assert services.wakeups == {}
    assert services.staged_events == []


def test_observer_is_idempotent_for_the_same_firing(services: FakeServices, tmp_path: Path) -> None:
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS + 86_400_000))
    services.events.append(_fired_event(sequence=4, run_id="run-9", schedule_id="sched-1"))

    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)
    first_wakeup_id = _entry(services, "sched-1").wakeup_id
    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)

    assert set(services.wakeups) == {first_wakeup_id}
    assert len(services.staged_events) == 1


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
def test_loop_parses_the_argument_and_arms_an_immediate_wakeup(services: FakeServices, tmp_path: Path, raw: str, interval: str, prompt: str) -> None:
    result = _invoke_loop(services, tmp_path, {"input": raw})

    assert result.status == "ok"
    schedule_id = str(result.data["schedule_id"])  # type: ignore[index]
    entry = _entry(services, schedule_id)
    assert entry.schedule_text == f"every {interval}"
    assert entry.kind == "every"
    assert entry.prompt == prompt
    assert entry.expires_at == NOW_MS + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000
    # Fired immediately: the wakeup is due now instead of one interval later.
    assert entry.wakeup_id is not None
    assert services.pending()[entry.wakeup_id]["due_at_ms"] == NOW_MS
    assert result.output == (
        f"Scheduled '{prompt}' on cadence 'every {interval}' "
        f"(job ID: {schedule_id}). "
        "Recurring tasks auto-expire after 7 days. "
        "To cancel sooner, use `cron_delete`."
    )
    assert result.output == loop_confirmation_message(
        type("Summary", (), {"id": schedule_id, "prompt": prompt, "human_schedule": f"every {interval}"})()
    )
    assert [name for name, _payload in services.staged_events] == ["schedule_created"]


@pytest.mark.parametrize("arguments", [{}, {"input": "   "}, {"input": "5m"}])
def test_loop_returns_the_usage_message_when_it_cannot_parse(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]) -> None:
    result = _invoke_loop(services, tmp_path, arguments)

    assert result.status == "ok"
    assert result.output == USAGE_MESSAGE
    assert services.wakeups == {}
    assert services.staged_events == []


# --- state document, registration ---


def test_state_document_is_strict() -> None:
    payload = CronState(schedules={"sched-1": ScheduleEntry("every 10m", "every", "x", True, None, "w-1")}).to_payload()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert CronState.from_payload(payload) == CronState(schedules={"sched-1": ScheduleEntry("every 10m", "every", "x", True, None, "w-1")})

    with pytest.raises(ValueError, match="field 'bogus' is not supported"):
        CronState.from_payload({**payload, "bogus": 1})
    with pytest.raises(ValueError, match="missing field"):
        CronState.from_payload({"schema_version": SCHEMA_VERSION})
    with pytest.raises(ValueError, match="schema_version must be 1"):
        CronState.from_payload({"schema_version": 2, "schedules": {}, "cursor_sequence": 0})
    with pytest.raises(ValueError, match="cursor_sequence must be a non-negative integer"):
        CronState.from_payload({"schema_version": 1, "schedules": {}, "cursor_sequence": -1})
    with pytest.raises(ValueError, match="schedules must be an object"):
        CronState.from_payload({"schema_version": 1, "schedules": [], "cursor_sequence": 0})
    with pytest.raises(ValueError, match="field 'bogus' is not supported"):
        CronState.from_payload(
            {
                "schema_version": 1,
                "schedules": {"sched-1": {**payload["schedules"]["sched-1"], "bogus": 1}},  # type: ignore[index]
                "cursor_sequence": 0,
            }
        )
    with pytest.raises(ValueError, match="kind must be one of"):
        CronState.from_payload(
            {
                "schema_version": 1,
                "schedules": {"sched-1": {**payload["schedules"]["sched-1"], "kind": "hourly"}},  # type: ignore[index]
                "cursor_sequence": 0,
            }
        )


def test_plugin_registers_tools_loop_command_and_observer() -> None:
    plugin = create_plugin(None)

    assert [tool.definition.name for tool in plugin.tools] == ["cron_create", "cron_delete", "cron_list"]
    assert [command.definition.name for command in plugin.commands] == ["loop"]
    assert plugin.observer is cron_observer
    assert plugin.context is None
    assert plugin.providers == ()
    assert plugin.views == ()


@pytest.mark.parametrize("foreign", ["owner", "identity"])
def test_matching_payload_cannot_settle_a_foreign_or_stale_wakeup(services: FakeServices, tmp_path: Path, foreign):
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS + 86_400_000))
    fact = _fired_event(sequence=5, run_id="run-9", schedule_id="sched-1")
    services.events.append(replace(fact, **({"owner": "other"} if foreign == "owner" else {"wakeup_id": "stale"})))
    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)
    assert services.wakeups == {} and services.staged_events == []
    assert _stored(services).cursor_sequence == 5
    assert _entry(services, "sched-1").wakeup_id == "wakeup-fired"


def test_cron_scan_advances_past_empty_fact_pages(services: FakeServices, tmp_path: Path):
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS + 86_400_000))
    pages = iter((FactPage((), 200, True), FactPage((_fired_event(sequence=201, run_id="run-9", schedule_id="sched-1"),), 201, False)))
    cursors = []

    def read_facts(*, after_sequence, limit):
        cursors.append(after_sequence)
        return next(pages)

    services.read_facts = read_facts
    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)
    assert cursors == [0, 200]
    assert _stored(services).cursor_sequence == 201
    assert len(services.wakeups) == 1


def test_cron_identity_follows_its_admitted_owner(services: FakeServices, tmp_path: Path):
    services.owner = "renamed-scheduler"
    services.seed_state(_schedule_payload(schedule_id="sched-1", schedule_text="every 10m", kind="every", expires_at=NOW_MS + 86_400_000))
    services.events.append(replace(_fired_event(sequence=5, run_id="run-9", schedule_id="sched-1"), owner=services.owner))
    cron_observer(_run_finished("completed", run_id="run-9"), _context(tmp_path), services)
    assert len(services.wakeups) == 1
