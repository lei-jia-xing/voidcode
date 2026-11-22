"""Capability activation coordination: prepare, fence, and commit one generation swap.

The coordinator owns the revision logic — candidate admission, private snapshot pinning,
old-generation fact flush, state-copy planning, the atomic `store.commit_activation`
call, and the activation checkpoint carry-over. It never owns Core's dispatch fence,
runtime assembly, or publication: the host object implements the explicit
`ActivationHost` protocol below, so the dependency is declared instead of reaching into
Core's internals.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..sdk.json_values import json_wire_object
from ..sdk.plugin import StateScope
from .activation import (
    PRIVATE_DIR_NAME,
    Admission,
    ExecutionConfig,
    StateCopyPlan,
    load_private_snapshot,
    plan_state_copies,
    write_private_snapshot,
)
from .events import EventType
from .plugins import PluginRuntime
from .sandbox import SandboxBackend
from .storage.store import DATABASE_FILENAME, CheckpointKind, EventDraft, SessionRecord, SqliteStore, StoreConflict


class ActivationFenced(RuntimeError):
    """A dispatch or activation raced the activation fence; retry after it settles."""


class ActivationSnapshot(Protocol):
    """The immutable generation view the coordinator needs (structurally `engine._Snapshot`)."""

    @property
    def revision_id(self) -> str: ...

    @property
    def plugin_runtime(self) -> PluginRuntime: ...


@dataclass(frozen=True, slots=True)
class PreparedActivation:
    """A validated candidate generation, its state copies, and the generation it was prepared against."""

    admission: Admission
    plugin_runtime: PluginRuntime
    copies: StateCopyPlan
    source: str
    prepared_against: str


class ActivationHost(Protocol):
    """The core capabilities the activation coordinator may use, stated explicitly."""

    @property
    def workspace(self) -> Path: ...

    @property
    def store(self) -> SqliteStore: ...

    @property
    def config(self) -> ExecutionConfig: ...

    @property
    def state_dir(self) -> Path: ...

    @property
    def config_filename(self) -> str: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def snapshot(self) -> ActivationSnapshot | None: ...

    @property
    def admits(self) -> Callable[..., Admission] | None: ...

    def ensure_open(self) -> None: ...

    def session(self, session_id: str) -> SessionRecord: ...

    def begin_activation(self) -> None:
        """Enter the dispatch fence, refusing while another activation or dispatch is active."""
        ...

    def end_activation(self) -> None:
        """Leave the dispatch fence without publishing a new generation."""
        ...

    def park_background_drivers(self, reason: str) -> None: ...

    def build_candidate_runtime(self, admission: Admission, target_revision_id: str) -> PluginRuntime: ...

    def flush_old_generation_facts(self, deferred_to_candidate: frozenset[str]) -> None: ...

    def activation_checkpoint(self, payload: Mapping[str, object] | None, /, *, source: str, request_id: str) -> Mapping[str, object]:
        """Carry the session cursor across the switch, injecting a resume intent where owed."""
        ...

    def record_activation_refusal(
        self,
        session_id: str,
        request_id: str,
        source: str,
        error: str,
        blockers: Sequence[str],
    ) -> None: ...

    def notify_commit(self) -> None: ...

    def publish_activation(self, snapshot: ActivationSnapshot, prepared: PreparedActivation) -> None:
        """Swap the snapshot, release the fence, and retire the old generation."""
        ...


class ActivationCoordinator:
    """Prepare and commit one capability revision on behalf of a host core."""

    def __init__(self, host: ActivationHost) -> None:
        self._host = host

    def activate(self, *, session_id: str, source: str, request_id: str = "", target_revision_id: str = "") -> dict[str, object]:
        """Run one capability activation for an already-safe boundary."""
        host = self._host
        host.ensure_open()
        snapshot = host.snapshot
        if snapshot is None:
            raise RuntimeError("core is not initialized")
        session = host.session(session_id)
        request_id = request_id or uuid4().hex
        blockers = host.store.activation_blockers(str(host.workspace), session_id)
        if blockers:
            error = f"activation blocked: {'; '.join(blockers)}"
            host.record_activation_refusal(session_id, request_id, source, error, blockers)
            return {"activated": False, "error": error, "request_id": request_id}
        try:
            if target_revision_id:
                admission = self._pinned_target(target_revision_id)
            else:
                target_revision_id, admission = self.prepare_target()
            prepared = self._prepare_activation(snapshot, admission=admission, source=source)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            blocked = host.store.activation_blockers(str(host.workspace), session_id)
            host.record_activation_refusal(session_id, request_id, source, error, blocked)
            return {"activated": False, "error": error, "request_id": request_id}
        try:
            self._commit_activation(snapshot, session, request_id=request_id, source=source, prepared=prepared)
        except Exception as exc:
            host.end_activation()
            published = host.snapshot
            if published is None or published.revision_id != prepared.admission.revision_id:
                prepared.plugin_runtime.close()
            if not isinstance(exc, StoreConflict):
                raise
            host.record_activation_refusal(session_id, request_id, source, str(exc), ())
            return {"activated": False, "error": str(exc), "request_id": request_id}
        return {
            "activated": True,
            "request_id": request_id,
            "from_revision_id": snapshot.revision_id,
            "to_revision_id": prepared.admission.revision_id,
            "deactivated_owners": list(prepared.copies.deactivated_owners),
        }

    def prepare_target(self) -> tuple[str, Admission]:
        """Pin the candidate the live configuration currently describes; imports no user code."""
        admits = self._host.admits
        if admits is None:
            raise ValueError("capability activation is unavailable")
        admission = admits()
        # Only the private configuration is pinned here; the revision row is inserted atomically
        # with the activation commit, after the candidate runtime reported its metadata.
        write_private_snapshot(self._host.state_dir, admission.revision_id, admission.private_snapshot)
        return admission.revision_id, admission

    def _pinned_target(self, target_revision_id: str) -> Admission:
        """Rebuild a previously pinned target from durable records and its current plugin source."""
        snapshot = load_private_snapshot(self._host.state_dir, target_revision_id)
        admits = self._host.admits
        if admits is None:
            raise ValueError("capability activation is unavailable")
        return admits(snapshot=snapshot, revision_id=target_revision_id)

    def _prepare_activation(self, snapshot: ActivationSnapshot, *, admission: Admission, source: str) -> PreparedActivation:
        """Build and verify a candidate generation under the activation fence."""
        self._host.begin_activation()
        # Drivers hold no dispatch slot between invokes, so the busy check above cannot see
        # them: park them explicitly before the candidate builds, or an old-revision driver
        # would keep running (and its next invoke would only then be refused).
        self._host.park_background_drivers("capability activation")
        try:
            return self._prepare_activation_locked(snapshot, admission=admission, source=source)
        except Exception:
            self._host.end_activation()
            raise

    def candidate_sandbox_backend(self, admission: Admission) -> SandboxBackend | None:
        """Rebuild the exec backend for a candidate generation (core-side assembly)."""
        host = self._host
        snapshot = host.snapshot
        current = snapshot.plugin_runtime.sandbox_backend if snapshot is not None else None
        if current is None:
            return None
        protected = [str(host.workspace / host.config_filename)]
        for source in admission.plugins:
            try:
                protected.append(str(source.root.resolve(strict=True)))
            except OSError:
                protected.append(str(source.root))
        if current.helper_path:
            protected.append(current.helper_path)
        protected.append(str(host.state_dir / PRIVATE_DIR_NAME))
        protected.append(str(host.state_dir / DATABASE_FILENAME))
        protected.append(str(host.state_dir / "process-spool"))
        return SandboxBackend(
            policy=current.policy,
            helper_path=current.helper_path,
            protected_paths=tuple(dict.fromkeys(protected)),
            env_baseline=current.env_baseline,
        )

    def _prepare_activation_locked(self, snapshot: ActivationSnapshot, *, admission: Admission, source: str) -> PreparedActivation:
        host = self._host
        target_revision_id = admission.revision_id
        if target_revision_id == snapshot.revision_id:
            raise ValueError("the configured sources already produce the active revision")
        candidate_runtime: PluginRuntime | None = None
        try:
            candidate_runtime = host.build_candidate_runtime(admission, target_revision_id)
            # Flush facts that belong to the OLD generation before its runtime and subscriptions go
            # away. An eligible owner the candidate re-registers receives its fact right after the
            # swap, so that fact is deferred instead of aborting the activation.
            host.flush_old_generation_facts(admission.plugin_ids)
            if admission.validate is not None:
                admission.validate(candidate_runtime.providers)
            session_ids = tuple(record.session_id for record in host.store.sessions(workspace=str(host.workspace)))
            old_states = snapshot.plugin_runtime.state_metadata()
            candidate_states = candidate_runtime.state_metadata()
            copies = plan_state_copies(
                store=host.store,
                workspace=str(host.workspace),
                session_ids=session_ids,
                old_states=old_states,
                candidate_states=candidate_states,
                deactivate=lambda owner, payload, scope, scope_key, tip: self._deactivate_state(
                    snapshot.plugin_runtime, owner, payload, scope, scope_key, tip
                ),
            )
        except Exception:
            if candidate_runtime is not None:
                candidate_runtime.close()
            raise
        return PreparedActivation(
            admission=admission,
            plugin_runtime=candidate_runtime,
            copies=copies,
            source=source,
            prepared_against=snapshot.revision_id,
        )

    @staticmethod
    def _deactivate_state(
        runtime: PluginRuntime,
        owner: str,
        payload: Mapping[str, object],
        scope: StateScope,
        scope_key: str,
        tip_sequence: int | None,
    ) -> Mapping[str, object]:
        try:
            return runtime.prepare_deactivate_owner_state(
                owner,
                payload,
                scope=scope,
                scope_key=scope_key,
                tip_sequence=tip_sequence,
            )
        except RuntimeError as exc:
            raise ValueError(f"state deactivation for {owner} failed: {exc}") from exc

    @staticmethod
    def _deactivated_owners(snapshot: ActivationSnapshot, admission: Admission) -> frozenset[str]:
        """Owners loaded before that the candidate does not admit: disabled, removed, or missing."""
        return frozenset(snapshot.plugin_runtime.plugin_ids) - admission.plugin_ids

    def _commit_activation(
        self,
        snapshot: ActivationSnapshot,
        session: SessionRecord,
        *,
        request_id: str,
        source: str,
        prepared: PreparedActivation,
    ) -> None:
        """One CAS transaction, then publish one new pointer and revoke the old generation."""
        host = self._host
        admission = prepared.admission
        target_revision_id = admission.revision_id
        checkpoint = host.store.checkpoint(session.session_id)
        checkpoint_payload: Mapping[str, object] | None = None
        checkpoint_kind: CheckpointKind | None = None
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            checkpoint_payload = host.activation_checkpoint(
                json_wire_object(checkpoint.payload),
                source=source,
                request_id=request_id,
            )
            checkpoint_kind = checkpoint.kind
        disabled_owners = self._deactivated_owners(snapshot, admission)
        # The model's request was already recorded durably by its own tool commit; this
        # transaction only answers it (a client-initiated switch answers no staged request).
        events = (
            EventDraft(
                EventType.ACTIVATION_COMMITTED,
                "core",
                "core",
                {
                    "request_id": request_id,
                    "source": source,
                    "from_revision_id": snapshot.revision_id,
                    "to_revision_id": target_revision_id,
                    "deactivated_owners": list(prepared.copies.deactivated_owners),
                },
            ),
        )
        host.store.commit_activation(
            workspace=str(host.workspace),
            session_id=session.session_id,
            expected_sequence=session.last_event_sequence,
            expected_run_id=session.run_id,
            expected_revision_id=snapshot.revision_id,
            target_revision_id=target_revision_id,
            target_payload=admission.revision_payload,
            # Control facts carry the authentic operation identity, never an invented run.
            run_id=session.run_id or request_id,
            status=session.status,
            events=events,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            state_updates=prepared.copies.updates,
            cancelled_owners=tuple(sorted(disabled_owners)),
            cleared_budget_owners=tuple(sorted(disabled_owners)),
        )
        host.notify_commit()
        host.publish_activation(snapshot, prepared)
