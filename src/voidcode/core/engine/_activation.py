"""Capability activation: candidate assembly, dispatch fence, and publication.

The lifecycle base owns state and shutdown; this mixin owns the generation swap:
it builds a candidate runtime, waits out cooperative drivers, replaces the
snapshot pointer, replays deferred facts, and retires the old runtime.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence

from ...sdk.codec import encode_tool_call
from ...sdk.json_values import json_wire_object
from ...sdk.tools import ToolCall
from ..activation import Admission
from ..activation_coordinator import ActivationFenced, ActivationSnapshot, PreparedActivation
from ..events import EventType
from ..execution import _Cursor, _Execution
from ..plugins import PluginRuntime, PluginSource
from ..sandbox import SandboxBackend
from ..storage.store import CheckpointKind, EventDraft, StoreConflict
from ._capability import _Snapshot
from ._lifecycle import CoreLifecycle

logger = logging.getLogger(__name__)

#: Bounded window for cooperative drivers to release dispatch slots before an activation
#: fence refuses. Long model turns and tools are already refused by the activation blockers.
_ACTIVATION_DRAIN_SECONDS = 2.0


class CoreActivation(CoreLifecycle):
    """Build, fence, publish, and retire capability generations."""

    def activate(self, *, session_id: str, source: str, request_id: str = "", target_revision_id: str = "") -> dict[str, object]:
        """Run one capability activation for an already-safe boundary; refusals stay durable outcomes."""
        return self._activation.activate(
            session_id=session_id,
            source=source,
            request_id=request_id,
            target_revision_id=target_revision_id,
        )

    def begin_activation(self) -> None:
        """Enter the dispatch fence after bounded cooperation from long-lived drivers.

        Short dispatches (model turns, tools, commands) are waited out for the drain
        window so a switch is not refused by a momentarily busy callback; cooperative
        background drivers observe `activation_requested` and stop starting dispatches.
        A driver that never yields still refuses the activation after the window.
        """
        thread_id = threading.get_ident()
        deadline = time.monotonic() + _ACTIVATION_DRAIN_SECONDS
        while True:
            with self._calls_lock:
                if self._activating_thread is not None:
                    raise ActivationFenced("another capability activation is already in progress")
                if not self._active_calls:
                    self._activating_thread = thread_id
                    return
                busy = ", ".join(sorted(self._active_calls.values()))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ActivationFenced(f"capability activation is blocked by in-flight work: {busy}")
                self._calls_lock.wait(timeout=min(remaining, 0.05))

    def end_activation(self) -> None:
        self._clear_fence()

    def park_background_drivers(self, reason: str) -> None:
        self._background.park(reason)

    def flush_old_generation_facts(self, deferred_to_candidate: frozenset[str]) -> None:
        self._fact_delivery.replay_pending(require_complete=True, defer_to_candidate=deferred_to_candidate)

    def _build_runtime(self, sources: tuple[PluginSource, ...], *, revision_id: str, sandbox_backend: SandboxBackend | None) -> PluginRuntime:
        """Assemble one runtime generation with every host callback bound to this core."""
        return PluginRuntime(
            sources,
            workspace=self.workspace,
            revision_id=revision_id,
            store=self.store,
            usage_callback=self._get_session_usage,
            budget_callback=self._get_owner_budget,
            spawn_child_callback=self._spawn_child_callback,
            find_child_callback=self._delegation.find_keyed_child,
            join_child_callback=lambda parent, child, timeout: self.join_child(parent, child, timeout_seconds=timeout),
            wait_children_callback=lambda _owner, parent, children, guard: self._delegation.wait_children(parent, children, admission_guard=guard),
            acknowledge_child_completion_callback=lambda _owner, parent, child, run: self._delegation.acknowledge_child_completion(
                parent, child, run
            ),
            send_child_callback=lambda parent, child, prompt, label, notify, guard: self.send_child(
                parent,
                child,
                prompt,
                label=label,
                notify_on_completion=notify,
                admission_guard=guard,
            ),
            cancel_child_callback=self.cancel_child,
            list_descendants_callback=self.list_descendants,
            read_child_snapshot_callback=self._delegation.read_child_snapshot,
            cancel_subtree_callback=lambda bound, target, run, reason: self.cancel_descendant_subtree(
                bound, target, expected_run_id=run, reason=reason
            ),
            cancel_background_action_callback=self.cancel_background_action,
            model_query_callback=self.governed_model_query,
            model_query_structured_callback=self.governed_model_query_structured,
            progress_callback=self._report_progress,
            tool_definition_callback=self._governance.visible_tool_definition,
            session_read_only_callback=self.session_read_only,
            artifacts_dir=self._state_dir / "artifacts",
            sandbox_backend=sandbox_backend,
            process_host=self._process_host,
        )

    def build_candidate_runtime(self, admission: Admission, target_revision_id: str) -> PluginRuntime:
        """Assemble one candidate generation with every host callback bound to this core."""
        return self._build_runtime(
            admission.plugins,
            revision_id=target_revision_id,
            sandbox_backend=self._activation.candidate_sandbox_backend(admission),
        )

    def publish_activation(self, snapshot: ActivationSnapshot, prepared: PreparedActivation) -> None:
        """Swap the generation pointer, release the fence, and retire the old runtime."""
        admission = prepared.admission
        with self._calls_lock:
            self._snapshot = _Snapshot.build(
                revision_id=admission.revision_id,
                config=admission.config,
                admission=admission,
                plugin_runtime=prepared.plugin_runtime,
                with_reload=self._admits is not None,
            )
            self._activating_thread = None
            self._calls_lock.notify_all()
        # The revision is durable and published: nothing below may escape, and no
        # failure may leave `_snapshot` pointing at a runtime this method closes.
        try:
            self._wire_background_runtime(prepared.plugin_runtime)
            self._background.notify()
        except Exception:
            logger.exception("activation %s: background runtime wiring failed after publication", admission.revision_id)
        # Deliver facts deferred to owners the candidate now registers, under the new runtime.
        try:
            self._fact_delivery.replay_pending()
        except Exception:
            logger.exception("activation %s: deferred fact replay failed after publication", admission.revision_id)
        try:
            snapshot.plugin_runtime.close()
        except Exception:
            logger.exception("activation %s: old runtime close failed after publication", admission.revision_id)

    def activation_checkpoint(self, payload: Mapping[str, object] | None, /, *, source: str, request_id: str) -> Mapping[str, object]:
        """Carry the session's own cursor across the switch, plus a real resume intent where owed."""
        cursor = _Cursor()
        if payload is not None:
            cursor = _Cursor.model_validate(json_wire_object(payload))
        if source == "model":
            cursor.resume_intent = request_id
        return json_wire_object(cursor.model_dump())

    def _bound_target(self, execution: _Execution, call: ToolCall) -> str | None:
        """The pinned candidate an approval was bound to, if one exists for this exact native call."""
        encoded = encode_tool_call(call)
        target: str | None = None
        for event in execution.events:
            if event.event_type != EventType.APPROVAL_REQUESTED:
                continue
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            bound = payload.get("target_revision_id")
            bound_call = payload.get("call")
            if isinstance(bound, str) and isinstance(bound_call, Mapping) and json_wire_object(bound_call) == encoded:
                target = bound
        return target

    def _pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None:
        """The newest staged activation request of this run that no answer has consumed yet."""
        request: Mapping[str, object] | None = None
        answered: set[str] = set()
        for event in execution.events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            if event.event_type == EventType.ACTIVATION_REQUESTED:
                request = payload
            elif event.event_type in {EventType.ACTIVATION_COMMITTED, EventType.ACTIVATION_FAILED}:
                answered.add(request_id)
        if request is None:
            return None
        return None if str(request.get("request_id", "")) in answered else request

    def _clear_fence(self) -> None:
        with self._calls_lock:
            self._activating_thread = None
            self._calls_lock.notify_all()

    def record_activation_refusal(
        self,
        session_id: str,
        request_id: str,
        source: str,
        error: str,
        blockers: Sequence[str],
    ) -> None:
        """Record why an activation did not happen, on the acting session, without consuming the request."""
        session = self.store.session(session_id)
        cursor = _Cursor()
        checkpoint = self.store.checkpoint(session_id)
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        execution = _Execution(session, cursor, list(self.store.events(session_id)), session.run_id or request_id)
        try:
            self._commit_pipeline.commit(
                execution,
                session.status,
                checkpoint.kind if checkpoint is not None else CheckpointKind.READY,
                EventDraft(
                    EventType.ACTIVATION_FAILED,
                    "core",
                    "core",
                    {
                        "request_id": request_id,
                        "source": source,
                        "error": error,
                        "blockers": list(blockers),
                    },
                ),
            )
        except StoreConflict, ValueError:
            # The refusal is a diagnostic, never a reason to lose the durable request.
            pass

    def prepare_activation_target(self) -> tuple[str, Admission]:
        return self._activation.prepare_target()

    def pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None:
        return self._pending_activation_request(execution)
