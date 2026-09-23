"""The CLI's runtime boundary.

This module is the only place the CLI loads runtime config, constructs
``VoidCodeRuntime``, and consumes runtime streams; command modules call it
instead of touching the runtime service, graph, or storage directly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypedDict, Unpack, cast

from ..cli_support import (
    EXIT_APPROVAL_DENIED,
    EXIT_APPROVAL_REQUIRED,
    EXIT_CONFIG_ERROR,
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    RuntimeStreamResult,
    format_event,
)
from ..runtime.config import RuntimeConfig, load_runtime_config
from ..runtime.contracts import RuntimeRequest, RuntimeStreamChunk
from ..runtime.events import EventEnvelope
from ..runtime.permission import PermissionDecision, PermissionResolution
from ..runtime.question import QuestionResponse
from ..runtime.service import VoidCodeRuntime
from ..runtime.session import SessionState
from ..runtime.session_metadata_helpers import runtime_state_run_id
from .errors import CliError
from .output import print_runtime_output, safe_detail
from .trace import TracePrinter, discarded_output_notice


class RuntimeConfigKwargs(TypedDict, total=False):
    approval_mode: PermissionDecision | None
    model: str
    reasoning_effort: str | None


def close_runtime(runtime: VoidCodeRuntime) -> None:
    try:
        runtime.__exit__(None, None, None)
    except Exception as exc:
        print(f"warning: runtime cleanup error: {exc}", file=sys.stderr)


@contextmanager
def open_runtime(
    workspace: Path,
    config: RuntimeConfig | None = None,
) -> Iterator[VoidCodeRuntime]:
    """Construct a runtime, yield it, and guarantee cleanup on exit."""
    if config is not None:
        runtime = VoidCodeRuntime(workspace=workspace, config=config)
    else:
        runtime = VoidCodeRuntime(workspace=workspace)
    try:
        yield runtime
    finally:
        close_runtime(runtime)


def load_cli_config(
    workspace: Path,
    **kwargs: Unpack[RuntimeConfigKwargs],
) -> RuntimeConfig:
    """Load config at a CLI boundary without leaking parser tracebacks."""
    try:
        return load_runtime_config(workspace, **kwargs)
    except ValueError as exc:
        raise CliError(code=EXIT_CONFIG_ERROR, message=f"invalid runtime config: {exc}") from None


def run_with_inline_approval(
    runtime: VoidCodeRuntime,
    request: RuntimeRequest,
    *,
    interactive: bool,
    emit_events: bool,
    trace_events: bool = False,
    show_thinking: bool = False,
) -> RuntimeStreamResult:
    trace_printer = TracePrinter(show_thinking=show_thinking) if trace_events else None
    result = consume_runtime_stream(
        runtime.run_stream(request),
        emit_events=emit_events,
        trace_printer=trace_printer,
        show_thinking=show_thinking,
        on_interrupt=lambda session_id, run_id: runtime.cancel_session(
            session_id,
            run_id=run_id,
            reason="cli KeyboardInterrupt",
        ),
    )

    while interactive:
        approval_event = pending_approval_event(result.session, last_event(result))
        question_event = pending_question_event(result.session, last_event(result))
        if approval_event is None and question_event is None:
            break
        if approval_event is not None:
            resumed_chunks = runtime.resume_stream(
                session_id=result.session.session.id,
                approval_request_id=approval_request_id(approval_event),
                approval_decision=prompt_for_approval(approval_event),
            )
        else:
            assert question_event is not None
            resumed_chunks = runtime.answer_question_stream(
                result.session.session.id,
                question_request_id=str(question_event.payload["request_id"]),
                responses=prompt_for_question(question_event),
            )
        resumed_result = consume_runtime_stream(
            resumed_chunks,
            emit_events=emit_events,
            trace_printer=trace_printer,
            show_thinking=show_thinking,
            on_interrupt=lambda session_id, run_id: runtime.cancel_session(
                session_id,
                run_id=run_id,
                reason="cli KeyboardInterrupt",
            ),
        )
        result = RuntimeStreamResult(
            output=resumed_result.output,
            session=resumed_result.session,
            events=(*result.events, *resumed_result.events),
        )

    if interactive and (not emit_events or trace_events):
        return result
    if interactive:
        print_runtime_output(result.output)

    return result


def print_live_event(event: EventEnvelope, *, show_thinking: bool) -> None:
    """Print one runtime event to the interactive transcript.

    A restarted provider attempt announces itself first: its predecessor's
    streamed text is already on screen and is not persisted truth.
    """
    notice = discarded_output_notice(event)
    if notice is not None:
        print(notice, flush=True)
    print(
        format_event(
            event.event_type,
            event.source,
            event.payload,
            show_thinking=show_thinking,
        ),
        flush=True,
    )


def consume_runtime_stream(
    chunks: Iterator[RuntimeStreamChunk],
    *,
    emit_events: bool,
    trace_printer: TracePrinter | None = None,
    show_thinking: bool = False,
    on_interrupt: Callable[[str, str | None], object] | None = None,
) -> RuntimeStreamResult:
    output: str | None = None
    final_session: SessionState | None = None
    events: list[EventEnvelope] = []

    try:
        for chunk in chunks:
            final_session = chunk.session
            if chunk.event is not None:
                if emit_events:
                    print_live_event(chunk.event, show_thinking=show_thinking)
                if trace_printer is not None:
                    trace_printer.handle_event(chunk.event)
                events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
    except KeyboardInterrupt:
        if final_session is not None and on_interrupt is not None:
            on_interrupt(
                final_session.session.id,
                run_id_from_session_metadata(final_session.metadata),
            )
        raise

    if final_session is None:
        raise ValueError("runtime stream emitted no chunks")

    return RuntimeStreamResult(output=output, session=final_session, events=tuple(events))


def incomplete_runtime_stream_message(result: RuntimeStreamResult) -> str | None:
    if result.session.status in {"completed", "failed", "waiting"}:
        return None
    if has_permission_denied_tool_result(result.events):
        return None
    if pending_blocked_event(result.session, last_event(result)) is not None:
        return None
    final_event = last_event(result)
    if final_event is not None and final_event.event_type == "runtime.failed":
        return None
    return f"runtime stream ended without a terminal outcome; last session status was {result.session.status}"


def last_event_is_permission_denied_tool_result(event: EventEnvelope | None) -> bool:
    if event is None or event.event_type != "runtime.tool_completed":
        return False
    return event.payload.get("permission_denied") is True


def has_permission_denied_tool_result(events: tuple[EventEnvelope, ...]) -> bool:
    return any(last_event_is_permission_denied_tool_result(event) for event in events)


def run_id_from_session_metadata(metadata: dict[str, object]) -> str | None:
    run_id = runtime_state_run_id(metadata)
    return run_id if run_id else None


def last_event(result: RuntimeStreamResult) -> EventEnvelope | None:
    return result.events[-1] if result.events else None


def pending_approval_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    if session.status != "waiting":
        return None
    if event is None or event.event_type != "runtime.approval_requested":
        return None
    return event


def pending_question_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    if session.status != "waiting":
        return None
    if event is None or event.event_type != "runtime.question_requested":
        return None
    return event


def pending_blocked_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    return pending_approval_event(session, event) or pending_question_event(session, event)


def blocked_exit_code(event: EventEnvelope) -> int:
    if event.event_type == "runtime.approval_requested":
        return EXIT_APPROVAL_REQUIRED
    return EXIT_RUNTIME_ERROR


def approval_request_id(event: EventEnvelope) -> str:
    return str(event.payload["request_id"])


def prompt_for_approval(event: EventEnvelope) -> PermissionResolution:
    tool = safe_detail(event.payload.get("tool"), limit=64)
    target_summary = safe_detail(event.payload.get("target_summary"), limit=160)
    prompt = f"Approve {tool} for {target_summary}? [y/N]: " if target_summary else f"Approve {tool}? [y/N]: "
    sys.stderr.write(prompt)
    sys.stderr.flush()
    response = sys.stdin.readline()
    return "allow" if response.strip().lower() in {"y", "yes"} else "deny"


def prompt_for_question(event: EventEnvelope) -> tuple[QuestionResponse, ...]:
    raw_questions = event.payload.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raw_questions = [{"header": "response", "question": "Answer"}]
    responses: list[QuestionResponse] = []
    for index, raw_question in enumerate(raw_questions, start=1):
        question = raw_question if isinstance(raw_question, dict) else {}
        header = safe_detail(question.get("header") or f"question-{index}", limit=64)
        text = safe_detail(question.get("question") or "Answer", limit=240)
        options = question.get("options")
        option_hint = ""
        if isinstance(options, list) and options:
            labels = [safe_detail(item.get("label"), limit=64) for item in options if isinstance(item, dict) and item.get("label")]
            if labels:
                option_hint = f" [{', '.join(labels)}]"
        sys.stderr.write(f"Question {header}: {text}{option_hint}\n> ")
        sys.stderr.flush()
        responses.append(QuestionResponse(header=header, answers=(sys.stdin.readline().strip(),)))
    return tuple(responses)


def _runtime_response_result(response: object) -> RuntimeStreamResult:
    typed = cast("RuntimeResponseLike", response)
    return RuntimeStreamResult(
        output=typed.output,
        session=typed.session,
        events=cast(tuple[EventEnvelope, ...], typed.events),
    )


def session_result_exit_code(result: RuntimeStreamResult) -> int:
    blocked_event = pending_blocked_event(result.session, last_event(result))
    if blocked_event is not None:
        return blocked_exit_code(blocked_event)
    if any(event.event_type == "runtime.approval_resolved" and event.payload.get("decision") == "deny" for event in result.events):
        return EXIT_APPROVAL_DENIED
    if result.session.status == "failed":
        return EXIT_RUNTIME_ERROR
    if result.session.status == "waiting":
        return EXIT_RUNTIME_ERROR
    return EXIT_SUCCESS


def consume_session_stream(
    chunks: Iterator[RuntimeStreamChunk],
    *,
    fallback: Callable[[], object],
    show_thinking: bool,
    on_interrupt: Callable[[str, str | None], object] | None = None,
) -> RuntimeStreamResult:
    try:
        return consume_runtime_stream(
            chunks,
            emit_events=True,
            show_thinking=show_thinking,
            on_interrupt=on_interrupt,
        )
    except ValueError as exc:
        # Keeps adapters written against the pre-stream runtime contract
        # usable while real runtimes always take the stream path.
        if str(exc) != "runtime stream emitted no chunks":
            raise
        return _runtime_response_result(fallback())


class EventLikeProtocol(Protocol):
    event_type: str
    source: str
    payload: dict[str, object]


class RuntimeResponseLike(Protocol):
    events: tuple[EventLikeProtocol, ...]
    output: str | None

    session: SessionState
