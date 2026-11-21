"""Provider-context projection over an execution's committed event prefix.

This module owns how already-committed facts become one provider request:
history replay, the verified window-summary prefix, deferred tool results,
plugin-contributed context, the activation outcome note, ephemeral steering,
and the shrink-only adaptive re-projection of tool output. It never commits
facts and never mutates durable state; the execution projection it is handed is
caller-owned scratch.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Protocol

from ..sdk.codec import decode_provider_result, decode_tool_result, encode_tool_call
from ..sdk.json_values import json_wire_object
from ..sdk.provider import ImageAttachment, ProviderMessage
from ..sdk.services import ArtifactPage
from ..sdk.tools import TOOL_OUTPUT_BYTES, ToolCall, ToolFailure, ToolResult
from .coverage import allocate_tool_text_budget, measured_tokens_per_character, project_tool_result
from .events import EventType
from .execution import _Execution, committed_event, wire_object
from .plugins import PluginRuntime
from .storage.store import SqliteStore


@dataclass(slots=True)
class _ShrinkableToolProjection:
    """One ceiling-rendered tool message that adaptive sizing may re-project smaller."""

    index: int
    result: ToolResult
    label: str | None


class ProjectionSnapshot(Protocol):
    """The capability generation visible while context is assembled."""

    @property
    def plugin_runtime(self) -> PluginRuntime: ...


class ContextProjectionHost(Protocol):
    """The core capabilities context projection may use, stated explicitly."""

    @property
    def store(self) -> SqliteStore: ...

    def dispatch(self, description: str) -> AbstractContextManager[ProjectionSnapshot]: ...

    def artifact_page(self, digest: str, offset: int, limit: int) -> ArtifactPage: ...


class ContextProjector:
    def __init__(self, host: ContextProjectionHost) -> None:
        self._host = host

    def messages(
        self,
        execution: _Execution,
        *,
        allow_pending: bool = False,
        completed_only: bool = False,
        model: str | None = None,
        tool_schema_characters: int = 0,
    ) -> tuple[ProviderMessage, ...]:
        messages: list[ProviderMessage] = []
        pending_calls: tuple[ToolCall, ...] = ()
        pending_index = 0
        batch_start = 0
        batch_sequence: int | None = None
        shrinkable: list[_ShrinkableToolProjection] = []
        execution.projection_allocation = None
        execution.allocation_ineffective_tools = 0
        # A verified projection replaces a committed prefix with its owner's summary, so a long
        # session keeps a bounded window without rewriting a single historical event.
        projection = self._host.store.window_projection(execution.session.session_id)
        projected_through = 0
        if projection is not None:
            projected_through = projection.covered_to_sequence
            messages.append(
                ProviderMessage(
                    "system",
                    f"[window summary by {projection.owner}; {projection.covered_to_sequence} covered events "
                    f"({projection.covered_bytes} bytes -> {projection.dropped_bytes} dropped)]\n{projection.replacement}",
                )
            )
        for event in execution.events:
            if event.sequence <= projected_through:
                continue
            if event.event_type == EventType.USER_MESSAGE:
                if pending_calls:
                    raise ValueError("user message crossed an unfinished native batch")
                raw_images = event.payload.get("images")
                imgs = tuple(ImageAttachment.from_payload(item) for item in raw_images) if isinstance(raw_images, (list, tuple)) else ()
                messages.append(ProviderMessage("user", str(event.payload["content"]), images=imgs))
            elif event.event_type == EventType.CONTINUATION_STARTED:
                if pending_calls:
                    raise ValueError("continuation crossed an unfinished native batch")
                p_payload = event.payload.get("payload")
                p_map = wire_object(p_payload, "continuation payload")
                payload_str = json.dumps(json_wire_object(p_map), ensure_ascii=False)
                if event.payload.get("role") == "user":
                    # The owner declared a user-role message: its text (or payload) is verbatim.
                    text = p_map.get("text")
                    if text is None and "prompt" in p_map:
                        text = p_map.get("prompt")
                    raw_images = p_map.get("images")
                    imgs = tuple(ImageAttachment.from_payload(item) for item in raw_images) if isinstance(raw_images, (list, tuple)) else ()
                    messages.append(ProviderMessage("user", text if isinstance(text, str) else payload_str, images=imgs))
                else:
                    messages.append(ProviderMessage("system", f"[Durable continuation from {event.source}]\n{payload_str}"))
            elif event.event_type == EventType.WAKEUP_FIRED:
                if pending_calls:
                    raise ValueError("wakeup crossed an unfinished native batch")
                w_payload = event.payload.get("payload")
                w_map = wire_object(w_payload, "wakeup payload")
                payload_str = json.dumps(json_wire_object(w_map), ensure_ascii=False)
                if event.payload.get("role") == "user":
                    text = w_map.get("text")
                    messages.append(ProviderMessage("user", text if isinstance(text, str) else payload_str))
                else:
                    owner_label = str(event.payload.get("owner", event.source))
                    messages.append(ProviderMessage("system", f"[Durable wakeup from {owner_label}]\n{payload_str}"))
            elif event.event_type == EventType.MODEL_TURN:
                if pending_calls:
                    raise ValueError("provider turn crossed an unfinished native batch")
                turn = decode_provider_result(wire_object(event.payload["result"], "model turn"))
                batch_start = len(messages)
                messages.append(
                    ProviderMessage("assistant", turn.output, reasoning=turn.reasoning, tool_calls=turn.tool_calls, metadata=turn.metadata or {})
                )
                if turn.tool_calls:
                    pending_calls = turn.tool_calls
                    pending_index = 0
                    batch_sequence = event.sequence
            elif event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED}:
                if event.payload.get("deferred") is True:
                    deferred_sequence = event.payload["batch_sequence"]
                    if type(deferred_sequence) is not int:
                        raise ValueError("deferred tool result has no batch sequence")
                    request = committed_event(execution, deferred_sequence, EventType.TOOL_DEFERRED)
                    if event.payload["call"] != request.payload["call"]:
                        raise ValueError("deferred tool result does not match its committed request")
                    if request.payload.get("result_delivery") == "requester":
                        continue
                    result = decode_tool_result(wire_object(event.payload["result"], "tool result"))
                    text, images, _clipped = project_tool_result(result, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=self._host.artifact_page)
                    label = (
                        f"[Untrusted deferred tool result: {result.tool_name}; "
                        f"invocation {event.payload.get('invocation_id', 'not-dispatched')}; "
                        f"request {request.payload['request_id']}; caller {request.payload['caller_invocation_id']}]\n"
                    )
                    messages.append(ProviderMessage("user", label + (text or ""), images=images))
                    shrinkable.append(_ShrinkableToolProjection(len(messages) - 1, result, label))
                    continue
                if not pending_calls or event.payload["batch_sequence"] != batch_sequence:
                    raise ValueError("reported tool result crossed an unfinished native batch")
                call = pending_calls[pending_index]
                if wire_object(event.payload["call"], "tool call") != json_wire_object(encode_tool_call(call)):
                    raise ValueError("reported tool result does not match the active batch call")
                result = decode_tool_result(wire_object(event.payload["result"], "tool result"))
                text, images, _clipped = project_tool_result(result, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=self._host.artifact_page)
                messages.append(
                    ProviderMessage(
                        "tool",
                        text,
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        is_error=isinstance(result, ToolFailure),
                        images=images,
                    )
                )
                shrinkable.append(_ShrinkableToolProjection(len(messages) - 1, result, None))
                pending_index += 1
                if pending_index == len(pending_calls):
                    pending_calls = ()
        if pending_calls and completed_only:
            del messages[batch_start:]
            shrinkable = [entry for entry in shrinkable if entry.index < len(messages)]
        elif pending_calls:
            if not allow_pending:
                raise ValueError("cannot call the provider before completing the pending native batch")
            if execution.cursor.batch_sequence != batch_sequence or execution.cursor.next_call != pending_index:
                raise ValueError("checkpoint is not the authentic completed batch prefix")
        elif execution.cursor.batch_sequence is not None or execution.cursor.next_call != 0:
            raise ValueError("checkpoint references an already completed batch")

        # Contributed context from plugins only when not allow_pending. A context
        # failure is never suppressed or re-wrapped here: the drive boundary
        # finishes the run failed before any provider charge.
        if not allow_pending:
            with self._host.dispatch("context") as snapshot:
                contributions = snapshot.plugin_runtime.contribute_context(
                    execution.session.session_id,
                    run_id=execution.run_id,
                    logical_turn=execution.cursor.logical_turn,
                )
                for c in contributions:
                    if c.get("type") != "text":
                        continue
                    owner = str(c.get("owner", "plugin"))
                    content = str(c.get("content", ""))
                    if c.get("role") == "user":
                        # Declarative role: verbatim, never re-wrapped as core system prose.
                        messages.append(ProviderMessage("user", content))
                    else:
                        messages.append(ProviderMessage("system", f"[{owner} context]\n{content}"))

            activation_note = self._activation_outcome_note(execution)
            if activation_note is not None:
                messages.append(ProviderMessage("system", activation_note))
            if execution.ephemeral_steering:
                messages.append(ProviderMessage("system", "[Tool Guidance]\n" + "\n".join(execution.ephemeral_steering)))
                execution.ephemeral_steering.clear()

        if model is not None and not allow_pending:
            self._apply_projection_allocation(execution, messages, shrinkable, model=model, tool_schema_characters=tool_schema_characters)

        return tuple(messages)

    def _apply_projection_allocation(
        self,
        execution: _Execution,
        messages: list[ProviderMessage],
        shrinkable: list[_ShrinkableToolProjection],
        *,
        model: str,
        tool_schema_characters: int,
    ) -> None:
        """Shrink ceiling-rendered tool projections to the active model's remaining headroom.

        Phase one above rendered every tool result at the fixed ``TOOL_OUTPUT_BYTES``
        host ceiling; this phase measures that window, pairs the session's own
        committed token rate for the active model, and re-projects smaller only
        when every input is known. Any unknown keeps the ceiling bytes exactly,
        and a per-tool re-projection that cannot fit its identities keeps its own
        ceiling rendering: adaptive sizing never refuses a dispatch.
        """
        provider_name, separator, model_name = model.partition("/")
        limits = None
        rate: float | None = None
        if separator and model_name:
            with self._host.dispatch("context") as snapshot:
                limits = snapshot.plugin_runtime.provider_model_limits(provider_name, model_name)
            rate = measured_tokens_per_character(execution.events, provider_name=provider_name, model_name=model_name)
        shrinkable_characters = 0
        for entry in shrinkable:
            shrinkable_characters += len(messages[entry.index].content or "") - len(entry.label or "")
        fixed_characters = sum(len(message.content or "") + len(message.reasoning or "") for message in messages) - shrinkable_characters
        allocation = allocate_tool_text_budget(
            tool_count=len(shrinkable),
            fixed_characters=fixed_characters,
            tool_schema_characters=tool_schema_characters,
            images_present=any(message.images for message in messages),
            limits=limits,
            rate_tokens_per_char=rate,
            ceiling_bytes=TOOL_OUTPUT_BYTES,
        )
        execution.projection_allocation = allocation
        budget = allocation.per_tool_max_bytes
        if allocation.policy != "adaptive" or budget is None:
            return
        ineffective = 0
        for entry in shrinkable:
            try:
                text, images, _clipped = project_tool_result(entry.result, max_bytes=budget, artifact_reader=self._host.artifact_page)
            except ValueError, OSError, RuntimeError:
                # The ceiling rendering in hand is authoritative; a budget that
                # cannot fit this tool's identities keeps it instead of failing.
                ineffective += 1
                continue
            current = messages[entry.index]
            messages[entry.index] = replace(current, content=(entry.label or "") + text, images=images)
        execution.allocation_ineffective_tools = ineffective

    def _activation_outcome_note(self, execution: _Execution) -> str | None:
        """Tell the next prompt what happened to a staged activation this run asked for."""
        request_id: str | None = None
        outcome: tuple[str, str] | None = None
        for event in execution.events:
            payload = event.payload
            candidate = payload.get("request_id")
            if not isinstance(candidate, str) or not candidate:
                continue
            if event.run_id != execution.run_id:
                # Only the run that asked for the switch is told about it; later turns are not
                # re-fed an old decision as if it were news.
                continue
            if event.event_type == EventType.ACTIVATION_REQUESTED:
                request_id = candidate
                outcome = None
            elif event.event_type == EventType.ACTIVATION_COMMITTED and candidate == request_id:
                outcome = ("committed", str(payload.get("to_revision_id", "")))
            elif outcome is None and candidate == request_id and event.event_type == EventType.ACTIVATION_FAILED:
                outcome = ("refused", str(payload.get("error", "")))
        if request_id is None or outcome is None:
            return None
        kind, detail = outcome
        if kind == "committed":
            return f"[core] capability activation {request_id} committed the revision {detail}."
        return f"[core] capability activation {request_id} was refused and the previous revision is still active: {detail}"
