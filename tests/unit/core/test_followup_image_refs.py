"""Reference-backed queued follow-up images: spill, rehydrate, and failure visibility."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import BLOCK_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import MAX_CONTINUATION_PAYLOAD_BYTES, StoreConflict, canonical_json
from voidcode.sdk.provider import ImageAttachment

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Generator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _setup_core(tmp_path: Path, server: OpenAILoopbackServer) -> tuple[Core, Path]:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", blocking=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=tmp_path / "state")
    return core, workspace


def _wait_for_marker(workspace: Path) -> None:
    marker = workspace / "block.started"
    deadline = time.monotonic() + 10
    while not marker.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.is_file()


def _read_all(core: Core, sha: str) -> bytes:
    chunks = bytearray()
    offset = 0
    while True:
        page = core.read_artifact(sha, offset=offset, limit=65536)
        chunks.extend(page.data)
        if page.eof:
            return bytes(chunks)
        offset += len(page.data)


def test_queued_followup_images_spill_to_references_under_bound(tmp_path: Path) -> None:
    """A queued follow-up whose inline images would exceed the continuation bound now commits via refs."""
    raw = (bytes(range(256)) * 3200)[:819200]
    assert len(raw) == 819200
    data = base64.b64encode(raw).decode("ascii")
    img = ImageAttachment(media_type="image/png", data=data)
    inline_payload = {"prompt": "large follow-up", "root_action_budget": None, "images": [img.as_payload()]}
    inline_size = len(canonical_json(inline_payload).encode("utf-8"))
    assert inline_size > MAX_CONTINUATION_PAYLOAD_BYTES, inline_size

    replies = [_turn(_call("block-1", BLOCK_TOOL)), _turn(output="turn 1 done")]
    with _server(replies) as server:
        core, workspace = _setup_core(tmp_path, server)
        core.auto_drain = False
        try:
            session_id = core.create_session("sess-large-image").session_id
            t1 = threading.Thread(target=lambda: core.run("Turn 1 prompt", session_id=session_id), daemon=True)
            t1.start()
            _wait_for_marker(workspace)
            assert core.store.session(session_id).status == "running"

            queued = core.run("large follow-up", session_id=session_id, images=(img,))
            assert queued.status == "queued"

            pending = core.store.pending_continuations(session_id, limit=10)
            kernel_pending = [c for c in pending if c.owner == "kernel"]
            assert len(kernel_pending) == 1
            payload = kernel_pending[0].payload
            assert "images" in payload
            refs = payload["images"]
            assert isinstance(refs, (list, tuple)) and len(refs) == 1
            ref = refs[0]
            assert isinstance(ref, dict) or hasattr(ref, "keys")
            assert "data" not in ref
            assert ref["media_type"] == "image/png"
            assert isinstance(ref["sha256"], str) and len(ref["sha256"]) == 64
            assert ref["bytes"] == len(raw)
            payload_size = len(canonical_json(payload).encode("utf-8"))
            assert payload_size < MAX_CONTINUATION_PAYLOAD_BYTES, payload_size
            assert payload_size < inline_size // 100

            stored = _read_all(core, ref["sha256"])
            assert stored == raw
            assert hashlib.sha256(stored).hexdigest() == ref["sha256"]

            (workspace / "block.release").touch()
            t1.join(timeout=10)
            assert not t1.is_alive()
            assert core.store.continuation_status(kernel_pending[0].continuation_id) == "pending"
        finally:
            core.close()


def test_claim_rehydrates_byte_identical_to_direct(tmp_path: Path) -> None:
    """Claim/delivery rebuilds the same provider-visible bytes the direct path commits."""
    raw = b"fake-png-bytes-12345-queued-vs-direct"
    data = base64.b64encode(raw).decode("ascii")
    img = ImageAttachment(media_type="image/png", data=data)
    replies = [
        _turn(_call("block-1", BLOCK_TOOL)),
        _turn(output="turn 1 done"),
        _turn(output="turn 2 done"),
        _turn(output="direct done"),
    ]
    with _server(replies) as server:
        core, workspace = _setup_core(tmp_path, server)
        try:
            session_id = core.create_session("sess-rehydrate").session_id
            t1 = threading.Thread(target=lambda: core.run("Turn 1 prompt", session_id=session_id), daemon=True)
            t1.start()
            _wait_for_marker(workspace)

            queued = core.run("follow-up with image", session_id=session_id, images=(img,))
            assert queued.status == "queued"

            (workspace / "block.release").touch()
            t1.join(timeout=10)
            assert not t1.is_alive()

            events = core.store.events(session_id)
            user_events = [e for e in events if e.event_type == "core.user_message"]
            assert len(user_events) == 2
            queued_images = user_events[1].payload.get("images")
            assert isinstance(queued_images, (list, tuple)) and len(queued_images) == 1
            queued_entry = queued_images[0]
            assert queued_entry["media_type"] == "image/png"
            assert queued_entry["data"] == data
            assert base64.b64decode(queued_entry["data"], validate=True) == raw

            direct = core.run("direct with image", session_id="sess-direct", images=(img,))
            assert direct.status in ("completed", "idle")
            direct_events = core.store.events("sess-direct")
            direct_user = [e for e in direct_events if e.event_type == "core.user_message"]
            assert len(direct_user) == 1
            direct_images = direct_user[0].payload.get("images")
            assert isinstance(direct_images, (list, tuple)) and len(direct_images) == 1
            assert direct_images[0]["media_type"] == queued_entry["media_type"]
            assert direct_images[0]["data"] == queued_entry["data"]
            assert base64.b64decode(direct_images[0]["data"], validate=True) == raw
        finally:
            core.close()


def test_text_only_followup_payload_unchanged(tmp_path: Path) -> None:
    """Text-only follow-ups keep byte-identical payloads (no images key, same canonical bytes)."""
    replies = [_turn(_call("block-1", BLOCK_TOOL)), _turn(output="turn 1 done"), _turn(output="turn 2 done")]
    with _server(replies) as server:
        core, workspace = _setup_core(tmp_path, server)
        core.auto_drain = False
        try:
            session_id = core.create_session("sess-text-only").session_id
            t1 = threading.Thread(target=lambda: core.run("Turn 1 prompt", session_id=session_id, root_action_budget=3), daemon=True)
            t1.start()
            _wait_for_marker(workspace)

            queued = core.run("Turn 2 follow-up prompt", session_id=session_id, max_steps=7)
            assert queued.status == "queued"

            pending = core.store.pending_continuations(session_id, limit=10)
            kernel_pending = [c for c in pending if c.owner == "kernel"]
            assert len(kernel_pending) == 1
            payload = kernel_pending[0].payload
            expected: dict[str, object] = {"prompt": "Turn 2 follow-up prompt", "root_action_budget": 3, "max_steps": 7}
            assert payload == expected
            assert "images" not in payload
            assert canonical_json(payload).encode("utf-8") == canonical_json(expected).encode("utf-8")

            (workspace / "block.release").touch()
            t1.join(timeout=10)
            assert not t1.is_alive()
        finally:
            core.close()


def test_missing_artifact_bytes_fail_visibly(tmp_path: Path) -> None:
    """A dangling image reference fails the claim visibly, never delivering text-only."""
    with _server([]) as server:
        core, _workspace = _setup_core(tmp_path, server)
        try:
            session_id = core.create_session("sess-missing-bytes").session_id
            missing_sha = "ab" * 32
            payload: dict[str, object] = {
                "prompt": "follow-up with dangling image",
                "root_action_budget": None,
                "images": [{"media_type": "image/png", "sha256": missing_sha, "bytes": 123}],
            }
            cid = core.store.enqueue_continuation(
                "cid-dangling",
                "kernel",
                session_id,
                "user_message",
                "",
                core.revision_id,
                payload,
                root_id="root-test",
                role="user",
            )
            with pytest.raises(StoreConflict, match="missing or unreadable"):
                core._drive_pending(session_id)
            assert core.store.continuation_status(cid) == "failed"
            events = core.store.events(session_id)
            assert [e for e in events if e.event_type == "core.user_message"] == []
        finally:
            core.close()
