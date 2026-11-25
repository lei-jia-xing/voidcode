"""Governed drive loop: provider dispatch, native batches, approvals, and stops."""

from __future__ import annotations

import time
from uuid import uuid4

from ...sdk.client import BudgetExhaustion
from ...sdk.codec import decode_provider_result, decode_tool_call, encode_provider_result, encode_tool_call
from ...sdk.provider import ProviderExecutionError, ProviderTurnRequest
from ...sdk.tools import ToolFailure
from ..events import EventType
from ..execution import _Execution, wire_object
from ..storage.store import CheckpointKind, ContinuationStatus, EventDraft, RootBudgetExhausted, SessionStatus
from ._provider_turn import TurnStreamMixin
from ._turn_contracts import MODEL_CONFIGURATION_GUIDANCE, CoreResult
from ._turn_policy import TurnPolicyMixin
from .governance import request_window, tool_schema_characters


class TurnDriveMixin(TurnStreamMixin, TurnPolicyMixin):
    """The governed drive loop over provider turns and native tool batches."""

    def drive(self, execution: _Execution) -> CoreResult:
        while True:
            # The process abort stays on self; the live run abort lives in the registry.
            session_abort = self._host.active_abort(execution.session.session_id)
            default_abort = self._host.default_abort
            if (session_abort and session_abort.cancelled) or default_abort.cancelled:
                cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                reason = (session_abort.reason if session_abort and session_abort.cancelled else default_abort.reason) or "cancelled"
                return self.stop(execution, reason, stop_cause=cause)
            snapshot = self._host.snapshot
            cursor = execution.cursor
            if cursor.batch_sequence is None and cursor.deferred_sequence is None:
                request = self.next_deferred(execution)
                if request is not None:
                    cursor.deferred_sequence = request.sequence
                    raw_depth = request.payload.get("depth", 0)
                    cursor.deferred_depth = raw_depth if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) else 0
                    if cursor.deferred_authority_missing:
                        call = decode_tool_call(wire_object(request.payload["call"], "deferred tool call"))
                        self._host.report(execution, call, ToolFailure(call.tool_name, "background tool request lacks captured origin authorization"))
                        continue
                    try:
                        self._host.commit(
                            execution,
                            SessionStatus.RUNNING,
                            CheckpointKind.READY,
                            reserve_root=True,
                        )
                    except RootBudgetExhausted as exc:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=exc.budget,
                            consumed=exc.reserved,
                            remaining=max(0, exc.budget - exc.reserved),
                            continue_with="extend_root_actions",
                        )
                        message = f"root action budget exhausted: consumed {exc.reserved} of {exc.budget}, remaining 0"
                        if request.payload.get("background_action_id") is not None and request.payload.get("result_delivery") == "requester":
                            return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
                        cursor.deferred_sequence = None
                        cursor.deferred_depth = 0
                        return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
            if cursor.batch_sequence is None and cursor.deferred_sequence is None:
                # A staged activation applies only here: the whole previous native batch has been
                # reported and committed, so no result can mix generations, and the request is
                # consumed durably in the same transaction as the switch.
                pending_request = self._host.pending_activation_request(execution)
                if pending_request is not None:
                    outcome = self._host.activate(
                        session_id=execution.session.session_id,
                        source="model",
                        request_id=str(pending_request.get("request_id", "")),
                        target_revision_id=str(pending_request.get("target_revision_id", "")),
                    )
                    if outcome["activated"]:
                        # This run is the automatic continuation the activation implied; the
                        # next commit persists the consumed intent.
                        execution.cursor.resume_intent = ""
                    execution.session = self._host.store_session(execution.session.session_id)
                    execution.events = list(self._host.store_events(execution.session.session_id))
                    cursor = execution.cursor
                    snapshot = self._host.snapshot
                if cursor.deferred_only:
                    self._host.commit(
                        execution,
                        SessionStatus.COMPLETED,
                        CheckpointKind.TERMINAL,
                        EventDraft(
                            EventType.RUN_COMPLETED,
                            "core",
                            "plugin",
                            {"output": None, "logical_turn": cursor.logical_turn, **self._host.measured_run_elapsed_fields(execution)},
                        ),
                    )
                    return self.result(execution)
                if cursor.max_steps is not None and cursor.step >= cursor.max_steps:
                    exhaustion = BudgetExhaustion(
                        dimension="provider_turns",
                        source="max_steps",
                        limit=cursor.max_steps,
                        consumed=cursor.step,
                        remaining=max(0, cursor.max_steps - cursor.step),
                        continue_with="resume_with_max_steps",
                    )
                    message = f"provider turn cap exhausted: consumed {cursor.step} of {cursor.max_steps}, remaining 0"
                    return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)

                budget_stop = self.budget_admission(execution)
                if budget_stop is not None:
                    return budget_stop

                # Safe boundary: every member of the previous native batch is reported and
                # committed, so messages addressed to this running session may join THIS run
                # before the next provider dispatch. Never across an in-flight tool call, an
                # approval checkpoint, or a token stream.
                self._host.merge_child_messages(execution)

                with self._host.dispatch("provider") as dispatch_snapshot:
                    if not execution.lifecycle_started:
                        self._host.lifecycle_events(execution, dispatch_snapshot, "session_start")
                        execution.lifecycle_started = True
                    model = dispatch_snapshot.config.model
                    if model is None:
                        return self.fail(execution, MODEL_CONFIGURATION_GUIDANCE)
                    provider_name, separator, model_name = model.partition("/")
                    if not separator or not model_name or provider_name not in dispatch_snapshot.plugin_runtime.providers:
                        return self.fail(execution, "model must name a configured provider/model")
                    # Settle any earlier dispatch of this run that never completed BEFORE
                    # context assembly: observers see unknown usage exactly once, and any
                    # budget they set (even a zero cap) or cancellation they raise is
                    # already durable when the admission checks below read it.
                    self._host.settle_unsettled_dispatches(execution)
                    try:
                        visible_tools = self._host.visible_tools(execution, dispatch_snapshot)
                        messages = self._host.project_messages(
                            execution,
                            model=model,
                            tool_schema_characters=tool_schema_characters(visible_tools),
                        )
                    except ValueError as exc:
                        return self.fail(execution, str(exc))
                    except (OSError, RuntimeError) as exc:
                        # Critical context/admission failure: the run finishes durably
                        # failed before any intent is committed or provider is billed.
                        return self.fail(execution, f"context assembly failed: {type(exc).__name__}: {exc}")

                    effective_abort = session_abort or default_abort
                    request = ProviderTurnRequest(
                        messages=messages,
                        available_tools=visible_tools,
                        raw_model=model,
                        provider_name=provider_name,
                        model_name=model_name,
                        reasoning_effort=dispatch_snapshot.config.reasoning_effort,
                        abort_signal=effective_abort,
                    )

                    dispatch_id = uuid4().hex
                    start_time_ms = int(time.time() * 1000)
                    # Final admission: context assembly may have taken a long RPC, so
                    # re-check cancellation and budgets immediately before the native
                    # call that reserves the root step and bills the provider.
                    late_abort = self.abort_stop(execution, session_abort)
                    if late_abort is not None:
                        return late_abort
                    late_budget = self.budget_admission(execution)
                    if late_budget is not None:
                        return late_budget
                    # A fresh provider dispatch is a new logical turn. The current
                    # native batch keeps this identity (checkpointed), so approvals,
                    # tool reports, and resumes all attribute to the same turn.
                    cursor.logical_turn = uuid4().hex
                    window = request_window(messages, request.available_tools)
                    allocation = execution.projection_allocation
                    allocation_payload: dict[str, object] | None = None
                    if allocation is not None:
                        allocation_payload = dict(allocation.as_payload())
                        allocation_payload["ineffective_tools"] = execution.allocation_ineffective_tools
                    try:
                        intent_events = self._host.commit(
                            execution,
                            SessionStatus.RUNNING,
                            CheckpointKind.READY,
                            EventDraft(
                                EventType.PROVIDER_INTENT,
                                "core",
                                "core",
                                {
                                    "attempt": cursor.step,
                                    "provider": provider_name,
                                    "model": model_name,
                                    "dispatch_id": dispatch_id,
                                    "start_time_ms": start_time_ms,
                                    "logical_turn": execution.cursor.logical_turn,
                                    # Measured inventory of this exact request: a client
                                    # panel reads facts here instead of estimating tokens.
                                    "request_window": window.as_payload(),
                                    # The sizing decision behind the tool projections above.
                                    "projection_allocation": allocation_payload,
                                },
                            ),
                            reserve_root=True,
                        )
                    except RootBudgetExhausted as exc:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=exc.budget,
                            consumed=exc.reserved,
                            remaining=max(0, exc.budget - exc.reserved),
                            continue_with="extend_root_actions",
                        )
                        message = f"root action budget exhausted: consumed {exc.reserved} of {exc.budget}, remaining 0"
                        return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
                    intent_seq = next(ev.sequence for ev in intent_events if ev.event_type == EventType.PROVIDER_INTENT)

                    t0 = time.monotonic()
                    try:
                        if dispatch_snapshot.plugin_runtime.provider_supports_streaming(provider_name):
                            turn = self.stream_provider_turn(
                                dispatch_snapshot,
                                execution,
                                request,
                                provider_name=provider_name,
                                dispatch_id=dispatch_id,
                                dispatch_sequence=intent_seq,
                            )
                        else:
                            turn = dispatch_snapshot.plugin_runtime.propose_turn(request, provider=provider_name)
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                    except ProviderExecutionError as exc:
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                        self._host.commit(
                            execution,
                            SessionStatus.INTERRUPTED,
                            CheckpointKind.INTERRUPTED,
                            EventDraft(
                                EventType.PROVIDER_INTERRUPTED,
                                "core",
                                "core",
                                {
                                    "error": str(exc),
                                    "unknown_usage": True,
                                    "dispatch_sequence": intent_seq,
                                    "dispatch_id": dispatch_id,
                                    "elapsed_ms": elapsed_ms,
                                    "provider": provider_name,
                                    "model": model_name,
                                },
                            ),
                        )
                        # Only a real cancellation may report a user decision; a provider
                        # failure is a service failure, never a user stop.
                        if effective_abort.cancelled:
                            return self.stop(
                                execution,
                                str(exc),
                                stop_cause=effective_abort.stop_cause or "user_stop",
                            )
                        return self.fail(execution, f"provider failed: {exc}")
                    except Exception as exc:
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                        self._host.commit(
                            execution,
                            SessionStatus.INTERRUPTED,
                            CheckpointKind.INTERRUPTED,
                            EventDraft(
                                EventType.PROVIDER_INTERRUPTED,
                                "core",
                                "core",
                                {
                                    "error": str(exc),
                                    "unknown_usage": True,
                                    "dispatch_sequence": intent_seq,
                                    "dispatch_id": dispatch_id,
                                    "elapsed_ms": elapsed_ms,
                                    "provider": provider_name,
                                    "model": model_name,
                                },
                            ),
                        )
                        return self.fail(execution, f"provider failed: {type(exc).__name__}: {exc}")

                    cursor.step += 1

                    ids = [call.tool_call_id for call in turn.tool_calls]
                    has_duplicate_or_missing_ids = any(not identity for identity in ids) or len(ids) != len(set(ids))
                    invalid_done_reason = turn.done_reason not in {"stop", "tool_calls", "completed"}

                    model_turn_payload: dict[str, object] = {
                        "result": encode_provider_result(turn),
                        "dispatch_sequence": intent_seq,
                        "dispatch_id": dispatch_id,
                        "elapsed_ms": elapsed_ms,
                        "provider": provider_name,
                        "model": model_name,
                    }
                    if effective_abort.cancelled and turn.done_reason == "cancelled":
                        # A provider may acknowledge cancellation with a typed result
                        # instead of raising. Preserve its measured result/usage, then
                        # settle the actual user stop rather than inventing a failure.
                        self._host.commit(
                            execution,
                            SessionStatus.INTERRUPTED,
                            CheckpointKind.INTERRUPTED,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                        )
                        return self.stop(
                            execution,
                            effective_abort.reason or "run cancelled",
                            stop_cause=effective_abort.stop_cause or "user_stop",
                        )

                    if has_duplicate_or_missing_ids:
                        self._host.commit(
                            execution,
                            SessionStatus.FAILED,
                            CheckpointKind.TERMINAL,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status=ContinuationStatus.FAILED,
                        )
                        return self.fail(execution, "provider returned missing/duplicate native call identities")

                    if invalid_done_reason:
                        self._host.commit(
                            execution,
                            SessionStatus.FAILED,
                            CheckpointKind.TERMINAL,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status=ContinuationStatus.FAILED,
                        )
                        return self.fail(execution, f"provider did not complete its turn: {turn.done_reason}")

                    if turn.tool_calls:
                        # A native batch starts at its first member, and the checkpoint
                        # references this exact appended turn in the same transaction:
                        # a crash can never leave a batch event without its checkpoint ref.
                        cursor.next_call = 0
                        self._host.commit(
                            execution,
                            SessionStatus.RUNNING,
                            CheckpointKind.READY,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            checkpoint_bindings=(("batch_sequence", EventType.MODEL_TURN),),
                        )
                    else:
                        self._host.commit(
                            execution,
                            SessionStatus.COMPLETED,
                            CheckpointKind.TERMINAL,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            EventDraft(
                                EventType.RUN_COMPLETED,
                                "core",
                                "core",
                                {"logical_turn": execution.cursor.logical_turn, **self._host.measured_run_elapsed_fields(execution)},
                            ),
                            *self._host.lifecycle_events(execution, dispatch_snapshot, "session_end", status="completed"),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status=ContinuationStatus.COMPLETED,
                        )
                        return self.result(execution, output=turn.output, stop_cause="completed")
                    if default_abort.cancelled or (session_abort and session_abort.cancelled):
                        cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                        reason = (session_abort.reason if session_abort and session_abort.cancelled else default_abort.reason) or "cancelled"
                        return self.stop(execution, reason, stop_cause=cause)

            batch, call = self.pending(execution)
            if cursor.deferred_sequence is None and cursor.intent_sequence is None and cursor.batch_sequence is not None:
                batch_turn = decode_provider_result(wire_object(batch.payload["result"], "model turn"))
                remaining_calls = batch_turn.tool_calls[cursor.next_call :]
                if len(remaining_calls) > 1 and self._can_run_concurrent_readonly(execution, snapshot, remaining_calls):
                    definitions = tuple(snapshot.definition_of(c.tool_name) for c in remaining_calls)
                    assert all(d is not None for d in definitions)
                    stopped = self._host.invoke_readonly_batch(execution, remaining_calls, tuple(d for d in definitions if d is not None))
                    if stopped is not None:
                        return stopped
                    continue

            if cursor.deferred_sequence is not None and batch.payload["expected_revision"] != snapshot.revision_id:
                self._host.report(execution, call, ToolFailure(call.tool_name, "requested tool belongs to a different capability revision"))
                continue
            scope = execution.session.tool_allowlist
            if scope is not None and call.tool_name not in scope:
                self._host.report(execution, call, ToolFailure(call.tool_name, "tool is outside this session's profile scope"))
                continue
            if cursor.deferred_origin_tools is not None and call.tool_name not in cursor.deferred_origin_tools:
                self._host.report(execution, call, ToolFailure(call.tool_name, "tool is outside the captured origin scope"))
                continue
            definition = snapshot.definition_of(call.tool_name)
            if definition is None:
                self._host.report(execution, call, ToolFailure(call.tool_name, "tool is not in the active capability revision"))
                continue
            effective_read_only = self._host.session_read_only(execution.session.session_id) or cursor.deferred_origin_readonly is True
            denial = self._host.denial(call, definition, read_only=effective_read_only)
            if denial is None:
                denial = self._host.gate_denial(execution, snapshot, call, readonly=effective_read_only)
            if denial:
                self._host.report(execution, call, ToolFailure(call.tool_name, denial))
                continue
            if (
                cursor.intent_sequence is None
                and self._host.needs_approval(definition)
                and not self._host.has_approval_policy(execution.session.session_id, call.tool_name, call.arguments)
            ):
                if execution.interaction == "unattended":
                    # D3: an unattended run refuses the approval park at the
                    # park-commit site with a typed failure and continues; the
                    # model sees explicit unsupported, never a hang or a fabricated allow.
                    self._host.report(
                        execution,
                        call,
                        ToolFailure(call.tool_name, f"approval is unsupported while unattended; {call.tool_name} was not executed"),
                    )
                    continue
                payload = {
                    "request_id": uuid4().hex,
                    "batch_sequence": batch.sequence,
                    "call": encode_tool_call(call),
                    "owner": snapshot.owner_of(call.tool_name),
                }
                if call.tool_name in snapshot.native_tools:
                    # Pin the exact candidate this approval is about, before the user is asked:
                    # admission imports nothing, and after approval the already-pinned artifact
                    # is activated, so editing the workspace source in between cannot change it.
                    target_revision_id, _admission = self._host.prepare_activation_target()
                    payload["target_revision_id"] = target_revision_id
                    payload["activation_pending"] = True
                self._host.commit(
                    execution,
                    SessionStatus.WAITING,
                    CheckpointKind.APPROVAL_WAIT,
                    EventDraft(EventType.APPROVAL_REQUESTED, "core", "plugin" if cursor.deferred_sequence is not None else "model", payload),
                    checkpoint_bindings=(("approval_sequence", EventType.APPROVAL_REQUESTED),),
                )
                return self.result(execution, pending_approval=payload)
            stopped = self._host.invoke(execution, call, definition)
            if stopped is not None:
                return stopped
