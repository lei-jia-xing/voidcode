"""Startup recovery for accepted spawns and unpublished terminal child runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ...sdk.child_schema import CHILD_PROMPT_MAX_BYTES
from ..events import EventType, child_completion_notice_id
from ..storage.store import (
    MAX_CHILD_LABEL_BYTES,
    CheckpointKind,
    EventDraft,
    SessionRecord,
    SessionStatus,
    StoreConflict,
)
from ._base import _CHILD_RECOVERY_ERROR_BYTES
from ._controls import _DelegationControls
from ._outcomes import _utf8_preview


class _DelegationRecovery(_DelegationControls):
    """Startup recovery for accepted spawns and unpublished terminal child runs."""

    def _recover_unstarted_child_drivers(self) -> list[tuple[str, str]]:
        """Restart accepted spawns whose driver future never ran; never replay started work.

        A close/restart may cancel a queued driver after the child row and its parent
        spawn fact are durable but before any run commits. Such a child is idle with no
        run id, no user message, no started continuation, and no terminal fact. The
        birth record carries the exact driver input, so submitting the existing driver
        continues the accepted spawn. Anything that is not pristine is either left to
        publication recovery or failed visibly instead of staying idle forever.
        """
        notices: list[tuple[str, str]] = []
        finished_children: dict[str, set[str]] = {}
        for child in self.core.store.sessions(workspace=str(self.core.workspace), limit=None):
            if self.core._closed:
                break
            parent_id = child.parent_session_id
            if parent_id is None or child.status is not SessionStatus.IDLE or child.run_id is not None:
                continue
            if child.session_id in self._started_children:
                continue
            try:
                parent = self.core.store.session(parent_id)
            except KeyError:
                continue
            if parent.workspace != child.workspace:
                continue
            if parent_id not in finished_children:
                finished_children[parent_id] = {
                    str(event.payload.get("child_session_id")) for event in self.core.store.events_by_types(parent_id, (EventType.CHILD_FINISHED,))
                }
            if child.session_id in finished_children[parent_id]:
                continue
            events = self.core.store.events(child.session_id)
            worked = any(event.event_type in (EventType.USER_MESSAGE, EventType.CONTINUATION_STARTED) or event.run_id for event in events)
            birth = self._birth_record(child.session_id)
            if worked:
                reason = "child has committed work but no run identity; refusing to replay it"
            elif self.core.store.checkpoint(child.session_id) is not None:
                reason = "idle child already holds a checkpoint"
            else:
                reason = None
            try:
                if reason is None:
                    notice = self._recover_unstarted_child(parent, child, birth)
                else:
                    notice = self._fail_unstarted_child(parent, child, birth, reason)
            except Exception as exc:
                # One corrupt reservation must not starve the rest of startup recovery.
                try:
                    notice = self._fail_unstarted_child(parent, child, birth, f"recovery failed: {type(exc).__name__}: {exc}")
                except Exception:
                    notice = None
            if notice is not None:
                notices.append(notice)
        return notices

    def _recover_unstarted_child(
        self,
        parent: SessionRecord,
        child: SessionRecord,
        birth: Mapping[str, object] | None,
    ) -> tuple[str, str] | None:
        """Continue one pristine accepted spawn, or publish a bounded failure cause."""
        reason = self._unstarted_child_blocker(child, birth)
        if reason is None:
            assert birth is not None
            try:
                if birth.get("readonly") is True and "read-only" not in self.core.store.session_overlays(child.session_id):
                    self.core.store.set_session_overlay(child.session_id, "core")
                self._ensure_child_spawn_fact(parent, child, birth)
            except Exception as exc:  # Recovery turns any mapping failure into a visible outcome
                reason = f"spawn mapping could not be recovered: {type(exc).__name__}: {exc}"
            else:
                prompt = cast(str, birth["prompt"])
                raw_steps = birth.get("max_steps")
                max_steps = cast(int | None, raw_steps)
                label = cast(str, birth.get("label", ""))
                notify = bool(birth.get("notify_on_completion"))
                try:
                    self._start_child_run(parent.session_id, child.session_id, prompt, max_steps, label, notify)
                except Exception as exc:  # A failed submit must not leave the reservation silent
                    reason = f"child driver could not be submitted: {type(exc).__name__}: {exc}"
                else:
                    return None
        return self._fail_unstarted_child(parent, child, birth, reason)

    def _unstarted_child_blocker(self, child: SessionRecord, birth: Mapping[str, object] | None) -> str | None:
        """Why this idle reservation cannot be safely driven, or None when it can."""
        if child.active_revision_id != self.core.revision_id:
            return "child session revision does not match the active revision"
        if birth is None:
            return "child reservation has no birth record"
        if birth.get("parent_session_id") != child.parent_session_id:
            return "child birth record does not name its parent session"
        if birth.get("depth") != child.depth:
            return "child birth record depth does not match the session"
        root_id = birth.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            return "child birth record has no captured root action identity"
        if "root_action_budget" not in birth:
            return "child birth record has no captured root budget state"
        budget = birth.get("root_action_budget")
        if budget is not None and (type(budget) is not int or budget < 0):
            return "child birth record has an invalid captured root budget"
        root_state = self.core.store.root_budget_state(root_id)
        if budget is None:
            if root_state is not None:
                return "captured uncapped root authorization conflicts with a finite budget row"
        elif root_state is None or root_state[0] < budget:
            return "captured finite root authorization has no matching durable budget row"
        prompt = birth.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > CHILD_PROMPT_MAX_BYTES:
            return "child birth record has no recoverable spawn prompt"
        if "max_steps" not in birth:
            return "child birth record has no captured spawn max_steps"
        max_steps = birth.get("max_steps")
        if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
            return "child birth record has an invalid spawn max_steps"
        label = birth.get("label", "")
        if not isinstance(label, str) or len(label.encode("utf-8")) > MAX_CHILD_LABEL_BYTES:
            return "child birth record has an invalid spawn label"
        if type(birth.get("notify_on_completion")) is not bool:
            return "child birth record has no notify_on_completion"
        return None

    def _ensure_child_spawn_fact(self, parent: SessionRecord, child: SessionRecord, birth: Mapping[str, object]) -> None:
        """Repair a crash-window parent mapping before the recovered driver publishes."""
        if self._spawned_fact(parent.session_id, child.session_id) is not None:
            return
        root_id = cast(str, birth["root_id"])
        budget = birth.get("root_action_budget")
        payload: dict[str, object] = {
            "child_session_id": child.session_id,
            "root_id": root_id,
            "root_action_budget": budget,
            "label": cast(str, birth.get("label", "")),
            "depth": child.depth,
            "tool_allowlist": None if child.tool_allowlist is None else list(child.tool_allowlist),
            "notify_on_completion": bool(birth.get("notify_on_completion")),
        }
        for key in ("result_schema", "spawn_owner", "idempotency_key", "spec_hash"):
            if key in birth:
                payload[key] = birth[key]
        try:
            self.core.commit_public_fact(
                parent.session_id,
                EventDraft(EventType.CHILD_SPAWNED, "core", "plugin", payload),
                root_action_id=root_id,
                initial_root_budget=budget if isinstance(budget, int) else None,
            )
        except StoreConflict:
            if self._spawned_fact(parent.session_id, child.session_id) is None:
                raise

    def _fail_unstarted_child(
        self,
        parent: SessionRecord,
        child: SessionRecord,
        birth: Mapping[str, object] | None,
        reason: str,
    ) -> tuple[str, str] | None:
        """Claim one unstartable reservation as a terminal failed run and publish its fact."""
        bounded = _utf8_preview(f"unstarted child was not recovered: {reason}", _CHILD_RECOVERY_ERROR_BYTES)
        run_id = f"unstarted:{child.session_id}"
        current = self.core.store.session(child.session_id)
        if current.status is SessionStatus.IDLE and current.run_id is None:
            try:
                self.core.store.commit_staged(
                    child.session_id,
                    expected_sequence=current.last_event_sequence,
                    expected_run_id=None,
                    run_id=run_id,
                    revision_id=self.core.revision_id,
                    events=(
                        EventDraft(
                            EventType.RUN_FAILED,
                            "core",
                            "core",
                            {"error": bounded, "logical_turn": "", "eligible_owners": []},
                        ),
                    ),
                    status=SessionStatus.FAILED,
                    checkpoint_kind=CheckpointKind.TERMINAL,
                    checkpoint_payload={"step": 0, "stop_cause": "error"},
                )
                self.core.notify_commit()
            except StoreConflict:
                current = self.core.store.session(child.session_id)
                if current.status is not SessionStatus.FAILED or current.run_id != run_id:
                    return None
        elif current.status is not SessionStatus.FAILED or current.run_id != run_id:
            return None
        label = ""
        notify = False
        if birth is not None:
            raw_label = birth.get("label")
            if isinstance(raw_label, str) and len(raw_label.encode("utf-8")) <= MAX_CHILD_LABEL_BYTES:
                label = raw_label
            root_id = birth.get("root_id")
            notify = birth.get("notify_on_completion") is True and isinstance(root_id, str) and bool(root_id)
        try:
            self._publish_child_terminal(
                parent.session_id,
                child.session_id,
                run_id=run_id,
                status="failed",
                output=None,
                error=bounded,
                stop_cause="error",
                label=label,
                notify=notify,
                deliver=False,
            )
        except Exception:  # The durable terminal run is retried by publication recovery
            return None
        if notify and self._child_fact_exists(parent.session_id, child.session_id, run_id):
            return (parent.session_id, child_completion_notice_id(parent.session_id, child.session_id, run_id))
        return None

    def _recover_child_publications(self) -> list[tuple[str, str]]:
        """Publish every terminal child run whose fact a crash never committed (no child rerun).

        A later follow-up run must not hide an older unpublished terminal run: the scan is over
        each session's durable terminal events, not only the session's current run id.
        """
        notices: list[tuple[str, str]] = []
        for session in self.core.store.sessions(workspace=str(self.core.workspace), limit=None):
            parent_id = session.parent_session_id
            if parent_id is None:
                continue
            events = self.core.store.events(session.session_id)
            run_ids: dict[str, None] = {}
            for event in events:
                if event.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED) and event.run_id:
                    run_ids.setdefault(event.run_id, None)
            for run_id in run_ids:
                if self._child_fact_exists(parent_id, session.session_id, run_id):
                    continue
                facts = self._child_run_facts(session, run_id, events)
                if facts is None:
                    continue
                notify = facts.notify
                self._publish_child_terminal(
                    parent_id,
                    session.session_id,
                    run_id=run_id,
                    status=facts.status,
                    output=facts.output,
                    error=facts.error,
                    stop_cause=facts.stop_cause,
                    label=facts.label,
                    notify=notify,
                    deliver=False,
                    budget_exhaustion=facts.budget_exhaustion,
                    output_error=facts.output_error,
                )
                if notify:
                    notices.append((parent_id, child_completion_notice_id(parent_id, session.session_id, run_id)))
        return notices
