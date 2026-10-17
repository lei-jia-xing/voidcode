from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from voidcode.cli.app import root_cli
from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.service import VoidCodeRuntime


def test_cron_cli_list_empty(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(root_cli, ["cron", "list", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No scheduled tasks found." in result.output


def test_cron_cli_crud_flow(tmp_path: Path) -> None:
    runner = CliRunner()
    config = RuntimeConfig(approval_mode="yolo")

    # 1. Seed a schedule using runtime
    with VoidCodeRuntime(workspace=tmp_path, config=config) as runtime:
        summary = runtime.create_schedule(
            schedule="every 10m",
            prompt="check database health",
        )
        sched_id = summary.id

    # 2. Test cron list (table format)
    list_res = runner.invoke(root_cli, ["cron", "list", "--workspace", str(tmp_path)])
    assert list_res.exit_code == 0
    assert sched_id in list_res.output
    assert "every 10m" in list_res.output
    assert "yes" in list_res.output

    # 3. Test cron list (--json format)
    json_list_res = runner.invoke(root_cli, ["cron", "list", "--json", "--workspace", str(tmp_path)])
    assert json_list_res.exit_code == 0
    payload = json.loads(json_list_res.output)
    assert "schedules" in payload
    assert len(payload["schedules"]) == 1
    assert payload["schedules"][0]["id"] == sched_id
    assert payload["schedules"][0]["prompt"] == "check database health"

    # 4. Test cron show (text format)
    show_res = runner.invoke(root_cli, ["cron", "show", sched_id, "--workspace", str(tmp_path)])
    assert show_res.exit_code == 0
    assert f"ID:             {sched_id}" in show_res.output
    assert "Schedule:       every 10m" in show_res.output
    assert "check database health" in show_res.output

    # 5. Test cron show (--json format)
    show_json_res = runner.invoke(root_cli, ["cron", "show", sched_id, "--json", "--workspace", str(tmp_path)])
    assert show_json_res.exit_code == 0
    show_payload = json.loads(show_json_res.output)
    assert show_payload["id"] == sched_id
    assert show_payload["prompt"] == "check database health"

    # 6. Test cron disable
    dis_res = runner.invoke(root_cli, ["cron", "disable", sched_id, "--workspace", str(tmp_path)])
    assert dis_res.exit_code == 0
    assert f"Schedule '{sched_id}' disabled." in dis_res.output

    with VoidCodeRuntime(workspace=tmp_path, config=config) as runtime:
        assert runtime.get_schedule(schedule_id=sched_id).enabled is False

    # 7. Test cron enable
    en_res = runner.invoke(root_cli, ["cron", "enable", sched_id, "--workspace", str(tmp_path)])
    assert en_res.exit_code == 0
    assert f"Schedule '{sched_id}' enabled." in en_res.output

    with VoidCodeRuntime(workspace=tmp_path, config=config) as runtime:
        assert runtime.get_schedule(schedule_id=sched_id).enabled is True

    # 8. Test cron remove
    rm_res = runner.invoke(root_cli, ["cron", "remove", sched_id, "--workspace", str(tmp_path)])
    assert rm_res.exit_code == 0
    assert f"Schedule '{sched_id}' removed." in rm_res.output

    with VoidCodeRuntime(workspace=tmp_path, config=config) as runtime:
        assert runtime.get_schedule(schedule_id=sched_id) is None


def test_cron_cli_missing_schedule_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    bad_id = "non-existent-sched-id"

    # show missing
    show_res = runner.invoke(root_cli, ["cron", "show", bad_id, "--workspace", str(tmp_path)])
    assert show_res.exit_code != 0
    assert "not found" in show_res.output

    # enable missing
    en_res = runner.invoke(root_cli, ["cron", "enable", bad_id, "--workspace", str(tmp_path)])
    assert en_res.exit_code != 0
    assert "not found" in en_res.output

    # disable missing
    dis_res = runner.invoke(root_cli, ["cron", "disable", bad_id, "--workspace", str(tmp_path)])
    assert dis_res.exit_code != 0
    assert "not found" in dis_res.output

    # remove missing
    rm_res = runner.invoke(root_cli, ["cron", "remove", bad_id, "--workspace", str(tmp_path)])
    assert rm_res.exit_code != 0
    assert "not found" in rm_res.output
