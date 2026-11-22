"""Follow-up queue domain: sequential user follow-up queueing, image spill, rehydration, and execution.

This module owns:
- Enqueueing follow-up user messages while a run or ask is active
- Spilling follow-up images into content-addressed artifact references
- Rehydrating images with cryptographic integrity checks from artifact storage
- Claiming and executing queued follow-up continuations at safe turn boundaries
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from ...sdk.json_values import json_wire_object
from ...sdk.provider import ImageAttachment
from ..artifacts import SHA256_PATTERN, ArtifactStore
from ..cancellation import Cancellation, CancellationCoordinator
from ..coverage import ArtifactPage, ArtifactReference, read_artifact_content
from ..events import EventType
from ..execution import _Cursor, _Execution
from ..storage.store import (
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
    SessionRecord,
    SessionStatus,
    SqliteStore,
    StoreConflict,
    StoredEvent,
)

if TYPE_CHECKING:
    from .turn_execution import CoreResult


@dataclass(frozen=True, slots=True)
class FollowupImageRef:
    """Content-addressed reference to a spilled follow-up image."""

    media_type: str
    sha256: str
    bytes: int

    def as_dict(self) -> dict[str, object]:
        return {"media_type": self.media_type, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class FollowupSubmission:
    """One user follow-up prompt submission to queue."""

    prompt: str
    max_steps: int | None = None
    images: tuple[ImageAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimedFollowup:
    """One claimed user follow-up continuation with rehydrated images."""

    continuation_id: str
    prompt: str
    max_steps: int | None
    images: tuple[ImageAttachment, ...]
    root_id: str | None
    root_action_budget: int | None


class FollowupHost(Protocol):
    """Core capabilities required by the follow-up queue runtime."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def artifacts(self) -> ArtifactStore: ...

    @property
    def abort_signal(self) -> Cancellation: ...

    @property
    def cancellations(self) -> CancellationCoordinator: ...

    def artifact_page(self, digest: str, offset: int, limit: int) -> ArtifactPage: ...

    def session_lock(self, session_id: str) -> AbstractContextManager[None]: ...

    def ensure_open(self) -> None: ...

    def check_session(self, session: SessionRecord) -> None: ...

    def admits_fresh_work(self, session: SessionRecord) -> bool: ...

    def interrupt_batch(self, execution: _Execution) -> None: ...

    def commit(
        self,
        execution: _Execution,
        status: SessionStatus,
        kind: CheckpointKind,
        *events: EventDraft,
        start_turn: bool = False,
    ) -> tuple[StoredEvent, ...]: ...

    def drive(self, execution: _Execution) -> CoreResult: ...

    def notify_child_state_changed(self) -> None: ...


class FollowupManager:
    """Owns follow-up queueing, image spill/rehydration, and continuation driving."""

    def __init__(self, host: FollowupHost) -> None:
        self._host = host

    def spill_followup_images(self, images: tuple[ImageAttachment, ...]) -> tuple[FollowupImageRef, ...]:
        """Spill queued follow-up image bytes to the artifact store, returning references.

        Each reference is `{"media_type", "sha256", "bytes"}` — the same content
        address the tool-result spill path publishes, never a second blob store.
        The continuation payload carries only these small references, so a large
        image no longer blows the continuation bound; delivery rehydrates to the
        same base64 bytes the direct path commits. Invalid base64 fails here,
        visibly, before any row is staged.
        """
        decoded: list[bytes] = []
        for index, img in enumerate(images):
            try:
                decoded.append(base64.b64decode(img.data, validate=True))
            except ValueError as exc:
                raise ValueError(f"queued follow-up image {index} has invalid base64 data") from exc
        refs: list[FollowupImageRef] = []
        for img, raw in zip(images, decoded, strict=True):
            spill_id, spill = self._host.artifacts.start_spill(img.media_type, owner="core")
            try:
                spill.append(raw)
                digest, size, _media_type = self._host.artifacts.finish(spill_id)
            except BaseException:
                self._host.artifacts.discard(spill_id)
                raise
            refs.append(FollowupImageRef(media_type=img.media_type, sha256=digest, bytes=size))
        return tuple(refs)

    def rehydrate_followup_images(
        self,
        raw_images: Sequence[FollowupImageRef | Mapping[str, object] | object],
        continuation_id: str,
    ) -> tuple[ImageAttachment, ...]:
        """Rebuild provider-visible images from continuation references.

        Reference entries (`{"media_type", "sha256", "bytes"}`) read back through
        the shared artifact pager with digest+size verification. Missing,
        unreadable, or malformed references raise `StoreConflict` — the caller
        completes the continuation as failed first, so delivery never falls back
        to text-only pretending completeness.
        """
        if not raw_images:
            return ()
        rehydrated: list[ImageAttachment] = []
        for index, entry in enumerate(raw_images):
            if isinstance(entry, FollowupImageRef):
                media_type, sha, size = entry.media_type, entry.sha256, entry.bytes
            elif isinstance(entry, Mapping):
                media_type = entry.get("media_type")
                sha = entry.get("sha256")
                size = entry.get("bytes")
            else:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} is malformed")
            if not isinstance(media_type, str) or not media_type:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} has no media type")
            if not isinstance(sha, str) or SHA256_PATTERN.fullmatch(sha) is None:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} has an invalid artifact reference")
            if type(size) is not int or size < 0:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} has an invalid artifact size")
            try:
                content = read_artifact_content(ArtifactReference(sha, size, media_type), self._host.artifact_page)
            except (ValueError, OSError) as exc:
                raise StoreConflict(f"queued follow-up {continuation_id!r} image {index} references missing or unreadable artifact {sha}") from exc
            rehydrated.append(ImageAttachment(media_type, base64.b64encode(content).decode("ascii"), sha))
        return tuple(rehydrated)

    def queue_followup(
        self,
        session: SessionRecord,
        submission: FollowupSubmission | str,
        max_steps: int | None = None,
        images: tuple[ImageAttachment, ...] = (),
    ) -> CoreResult:
        """Queue a user prompt and optional images for execution after the current run."""
        from .turn_execution import CoreResult

        sub = submission if isinstance(submission, FollowupSubmission) else FollowupSubmission(submission, max_steps, images)
        checkpoint = self._host.store.checkpoint(session.session_id)
        if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
            raise StoreConflict(f"running session {session.session_id!r} has no captured root authorization")
        root_id = checkpoint.payload.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict(f"running session {session.session_id!r} has no captured root action identity")
        if "initial_root_budget" not in checkpoint.payload:
            raise StoreConflict(f"running session {session.session_id!r} has no captured root budget state")
        root_budget = checkpoint.payload.get("initial_root_budget")
        if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
            raise StoreConflict(f"running session {session.session_id!r} has an invalid captured root budget")
        cid = uuid4().hex
        payload: dict[str, object] = {"prompt": sub.prompt, "root_action_budget": root_budget}
        if sub.max_steps is not None:
            payload["max_steps"] = sub.max_steps
        if sub.images:
            refs = self.spill_followup_images(sub.images)
            payload["images"] = [ref.as_dict() for ref in refs]
        self._host.store.enqueue_continuation(
            cid,
            "core",
            session.session_id,
            "user_message",
            session.run_id or "",
            self._host.revision_id,
            payload,
            root_id=root_id,
            role="user",
        )
        self._host.notify_child_state_changed()
        return CoreResult(
            session_id=session.session_id,
            run_id=session.run_id or "",
            revision_id=self._host.revision_id,
            status="queued",
        )

    def claim_pending_followup(self, session_id: str) -> ClaimedFollowup | None:
        """Claim next pending user follow-up continuation and rehydrate its images."""
        followup = self._host.store.claim_pending_followup(session_id)
        if followup is None:
            return None
        cid, prompt, steps, raw_images, root_id, root_budget = followup
        try:
            images = self.rehydrate_followup_images(raw_images, cid)
        except Exception:
            try:
                if self._host.store.continuation_status(cid) is ContinuationStatus.CLAIMED:
                    self._host.store.complete_continuation(cid, status=ContinuationStatus.FAILED)
            except Exception:
                pass
            raise
        return ClaimedFollowup(
            continuation_id=cid,
            prompt=prompt,
            max_steps=steps,
            images=images,
            root_id=root_id,
            root_action_budget=root_budget,
        )

    def execute_claimed_followup(self, session_id: str, claim: ClaimedFollowup) -> CoreResult:
        """Execute one claimed follow-up, ensuring failure status is recorded on error."""
        try:
            return self.execute_user_followup(
                session_id=session_id,
                continuation_id=claim.continuation_id,
                prompt=claim.prompt,
                max_steps=claim.max_steps,
                root_id=claim.root_id,
                root_action_budget=claim.root_action_budget,
                images=claim.images,
            )
        except Exception:
            try:
                if self._host.store.continuation_status(claim.continuation_id) is ContinuationStatus.CLAIMED:
                    self._host.store.complete_continuation(claim.continuation_id, status=ContinuationStatus.FAILED)
            except Exception:
                pass
            raise

    def execute_user_followup(
        self,
        session_id: str,
        continuation_id: str,
        prompt: str,
        max_steps: int | None,
        root_id: str | None,
        root_action_budget: int | None,
        images: tuple[ImageAttachment, ...] = (),
    ) -> CoreResult:
        """Execute a claimed user follow-up continuation."""
        from .turn_execution import CoreResult

        if not isinstance(root_id, str) or not root_id:
            self._host.store.complete_continuation(continuation_id, status=ContinuationStatus.FAILED)
            return CoreResult(
                session_id,
                "",
                self._host.revision_id,
                SessionStatus.FAILED,
                error="queued follow-up has no captured root action identity; refusing to create a new budget",
                stop_cause="error",
            )
        if root_action_budget is not None and (type(root_action_budget) is not int or root_action_budget < 0):
            self._host.store.complete_continuation(continuation_id, status=ContinuationStatus.FAILED)
            return CoreResult(
                session_id,
                "",
                self._host.revision_id,
                SessionStatus.FAILED,
                error="queued follow-up has an invalid captured root budget",
                stop_cause="error",
            )
        with self._host.session_lock(session_id):
            self._host.ensure_open()
            self._host.abort_signal.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            self._host.cancellations.register_run(session_id, run_abort_id, abort)
            try:
                session = self._host.store.session(session_id)
                if not self._host.admits_fresh_work(session):
                    self._host.store.complete_continuation(continuation_id, status=ContinuationStatus.FAILED)
                    return CoreResult(session_id, "", self._host.revision_id, SessionStatus.FAILED, error="session does not admit fresh work")
                self._host.check_session(session)
                root_state = self._host.store.root_budget_state(root_id)
                if root_action_budget is None and root_state is not None:
                    self._host.store.complete_continuation(continuation_id, status=ContinuationStatus.FAILED)
                    return CoreResult(
                        session_id,
                        "",
                        self._host.revision_id,
                        SessionStatus.FAILED,
                        error="queued follow-up captured an uncapped root with a finite budget row",
                        stop_cause="error",
                    )
                if root_action_budget is not None and (root_state is None or root_state[0] < root_action_budget):
                    self._host.store.complete_continuation(continuation_id, status=ContinuationStatus.FAILED)
                    return CoreResult(
                        session_id,
                        "",
                        self._host.revision_id,
                        SessionStatus.FAILED,
                        error="queued follow-up lost its captured finite root authorization",
                        stop_cause="error",
                    )
                execution = _Execution(
                    session,
                    _Cursor(
                        root_id=root_id,
                        initial_root_budget=root_action_budget,
                        max_steps=max_steps,
                        continuation_id=continuation_id,
                    ),
                    list(self._host.store.events(session_id)),
                    run_abort_id,
                )
                if session.status is SessionStatus.INTERRUPTED:
                    previous = self._host.store.checkpoint(session_id)
                    if previous is not None and isinstance(previous.payload, Mapping):
                        execution.cursor = _Cursor.model_validate(json_wire_object(previous.payload))
                        execution.cursor.step = 0
                        execution.cursor.stop_cause = None
                    if execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
                        self._host.interrupt_batch(execution)
                    execution.cursor = _Cursor(
                        root_id=root_id,
                        initial_root_budget=root_action_budget,
                        max_steps=max_steps,
                        continuation_id=continuation_id,
                    )
                user_payload: dict[str, object] = {"content": prompt}
                if max_steps is not None:
                    user_payload["max_steps"] = max_steps
                if images:
                    user_payload["images"] = [img.as_payload() for img in images]
                self._host.commit(
                    execution,
                    SessionStatus.RUNNING,
                    CheckpointKind.READY,
                    EventDraft(EventType.USER_MESSAGE, "client", "user", user_payload),
                    start_turn=True,
                )
                self._host.store.set_continuation_run_id(continuation_id, execution.run_id)
                result = self._host.drive(execution)
                status: ContinuationStatus
                if result.status in ("completed", "idle"):
                    status = ContinuationStatus.COMPLETED
                elif result.status == "failed":
                    status = ContinuationStatus.FAILED
                elif result.status == "interrupted":
                    if result.stop_cause in ("user_stop", "owner_cancel"):
                        status = ContinuationStatus.CANCELLED
                    else:
                        return result
                elif result.status == "waiting":
                    return result
                else:
                    status = ContinuationStatus.FAILED
                self._host.store.complete_continuation(continuation_id, status=status)
                return result
            finally:
                self._host.cancellations.unregister_run(session_id)
