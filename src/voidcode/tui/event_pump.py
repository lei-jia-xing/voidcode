"""Typed event pump and handlers for the inline TUI.

Workers construct ``tui.events`` frames; this module owns the one drain loop,
the session/generation guards, and the handler table. ``CoreTuiApp`` remains
the state owner; handlers call its state and rendering methods directly.
"""

from __future__ import annotations

import queue
import time
from typing import TYPE_CHECKING

from voidcode.sdk.views import ChoicesViewData, MarkdownViewData, PluginViewDescriptor, parse_view_data

from . import catalog
from .artifact_viewer import InteractiveArtifactViewer
from .events import (
    ArtifactErrorEvent,
    ArtifactEvent,
    ArtifactImageErrorEvent,
    ArtifactImageEvent,
    ArtifactImageProgressEvent,
    ArtifactOpenEvent,
    ArtifactPageEvent,
    ArtifactTransmittedEvent,
    CancelDoneEvent,
    CommandDoneEvent,
    CommandInputReadyEvent,
    CommandPresentationEvent,
    ConfirmationEvent,
    DecisionDoneEvent,
    FollowupFailedEvent,
    FollowupQueuedEvent,
    FormLoadEvent,
    GateAcceptedEvent,
    HealthEvent,
    InputEofEvent,
    InputEvent,
    InvokeAcceptedEvent,
    KittyReplyEvent,
    LiveInputReadyEvent,
    ModelSwitchDoneEvent,
    NoticeEvent,
    ReconciledEvent,
    RunRejectedEvent,
    RunStatusEvent,
    RunWaitingEvent,
    ServerRestartEvent,
    SessionCreatedEvent,
    SessionCreateFailedEvent,
    SessionsFailedEvent,
    SessionsLoadedEvent,
    StreamErrorEvent,
    StreamEventEvent,
    StreamHeadEvent,
    ViewsFailedEvent,
    ViewsLoadedEvent,
    WakeEvent,
)
from .formatting import notice_row, prefixed_rows, safe
from .gates import ApprovalRequested, ConfirmationGate, ConfirmationRequested, GateDecided
from .views import InteractiveChoices, InteractiveConfirmation, InteractiveForm, InteractiveMarkdown, render_plugin_view

if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


def drain_events(app: CoreTuiApp, timeout: float | None = None) -> int:
    """Drain typed frames; with timeout, block for the first. Returns frames dequeued."""
    rows_to_commit: list[str] = []
    processed = 0
    wait = timeout
    while True:
        try:
            frame = app.event_queue.get_nowait() if wait is None else app.event_queue.get(timeout=max(0.0, wait))
            wait = None
        except queue.Empty:
            break
        processed += 1
        if frame.generation is not None and frame.generation != app._generation:
            continue  # a worker from a previous session: never touch this UI
        if frame.session_id is not None and frame.session_id != app.session_id:
            continue  # a result for another session can never commit rows here
        if not app._stream_ready and isinstance(frame, (StreamHeadEvent, StreamEventEvent, ServerRestartEvent, StreamErrorEvent)):
            # A frame on a not-ready stream means a (re)connection was just
            # established: run the one reconciliation path. The worker rearms
            # the flag on every reconnect, so announced restarts and silent
            # drops share this code path — no per-frame re-trigger.
            app._stream_ready = True
            app._reconcile_connection_async()
        match frame:
            case WakeEvent():
                pass
            case InputEvent():
                _handle_input(app, frame, rows_to_commit)
            case InputEofEvent():
                _handle_input_eof(app, frame, rows_to_commit)
            case KittyReplyEvent():
                _handle_kitty(app, frame, rows_to_commit)
            case StreamHeadEvent():
                _handle_stream_head(app, frame, rows_to_commit)
            case StreamEventEvent():
                _handle_stream_event(app, frame, rows_to_commit)
            case ServerRestartEvent():
                _handle_server_restart(app, frame, rows_to_commit)
            case StreamErrorEvent():
                _handle_stream_error(app, frame, rows_to_commit)
            case HealthEvent():
                _handle_health(app, frame, rows_to_commit)
            case ReconciledEvent():
                _handle_reconciled(app, frame, rows_to_commit)
            case NoticeEvent():
                _handle_notice(app, frame, rows_to_commit)
            case CommandDoneEvent():
                _handle_command_done(app, frame, rows_to_commit)
            case CommandInputReadyEvent():
                _handle_command_input_ready(app, frame, rows_to_commit)
            case LiveInputReadyEvent():
                _handle_live_input_ready(app, frame, rows_to_commit)
            case CommandPresentationEvent():
                _handle_command_presentation(app, frame, rows_to_commit)
            case GateAcceptedEvent():
                _handle_gate_accepted(app, frame, rows_to_commit)
            case DecisionDoneEvent():
                _handle_decision_done(app, frame, rows_to_commit)
            case InvokeAcceptedEvent():
                _handle_invoke_accepted(app, frame, rows_to_commit)
            case RunStatusEvent():
                _handle_run_status(app, frame, rows_to_commit)
            case CancelDoneEvent():
                _handle_cancel_done(app, frame, rows_to_commit)
            case FollowupQueuedEvent():
                _handle_followup_queued(app, frame, rows_to_commit)
            case FollowupFailedEvent():
                _handle_followup_failed(app, frame, rows_to_commit)
            case RunRejectedEvent():
                _handle_run_rejected(app, frame, rows_to_commit)
            case RunWaitingEvent():
                _handle_run_waiting(app, frame, rows_to_commit)
            case ConfirmationEvent():
                _handle_confirmation(app, frame, rows_to_commit)
            case FormLoadEvent():
                _handle_form_load(app, frame, rows_to_commit)
            case SessionsLoadedEvent():
                _handle_sessions_loaded(app, frame, rows_to_commit)
            case SessionsFailedEvent():
                _handle_sessions_failed(app, frame, rows_to_commit)
            case SessionCreatedEvent():
                _handle_session_created(app, frame, rows_to_commit)
            case SessionCreateFailedEvent():
                _handle_session_create_failed(app, frame, rows_to_commit)
            case ViewsLoadedEvent():
                _handle_views_loaded(app, frame, rows_to_commit)
            case ViewsFailedEvent():
                _handle_views_failed(app, frame, rows_to_commit)
            case ModelSwitchDoneEvent():
                _handle_model_switch_done(app, frame, rows_to_commit)
            case ArtifactPageEvent():
                _handle_artifact_page(app, frame)
            case ArtifactErrorEvent():
                _handle_artifact_error(app, frame)
            case ArtifactOpenEvent():
                _handle_artifact_open(app, frame)
            case ArtifactImageProgressEvent():
                _handle_artifact_image_progress(app, frame)
            case ArtifactImageEvent():
                _handle_artifact_image(app, frame)
            case ArtifactImageErrorEvent():
                _handle_artifact_image_error(app, frame)
            case ArtifactTransmittedEvent():
                _handle_artifact_transmitted(app, frame)
            case _:
                raise TypeError(f"unhandled TUI frame: {type(frame).__name__}")
    if rows_to_commit:
        app.terminal.commit_rows(rows_to_commit)
    return processed


def _handle_wake(_app: CoreTuiApp, _frame: WakeEvent, _rows: list[str]) -> None:
    return


def _handle_input(app: CoreTuiApp, frame: InputEvent, _rows: list[str]) -> None:
    app._handle_input_bytes(frame.chunk, arrived_at=frame.arrived_at)


def _handle_input_eof(app: CoreTuiApp, _frame: InputEofEvent, _rows: list[str]) -> None:
    app.running = False


def _handle_kitty(app: CoreTuiApp, frame: KittyReplyEvent, _rows: list[str]) -> None:
    app._apply_kitty_reply(frame.reply)


def _handle_stream_head(app: CoreTuiApp, frame: StreamHeadEvent, rows: list[str]) -> None:
    if frame.frame.events:
        app._welcomed_sessions.add(app.session_id)
    for event in frame.frame.events:
        app._handle_stream_event(event, rows)


def _handle_stream_event(app: CoreTuiApp, frame: StreamEventEvent, rows: list[str]) -> None:
    app._welcomed_sessions.add(app.session_id)
    app._handle_stream_event(frame.frame.event, rows)


def _handle_server_restart(app: CoreTuiApp, _frame: ServerRestartEvent, _rows: list[str]) -> None:
    # Watermark only: queued but uncommitted events must never be dropped.
    app.status_line = "Reconnecting…"
    if isinstance(app.active_overlay, InteractiveArtifactViewer):
        app.active_overlay.connection_notice = "Core restarted/reconnecting; immutable source identity retained."


def _handle_stream_error(app: CoreTuiApp, frame: StreamErrorEvent, rows: list[str]) -> None:
    rows.extend(prefixed_rows("Error: event stream:", frame.error or "Stream stopped", app.styles, slot="error"))
    if isinstance(app.active_overlay, InteractiveArtifactViewer):
        app.active_overlay.connection_notice = f"Core connection error: {frame.error or 'stream stopped'}"


def _handle_health(app: CoreTuiApp, frame: HealthEvent, _rows: list[str]) -> None:
    if frame.token.aborted:
        return
    app.upgrade = frame.health
    gate_open = app.pending_approval is not None or isinstance(app.active_overlay, InteractiveConfirmation)
    if frame.health.fenced:
        app.status_line = "Core upgrade in progress: dispatch paused"
    elif not gate_open:
        app.status_line = ""


def _handle_reconciled(app: CoreTuiApp, frame: ReconciledEvent, rows: list[str]) -> None:
    if frame.token.aborted:
        return
    if frame.catalog is not None:
        catalog.cache(app, frame.catalog.commands, frame.catalog.revision_id)
    elif frame.failures and not app.catalog_revision:
        detail = next((item for item in frame.failures if item.startswith("catalog (")), None)
        if detail is not None:
            catalog.unavailable(app, detail.removeprefix("catalog (").removesuffix(")"))
    if frame.session_status:
        app.status_line = f"Run {frame.session_status}" if frame.session_status in ("running", "waiting") else app.status_line
    if frame.failures:
        rows.append(app.styles.fg("warning", f"[Reconnect refresh incomplete: {', '.join(frame.failures)}]"))


def _handle_notice(app: CoreTuiApp, frame: NoticeEvent, rows: list[str]) -> None:
    rows.append(notice_row(frame.text, frame.tone, app.styles))


def _handle_command_done(app: CoreTuiApp, frame: CommandDoneEvent, rows: list[str]) -> None:
    fenced = app.upgrade is not None and app.upgrade.fenced
    gate_open = app.pending_approval is not None or isinstance(app.active_overlay, InteractiveConfirmation)
    if frame.status in ("ok", "error") and not fenced and not gate_open:
        app.status_line = ""
    if frame.result is not None:
        rows.extend(app._command_receipt_rows(frame.name, frame.result))


def _handle_command_input_ready(app: CoreTuiApp, frame: CommandInputReadyEvent, rows: list[str]) -> None:
    if frame.token.aborted or frame.revision_id != app.catalog_revision:
        return
    if app.status_line.startswith("Loading "):
        app.status_line = ""
    if app.active_overlay is not None or app.pending_approval is not None:
        return
    if app.upgrade is not None and app.upgrade.fenced:
        return
    if frame.error:
        app._live_input = None
        app._live_refresh_at = 0.0
        rows.append(app.styles.fg("error", f"Command input unavailable: {safe(frame.error)}"))
        return
    view = frame.view
    if not isinstance(view, PluginViewDescriptor):
        return
    refresh_after_ms = frame.refresh_after_ms
    if refresh_after_ms is not None:
        action = frame.action or view.name
        if not app._mount_live_view(
            view,
            app.catalog_revision,
            name=action,
            arguments=dict(frame.arguments),
            refresh_after_ms=refresh_after_ms,
        ):
            app._close_live_input()
            rows.append(app.styles.fg("warning", f"Live view {action or 'unavailable'} returned an unsupported type; refresh paused"))
    elif not app._mount_interactive_view(view, app.catalog_revision):
        rows.extend(render_plugin_view(view, app.terminal.width, app.styles))


def _handle_live_input_ready(app: CoreTuiApp, frame: LiveInputReadyEvent, rows: list[str]) -> None:
    if frame.token.aborted:
        return
    live = app._live_input
    if live is None or not isinstance(app.active_overlay, (InteractiveChoices, InteractiveMarkdown)):
        return
    if live.get("source") is not app.active_overlay:
        return
    same_action = frame.action == str(live.get("name", ""))
    same_args = dict(frame.arguments) == dict(live.get("arguments") or {})
    if not (same_action and same_args):
        return
    if frame.revision_id != app.catalog_revision or frame.revision_id != str(live.get("revision", "")):
        return
    refresh_after_ms = frame.refresh_after_ms
    cadence = live.get("refresh_after_ms")
    if not isinstance(cadence, int):
        cadence = refresh_after_ms
    view = frame.view
    if frame.error or not isinstance(view, PluginViewDescriptor):
        app.status_line = f"Refresh failed: {safe(frame.error or 'unavailable')} · reopen to retry"
        if isinstance(cadence, int):
            app._live_refresh_at = time.monotonic() + cadence / 1000.0
        return
    try:
        data = parse_view_data(view.view_type, view.data)
    except Exception as exc:
        app.status_line = f"Refresh failed: {safe(exc)} · reopen to retry"
        if isinstance(cadence, int):
            app._live_refresh_at = time.monotonic() + cadence / 1000.0
        return
    if view.view_type == "choices" and isinstance(app.active_overlay, InteractiveChoices):
        if not isinstance(data, ChoicesViewData):
            return
        app.active_overlay.update_view(data)
        app.active_overlay.revision_id = frame.revision_id
        app.active_overlay.title = view.title
    elif view.view_type == "markdown" and isinstance(app.active_overlay, InteractiveMarkdown):
        if not isinstance(data, MarkdownViewData):
            return
        previous_scroll = app.active_overlay.scroll
        app.active_overlay.view = data
        app.active_overlay.revision_id = frame.revision_id
        app.active_overlay.title = view.title
        app.active_overlay.selected_index = min(app.active_overlay.selected_index, max(0, len(data.actions) - 1))
        app.active_overlay.scroll = previous_scroll
    else:
        rows.append(app.styles.fg("warning", f"Live view returned {view.view_type}; refresh paused until reopened"))
        app._close_live_input()
        return
    app._arm_live_input(
        name=str(live.get("name", "")),
        arguments=dict(live.get("arguments") or {}),
        revision=frame.revision_id,
        refresh_after_ms=refresh_after_ms if refresh_after_ms is not None else cadence,
        source=app.active_overlay,
    )


def _handle_command_presentation(app: CoreTuiApp, frame: CommandPresentationEvent, _rows: list[str]) -> None:
    if frame.token.aborted:
        return
    view = frame.view
    if isinstance(view, PluginViewDescriptor) and frame.revision_id == app.catalog_revision:
        if app.pending_approval is None and app.active_overlay is None:
            app._mount_interactive_view(view, frame.revision_id)


def _handle_gate_accepted(app: CoreTuiApp, frame: GateAcceptedEvent, _rows: list[str]) -> None:
    app._in_flight.discard(frame.request_id)
    if frame.request_id:
        app._apply_gate_change(GateDecided(frame.request_id))
    app.status_line = ""


def _handle_decision_done(app: CoreTuiApp, frame: DecisionDoneEvent, _rows: list[str]) -> None:
    app._in_flight.discard(frame.request_id)


def _handle_invoke_accepted(app: CoreTuiApp, frame: InvokeAcceptedEvent, _rows: list[str]) -> None:
    app._in_flight.discard(frame.request_id)
    overlay = app.active_overlay
    if (
        frame.request_id
        and isinstance(overlay, (InteractiveChoices, InteractiveForm, InteractiveMarkdown))
        and frame.request_id == f"invoke:{frame.action}@{frame.revision_id}"
        and overlay.revision_id == frame.revision_id
        and overlay is frame.overlay
    ):
        app._close_live_input()
        app.active_overlay = None
        app._present_queued_confirmation()


def _handle_run_status(app: CoreTuiApp, frame: RunStatusEvent, rows: list[str]) -> None:
    if frame.status in ("failed", "interrupted") and not frame.run_id:
        app._render_run_notice(frame.status, frame.error, rows)
    if frame.status in ("completed", "failed", "interrupted"):
        app._run_active = False
        app._cancel_pending = False
        app.status_line = ""
    elif frame.status == "queued":
        app.status_line = "Follow-up queued for when run yields · Esc to interrupt"
    elif frame.status in ("running", "waiting"):
        app._run_active = True
        app.status_line = ""


def _handle_cancel_done(app: CoreTuiApp, _frame: CancelDoneEvent, _rows: list[str]) -> None:
    app._cancel_pending = False
    app.status_line = ""


def _handle_followup_queued(app: CoreTuiApp, _frame: FollowupQueuedEvent, _rows: list[str]) -> None:
    app.status_line = "Follow-up queued for when run yields · Esc to interrupt"


def _handle_followup_failed(app: CoreTuiApp, frame: FollowupFailedEvent, _rows: list[str]) -> None:
    if not app.composer.value.strip():
        app.composer.set_text(frame.prompt)
    app.status_line = f"Error: {frame.error}"


def _handle_run_rejected(app: CoreTuiApp, frame: RunRejectedEvent, _rows: list[str]) -> None:
    app._run_active = False
    app._cancel_pending = False
    if not app.composer.value:
        app.composer.set_text(frame.prompt)


def _handle_run_waiting(app: CoreTuiApp, frame: RunWaitingEvent, _rows: list[str]) -> None:
    app._apply_gate_change(ApprovalRequested(frame.gate))


def _handle_confirmation(app: CoreTuiApp, frame: ConfirmationEvent, _rows: list[str]) -> None:
    gate = ConfirmationGate.from_request(frame.confirmation, revision_id=frame.revision_id)
    app._apply_gate_change(ConfirmationRequested(gate))


def _handle_form_load(app: CoreTuiApp, frame: FormLoadEvent, _rows: list[str]) -> None:
    form = frame.form
    if isinstance(form, InteractiveForm) and form is app.active_overlay:
        if frame.data is not None:
            form.populate_from_load_action(frame.data)
        if frame.error:
            form.load_error = frame.error


def _handle_sessions_loaded(app: CoreTuiApp, frame: SessionsLoadedEvent, _rows: list[str]) -> None:
    app.status_line = ""
    app._present_sessions(list(frame.sessions))


def _handle_sessions_failed(app: CoreTuiApp, frame: SessionsFailedEvent, rows: list[str]) -> None:
    rows.append(app.styles.fg("error", f"Failed to list sessions: {safe(frame.error or 'unknown error')}"))


def _handle_session_created(app: CoreTuiApp, frame: SessionCreatedEvent, rows: list[str]) -> None:
    if frame.new_session_id:
        app.status_line = ""
        app._switch_session(frame.new_session_id)
    else:
        rows.append(app.styles.fg("error", "Failed to create session: empty session id"))


def _handle_session_create_failed(app: CoreTuiApp, frame: SessionCreateFailedEvent, rows: list[str]) -> None:
    rows.append(app.styles.fg("error", f"Failed to create session: {safe(frame.error or 'unknown error')}"))


def _handle_views_loaded(app: CoreTuiApp, frame: ViewsLoadedEvent, _rows: list[str]) -> None:
    app.status_line = ""
    app._present_views(frame.snapshot, frame.name_filter)


def _handle_views_failed(app: CoreTuiApp, frame: ViewsFailedEvent, rows: list[str]) -> None:
    rows.append(app.styles.fg("error", f"Failed to read views: {safe(frame.error or 'unknown error')}"))


def _handle_model_switch_done(app: CoreTuiApp, frame: ModelSwitchDoneEvent, rows: list[str]) -> None:
    if frame.ok:
        rows.append(app.styles.fg("success", f"[Model switched: revision {safe(frame.revision)} admitted]"))
        app._refresh_health_async()
        return
    label = "Config rewrite refused" if frame.stage == "config" else "Model switch failed"
    rows.append(app.styles.fg("error", f"{label}: {safe(frame.error or 'unknown error')}"))


def _artifact_viewer(app: CoreTuiApp, frame: ArtifactEvent) -> InteractiveArtifactViewer | None:
    viewer = app.active_overlay
    if isinstance(viewer, InteractiveArtifactViewer) and frame.viewer == viewer.identity:
        return viewer
    return None


def _handle_artifact_page(app: CoreTuiApp, frame: ArtifactPageEvent) -> None:
    viewer = _artifact_viewer(app, frame)
    if viewer is not None and viewer.accept_page(frame.request_id, frame.page) and viewer.page is not None:
        app._load_artifact_image(viewer)


def _handle_artifact_error(app: CoreTuiApp, frame: ArtifactErrorEvent) -> None:
    viewer = _artifact_viewer(app, frame)
    if viewer is not None:
        viewer.reject_page(frame.request_id, frame.error or "artifact page request failed")


def _handle_artifact_open(app: CoreTuiApp, frame: ArtifactOpenEvent) -> None:
    viewer = _artifact_viewer(app, frame)
    if viewer is not None:
        viewer.finish_open(frame.request_id, frame.message or "external open failed")


def _image_viewer(
    app: CoreTuiApp,
    frame: ArtifactImageProgressEvent | ArtifactImageEvent | ArtifactImageErrorEvent | ArtifactTransmittedEvent,
) -> InteractiveArtifactViewer | None:
    viewer = _artifact_viewer(app, frame)
    if viewer is None or frame.request_id != viewer.image_request_id or frame.sha256 != viewer.source.sha256:
        return None
    return viewer


def _handle_artifact_image_progress(app: CoreTuiApp, frame: ArtifactImageProgressEvent) -> None:
    viewer = _image_viewer(app, frame)
    if viewer is not None:
        viewer.update_image_progress(frame.request_id, frame.sha256, frame.received)


def _handle_artifact_image(app: CoreTuiApp, frame: ArtifactImageEvent) -> None:
    viewer = _image_viewer(app, frame)
    if viewer is not None:
        viewer.accept_image(frame.request_id, frame.sha256, frame.data)
        cached = app.terminal.cached_kitty_image(frame.sha256)
        if cached is not None:
            viewer.mark_image_cached(frame.sha256, cached)


def _handle_artifact_image_error(app: CoreTuiApp, frame: ArtifactImageErrorEvent) -> None:
    viewer = _image_viewer(app, frame)
    if viewer is not None:
        viewer.fail_image(frame.sha256, frame.error or "image load failed")


def _handle_artifact_transmitted(app: CoreTuiApp, frame: ArtifactTransmittedEvent) -> None:
    viewer = _image_viewer(app, frame)
    if viewer is not None and viewer.image_status == "transferring":
        app._artifact_ack_deadline = (viewer.identity, frame.sha256, time.monotonic() + 5.0)
