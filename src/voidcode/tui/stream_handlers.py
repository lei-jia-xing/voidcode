"""Stream event handling split by event family.

``handle_stream_event`` keeps the session/cursor guards that decide whether an
event may touch this UI at all, then delegates each event family to one small
handler. Ordering and scrollback commit points are unchanged from the original
single-method implementation.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from voidcode.core.events import EventType
from voidcode.sdk.client import CommandResult, StreamEvent
from voidcode.sdk.codec import decode_tool_call, decode_tool_result
from voidcode.sdk.commands import ConfirmationRequest
from voidcode.sdk.icons import Icon
from voidcode.sdk.provider import ProviderTurnResult
from voidcode.sdk.transcript import TranscriptEntry, TranscriptTool

from . import catalog
from .artifact_viewer import InteractiveArtifactViewer
from .formatting import prefixed_rows, safe
from .gates import (
    ActivationCommitted,
    ApprovalGate,
    ApprovalRequested,
    ConfirmationGate,
    ConfirmationRequested,
    GateChange,
    GateDecided,
    RunTerminated,
)
from .rendering import ActiveToolProgress, content_rows

if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp

_TERMINAL_RUN_EVENTS = (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED)
_GATE_EVENTS = (
    EventType.CONFIRMATION_REQUESTED,
    EventType.CONFIRMATION_DECIDED,
    EventType.APPROVAL_REQUESTED,
    EventType.APPROVAL_DECIDED,
)


def handle_stream_event(app: CoreTuiApp, event: StreamEvent, rows: list[str]) -> None:
    if event.session_id != app.session_id:
        return  # Reject wrong-session events immediately without advancing this session's cursor
    committed = app._session_cursors.get(app.session_id, 0)
    if event.sequence <= committed:
        return  # Settled ledger: never replay already committed events
    app._session_cursors[app.session_id] = event.sequence
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    entry = event.transcript
    etype = event.type
    _transcript_entry(app, event, payload, entry, rows)
    if etype in (EventType.USER_MESSAGE, EventType.RUN_RESUMED):
        _run_started(app)
    elif etype == EventType.MODEL_TURN:
        app._record_model_turn(payload, entry.content if entry else None)
    elif etype == EventType.TOOL_INTENT:
        _tool_intent(app, event, payload)
    elif etype == EventType.TOOL_PROGRESS:
        _tool_progress(app, event, payload)
    elif etype in (EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED):
        _tool_terminal(app, event, payload, entry)
    elif etype == EventType.PROVIDER_PROGRESS:
        _provider_progress(app, payload)
    elif etype == EventType.COMMAND_COMPLETED:
        _command_completed(app, payload, rows)
    elif etype == EventType.ACTIVATION_COMMITTED:
        _activation_committed(app, payload, rows)
    elif etype in _GATE_EVENTS:
        _gate_change(app, event)
    elif etype in (EventType.RUN_INTERRUPTED, EventType.RUN_FAILED):
        _run_notice(app, event, payload, rows)
    elif etype == EventType.RUN_COMPLETED:
        _run_completed(app, event)
    elif etype == EventType.OBSERVER_ERROR:
        _observer_error(app, payload, rows)


def _run_started(app: CoreTuiApp) -> None:
    app._run_active = True
    app.status_line = ""


def _transcript_entry(
    app: CoreTuiApp,
    event: StreamEvent,
    payload: Mapping[str, object],
    entry: TranscriptEntry | None,
    rows: list[str],
) -> None:
    if entry is None or entry.sequence != event.sequence or event.type in _TERMINAL_RUN_EVENTS:
        return
    if isinstance(entry.content, ProviderTurnResult):
        app._settle_stream(payload)
    content = entry.content
    if isinstance(content, TranscriptTool):
        invocation = payload.get("invocation_id")
        active = None
        if isinstance(invocation, str) and invocation in app._active_tools:
            tool_entry = app._active_tools[invocation]
            if (
                event.session_id == tool_entry.session_id
                and event.run_id == tool_entry.run_id
                and event.revision_id == tool_entry.revision_id
                and app._generation == tool_entry.generation
            ):
                active = app._active_tools.pop(invocation, None)
        if active is not None and content.icon is None and active.icon is not None:
            content = TranscriptTool(
                content.call,
                content.result,
                interrupted=content.interrupted,
                batch_sequence=content.batch_sequence,
                deferred=content.deferred,
                icon=active.icon,
            )
        app._record_tool_artifact(content)
    rows.extend(content_rows(content, app.terminal.width, app.styles, include_tool_header=True))


def _tool_intent(app: CoreTuiApp, event: StreamEvent, payload: Mapping[str, object]) -> None:
    invocation = payload.get("invocation_id")
    raw_call = payload.get("call")
    if isinstance(invocation, str) and invocation and isinstance(raw_call, Mapping):
        call = decode_tool_call(raw_call)
        icon = _tool_icon(payload.get("icon"))
        app._active_tools[invocation] = ActiveToolProgress(
            invocation_id=invocation,
            tool_name=safe(call.tool_name or "tool"),
            icon=icon,
            start_time=time.monotonic(),
            session_id=event.session_id,
            run_id=event.run_id,
            revision_id=event.revision_id,
            generation=app._generation,
        )
        app._run_active = True


def _tool_progress(app: CoreTuiApp, event: StreamEvent, payload: Mapping[str, object]) -> None:
    invocation = payload.get("invocation_id")
    raw_progress = payload.get("progress")
    if isinstance(invocation, str) and invocation in app._active_tools and isinstance(raw_progress, Mapping):
        active = app._active_tools[invocation]
        if (
            event.session_id == active.session_id
            and app._generation == active.generation
            and event.run_id == active.run_id
            and event.revision_id == active.revision_id
        ):
            text = raw_progress.get("text")
            stream_name = str(raw_progress.get("stream", "stdout"))
            if isinstance(text, str) and text:
                cols = max(1, app.terminal.width)
                rows_budget = max(1, getattr(app.terminal, "height", 24))
                active.append_chunk(stream_name, text, cols=cols, rows=rows_budget)


def _tool_terminal(app: CoreTuiApp, event: StreamEvent, payload: Mapping[str, object], entry: TranscriptEntry | None) -> None:
    invocation = payload.get("invocation_id")
    if isinstance(invocation, str) and invocation in app._active_tools:
        tool_entry = app._active_tools[invocation]
        if (
            event.session_id == tool_entry.session_id
            and event.run_id == tool_entry.run_id
            and event.revision_id == tool_entry.revision_id
            and app._generation == tool_entry.generation
        ):
            app._active_tools.pop(invocation, None)
    if entry is None or not isinstance(entry.content, TranscriptTool):
        raw_result = payload.get("result")
        if isinstance(raw_result, Mapping):
            app._record_tool_artifact(decode_tool_result(raw_result))


def _provider_progress(app: CoreTuiApp, payload: Mapping[str, object]) -> None:
    dispatch = payload.get("dispatch_id")
    delta = payload.get("text_delta")
    if isinstance(dispatch, str) and dispatch and isinstance(delta, str) and delta:
        channel = str(payload.get("channel", "text"))
        target = app._stream_reasoning if channel == "reasoning" else app._stream_buffers
        target.setdefault(dispatch, []).append(delta)
        app._stream_dispatch = dispatch
        app._run_active = True


def _command_completed(app: CoreTuiApp, payload: Mapping[str, object], rows: list[str]) -> None:
    raw_result = payload.get("result")
    raw_name = str(payload.get("name") or "command")
    if isinstance(raw_result, Mapping):
        rows.extend(app._command_receipt_rows(raw_name, CommandResult.from_payload(raw_result)))


def _activation_committed(app: CoreTuiApp, payload: Mapping[str, object], rows: list[str]) -> None:
    # The displayed generation changed: the command catalog is
    # refetched and anything captured under the previous revision is
    # dropped — never relabelled, never retried. Optional views are
    # explicit-only, so activation never fetches or renders them.
    to_revision_id = str(payload.get("to_revision_id") or "")
    overlay = app.active_overlay
    if isinstance(overlay, InteractiveArtifactViewer):
        overlay.mark_revision_changed(to_revision_id)
    dropped = overlay is not None and not isinstance(overlay, InteractiveArtifactViewer) and getattr(overlay, "revision_id", "") != to_revision_id
    if dropped:
        app._close_live_input()
        app.active_overlay = None
    app._apply_gate_change(ActivationCommitted(to_revision_id))
    catalog.load(app)
    app._refresh_health_async()
    app._present_queued_confirmation()
    if dropped:
        rows.append(app.styles.fg("warning", "Warning: pending action discarded after configuration changed"))


def _gate_change(app: CoreTuiApp, event: StreamEvent) -> None:
    for change in _gate_changes(event)[0]:
        app._apply_gate_change(change)


def _run_notice(app: CoreTuiApp, event: StreamEvent, payload: Mapping[str, object], rows: list[str]) -> None:
    if event.type == EventType.RUN_INTERRUPTED:
        app._render_run_notice("interrupted", payload.get("reason"), rows, stop_cause=payload.get("stop_cause"))
    else:
        app._render_run_notice("failed", payload.get("error"), rows)
    app._clear_run_tools(event.session_id, event.run_id)
    for change in _gate_changes(event)[0]:
        app._apply_gate_change(change)
    app._clear_stream()
    app._present_queued_confirmation()


def _run_completed(app: CoreTuiApp, event: StreamEvent) -> None:
    app._clear_run_tools(event.session_id, event.run_id)
    for change in _gate_changes(event)[0]:
        app._apply_gate_change(change)
    app._clear_stream()


def _observer_error(app: CoreTuiApp, payload: Mapping[str, object], rows: list[str]) -> None:
    rows.extend(prefixed_rows("Warning: plugin observer:", str(payload.get("error") or "Observer failed"), app.styles, slot="warning"))


def _approval_gate_from_payload(payload: Mapping[str, object], *, run_id: str, revision_id: str) -> ApprovalGate:
    """Decode one core approval request payload into its typed gate (outer only)."""
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("approval payload requires a non-empty request_id")
    raw_call = payload.get("call")
    if not isinstance(raw_call, Mapping):
        raise ValueError("approval payload requires a call object")
    call = decode_tool_call(raw_call)
    return ApprovalGate(call=call, request_id=request_id, run_id=run_id, revision_id=revision_id)


def _gate_changes(event: StreamEvent) -> tuple[list[GateChange], bool]:
    """Decode one stream event's gate changes plus whether the run terminated.

    Returns ``(changes, run_terminated)``; the caller folds changes through
    the reducer and applies the same gate UI rules as before.
    """
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    if event.type == EventType.CONFIRMATION_REQUESTED:
        if str(payload.get("ui_type", "")) != "confirmation":
            return [], False
        request = ConfirmationRequest.from_payload(payload)
        return [ConfirmationRequested(ConfirmationGate.from_request(request, revision_id=event.revision_id))], False
    if event.type == EventType.CONFIRMATION_DECIDED:
        request_id = str(payload.get("request_id") or "")
        return ([GateDecided(request_id)] if request_id else []), False
    if event.type == EventType.APPROVAL_REQUESTED:
        request_id = payload.get("request_id")
        raw_call = payload.get("call")
        if not isinstance(request_id, str) or not request_id or not isinstance(raw_call, Mapping):
            return [], False
        gate = _approval_gate_from_payload(payload, run_id=event.run_id, revision_id=event.revision_id)
        return [ApprovalRequested(gate)], False
    if event.type == EventType.APPROVAL_DECIDED:
        request_id = str(payload.get("request_id") or "")
        return ([GateDecided(request_id)] if request_id else []), False
    if event.type == EventType.ACTIVATION_COMMITTED:
        return [ActivationCommitted(str(payload.get("to_revision_id") or ""))], False
    if event.type in (EventType.RUN_INTERRUPTED, EventType.RUN_FAILED):
        return [RunTerminated(event.run_id)], True
    if event.type == EventType.RUN_COMPLETED:
        return [RunTerminated(event.run_id)], False
    return [], False


def _tool_icon(raw: object) -> Icon | None:
    """Strict birth icon: None stays generic, malformed fails visibly."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"tool icon must be an object or null, got {type(raw).__name__}")
    glyph = raw.get("glyph")
    if not isinstance(glyph, str):
        raise ValueError("tool icon requires a glyph string")
    return Icon(glyph=glyph)
