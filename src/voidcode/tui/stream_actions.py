"""Extracted CoreTuiApp behavior: stream actions.

Methods are applied to the app through RuntimeActions mixins; the facade stays in
terminal_app.py and owns composition, not these responsibilities.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from voidcode.sdk.provider import ProviderTokenUsage, ProviderTurnResult
from voidcode.sdk.tools import (
    ToolResult,
)
from voidcode.sdk.transcript import (
    TranscriptTool,
)

from . import events
from .artifact_viewer import (
    InteractiveArtifactViewer,
    artifact_sources,
)
from .formatting import NoticeTone
from .rendering import run_notice_rows
from .term import TerminalKittyReply

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


class StreamActions:
    def _record_user_prompt_history(self: CoreTuiApp, prompt: str) -> None:
        self.composer._push_history(prompt)

    def _record_model_turn(self: CoreTuiApp, payload: Mapping[str, object], entry_content: object = None) -> None:
        usage = None
        if isinstance(entry_content, ProviderTurnResult) and entry_content.usage is not None:
            usage = entry_content.usage
        if usage is None:
            raw_result = payload.get("result")
            if isinstance(raw_result, ProviderTurnResult) and raw_result.usage is not None:
                usage = raw_result.usage
            elif isinstance(raw_result, Mapping):
                usage = raw_result.get("usage")
        if usage is None:
            usage = payload.get("usage")

        in_tok: int | None = None
        out_tok: int | None = None
        if isinstance(usage, ProviderTokenUsage):
            if not isinstance(usage.input_tokens, bool) and isinstance(usage.input_tokens, int):
                in_tok = usage.input_tokens
            if not isinstance(usage.output_tokens, bool) and isinstance(usage.output_tokens, int):
                out_tok = usage.output_tokens
        elif isinstance(usage, Mapping):
            raw_in = usage.get("input_tokens")
            if not isinstance(raw_in, bool) and isinstance(raw_in, int):
                in_tok = raw_in
            raw_out = usage.get("output_tokens")
            if not isinstance(raw_out, bool) and isinstance(raw_out, int):
                out_tok = raw_out

        if in_tok is not None:
            self.latest_input_tokens = in_tok
            self.cumulative_input_tokens += in_tok
        else:
            self.latest_input_tokens = None
        if out_tok is not None:
            self.cumulative_output_tokens += out_tok

    def _record_tool_artifact(self: CoreTuiApp, target: TranscriptTool | ToolResult) -> None:
        if isinstance(target, TranscriptTool):
            tool_name = target.call.tool_name or "tool"
            result = target.result
        else:
            tool_name = target.tool_name or "tool"
            result = target
        sources = artifact_sources(result)
        if sources:
            self._latest_tool_artifacts = (tool_name, sources)

    def _mark_run_active(self: CoreTuiApp) -> None:
        if not self._run_active or self._run_started_at is None:
            self._run_started_at = time.monotonic()
        self._run_active = True

    def _mark_run_inactive(self: CoreTuiApp) -> None:
        self._run_active = False
        if not self._active_tools:
            self._run_started_at = None

    def _mark_command_active(self: CoreTuiApp) -> None:
        self._commands_in_flight += 1

    def _mark_command_finished(self: CoreTuiApp) -> None:
        self._commands_in_flight = max(0, self._commands_in_flight - 1)
        if not self._commands_in_flight and not self._run_active:
            self._cancel_pending = False

    def _render_run_notice(self: CoreTuiApp, status: str, detail: object, rows: list[str], *, stop_cause: object = None) -> None:
        rows.extend(run_notice_rows(status, detail, self.styles, stop_cause=stop_cause))

    def _settle_stream(self: CoreTuiApp, payload: Mapping[str, object]) -> None:
        """Drop the live buffer the committed turn replaces; the rows settle, never replay."""
        dispatch = payload.get("dispatch_id")
        if isinstance(dispatch, str) and dispatch:
            self._stream_buffers.pop(dispatch, None)
            self._stream_reasoning.pop(dispatch, None)
            if self._stream_dispatch == dispatch:
                self._stream_dispatch = None
        else:
            self._stream_buffers.clear()
            self._stream_reasoning.clear()
            self._stream_dispatch = None

    def _clear_run_tools(self: CoreTuiApp, session_id: str, run_id: str | None = None) -> None:
        to_remove = [
            inv_id
            for inv_id, tool in self._active_tools.items()
            if tool.session_id == session_id and (run_id is None or tool.run_id is None or tool.run_id == run_id)
        ]
        for inv_id in to_remove:
            self._active_tools.pop(inv_id, None)

    def _clear_stream(self: CoreTuiApp) -> None:
        self._stream_buffers.clear()
        self._stream_reasoning.clear()
        self._stream_dispatch = None
        if not self._active_tools:
            self._mark_run_inactive()
        self._cancel_pending = False
        self.status_line = ""

    def _close_live_input(self: CoreTuiApp) -> None:
        """Stop polling; the open overlay (if any) is the caller's to clear."""
        self._live_input = None
        self._live_refresh_at = 0.0
        self._follower.abort_live()
        self._overlays.clear_suspended()

    def _apply_kitty_reply(self: CoreTuiApp, reply: TerminalKittyReply) -> None:
        viewer = self.active_overlay
        if isinstance(viewer, InteractiveArtifactViewer) and viewer.source.sha256 == reply.sha256:
            if reply.message != "OK":
                viewer.fail_image(reply.sha256, f"Kitty {reply.operation} rejected image: {reply.message}")
            elif reply.operation == "upload":
                viewer.mark_image_uploaded(reply.sha256)
            if (
                reply.operation == "upload"
                and self._artifact_ack_deadline is not None
                and self._artifact_ack_deadline[:2] == (viewer.identity, reply.sha256)
            ):
                # The awaited acknowledgement arrived (success or failure): release the deadline.
                self._artifact_ack_deadline = None

    def _queue_notice(
        self: CoreTuiApp,
        text: str,
        *,
        tone: NoticeTone = "info",
        session_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        """Hand worker-side text to the UI thread; only the main loop commits rows."""
        self.event_queue.put(
            events.NoticeEvent(
                text=text,
                tone=tone,
                session_id=self.session_id if session_id is None else session_id,
                generation=self._generation if generation is None else generation,
            )
        )

    def _upgrade_pending_notice(self: CoreTuiApp, action: str, *, session_id: str, generation: int) -> None:
        """Name a fenced refusal and refresh identity so the status line shows the fence."""
        self._refresh_health_async(session_id=session_id, generation=generation)
        self._queue_notice(
            f"Core is draining for an upgrade; {action} was not dispatched",
            tone="warning",
            session_id=session_id,
            generation=generation,
        )

    def _refresh_health_async(self: CoreTuiApp, *, session_id: str | None = None, generation: int | None = None) -> None:
        """Refresh server identity/liveness off the UI thread; stale replies are dropped."""
        self._follower.refresh_health(
            self.session_id if session_id is None else session_id,
            self._generation if generation is None else generation,
        )

    def _reconcile_connection_async(self: CoreTuiApp) -> None:
        """ONE connection-established reconciliation path, entirely off the UI thread.

        Runs on the first frame of every (re)connected stream — announced restart
        or silent drop. Fetches health + catalog + durable session status in one
        worker round, then hands the UI thread token-tagged envelopes.
        Optional views are never fetched here: only explicit ``/tui views``
        presents them. Only the newest round renders (generation alone cannot
        order overlapping replies). Never resends the prompt, never touches the
        applied cursor.
        """
        self._follower.reconcile_connection(self.session_id)
