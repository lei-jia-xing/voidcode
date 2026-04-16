"""Capability doctor report formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checker import CapabilityCheckResult, CapabilityCheckStatus


@dataclass
class CapabilityReport:
    """A structured report of all capability checks."""

    workspace: Path | None
    summary: dict[str, int]
    results: list[CapabilityCheckResult]
    errors: list[CapabilityCheckResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if there are any errors or missing items."""
        return bool(self.errors)

    @property
    def is_healthy(self) -> bool:
        """Return True if all critical capabilities are ready."""
        return self.summary["missing"] == 0 and self.summary["errors"] == 0

    def to_dict(self) -> dict[str, Any]:
        """Convert report to a dictionary suitable for JSON serialization."""
        return {
            "workspace": str(self.workspace) if self.workspace else None,
            "summary": self.summary,
            "results": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "check_type": r.check_type,
                    "details": r.details,
                    "error_message": r.error_message,
                }
                for r in self.results
            ],
            "has_errors": self.has_errors,
            "is_healthy": self.is_healthy,
        }


def create_report(
    results: list[CapabilityCheckResult],
    workspace: Path | None = None,
) -> CapabilityReport:
    """Create a CapabilityReport from a list of check results.

    Args:
        results: List of CapabilityCheckResult objects
        workspace: Optional workspace path

    Returns:
        A CapabilityReport with categorized results
    """
    errors = [r for r in results if r.status != CapabilityCheckStatus.READY]
    summary = {
        "total": len(results),
        "ready": sum(1 for r in results if r.status == CapabilityCheckStatus.READY),
        "missing": sum(1 for r in results if r.status == CapabilityCheckStatus.NOT_FOUND),
        "errors": sum(1 for r in results if r.status == CapabilityCheckStatus.ERROR),
        "not_configured": sum(
            1 for r in results if r.status == CapabilityCheckStatus.NOT_CONFIGURED
        ),
    }
    return CapabilityReport(
        workspace=workspace,
        summary=summary,
        results=list(results),
        errors=errors,
    )


def format_report(report: CapabilityReport, *, verbose: bool = False) -> str:
    """Format a capability report for human-readable CLI output.

    Args:
        report: The CapabilityReport to format
        verbose: If True, include all details including successful checks

    Returns:
        A formatted string suitable for CLI output
    """
    lines: list[str] = []

    # Use ASCII-safe icons for better cross-platform compatibility
    CHECK_ICON = "[+]"
    CROSS_ICON = "[-]"
    WARN_ICON = "[!]"
    CIRCLE_ICON = "[o]"
    LIGHTNING_ICON = "[*]"

    # Header
    workspace_str = f"workspace={report.workspace}" if report.workspace else "no workspace"
    lines.append(f"VoidCode Capability Doctor [{workspace_str}]")
    lines.append("=" * 60)

    # Summary
    lines.append("\nSummary:")
    lines.append(f"  Total checks: {report.summary['total']}")
    lines.append(f"  {CHECK_ICON} Ready: {report.summary['ready']}")
    if report.summary["missing"] > 0:
        lines.append(f"  {CROSS_ICON} Missing: {report.summary['missing']}")
    if report.summary["errors"] > 0:
        lines.append(f"  {WARN_ICON} Errors: {report.summary['errors']}")
    if report.summary["not_configured"] > 0:
        lines.append(f"  {CIRCLE_ICON} Not configured: {report.summary['not_configured']}")

    # Detailed results
    lines.append("\nResults:")

    if not verbose:
        # Only show problems by default
        problem_results = [r for r in report.results if r.status != CapabilityCheckStatus.READY]
        if not problem_results:
            lines.append("  All capabilities are ready!")
            return "\n".join(lines)

        for result in problem_results:
            lines.append(_format_result(result, CHECK_ICON, CROSS_ICON, WARN_ICON, CIRCLE_ICON))
    else:
        # Show all results
        for result in report.results:
            lines.append(_format_result(result, CHECK_ICON, CROSS_ICON, WARN_ICON, CIRCLE_ICON))

    # Recommendations
    if report.errors:
        lines.append("\nRecommendations:")
        for error in report.errors:
            if error.error_message:
                lines.append(f"  {LIGHTNING_ICON} {error.error_message}")

    return "\n".join(lines)


def _format_result(
    result: CapabilityCheckResult,
    check_icon: str = "[+]",
    cross_icon: str = "[-]",
    warn_icon: str = "[!]",
    circle_icon: str = "[o]",
) -> str:
    """Format a single check result."""
    status_icon_map = {
        CapabilityCheckStatus.READY: check_icon,
        CapabilityCheckStatus.NOT_FOUND: cross_icon,
        CapabilityCheckStatus.ERROR: warn_icon,
        CapabilityCheckStatus.NOT_CONFIGURED: circle_icon,
    }
    status_icon = status_icon_map.get(result.status, "?")

    status_text = {
        CapabilityCheckStatus.READY: "ready",
        CapabilityCheckStatus.NOT_FOUND: "not found",
        CapabilityCheckStatus.ERROR: "error",
        CapabilityCheckStatus.NOT_CONFIGURED: "not configured",
    }.get(result.status, "unknown")

    lines = [f"  {status_icon} {result.name}: {status_text}"]

    if result.error_message and result.status != CapabilityCheckStatus.READY:
        # Indent error message
        for line in result.error_message.split("\n"):
            lines.append(f"    {line}")

    if result.details:
        # Show relevant details
        details_lines = _format_details(result)
        for detail in details_lines:
            lines.append(f"    {detail}")

    return "\n".join(lines)


def _format_details(result: CapabilityCheckResult) -> list[str]:
    """Format relevant details from a check result."""
    details: list[str] = []
    d = result.details

    if "command" in d and result.status != CapabilityCheckStatus.READY:
        details.append(f"command: {d['command']}")
    if "available_commands" in d:
        details.append(f"available: {', '.join(d['available_commands'])}")
    if "languages" in d and d["languages"]:
        details.append(f"languages: {', '.join(d['languages'])}")
    if "extensions" in d and d["extensions"]:
        ext_sample = d["extensions"][:5]
        if len(d["extensions"]) > 5:
            details.append(f"extensions: {', '.join(ext_sample)} +{len(d['extensions']) - 5} more")
        else:
            details.append(f"extensions: {', '.join(ext_sample)}")
    if "preset_name" in d:
        details.append(f"preset: {d['preset_name']}")
    if "version_info" in d and d["version_info"]:
        details.append(f"version: {d['version_info']}")

    return details


def format_report_json(report: CapabilityReport) -> str:
    """Format a capability report as JSON.

    Args:
        report: The CapabilityReport to format

    Returns:
        A JSON string representation
    """
    return json.dumps(report.to_dict(), indent=2)
