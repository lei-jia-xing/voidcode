"""Tests for the voidcode.doctor package."""

from __future__ import annotations

from voidcode import doctor as doctor
from voidcode.doctor import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    CapabilityDoctor,
    CapabilityReport,
    DoctorCheck,
    DoctorCheckType,
    ExecutableChecker,
    FormatterPresetChecker,
    LspServerChecker,
    McpServerChecker,
    create_doctor_for_config,
    create_report,
    format_report,
    format_report_json,
)


def test_module_exports() -> None:
    """Test that all expected exports are available."""
    assert CapabilityCheckResult is not None
    assert CapabilityCheckStatus is not None
    assert CapabilityDoctor is not None
    assert CapabilityReport is not None
    assert DoctorCheck is not None
    assert DoctorCheckType is not None
    assert ExecutableChecker is not None
    assert FormatterPresetChecker is not None
    assert LspServerChecker is not None
    assert McpServerChecker is not None
    assert create_doctor_for_config is not None
    assert create_report is not None
    assert format_report is not None
    assert format_report_json is not None


def test_check_status_enum() -> None:
    """Test that all expected status values are available."""
    assert CapabilityCheckStatus.READY.value == "ready"
    assert CapabilityCheckStatus.NOT_FOUND.value == "not_found"
    assert CapabilityCheckStatus.ERROR.value == "error"
    assert CapabilityCheckStatus.NOT_CONFIGURED.value == "not_configured"


def test_check_type_enum() -> None:
    """Test that all expected check types are available."""
    assert DoctorCheckType.EXECUTABLE.value == "executable"
    assert DoctorCheckType.FORMATTER_PRESET.value == "formatter_preset"
    assert DoctorCheckType.LSP_SERVER.value == "lsp_server"
    assert DoctorCheckType.MCP_SERVER.value == "mcp_server"
