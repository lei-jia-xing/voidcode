"""Extracted CoreTuiApp behavior: overlay actions.

Methods are applied to the app through RuntimeActions mixins; the facade stays in
terminal_app.py and owns composition, not these responsibilities.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from voidcode.sdk.client import (
    CommandResult,
    CoreClientError,
    ResumeSpec,
)
from voidcode.sdk.views import (
    PluginViewDescriptor,
)
from voidcode.tui.gates import (
    ActivationCommitted,
    ApprovalRequested,
    AskRequested,
    ConfirmationRequested,
    GateChange,
    GateDecided,
    RunTerminated,
    reduce_gates,
)

from . import catalog, events
from .artifact_viewer import (
    ArtifactOpenRequested,
    ArtifactPageRequested,
    InteractiveArtifactViewer,
)
from .formatting import (
    safe as _safe,
)
from .formatting import (
    slash_display as _slash_display,
)
from .keys import Key
from .views import (
    AskAnswered,
    AskCancelled,
    ConfirmationDecided,
    InteractiveApproval,
    InteractiveAsk,
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    InteractiveMarkdown,
    OverlayActionSelected,
    OverlayCancelled,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


class OverlayActions:
    def _apply_gate_change(self: CoreTuiApp, change: GateChange) -> None:
        """Fold one typed gate change and apply its explicit overlay transition."""
        before = self._gates
        after = reduce_gates(before, change)
        self._gates = after
        if isinstance(change, ApprovalRequested):
            if after.approval is not None and after.approval.request_id == change.gate.request_id:
                current = self._overlays.approval
                if current is None or current.request_id != change.gate.request_id:
                    self._overlays.show_approval(InteractiveApproval(change.gate))
                self.status_line = f"Tool approval required: {change.gate.tool_name}"
            return
        if isinstance(change, ConfirmationRequested):
            gate = change.gate
            if gate.request_id in after.decided or not any(entry.request_id == gate.request_id for entry in after.confirmations):
                return
            disposition = self._overlays.request_confirmation(
                InteractiveConfirmation(gate.view, revision_id=gate.revision_id),
                decided=after.decided,
                live_input=self._live_input,
            )
            if disposition in ("shown", "suspended"):
                self.status_line = "Confirmation required"
            if disposition == "suspended":
                # A live view yields to its own cancel confirmation: the instance
                # (filter/selection/scroll) is retained, polling pauses, and only a
                # direct decision restores it — never a historical replay.
                self._live_refresh_at = 0.0
                self._follower.abort_live()
            return
        if isinstance(change, AskRequested):
            gate = change.gate
            if gate.request_id in after.decided or not any(entry.request_id == gate.request_id for entry in after.asks):
                return
            if self._overlays.request_ask(InteractiveAsk(gate.request, revision_id=gate.revision_id), decided=after.decided) == "shown":
                self.status_line = "Interactive ask required"
            return
        if isinstance(change, GateDecided):
            if self._overlays.decide(change.request_id):
                self._present_queued_confirmation()
            return
        if isinstance(change, ActivationCommitted):
            self._overlays.clear_approval()
            if self._overlays.retain_revision(change.to_revision_id):
                self._live_input = None
                self._live_refresh_at = 0.0
            if before.approval is not None or len(before.confirmations) != len(after.confirmations) or len(before.asks) != len(after.asks):
                self._present_queued_confirmation()
            return
        if isinstance(change, RunTerminated):
            if self._overlays.approval is not None and after.approval is None:
                self._overlays.clear_approval()
                self._present_queued_confirmation()
            return
        raise ValueError(f"unknown gate change: {type(change).__name__}")

    def _present_queued_confirmation(self: CoreTuiApp) -> None:
        """Present the oldest still-pending gate, then restore a suspended live view."""
        presented = self._overlays.present(decided=self._gates.decided, catalog_revision=self.catalog_revision)
        if presented.kind == "confirmation":
            self.status_line = "Confirmation required"
        elif presented.kind == "ask":
            self.status_line = "Interactive ask required"
        elif presented.kind == "live":
            suspended = presented.suspended
            assert suspended is not None
            self._live_input = suspended.live_input
            self._live_refresh_at = time.monotonic()
            self.status_line = ""

    def _handle_approval_key(self: CoreTuiApp, key: Key) -> None:
        """Native approval gate: scroll the preview, a=allow, d/Esc=deny; both hit /resume."""
        approval = self.pending_approval
        assert approval is not None
        outcome = approval.handle_key(key)
        if outcome is None:
            return
        self._resolve_approval(outcome.decision, reason=outcome.reason, grant_session_policy=outcome.grant_session_policy)

    def _resolve_approval(
        self: CoreTuiApp,
        decision: Literal["allow", "deny"],
        *,
        reason: str | None = None,
        grant_session_policy: bool = False,
    ) -> None:
        approval = self.pending_approval
        if approval is None:
            return
        request_id = approval.request_id
        if not request_id:
            self.status_line = "No pending approval to resolve"
            return
        if request_id in self._in_flight:
            return  # a held key while the decision is outstanding: not a second thread
        # The gate stays mounted until the server accepts or a durable decided
        # fact closes it: clearing early would strand the approval, because
        # applied-cursor dedup never replays the old request fact.
        self._in_flight.add(request_id)
        session_id, generation = self.session_id, self._generation
        self.status_line = f"Submitting approval {decision}..."

        def worker() -> None:
            try:
                self.client.resume(
                    session_id,
                    ResumeSpec(
                        approval_request_id=request_id,
                        decision=decision,
                        reason=reason,
                        grant_session_policy=grant_session_policy,
                    ),
                )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    # Still armed with the same request id: no auto-resend, the
                    # user re-decides after the restart.
                    self._upgrade_pending_notice(f"approval {decision}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"Approval {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"Approval {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            self.event_queue.put(events.GateAcceptedEvent(request_id=request_id, session_id=session_id, generation=generation))

        self._work.submit("CoreApproval", worker)

    def _handle_overlay_key(self: CoreTuiApp, key: Key) -> None:
        overlay = self.active_overlay
        assert overlay is not None
        outcome = overlay.handle_key(key)
        if outcome is None:
            return
        if isinstance(outcome, OverlayCancelled):
            self._follower.abort_interaction()
            self._close_live_input()
            self._overlays.clear_active()
            self._artifact_ack_deadline = None
            self.status_line = ""
            self._present_queued_confirmation()
            return
        if isinstance(outcome, ArtifactPageRequested):
            if isinstance(overlay, InteractiveArtifactViewer):
                self._load_artifact_page(overlay, outcome.offset)
            return
        if isinstance(outcome, ArtifactOpenRequested):
            if isinstance(overlay, InteractiveArtifactViewer):
                self._open_artifact(overlay)
            return
        if isinstance(outcome, ConfirmationDecided) and isinstance(overlay, InteractiveConfirmation):
            if outcome.decision not in ("allow", "deny"):
                self.status_line = f"Unknown confirmation decision: {_safe(outcome.decision)}"
                return
            self._resolve_confirmation(outcome.decision)
            return
        if isinstance(outcome, AskAnswered) and isinstance(overlay, InteractiveAsk):
            self._resolve_ask(outcome.values)
            return
        if isinstance(outcome, AskCancelled) and isinstance(overlay, InteractiveAsk):
            self._cancel_ask()
            return
        if isinstance(outcome, OverlayActionSelected) and isinstance(overlay, (InteractiveChoices, InteractiveForm, InteractiveMarkdown)):
            self._handle_overlay_action(overlay, outcome)

    def _handle_overlay_action(
        self: CoreTuiApp,
        overlay: InteractiveChoices | InteractiveForm | InteractiveMarkdown,
        outcome: OverlayActionSelected,
    ) -> None:
        """Dispatch one command action selected in a choices/form/markdown overlay."""
        action = outcome.action
        arguments = outcome.arguments
        if isinstance(overlay, InteractiveChoices) and action == "tui.switch_session":
            self._overlays.clear_active()
            target_id = str(arguments.get("session_id", ""))
            if target_id and target_id != self.session_id:
                self._switch_session(target_id)
            return
        revision = overlay.revision_id
        if outcome.prepare_input:
            descriptors = catalog.get(self, retry=True)
            if not self.catalog_revision:
                catalog.unavailable(self)
                return
            descriptor = next((item for item in descriptors if item.name == action), None)
            if revision != self.catalog_revision or descriptor is None or not descriptor.has_input:
                self.status_line = "Command input unavailable; refresh the catalog"
                return
            if self._live_input is not None:
                self._live_input = events.LiveInput(name=action, arguments=arguments, revision=revision, source=overlay)
            self._overlays.clear_active()
            self._prepare_command_input_async(descriptor, arguments=arguments)
            return
        # The overlay (with its typed inputs) stays mounted until the
        # server accepts: a late 503 must not lose the user's draft. The
        # in-flight marker stops a held Enter from spawning a second call.
        submit_key = f"invoke:{action}@{revision}"
        if submit_key in self._in_flight:
            return
        self._in_flight.add(submit_key)
        self.status_line = f"Dispatching /{_slash_display(action)}..."
        self._invoke_async(action, arguments, revision, submit_key=submit_key)

    def _resolve_confirmation(self: CoreTuiApp, decision: Literal["allow", "deny"]) -> None:
        """Submit the stored request id's decision; the overlay never lies about it."""
        request_id = getattr(self.active_overlay, "request_id", "")
        # The revision of the envelope that displayed this confirmation.
        revision = getattr(self.active_overlay, "revision_id", "")
        if not request_id:
            self.status_line = "No pending confirmation to resolve"
            return
        if request_id in self._in_flight:
            return  # a held key while the decision is outstanding: not a second thread
        # The overlay stays mounted until the server accepts or the durable
        # decided fact closes it: closing early would strand the request,
        # because applied-cursor dedup never replays the old requested fact.
        self._in_flight.add(request_id)
        session_id, generation = self.session_id, self._generation
        self.status_line = f"Submitting confirmation {decision}..."

        def worker() -> None:
            try:
                self.client.confirm(
                    session_id,
                    request_id,
                    decision,
                    expected_revision=revision,
                )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    # Still armed with the same request id and revision: no
                    # auto-resend, the user re-decides after the restart.
                    self._upgrade_pending_notice(f"confirmation {decision}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"Confirmation {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"Confirmation {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            self.event_queue.put(events.GateAcceptedEvent(request_id=request_id, session_id=session_id, generation=generation))

        self._work.submit("CoreConfirm", worker)

    def _resolve_ask(self: CoreTuiApp, values: Mapping[str, object]) -> None:
        """Submit the stored request id's answers for an interactive ask."""
        ask = self.active_overlay
        if not isinstance(ask, InteractiveAsk):
            return
        request_id = ask.request_id
        revision = ask.revision_id
        if not request_id:
            self.status_line = "No pending ask to resolve"
            return
        if request_id in self._in_flight:
            return
        self._in_flight.add(request_id)
        session_id, generation = self.session_id, self._generation
        self.status_line = "Submitting answers..."

        def worker() -> None:
            try:
                self.client.answer_ask(
                    session_id,
                    request_id,
                    values,
                    expected_revision=revision,
                )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    self._upgrade_pending_notice("ask answer", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"Ask answer failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"Ask answer failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            self.event_queue.put(events.GateAcceptedEvent(request_id=request_id, session_id=session_id, generation=generation))

        self._work.submit("CoreAskAnswer", worker)

    def _cancel_ask(self: CoreTuiApp) -> None:
        """Submit cancellation for an interactive ask."""
        ask = self.active_overlay
        if not isinstance(ask, InteractiveAsk):
            return
        request_id = ask.request_id
        revision = ask.revision_id
        if not request_id:
            self.status_line = "No pending ask to cancel"
            return
        if request_id in self._in_flight:
            return
        self._in_flight.add(request_id)
        session_id, generation = self.session_id, self._generation
        self.status_line = "Cancelling ask..."

        def worker() -> None:
            try:
                self.client.cancel_ask(
                    session_id,
                    request_id,
                    expected_revision=revision,
                )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    self._upgrade_pending_notice("ask cancel", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"Ask cancel failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"Ask cancel failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            self.event_queue.put(events.GateAcceptedEvent(request_id=request_id, session_id=session_id, generation=generation))

        self._work.submit("CoreAskCancel", worker)

    def _invoke_async(
        self: CoreTuiApp,
        name: str,
        arguments: Mapping[str, object],
        revision: str,
        *,
        submit_key: str | None = None,
        surface_result: bool = False,
        display_name: str | None = None,
    ) -> None:
        """Run one command off the UI thread under the revision the user saw.

        Only a core read-only receipt is surfaced directly. Plugin commands
        project their result through the durable ``core.command_completed``
        event, and run/resume project through run events; rendering those direct
        answers too would duplicate settled rows. ``surface_result`` comes from
        catalog metadata (owner plus effects), never a client command list.
        """
        token = self._follower.begin_interaction()
        submitted_overlay = self.active_overlay
        shown = display_name if display_name else _slash_display(name)
        session_id, generation = self.session_id, self._generation

        def finish(status: str, result: CommandResult | None = None) -> None:
            """Hand this command's captured completion to the UI thread once."""
            self.event_queue.put(events.CommandDoneEvent(name=name, status=status, result=result, session_id=session_id, generation=generation))

        def worker() -> None:
            try:
                res = self.client.invoke_command(
                    session_id,
                    name,
                    arguments,
                    expected_revision=revision,
                )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    self._upgrade_pending_notice(f"/{shown}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"/{shown} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                    finish("error")
                if submit_key is not None:
                    self.event_queue.put(events.DecisionDoneEvent(request_id=submit_key, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"/{shown} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                finish("error")
                if submit_key is not None:
                    self.event_queue.put(events.DecisionDoneEvent(request_id=submit_key, session_id=session_id, generation=generation))
                return
            status = res.status
            if status == "pending_confirmation":
                confirmation = res.confirmation
                if confirmation is not None:
                    self.event_queue.put(
                        events.ConfirmationEvent(confirmation=confirmation, revision_id=revision, session_id=session_id, generation=generation)
                    )
            elif status == "error":
                self._queue_notice(
                    f"/{shown} failed: {res.error or 'error'}",
                    tone="error",
                    session_id=session_id,
                    generation=generation,
                )
            # An error notice already owns the failure text; a plugin/run result
            # is already projected by its durable event. Only the core read
            # receipt has no other display path.
            receipt = res if surface_result and status == "ok" else None
            finish(status, receipt)
            # Failed validation leaves the editor and draft mounted for correction.
            # Only an accepted action may close the overlay that submitted it.
            if submit_key is not None:
                if status == "error":
                    self.event_queue.put(events.DecisionDoneEvent(request_id=submit_key, session_id=session_id, generation=generation))
                else:
                    self.event_queue.put(
                        events.InvokeAcceptedEvent(
                            request_id=submit_key,
                            action=name,
                            revision_id=revision,
                            overlay=submitted_overlay,
                            session_id=session_id,
                            generation=generation,
                        )
                    )

            if res.status == "ok" and res.presentation is not None:
                presentation = res.presentation
                self.event_queue.put(
                    events.CommandPresentationEvent(
                        token=token,
                        view=PluginViewDescriptor(
                            owner="command", name=name, title=presentation.title, view_type=presentation.view_type, data=presentation.data
                        ),
                        revision_id=revision,
                        session_id=session_id,
                        generation=generation,
                    )
                )

        self._mark_command_active()
        if self._work.submit("CoreInvoke", worker) is None:
            self._mark_command_finished()

    def _cancel_active_run(self: CoreTuiApp) -> None:
        if not (self._run_active or self._commands_in_flight) or self._cancel_pending:
            return
        self._cancel_pending = True
        self.status_line = ""
        session_id, generation = self.session_id, self._generation

        def worker() -> None:
            try:
                res = self.client.cancel(session_id)
                if not res.cancellation_requested:
                    self.event_queue.put(events.CancelDoneEvent(session_id=session_id, generation=generation))
            except Exception as exc:
                self._queue_notice(f"Cancel error: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.CancelDoneEvent(session_id=session_id, generation=generation))

        self._work.submit("CoreCancel", worker)
