"""VoidCode capability checks for command availability and configuration.

This module reports local executable availability and configured capabilities.
It does not verify runtime behavior or remote connectivity.
"""

from __future__ import annotations

from .checker import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
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
