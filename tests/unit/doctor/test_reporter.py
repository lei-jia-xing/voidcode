"""Tests for the capability doctor reporter module."""

from __future__ import annotations

import json

from voidcode.doctor import (
    create_report,
    format_report,
    format_report_json,
)
from voidcode.doctor.checker import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
)


class TestCreateReport:
    """Tests for the create_report function."""

    def test_create_report_with_all_ready(self) -> None:
        """Test report creation with all checks ready."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="python",
                check_type="executable",
                details={"command": "python"},
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="formatter:python",
                check_type="formatter_preset",
                details={"preset_name": "python"},
            ),
        ]

        report = create_report(results)

        assert report.summary["total"] == 2
        assert report.summary["ready"] == 2
        assert report.summary["missing"] == 0
        assert report.is_healthy is True
        assert report.has_errors is False

    def test_create_report_with_missing(self) -> None:
        """Test report creation with missing capabilities."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="python",
                check_type="executable",
                details={"command": "python"},
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_FOUND,
                name="missing-tool",
                check_type="executable",
                error_message="missing-tool not found",
            ),
        ]

        report = create_report(results)

        assert report.summary["total"] == 2
        assert report.summary["ready"] == 1
        assert report.summary["missing"] == 1
        assert report.is_healthy is False
        assert report.has_errors is True

    def test_create_report_with_runtime_config_error_is_unhealthy(self) -> None:
        """Runtime config parse errors should mark report unhealthy."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="ast-grep",
                check_type="executable",
                details={"command": "ast-grep"},
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.ERROR,
                name="runtime.config",
                check_type="runtime_config",
                error_message="runtime config parse failed",
            ),
        ]

        report = create_report(results)

        assert report.summary["total"] == 2
        assert report.summary["ready"] == 1
        assert report.summary["errors"] == 1
        assert report.is_healthy is False
        assert report.has_errors is True
        assert report.first_task_readiness is not None
        assert report.first_task_readiness.status == "not_ready"
        assert report.first_task_readiness.details["workspace_config_valid"] is False

    def test_create_report_marks_missing_model_first_task_not_ready(self) -> None:
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.ERROR,
                name="provider.readiness",
                check_type="provider_readiness",
                details={"status": "missing_model", "auth_present": None},
                error_message="Configure a provider/model, for example model: 'openai/gpt-4o'.",
            ),
        ]

        report = create_report(results)

        assert report.first_task_readiness is not None
        assert report.first_task_readiness.status == "not_ready"
        assert report.first_task_readiness.details["workspace_config_valid"] is True
        assert report.first_task_readiness.details["local_tools"] == []
        assert report.first_task_readiness.blockers == [
            "Configure a provider/model, for example model: 'openai/gpt-4o'."
        ]
        assert "config init --execution-engine provider" in report.first_task_readiness.next_step

    def test_create_report_marks_ready_provider_with_missing_tool_degraded(self) -> None:
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="provider.readiness",
                check_type="provider_readiness",
                details={
                    "provider": "openai",
                    "model": "gpt-4o",
                    "status": "ready",
                    "auth_present": True,
                    "context_window": 128_000,
                    "fallback_chain": ["openai/gpt-4o"],
                },
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_FOUND,
                name="ast-grep",
                check_type="executable",
                error_message="'ast-grep' not found. Tried: ast-grep",
            ),
        ]

        report = create_report(results)

        assert report.first_task_readiness is not None
        assert report.first_task_readiness.status == "degraded"
        assert report.first_task_readiness.details["workspace_config_valid"] is True
        assert report.first_task_readiness.details["local_tools"] == [
            {"name": "ast-grep", "status": "not_found"}
        ]
        assert report.first_task_readiness.blockers == []
        assert report.first_task_readiness.warnings == [
            "ast-grep: 'ast-grep' not found. Tried: ast-grep"
        ]


class TestFormatReport:
    """Tests for the format_report function."""

    def test_format_report_shows_problems_by_default(self) -> None:
        """Test that format_report only shows problems by default."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="python",
                check_type="executable",
                details={"command": "python"},
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_FOUND,
                name="missing-tool",
                check_type="executable",
                error_message="missing-tool not found in PATH",
            ),
        ]

        report = create_report(results)
        output = format_report(report)

        # Should contain the missing tool
        assert "missing-tool" in output
        # Should contain the header
        assert "VoidCode Capability Doctor" in output
        # Should contain summary
        assert "Summary" in output

    def test_format_report_verbose_shows_all(self) -> None:
        """Test that verbose mode shows all capabilities."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="python",
                check_type="executable",
                details={"command": "python"},
            ),
        ]

        report = create_report(results)
        output = format_report(report, verbose=True)

        assert "python" in output
        assert "ready" in output.lower()

    def test_format_report_healthy_message(self) -> None:
        """Test that healthy reports show success message."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="python",
                check_type="executable",
                details={"command": "python"},
            ),
        ]

        report = create_report(results)
        output = format_report(report)

        assert "ready" in output.lower() or "capabilities are ready" in output.lower()

    def test_format_report_includes_first_task_readiness_section(self) -> None:
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.ERROR,
                name="provider.readiness",
                check_type="provider_readiness",
                details={"status": "missing_auth", "provider": "openai", "model": "gpt-4o"},
                error_message="Add provider credentials.",
            ),
            CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_FOUND,
                name="ast-grep",
                check_type="executable",
                error_message="'ast-grep' not found. Tried: ast-grep",
            ),
        ]

        report = create_report(results)
        output = format_report(report)

        assert "First task readiness:" in output
        assert "status: not_ready" in output
        assert "execution_engine: provider" in output
        assert "workspace_config_valid: True" in output
        assert "provider: openai" in output
        assert "model: gpt-4o" in output
        assert "local_tools:" in output
        assert "ast-grep: not_found" in output
        assert "Add provider credentials." in output

    def test_format_report_includes_first_task_context_budget(self) -> None:
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="provider.readiness",
                check_type="provider_readiness",
                details={
                    "status": "ready",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "auth_present": True,
                    "context_window": 128_000,
                    "max_output_tokens": 8_192,
                },
            ),
        ]

        report = create_report(results)
        output = format_report(report)

        assert "context_window: 128000" in output
        assert "max_output_tokens: 8192" in output


class TestFormatReportJson:
    """Tests for the format_report_json function."""

    def test_format_report_json_output(self) -> None:
        """Test JSON output format."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="python",
                check_type="executable",
                details={"command": "python"},
            ),
        ]

        report = create_report(results)
        json_output = format_report_json(report)

        # Should be valid JSON
        parsed = json.loads(json_output)

        assert "summary" in parsed
        assert "results" in parsed
        assert "first_task_readiness" in parsed
        assert "is_healthy" in parsed
        assert parsed["summary"]["ready"] == 1


class TestCapabilityReport:
    """Tests for the CapabilityReport class."""

    def test_report_to_dict(self) -> None:
        """Test report serialization to dict."""
        results = [
            CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name="test",
                check_type="executable",
                details={"key": "value"},
            ),
        ]

        report = create_report(results)
        dict_output = report.to_dict()

        assert isinstance(dict_output, dict)
        assert "workspace" in dict_output
        assert "summary" in dict_output
        assert "results" in dict_output
        assert "is_healthy" in dict_output
        assert "has_errors" in dict_output
