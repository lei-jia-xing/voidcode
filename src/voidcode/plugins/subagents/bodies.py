"""Typed tool bodies and the shared failure builders for the subagent surfaces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from voidcode.sdk.tools import TextOutput, ToolFailure, ToolResult, UnconfirmedStop

from .args import TaskArgsModel
from .uris import _agent_uri, _transcript_uri


@dataclass(frozen=True, slots=True)
class WaitBody:
    """Freshly delivered child outcomes plus the selected children still pending."""

    children: tuple[Mapping[str, object], ...] = ()
    pending: tuple[Mapping[str, object], ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "operation": "wait",
            "children": [dict(child) for child in self.children],
            "pending": [dict(child) for child in self.pending],
        }


@dataclass(frozen=True, slots=True)
class TaskBody:
    """One ``task`` outcome: a spawn, a join, a message receipt, a cancellation, or history."""

    operation: str
    task_id: str | None = None
    label: str | None = None
    status: str | None = None
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None
    budget_exhaustion: Mapping[str, object] | None = None
    cancelled: bool | None = None
    subagent_type: str | None = None
    #: Send receipt facts (never a completion claim).
    delivery: str | None = None
    child_status: str | None = None
    run_id: str | None = None
    message_id: str | None = None
    #: Output/result references. ``output_uri`` is the immutable full output artifact.
    output_uri: str | None = None
    output_offset: int | None = None
    output_bytes: int | None = None
    next_output_offset: int | None = None
    output_preview: str | None = None
    output_error: str | None = None
    history_error: str | None = None
    #: Validated structured delivery (exactly one of data/parse_error) when a schema was
    #: declared and the run completed with full text captured; absent means undeclared,
    #: undelivered, or uncaptured — never valid, never a guess.
    data: Mapping[str, object] | None = None
    parse_error: Mapping[str, object] | None = None
    #: Committed cause when a declared schema could not be checked against full output
    #: (no captured text, or the text exceeded the structured-read byte cap).
    structured_unavailable: str | None = None
    #: Current status/latest published output; distinct from the transcript.
    agent_uri: str | None = None
    #: Live transcript and immutable saved-history references.
    history_uri: str | None = None
    history_sha256: str | None = None
    #: History page cursor.
    after_sequence: int | None = None
    next_after_sequence: int | None = None
    next_event_offset: int | None = None
    truncated: bool | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"operation": self.operation}
        for key in (
            "task_id",
            "status",
            "output",
            "error",
            "stop_cause",
            "budget_exhaustion",
            "cancelled",
            "subagent_type",
            "delivery",
            "child_status",
            "run_id",
            "message_id",
            "output_uri",
            "output_offset",
            "output_bytes",
            "next_output_offset",
            "output_preview",
            "output_error",
            "history_error",
            "data",
            "parse_error",
            "structured_unavailable",
            "agent_uri",
            "history_uri",
            "history_sha256",
            "after_sequence",
            "next_after_sequence",
            "next_event_offset",
            "truncated",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.label:
            payload["label"] = self.label
        return payload


@dataclass(frozen=True, slots=True)
class _BatchChild:
    """One spawned batch item and the agent definition it used."""

    index: int
    task_id: str
    subagent_type: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"index": self.index, "task_id": self.task_id, "agent_uri": _agent_uri(self.task_id)}
        if self.subagent_type is not None:
            payload["subagent_type"] = self.subagent_type
        return payload


@dataclass(frozen=True, slots=True)
class BatchBody:
    """One ``task_batch`` outcome: a group spawn (possibly partial) or a group read."""

    operation: str
    parallel_group_id: str
    task_ids: tuple[str, ...] = ()
    partial: bool = False
    created: tuple[dict[str, object], ...] = ()
    failed: tuple[dict[str, object], ...] = ()
    children: tuple[dict[str, object], ...] = ()
    pending: tuple[dict[str, object], ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"operation": self.operation, "parallel_group_id": self.parallel_group_id}
        if self.operation == "spawn":
            payload["task_ids"] = list(self.task_ids)
            payload["partial"] = self.partial
            payload["created"] = [dict(entry) for entry in self.created]
            if self.partial:
                payload["failed"] = [dict(entry) for entry in self.failed]
        else:
            payload["children"] = [dict(entry) for entry in self.children]
            payload["pending"] = [dict(entry) for entry in self.pending]
        return payload


def _failure(tool_name: str, message: str) -> ToolResult:
    return ToolFailure(tool_name=tool_name, error=message, output=TextOutput(text=message))


def _stopped_failure(tool_name: str, message: str, *, body: TaskBody | BatchBody | None = None) -> ToolFailure:
    """A parent-stop failure that keeps created-child facts visible and commits staged work."""
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(text=message),
        body=body,
        execution=UnconfirmedStop(cancellation_signalled=True),
    )


def _spawn_body(
    args: TaskArgsModel,
    child_id: str | None,
    *,
    status: str | None = None,
    run_id: str | None = None,
    error: str | None = None,
) -> TaskBody:
    """The spawn fact body; a created id survives even when the call is cancelled."""
    return TaskBody(
        operation="spawn",
        task_id=child_id,
        label=args.label,
        status=status,
        subagent_type=args.subagent_type,
        run_id=run_id,
        error=error,
        agent_uri=None if child_id is None else _agent_uri(child_id),
        history_uri=None if child_id is None else _transcript_uri(child_id),
    )


def _batch_body(
    group_id: str,
    created: Sequence[_BatchChild],
    failed: Sequence[tuple[int, str]],
    *,
    partial: bool = True,
) -> BatchBody:
    """The group spawn body; created ids survive a partial or cancelled call."""
    return BatchBody(
        operation="spawn",
        parallel_group_id=group_id,
        task_ids=tuple(entry.task_id for entry in created),
        partial=partial or bool(failed),
        created=tuple(entry.as_payload() for entry in created),
        failed=tuple({"index": index, "error": message} for index, message in failed),
    )
