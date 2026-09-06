"""Pure tool-result projection, diagnostics, and serialization helpers.

The runtime loop owns governance and persistence; this module only derives
provider/event-facing payloads from tool results and session metadata.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Protocol

from ...tools.contracts import (
    ToolCall,
    ToolDiagnostics,
    ToolDiagnosticsDetails,
    ToolResult,
)
from ...tools.output import cap_tool_result_output, sanitize_tool_result_data
from ..context.window import ToolResultView
from ..session import SessionState
from ..session_metadata_helpers import session_model_identity
from ..tool_display import build_tool_display, build_tool_status


class _ToolDiagnosticError(Protocol):
    error_kind: str
    error_details: dict[str, object]
    retry_guidance: str | None


def _tool_completed_identity_payload(session: SessionState) -> dict[str, str]:
    """Additive model/provider identity for ``runtime.tool_completed`` payloads.

    Merged into the payload before the existing keys so it never overrides
    result data; omitted entirely when the session metadata does not carry a
    model/provider.
    """
    model, provider = session_model_identity(session.metadata)
    identity: dict[str, str] = {}
    if model is not None:
        identity["model"] = model
    if provider is not None:
        identity["provider"] = provider
    return identity


def _normalized_tool_result(
    *,
    tool_result: ToolResult,
    session: SessionState,
    plan_tool_call: ToolCall,
    sequence: int,
    tool_call_id: str,
) -> tuple[ToolResult, dict[str, object]]:
    """Cap and sanitize a tool result before delivery."""
    _ = plan_tool_call, sequence
    runtime_tool_result_data = dict(tool_result.data)
    tool_result = cap_tool_result_output(
        tool_result,
        session_id=session.session.id,
        tool_call_id=tool_call_id,
    )
    tool_result = replace(
        tool_result,
        data=sanitize_tool_result_data(tool_result.data),
    )
    return tool_result, runtime_tool_result_data


def _tool_completed_payload(
    *,
    session: SessionState,
    tool_result: ToolResult,
    tool_call_id: str,
    sanitized_arguments: dict[str, object],
    display_tool_name: str | None = None,
) -> dict[str, object]:
    """Assemble the ``runtime.tool_completed`` payload for a delivered result.

    ``display_tool_name`` preserves the native-call path's historical display
    selection when a tool returns a result under a different name; the
    invoke-tool path keeps the result name as its default.
    """
    completed_payload: dict[str, object] = {
        **_tool_completed_identity_payload(session),
        **tool_result.data,
        "tool_call_id": tool_call_id,
        "arguments": sanitized_arguments,
        "status": tool_result.status,
        "content": tool_result.content,
        "error": tool_result.error,
    }
    if tool_result.diagnostics is not None:
        completed_payload["diagnostics"] = tool_result.diagnostics.as_payload()
    completed_payload.setdefault("tool", tool_result.tool_name)

    completed_display = build_tool_display(
        tool_result.tool_name if display_tool_name is None else display_tool_name,
        sanitized_arguments,
        result_data=tool_result.data,
    )
    completed_status = build_tool_status(
        tool_result.tool_name,
        tool_call_id,
        phase="completed" if tool_result.status == "ok" else "failed",
        status="completed" if tool_result.status == "ok" else "failed",
        display=completed_display,
    )
    completed_payload["display"] = completed_display
    completed_payload["tool_status"] = completed_status
    return completed_payload


def _serialized_tool_results(tool_results: Sequence[ToolResult | ToolResultView]) -> tuple[dict[str, object], ...]:
    """Serialize authoritative tool results into the strict checkpoint shape."""
    serialized: list[dict[str, object]] = []
    for result in tool_results:
        source_result = result.result if isinstance(result, ToolResultView) else result
        is_err = source_result.status == "error"
        entry: dict[str, object] = {
            "tool_name": source_result.tool_name,
            "content": source_result.content if source_result.content is not None and not is_err else None,
            "status": "error" if is_err else "ok",
            "data": dict(source_result.data),
            "error": source_result.error if source_result.error is not None and is_err else None,
        }
        if is_err:
            if source_result.diagnostics is not None:
                entry["diagnostics"] = source_result.diagnostics.as_payload()
        serialized.append(entry)
    return tuple(serialized)


def _tool_result_call_id(result: ToolResult) -> str | None:
    value = result.data.get("tool_call_id")
    return value if isinstance(value, str) and value else None


def _is_terminal_yield_result(result: ToolResult) -> bool:
    if result.tool_name != "yield":
        return False
    if result.data.get("yield_kind") == "progress":
        return False
    return result.status in ("ok", "error") and isinstance(result.data.get("handoff"), Mapping)


def _progress_payload_size(payload: Mapping[str, object]) -> int:
    try:
        return len(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ValueError("yield progress payload must be JSON serializable") from exc


def _fit_numbered_progress_payload(
    payload: dict[str, object],
    *,
    max_chars: int,
) -> dict[str, object]:
    """Compact user progress after runtime metadata is attached."""
    if _progress_payload_size(payload) <= max_chars:
        return payload
    result = payload.get("result")
    if isinstance(result, str):
        # Keep the required human-readable field, trimming only the overflow
        # introduced by ordinal/retained_chars metadata.
        for length in range(len(result), 0, -1):
            candidate = dict(payload)
            candidate["result"] = result[: max(1, length - 1)] + "…"
            if _progress_payload_size(candidate) <= max_chars:
                return candidate
    data = payload.get("data")
    if data is not None:
        candidate = dict(payload)
        candidate["data"] = {"truncated": True}
        if _progress_payload_size(candidate) <= max_chars:
            return candidate
    types = payload.get("type")
    if isinstance(types, list):
        candidate = dict(payload)
        candidate["type"] = types[:1]
        if _progress_payload_size(candidate) <= max_chars:
            return candidate
    raise ValueError(f"yield progress section must be at most {max_chars} characters after runtime metadata")


def _tool_error_summary(error: str) -> str:
    cleaned = error.removeprefix("Error: ").strip()
    return cleaned or error


def _tool_error_retry_guidance(error: str) -> str | None:
    lowered = error.lower()
    if "validation error:" in lowered:
        return "Retry with corrected arguments that satisfy the tool schema."
    if "permission denied" in lowered:
        return "Adjust the request or approval settings, then retry."
    if "timed out" in lowered or "timeout" in lowered:
        return "Reduce the command scope, increase the timeout, or retry."
    return None


def _tool_error_details(
    *,
    tool_name: str,
    extra: dict[str, object] | None = None,
) -> ToolDiagnosticsDetails:
    details: ToolDiagnosticsDetails = {"tool_name": tool_name}
    if extra:
        details.update(extra)
    return details


def _tool_error_diagnostics(
    *,
    tool_name: str,
    error: str,
    error_kind: str | None = None,
    extra_details: dict[str, object] | None = None,
) -> ToolDiagnostics:
    return ToolDiagnostics(
        kind=error_kind,
        summary=_tool_error_summary(error),
        details=_tool_error_details(tool_name=tool_name, extra=extra_details),
        guidance=_tool_error_retry_guidance(error),
    )


def _tool_error_payload(
    *,
    tool_name: str,
    error: str,
    error_kind: str | None = None,
    extra_details: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "error": error,
        "diagnostics": _tool_error_diagnostics(
            tool_name=tool_name,
            error=error,
            error_kind=error_kind,
            extra_details=extra_details,
        ).as_payload(),
    }


def _tool_diagnostic_payload(
    *,
    tool_name: str,
    error: _ToolDiagnosticError,
) -> dict[str, object]:
    return {
        "diagnostics": ToolDiagnostics(
            kind=error.error_kind,
            summary=_tool_error_summary(str(error)),
            details=_tool_error_details(
                tool_name=tool_name,
                extra=error.error_details,
            ),
            guidance=error.retry_guidance,
        ).as_payload()
    }


__all__ = [
    "_fit_numbered_progress_payload",
    "_is_terminal_yield_result",
    "_normalized_tool_result",
    "_progress_payload_size",
    "_serialized_tool_results",
    "_tool_completed_identity_payload",
    "_tool_completed_payload",
    "_tool_diagnostic_payload",
    "_tool_error_details",
    "_tool_error_diagnostics",
    "_tool_error_payload",
    "_tool_error_retry_guidance",
    "_tool_error_summary",
    "_tool_result_call_id",
]
