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
