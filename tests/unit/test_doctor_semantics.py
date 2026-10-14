from __future__ import annotations

import json
import sys
from pathlib import Path

from voidcode.cli.readiness import _readiness_check_result, _run_readiness_actions
from voidcode.doctor import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    create_report,
    format_report,
    format_report_json,
)
from voidcode.doctor.checker import LspServerChecker, McpServerChecker
from voidcode.lsp.contracts import LspServerPreset
from voidcode.runtime.config.loading import RuntimeMcpServerConfig
from voidcode.runtime.contracts import ProviderReadinessResult


def test_lsp_command_check_reports_availability_not_server_readiness() -> None:
    preset = LspServerPreset(id="python", command=(sys.executable,), extensions=(".py",))

    result = LspServerChecker("python", preset).check()

    assert result.status is CapabilityCheckStatus.AVAILABLE
    assert result.details["command"] == [sys.executable]


def test_remote_mcp_is_reported_as_configured_without_connecting() -> None:
    config = RuntimeMcpServerConfig(transport="remote-http", url="https://mcp.example.test")

    result = McpServerChecker("remote", config).check()

    assert result.status is CapabilityCheckStatus.CONFIGURED
    assert result.details["transport"] == "remote-http"


def test_doctor_api_separates_available_and_configured_from_missing() -> None:
    report = create_report(
        [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.AVAILABLE,
                name="lsp:python",
                check_type="lsp_server",
                details={"command": (sys.executable,)},
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.CONFIGURED,
                name="mcp:remote",
                check_type="mcp_server",
                details={"transport": "remote-http"},
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_FOUND,
                name="lsp:missing",
                check_type="lsp_server",
            ),
        ]
    )

    payload = json.loads(format_report_json(report))
    assert payload["summary"] == {
        "total": 3,
        "available": 1,
        "configured": 1,
        "missing": 1,
        "errors": 0,
        "not_configured": 0,
    }
    assert "ready" not in payload["summary"]
    human_report = format_report(report, verbose=True)
    assert "lsp:python: available" in human_report
    assert "mcp:remote: configured" in human_report


def test_provider_readiness_is_configuration_only_not_execution_guarantee(tmp_path: Path) -> None:
    result = _readiness_check_result(
        ProviderReadinessResult(
            provider="openai",
            model="openai/gpt-test",
            configured=True,
            ok=True,
            status="ready",
            guidance="configuration is present",
            auth_present=True,
        )
    )

    assert result.status is CapabilityCheckStatus.CONFIGURED
    readiness = create_report([result], workspace=tmp_path).first_task_readiness
    assert readiness is not None
    assert readiness.status == "configured"
    assert "not verified" in readiness.summary
    assert "not guaranteed" in readiness.summary
    assert readiness.details["provider_config_status"] == "configured"
    assert readiness.details["provider_verification_scope"] == "configuration_only"


def test_provider_configuration_failure_keeps_remediation_actions(tmp_path: Path) -> None:
    result = _readiness_check_result(
        ProviderReadinessResult(
            provider="openai",
            model=None,
            configured=False,
            ok=False,
            status="missing_auth",
            guidance="Credentials are missing.",
        )
    )

    readiness = create_report([result], workspace=tmp_path).first_task_readiness
    assert readiness is not None
    assert readiness.details["provider_config_status"] == "missing_auth"
    actions = _run_readiness_actions(readiness, workspace=tmp_path)
    assert actions[0]["kind"] == "provider_credentials"
