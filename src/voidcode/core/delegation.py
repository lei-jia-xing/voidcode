"""Child delegation orchestration.

Houses child session lifecycle: spawn, join, send, cancel, completion publications,
durable facts, notice delivery, and continuation merging.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from ..sdk.child_schema import CHILD_PROMPT_MAX_BYTES, canonical_result_schema, compose_child_prompt
from ..sdk.codec import decode_provider_result
from ..sdk.hooks import StopCause, is_stop_cause
from ..sdk.json_values import json_wire_object
from ..sdk.services import BudgetExhaustion, CancelSubtreeReceipt, ChildOutcome, ChildSendReceipt, ChildSessionSnapshot, DescendantNode
from .cancellation import RUN_BOUND
from .child_contracts import ChildCompletionReceipt, ChildFinishedNoticePayload, ChildRunStatus, ChildWaitDelivery
from .engine.governance import validate_steps
from .events import CHILD_MESSAGE_OWNER, CHILD_OUTCOME_RECEIPT_OWNER, EventType, child_completion_notice_id
from .history_renderer import iter_event_blocks
from .storage.store import (
    MAX_CHILD_LABEL_BYTES,
    CheckpointKind,
    ContinuationRecord,
    ContinuationStatus,
    EventDraft,
    FencedSubtree,
    SequenceConflict,
    SessionRecord,
    SessionStatus,
    SqliteStore,
    StagedContinuationDraft,
    StoreConflict,
    StoredEvent,
    canonical_json,
)

if TYPE_CHECKING:
    from .engine import Core, _Execution
    from .engine.turn_execution import CoreResult


#: A parent may nest children at most this deep. A child inherits the parent's policy
#: unchanged, so delegation never widens it; the chain-wide child count is governed by
#: the operator-configurable ExecutionConfig.max_root_children.
DELEGATION_MAX_DEPTH = 3
#: Larger terminal child output is represented by a full artifact reference and bounded preview;
#: this threshold is an inline representation policy, not a transport-frame or total-output cap.
CHILD_INLINE_OUTPUT_BYTES = 32 * 1024
#: Preview carried by a completion notice when the output is artifact-backed.
CHILD_OUTPUT_PREVIEW_BYTES = 4 * 1024
#: Max concurrent child drivers and notice deliveries on the shared pool.
_DRIVER_MAX_WORKERS = 32
#: Child-fact publication retries a moved parent tip this many times before leaving
#: the terminal child session for the startup recovery pass. Backoff between attempts
#: is uniform(0, 50ms), (0, 100ms), … capped at 2s; anything that is not a tip race
#: fails fast instead of spinning.
_CHILD_FACT_MAX_ATTEMPTS = 8
_CHILD_FACT_BACKOFF_BASE_SECONDS = 0.05
_CHILD_FACT_BACKOFF_CAP_SECONDS = 2.0


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


class DelegationManager:
    """Orchestrates child delegation lifecycle, publications, and notices."""

    def __init__(self, core: Core) -> None:
        self.core = core
        #: In-process outcome cache: {child_id: {run_id: payload}}. The durable parent fact is
        #: restart truth.
        self._child_finished: dict[str, dict[str, dict[str, object]]] = {}
        self._child_finished_lock = threading.Condition()
        self._child_finished_generation = 0
        #: One durable publication per (child, run); a later recovery pass is idempotent.
        self._published_child_runs: set[tuple[str, str]] = set()
        #: (child, run) whose terminal fact/artifact is currently being produced.
        self._child_publication_inflight: set[tuple[str, str]] = set()
        #: Child ids this process already drives. A keyed retry that lands here must not
        #: start a second driver for a child whose first thread simply has not run yet;
        #: only a fresh process (empty set) may repair the crash window by starting one.
        #: ponytail: unbounded, one small id per spawn this process drove; cap it if a
        #: long-lived core ever drives enough children for this to matter.
        self._started_children: set[str] = set()
        #: Subtree targets currently being cancelled, with an install count so
        #: overlapping cancels of the same target never lift each other's fence.
        #: Install/release happens under the lifecycle lock; registration
        #: transactions consult the guard under the store lock over a
        #: caller-captured ancestor chain, so no store call happens under the
        #: lifecycle lock and lock order never inverts.
        self._lifecycle_lock = threading.Lock()
        self._cancel_fences: dict[str, int] = {}
        #: One bounded pool for child drivers and notice deliveries instead of bare
        #: threads. close() cancels queued work; in-flight drivers run to completion.
        self._driver_executor = ThreadPoolExecutor(max_workers=_DRIVER_MAX_WORKERS, thread_name_prefix="DelegationDriver")

    def close(self) -> None:
        with self._child_finished_lock:
            self._child_finished_generation += 1
            self._child_finished_lock.notify_all()
        self._driver_executor.shutdown(wait=False, cancel_futures=True)

    def notify_child_state_changed(self) -> None:
        """Wake first-ready waiters after a durable child or parent-input transition."""
        with self._child_finished_lock:
            self._child_finished_generation += 1
            self._child_finished_lock.notify_all()

    def _check_child_wait_interrupt(self, parent_session_id: str, admission_guard: Callable[[], bool] | None) -> None:
        self.core.ensure_open()
        if admission_guard is not None and not admission_guard():
            raise CancelledError("child wait was cancelled")
        if self.core.store.has_pending_user_message(parent_session_id):
            raise InterruptedError("child wait was interrupted by queued user input")

    def spawn_child(
        self,
        parent_session_id: str,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        owner: str = "",
        idempotency_key: str | None = None,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
        admission_guard: Callable[[], bool] | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        """Create and start a delegated child session under `parent_session_id`."""
        canonical_schema: dict[str, object] | None = None
        if result_schema is not None:
            if not isinstance(result_schema, Mapping):
                raise ValueError("result_schema must be an object or null")
            canonical_schema = canonical_result_schema(result_schema)
        effective_prompt = compose_child_prompt(prompt, canonical_schema)
        if not isinstance(prompt, str) or not prompt.strip() or len(effective_prompt.encode("utf-8")) > CHILD_PROMPT_MAX_BYTES:
            # A core guard, not a product budget: a delegated prompt may carry a whole
            # review diff, so owners enforce their own tighter caps before calling here.
            # The bound applies to the effective body (base text plus any delivery hint).
            raise ValueError(f"child prompt must be nonempty UTF-8 text within {CHILD_PROMPT_MAX_BYTES // 1024} KiB")
        if not isinstance(label, str) or len(label.encode("utf-8")) > MAX_CHILD_LABEL_BYTES:
            raise ValueError(f"child birth label must be UTF-8 text within {MAX_CHILD_LABEL_BYTES // 1024} KiB")
        if type(notify_on_completion) is not bool:
            raise ValueError("notify_on_completion must be a boolean")
        validate_steps(max_steps)
        self.core.ensure_open()
        parent = self.core.session(parent_session_id)
        depth = parent.depth + 1
        if depth > DELEGATION_MAX_DEPTH:
            raise ValueError(f"delegation depth limit exceeded: depth {depth} exceeds max {DELEGATION_MAX_DEPTH}")
        budget = self.core.config.max_root_children
        scope = self._child_tool_scope(parent, tool_allowlist)
        if not isinstance(root_action_id, str) or not root_action_id or "\0" in root_action_id:
            raise ValueError("captured root action identity is required at child reservation")
        if initial_root_budget is not None and (type(initial_root_budget) is not int or initial_root_budget < 0):
            raise ValueError("captured root action budget must be a non-negative integer or null")
        existing_birth = None
        if idempotency_key is None:
            child_id = uuid4().hex
            retry_root_id = None
            retry_root_budget = None
        else:
            child_id = self._keyed_child_id(parent.session_id, owner, idempotency_key)
            existing_birth = self._birth_record(child_id)
            retry_root_id = None if existing_birth is None else existing_birth.get("root_id")
            retry_root_budget = None if existing_birth is None else existing_birth.get("root_action_budget")
            if existing_birth is not None and "root_action_budget" not in existing_birth:
                raise StoreConflict("keyed child birth has no captured root budget state")
            if existing_birth is not None and (not isinstance(retry_root_id, str) or not retry_root_id):
                raise ValueError("keyed child birth has no captured root action identity")
        if retry_root_id is not None and retry_root_id != root_action_id:
            raise StoreConflict("keyed child retry carries a different captured root action identity")
        if existing_birth is not None and retry_root_budget != initial_root_budget:
            raise StoreConflict("keyed child retry carries a different captured root action budget")
        root_id = root_action_id
        root_cap = initial_root_budget
        root_state = self.core.store.root_budget_state(root_id)
        if root_cap is not None and (root_state is None or root_state[0] < root_cap):
            raise StoreConflict("captured finite root authorization has no matching durable budget row")
        if root_cap is None and root_state is not None:
            raise StoreConflict("captured uncapped root authorization conflicts with a finite budget row")
        spec_hash = self._spawn_spec_hash(
            prompt,
            scope,
            max_steps,
            bool(readonly),
            root_id,
            root_cap,
            label,
            notify_on_completion,
            result_schema=canonical_schema,
        )
        if idempotency_key is not None:
            recovery = self._recover_keyed_spawn(
                parent.session_id,
                child_id,
                depth=depth,
                label=label,
                scope=scope,
                max_steps=max_steps,
                readonly=bool(readonly),
                notify_on_completion=notify_on_completion,
                spec_hash=spec_hash,
                prompt=effective_prompt,
                owner=owner,
                idempotency_key=idempotency_key,
                root_id=root_id,
            )
            if recovery is not None:
                return recovery
        birth: dict[str, object] = {
            "child_session_id": child_id,
            "parent_session_id": parent.session_id,
            "root_id": root_id,
            "root_action_budget": root_cap,
            "label": label,
            "depth": depth,
            "tool_allowlist": list(scope) if scope is not None else None,
            "notify_on_completion": notify_on_completion,
            "readonly": bool(readonly),
        }
        if canonical_schema is not None:
            birth["result_schema"] = canonical_schema
        if idempotency_key is not None:
            birth.update(
                {
                    "spawn_owner": owner,
                    "idempotency_key": idempotency_key,
                    "spec_hash": spec_hash,
                }
            )
        # Capture the immutable ancestor chain and fast pre-check; the
        # authoritative fence decision happens inside the store transaction
        # via _guard_fence, so a fence installed between here and commit still
        # refuses this spawn.
        chain = self._chain_of(parent_session_id)
        with self._lifecycle_lock:
            fenced = self._fence_holds(chain)
            if fenced is not None:
                raise PermissionError(f"session {parent_session_id!r} is inside subtree {fenced!r} under cancellation")
        try:
            self.core.store.create_child_session(
                child_id,
                str(self.core.workspace),
                self.core.revision_id,
                parent_session_id=parent.session_id,
                root_session_id=parent.root_session_id,
                depth=depth,
                tool_allowlist=scope,
                max_root_children=budget,
                birth=birth,
                caller_chain=chain,
                admission_guard=admission_guard,
            )
            self.core.notify_commit()
        except FencedSubtree as exc:
            raise PermissionError(str(exc)) from exc
        except sqlite3.IntegrityError:
            collided = True
        else:
            collided = False
        if collided:
            # Lost a same-key race: the winner owns the id, so re-enter the recovery path
            # (spec-mismatched retries still refuse there). Only keyed ids can collide.
            if idempotency_key is None:
                raise sqlite3.IntegrityError("child session id collision")
            recovery = self._recover_keyed_spawn(
                parent.session_id,
                child_id,
                depth=depth,
                label=label,
                scope=scope,
                max_steps=max_steps,
                readonly=bool(readonly),
                notify_on_completion=notify_on_completion,
                spec_hash=spec_hash,
                prompt=effective_prompt,
                owner=owner,
                idempotency_key=idempotency_key,
                root_id=root_id,
            )
            if recovery is None:  # pragma: no cover - the colliding row vanished mid-race
                raise sqlite3.IntegrityError("child session id collision")
            return recovery
        if readonly:
            # Core-owned stance record: the child stays restricted even if the parent's
            # own overlay is later cleared, and no plugin can revoke it.
            self.core.store.set_session_overlay(child_id, "core")
        spawned_payload: dict[str, object] = {
            "child_session_id": child_id,
            "root_id": root_id,
            "root_action_budget": root_cap,
            "label": label,
            "depth": depth,
            "tool_allowlist": list(scope) if scope is not None else None,
            "notify_on_completion": notify_on_completion,
        }
        if canonical_schema is not None:
            spawned_payload["result_schema"] = canonical_schema
        if idempotency_key is not None:
            spawned_payload["spawn_owner"] = owner
            spawned_payload["idempotency_key"] = idempotency_key
            spawned_payload["spec_hash"] = spec_hash
        try:
            self.core.commit_public_fact(
                parent.session_id,
                EventDraft(EventType.CHILD_SPAWNED, "core", "plugin", spawned_payload),
                root_action_id=root_id,
                initial_root_budget=root_cap,
            )
        except StoreConflict:
            if self._spawned_fact(parent.session_id, child_id) is None:
                raise
        self._start_child_run(parent.session_id, child_id, effective_prompt, max_steps, label, notify_on_completion)
        return child_id

    @staticmethod
    def _keyed_child_id(parent_session_id: str, owner: str, idempotency_key: str) -> str:
        """The deterministic child id for one parent, owner, and key; a core identity, not a plugin hash."""
        encoded = canonical_json({"parent": parent_session_id, "owner": owner, "key": idempotency_key})
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _spawn_spec_hash(
        prompt: str,
        scope: tuple[str, ...] | None,
        max_steps: int | None,
        readonly: bool,
        root_id: str,
        root_action_budget: int | None,
        label: str,
        notify_on_completion: bool,
        *,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        """Fingerprint stable child spec and captured root authorization, never usage counters.

        The schema slot is always present — a declared schema or an explicit null —
        so declared and undeclared retries never collide.
        """
        spec: dict[str, object] = {
            "prompt": prompt,
            "scope": None if scope is None else list(scope),
            "max_steps": max_steps,
            "readonly": readonly,
            "root_id": root_id,
            "root_action_budget": root_action_budget,
            "label": label,
            "notify_on_completion": notify_on_completion,
            "result_schema": None if result_schema is None else json_wire_object(result_schema),
        }
        encoded = canonical_json(spec)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _spawned_fact(self, parent_session_id: str, child_id: str) -> dict[str, object] | None:
        """The durable spawn fact for one child, if the crash window already committed it."""
        spawns = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_SPAWNED,))
        for event in reversed(spawns):
            if event.event_type == EventType.CHILD_SPAWNED and event.payload.get("child_session_id") == child_id:
                return dict(event.payload)
        return None

    def _birth_record(self, child_id: str) -> dict[str, object] | None:
        """The child's own atomic birth record (sequence 1), or None when it is missing.

        None covers both a child that predates birth records and a session id with no
        row at all: a first keyed spawn probes its deterministic id before creating it.
        The birth record commits in the same transaction as the child row, so it is the
        crash-window truth when the parent fact never landed. It is read-only here.
        """
        try:
            first = self.core.store.events(child_id, limit=1)
        except KeyError:
            return None
        for event in first:
            if event.sequence == 1 and event.event_type == EventType.CHILD_RESERVED:
                return dict(event.payload)
        return None

    def _captured_child_root_id(self, child_id: str) -> str:
        """Return the immutable root action identity committed at child reservation."""
        birth = self._birth_record(child_id)
        root_id = None if birth is None else birth.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict(f"child {child_id!r} has no captured root action identity")
        return root_id

    def _root_action_id_for_send(self, child: SessionRecord) -> str:
        """Child sends retain the root authorization captured at the child's birth."""
        return self._captured_child_root_id(child.session_id)

    def _keyed_child(self, parent_session_id: str, owner: str, idempotency_key: str) -> str | None:
        """The child id already mapped under one parent, owner, and key; None when unmapped.

        The parent fact is the fast path; when a crash landed between the child row and
        the parent fact, the child's own birth record verifies the mapping instead.
        """
        child_id = self._keyed_child_id(parent_session_id, owner, idempotency_key)
        try:
            child = self.core.store.session(child_id)
        except KeyError:
            return None
        if child.parent_session_id != parent_session_id:
            return None
        fact = self._spawned_fact(parent_session_id, child_id)
        if fact is not None:
            if fact.get("spawn_owner") != owner or fact.get("idempotency_key") != idempotency_key:
                return None
            return child_id
        birth = self._birth_record(child_id)
        if birth is None:
            return None
        if birth.get("spawn_owner") != owner or birth.get("idempotency_key") != idempotency_key:
            return None
        return child_id

    def find_keyed_child(self, parent_session_id: str, owner: str, idempotency_key: str) -> str | None:
        """Public read-only mapping lookup: bound to this parent and owner, and it never creates."""
        return self._keyed_child(parent_session_id, owner, idempotency_key)

    def _recover_keyed_spawn(
        self,
        parent_session_id: str,
        child_id: str,
        *,
        depth: int,
        label: str,
        scope: tuple[str, ...] | None,
        max_steps: int | None,
        readonly: bool,
        notify_on_completion: bool,
        spec_hash: str,
        prompt: str,
        owner: str,
        idempotency_key: str,
        root_id: str,
    ) -> str | None:
        """Return the existing keyed child when the id already maps; None means create it now.

        A retry with a differing spec is refused visibly. A crash that left a pristine idle
        child without its spawn fact (or without its first run) is repaired by committing the
        fact and starting the run; a child that already started work is returned as-is and its
        unknown mutations are never replayed.
        """
        try:
            child = self.core.store.session(child_id)
        except KeyError:
            return None
        if child.parent_session_id != parent_session_id:
            raise ValueError("spawn idempotency key collides with another parent's child")
        fact = self._spawned_fact(parent_session_id, child_id)
        if fact is not None:
            if fact.get("spawn_owner") != owner or fact.get("idempotency_key") != idempotency_key:
                raise ValueError("spawn idempotency key collides with another owner's child")
            if fact.get("root_id") != root_id:
                raise ValueError("spawn idempotency key was already used with a different root action authorization")
            if fact.get("spec_hash") != spec_hash:
                raise ValueError("spawn idempotency key was already used with a different spec")
            if "notify_on_completion" not in fact:
                raise KeyError(f"spawn fact for child {child_id!r} is missing notify_on_completion")
            pristine = child.status is SessionStatus.IDLE and child.run_id is None
            if pristine:
                repaired: dict[str, object] = dict(fact)
                repaired["notify_on_completion"] = notify_on_completion
                root_budget = repaired.get("root_action_budget")
                if root_budget is not None and type(root_budget) is not int:
                    raise StoreConflict("spawn fact has an invalid captured root budget")
                # A concurrent same-key retry may have committed the fact first; adoption
                # is the idempotent result, and the winner starts the single initial run.
                try:
                    self.core.commit_public_fact(
                        parent_session_id,
                        EventDraft(EventType.CHILD_SPAWNED, "core", "plugin", repaired),
                        root_action_id=root_id,
                        initial_root_budget=root_budget,
                    )
                except StoreConflict:
                    if self._spawned_fact(parent_session_id, child_id) is None:
                        raise
                # Fresh process repairing the crash window; the start claim itself is
                # atomic, so a retry in the spawning process never starts a second driver.
                self._start_child_run(parent_session_id, child_id, prompt, max_steps, label, notify_on_completion)
            return child_id
        # Session row without a parent fact: the crash window. Verify the birth record
        # FIRST — even a non-pristine child must prove this owner and spec before it is
        # returned, and a row with neither fact nor birth cannot prove its origin, so it
        # fails visibly instead of being adopted on a depth/scope guess.
        birth = self._birth_record(child_id)
        if birth is None:
            raise ValueError("spawn idempotency key maps to a child with no verifiable origin; refusing to adopt it")
        if birth.get("spawn_owner") != owner or birth.get("idempotency_key") != idempotency_key:
            raise ValueError("spawn idempotency key collides with another owner's child")
        if birth.get("root_id") != root_id:
            raise ValueError("spawn idempotency key was already used with a different root action authorization")
        if birth.get("spec_hash") != spec_hash:
            raise ValueError("spawn idempotency key was already used with a different spec")
        if birth.get("readonly") is True and "read-only" not in self.core.store.session_overlays(child_id):
            self.core.store.set_session_overlay(child_id, "core")
        if child.status is not SessionStatus.IDLE or child.run_id is not None:
            return child_id
        if readonly:
            self.core.store.set_session_overlay(child_id, "core")
        birth_budget = birth.get("root_action_budget")
        if birth_budget is not None and type(birth_budget) is not int:
            raise StoreConflict("child birth has an invalid captured root budget")
        try:
            self.core.commit_public_fact(
                parent_session_id,
                EventDraft(
                    EventType.CHILD_SPAWNED,
                    "core",
                    "plugin",
                    {
                        "child_session_id": child_id,
                        "root_id": root_id,
                        "label": label,
                        "depth": depth,
                        "tool_allowlist": list(scope) if scope is not None else None,
                        "notify_on_completion": notify_on_completion,
                        "spawn_owner": owner,
                        "idempotency_key": idempotency_key,
                        "spec_hash": spec_hash,
                    },
                ),
                root_action_id=root_id,
                initial_root_budget=birth_budget,
            )
        except StoreConflict:
            if self._spawned_fact(parent_session_id, child_id) is None:
                raise
        self._start_child_run(parent_session_id, child_id, prompt, max_steps, label, notify_on_completion)
        return child_id

    def _start_child_run(
        self,
        parent_session_id: str,
        child_id: str,
        prompt: str,
        max_steps: int | None,
        label: str,
        notify_on_completion: bool,
    ) -> bool:
        """Submit one child driver run; False when this process already drives it.

        The check-and-claim is atomic under the publication lock, so concurrent retries
        of one key start exactly one initial run. Follow-up runs go through `send_child`
        and never consult this set.
        """
        with self._child_finished_lock:
            if child_id in self._started_children:
                return False
            self._started_children.add(child_id)
        self._driver_executor.submit(self._run_child, parent_session_id, child_id, prompt, max_steps, label, notify_on_completion)
        return True

    def _child_tool_scope(self, parent: SessionRecord, requested: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Validate a requested child tool scope: known names, and never wider than the parent."""
        if requested is None:
            return parent.tool_allowlist
        if not isinstance(requested, (list, tuple)) or not requested:
            raise ValueError("tool_allowlist must be a non-empty sequence of tool names")
        names = tuple(sorted({str(name) for name in requested}))
        unknown = [name for name in names if self.core._snapshot.definition_of(name) is None]
        if unknown:
            raise ValueError(f"tool_allowlist names an unknown tool: {unknown[0]}")
        if parent.tool_allowlist is not None:
            widened = sorted(set(names) - set(parent.tool_allowlist))
            if widened:
                raise ValueError(f"tool_allowlist widens the parent scope: {widened[0]}")
        return names

    def _run_child(
        self,
        parent_session_id: str,
        child_id: str,
        prompt: str,
        max_steps: int | None,
        label: str,
        notify_on_completion: bool,
    ) -> None:
        """Drive one spawn run. Waiting is NOT terminal: only a later resume/cancel publishes."""
        run_id = ""
        budget_exhaustion: BudgetExhaustion | None = None
        try:
            root_id = self._captured_child_root_id(child_id)
            result = self.core._run(
                prompt,
                session_id=child_id,
                max_steps=max_steps,
                prompt_max_bytes=CHILD_PROMPT_MAX_BYTES,
                _root_id=root_id,
                _refuse_reserved_cancelled=True,
            )
            run_id = result.run_id
            status = _child_run_status(result)
            output, error, stop_cause = result.output, result.error, result.stop_cause
            budget_exhaustion = result.budget_exhaustion
            terminal = _terminal_run_result(result)
        except Exception as exc:
            try:
                run_id = self.core.store.session(child_id).run_id or ""
            except Exception:
                run_id = ""
            if not run_id:
                return
            status, output, error, stop_cause = "failed", None, f"{type(exc).__name__}: {exc}", "error"
            terminal = True
        if status == "waiting" or not terminal:
            # Waiting is parked at an authentic approval checkpoint, and a recoverable
            # interruption keeps its durable resume checkpoint. A public resume/cancel later
            # publishes the real terminal result exactly once; this thread must not guess one.
            self.notify_child_state_changed()
            return
        self._publish_child_terminal(
            parent_session_id,
            child_id,
            run_id=run_id,
            status=status,
            output=output,
            error=error,
            stop_cause=stop_cause,
            label=label,
            notify=notify_on_completion,
            deliver=True,
            budget_exhaustion=budget_exhaustion,
        )

    def wait_children(
        self,
        parent_session_id: str,
        child_session_ids: Sequence[str],
        *,
        admission_guard: Callable[[], bool] | None = None,
    ) -> tuple[ChildWaitDelivery, ...]:
        """Deliver exact undelivered child facts, then wait for entry-bound work."""
        self.core.ensure_open()
        if isinstance(child_session_ids, (str, bytes)) or not isinstance(child_session_ids, Sequence):
            raise ValueError("child_session_ids must be a sequence of session ids")
        selected: list[str] = []
        seen_children: set[str] = set()
        for child_id in child_session_ids:
            if not isinstance(child_id, str) or not child_id:
                raise ValueError("child_session_ids must contain non-empty strings")
            if child_id in seen_children:
                continue
            self._owned_child(parent_session_id, child_id)
            seen_children.add(child_id)
            selected.append(child_id)
        if not selected:
            self._check_child_wait_interrupt(parent_session_id, admission_guard)
            return ()

        registrations = self.core.store.child_wait_registrations(tuple(selected))
        captured_messages: dict[str, set[str]] = {}
        bound_runs: set[tuple[str, str]] = set()
        initial_spawn_thresholds: dict[str, int] = {}
        approval_resume_thresholds: dict[str, int] = {}
        child_seen_sequences: dict[str, int] = {}
        parent_events = list(self.core.store.events(parent_session_id))
        parent_seen_sequence = max((event.sequence for event in parent_events), default=0)
        spawned_ids = {
            child_id
            for event in parent_events
            if event.event_type == EventType.CHILD_SPAWNED and isinstance((child_id := event.payload.get("child_session_id")), str)
        }

        def bind_child_events(child_id: str, events: Sequence[StoredEvent]) -> set[str]:
            started_message_ids: set[str] = set()
            continuation_runs = {event.run_id for event in events if event.event_type == EventType.CONTINUATION_STARTED and event.run_id}
            for event in events:
                if event.event_type == EventType.CONTINUATION_STARTED:
                    message_id = event.payload.get("continuation_id")
                    if isinstance(message_id, str) and message_id in captured_messages[child_id]:
                        started_message_ids.add(message_id)
                        if event.run_id:
                            bound_runs.add((child_id, event.run_id))
                elif (
                    event.event_type == EventType.USER_MESSAGE
                    and child_id in initial_spawn_thresholds
                    and event.sequence > initial_spawn_thresholds[child_id]
                    and event.run_id
                    and event.run_id not in continuation_runs
                ):
                    # The spawn fact reserves this first child run. A later queued
                    # follow-up is bound only by its own continuation identity.
                    bound_runs.add((child_id, event.run_id))
                    initial_spawn_thresholds.pop(child_id, None)
                elif (
                    event.event_type == EventType.RUN_RESUMED
                    and child_id in approval_resume_thresholds
                    and event.sequence > approval_resume_thresholds[child_id]
                    and event.run_id
                ):
                    # A public approval resume is a durable continuation of the exact
                    # waiting checkpoint captured at entry, with its actual new run id.
                    bound_runs.add((child_id, event.run_id))
            return started_message_ids

        started_messages: set[tuple[str, str]] = set()
        for child, message_ids in registrations:
            if child.parent_session_id != parent_session_id:
                raise PermissionError("child session does not belong to this parent session")
            captured_messages[child.session_id] = set(message_ids)
            if isinstance(child.run_id, str) and child.run_id:
                bound_runs.add((child.session_id, child.run_id))
            if child.status is SessionStatus.WAITING and isinstance(child.run_id, str) and child.run_id:
                approval_resume_thresholds[child.session_id] = child.last_event_sequence
            elif (
                child.run_id is None
                and child.session_id in spawned_ids
                and child.status
                not in (
                    SessionStatus.COMPLETED,
                    SessionStatus.FAILED,
                    SessionStatus.INTERRUPTED,
                )
            ):
                initial_spawn_thresholds[child.session_id] = child.last_event_sequence
            child_events = self.core.store.events(child.session_id)
            started_messages.update((child.session_id, message_id) for message_id in bind_child_events(child.session_id, child_events))
            child_seen_sequences[child.session_id] = max(
                child.last_event_sequence,
                max((event.sequence for event in child_events), default=0),
            )

        with self._child_finished_lock:
            generation = self._child_finished_generation
        selected_set = set(selected)
        while True:
            self._check_child_wait_interrupt(parent_session_id, admission_guard)
            for child_id in selected:
                new_events = self.core.store.events(child_id, after_sequence=child_seen_sequences[child_id])
                if new_events:
                    child_seen_sequences[child_id] = max(event.sequence for event in new_events)
                    started_messages.update((child_id, message_id) for message_id in bind_child_events(child_id, new_events))
            new_parent_events = self.core.store.events(parent_session_id, after_sequence=parent_seen_sequence)
            if new_parent_events:
                parent_events.extend(new_parent_events)
                parent_seen_sequence = max(event.sequence for event in new_parent_events)

            started_notice_ids = {
                notice_id
                for event in parent_events
                if event.event_type == EventType.CONTINUATION_STARTED and isinstance((notice_id := event.payload.get("continuation_id")), str)
            }
            deliveries: list[ChildWaitDelivery] = []
            for event in parent_events:
                if event.event_type != EventType.CHILD_FINISHED:
                    continue
                child_id = event.payload.get("child_session_id")
                run_id = event.payload.get("run_id")
                if child_id not in selected_set:
                    continue
                if not isinstance(child_id, str) or not isinstance(run_id, str) or not run_id:
                    raise StoreConflict("committed child completion fact has no exact child/run identity")
                if self.core.store.has_observer_receipt(parent_session_id, event.sequence, CHILD_OUTCOME_RECEIPT_OWNER):
                    continue
                notice_id = event.payload.get("notice_id")
                if notice_id is None and event.payload.get("notify_on_completion") is True:
                    notice_id = child_completion_notice_id(parent_session_id, child_id, run_id)
                if notice_id is not None and (not isinstance(notice_id, str) or not notice_id):
                    raise StoreConflict("committed child completion fact has an invalid notice identity")
                if notice_id is not None and (notice_id in started_notice_ids or self.core.store.continuation_started(parent_session_id, notice_id)):
                    continue
                status = event.payload.get("status")
                if status not in ("completed", "failed", "interrupted", "cancelled"):
                    raise StoreConflict(f"committed child completion fact has an unknown status: {status!r}")
                outcome = self._child_outcome(child_id, event.payload)
                if outcome.run_id != run_id:
                    raise StoreConflict("committed child completion fact changed its run identity")
                deliveries.append(
                    ChildWaitDelivery(
                        outcome=outcome,
                        source_session_id=parent_session_id,
                        event_sequence=event.sequence,
                        notice_id=notice_id,
                    )
                )

            for child_id in selected:
                child = self.core.store.session(child_id)
                run_id = child.run_id
                if child.status is not SessionStatus.INTERRUPTED or not isinstance(run_id, str) or not run_id:
                    continue
                if (child_id, run_id) not in bound_runs:
                    continue
                stop_event = self.core.store.latest_stop_event(child_id, run_id)
                if stop_event is None:
                    continue
                stop_cause = stop_event.payload.get("stop_cause")
                if stop_cause in ("owner_cancel", "user_stop"):
                    continue
                if self.core.store.has_observer_receipt(child_id, stop_event.sequence, CHILD_OUTCOME_RECEIPT_OWNER):
                    continue
                budget_payload = stop_event.payload.get("budget_exhaustion")
                budget_exhaustion = BudgetExhaustion.from_payload(budget_payload)
                if budget_payload is not None and budget_exhaustion is None:
                    raise StoreConflict("committed child stop has malformed budget exhaustion facts")
                reason = stop_event.payload.get("reason")
                outcome = ChildOutcome(
                    child_session_id=child_id,
                    status="interrupted",
                    error=reason if isinstance(reason, str) else None,
                    stop_cause=stop_cause if isinstance(stop_cause, str) else None,
                    run_id=run_id,
                    budget_exhaustion=budget_exhaustion,
                )
                deliveries.append(
                    ChildWaitDelivery(
                        outcome=outcome,
                        source_session_id=child_id,
                        event_sequence=stop_event.sequence,
                        notice_id=None,
                    )
                )
            if deliveries:
                self._check_child_wait_interrupt(parent_session_id, admission_guard)
                return tuple(deliveries)

            pending = False
            for child_id, run_id in bound_runs:
                child = self.core.store.session(child_id)
                if child.run_id != run_id:
                    continue
                if child.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
                    pending = True
                    break
                if child.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
                    if self._child_fact(parent_session_id, child_id, run_id) is None:
                        pending = True
                        break
                elif child.status is SessionStatus.INTERRUPTED:
                    stop_event = self.core.store.latest_stop_event(child_id, run_id)
                    if stop_event is None:
                        pending = True
                        break
                    stop_cause = stop_event.payload.get("stop_cause")
                    if stop_cause in ("owner_cancel", "user_stop") and self._child_fact(parent_session_id, child_id, run_id) is None:
                        pending = True
                        break
                elif child.status is not SessionStatus.IDLE:
                    raise StoreConflict(f"child session has an unknown wait status: {child.status.value!r}")
            if not pending:
                for child_id, message_ids in captured_messages.items():
                    for message_id in message_ids:
                        if (child_id, message_id) in started_messages:
                            continue
                        status = self.core.store.continuation_status(message_id)
                        if status in (ContinuationStatus.PENDING, ContinuationStatus.CLAIMED):
                            pending = True
                            break
                        if status not in (
                            None,
                            ContinuationStatus.COMPLETED,
                            ContinuationStatus.FAILED,
                            ContinuationStatus.CANCELLED,
                        ):
                            raise StoreConflict(f"child message has an unknown continuation state: {status.value!r}")
                    if pending:
                        break
            if not pending:
                for child_id in initial_spawn_thresholds:
                    child = self.core.store.session(child_id)
                    if child.run_id is None and child.status not in (
                        SessionStatus.COMPLETED,
                        SessionStatus.FAILED,
                        SessionStatus.INTERRUPTED,
                    ):
                        pending = True
                        break
                    if (
                        child.status in (SessionStatus.COMPLETED, SessionStatus.FAILED)
                        and child.run_id
                        and self._child_fact(parent_session_id, child_id, child.run_id) is None
                    ):
                        pending = True
                        break
            self._check_child_wait_interrupt(parent_session_id, admission_guard)
            if not pending:
                # Rebind child-side reservations, starts, resumes, and stop events that
                # raced the live-state check before making an empty-work decision.
                child_events_changed = False
                for child_id in selected:
                    new_child_events = self.core.store.events(child_id, after_sequence=child_seen_sequences[child_id])
                    if new_child_events:
                        child_seen_sequences[child_id] = max(event.sequence for event in new_child_events)
                        started_messages.update((child_id, message_id) for message_id in bind_child_events(child_id, new_child_events))
                        child_events_changed = True
                if child_events_changed:
                    continue
                # A completion may commit after the first parent-event scan but before the
                # live-child check observes its terminal fact. Recheck the durable cursor
                # before deciding that no work is ready; notifications follow the commit.
                newer_parent_events = self.core.store.events(parent_session_id, after_sequence=parent_seen_sequence)
                if newer_parent_events:
                    parent_events.extend(newer_parent_events)
                    parent_seen_sequence = max(event.sequence for event in newer_parent_events)
                    continue
                with self._child_finished_lock:
                    self._check_child_wait_interrupt(parent_session_id, admission_guard)
                    if generation != self._child_finished_generation:
                        generation = self._child_finished_generation
                        continue
                    return ()
            with self._child_finished_lock:
                if generation != self._child_finished_generation:
                    generation = self._child_finished_generation
                    continue
                self._child_finished_lock.wait()
                generation = self._child_finished_generation

    def join_child(self, parent_session_id: str, child_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        """Wait for the child's CURRENT run; never satisfy a newer join with an older fact."""
        self.core.ensure_open()
        self._owned_child(parent_session_id, child_id)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._child_finished_lock:
            generation = self._child_finished_generation
        parent_seen_sequence: int | None = None

        def published_fact(run_id: str | None) -> dict[str, object] | None:
            """This run's published fact: memory cache first, then the parent history past its cursor."""
            nonlocal parent_seen_sequence
            fact = self._latest_child_fact(child_id, run_id)
            if fact is not None:
                return fact
            # The registry is only a cache: the durable parent fact is the restart truth.
            # Past the first scan only new parent events can carry an uncached publish:
            # a current run's fact cannot be evicted while it stays current, since 32
            # newer same-child publishes would have advanced the run past it.
            fresh = self.core.store.events(parent_session_id, after_sequence=parent_seen_sequence or 0)
            if fresh:
                parent_seen_sequence = max(event.sequence for event in fresh)
            elif parent_seen_sequence is not None:
                return None
            else:
                parent_seen_sequence = 0
            return self._durable_child_fact(parent_session_id, child_id, run_id)

        while True:
            child = self.core.store.session(child_id)
            if child.status is SessionStatus.WAITING:
                # Approval is not terminal and not something a parent wait can resolve.
                return self._child_status_outcome(child)
            if child.status is SessionStatus.INTERRUPTED and not self.core.admits_fresh_work(child):
                # A recoverable interruption needs an explicit public resume, not a join.
                # A terminal cancellation still publishes a fact (error/history); prefer it.
                fact = published_fact(child.run_id)
                if fact is not None:
                    return self._child_outcome(child_id, fact)
                with self._child_finished_lock:
                    publishing = child.run_id is not None and (child_id, child.run_id) in self._child_publication_inflight
                if publishing:
                    if deadline - time.monotonic() <= 0:
                        return self._child_status_outcome(child)
                    generation = self._await_child_wake(deadline, generation)
                    continue
                return self._child_status_outcome(child)
            if not self._child_has_outstanding_work(child):
                fact = published_fact(child.run_id)
                if fact is not None:
                    return self._child_outcome(child_id, fact)
                with self._child_finished_lock:
                    publishing = child.run_id is not None and (child_id, child.run_id) in self._child_publication_inflight
                terminal = child.status in (SessionStatus.COMPLETED, SessionStatus.FAILED)
                if not publishing and not terminal:
                    return self._child_status_outcome(child)
                # A terminal session whose fact/artifacts are still in flight is not a
                # completed outcome. Wait for the publisher; a deadline returns the honest
                # nonterminal wait outcome instead of completed/output=None.
            if deadline - time.monotonic() <= 0:
                return self._child_wait_outcome(child)
            generation = self._await_child_wake(deadline, generation)

    def _await_child_wake(self, deadline: float, generation: int) -> int:
        """Sleep until a child-state notify or the deadline; returns the observed generation.

        A notify that landed before this call is never slept through: the caller
        re-reads durable state and only sleeps on a still-current generation, so a
        wake is never lost and a spurious wake just re-reads unchanged state.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return generation
        with self._child_finished_lock:
            if generation != self._child_finished_generation:
                return self._child_finished_generation
            self._child_finished_lock.wait(timeout=remaining)
            return self._child_finished_generation

    def acknowledge_child_completion(self, parent_session_id: str, child_id: str, run_id: str) -> ChildCompletionReceipt | None:
        """Stage common receipt identity only for this run's durable terminal fact."""
        self.core.ensure_open()
        self._owned_child(parent_session_id, child_id)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        event = self._child_fact_event(parent_session_id, child_id, run_id)
        if event is None:
            return None
        status = event.payload.get("status")
        if status not in ("completed", "failed", "interrupted", "cancelled"):
            raise StoreConflict(f"committed child completion fact has an unknown status: {status!r}")
        fact_run = event.payload.get("run_id")
        if fact_run != run_id:
            raise StoreConflict("committed child completion fact changed its run identity")
        notice_id = event.payload.get("notice_id")
        if notice_id is None and event.payload.get("notify_on_completion") is True:
            notice_id = child_completion_notice_id(parent_session_id, child_id, run_id)
        if notice_id is not None and (not isinstance(notice_id, str) or not notice_id):
            raise StoreConflict("committed child completion fact has an invalid notice identity")
        if notice_id is not None and self.core.store.continuation_started(parent_session_id, notice_id):
            notice_id = None
        return ChildCompletionReceipt(
            source_session_id=event.session_id,
            event_sequence=event.sequence,
            notice_id=notice_id,
        )

    def send_child(
        self,
        parent_session_id: str,
        child_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
        admission_guard: Callable[[], bool] | None = None,
    ) -> ChildSendReceipt:
        """Send one attributed follow-up to a direct child, in the child's own session.

        The message is a durable continuation, never a filesystem mutation or a user-authority
        action. A running or approval-waiting child consumes it at its next committed batch
        boundary; an open child reserves a run identity and starts. A recoverably interrupted
        child keeps the message durable but cannot claim it without an explicit resume, so the
        receipt says ``parked`` instead of promising a boundary. Scope and stance can never widen:
        a parent that is now read-only may not steer a child that is not durably read-only.
        """
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > CHILD_PROMPT_MAX_BYTES:
            raise ValueError(f"child prompt must be nonempty UTF-8 text within {CHILD_PROMPT_MAX_BYTES // 1024} KiB")
        if type(notify_on_completion) is not bool:
            raise ValueError("notify_on_completion must be a boolean")
        self.core.ensure_open()
        parent = self.core.session(parent_session_id)
        child = self._owned_child(parent_session_id, child_id)
        self.core.check_session(child)
        if self.core.session_read_only(parent.session_id) and not self.core.session_read_only(child.session_id):
            raise PermissionError("a read-only parent cannot steer a child that is not durably read-only")
        message_id = uuid4().hex
        attributed = "[Follow-up from your parent agent; delegated agent input, not a direct user message]\n\n" + prompt
        root_id = self._root_action_id_for_send(child)
        # Fast pre-check on a captured chain; the authoritative fence decision
        # happens inside the store transaction via _guard_fence.
        chain = self._chain_of(parent_session_id)
        with self._lifecycle_lock:
            fenced = self._fence_holds(chain, child_id)
            if fenced is not None:
                raise PermissionError(f"session {child_id!r} is inside subtree {fenced!r} under cancellation")
        try:
            self.core.store.enqueue_continuation(
                message_id,
                CHILD_MESSAGE_OWNER,
                child_id,
                "core",
                run_id=parent.run_id or uuid4().hex,
                revision_id=self.core.revision_id,
                payload={
                    "kind": "child_followup",
                    "text": attributed,
                    "prompt": prompt,
                    "label": label,
                    "notify_on_completion": notify_on_completion,
                },
                root_id=root_id,
                role="user",
                caller_chain=chain,
                admission_guard=admission_guard,
            )
        except FencedSubtree as exc:
            raise PermissionError(str(exc)) from exc
        outstanding = child.status in (SessionStatus.RUNNING, SessionStatus.WAITING)
        delivery: Literal["started", "queued", "parked"] = "queued"
        started_run: str | None = None
        if not outstanding:
            run_id = uuid4().hex
            try:
                claimed, _session = self.core.store.claim_and_start_continuation(
                    child_id,
                    run_id,
                    uuid4().hex,
                    revision_id=self.core.revision_id,
                    initial_root_budget=None,
                    continuation_id=message_id,
                    caller_chain=chain,
                )
                self.core.notify_commit()
            except FencedSubtree:
                # The fence won the race: the message stays queued and the
                # canceller's disarm sweep owns it. The receipt honestly says queued.
                pass
            except StoreConflict as exc:
                # A loss to a concurrent claim stays queued behind that run. An interrupted
                # child with a non-terminal checkpoint cannot claim follow-ups without an
                # explicit resume, so its receipt must not promise a future boundary.
                current = self.core.store.session(child_id)
                if current.status is SessionStatus.INTERRUPTED and not self.core.admits_fresh_work(current):
                    delivery = "parked"
                elif self.core.store.continuation_status(message_id) not in (
                    ContinuationStatus.PENDING,
                    ContinuationStatus.CLAIMED,
                ):
                    raise StoreConflict(f"follow-up {message_id!r} is no longer durably claimable") from exc
            else:
                # The receipt names the run the store actually claimed, never an unused token.
                delivery, started_run = "started", claimed.run_id
        self._driver_executor.submit(self._deliver_child_messages, child_id, message_id)
        status = "running" if delivery == "started" else self.core.store.session(child_id).status
        self.notify_child_state_changed()
        return ChildSendReceipt(
            child_session_id=child_id,
            delivery=delivery,
            child_status=status,
            run_id=started_run,
            message_id=message_id,
        )

    def cancel_child(self, parent_session_id: str, child_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel a child (executing, parked, reserved-but-unstarted, or with queued follow-ups).

        A send that overlaps this call may legally linearize after the pending sweep and start
        new work. Cancellation therefore publishes only the run identity it observed, never a
        newer run that a concurrent follow-up legitimately claimed. A reserved child whose
        first run never started is claimed with an owner-cancel interruption, so a late
        driver thread can never start it afterwards.
        """
        self._owned_child(parent_session_id, child_id)
        target_run = self.core.store.session(child_id).run_id
        if target_run is None and self._claim_reserved_child(child_id, reason):
            return True
        cancelled = self.core.cancel_session(child_id, reason, cause="owner_cancel")
        pending = self.core.store.cancel_pending_continuations(child_id, CHILD_MESSAGE_OWNER)
        if cancelled or pending > 0:
            # The sweep above commits after cancel_session's notify; wake joiners past it.
            self.notify_child_state_changed()
        child = self.core.store.session(child_id)
        if child.parent_session_id is not None and target_run and child.run_id == target_run:
            facts = self._reconstruct_child_outcome(child)
            if facts is not None and not self._child_fact_exists(parent_session_id, child_id, target_run):
                self._publish_child_terminal(
                    parent_session_id,
                    child_id,
                    run_id=target_run,
                    status=facts.status,
                    output=facts.output,
                    error=facts.error,
                    stop_cause=facts.stop_cause,
                    label=facts.label,
                    notify=facts.notify,
                    budget_exhaustion=facts.budget_exhaustion,
                    output_error=facts.output_error,
                    deliver=False,
                )
        return cancelled or pending > 0

    def _owned_child(self, parent_session_id: str, child_id: str) -> SessionRecord:
        child = self.core.session(child_id)
        if child.parent_session_id != parent_session_id:
            raise PermissionError("child session does not belong to this parent session")
        return child

    def list_descendants(self, session_id: str) -> tuple[DescendantNode, ...]:
        """Every transitive descendant of one session with verbatim lifecycle facts."""
        self.core.ensure_open()
        caller = self.core.session(session_id)
        timestamps = self.core.store.descendant_timestamps(session_id)
        now_ms = int(time.time() * 1000)
        nodes: list[DescendantNode] = []
        for child in self.core.store.descendant_sessions(session_id):
            if child.workspace != caller.workspace:
                continue
            label = self._child_label(child.parent_session_id or "", child.session_id) if child.parent_session_id else ""
            # latest_stop_cause(run_id) returns None for the "" bookkeeping
            # marker; a claimed-reserved node reports its committed owner_cancel.
            stop_cause = self.core.store.latest_stop_cause(child.session_id, child.run_id or None) or ("owner_cancel" if child.run_id == "" else None)
            queued = self.core.store.has_queued_child_followup(child.session_id)
            tool_fact = self.core.store.latest_tool_fact(child.session_id)
            activity = None if tool_fact is None else f"{tool_fact[0]} {tool_fact[1]}"
            created_ms, updated_ms = timestamps.get(child.session_id, (None, None))
            elapsed_ms: int | None = None
            if created_ms is not None and updated_ms is not None:
                if child.status in (SessionStatus.IDLE, SessionStatus.RUNNING, SessionStatus.WAITING):
                    elapsed_ms = max(0, now_ms - created_ms)
                else:
                    elapsed_ms = max(0, updated_ms - created_ms)
            nodes.append(
                DescendantNode(
                    session_id=child.session_id,
                    parent_session_id=child.parent_session_id,
                    depth=child.depth,
                    status=child.status.value,
                    # The reserved-claim bookkeeping marker ("") is not a real
                    # run: project it as null so detail/cancel bindings stay
                    # valid and a claimed parent with descendants stays selectable.
                    run_id=child.run_id or None,
                    stop_cause=stop_cause,
                    label=label or None,
                    activity=activity,
                    turn=child.turn,
                    last_event_sequence=child.last_event_sequence,
                    has_queued_followup=queued,
                    awaiting_approval=child.status is SessionStatus.WAITING,
                    started_at_ms=created_ms,
                    updated_at_ms=updated_ms,
                    elapsed_ms=elapsed_ms,
                )
            )
        return tuple(nodes)

    def read_child_snapshot(self, caller_session_id: str, child_session_id: str) -> ChildSessionSnapshot | None:
        """Read one caller-visible descendant and its latest eligible committed result.

        Non-visible and nonexistent ids share ``None``. Terminal child events are append-only;
        only a successful publication carrying nonempty inline output or a full output artifact
        replaces the latest result. Failed, interrupted, cancelled, empty, and preview-only
        publications do not erase a prior result. Inline committed output is materialized as a
        deduplicated immutable artifact for resource paging; the committed event and join result
        stay unchanged. The event contract has no separate OMP-style accepted-yield marker.
        """
        self.core.ensure_open()
        initial_descendant = next(
            (node for node in self.list_descendants(caller_session_id) if node.session_id == child_session_id),
            None,
        )
        if initial_descendant is None:
            return None

        # Use the first scoped projection for authorization and the stable parent lookup. The
        # current-run projection is refreshed after reading committed publications so a result
        # published between these reads cannot be presented as newer than the sampled run.
        parent_session_id = initial_descendant.parent_session_id

        latest_outcome: ChildOutcome | None = None
        if parent_session_id is not None:
            finished = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_FINISHED,))
            for event in reversed(finished):
                payload = event.payload
                if event.event_type != EventType.CHILD_FINISHED or payload.get("child_session_id") != child_session_id:
                    continue
                output = payload.get("output")
                output_artifact = payload.get("output_sha256")
                accepted = payload.get("status") == "completed" and (
                    isinstance(output, str) and bool(output) or isinstance(output_artifact, str) and bool(output_artifact)
                )
                if accepted:
                    latest_outcome = self._child_outcome(child_session_id, payload)
                    if latest_outcome.output_sha256 is None and latest_outcome.output is not None:
                        reference = self._spill_child_output(latest_outcome.output)
                        digest = reference.get("sha256")
                        if not isinstance(digest, str) or not digest:
                            raise RuntimeError("inline child output did not produce an immutable artifact reference")
                        latest_outcome = replace(latest_outcome, output_sha256=digest)
                    break

        descendant = next(
            (node for node in self.list_descendants(caller_session_id) if node.session_id == child_session_id),
            None,
        )
        if descendant is None:
            return None
        return ChildSessionSnapshot(descendant=descendant, latest_outcome=latest_outcome)

    def _fence_holds(self, chain: frozenset[str], child_id: str | None = None) -> str | None:
        """The active fence blocking spawn/send through this chain, if any (caller holds the lifecycle lock)."""
        fences = self._cancel_fences
        if child_id is not None and child_id in fences:
            return child_id
        for fenced in fences:
            if fenced in chain:
                return fenced
        return None

    def _chain_of(self, session_id: str) -> frozenset[str]:
        """One session's ancestor chain snapshot (outside any store transaction)."""
        try:
            record = self.core.store.session(session_id)
        except KeyError:
            return frozenset((session_id,))
        return frozenset((session_id, *self._parent_chain(record)))

    def _guard_fence(self, chain: frozenset[str], child_id: str | None) -> str | None:
        """Store-transaction fence check: in-memory fence set plus a captured chain."""
        with self._lifecycle_lock:
            return self._fence_holds(chain, child_id)

    def attach_store(self, store: SqliteStore) -> None:
        """Install the in-transaction fence guard on the owning store (called once by Core)."""
        store._cancel_fence_guard = self._guard_fence  # noqa: SLF001 - core owns the store seam

    def cancel_descendant_subtree(
        self, bound_session_id: str, target_session_id: str, *, expected_run_id: str | None, reason: str = "owner cancellation"
    ) -> CancelSubtreeReceipt:
        self.core.ensure_open()
        bound = self.core.session(bound_session_id)
        try:
            target = self.core.store.session(target_session_id)
        except KeyError as exc:
            raise PermissionError("cancellation target is not a descendant of this session") from exc
        if target.workspace != bound.workspace:
            raise PermissionError("cancellation target is not a descendant of this session")
        chain = self._parent_chain(target)
        if bound_session_id not in chain:
            raise PermissionError("cancellation target is not a descendant of this session")
        if (target.run_id or None) != expected_run_id:
            raise ValueError("cancellation target moved to a newer run; refresh and confirm again")
        # Registration takes store -> lifecycle. Install the fence without
        # reading the store under lifecycle, then census committed admissions.
        with self._lifecycle_lock:
            self._cancel_fences[target_session_id] = self._cancel_fences.get(target_session_id, 0) + 1
        try:
            descendants = self.core.store.descendant_sessions(target_session_id)
            if (self.core.store.session(target_session_id).run_id or None) != expected_run_id:
                raise ValueError("cancellation target moved to a newer run; refresh and confirm again")
            subtree = (target_session_id, *(child.session_id for child in descendants))
            observed_runs = {child.session_id: child.run_id or None for child in descendants}
            observed_runs[target_session_id] = expected_run_id
            cancelled: list[str] = []
            already_terminal: list[str] = []
            disarmed: list[str] = []
            claimed: list[str] = []
            moved: list[str] = []
            for node_id in reversed(subtree):
                record = self.core.store.session(node_id)
                observed_run = observed_runs.get(node_id)
                if (record.run_id or None) != observed_run:
                    moved.append(node_id)
                    continue
                # Disarm first so an already-enqueued follow-up cannot start a
                # new run after the snapshot below. The fence blocks new
                # enqueues; a newer run that still slips through is left alone
                # and reported distinctly below. No session lock is taken here:
                # a live run holds its own lock for the whole provider call,
                # and the bound signal below binds inside the abort registry.
                pending = self.core.store.cancel_pending_continuations(node_id, CHILD_MESSAGE_OWNER, expected_session=record)
                fresh = self.core.store.session(node_id)
                fresh_run = fresh.run_id or None
                if fresh_run != observed_run:
                    if pending:
                        disarmed.append(node_id)
                    moved.append(node_id)
                    continue
                if fresh.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
                    if pending:
                        disarmed.append(node_id)
                    else:
                        already_terminal.append(node_id)
                    continue
                if record.run_id is None and record.status is SessionStatus.IDLE:
                    # Reserved but unstarted: the lock-free CAS claim below wins
                    # only on the pristine state, so a racing first run either
                    # claims first (claim is moot) or loses its own CAS.
                    if self._claim_reserved_child(node_id, reason):
                        claimed.append(node_id)
                        continue
                    if (self.core.store.session(node_id).run_id or None) != observed_run:
                        moved.append(node_id)
                        continue
                stopped = self.core._cancel_session_run(node_id, reason, cause="owner_cancel", target=RUN_BOUND(observed_run))
                if not stopped and (self.core.store.session(node_id).run_id or None) != observed_run:
                    # The bound signal refused: a newer run claimed after the
                    # pre-signal read. It was never signalled; report distinctly.
                    moved.append(node_id)
                    continue
                if pending and not stopped:
                    disarmed.append(node_id)
                elif stopped:
                    cancelled.append(node_id)
                else:
                    already_terminal.append(node_id)
                child = self.core.store.session(node_id)
                if child.parent_session_id is not None and record.run_id and child.run_id == record.run_id:
                    facts = self._reconstruct_child_outcome(child)
                    if facts is not None and not self._child_fact_exists(child.parent_session_id, node_id, record.run_id):
                        self._publish_child_terminal(
                            child.parent_session_id,
                            node_id,
                            run_id=record.run_id,
                            status=facts.status,
                            output=facts.output,
                            error=facts.error,
                            stop_cause=facts.stop_cause,
                            label=facts.label,
                            notify=facts.notify,
                            deliver=False,
                            budget_exhaustion=facts.budget_exhaustion,
                            output_error=facts.output_error,
                        )
            return CancelSubtreeReceipt(
                target_session_id=target_session_id,
                expected_run_id=expected_run_id,
                cancelled=tuple(cancelled),
                already_terminal=tuple(already_terminal),
                disarmed_queued=tuple(disarmed),
                claimed_reserved=tuple(claimed),
                observed_newer_run=tuple(moved),
            )
        finally:
            with self._lifecycle_lock:
                remaining = self._cancel_fences.get(target_session_id, 0) - 1
                if remaining > 0:
                    self._cancel_fences[target_session_id] = remaining
                else:
                    self._cancel_fences.pop(target_session_id, None)

    def _parent_chain(self, record: SessionRecord) -> tuple[str, ...]:
        chain: list[str] = []
        current = record.parent_session_id
        seen = {record.session_id}
        while current is not None and current not in seen:
            chain.append(current)
            seen.add(current)
            try:
                current = self.core.store.session(current).parent_session_id
            except KeyError:
                break
        return tuple(chain)

    def _claim_reserved_child(self, child_id: str, reason: str) -> bool:
        """Claim a pristine reserved child with an owner-cancel interruption; False when it already ran.

        The claim is one lock-free CAS transaction guarded on the pristine
        state (idle, no run, expected sequence), so a racing first run either
        wins the CAS (and this claim is moot) or loses it. A claimed child
        reports cancelled and its late driver refuses to start; an explicit
        follow-up afterwards is still a legal fresh run. No session lock is
        taken: an initial driver may already hold it for its provider call.
        """
        child = self.core.store.session(child_id)
        if child.status is not SessionStatus.IDLE or child.run_id is not None:
            return False
        for event in self.core.store.events(child_id):
            if event.event_type == EventType.USER_MESSAGE:
                return False
        try:
            self.core.commit_public_staged(
                child_id,
                run_id="",
                status=SessionStatus.INTERRUPTED,
                events=(
                    EventDraft(
                        EventType.RUN_INTERRUPTED,
                        "core",
                        "core",
                        {
                            "reason": reason,
                            "stop_cause": "owner_cancel",
                            "logical_turn": "",
                            # A pristine reservation has no run to report: freeze an empty
                            # eligibility set rather than offering observers a run-less fact.
                            "eligible_owners": [],
                        },
                    ),
                ),
                checkpoint_kind=CheckpointKind.TERMINAL,
                checkpoint_payload={"step": 0, "stop_cause": "owner_cancel"},
            )
        except StoreConflict:
            return False
        self.notify_child_state_changed()
        return True

    def _child_has_outstanding_work(self, child: SessionRecord) -> bool:
        """A running child, or a follow-up queued for it (never unread notices or parked work)."""
        if child.status is SessionStatus.RUNNING:
            return True
        if child.status is SessionStatus.IDLE:
            # A just-created spawn has not committed its first run event yet.
            return True
        if child.status not in (SessionStatus.COMPLETED, SessionStatus.FAILED):
            return False
        for continuation in self.core.store.pending_continuations(child.session_id, limit=50):
            if continuation.payload.get("kind") == "child_followup":
                return True
        return False

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

    # --- Terminal publication -------------------------------------------------

    def _publish_child_terminal(
        self,
        parent_session_id: str,
        child_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        output: str | None,
        error: str | None,
        stop_cause: StopCause | None,
        label: str,
        notify: bool,
        deliver: bool,
        budget_exhaustion: BudgetExhaustion | None = None,
        output_error: str | None = None,
    ) -> None:
        """Publish one terminal child run exactly once: fact + immutable history + notice.

        A waiting join returns the outcome only after the durable fact commits and the outcome is
        remembered; the durable fact is the restart truth, and a recovered publication is
        idempotent. Publication is claimed per (child, run).
        """
        if not run_id or status not in ("completed", "failed", "interrupted", "cancelled"):
            return
        key = (child_id, run_id)
        # A concurrent publication for this run is already working: join it instead of
        # silently skipping, so the caller never observes "done" before the fact lands.
        deadline = time.monotonic() + 30.0
        with self._child_finished_lock:
            while key in self._child_publication_inflight and time.monotonic() < deadline:
                self._child_finished_lock.wait(timeout=0.2)
            already_done = key in self._published_child_runs
            if not already_done:
                self._published_child_runs.add(key)
                self._child_publication_inflight.add(key)
                self._child_finished_lock.notify_all()
                self._child_finished_generation += 1
        claimed_here = not already_done
        try:
            existing = self._child_fact(parent_session_id, child_id, run_id)
            if existing is not None:
                self._remember_child_outcome(child_id, run_id, existing)
                return
            if already_done:
                # Winner still working past the deadline (or lost the race after it):
                # never double-commit; retry paths observe the terminal child again.
                return
            resolved_label = label or self._child_label(parent_session_id, child_id)
            notice_root_id: str | None = None
            notice_id: str | None = None
            if notify:
                notice_root_id = self._captured_child_root_id(child_id)
                notice_id = child_completion_notice_id(parent_session_id, child_id, run_id)
            payload = self._child_fact_payload(
                child_id,
                run_id=run_id,
                status=status,
                output=output,
                error=error,
                stop_cause=stop_cause,
                label=resolved_label,
                notify=notify,
                notice_id=notice_id,
                budget_exhaustion=budget_exhaustion,
                output_error=output_error,
            )
            notice = None
            if notify and notice_id is not None and notice_root_id is not None:
                notice = StagedContinuationDraft(
                    continuation_id=notice_id,
                    owner=CHILD_MESSAGE_OWNER,
                    source="core",
                    payload=self._child_notice_payload(
                        child_id,
                        run_id=run_id,
                        status=status,
                        stop_cause=stop_cause,
                        label=resolved_label,
                        output=output,
                        error=error,
                        payload=payload,
                        notice_id=notice_id,
                    ),
                    idempotency_key=f"child-finished:{child_id}:{run_id}",
                    ordinal=0,
                    root_id=notice_root_id,
                )
            committed = self._commit_child_fact(
                parent_session_id,
                EventDraft(EventType.CHILD_FINISHED, "core", "plugin", payload),
                notice,
            )
            if not committed:
                # No durable fact means no publishable outcome. Drop the in-process claim so
                # the next join/recovery path can retry instead of serving a cache-only result.
                with self._child_finished_lock:
                    self._published_child_runs.discard(key)
                return
            self._remember_child_outcome(child_id, run_id, payload)
            if deliver and notice_id is not None:
                self._deliver_child_notice(parent_session_id, notice_id)
        except Exception:
            # A failed publication is retried by the next path that observes the terminal child
            # (startup recovery, a later join, or a new run). Never lose the claim for retries.
            with self._child_finished_lock:
                if claimed_here:
                    self._published_child_runs.discard(key)
            raise
        finally:
            with self._child_finished_lock:
                if claimed_here:
                    self._child_publication_inflight.discard(key)
                self._child_finished_lock.notify_all()
                self._child_finished_generation += 1

    def _child_fact_payload(
        self,
        child_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        output: str | None,
        error: str | None,
        stop_cause: StopCause | None,
        label: str,
        notify: bool,
        notice_id: str | None,
        budget_exhaustion: BudgetExhaustion | None,
        output_error: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": child_id,
            "run_id": run_id,
            "status": status,
            "error": error,
            "stop_cause": stop_cause,
            "label": label,
            "notify_on_completion": notify,
        }
        if budget_exhaustion is not None:
            payload["budget_exhaustion"] = budget_exhaustion.as_payload()
        if notice_id is not None:
            payload["notice_id"] = notice_id
        if output_error is not None:
            payload["output_error"] = output_error
        if output is not None:
            encoded = output.encode("utf-8")
            payload["output_bytes"] = len(encoded)
            if len(encoded) <= CHILD_INLINE_OUTPUT_BYTES:
                payload["output"] = output
            else:
                payload["output_preview"] = _utf8_preview(output, CHILD_OUTPUT_PREVIEW_BYTES)
                try:
                    reference = self._spill_child_output(output)
                except Exception as exc:
                    payload["output_error"] = f"{type(exc).__name__}: {exc}"
                else:
                    payload["output_sha256"] = reference["sha256"]
                    payload["output_bytes"] = reference["bytes"]
        else:
            payload["output"] = None
        try:
            run_type_events = self.core.store.events_by_types(child_id, (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED))
            # The snapshot ends exactly at this run's genuine terminal event (prior history
            # included). A late control/progress row tagged with the same run id must not extend
            # an immutable snapshot past the terminal commit.
            run_events = [event for event in run_type_events if event.run_id == run_id]
            terminal = self._terminal_child_event(run_events)
            if terminal is None:
                raise ValueError(f"no genuine terminal event for child run {run_id!r}; refusing a whole-session snapshot")
            items = self.core.store.events(child_id, limit=terminal[1].sequence)
            header = (
                f"# Child session {child_id}\n\n"
                f"- run: {run_id or '(none)'}\n"
                f"- status: {status}\n"
                f"- label: {label or '(none)'}\n"
                f"- events: {len(items)}\n"
                f"- live view (paged): transcript://{child_id}\n\n"
            )
            spill_id, spill = self.core._artifacts.start_spill("text/markdown; charset=utf-8", owner="core")
            try:
                spill.append(header.encode("utf-8"))
                for index, block in enumerate(iter_event_blocks(items)):
                    separator = "\n\n" if index else ""
                    spill.append(f"{separator}{block}".encode())
                sha256, size, media_type = self.core._artifacts.finish(spill_id)
            except Exception:
                self.core._artifacts.discard(spill_id)
                raise
            payload["history"] = {
                "sha256": sha256,
                "bytes": size,
                "media_type": media_type,
                "events": len(items),
            }
        except Exception as exc:
            payload["history_error"] = f"{type(exc).__name__}: {exc}"
        return payload

    def _child_notice_payload(
        self,
        child_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        stop_cause: StopCause | None,
        label: str,
        output: str | None,
        error: str | None,
        payload: Mapping[str, object],
        notice_id: str,
    ) -> dict[str, object]:
        preview = payload.get("output_preview")
        if preview is None and output is not None:
            preview = _utf8_preview(output, CHILD_OUTPUT_PREVIEW_BYTES)
        raw_error = _utf8_preview(error, CHILD_OUTPUT_PREVIEW_BYTES) if error is not None else None
        output_error = payload.get("output_error")
        history_error = payload.get("history_error")
        output_sha256 = payload.get("output_sha256")
        history = payload.get("history")
        history_sha256 = history.get("sha256") if isinstance(history, Mapping) else None
        output_bytes = payload.get("output_bytes")
        notice = ChildFinishedNoticePayload(
            notice_id=notice_id,
            child_session_id=child_id,
            run_id=run_id,
            label=label,
            status=status,
            stop_cause=stop_cause,
            output_bytes=output_bytes if isinstance(output_bytes, int) else None,
            preview=str(preview) if preview is not None else None,
            output_sha256=str(output_sha256) if output_sha256 is not None else None,
            history_sha256=str(history_sha256) if history_sha256 is not None else None,
            output_error=str(output_error) if isinstance(output_error, str) and output_error else None,
            history_error=str(history_error) if isinstance(history_error, str) and history_error else None,
            error=raw_error,
        )
        return notice.to_mapping()

    def _spill_child_output(self, text: str) -> dict[str, object]:
        spill_id, spill = self.core._artifacts.start_spill("text/plain; charset=utf-8", owner="core")
        try:
            spill.append(text.encode("utf-8"))
            sha256, size, _media_type = self.core._artifacts.finish(spill_id)
        except Exception:
            self.core._artifacts.discard(spill_id)
            raise
        return {"sha256": sha256, "bytes": size}

    def _commit_child_fact(
        self,
        parent_session_id: str,
        event: EventDraft,
        notice: StagedContinuationDraft | None,
    ) -> bool:
        """Commit the fact and its notice atomically against the parent's fresh tip.

        Only a moved tip is retried, with bounded attempts and jittered backoff; a
        parent waiting in join must never be able to deadlock a child, so anything
        else fails fast and the next join/recovery path retries. The durable terminal
        child session remains for recovery if this process exits before the commit.
        """
        for attempt in range(1, _CHILD_FACT_MAX_ATTEMPTS + 1):
            if self.core._closed:
                return False
            session = self.core.store.session(parent_session_id)
            if session.active_revision_id != self.core.revision_id:
                return False
            try:
                self.core.commit_public_staged(
                    parent_session_id,
                    events=(event,),
                    revision_id=self.core.revision_id,
                    staged_continuations=(notice,) if notice is not None else (),
                )
                return True
            except SequenceConflict:
                if attempt >= _CHILD_FACT_MAX_ATTEMPTS or self.core._closed:
                    return False
                time.sleep(random.uniform(0.0, min(_CHILD_FACT_BACKOFF_CAP_SECONDS, _CHILD_FACT_BACKOFF_BASE_SECONDS * (2.0 ** (attempt - 1)))))
            except StoreConflict:
                return False
        return False

    def _child_fact(self, parent_session_id: str, child_id: str, run_id: str) -> dict[str, object] | None:
        finished = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_FINISHED,))
        for event in reversed(finished):
            if event.event_type != EventType.CHILD_FINISHED or event.payload.get("child_session_id") != child_id:
                continue
            fact_run = event.payload.get("run_id")
            if not isinstance(fact_run, str) or not fact_run:
                raise StoreConflict(f"committed child completion fact for {child_id!r} has no exact run identity")
            if fact_run == run_id:
                return dict(event.payload)
            # Another run's fact proves nothing about this run: keep scanning older facts.
            continue
        return None

    def _child_fact_event(self, parent_session_id: str, child_id: str, run_id: str) -> StoredEvent | None:
        """Find one exact terminal result event, never a current-session or latest-output substitute."""
        finished = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_FINISHED,))
        for event in reversed(finished):
            if event.event_type != EventType.CHILD_FINISHED or event.payload.get("child_session_id") != child_id:
                continue
            if event.payload.get("run_id") == run_id:
                return event
        return None

    def _durable_child_fact(self, parent_session_id: str, child_id: str, run_id: str | None) -> dict[str, object] | None:
        """Load one run's published fact from durable parent events; the registry is only a cache."""
        if run_id is None:
            return None
        payload = self._child_fact(parent_session_id, child_id, run_id)
        if payload is not None:
            self._remember_child_outcome(child_id, run_id, payload)
        return payload

    def _child_fact_exists(self, parent_session_id: str, child_id: str, run_id: str) -> bool:
        return self._child_fact(parent_session_id, child_id, run_id) is not None

    def _child_label(self, parent_session_id: str, child_id: str) -> str:
        spawns = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_SPAWNED,))
        for event in reversed(spawns):
            if event.event_type == EventType.CHILD_SPAWNED and event.payload.get("child_session_id") == child_id:
                label = event.payload.get("label")
                return label if isinstance(label, str) else ""
        return ""

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

    def _deliver_child_notice(self, session_id: str, continuation_id: str | None = None) -> None:
        """Wake an idle session; a busy one merges the message at its next batch boundary."""
        if not self.core.auto_drain:
            return
        try:
            if continuation_id is None:
                self.core.drive_pending(session_id, limit=32)
            else:
                self.core.drain_continuations(session_id, limit=1, continuation_id=continuation_id, _automatic=True)
        except Exception:
            # The message is durable; the next run, drain, or restart delivers it.
            return

    def _deliver_child_messages(self, session_id: str, continuation_id: str) -> None:
        """Deliver the exact follow-up a send reserved, even if another continuation is older."""
        try:
            self.core.drain_continuations(session_id, limit=1, continuation_id=continuation_id)
        except Exception:
            return

    def _merge_child_messages(self, execution: _Execution) -> int:
        """Claim messages into the CURRENT run after a committed batch, before next dispatch."""
        if execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
            return 0
        session_id = execution.session.session_id
        try:
            claimed = self.core.store.claim_child_messages(
                session_id,
                execution.run_id,
                revision_id=self.core.revision_id,
            )
            self.core.notify_commit()
        except StoreConflict:
            return 0
        if not claimed:
            # Ineligible (another run/revision/status) or no rows: keep this execution's
            # snapshot where it is; the next commit validates/reconciles the parked prefix.
            return 0
        # The claim inserts continuation_started at the current tip but returns only its own
        # rows; any control event committed before it (typically the atomic child fact +
        # notice) is durable but absent from that return. Reconcile the whole bounded range
        # to the post-claim tip, allowing exactly the claimed sequences.
        current = self.core.store.session(session_id)
        self.core._commit_pipeline.reconcile_execution_events(
            execution,
            upto=current.last_event_sequence,
            session=current,
            parked=True,
            allowed_sequences=frozenset(event.sequence for event in claimed),
        )
        return len(claimed)

    def _first_child_message(self, session_id: str) -> ContinuationRecord | None:
        """The oldest eligible core child message, excluding implicit stopped-session wakeups."""
        interrupted = self.core.store.session(session_id).status is SessionStatus.INTERRUPTED
        for records in (
            self.core.store.claimed_continuations(session_id, limit=100),
            self.core.store.pending_continuations(session_id, limit=100),
        ):
            for record in records:
                if record.owner == CHILD_MESSAGE_OWNER:
                    if interrupted and record.payload.get("kind") == "child_finished":
                        continue
                    return record
        return None
