from __future__ import annotations

import json
from pathlib import Path

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.background.models import ScheduleSummary
from voidcode.tools.contracts import (
    TextOutput,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolSuccess,
)
from voidcode.tools.cron import (
    MAX_SCHEDULES_PER_WORKSPACE,
    CronCreateTool,
    CronDeleteTool,
    CronListTool,
)


class _MockCronRuntime:
    def __init__(self) -> None:
        self.schedules: dict[str, ScheduleSummary] = {}
        self._next_id = 1

    def create_schedule(
        self,
        *,
        schedule: str,
        prompt: str,
        parent_session_id: str | None = None,
        enabled: bool = True,
        expires_at: int | None = None,
        schedule_id: str | None = None,
    ) -> ScheduleSummary:
        from voidcode.runtime.background.schedule_expr import human_schedule, parse_schedule

        expr = parse_schedule(schedule)
        sched_id = schedule_id or f"sched-{self._next_id}"
        self._next_id += 1
        summary = ScheduleSummary(
            id=sched_id,
            workspace=".",
            schedule_text=schedule,
            kind=expr.kind,
            prompt=prompt,
            parent_session_id=parent_session_id,
            enabled=enabled,
            next_fire_at=1000,
            running_task_id=None,
            created_at=500,
            expires_at=expires_at,
            human_schedule=human_schedule(expr),
        )
        self.schedules[sched_id] = summary
        return summary

    def delete_schedule(self, *, schedule_id: str) -> bool:
        if schedule_id in self.schedules:
            del self.schedules[schedule_id]
            return True
        return False

    def list_schedules(self, *, parent_session_id: str | None = None) -> tuple[ScheduleSummary, ...]:
        if parent_session_id is not None:
            return tuple(s for s in self.schedules.values() if s.parent_session_id == parent_session_id)
        return tuple(self.schedules.values())

    def get_schedule(self, *, schedule_id: str) -> ScheduleSummary | None:
        return self.schedules.get(schedule_id)


def test_cron_tool_definitions() -> None:
    assert CronCreateTool.definition.name == "cron_create"
    assert CronCreateTool.definition.effects == frozenset({ToolEffect.WRITE})
    assert CronCreateTool.definition.replay_policy == "never"
    assert CronCreateTool.definition.supports_parallel is False

    assert CronDeleteTool.definition.name == "cron_delete"
    assert CronDeleteTool.definition.effects == frozenset({ToolEffect.WRITE})
    assert CronDeleteTool.definition.replay_policy == "never"
    assert CronDeleteTool.definition.supports_parallel is False

    assert CronListTool.definition.name == "cron_list"
    assert CronListTool.definition.effects == frozenset({ToolEffect.READ})
    assert CronListTool.definition.replay_policy == "safe"
    assert CronListTool.definition.supports_parallel is True


def test_cron_create_and_list_and_delete() -> None:
    runtime = _MockCronRuntime()
    create_tool = CronCreateTool(runtime=runtime)
    delete_tool = CronDeleteTool(runtime=runtime)
    list_tool = CronListTool(runtime=runtime)
    context = ToolContext(workspace=Path("."), session_id="session-1")

    # 1. Create schedule
    call = ToolCall(
        tool_name="cron_create",
        arguments={"schedule": "every 10m", "prompt": "check the deploy"},
    )
    result = create_tool.invoke(call, context=context)
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.output, TextOutput)
    created_payload = json.loads(result.output.text)
    assert "id" in created_payload
    assert created_payload["human_schedule"] == "every 10m"
    sched_id = created_payload["id"]

    # 2. List schedules
    list_call = ToolCall(tool_name="cron_list", arguments={})
    list_result = list_tool.invoke(list_call, context=context)
    assert isinstance(list_result, ToolSuccess)
    assert isinstance(list_result.output, TextOutput)
    list_payload = json.loads(list_result.output.text)
    assert "jobs" in list_payload
    jobs = list_payload["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == sched_id
    assert jobs[0]["schedule_text"] == "every 10m"
    assert jobs[0]["human_schedule"] == "every 10m"
    assert jobs[0]["prompt"] == "check the deploy"
    assert jobs[0]["enabled"] is True
    assert jobs[0]["running"] is False

    # 3. Delete schedule
    del_call = ToolCall(tool_name="cron_delete", arguments={"id": sched_id})
    del_result = delete_tool.invoke(del_call, context=context)
    assert isinstance(del_result, ToolSuccess)
    assert isinstance(del_result.output, TextOutput)
    del_payload = json.loads(del_result.output.text)
    assert del_payload == {"id": sched_id}

    # Verify list is now empty
    list_result2 = list_tool.invoke(list_call, context=context)
    assert isinstance(list_result2, ToolSuccess)
    assert isinstance(list_result2.output, TextOutput)
    assert json.loads(list_result2.output.text)["jobs"] == []


def test_cron_create_50_cap_enforced() -> None:
    runtime = _MockCronRuntime()
    create_tool = CronCreateTool(runtime=runtime)
    context = ToolContext(workspace=Path("."), session_id="session-1")

    # Seed 50 schedules
    for i in range(MAX_SCHEDULES_PER_WORKSPACE):
        call = ToolCall(
            tool_name="cron_create",
            arguments={"schedule": "every 10m", "prompt": f"task {i}"},
        )
        res = create_tool.invoke(call, context=context)
        assert isinstance(res, ToolSuccess)

    assert len(runtime.list_schedules()) == 50

    # 51st attempt must fail
    overflow_call = ToolCall(
        tool_name="cron_create",
        arguments={"schedule": "every 10m", "prompt": "overflow task"},
    )
    res51 = create_tool.invoke(overflow_call, context=context)
    assert isinstance(res51, ToolFailure)
    assert "Workspace schedule limit reached (maximum 50 jobs)" in res51.error


def test_cron_create_invalid_schedule_grammar() -> None:
    runtime = _MockCronRuntime()
    create_tool = CronCreateTool(runtime=runtime)
    context = ToolContext(workspace=Path("."), session_id="session-1")

    call = ToolCall(
        tool_name="cron_create",
        arguments={"schedule": "not a valid schedule", "prompt": "test"},
    )
    result = create_tool.invoke(call, context=context)
    assert isinstance(result, ToolFailure)
    assert "Invalid schedule 'not a valid schedule'" in result.error


def test_cron_delete_missing_returns_failure() -> None:
    runtime = _MockCronRuntime()
    delete_tool = CronDeleteTool(runtime=runtime)
    context = ToolContext(workspace=Path("."), session_id="session-1")

    call = ToolCall(
        tool_name="cron_delete",
        arguments={"id": "non-existent-sched-id"},
    )
    result = delete_tool.invoke(call, context=context)
    assert isinstance(result, ToolFailure)
    assert "Schedule 'non-existent-sched-id' not found" in result.error
