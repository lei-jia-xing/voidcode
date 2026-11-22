"""Unit tests for followup domain types, image references, and rehydration edge cases."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from voidcode.core.engine.followup import (
    ClaimedFollowup,
    FollowupHost,
    FollowupImageRef,
    FollowupManager,
    FollowupSubmission,
)
from voidcode.core.storage.store import StoreConflict
from voidcode.sdk.provider import ImageAttachment


def test_followup_domain_dataclasses() -> None:
    ref = FollowupImageRef(media_type="image/png", sha256="a" * 64, bytes=1024)
    assert ref.as_dict() == {"media_type": "image/png", "sha256": "a" * 64, "bytes": 1024}

    img = ImageAttachment(media_type="image/png", data="abc")
    submission = FollowupSubmission(prompt="hello", max_steps=5, images=(img,))
    assert submission.prompt == "hello"
    assert submission.max_steps == 5
    assert len(submission.images) == 1

    claim = ClaimedFollowup(
        continuation_id="cid-1",
        prompt="hello",
        max_steps=5,
        images=(img,),
        root_id="root-1",
        root_action_budget=10,
    )
    assert claim.continuation_id == "cid-1"
    assert claim.root_id == "root-1"
    assert claim.root_action_budget == 10


def test_spill_followup_images_invalid_base64() -> None:
    host = MagicMock(spec=FollowupHost)
    manager = FollowupManager(host)

    invalid_img = ImageAttachment(media_type="image/png", data="not-valid-base64!!!")
    with pytest.raises(ValueError, match="invalid base64 data"):
        manager.spill_followup_images((invalid_img,))


def test_rehydrate_followup_images_validation_errors() -> None:
    host = MagicMock(spec=FollowupHost)
    manager = FollowupManager(host)

    # Empty images returns empty tuple
    assert manager.rehydrate_followup_images([], "cid-1") == ()

    # Not a mapping
    with pytest.raises(StoreConflict, match="malformed"):
        manager.rehydrate_followup_images(["not-a-dict"], "cid-1")

    # Missing media type
    with pytest.raises(StoreConflict, match="no media type"):
        manager.rehydrate_followup_images([{"sha256": "a" * 64, "bytes": 10}], "cid-1")

    # Invalid sha256 reference
    with pytest.raises(StoreConflict, match="invalid artifact reference"):
        manager.rehydrate_followup_images([{"media_type": "image/png", "sha256": "short", "bytes": 10}], "cid-1")

    # Invalid size
    with pytest.raises(StoreConflict, match="invalid artifact size"):
        manager.rehydrate_followup_images([{"media_type": "image/png", "sha256": "a" * 64, "bytes": -1}], "cid-1")

    # Support FollowupImageRef directly
    ref = FollowupImageRef(media_type="image/png", sha256="a" * 64, bytes=4)
    with pytest.raises(StoreConflict, match="references missing or unreadable artifact"):
        host.artifact_page.side_effect = OSError("not found")
        manager.rehydrate_followup_images([ref], "cid-1")


def test_spill_and_rehydrate_followup_images_roundtrip() -> None:
    import base64
    import hashlib

    from voidcode.core.coverage import ArtifactPage, ArtifactReference

    host = MagicMock(spec=FollowupHost)
    manager = FollowupManager(host)

    raw_bytes = b"sample image bytes 123456"
    real_sha = hashlib.sha256(raw_bytes).hexdigest()
    b64_data = base64.b64encode(raw_bytes).decode("ascii")
    img = ImageAttachment(media_type="image/png", data=b64_data)

    spill_mock = MagicMock()
    host.artifacts.start_spill.return_value = ("spill-1", spill_mock)
    host.artifacts.finish.return_value = (real_sha, len(raw_bytes), "image/png")

    refs = manager.spill_followup_images((img,))
    assert isinstance(refs, tuple)
    assert len(refs) == 1
    assert refs[0] == FollowupImageRef(media_type="image/png", sha256=real_sha, bytes=len(raw_bytes))

    # Rehydrate
    host.artifact_page.return_value = ArtifactPage(
        artifact=ArtifactReference(sha256=real_sha, bytes=len(raw_bytes), media_type="image/png"),
        offset=0,
        data=raw_bytes,
        eof=True,
    )
    rehydrated = manager.rehydrate_followup_images(refs, "cid-1")
    assert len(rehydrated) == 1
    assert rehydrated[0].media_type == "image/png"
    assert rehydrated[0].data == b64_data


def test_queue_followup_valid_and_errors() -> None:
    from voidcode.core.storage.store import Checkpoint, CheckpointKind, SessionRecord, SessionStatus

    host = MagicMock(spec=FollowupHost)
    host.revision_id = "rev-42"
    manager = FollowupManager(host)

    sess = SessionRecord(
        session_id="s1",
        workspace="/tmp",
        status=SessionStatus.RUNNING,
        active_revision_id="rev-42",
        run_id="r1",
        turn=1,
        last_event_sequence=1,
    )

    # 1. Missing checkpoint
    host.store.checkpoint.return_value = None
    with pytest.raises(StoreConflict, match="no captured root authorization"):
        manager.queue_followup(sess, FollowupSubmission("prompt 1"))

    # 2. Checkpoint missing root_id
    host.store.checkpoint.return_value = Checkpoint(
        kind=CheckpointKind.READY,
        revision_id="rev-42",
        run_id="r1",
        last_event_sequence=1,
        payload={"initial_root_budget": 5},
    )
    with pytest.raises(StoreConflict, match="no captured root action identity"):
        manager.queue_followup(sess, FollowupSubmission("prompt 1"))

    # 3. Checkpoint missing initial_root_budget
    host.store.checkpoint.return_value = Checkpoint(
        kind=CheckpointKind.READY,
        revision_id="rev-42",
        run_id="r1",
        last_event_sequence=1,
        payload={"root_id": "root-1"},
    )
    with pytest.raises(StoreConflict, match="no captured root budget state"):
        manager.queue_followup(sess, FollowupSubmission("prompt 1"))

    # 4. Success with FollowupSubmission
    host.store.checkpoint.return_value = Checkpoint(
        kind=CheckpointKind.READY,
        revision_id="rev-42",
        run_id="r1",
        last_event_sequence=1,
        payload={"root_id": "root-1", "initial_root_budget": 10},
    )
    res = manager.queue_followup(sess, FollowupSubmission("hello prompt", max_steps=3))
    assert res.status == "queued"
    assert res.session_id == "s1"
    assert res.run_id == "r1"
    assert res.revision_id == "rev-42"

    args, kwargs = host.store.enqueue_continuation.call_args
    assert kwargs["root_id"] == "root-1"
    assert kwargs["role"] == "user"
    assert args[6]["prompt"] == "hello prompt"
    assert args[6]["max_steps"] == 3
    assert args[6]["root_action_budget"] == 10
    host.notify_child_state_changed.assert_called_once()


def test_claim_pending_followup_marks_failed_on_rehydrate_error() -> None:
    from voidcode.core.storage.store import ContinuationStatus

    host = MagicMock(spec=FollowupHost)
    manager = FollowupManager(host)

    # Return a pending followup whose image references are corrupt
    host.store.claim_pending_followup.return_value = (
        "cid-corrupt",
        "some prompt",
        None,
        ["corrupt-image-not-dict"],
        "root-1",
        None,
    )
    host.store.continuation_status.return_value = ContinuationStatus.CLAIMED

    with pytest.raises(StoreConflict, match="malformed"):
        manager.claim_pending_followup("s1")

    host.store.complete_continuation.assert_called_once_with("cid-corrupt", status=ContinuationStatus.FAILED)
