"""Child outcome projection: typed terminal decoding, run facts, and the in-process cache."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from ...sdk.codec import decode_provider_result
from ...sdk.hooks import StopCause, is_stop_cause
from ...sdk.services import (
    BudgetExhaustion,
    ChildOutcome,
    TerminalCaptureKind,
    TerminalRunCapture,
)
from ..child_contracts import ChildRunStatus
from ..events import EventType
from ..storage.store import (
    SessionRecord,
    SessionStatus,
    StoreConflict,
    StoredEvent,
)
from ._records import _DelegationRecords

if TYPE_CHECKING:
    from ..engine.turn_execution import CoreResult


class _DelegationOutcomes(_DelegationRecords):
    """Child outcome projection: typed terminal decoding, run facts, and the in-process cache."""

    def _latest_child_fact(self, child_id: str, run_id: str | None) -> dict[str, object] | None:
        with self._child_finished_lock:
            runs = self._child_finished.get(child_id, {})
            if run_id is not None and run_id in runs:
                return dict(runs[run_id])
            if run_id is None and runs:
                return dict(next(reversed(runs.values())))
        return None

    def _remember_child_outcome(self, child_id: str, run_id: str, payload: Mapping[str, object]) -> None:
        with self._child_finished_lock:
            runs = self._child_finished.setdefault(child_id, {})
            runs[run_id] = dict(payload)
            while len(runs) > 32:
                runs.pop(next(iter(runs)))
            self._child_finished_lock.notify_all()
            self._child_finished_generation += 1

    def _child_status_outcome(self, child: SessionRecord) -> ChildOutcome:
        status: Literal["completed", "failed", "interrupted", "cancelled", "running", "waiting"]
        stop_payload = self.core.store.latest_stop_payload(child.session_id, child.run_id)
        if stop_payload is None and child.run_id == "":
            # The reserved-claim bookkeeping marker ("") is not a real run, so the
            # run-scoped stop lookup cannot see it; read the claim event itself.
            stop_event = self.core.store.latest_event(child.session_id, EventType.RUN_INTERRUPTED)
            if stop_event is not None:
                stop_payload = stop_event.payload
        stop_cause = None if stop_payload is None else stop_payload.get("stop_cause")
        if not isinstance(stop_cause, str):
            stop_cause = None
        error = None
        if child.status is SessionStatus.INTERRUPTED and stop_payload is not None:
            reason = stop_payload.get("reason")
            error = reason if isinstance(reason, str) else None
        if child.status is SessionStatus.COMPLETED:
            status = "completed"
        elif child.status is SessionStatus.FAILED:
            status = "failed"
        elif child.status is SessionStatus.WAITING:
            status = "waiting"
        elif child.status is SessionStatus.RUNNING:
            status = "running"
        elif child.status is SessionStatus.INTERRUPTED:
            status = "cancelled" if stop_cause in ("user_stop", "owner_cancel") else "interrupted"
        else:
            status = "interrupted"
        budget_exhaustion = (
            BudgetExhaustion.from_payload(stop_payload.get("budget_exhaustion"))
            if child.status is SessionStatus.INTERRUPTED and stop_payload is not None
            else None
        )
        return ChildOutcome(
            child_session_id=child.session_id,
            status=status,
            run_id=child.run_id,
            stop_cause=stop_cause,
            error=error,
            budget_exhaustion=budget_exhaustion,
        )

    def _child_wait_outcome(self, child: SessionRecord) -> ChildOutcome:
        if child.status is SessionStatus.WAITING:
            return self._child_status_outcome(child)
        return ChildOutcome(child_session_id=child.session_id, status="running", run_id=child.run_id)

    def _child_outcome(self, child_id: str, payload: Mapping[str, object]) -> ChildOutcome:
        return _decode_child_outcome(child_id, payload)

    def _child_run_label(self, parent_id: str | None, child_id: str, run_id: str, events: Sequence[StoredEvent]) -> str:
        """Label of one run: the follow-up payload's label, else the spawn label."""
        for event in reversed(events):
            if event.event_type != EventType.CONTINUATION_STARTED or event.run_id != run_id:
                continue
            inner = event.payload.get("payload")
            inner_map = inner if isinstance(inner, Mapping) else {}
            label = inner_map.get("label")
            return label if isinstance(label, str) else ""
        return self._child_label(parent_id, child_id) if parent_id is not None else ""

    def _notify_requested(self, session: SessionRecord, events: Sequence[StoredEvent], *, run_id: str | None = None) -> bool:
        """Whether one child run asked for a completion notice, from durable facts only."""
        target = run_id or session.run_id
        for event in reversed(events):
            if event.event_type != EventType.CONTINUATION_STARTED or event.run_id != target:
                continue
            inner = event.payload.get("payload")
            inner_map = inner if isinstance(inner, Mapping) else {}
            if inner_map.get("kind") == "child_followup":
                return bool(inner_map.get("notify_on_completion", True))
        parent = session.parent_session_id
        if parent is not None:
            spawns = self.core.store.events_by_types(parent, (EventType.CHILD_SPAWNED,))
            for event in reversed(spawns):
                if event.event_type == EventType.CHILD_SPAWNED and event.payload.get("child_session_id") == session.session_id:
                    return bool(event.payload.get("notify_on_completion", False))
        return False

    @staticmethod
    def _terminal_child_event(run_events: Sequence[StoredEvent]) -> tuple[ChildRunStatus, StoredEvent] | None:
        """The genuine terminal event of one run; late same-run control rows never qualify."""
        for event in reversed(run_events):
            if event.event_type == EventType.RUN_COMPLETED:
                return "completed", event
            if event.event_type == EventType.RUN_FAILED:
                return "failed", event
            if event.event_type == EventType.RUN_INTERRUPTED:
                raw_cause = event.payload.get("stop_cause")
                if raw_cause in ("user_stop", "owner_cancel"):
                    return "cancelled", event
        return None

    def _child_run_facts(self, session: SessionRecord, run_id: str, events: Sequence[StoredEvent]) -> _ChildRunFacts | None:
        """Rebuild ONE terminal run from its durable events; parked/nonterminal runs return None."""
        run_events = [event for event in events if event.run_id == run_id]
        if not run_events:
            return None
        terminal_fact = self._terminal_child_event(run_events)
        if terminal_fact is None:
            return None
        status, terminal = terminal_fact
        output: str | None = None
        output_error: str | None = None
        error: str | None = None
        stop_cause: StopCause = "completed" if status == "completed" else "error"
        if status == "completed":
            for event in reversed(run_events):
                if event.event_type != EventType.MODEL_TURN:
                    continue
                wire = event.payload.get("result")
                if not isinstance(wire, Mapping):
                    output_error = f"committed provider result for child run {run_id!r} is not an object: {type(wire).__name__}"
                else:
                    try:
                        output = decode_provider_result(wire).output
                    except (TypeError, ValueError) as exc:
                        output_error = f"{type(exc).__name__}: {exc}"
                break
        elif status == "failed":
            error = str(terminal.payload.get("error") or "run failed")
        else:
            error = str(terminal.payload.get("reason") or "run interrupted")
            raw_cause = terminal.payload.get("stop_cause")
            if is_stop_cause(raw_cause):
                stop_cause = raw_cause
            else:
                stop_cause = "owner_cancel"
        parent_id = session.parent_session_id
        return _ChildRunFacts(
            status=status,
            output=output,
            error=error,
            stop_cause=stop_cause,
            label=self._child_run_label(parent_id, session.session_id, run_id, events),
            notify=self._notify_requested(session, events, run_id=run_id),
            budget_exhaustion=BudgetExhaustion.from_payload(terminal.payload.get("budget_exhaustion")),
            output_error=output_error,
        )

    def _reconstruct_child_outcome(self, child: SessionRecord) -> _ChildRunFacts | None:
        """Rebuild the current terminal run's outcome from durable facts; never rerun child work."""
        if not child.run_id:
            return None
        events = self.core.store.events(child.session_id)
        return self._child_run_facts(child, child.run_id, events)


def _utf8_preview(text: str, limit: int) -> str:
    """A bounded UTF-8 preview with an honest marker, never a mojibake cut."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = limit
    while cut > 0 and cut < len(raw) and raw[cut] & 0xC0 == 0x80:
        cut -= 1
    return raw[:cut].decode("utf-8") + f"\n[preview clipped; {len(raw)} bytes total]"


def _child_run_status(
    result: CoreResult,
) -> ChildRunStatus:
    """Map one terminal/parked run result to the child outcome vocabulary used by the parent."""
    if result.status in ("completed", "idle"):
        return "completed"
    if result.status == "failed":
        return "failed"
    if result.status == "waiting":
        return "waiting"
    if result.stop_cause in ("user_stop", "owner_cancel"):
        return "cancelled"
    return "interrupted"


def _terminal_run_result(result: CoreResult) -> bool:
    """Whether this run can never continue (waiting and recoverable interruptions are parked)."""
    if result.status in ("completed", "idle", "failed"):
        return True
    return result.status == "interrupted" and result.stop_cause in ("user_stop", "owner_cancel")


@dataclass(frozen=True, slots=True)
class _ChildRunFacts:
    """One terminal child run rebuilt from durable events; never a second truth."""

    status: ChildRunStatus
    output: str | None
    error: str | None
    stop_cause: StopCause | None
    label: str
    notify: bool
    budget_exhaustion: BudgetExhaustion | None
    output_error: str | None = None


_TERMINAL_CHILD_FACT_STATUSES: tuple[str, ...] = ("completed", "failed", "interrupted", "cancelled")


def _required_fact_str(payload: Mapping[str, object], field: str, subject: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise StoreConflict(f"{subject} has no valid {field}: {value!r}")
    return value


def _optional_fact_str(payload: Mapping[str, object], field: str, subject: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoreConflict(f"{subject} has a non-string {field}: {value!r}")
    return value


def _optional_fact_int(payload: Mapping[str, object], field: str, subject: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise StoreConflict(f"{subject} has an invalid {field}: {value!r}")
    return value


def _decode_child_outcome(child_id: str, payload: Mapping[str, object]) -> ChildOutcome:
    """Decode one committed terminal fact strictly; malformed attribution fails visibly."""
    subject = f"committed child completion fact for {child_id!r}"
    fact_child = _required_fact_str(payload, "child_session_id", subject)
    if fact_child != child_id:
        raise StoreConflict(f"{subject} names child {fact_child!r}")
    status = _required_fact_str(payload, "status", subject)
    if status not in _TERMINAL_CHILD_FACT_STATUSES:
        raise StoreConflict(f"{subject} has an unknown status: {status!r}")
    run_id = _required_fact_str(payload, "run_id", subject)
    stop_cause = _optional_fact_str(payload, "stop_cause", subject)
    if stop_cause is not None and not is_stop_cause(stop_cause):
        raise StoreConflict(f"{subject} has an unknown stop cause: {stop_cause!r}")
    raw_history = payload.get("history")
    if raw_history is None:
        history: Mapping[str, object] = {}
    elif isinstance(raw_history, Mapping):
        history = raw_history
    else:
        raise StoreConflict(f"{subject} has a non-object history: {raw_history!r}")
    truncated = history.get("truncated", False)
    if type(truncated) is not bool:
        raise StoreConflict(f"{subject} has a non-boolean history truncation: {truncated!r}")
    return ChildOutcome(
        child_session_id=child_id,
        status=cast(Literal["completed", "failed", "interrupted", "cancelled"], status),
        output=_optional_fact_str(payload, "output", subject),
        error=_optional_fact_str(payload, "error", subject),
        stop_cause=stop_cause,
        run_id=run_id,
        history_sha256=_optional_fact_str(history, "sha256", subject),
        history_bytes=_optional_fact_int(history, "bytes", subject),
        history_truncated=truncated,
        output_preview=_optional_fact_str(payload, "output_preview", subject),
        output_sha256=_optional_fact_str(payload, "output_sha256", subject),
        output_bytes=_optional_fact_int(payload, "output_bytes", subject),
        output_error=_optional_fact_str(payload, "output_error", subject),
        history_error=_optional_fact_str(payload, "history_error", subject),
        budget_exhaustion=BudgetExhaustion.from_payload(payload.get("budget_exhaustion")),
    )


def _terminal_capture_kind(
    *,
    status: str,
    output: str | None,
    output_preview: str | None,
    output_sha256: str | None,
    output_error: str | None,
) -> TerminalCaptureKind:
    """Classify one terminal fact's captured output from typed fields, never error text."""
    if status != "completed":
        return "not_completed"
    if output_sha256:
        return "artifact"
    if output:
        return "inline"
    if output_preview:
        return "preview_only"
    if output_error:
        return "capture_failed"
    return "empty"


def _decode_terminal_run_capture(child_id: str, event: StoredEvent) -> TerminalRunCapture:
    """Decode the newest committed terminal fact exactly; a malformed payload fails visibly."""
    payload = event.payload
    subject = f"committed child terminal fact for {child_id!r} at sequence {event.sequence}"
    fact_child = _required_fact_str(payload, "child_session_id", subject)
    if fact_child != child_id:
        raise StoreConflict(f"{subject} names child {fact_child!r}")
    status = _required_fact_str(payload, "status", subject)
    if status not in _TERMINAL_CHILD_FACT_STATUSES:
        raise StoreConflict(f"{subject} has an unknown status: {status!r}")
    run_id = _required_fact_str(payload, "run_id", subject)
    stop_cause = _optional_fact_str(payload, "stop_cause", subject)
    if stop_cause is not None and not is_stop_cause(stop_cause):
        raise StoreConflict(f"{subject} has an unknown stop cause: {stop_cause!r}")
    output = _optional_fact_str(payload, "output", subject)
    output_preview = _optional_fact_str(payload, "output_preview", subject) or None
    output_sha256 = _optional_fact_str(payload, "output_sha256", subject) or None
    output_error = _optional_fact_str(payload, "output_error", subject) or None
    history_error = _optional_fact_str(payload, "history_error", subject) or None
    return TerminalRunCapture(
        run_id=run_id,
        terminal_sequence=event.sequence,
        status=cast(Literal["completed", "failed", "interrupted", "cancelled"], status),
        capture=_terminal_capture_kind(
            status=status,
            output=output,
            output_preview=output_preview,
            output_sha256=output_sha256,
            output_error=output_error,
        ),
        stop_cause=stop_cause,
        output_sha256=output_sha256,
        output_bytes=_optional_fact_int(payload, "output_bytes", subject),
        output_preview=output_preview,
        output_error=output_error,
        history_error=history_error,
    )
