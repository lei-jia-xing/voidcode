"""Capability doctor report formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .checker import CapabilityCheckResult, CapabilityCheckStatus


@dataclass
class FirstTaskReadiness:
    """Readiness summary for the first provider-backed coding task."""

    status: str
    summary: str
    next_step: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert readiness summary to JSON-safe payload."""
        return {
            "status": self.status,
            "summary": self.summary,
            "next_step": self.next_step,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "details": dict(self.details),
        }


@dataclass
class CapabilityReport:
    """A structured report of all capability checks."""

    workspace: Path | None
    summary: dict[str, int]
    results: list[CapabilityCheckResult]
    errors: list[CapabilityCheckResult] = field(default_factory=list)
    first_task_readiness: FirstTaskReadiness | None = None

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
            "first_task_readiness": (
                self.first_task_readiness.to_dict() if self.first_task_readiness else None
            ),
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
        first_task_readiness=_create_first_task_readiness(results, workspace),
    )


def _create_first_task_readiness(
    results: list[CapabilityCheckResult], workspace: Path | None
) -> FirstTaskReadiness:
    provider_result = next((r for r in results if r.name == "provider.readiness"), None)
    config_result = next((r for r in results if r.name == "runtime.config"), None)
    workspace_arg = f" --workspace {workspace}" if workspace else ""
    doctor_command = f"voidcode doctor{workspace_arg}"
    run_command = f'voidcode run "read README.md"{workspace_arg}'

    if config_result is not None and config_result.status != CapabilityCheckStatus.READY:
        message = config_result.error_message or "Runtime config could not be parsed."
        return FirstTaskReadiness(
            status="not_ready",
            summary="VoidCode cannot evaluate first-task readiness until runtime config is valid.",
            next_step=f"Fix .voidcode.json, then run `{doctor_command}` again.",
            blockers=[message],
            details={
                "workspace_config_valid": False,
                "runtime_config_status": config_result.status.value,
                "local_tools": _local_tool_availability(results),
            },
        )

    if provider_result is None:
        return FirstTaskReadiness(
            status="not_ready",
            summary="No provider-backed readiness check ran for the first coding task.",
            next_step=(
                "Run `voidcode config init --model provider/model"
                f"{workspace_arg}` with a real provider/model, then run `{doctor_command}` again."
            ),
            blockers=["provider.readiness check is missing"],
            details={
                "workspace_config_valid": True,
                "local_tools": _local_tool_availability(results),
            },
        )

    provider_details = dict(provider_result.details)
    if provider_result.status != CapabilityCheckStatus.READY:
        provider_status = str(provider_details.get("status") or provider_result.status.value)
        blocker = provider_result.error_message or "Provider/model readiness is incomplete."
        return FirstTaskReadiness(
            status="not_ready",
            summary=f"First provider-backed coding task is blocked: {provider_status}.",
            next_step=_next_step_for_provider_status(provider_status, workspace_arg),
            blockers=[blocker],
            details=_first_task_details(provider_result, results),
        )

    warnings = [
        f"{result.name}: {result.error_message or result.status.value}"
        for result in results
        if result.status != CapabilityCheckStatus.READY
        and result.name != "provider.readiness"
        and result.name != "runtime.config"
    ]
    if warnings:
        return FirstTaskReadiness(
            status="degraded",
            summary=(
                "Provider/model/auth are ready, but local tooling may reduce first-task quality."
            ),
            next_step=f"You can try `{run_command}` now, then address the warnings below.",
            warnings=warnings,
            details=_first_task_details(provider_result, results),
        )

    return FirstTaskReadiness(
        status="ready",
        summary="Provider/model/auth and local checks are ready for a first coding task.",
        next_step=f"Try `{run_command}`.",
        details=_first_task_details(provider_result, results),
    )


def _first_task_details(
    result: CapabilityCheckResult, results: list[CapabilityCheckResult]
) -> dict[str, Any]:
    details = dict(result.details)
    return {
        "workspace_config_valid": True,
        "provider": details.get("provider"),
        "model": details.get("model"),
        "provider_status": details.get("status"),
        "auth_present": details.get("auth_present"),
        "context_window": details.get("context_window"),
        "max_output_tokens": details.get("max_output_tokens"),
        "fallback_chain": list(details.get("fallback_chain") or []),
        "local_tools": _local_tool_availability(results),
    }


def _local_tool_availability(results: list[CapabilityCheckResult]) -> list[dict[str, str]]:
    return [
        {
            "name": result.name,
            "status": result.status.value,
        }
        for result in results
        if result.name not in {"provider.readiness", "runtime.config"}
    ]


def _next_step_for_provider_status(provider_status: str, workspace_arg: str) -> str:
    if provider_status == "missing_model":
        return (
            "Run `voidcode config init --model provider/model"
            f"{workspace_arg}` with a real provider/model, then rerun doctor."
        )
    if provider_status in {"missing_auth", "unconfigured"}:
        return (
            "Set the provider API key in your environment or user config, then run "
            f"`voidcode doctor{workspace_arg}` again."
        )
    if provider_status == "invalid_model":
        return (
            "Choose a supported provider/model with `voidcode provider models <provider>`, "
            f"then run `voidcode doctor{workspace_arg}` again."
        )
    return f"Follow the provider guidance above, then run `voidcode doctor{workspace_arg}` again."


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

    if report.first_task_readiness is not None:
        lines.append("\nFirst task readiness:")
        readiness = report.first_task_readiness
        lines.append(f"  status: {readiness.status}")
        lines.append(f"  summary: {readiness.summary}")
        lines.append(f"  next: {readiness.next_step}")
        details = readiness.details
        if "workspace_config_valid" in details:
            lines.append(f"  workspace_config_valid: {details['workspace_config_valid']}")
        if "provider" in details:
            lines.append(f"  provider: {details['provider']}")
        if "model" in details:
            lines.append(f"  model: {details['model']}")
        if "auth_present" in details:
            lines.append(f"  auth_present: {details['auth_present']}")
        if "context_window" in details and details["context_window"] is not None:
            lines.append(f"  context_window: {details['context_window']}")
        if "max_output_tokens" in details and details["max_output_tokens"] is not None:
            lines.append(f"  max_output_tokens: {details['max_output_tokens']}")
        local_tools = details.get("local_tools")
        if isinstance(local_tools, list) and local_tools:
            lines.append("  local_tools:")
            for tool in cast(list[object], local_tools):
                if isinstance(tool, dict):
                    tool_map = cast(dict[str, object], tool)
                    lines.append(f"    - {tool_map.get('name')}: {tool_map.get('status')}")
        if readiness.blockers:
            lines.append("  blockers:")
            for blocker in readiness.blockers:
                lines.append(f"    - {blocker}")
        if readiness.warnings:
            lines.append("  warnings:")
            for warning in readiness.warnings:
                lines.append(f"    - {warning}")

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
    if "transport" in d:
        details.append(f"transport: {d['transport']}")
    if "scope" in d:
        details.append(f"scope: {d['scope']}")
    if "configured_enabled" in d:
        details.append(f"configured_enabled: {d['configured_enabled']}")
    if "configured_server_count" in d:
        details.append(f"configured_server_count: {d['configured_server_count']}")
    if "scope_boundary" in d:
        details.append(f"scope_boundary: {d['scope_boundary']}")
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
    if "provider" in d:
        details.append(f"provider: {d['provider']}")
    if "model" in d:
        details.append(f"model: {d['model']}")
    if "auth_present" in d:
        details.append(f"auth_present: {d['auth_present']}")
    if "streaming_supported" in d:
        details.append(f"streaming_supported: {d['streaming_supported']}")
    if "context_window" in d and d["context_window"] is not None:
        details.append(f"context_window: {d['context_window']}")
    if "fallback_chain" in d and d["fallback_chain"]:
        details.append(f"fallback_chain: {', '.join(str(item) for item in d['fallback_chain'])}")

    return details


def format_report_json(report: CapabilityReport) -> str:
    """Format a capability report as JSON.

    Args:
        report: The CapabilityReport to format

    Returns:
        A JSON string representation
    """
    return json.dumps(report.to_dict(), indent=2)
