"""Spawn authorization, idempotent child creation, and the initial child driver."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from uuid import uuid4

from ...sdk.child_schema import CHILD_PROMPT_MAX_BYTES, canonical_result_schema, compose_child_prompt
from ...sdk.json_values import json_wire_object
from ...sdk.services import (
    BudgetExhaustion,
)
from ..events import EventType
from ..storage.store import (
    MAX_CHILD_LABEL_BYTES,
    EventDraft,
    FencedSubtree,
    SessionRecord,
    SessionStatus,
    StoreConflict,
    canonical_json,
)
from ._base import DELEGATION_MAX_DEPTH
from ._outcomes import _child_run_status, _terminal_run_result
from ._publication import _DelegationPublication


class _DelegationSpawn(_DelegationPublication):
    """Spawn authorization, idempotent child creation, and the initial child driver."""

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
        from ..engine.governance import validate_steps

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
            # The accepted spawn's exact driver input. Without it a restart can only
            # guess the work behind an idle reservation, so recovery must refuse.
            "prompt": effective_prompt,
            "max_steps": max_steps,
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
            # A recovery may have published a terminal outcome between the caller's
            # reservation and this submission. Only a still-pristine child starts.
            child = self.core.store.session(child_id)
            if child.status is not SessionStatus.IDLE or child.run_id is not None:
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
