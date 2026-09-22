"""First-task readiness preflight payloads derived from the doctor contract."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from ..cli_support import EXIT_PROVIDER_ERROR, print_json
from ..doctor import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    CapabilityReport,
    DoctorCheckType,
    create_report,
)
from ..doctor.reporter import FirstTaskReadiness
from ..runtime.contracts import ProviderReadinessResult
from ..runtime.provider_inspection import (
    guidance_for_provider_error_kind,
    missing_credentials_guidance,
    missing_model_guidance,
    unconfigured_provider_guidance,
)


def _readiness_check_result(
    readiness: ProviderReadinessResult,
) -> CapabilityCheckResult:
    """Adapt runtime-owned readiness to the doctor report contract."""
    details: dict[str, object] = {
        "provider": readiness.provider,
        "model": readiness.model,
        "configured": readiness.configured,
        "auth_present": readiness.auth_present,
        "streaming_configured": readiness.streaming_configured,
        "streaming_supported": readiness.streaming_supported,
        "context_window": readiness.context_window,
        "max_output_tokens": readiness.max_output_tokens,
        "fallback_chain": list(readiness.fallback_chain),
        "status": readiness.status,
    }
    return CapabilityCheckResult(
        status=CapabilityCheckStatus.READY if readiness.ok else CapabilityCheckStatus.ERROR,
        name="provider.readiness",
        check_type=DoctorCheckType.PROVIDER_READINESS.value,
        details=details,
        error_message=None if readiness.ok else readiness.guidance,
    )


def _action(kind: str, message: str, command: str) -> dict[str, str]:
    """One remediation: what kind it is, the runtime's guidance, a runnable command."""
    return {"kind": kind, "message": message, "command": command}


def _inspect_command(provider_name: str | None, workspace_arg: str) -> str:
    if provider_name is None:
        return f"voidcode doctor {workspace_arg}"
    return f"voidcode provider inspect {shlex.quote(provider_name)} {workspace_arg}"


def _provider_remediation(
    provider_status: object,
    provider_name: str | None,
    workspace_arg: str,
) -> dict[str, str] | None:
    """The status-specific remediation for a blocked first provider task.

    The guidance text is the runtime's own wording; the command is runnable as
    printed, so a placeholder value never has to be pasted back.
    """
    if provider_status == "missing_model":
        return _action("config_init", missing_model_guidance(), f"voidcode config init {workspace_arg}")
    if provider_status == "unconfigured":
        return _action(
            "provider_credentials",
            unconfigured_provider_guidance(provider_name),
            _inspect_command(provider_name, workspace_arg),
        )
    if provider_status == "missing_auth":
        return _action(
            "provider_credentials",
            missing_credentials_guidance(provider_name),
            _inspect_command(provider_name, workspace_arg),
        )
    if provider_status == "invalid_model":
        return _action(
            "provider_models",
            guidance_for_provider_error_kind("invalid_model"),
            _inspect_command(provider_name, workspace_arg),
        )
    if provider_status == "streaming_unsupported":
        return _action(
            "provider_inspect",
            guidance_for_provider_error_kind("unsupported_feature"),
            _inspect_command(provider_name, workspace_arg),
        )
    return None


def _run_readiness_actions(
    readiness: FirstTaskReadiness,
    *,
    workspace: Path,
) -> list[dict[str, str]]:
    """Return safe, copyable remediation for a first-task block.

    The commands are always runnable as printed; the messages are the runtime's
    own guidance for that provider status.
    """
    details = readiness.details
    provider_name = details.get("provider")
    workspace_arg = f"--workspace {shlex.quote(str(workspace))}"
    actions: list[dict[str, str]] = []
    remediation = _provider_remediation(
        details.get("provider_status"),
        provider_name if isinstance(provider_name, str) and provider_name else None,
        workspace_arg,
    )
    if remediation is not None:
        actions.append(remediation)
    actions.append(_action("doctor", "", f"voidcode doctor {workspace_arg}"))
    return actions


def _readiness_failure_payload(
    report: CapabilityReport,
    *,
    workspace: Path,
) -> dict[str, object]:
    """Build the stable machine-facing first-task readiness failure shape."""
    readiness = report.first_task_readiness
    if readiness is None:
        raise ValueError("doctor report did not include first-task readiness")
    return {
        "status": readiness.status,
        "error": readiness.blockers[0] if readiness.blockers else readiness.summary,
        "actions": _run_readiness_actions(readiness, workspace=workspace),
        "first_task_readiness": readiness.to_dict(),
    }


def print_readiness_failure(payload: dict[str, object]) -> None:
    """Print a concise, secret-free, copyable first-task recovery message."""
    print(f"status: {payload['status']}", file=sys.stderr)
    print(f"error: {payload['error']}", file=sys.stderr)
    print("actions:", file=sys.stderr)
    actions = payload.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict) and isinstance(action.get("command"), str):
                print(f"  {action['command']}", file=sys.stderr)


def run_readiness_preflight(
    *,
    readiness: ProviderReadinessResult,
    workspace: Path,
    json_output: bool,
) -> int | None:
    """Stop provider runs early with doctor-derived guidance when not ready."""
    if readiness.ok:
        return None
    report = create_report([_readiness_check_result(readiness)], workspace=workspace)
    payload = _readiness_failure_payload(report, workspace=workspace)
    if json_output:
        print_json(payload)
    else:
        print_readiness_failure(payload)
    return EXIT_PROVIDER_ERROR


def config_error_readiness_payload(message: str, *, workspace: Path) -> dict[str, object]:
    """Build the same doctor-derived shape for an invalid workspace config."""
    result = CapabilityCheckResult(
        status=CapabilityCheckStatus.ERROR,
        name="runtime.config",
        check_type=DoctorCheckType.RUNTIME_CONFIG.value,
        error_message=message,
    )
    report = create_report([result], workspace=workspace)
    return _readiness_failure_payload(report, workspace=workspace)
