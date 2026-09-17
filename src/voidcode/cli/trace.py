"""``voidcode run --trace`` streaming presentation.

Rendering rules (aligned with the OMP presentation conventions):

- one block per tool call: a title row, an indented argument summary, the tool's
  own output rows behind a vertical gutter, then one terminal state row;
- ``runtime.tool_started`` renders nothing: it is the same call rendered again,
  so a call produces exactly one call row and one state row (OMP renders a call
  through ``renderCall``/``renderResult`` with ``isPartial``, not twice);
- stdout rows use the ``│`` gutter, stderr rows the ``┃`` gutter, because a
  piped transcript carries no colour to distinguish them (OMP tints tool output
  with ``toolOutput``/``error`` instead);
- reasoning text is labelled ``[thinking]`` and only rendered when the caller
  asks for it (OMP gates thinking behind ``--hide-thinking``/``--print-thoughts``).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TypeGuard

from ..cli_support import RuntimeStreamResult
from ..runtime.events import EventEnvelope, redact_reasoning_payload

_PROVIDER_RESTART_EVENTS = frozenset({"runtime.provider_transient_retry", "runtime.provider_fallback"})
_PROVIDER_FALLBACK_EVENT = "runtime.provider_fallback"


def discarded_output_notice(event: EventEnvelope) -> str | None:
    """The notice for a restarted provider attempt whose live output was discarded.

    A failed attempt streams its text before anyone knows it will fail, so the
    runtime marks the retry/fallback that follows with
    ``discarded_streamed_output``. Without a notice the restarted attempt's text
    would silently continue the abandoned text and read as one duplicated reply.
    """
    if event.event_type not in _PROVIDER_RESTART_EVENTS or event.payload.get("discarded_streamed_output") is not True:
        return None
    if event.event_type == _PROVIDER_FALLBACK_EVENT:
        target = _trace_string(event.payload.get("to_provider")) or "the fallback model"
        return f"↻ Provider fallback to {target}: the failed attempt's partial output was discarded; restarting the turn."
    return "↻ Provider retry: the failed attempt's partial output was discarded; restarting the turn."


class TracePrinter:
    def __init__(self, *, show_thinking: bool = False) -> None:
        self._show_thinking = show_thinking
        self._model_text_open = False
        self._reasoning_open = False

    def handle_event(self, event: EventEnvelope) -> None:
        payload = redact_reasoning_payload(
            event.event_type,
            event.payload,
            show_thinking=self._show_thinking,
        )
        notice = discarded_output_notice(event)
        if notice is not None:
            self._close_open_streams()
            print(f"\n{notice}", flush=True)
            return
        if event.event_type == "graph.model_turn":
            self._close_open_streams()
            provider = _trace_string(payload.get("provider"))
            model = _trace_string(payload.get("model"))
            turn = payload.get("turn")
            label = " · ".join(part for part in (provider, model) if part)
            if label:
                print(f"\n● Model turn {turn}: {label}", flush=True)
            else:
                print(f"\n● Model turn {turn}", flush=True)
            return
        if event.event_type == "graph.provider_stream":
            channel = _trace_string(payload.get("channel"))
            if channel == "text" and payload.get("kind") in {
                "delta",
                "content",
            }:
                text = _trace_string(payload.get("text"))
                if text:
                    self._close_reasoning_text()
                    print(text, end="", flush=True)
                    self._model_text_open = True
            elif channel == "reasoning" and payload.get("kind") in {"delta", "content"}:
                text = _trace_string(payload.get("text"))
                if text:
                    self._print_reasoning_text(text)
            return
        if event.event_type == "runtime.reasoning_part":
            text = _trace_string(payload.get("text"))
            if text:
                self._print_reasoning_text(text)
            return
        if event.event_type == "graph.tool_request_created":
            self._close_open_streams()
            _print_trace_tool_request(payload)
            return
        if event.event_type == "runtime.tool_progress":
            self._close_open_streams()
            _print_trace_tool_progress(payload)
            return
        if event.event_type == "runtime.tool_completed":
            self._close_open_streams()
            _print_trace_tool_completed(payload)
            return
        if event.event_type == "runtime.todo_updated":
            self._close_open_streams()
            _print_trace_todos(payload)
            return
        if event.event_type == "runtime.approval_requested":
            self._close_open_streams()
            tool = _trace_string(payload.get("tool")) or "tool"
            target = _trace_string(payload.get("target_summary"))
            suffix = f" for {target}" if target else ""
            print(f"\n⚠ Approval required: {tool}{suffix}", flush=True)
            return
        if event.event_type == "runtime.question_requested":
            self._close_open_streams()
            count = payload.get("question_count")
            print(f"\n? Question required: {count or 1} prompt(s)", flush=True)
            return
        if event.event_type == "runtime.failed":
            self._close_open_streams()
            error = _trace_string(payload.get("error")) or "runtime failed"
            print(f"\n✖ Failed: {error}", flush=True)

    def _close_open_streams(self) -> None:
        self._close_model_text()
        self._close_reasoning_text()

    def _close_model_text(self) -> None:
        if self._model_text_open:
            print(flush=True)
            self._model_text_open = False

    def _close_reasoning_text(self) -> None:
        if self._reasoning_open:
            print(flush=True)
            self._reasoning_open = False

    def _print_reasoning_text(self, text: str) -> None:
        self._close_model_text()
        if not self._reasoning_open:
            print("[thinking] ", end="", flush=True)
            self._reasoning_open = True
        print(text, end="", flush=True)


def _print_trace_tool_request(payload: dict[str, object]) -> None:
    tool = _trace_string(payload.get("tool")) or "tool"
    arguments = payload.get("arguments")
    print(f"\n▸ Tool call: {tool}", flush=True)
    if tool == "shell_exec" and _is_string_keyed_mapping(arguments):
        command = _trace_string(arguments.get("command"))
        if command:
            print(f"  $ {command}", flush=True)
            return
    summary = _trace_tool_summary(payload)
    if summary:
        print(f"  {summary}", flush=True)


def _print_trace_tool_progress(payload: dict[str, object]) -> None:
    chunk = _trace_string(payload.get("chunk"))
    if not chunk:
        return
    stream = _trace_string(payload.get("stream")) or "output"
    for line in chunk.rstrip("\n").splitlines() or [""]:
        prefix = "│" if stream == "stdout" else "┃"
        print(f"  {prefix} {line}", flush=True)


def _print_trace_tool_completed(payload: dict[str, object]) -> None:
    tool = _trace_string(payload.get("tool")) or "tool"
    status = _trace_string(payload.get("status")) or "done"
    error = _trace_string(payload.get("error"))
    marker = "✓" if status == "ok" and not error else "✖"
    print(f"  {marker} {tool} {status}", flush=True)
    if error:
        print(f"    {error}", flush=True)


def _print_trace_todos(payload: dict[str, object]) -> None:
    phases = payload.get("phases")
    if not isinstance(phases, list):
        return
    print("\nTODO", flush=True)
    for raw_phase in phases:
        if not _is_string_keyed_mapping(raw_phase):
            continue
        phase_name = _trace_string(raw_phase.get("name")) or "Tasks"
        print(f"  {phase_name}", flush=True)
        tasks = raw_phase.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not _is_string_keyed_mapping(task):
                continue
            status = _trace_string(task.get("status")) or "pending"
            marker = {"completed": "x", "abandoned": "-", "blocked": "!"}.get(status, " ")
            content = _trace_string(task.get("content")) or "(empty todo)"
            reason = _trace_string(task.get("blocker"))
            suffix = f" — {reason}" if reason else ""
            print(f"    [{marker}] {content}{suffix}", flush=True)


def print_trace_final(result: RuntimeStreamResult) -> None:
    print(f"Session id: {result.session.session.id}", flush=True)
    if result.output is None:
        return
    print("\nResult", flush=True)
    print(result.output, end="", flush=True)
    if not result.output.endswith("\n"):
        print(flush=True)


def print_trace_blocked(
    result: RuntimeStreamResult,
    event: EventEnvelope,
    *,
    workspace: Path,
) -> None:
    workspace_arg = f"--workspace {shlex.quote(str(workspace))}"
    if event.event_type == "runtime.approval_requested":
        print(
            "Resume approval: "
            f"voidcode sessions resume {result.session.session.id} {workspace_arg} "
            f"--approval-request-id {event.payload['request_id']} --approval-decision allow",
            flush=True,
        )
        return
    request_id = _trace_string(event.payload.get("request_id")) or "<request-id>"
    print(
        "Answer question: "
        f"voidcode sessions answer {result.session.session.id} {workspace_arg} "
        f"--question-request-id {request_id} --response <answer>",
        flush=True,
    )


def _trace_tool_summary(payload: dict[str, object]) -> str | None:
    display = payload.get("display")
    if _is_string_keyed_mapping(display):
        summary = _trace_string(display.get("summary"))
        if summary:
            return summary
    path = _trace_string(payload.get("path"))
    if path:
        return path
    arguments = payload.get("arguments")
    if _is_string_keyed_mapping(arguments):
        for key in ("path", "pattern", "query", "url", "description"):
            value = _trace_string(arguments.get(key))
            if value:
                return value
    return None


def _is_string_keyed_mapping(value: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    return all(isinstance(key, str) for key in value)


def _trace_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
