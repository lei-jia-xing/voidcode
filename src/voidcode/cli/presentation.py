"""Plain and JSON presentation of a finished runtime stream."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..cli_support import RuntimeStreamResult, format_event, serialize_event, serialize_session_state
from ..runtime.events import EventEnvelope
from .output import print_runtime_output, safe_detail
from .runtime_gateway import approval_request_id, last_event, pending_blocked_event

if TYPE_CHECKING:
    # Annotation-only: the CLI never constructs or reaches into the runtime service
    # outside ``runtime_gateway``.
    from ..runtime.service import VoidCodeRuntime


def print_runtime_response(
    result: RuntimeStreamResult,
    *,
    show_thinking: bool = False,
) -> int:
    for event in result.events:
        print(
            format_event(
                event.event_type,
                event.source,
                event.payload,
                show_thinking=show_thinking,
            ),
            flush=True,
        )

    print_runtime_output(result.output)
    return len(result.events)


def print_runtime_failure_footer(
    runtime: VoidCodeRuntime,
    result: RuntimeStreamResult,
    *,
    workspace: Path,
) -> None:
    if result.session.status != "failed":
        return
    failed_event = next(
        (event for event in reversed(result.events) if event.event_type == "runtime.failed"),
        None,
    )
    if failed_event is None:
        return
    try:
        snapshot = runtime.session_debug_snapshot(session_id=result.session.session.id)
    except ValueError as exc:
        print(f"warning: session debug snapshot unavailable: {exc}", file=sys.stderr)
        snapshot = None
    workspace_arg = f"--workspace {shlex.quote(str(workspace))}"
    provider = failed_event.payload.get("provider")
    model = failed_event.payload.get("model")
    provider_error_kind = failed_event.payload.get("provider_error_kind")
    last_tool = snapshot.last_tool if snapshot is not None else None
    resumable = snapshot.resumable if snapshot is not None else None
    print("", file=sys.stderr, flush=True)
    print("VoidCode runtime failure summary", file=sys.stderr, flush=True)
    print(f"  session: {result.session.session.id}", file=sys.stderr, flush=True)
    print(f"  status: {result.session.status}", file=sys.stderr, flush=True)
    if isinstance(provider, str) and provider:
        print(f"  provider: {provider}", file=sys.stderr, flush=True)
    if isinstance(model, str) and model:
        print(f"  model: {model}", file=sys.stderr, flush=True)
    if isinstance(provider_error_kind, str) and provider_error_kind:
        print(f"  provider_error_kind: {provider_error_kind}", file=sys.stderr, flush=True)
    if resumable is not None:
        print(f"  resumable: {str(resumable).lower()}", file=sys.stderr, flush=True)
    if last_tool is not None:
        print(f"  last_successful_tool: {last_tool.tool_name}", file=sys.stderr, flush=True)
    print(
        f"  debug: voidcode sessions debug {result.session.session.id} {workspace_arg}",
        file=sys.stderr,
        flush=True,
    )
    if resumable:
        print(
            f"  resume: voidcode sessions resume {result.session.session.id} {workspace_arg}",
            file=sys.stderr,
            flush=True,
        )


def runtime_stream_payload(
    result: RuntimeStreamResult,
    *,
    workspace: Path,
    show_thinking: bool = False,
) -> dict[str, object]:
    blocked_event = pending_blocked_event(result.session, last_event(result))
    payload: dict[str, object] = {
        "workspace": str(workspace),
        "session": serialize_session_state(result.session),
        "output": result.output,
        "events": [serialize_event(event, show_thinking=show_thinking) for event in result.events],
    }
    if result.session.status == "failed":
        payload["status"] = "failed"
        error = runtime_failed_error(result)
        if error is not None:
            payload["error"] = error
    if blocked_event is not None:
        payload["blocked"] = blocked_payload(result, blocked_event)
    return payload


def runtime_failed_error(result: RuntimeStreamResult) -> str | None:
    failed_event = next(
        (event for event in reversed(result.events) if event.event_type == "runtime.failed"),
        None,
    )
    if failed_event is None:
        return None
    diagnostics = failed_event.payload.get("diagnostics")
    summary = diagnostics.get("summary") if isinstance(diagnostics, dict) else None
    if isinstance(summary, str) and summary:
        return format_runtime_error_summary(summary)
    error = failed_event.payload.get("error")
    if not isinstance(error, str) or not error:
        return None
    return format_runtime_error_summary(error)


def format_runtime_error_summary(error: str) -> str:
    cleaned = error.removeprefix("Error: ").strip()
    if not cleaned:
        return error
    for prefix in ("Runtime failed:", "runtime failed:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned or error


def blocked_payload(result: RuntimeStreamResult, event: EventEnvelope) -> dict[str, object]:
    if event.event_type == "runtime.approval_requested":
        return {
            "kind": "approval_required",
            "session_id": result.session.session.id,
            "request_id": approval_request_id(event),
            "tool": event.payload.get("tool"),
            "target_summary": event.payload.get("target_summary"),
        }
    return {
        "kind": "question_required",
        "session_id": result.session.session.id,
        "request_id": str(event.payload["request_id"]),
        "tool": event.payload.get("tool"),
        "question_count": event.payload.get("question_count"),
        "questions": event.payload.get("questions"),
    }


def print_noninteractive_blocked(
    result: RuntimeStreamResult,
    event: EventEnvelope,
    *,
    workspace: Path | None = None,
) -> None:
    workspace_arg = f" --workspace {shlex.quote(str(workspace))}" if workspace is not None else ""
    session_id = result.session.session.id
    request_id = safe_detail(event.payload.get("request_id"), limit=96)
    tool = safe_detail(event.payload.get("tool"), limit=64)
    if event.event_type == "runtime.question_requested":
        print(
            f"error: question response required for {tool}; "
            f"answer with: voidcode sessions answer {session_id}{workspace_arg} "
            f"--question-request-id {request_id} --response <answer>",
            file=sys.stderr,
            flush=True,
        )
        return
    target = safe_detail(event.payload.get("target_summary"), limit=160)
    target_suffix = f" for {target}" if target else ""
    print(
        f"error: approval required for {tool}{target_suffix}; "
        f"resume with: voidcode sessions resume {session_id}{workspace_arg} "
        f"--approval-request-id {request_id} --approval-decision allow",
        file=sys.stderr,
        flush=True,
    )
