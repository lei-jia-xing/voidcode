from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.command.loop import (
    DEFAULT_INTERVAL,
    loop_confirmation_message,
    parse_loop_command,
)
from voidcode.runtime.background.models import ScheduleSummary
from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.service import VoidCodeRuntime


@pytest.mark.parametrize(
    ("args", "expected_interval", "expected_prompt"),
    [
        ("5m /babysit-prs", "5m", "/babysit-prs"),
        ("30m check the deploy", "30m", "check the deploy"),
        ("1h /standup 1", "1h", "/standup 1"),
        ("2d run full backup", "2d", "run full backup"),
        ("check the deploy every 20m", "20m", "check the deploy"),
        ("run tests every 5 minutes", "5m", "run tests"),
        ("build docs every 2 hours", "2h", "build docs"),
        ("check the deploy", DEFAULT_INTERVAL, "check the deploy"),
        ("check every PR", DEFAULT_INTERVAL, "check every PR"),
    ],
)
def test_parse_loop_command_valid_cases(args: str, expected_interval: str, expected_prompt: str) -> None:
    interval, prompt = parse_loop_command(args)
    assert interval == expected_interval
    assert prompt == expected_prompt


@pytest.mark.parametrize(
    "args",
    [
        "",
        "   ",
        "5m",
        "30m   ",
        "every 20m",
    ],
)
def test_parse_loop_command_empty_prompt_returns_none_for_usage(args: str) -> None:
    interval, prompt = parse_loop_command(args)
    assert interval is None
    assert prompt is None


def test_loop_confirmation_message() -> None:
    summary = ScheduleSummary(
        id="sched-abc123",
        workspace=".",
        schedule_text="every 10m",
        kind="every",
        prompt="check the deploy",
        parent_session_id=None,
        enabled=True,
        next_fire_at=1000,
        running_task_id=None,
        created_at=500,
        expires_at=500 + 7 * 86400,
        human_schedule="every 10m",
    )
    msg = loop_confirmation_message(summary)
    assert "check the deploy" in msg
    assert "every 10m" in msg
    assert "sched-abc123" in msg
    assert "7 days" in msg
    assert "cron_delete" in msg
    assert "voidcode cron remove sched-abc123" in msg


def test_loop_schedule_persisted_and_immediate_fire(tmp_path: Path) -> None:
    config = RuntimeConfig(approval_mode="yolo")
    with VoidCodeRuntime(workspace=tmp_path, config=config) as runtime:
        raw_cmd = "5m check the build status"
        interval, prompt = parse_loop_command(raw_cmd)
        assert interval == "5m"
        assert prompt == "check the build status"

        # 1. Schedule created
        summary = runtime.create_schedule(
            schedule=f"every {interval}",
            prompt=prompt,
            parent_session_id=None,
        )
        assert summary.schedule_text == "every 5m"
        assert summary.human_schedule == "every 5m"
        assert summary.prompt == "check the build status"

        # 2. Schedule persisted in database
        loaded = runtime.get_schedule(schedule_id=summary.id)
        assert loaded is not None
        assert loaded.id == summary.id
        assert loaded.prompt == "check the build status"

        # 3. Immediate fire
        task_state = runtime.fire_schedule(schedule_id=summary.id)
        assert task_state is not None
        assert task_state.task.id is not None

        # Verify running_task_id is recorded on the schedule
        updated = runtime.get_schedule(schedule_id=summary.id)
        assert updated is not None
        assert updated.running_task_id == task_state.task.id
