"""Runtime-event projection: ``EventEnvelope`` -> transcript blocks + view state.

This is the behavioural transplant of the old Textual app's event handling
(``tui/app.py``: ``_write_event_line``, ``_render_background_event``,
``_render_tool_completed``, ``_handle_expand``, ``_buffer_tool_progress``,
``_extract_display``, ``_build_syntax_for_path``, ``_diff_renderable``,
``_format_runtime_error``, ``_discard_streamed_attempt``,
``_LIVE_ONLY_EVENT_TYPES``).  The semantics are unchanged; only the target
moved from Textual widgets to transcript blocks.

Load-bearing invariants kept here:

* Live-only provider events (``graph.provider_stream`` and the tool-call
  deltas) reuse the *persisted* sequence cursor, so they bypass the
  persisted-sequence dedupe -- a naive ``sequence > last`` filter silently
  drops streamed text.
* A provider retry/fallback that sets ``discarded_streamed_output`` retracts the
  in-flight attempt: the streamed prose and thinking are cleared from the view,
  not merely hidden.
* Approval and questions are request-id round trips, never callbacks: a pending
  overlay is surfaced and cleared exactly when the matching
  ``runtime.approval_resolved`` / ``runtime.question_answered`` arrives.
* Events the old app never rendered stay ignored -- no new UI is invented for
  them.

The module is pure: no terminal I/O, no ``rich``, no threads, no runtime calls.
It only reads envelopes and mutates its own view state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound

from ..runtime.events import EventEnvelope
from .statusline import StatusSegmentData
from .theme import Theme
from .transcript import (
    AssistantBlock,
    BackgroundTaskBlock,
    ErrorBlock,
    KeyHints,
    NoticeBlock,
    ThinkingBlock,
    ToolBlock,
    Transcript,
    short_session_id,
)

__all__ = [
    "LIVE_ONLY_EVENT_TYPES",
    "ApprovalRequest",
    "QuestionRequest",
    "SessionView",
    "ViewState",
    "format_runtime_error",
]

#: Live-only provider output (``app.py`` ``_LIVE_ONLY_EVENT_TYPES``): the runtime
#: never persists these, so their sequence is the current persisted cursor rather
#: than a fresh identity.  They must bypass the persisted-sequence dedupe (and be
#: retractable when the attempt restarts).
LIVE_ONLY_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "graph.provider_stream",
        "graph.tool_call_start",
        "graph.tool_call_delta",
        "graph.tool_call_end",
    }
)

#: Provider restarts that may retract the in-flight attempt's live projection.
_RETRACTION_EVENT_TYPES: Final[frozenset[str]] = frozenset({"runtime.provider_transient_retry", "runtime.provider_fallback"})

#: The background/delegated notices the old app rendered (``_render_background_event``).
_BACKGROUND_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "runtime.background_task_completed",
        "runtime.background_task_failed",
        "runtime.background_task_cancelled",
        "runtime.background_task_waiting_approval",
        "runtime.background_task_idle_reminder",
        "runtime.background_task_group_completed",
        "runtime.delegated_result_available",
    }
)

#: Tool kinds whose completed body renders as source (path-selected lexer) rather
#: than a plain preview (``app.py`` ``_render_tool_completed``).
_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"read", "search"})

#: Shared default so the frozen ``SessionView`` signature takes no call in a default.
_DEFAULT_HINTS: Final[KeyHints] = KeyHints()


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A pending approval: the fields the old ``ApprovalModal`` displayed."""

    request_id: str
    tool: str
    target: str
    reason: str
    arguments: str


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    """A pending question: the runtime's question payloads, verbatim."""

    request_id: str
    questions: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class ViewState:
    """What the app loop paints outside the transcript.

    ``status`` carries only what an ``EventEnvelope`` can prove -- the turn state
    word and the session label.  Model / mode / path / context / cost come from
    session metadata on the stream chunk, so the app loop fills them when it
    renders the bar.
    """

    state: str
    pending: ApprovalRequest | QuestionRequest | None
    status: StatusSegmentData


def format_runtime_error(error: object) -> str:
    """``_format_runtime_error`` (``app.py``): strip the ``Error:``/``Runtime failed:`` noise."""
    if not isinstance(error, str):
        return "Unknown error"
    cleaned = error.removeprefix("Error: ").strip()
    for prefix in ("Runtime failed:", "runtime failed:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned or error


def _display_field(display: Mapping[str, object] | None, key: str) -> str | None:
    if display is None:
        return None
    value = display.get(key)
    return value if isinstance(value, str) and value else None


def _display_copyable_path(display: Mapping[str, object] | None) -> str | None:
    if display is None:
        return None
    copyable = display.get("copyable")
    if not isinstance(copyable, dict):
        return None
    path = copyable.get("path")
    return path if isinstance(path, str) and path else None


def _display_command(display: Mapping[str, object] | None) -> str | None:
    if display is None:
        return None
    copyable = display.get("copyable")
    if not isinstance(copyable, dict):
        return None
    command = copyable.get("command")
    return command if isinstance(command, str) and command else None


def _tool_header(display: Mapping[str, object] | None, tool_name: str) -> tuple[str, str]:
    """Tool block header: ``display.title`` + ``display.summary``, with fallbacks.

    The old app joined the same two fields into one title string
    (``_render_tool_request_line`` / ``_tool_lifecycle_title``); the transcript
    renders them as ``icon title · summary``, so they stay separate here.
    """
    title = _display_field(display, "title")
    summary = _display_field(display, "summary")
    if title:
        return title, summary or ""
    if summary:
        return summary, ""
    return tool_name, ""


def _request_line_text(tool_name: str, display: Mapping[str, object] | None) -> str:
    """``_render_tool_request_line`` -- the no-``tool_call_id`` request line."""
    title = _display_field(display, "title")
    summary = _display_field(display, "summary")
    if title and summary:
        text = f"▶ {title}: {summary}"
    elif summary:
        text = f"▶ {summary}"
    else:
        text = f"▶ Started tool: {tool_name}"
    path = _display_copyable_path(display)
    if path:
        text += f"\n  {path}"
    command = _display_command(display)
    if command:
        text += f"\n  $ {command}"
    return text


def _tool_detail_lines(display: Mapping[str, object] | None) -> list[str]:
    """``_tool_call_details``: path, command, then the primitive argument list."""
    if display is None:
        return []
    lines: list[str] = []
    args = display.get("args")
    if isinstance(args, list):
        lines.extend(str(arg) for arg in args if isinstance(arg, (str, int, float, bool)))
    command = _display_command(display)
    if command and command not in lines:
        lines.insert(0, f"$ {command}")
    path = _display_copyable_path(display)
    if path and path not in lines:
        lines.insert(0, path)
    return lines


def _progress_lines(streams: Mapping[str, Sequence[str]]) -> list[str]:
    """``_tool_progress_renderable``: ``└ <stream>`` followed by the buffered output."""
    lines: list[str] = []
    for stream_name, chunks in streams.items():
        body = "".join(chunks).rstrip()
        if not body:
            continue
        lines.append(f"└ {stream_name}")
        lines.extend(body.splitlines())
    return lines


def _lexer_name_for_path(path: str | None, content: str) -> str | None:
    """``_build_syntax_for_path``: path (plus content) -> a lexer name.

    The old app built a ``rich`` ``Syntax`` here (``theme="monokai"``); the
    frozen transcript takes a lexer name and owns the palette.
    """
    if not path:
        return None
    try:
        lexer = get_lexer_for_filename(path, content)
    except ClassNotFound:
        return None
    aliases = getattr(lexer, "aliases", None)
    return aliases[0] if aliases else None


def _content_language(kind: str, display: Mapping[str, object] | None, content: str) -> str | None:
    if kind == "edit":
        return "diff"
    if kind in _SOURCE_KINDS:
        return _lexer_name_for_path(_display_copyable_path(display), content)
    return None


class SessionView:
    """The view state the inline app loop paints for one root session."""

    __slots__ = (
        "_assistant",
        "_approval_context",
        "_last_sequence",
        "_pending",
        "_pending_dispatched",
        "_pending_taken",
        "_session_id",
        "_state",
        "_stream_output",
        "_streamed_provider_text",
        "_thinking",
        "_thinking_block",
        "_tool_blocks",
        "_tool_display",
        "_tool_progress",
        "_transcript",
        "_width",
    )

    def __init__(self, *, theme: Theme, hints: KeyHints = _DEFAULT_HINTS, width: int) -> None:
        self._width = max(1, width)
        self._transcript = Transcript(theme, self._width, hints)
        self._state = "Idle"
        self._pending: ApprovalRequest | QuestionRequest | None = None
        self._pending_taken = False
        # True once an answer was handed to the runtime (``resolve_overlay``).
        # Only a dispatched answer can fail to reach the runtime, so only then may
        # ``finish_stream("failed")`` re-arm the request for another attempt.
        self._pending_dispatched = False
        self._session_id: str | None = None
        self._last_sequence: dict[str, int] = {}
        self._stream_output = ""
        self._thinking = ""
        self._streamed_provider_text = False
        self._assistant: AssistantBlock | None = None
        self._thinking_block: ThinkingBlock | None = None
        self._tool_blocks: dict[str, ToolBlock] = {}
        self._tool_display: dict[str, dict[str, object]] = {}
        self._tool_progress: dict[str, dict[str, list[str]]] = {}
        self._approval_context: dict[str, dict[str, object]] = {}

    # -- accessors ---------------------------------------------------------

    def transcript(self) -> Transcript:
        """The block tape the app commits rows from."""
        return self._transcript

    def view_state(self) -> ViewState:
        """The turn state, pending overlay, and event-derived status data."""
        return ViewState(
            state=self._state,
            pending=self._pending,
            status=StatusSegmentData(state=self._state, session_name=short_session_id(self._session_id)),
        )

    @property
    def streamed_provider_text(self) -> bool:
        """True once this attempt streamed provider prose.

        The old app used this flag to avoid rendering the final ``output`` chunk
        a second time after deltas; a retraction resets it, so the app must not
        keep its own copy of the rule.
        """
        return self._streamed_provider_text

    # -- lifecycle ---------------------------------------------------------

    def set_width(self, width: int) -> None:
        """Resize the tape (rows re-render on the next call)."""
        self._width = max(1, width)
        self._transcript.set_width(self._width)

    def reset_for_new_session(self) -> None:
        """Drop everything the old ``session.new`` path cleared.

        The tape itself is emptied; the new-session divider
        (:class:`~voidcode.tui.transcript.SessionMarkerBlock`) belongs to the app
        loop, which alone knows the session switched.

        Note: ``ApprovalRequest``/``QuestionRequest`` overlays are dropped too --
        a new session cannot answer the old session's request.
        """
        self._pending = None
        self._pending_taken = False
        self._pending_dispatched = False
        self._approval_context.clear()
        self._tool_progress.clear()
        self._tool_display.clear()
        self._tool_blocks.clear()
        self._last_sequence.clear()
        self._stream_output = ""
        self._thinking = ""
        self._streamed_provider_text = False
        self._assistant = None
        self._thinking_block = None
        self._session_id = None
        self._state = "Idle"
        self._transcript.blocks.clear()

    def finish_stream(self, final_status: str, *, error: object | None = None) -> None:
        """Settle one stream: the old ``on_stream_completed``/``on_stream_failed``.

        ``final_status`` is the last chunk's session status.  ``waiting`` returns
        early: the pending overlay owns the view (the old app also refused to
        touch the transcript while its modal was up).  ``error`` is the transport
        exception the app caught, if any.
        """
        if error is not None:
            self._transcript.add(ErrorBlock(text=format_runtime_error(error)))
        if final_status == "waiting":
            return
        if self._thinking_block is not None:
            self._thinking_block.expanded = False
        self._transcript.mark_settled()
        self._assistant = None
        self._thinking_block = None
        self._stream_output = ""
        self._thinking = ""
        self._streamed_provider_text = False
        if final_status == "failed":
            # Re-arm only a request whose answer was dispatched but never reached
            # the runtime. A failure while the overlay is still open and
            # unanswered must not reset the hand-out flag, or the user's own
            # answer would re-open the same request and send it twice.
            if self._pending_dispatched:
                self._pending_taken = False
                self._pending_dispatched = False
            self._state = "Failed"
        else:
            # A non-failed finish means any in-flight answer reached the runtime,
            # so a later failure must not re-open it.
            self._pending_dispatched = False
            self._state = "Idle"

    def take_pending_overlay(self) -> ApprovalRequest | QuestionRequest | None:
        """Hand the pending request out once (the app then opens its overlay).

        Taken requests stay pending until the matching resolution event arrives,
        so the app never has to guess whether a request is still live.
        """
        if self._pending is None or self._pending_taken:
            return None
        self._pending_taken = True
        return self._pending

    def resolve_overlay(self, request_id: str) -> None:
        """Record that an answer is being handed to the runtime.

        The request stays pending until ``runtime.question_answered`` /
        ``runtime.approval_resolved`` arrives, so an answer round trip that never
        reaches the runtime can still be re-armed -- the persisted
        ``runtime.question_requested`` event cannot re-deliver, because the
        sequence dedupe has already consumed it. ``_pending_dispatched`` marks
        that this attempt is in flight; only such an attempt may be re-armed.
        """
        if self._pending is None or self._pending.request_id != request_id:
            return
        self._pending_dispatched = True
        self._set_state("Running")

    # -- event projection --------------------------------------------------

    def apply_event(self, event: EventEnvelope) -> bool:
        """Project one runtime event; ``True`` when it changed the view."""
        self._session_id = event.session_id or self._session_id
        event_type = event.event_type
        payload = event.payload or {}

        # Live-only deltas share the persisted cursor by contract, so the
        # sequence dedupe (which exists for replayed persisted events) must not
        # swallow them.
        if event.sequence > 0 and event_type not in LIVE_ONLY_EVENT_TYPES:
            last_sequence = self._last_sequence.get(event.session_id, 0)
            if event.sequence <= last_sequence:
                return False
            self._last_sequence[event.session_id] = event.sequence

        if event_type in _RETRACTION_EVENT_TYPES:
            if payload.get("discarded_streamed_output") is True:
                self._retract_streamed_attempt()
                return True
            return False

        if event_type == "graph.provider_stream":
            return self._apply_provider_stream(payload)

        tool_call_id = payload.get("tool_call_id")
        raw_tool_name = payload.get("tool", "unknown_tool")
        tool_name = raw_tool_name if isinstance(raw_tool_name, str) else "unknown_tool"
        display = _extract_display(payload)

        if event_type == "graph.tool_request_created":
            return self._apply_tool_request_created(tool_name, display, tool_call_id)
        if event_type == "runtime.tool_started":
            return self._apply_tool_started(tool_name, display, tool_call_id)
        if event_type == "runtime.tool_progress":
            return self._apply_tool_progress(tool_name, display, tool_call_id, payload)
        if event_type == "runtime.tool_completed":
            return self._apply_tool_completed(tool_name, display, tool_call_id, payload)
        if event_type == "runtime.approval_requested":
            return self._apply_approval_requested(tool_name, payload)
        if event_type == "runtime.approval_resolved":
            return self._apply_approval_resolved(payload)
        if event_type == "runtime.question_requested":
            return self._apply_question_requested(payload)
        if event_type == "runtime.question_answered":
            return self._apply_question_answered(payload)
        if event_type == "runtime.failed":
            return self._apply_failed(payload)
        if event_type in _BACKGROUND_EVENT_TYPES:
            self._apply_background_event(event_type, payload)
            return True
        # Turn state the old app took from ``chunk.session.status``; these events
        # are the live carriers of those statuses and render no rows.
        if event_type == "runtime.request_received":
            return self._set_state("Running")
        if event_type == "graph.response_ready":
            return self._set_state("Completed")
        return False

    # -- provider stream ---------------------------------------------------

    def _apply_provider_stream(self, payload: Mapping[str, object]) -> bool:
        if payload.get("kind") not in {"delta", "content"}:
            return False
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return False
        channel = payload.get("channel")
        if channel == "reasoning":
            self._thinking += text
            self._ensure_thinking_block().text = self._thinking
            return True
        if channel != "text":
            return False
        self._streamed_provider_text = True
        if self._thinking_block is not None:
            # The old app collapsed the thinking block as soon as prose started.
            self._thinking_block.expanded = False
        self._stream_output += text
        self._ensure_assistant_block().text = self._stream_output
        return True

    def _ensure_thinking_block(self) -> ThinkingBlock:
        if self._thinking_block is None:
            self._thinking_block = ThinkingBlock(text=self._thinking, expanded=True, settled=False)
            self._transcript.add(self._thinking_block)
        return self._thinking_block

    def _ensure_assistant_block(self) -> AssistantBlock:
        if self._assistant is None:
            self._assistant = AssistantBlock(text=self._stream_output, settled=False)
            self._transcript.add(self._assistant)
        return self._assistant

    def _retract_streamed_attempt(self) -> None:
        """``_discard_streamed_attempt``: clear the retracted attempt's live text."""
        self._stream_output = ""
        self._thinking = ""
        self._streamed_provider_text = False
        if self._assistant is not None:
            self._assistant.text = ""
        if self._thinking_block is not None:
            self._thinking_block.text = ""

    # -- tools -------------------------------------------------------------

    def _tool_block(self, tool_call_id: str, tool_name: str) -> ToolBlock:
        block = self._tool_blocks.get(tool_call_id)
        if block is None:
            block = ToolBlock(tool=tool_name, settled=False)
            self._tool_blocks[tool_call_id] = block
            self._transcript.add(block)
        return block

    def _apply_tool_request_created(self, tool_name: str, display: Mapping[str, object] | None, tool_call_id: object) -> bool:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            # The graph omitted an id, so there is nothing to key an updatable
            # block on: the old app wrote a bare line.
            self._transcript.add(NoticeBlock(text=_request_line_text(tool_name, display)))
            return True
        if display is not None:
            self._tool_display[tool_call_id] = dict(display)
        title, summary = _tool_header(display, tool_name)
        block = self._tool_block(tool_call_id, tool_name)
        block.title = title
        block.summary = summary
        block.body = _tool_detail_lines(display)
        block.state = "pending"
        return True

    def _apply_tool_started(self, tool_name: str, display: Mapping[str, object] | None, tool_call_id: object) -> bool:
        title, summary = _tool_header(display, tool_name)
        if not isinstance(tool_call_id, str) or not tool_call_id:
            # No id to update: the old app wrote "◐ <title>: <summary>" as one line.
            self._transcript.add(NoticeBlock(text=f"{title}: {summary}" if summary else title))
            return True
        if display is not None:
            self._tool_display[tool_call_id] = dict(display)
        block = self._tool_block(tool_call_id, tool_name)
        block.title = title
        block.summary = summary
        block.state = "running"
        block.streaming = True
        return True

    def _apply_tool_progress(self, tool_name: str, display: Mapping[str, object] | None, tool_call_id: object, payload: Mapping[str, object]) -> bool:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return False
        chunk = payload.get("chunk")
        if not isinstance(chunk, str) or not chunk:
            return False
        stream = payload.get("stream")
        stream_name = stream if isinstance(stream, str) and stream else "stdout"
        streams = self._tool_progress.setdefault(tool_call_id, {})
        buffer = streams.setdefault(stream_name, [])
        if buffer:
            buffer[-1] = buffer[-1] + chunk
        else:
            buffer.append(chunk)

        block = self._tool_block(tool_call_id, tool_name)
        # A progress payload carries no display; fall back to the tool's own
        # display so the header does not degrade to the bare tool name mid-run.
        resolved = display if display is not None else self._tool_display.get(tool_call_id)
        block.title, block.summary = _tool_header(resolved, tool_name)
        block.body = _progress_lines(streams)
        block.state = "running"
        block.streaming = True
        return True

    def _apply_tool_completed(
        self, tool_name: str, display: Mapping[str, object] | None, tool_call_id: object, payload: Mapping[str, object]
    ) -> bool:
        if display is None and isinstance(tool_call_id, str):
            display = self._tool_display.get(tool_call_id)
        is_error = payload.get("status") == "error"
        title, summary = _tool_header(display, tool_name)
        streams = self._tool_progress.pop(tool_call_id, None) if isinstance(tool_call_id, str) else None
        kind = _display_field(display, "kind") or ""
        content = payload.get("content")
        body: list[str] = []
        language: str | None = None
        if isinstance(content, str) and content and kind != "write":
            # A write's ``content`` is the whole file; the old app never showed it.
            language = _content_language(kind, display, content)
            body = content.splitlines()
        if streams:
            body = [*_progress_lines(streams), *body]

        state = "error" if is_error else "success"
        if not isinstance(tool_call_id, str) or not tool_call_id:
            self._transcript.add(ToolBlock(tool=tool_name, title=title, summary=summary, body=body, language=language, state=state))
            return True
        block = self._tool_block(tool_call_id, tool_name)
        block.title = title
        block.summary = summary
        block.body = body
        block.language = language
        block.state = state
        block.streaming = False
        block.settled = True
        return True

    # -- approvals and questions -------------------------------------------

    def _apply_approval_requested(self, tool_name: str, payload: Mapping[str, object]) -> bool:
        request_id = payload.get("request_id")
        if isinstance(request_id, str) and request_id:
            self._approval_context[request_id] = dict(payload)
            target = payload.get("target_summary")
            reason = payload.get("reason")
            self._pending = ApprovalRequest(
                request_id=request_id,
                tool=tool_name,
                target=target if isinstance(target, str) else "",
                reason=reason if isinstance(reason, str) else "",
                arguments=_format_arguments(payload.get("arguments")),
            )
            self._pending_taken = False
            self._pending_dispatched = False
        # The old app noticed and switched state even without a usable id.
        self._transcript.add(NoticeBlock(text=f"⚠ Approval requested for tool: {tool_name}"))
        self._state = "Waiting approval"
        return True

    def _apply_approval_resolved(self, payload: Mapping[str, object]) -> bool:
        decision = payload.get("decision", "unknown")
        request_id = payload.get("request_id")
        context = self._approval_context.pop(request_id, None) if isinstance(request_id, str) else None
        resolved_tool = context.get("tool") if context is not None else None
        if isinstance(resolved_tool, str) and resolved_tool:
            text = f"ℹ Approval {decision} for tool: {resolved_tool}"
        else:
            text = f"ℹ Approval {decision}"
        self._transcript.add(NoticeBlock(text=text))
        if isinstance(request_id, str) and self._pending is not None and self._pending.request_id == request_id:
            self._pending = None
            self._pending_taken = False
            self._pending_dispatched = False
        self._state = "Running"
        return True

    def _apply_question_requested(self, payload: Mapping[str, object]) -> bool:
        request_id = payload.get("request_id")
        questions = payload.get("questions")
        count = payload.get("question_count", 1)
        if isinstance(request_id, str) and request_id:
            self._pending = QuestionRequest(
                request_id=request_id,
                questions=tuple(questions) if isinstance(questions, list) else (),
            )
            self._pending_taken = False
            self._pending_dispatched = False
        self._transcript.add(NoticeBlock(text=f"? Agent requested input ({count})"))
        self._state = "Waiting input"
        return True

    def _apply_question_answered(self, payload: Mapping[str, object]) -> bool:
        request_id = payload.get("request_id")
        self._transcript.add(ToolBlock(tool="question", title="Answered", body=_answer_lines(payload), state="success"))
        if isinstance(request_id, str) and self._pending is not None and self._pending.request_id == request_id:
            self._pending = None
            self._pending_taken = False
            self._pending_dispatched = False
        self._state = "Running"
        return True

    # -- failures and background notices -----------------------------------

    def _apply_failed(self, payload: Mapping[str, object]) -> bool:
        diagnostics = payload.get("diagnostics")
        summary = diagnostics.get("summary") if isinstance(diagnostics, dict) else None
        error_msg = summary if isinstance(summary, str) else payload.get("error", "Unknown error")
        self._transcript.add(ErrorBlock(text=f"Failed: {format_runtime_error(str(error_msg))}"))
        self._state = "Failed"
        return True

    def _apply_background_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        """``_render_background_event``: same notices, as one tape block."""
        task_id = payload.get("task_id")
        task_label = task_id if isinstance(task_id, str) and task_id else "background task"
        short_task_id = task_label.removeprefix("task-")[:12]
        summary = payload.get("summary_output")
        error = payload.get("error")
        child_session_id = payload.get("child_session_id")

        if event_type == "runtime.background_task_completed":
            title = f"✓ Background completed · {short_task_id}"
            body = summary if isinstance(summary, str) and summary else 'Result is available through task(operation="output").'
        elif event_type == "runtime.background_task_failed":
            title = f"✖ Background failed · {short_task_id}"
            body = error if isinstance(error, str) and error else "Background task failed."
        elif event_type == "runtime.background_task_cancelled":
            title = f"■ Background cancelled · {short_task_id}"
            body = error if isinstance(error, str) and error else "Background task was cancelled."
        elif event_type == "runtime.background_task_waiting_approval":
            title = f"⚠ Background waiting for approval · {short_task_id}"
            body = "Open the child session to resolve its pending approval."
        elif event_type == "runtime.background_task_idle_reminder":
            title = f"◌ Background waiting · {short_task_id}"
            reminder = payload.get("reminder")
            body = reminder if isinstance(reminder, str) and reminder else "Delegated child session is waiting for external action."
        elif event_type == "runtime.background_task_group_completed":
            group_id = payload.get("parallel_group_id")
            title = f"✓ Background group completed · {group_id or 'group'}"
            body = f"Terminal tasks: {payload.get('terminal_task_count', 0)}"
        else:
            title = f"↳ Delegated result available · {short_task_id}"
            body = summary if isinstance(summary, str) and summary else "Delegated result is ready to read."

        text = f"{title}\n{body}"
        if isinstance(child_session_id, str) and child_session_id:
            text += f"\nchild: {child_session_id}"
        text += f"\ntask: {task_label}"
        self._transcript.add(BackgroundTaskBlock(text=text))

    # -- helpers -----------------------------------------------------------

    def _set_state(self, state: str) -> bool:
        if state == self._state:
            return False
        self._state = state
        return True


def _extract_display(payload: Mapping[str, object]) -> dict[str, object] | None:
    """``_extract_display``: the ``display`` projection, when the emitter sent one."""
    raw = payload.get("display")
    return raw if isinstance(raw, dict) else None


def _format_arguments(arguments: object) -> str:
    """``ApprovalModal`` rendered the arguments as indented JSON."""
    if not arguments:
        return ""
    try:
        return json.dumps(arguments, indent=2)
    except TypeError, ValueError:
        return str(arguments)


def _answer_lines(payload: Mapping[str, object]) -> list[str]:
    """``_write_question_answers``: header + ``; ``-joined answers per response."""
    responses = payload.get("responses")
    if not isinstance(responses, list):
        return []
    lines: list[str] = []
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            continue
        header = response.get("header")
        if isinstance(header, str) and header:
            lines.append(header)
        answers = response.get("answers")
        if isinstance(answers, list):
            lines.append("; ".join(str(answer) for answer in answers))
        if index < len(responses) - 1:
            lines.append("")
    return lines
