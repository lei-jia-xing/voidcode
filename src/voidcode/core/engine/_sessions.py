"""Session, run, resume, transcript, usage, follow-up, ask, and approval entry points.

Every method resolves session truth and revision eligibility before delegating to
the runtime component that owns the work. The lifecycle base supplies state,
locks, and the dispatch fence."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from uuid import uuid4

from ...sdk.client import ApprovalPolicyRule, FollowupListReceipt, FollowupWithdrawReceipt, QueuedFollowup
from ...sdk.json_values import json_wire_object
from ...sdk.provider import ImageAttachment
from ...sdk.services import ArtifactPage, SessionUsageReport
from ...sdk.transcript import ContextHistory, TranscriptPage
from ..cancellation import Cancellation
from ..events import EventType
from ..execution import _Cursor, _Execution
from ..storage.store import CheckpointKind, EventDraft, SessionRecord, SessionStatus, StoreConflict, StoredEvent
from ._lifecycle import CoreLifecycle
from .followup import FollowupSubmission
from .interactive_ask import AskBinding, AskOutcome, is_ask_binding_expired
from .turn_execution import CoreResult


class CoreSessions(CoreLifecycle):
    """Session, run, resume, transcript, usage, follow-up, ask, and approval entry points."""

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        """Read immutable workspace artifact bytes, never a caller-supplied path."""
        self.ensure_open()
        return self._projection.read(sha256, offset=offset, limit=limit)

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        images: tuple[ImageAttachment, ...] = (),
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        return self._run_coordinator.run(
            prompt,
            session_id=session_id,
            max_steps=max_steps,
            root_action_budget=root_action_budget,
            images=images,
            interaction=interaction,
        )

    def _run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        prompt_max_bytes: int,
        images: tuple[ImageAttachment, ...] = (),
        _root_id: str | None = None,
        _refuse_reserved_cancelled: bool = False,
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        return self._run_coordinator._run(
            prompt,
            session_id=session_id,
            max_steps=max_steps,
            root_action_budget=root_action_budget,
            prompt_max_bytes=prompt_max_bytes,
            images=images,
            _root_id=_root_id,
            _refuse_reserved_cancelled=_refuse_reserved_cancelled,
            interaction=interaction,
        )

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: Literal["allow", "deny"] | None = None,
        max_steps: int | None = None,
        extend_root_actions: int | None = None,
        root_action_grant_id: str | None = None,
        interaction: Literal["interactive", "unattended"] | None = None,
        reason: str | None = None,
        grant_session_policy: bool = False,
    ) -> CoreResult:
        return self._run_coordinator.resume(
            session_id,
            approval_request_id=approval_request_id,
            decision=decision,
            max_steps=max_steps,
            extend_root_actions=extend_root_actions,
            root_action_grant_id=root_action_grant_id,
            interaction=interaction,
            reason=reason,
            grant_session_policy=grant_session_policy,
        )

    def answer_ask(
        self,
        session_id: str,
        request_id: str,
        values: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> CoreResult:
        """Adopt one client's answer for the session's live ask and continue the run."""
        return self._interactive_ask.answer_ask(session_id, request_id, values, expected_revision=expected_revision)

    def cancel_ask(self, session_id: str, request_id: str, *, expected_revision: str) -> CoreResult:
        """Adopt one client's cancellation for the session's live ask and continue the run."""
        return self._interactive_ask.cancel_ask(session_id, request_id, expected_revision=expected_revision)

    def sweep_expired_asks(self) -> None:
        """Settle caller-declared ask expiries due now; one bad session never stops the sweep."""
        self._interactive_ask.sweep_expired_asks()

    def drain_deferred_requests(
        self,
        session_id: str,
        *,
        background_action_id: str | None = None,
        action_abort: threading.Event | None = None,
    ) -> None:
        """Run committed deferred requests after their caller's callback lease has returned."""
        with self._session_lock(session_id):
            while True:
                self.ensure_open()
                session = self.session(session_id)
                if not self.admits_fresh_work(session):
                    return
                events = list(self.store.events(session_id))
                completed_sequences = {
                    event.payload.get("batch_sequence")
                    for event in events
                    if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED} and event.payload.get("deferred") is True
                }
                terminal_request_ids = {event.payload.get("request_id") for event in events if event.event_type == EventType.TOOL_DEFERRED_CANCELLED}
                request = next(
                    (
                        event
                        for event in events
                        if event.event_type == EventType.TOOL_DEFERRED
                        and event.sequence not in completed_sequences
                        and event.payload.get("request_id") not in terminal_request_ids
                        and (
                            event.payload.get("background_action_id") == background_action_id
                            if background_action_id is not None
                            else event.payload.get("background_action_id") is None
                        )
                    ),
                    None,
                )
                if request is None:
                    return
                raw_action_id = request.payload.get("background_action_id")
                request_action_id = raw_action_id if isinstance(raw_action_id, str) else None
                if action_abort is not None and action_abort.is_set():
                    if request_action_id is not None:
                        action_stop_cause = self._background.abort_cause(request_action_id)
                        status = "cancelled" if action_stop_cause == "owner_cancel" else "interrupted"
                        self.cancel_background_tool_requests(
                            session_id,
                            request_action_id,
                            status=status,
                            stop_cause=action_stop_cause,
                            reason="background action stopped before deferred dispatch",
                        )
                    return
                raw_root = request.payload.get("root_id")
                root_id = raw_root if isinstance(raw_root, str) and raw_root else ("" if request_action_id else uuid4().hex)
                raw_budget = request.payload.get("root_action_budget")
                root_budget = raw_budget if type(raw_budget) is int and raw_budget >= 0 else None
                raw_tools = request.payload.get("origin_visible_tools")
                origin_tools = (
                    tuple(raw_tools) if isinstance(raw_tools, (list, tuple)) and all(isinstance(name, str) and name for name in raw_tools) else None
                )
                raw_readonly = request.payload.get("origin_readonly")
                origin_readonly = raw_readonly if type(raw_readonly) is bool else None
                authority_missing = request_action_id is not None and (
                    not root_id or "root_action_budget" not in request.payload or origin_tools is None or origin_readonly is None
                )
                execution = _Execution(
                    session,
                    _Cursor(
                        root_id=root_id,
                        initial_root_budget=root_budget,
                        deferred_only=True,
                        deferred_action_id=request_action_id,
                        deferred_origin_tools=origin_tools,
                        deferred_origin_readonly=origin_readonly,
                        deferred_authority_missing=authority_missing,
                        logical_turn=uuid4().hex,
                    ),
                    events,
                    uuid4().hex,
                )
                if action_abort is None:
                    abort = Cancellation()
                else:
                    abort = Cancellation(
                        action_abort,
                        linked_reason="background action stopped",
                        linked_stop_cause_provider=lambda action_id=request_action_id or "": self._background.stop_cause(action_id),
                    )
                self._cancellations.register_run(session_id, execution.run_id, abort)
                try:
                    self._commit_pipeline.commit(execution, SessionStatus.RUNNING, CheckpointKind.READY)
                    result = self._turn_driver.drive(execution)
                finally:
                    self._cancellations.unregister_run(session_id)
                if result.status not in ("completed", "idle"):
                    return

    def create_session(self, session_id: str | None = None) -> SessionRecord:
        """Create a new durable session in idle state bound to workspace and active revision."""
        self.ensure_open()
        sid = uuid4().hex if session_id is None else session_id
        return self._create_session_row(sid)

    def _create_session_row(self, session_id: str) -> SessionRecord:
        """Create a session on the workspace's recorded revision, retrying if it moves first."""
        for _attempt in range(3):
            recorded = self.store.workspace_current(str(self.workspace)) or self.revision_id
            try:
                return self.store.create_session(session_id, str(self.workspace), recorded)
            except StoreConflict:
                continue
        raise StoreConflict(f"could not create session {session_id!r}: the workspace revision kept moving")

    def session(self, session_id: str) -> SessionRecord:
        """Fetch session metadata by ID, verifying workspace."""
        self.ensure_open()
        s = self.store.session(session_id)
        if s.workspace != str(self.workspace):
            raise ValueError("session belongs to another workspace")
        return s

    def sessions(self, *, limit: int = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions for this workspace, ordered by updated_at descending, up to limit."""
        self.ensure_open()
        return self.store.sessions(workspace=str(self.workspace), limit=limit)

    def plugin_query(self, session_id: str, name: str, arguments: Mapping[str, object]) -> Mapping[str, object] | None:
        """Answer one named read-only plugin query in this workspace's runtime.

        The session lookup verifies the caller's session belongs to this core;
        the answering plugin is selected by the current capability generation,
        so disabling it makes the query unavailable instead of falling back to
        a core-owned file listing.
        """
        self.ensure_open()
        self.session(session_id)
        return self.plugin_runtime.execute_query(name, arguments, session_id=session_id, workspace=self.workspace)

    def show(self, session_id: str, *, events: bool = False) -> dict[str, object]:
        """Return session metadata, active checkpoint, and optional events as JSON-serializable dict."""
        self.ensure_open()
        s = self.session(session_id)
        checkpoint = self.store.checkpoint(session_id)
        payload: dict[str, object] = {
            "session_id": s.session_id,
            "status": s.status.value,
            "run_id": s.run_id,
            "revision_id": s.active_revision_id,
            "turn": s.turn,
            "last_event_sequence": s.last_event_sequence,
            "checkpoint": None if checkpoint is None else {"kind": checkpoint.kind.value, "payload": json_wire_object(checkpoint.payload)},
        }
        if events:
            payload["events"] = [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "run_id": event.run_id,
                    "revision_id": event.revision_id,
                    "source": event.source,
                    "actor": event.actor,
                    "payload": json_wire_object(event.payload),
                }
                for event in self.store.events(session_id)
            ]
        return payload

    def events_after(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[StoredEvent, ...]:
        """Bounded tail read of stored events for continuous SSE streaming."""
        self.ensure_open()
        self.session(session_id)
        return self.store.events_after(session_id, after_sequence=after_sequence, limit=limit)

    def transcript_page(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        """One bounded transcript page; advance to next_sequence while has_more, even when empty."""
        self.ensure_open()
        self.session(session_id)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.transcript_page(session_id, after_sequence, limit)

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory:
        """One bounded context prefix from the session's first record."""
        self.ensure_open()
        self.session(session_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.context_history(session_id, limit)

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Calculate truthful token usage metrics strictly from immutable core facts."""
        self.ensure_open()
        s = self.session(session_id)
        return self._get_session_usage(session_id, since_sequence, until_sequence, session=s)

    def _get_session_usage(
        self,
        session_id: str,
        since_sequence: int = 0,
        until_sequence: int | None = None,
        *,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport:
        resolved = self.session(session_id) if session is None else session
        return self._usage.report(resolved, since_sequence=since_sequence, until_sequence=until_sequence)

    def get_session_usage(
        self,
        session_id: str,
        *,
        since_sequence: int | None = None,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport:
        seq = 0 if since_sequence is None else since_sequence
        return self._get_session_usage(session_id, since_sequence=seq, session=session)

    def list_followups(self, session_id: str) -> FollowupListReceipt:
        """List pending user follow-ups for one session in FIFO order."""
        self.ensure_open()
        self.session(session_id)
        rows = self.store.pending_followups(session_id)
        return FollowupListReceipt(
            session_id=session_id,
            revision_id=self.revision_id,
            followups=tuple(
                QueuedFollowup(
                    continuation_id=row.continuation_id,
                    prompt=row.prompt,
                    revision_id=row.revision_id,
                    created_at=row.created_at,
                    max_steps=row.max_steps,
                    image_count=row.image_count,
                )
                for row in rows
            ),
        )

    def withdraw_followup(
        self,
        session_id: str,
        continuation_id: str,
        *,
        expected_revision: str,
    ) -> FollowupWithdrawReceipt:
        """Withdraw one pending user follow-up before claim, bound to the displayed revision."""
        self.ensure_open()
        self._check_expected_revision(expected_revision)
        if not isinstance(continuation_id, str) or not continuation_id.strip() or "\0" in continuation_id:
            raise ValueError("continuation_id must be nonempty text without NUL")
        self.session(session_id)
        record = self.store.withdraw_pending_followup(
            session_id,
            continuation_id,
            expected_revision=expected_revision,
            revision_id=self.revision_id,
        )
        self.notify_commit()
        self._delegation.notify_child_state_changed()
        return FollowupWithdrawReceipt(
            session_id=session_id,
            continuation_id=record.continuation_id,
            revision_id=self.revision_id,
            prompt=record.prompt,
            max_steps=record.max_steps,
            image_count=record.image_count,
            withdrawn=True,
        )

    def record_followup_event(
        self,
        session_id: str,
        event: EventDraft,
        *,
        run_id: str | None = None,
    ) -> None:
        self.store.append_session_event_at_tip(
            session_id,
            revision_id=self.revision_id,
            event=event,
            run_id=run_id,
        )
        self.notify_commit()

    def queue_followup(
        self,
        session: SessionRecord,
        prompt: str,
        max_steps: int | None,
        *,
        images: tuple[ImageAttachment, ...] = (),
    ) -> CoreResult:
        return self._followup.queue_followup(
            session,
            FollowupSubmission(prompt=prompt, max_steps=max_steps, images=images),
        )

    def execute_claimed_followup(self, session_id: str, claim: Any) -> CoreResult:
        return self._followup.execute_claimed_followup(session_id, claim)

    def claim_pending_followup(self, session_id: str) -> Any | None:
        return self._followup.claim_pending_followup(session_id)

    def ask_binding(self, execution: _Execution, cursor: _Cursor) -> AskBinding:
        return self._interactive_ask.resolve_ask_binding(execution, cursor)

    def ask_binding_expired(self, binding: AskBinding) -> bool:
        return is_ask_binding_expired(binding)

    def adopt_ask_terminal(self, execution: _Execution, binding: AskBinding, outcome: AskOutcome) -> None:
        self._interactive_ask.adopt_ask_terminal(execution, binding, outcome)

    def has_approval_policy(self, session_id: str, tool_name: str, arguments: Mapping[str, object]) -> bool:
        policies = self.store.approval_policies(session_id)
        for policy in policies:
            target_tool = policy.get("tool_name")
            if target_tool not in ("*", tool_name):
                continue
            pattern = policy.get("pattern")
            if pattern is None:
                return True
            pattern_str = str(pattern)
            path = arguments.get("path")
            if isinstance(path, str) and (path.startswith(pattern_str) or path == pattern_str):
                return True
            command = arguments.get("command")
            if isinstance(command, str) and (command.startswith(pattern_str) or command == pattern_str):
                return True
            for val in arguments.values():
                if isinstance(val, str) and (val.startswith(pattern_str) or val == pattern_str):
                    return True
        return False

    def list_approval_policies(self, session_id: str) -> list[ApprovalPolicyRule]:
        self.ensure_open()
        raw_list = self.store.approval_policies(session_id)
        res: list[ApprovalPolicyRule] = []
        for item in raw_list:
            raw_created = item.get("created_at")
            created_at = int(raw_created) if isinstance(raw_created, (int, float, str)) else 0
            res.append(
                ApprovalPolicyRule(
                    rule_id=str(item["rule_id"]),
                    tool_name=str(item["tool_name"]),
                    pattern=str(item["pattern"]) if item.get("pattern") is not None else None,
                    created_at=created_at,
                )
            )
        return res

    def revoke_approval_policy(self, session_id: str, rule_id: str) -> bool:
        self.ensure_open()
        with self._session_lock(session_id):
            revoked = self.store.revoke_approval_policy(session_id, rule_id, revision_id=self.revision_id)
            if revoked:
                self.notify_commit()
            return revoked

    def session_budgets(self, session_id: str) -> Sequence[Any]:
        return self.store.session_budgets(session_id)

    def cancel_pending_continuations(self, session_id: str, reason: str) -> None:
        self.store.cancel_pending_continuations(session_id, reason)

    def artifact_page(self, digest: str, offset: int, limit: int) -> ArtifactPage:
        return self._projection.page(digest, offset, limit)

    def create_session_row(self, session_id: str) -> SessionRecord:
        return self._create_session_row(session_id)
