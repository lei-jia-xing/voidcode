"""Pure tool-result projection, diagnostics, and serialization helpers.

The runtime loop owns governance and persistence; this module only derives
provider/event-facing payloads from tool results and session metadata.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ...core.questions import QuestionCancellation
from ...core.transcript import output_text
from ...core.turns import ReportedCall
from ...formats.json_values import json_wire_object
from ...security.redaction import TOOL_OUTPUT_BYTES as MAX_TOOL_OUTPUT_BYTES
from ...tools.contracts import (
    ProgressYield,
    QuestionAnswered,
    QuestionPrepared,
    TerminalYield,
    TerminalYieldFailure,
    ToolDiagnostics,
    ToolDiagnosticsDetails,
    ToolFailure,
)
from ...tools.output import sanitize_tool_arguments
from ..session import SessionState
from ..session_metadata_helpers import session_model_identity
from ..tool_display import build_tool_display, build_tool_status
from .report_codec import report_payload


def _tool_completed_identity_payload(session: SessionState | None) -> dict[str, str]:
    """Additive model/provider identity for ``runtime.tool_completed`` payloads.

    Merged into the payload before the existing keys so it never overrides
    result data; omitted entirely when the session metadata does not carry a
    model/provider.
    """
    if session is None:
        return {}
    model, provider = session_model_identity(session.metadata)
    identity: dict[str, str] = {}
    if model is not None:
        identity["model"] = model
    if provider is not None:
        identity["provider"] = provider
    return identity


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _bounded_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "…"


_PUBLIC_ARTIFACT_FIELDS = frozenset(
    {
        "artifact",
        "artifacts",
        "artifact_id",
        "producer",
        "session_id",
        "tool_call_id",
        "tool_name",
        "kind",
        "byte_count",
        "line_count",
        "sha256",
        "content_type",
        "mime",
        "created_at",
        "path",
        "uri",
        "status",
        "source",
        "error_artifact",
        "output_artifact",
        "body_artifact",
        "reference",
    }
)


def _public_artifact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _public_artifact_value(item) for key, item in value.items() if str(key) in _PUBLIC_ARTIFACT_FIELDS}
    if isinstance(value, list | tuple):
        return [_public_artifact_value(item) for item in value[:8]]
    if isinstance(value, str):
        return _bounded_utf8(value, 256)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _fit_public_tool_payload(
    payload: dict[str, object],
    *,
    data: Mapping[str, object],
    report: ReportedCall,
    sanitized_arguments: Mapping[str, object],
    display_tool_name: str | None,
) -> dict[str, object]:
    content = payload.get("content")
    error = payload.get("error")
    if _json_bytes({key: value for key, value in payload.items() if key != "reported_call"}) <= MAX_TOOL_OUTPUT_BYTES:
        return payload

    argument_bytes = _json_bytes(sanitized_arguments)
    arguments: dict[str, object] = {"omitted": True, "byte_count": argument_bytes}
    compact_data: dict[str, object] = {
        "type": data.get("type", "data"),
        "truncated": True,
        "partial": True,
        "data_truncated": True,
        "byte_count": data.get("byte_count", _json_bytes(data)),
    }
    for key in (
        "artifact",
        "artifacts",
        "artifact_id",
        "reference",
        "error_artifact",
        "output_artifact",
        "body_artifact",
        "path",
        "source",
        "content_type",
        "mime",
        "line_count",
        "offset",
        "offset_unit",
        "next_offset",
        "byte_offset",
        "byte_limit",
        "next_byte_offset",
        "artifact_byte_count",
        "content_hash",
        "command",
        "exit_code",
        "timeout",
        "cwd",
        "stdout_truncated",
        "stderr_truncated",
        "output_char_count",
        "interrupted",
        "timed_out",
        "cancelled",
        "reason",
    ):
        if key in data:
            value = data[key]
            if key == "command" and isinstance(value, str):
                value = _bounded_utf8(value, 512)
            elif key == "cwd" and isinstance(value, str):
                value = _bounded_utf8(value, 256)
            compact_data[key] = _public_artifact_value(value) if key in _PUBLIC_ARTIFACT_FIELDS else value
    compact_data.update(tool_call_id=report.tool_call_id, arguments=arguments)

    display = build_tool_display(
        report.final_tool_name if display_tool_name is None else display_tool_name,
        arguments,
        result_data=compact_data,
    )
    compact: dict[str, object] = {
        **_tool_completed_identity_payload(None),
        **compact_data,
        "tool_call_id": report.tool_call_id,
        "arguments": arguments,
        "status": payload["status"],
        "content": _bounded_utf8(content, 4096) if isinstance(content, str) else content,
        "error": _bounded_utf8(error, 4096) if isinstance(error, str) else error,
        "tool": report.final_tool_name,
        "display": display,
        "tool_status": build_tool_status(
            report.final_tool_name,
            report.tool_call_id,
            phase="completed" if report.result.status == "ok" else "failed",
            status="completed" if report.result.status == "ok" else "failed",
            display=display,
        ),
        "public_projection_truncated": True,
    }
    for key in (
        "model",
        "provider",
        "diagnostics",
        "cancellation_signalled",
        "execution_stopped",
        "side_effect_state",
        "yield_kind",
        "type",
        "result",
        "progress",
        "handoff",
        "questions",
        "responses",
    ):
        value = payload.get(key)
        if key not in compact and value is not None and _json_bytes(value) <= 2048:
            compact[key] = value
    if _json_bytes(compact) > MAX_TOOL_OUTPUT_BYTES:
        for key in ("questions", "responses", "progress", "handoff", "diagnostics", "type", "result"):
            compact.pop(key, None)
            if _json_bytes(compact) <= MAX_TOOL_OUTPUT_BYTES:
                break
    if _json_bytes(compact) > MAX_TOOL_OUTPUT_BYTES:
        compact["content"] = _bounded_utf8(content, 512) if isinstance(content, str) else None
        compact["error"] = _bounded_utf8(error, 512) if isinstance(error, str) else None
        display = build_tool_display(report.final_tool_name, arguments)
        compact["display"] = display
        compact["tool_status"] = build_tool_status(
            report.final_tool_name,
            report.tool_call_id,
            phase="completed" if report.result.status == "ok" else "failed",
            status="completed" if report.result.status == "ok" else "failed",
            display=display,
        )
    if _json_bytes(compact) > MAX_TOOL_OUTPUT_BYTES:
        minimal: dict[str, object] = {
            **{key: compact[key] for key in ("model", "provider") if key in compact},
            "tool_call_id": report.tool_call_id,
            "tool": report.final_tool_name,
            "status": payload["status"],
            "arguments": arguments,
            "content": _bounded_utf8(content, 128) if isinstance(content, str) else None,
            "error": _bounded_utf8(error, 128) if isinstance(error, str) else None,
            "public_projection_truncated": True,
        }
        for key in ("artifact", "artifacts", "artifact_id", "reference", "error_artifact", "output_artifact", "body_artifact", "source_artifact"):
            if key in compact:
                minimal[key] = _public_artifact_value(compact[key])
        compact = minimal
    compact["reported_call"] = payload["reported_call"]
    return compact


def _tool_completed_payload(
    *,
    session: SessionState | None,
    report: ReportedCall,
    display_tool_name: str | None = None,
) -> dict[str, object]:
    """Assemble the stable event projection from the authoritative report."""
    tool_result = report.result
    tool_call_id = report.tool_call_id
    sanitized_arguments = sanitize_tool_arguments(report.authorized_arguments)
    data = dict(tool_result.body.as_payload()) if tool_result.body is not None else {}
    bounds = tool_result.output.bounds
    if bounds.truncated:
        data["truncated"] = True
    if bounds.partial:
        data["partial"] = True
    if bounds.reference is not None:
        data["reference"] = bounds.reference.uri
        if bounds.reference.artifact is not None:
            data.update(json_wire_object(bounds.reference.artifact))
    if bounds.source is not None:
        data["source"] = bounds.source
    if bounds.fallback_reason is not None:
        data["fallback_reason"] = bounds.fallback_reason
    data.update({"tool_call_id": tool_call_id, "arguments": sanitized_arguments})
    serialized_size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    if serialized_size > MAX_TOOL_OUTPUT_BYTES:
        bounded_data: dict[str, object] = {
            "type": data.get("type", "data"),
            "truncated": True,
            "partial": True,
            "data_truncated": True,
            "byte_count": data.get("byte_count", serialized_size),
        }
        for k in (
            "artifact",
            "artifacts",
            "artifact_id",
            "reference",
            "error_artifact",
            "output_artifact",
            "body_artifact",
            "path",
            "source",
            "content_type",
            "mime",
            "line_count",
            "offset",
            "exit_code",
            "limit",
            "next_offset",
            "tool_call_id",
            "arguments",
        ):
            if k in data:
                bounded_data[k] = data[k]
        data = bounded_data
    payload: dict[str, object] = {
        **_tool_completed_identity_payload(session),
        **data,
        "tool_call_id": report.tool_call_id,
        "arguments": sanitized_arguments,
        "status": tool_result.status,
        "content": output_text(tool_result.output),
        "error": tool_result.error if isinstance(tool_result, ToolFailure) else None,
        "tool": report.final_tool_name,
    }
    payload["reported_call"] = report_payload(report)
    if isinstance(tool_result, ToolFailure) and tool_result.diagnostics is not None:
        payload["diagnostics"] = tool_result.diagnostics.as_payload()
    if isinstance(tool_result, ToolFailure) and tool_result.execution is not None:
        payload.update(
            cancellation_signalled=tool_result.execution.cancellation_signalled,
            execution_stopped=tool_result.execution.execution_stopped,
            side_effect_state=tool_result.execution.side_effect_state,
        )
    control = tool_result.control
    if isinstance(control, ProgressYield):
        progress = control.as_payload()
        payload.update(yield_kind="progress", type=progress["type"], result=progress["result"], progress=progress)
    elif isinstance(control, TerminalYield):
        payload.update(yield_kind="terminal", result=control.summary, handoff=control.as_payload())
    elif isinstance(control, TerminalYieldFailure):
        payload["handoff"] = json_wire_object(control.data)
    elif isinstance(control, QuestionPrepared):
        payload["questions"] = [
            {
                "question": prompt.question,
                "header": prompt.header,
                "options": [{"label": option.label, "description": option.description} for option in prompt.options],
                "multiple": prompt.multiple,
                "allow_freeform": prompt.allow_freeform,
            }
            for prompt in control.prompts
        ]
    elif isinstance(control, QuestionAnswered):
        payload["responses"] = [{"header": response.header, "answers": list(response.answers)} for response in control.responses]
    elif isinstance(control, QuestionCancellation):
        payload["cancelled"] = True
    completed_display = build_tool_display(
        tool_result.tool_name if display_tool_name is None else display_tool_name,
        sanitized_arguments,
        result_data=data,
    )
    payload["display"] = completed_display
    payload["tool_status"] = build_tool_status(
        tool_result.tool_name,
        tool_call_id,
        phase="completed" if tool_result.status == "ok" else "failed",
        status="completed" if tool_result.status == "ok" else "failed",
        display=completed_display,
    )
    return _fit_public_tool_payload(
        payload,
        data=data,
        report=report,
        sanitized_arguments=sanitized_arguments,
        display_tool_name=display_tool_name,
    )


def _serialized_tool_results(tool_results: Sequence[ReportedCall]) -> tuple[dict[str, object], ...]:
    """Checkpoint authority is the canonical report, not a second client projection."""
    return tuple(
        {
            "tool_name": report.final_tool_name,
            "status": report.result.status,
            "reported_call": report_payload(report),
        }
        for report in tool_results
    )


def _is_terminal_yield_result(report: ReportedCall) -> bool:
    return report.final_tool_name == "yield" and isinstance(report.result.control, (TerminalYield, TerminalYieldFailure))


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
    if isinstance(data, dict):
        metadata = {key: data[key] for key in ("ordinal", "retained_chars") if key in data}
        candidate = dict(payload)
        candidate["data"] = {"truncated": True, **metadata}
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
    details: dict[str, object] = {"tool_name": tool_name}
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


__all__ = [
    "_fit_numbered_progress_payload",
    "_is_terminal_yield_result",
    "_serialized_tool_results",
    "_tool_completed_identity_payload",
    "_tool_completed_payload",
    "_tool_error_details",
    "_tool_error_diagnostics",
    "_tool_error_payload",
    "_tool_error_retry_guidance",
    "_tool_error_summary",
]
