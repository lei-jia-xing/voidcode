"""VoidCode Capability Doctor - runtime capability readiness checker.

This module provides a unified capability doctor surface for checking external
tool readiness including:
- ast-grep binary availability
- formatter presets and their executables
- LSP server commands
- MCP server commands

The doctor is non-blocking and provides structured output suitable for both
CLI users and agents.
"""

from __future__ import annotations

from .checker import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    DoctorCheck,
    DoctorCheckType,
    ExecutableChecker,
    FormatterPresetChecker,
    LspServerChecker,
    McpServerChecker,
)
from .doctor import CapabilityDoctor, create_doctor_for_config
from .reporter import (
    CapabilityReport,
    create_report,
    format_report,
    format_report_json,
)

__all__ = [
    "CapabilityCheckResult",
    "CapabilityCheckStatus",
    "CapabilityDoctor",
    "CapabilityReport",
    "DoctorCheck",
    "DoctorCheckType",
    "ExecutableChecker",
    "FormatterPresetChecker",
    "LspServerChecker",
    "McpServerChecker",
    "create_doctor_for_config",
    "create_report",
    "format_report",
    "format_report_json",
]
