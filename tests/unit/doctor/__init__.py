"""Tests for the voidcode.doctor package."""

from __future__ import annotations

from voidcode.doctor import (
    CapabilityCheckStatus,
    DoctorCheckType,
)


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
