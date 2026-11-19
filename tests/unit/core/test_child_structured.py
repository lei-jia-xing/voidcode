"""Structured child delivery at the core: digest compat, carriage, and keyed refusal."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.delegation import DelegationManager
from voidcode.core.engine import Core
from voidcode.core.events import EventType
from voidcode.core.storage.store import canonical_json
from voidcode.sdk.child_schema import DELIVERY_SENTENCE
from voidcode.sdk.json_values import json_wire_object

_SCHEMA = {
    "type": "object",
    "required": ["files", "ok"],
    "properties": {"files": {"type": "array", "items": {"type": "string"}}, "ok": {"type": "boolean"}},
    "additionalProperties": False,
}


def _expected_hash() -> str:
    encoded = canonical_json(
        {
            "prompt": "base",
            "scope": None,
            "max_steps": None,
            "readonly": False,
            "root_id": "root-1",
            "root_action_budget": None,
            "label": "",
            "notify_on_completion": False,
            "result_schema": None,
        }
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_undeclared_spec_hash_carries_explicit_null_schema() -> None:
    assert DelegationManager._spawn_spec_hash("base", None, None, False, "root-1", None, "", False) == _expected_hash()
    assert DelegationManager._spawn_spec_hash("base", None, None, False, "root-1", None, "", False, result_schema=None) == _expected_hash()


def test_declared_schema_alters_spec_hash() -> None:
    plain = DelegationManager._spawn_spec_hash("base", None, None, False, "root-1", None, "", False)
    shaped = DelegationManager._spawn_spec_hash("base", None, None, False, "root-1", None, "", False, result_schema=dict(_SCHEMA))
    other = DelegationManager._spawn_spec_hash("base", None, None, False, "root-1", None, "", False, result_schema={"type": "object"})
    assert shaped != plain
    assert other != plain
    assert other != shaped


def _turn(output: str | None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output}, finish="stop")


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _open(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    core = open_core(workspace, state_dir=tmp_path / "state")
    core.create_session("root")
    return core


def _request_texts(server: OpenAILoopbackServer) -> str:
    parts: list[str] = []
    for request in server.requests:
        messages = request.get("messages")
        if isinstance(messages, list):
            for entry in messages:
                if isinstance(entry, dict):
                    parts.append(json.dumps(entry.get("content"), ensure_ascii=False))
    return "\n".join(parts)


def test_undeclared_keyed_respawn_stays_accepted(tmp_path: Path) -> None:
    with _server([_turn("plain answer")]) as server:
        core = _open(tmp_path, server)
        try:
            first = core.spawn_child("root", "plain work", owner="probe", idempotency_key="k-plain")
            second = core.spawn_child("root", "plain work", owner="probe", idempotency_key="k-plain")
            assert second == first
            outcome = core.join_child("root", first, timeout_seconds=30)
            assert outcome.status == "completed"
        finally:
            core.close()


def test_schema_mismatch_refuses_keyed_retry_both_directions(tmp_path: Path) -> None:
    with _server([_turn("shaped answer"), _turn("other answer")]) as server:
        core = _open(tmp_path, server)
        try:
            shaped = core.spawn_child("root", "shaped work", owner="probe", idempotency_key="k-shaped", result_schema=dict(_SCHEMA))
            with pytest.raises(ValueError, match="different spec"):
                core.spawn_child("root", "shaped work", owner="probe", idempotency_key="k-shaped")
            other = core.spawn_child("root", "other work", owner="probe", idempotency_key="k-other")
            with pytest.raises(ValueError, match="different spec"):
                core.spawn_child("root", "other work", owner="probe", idempotency_key="k-other", result_schema=dict(_SCHEMA))
            assert core.join_child("root", shaped, timeout_seconds=30).status == "completed"
            assert core.join_child("root", other, timeout_seconds=30).status == "completed"
        finally:
            core.close()


def test_spawn_carries_schema_and_hints_prompt_without_touching_facts(tmp_path: Path) -> None:
    replies = [_turn('{"files": ["a"], "ok": true}'), _turn("plain answer")]
    with _server(replies) as server:
        core = _open(tmp_path, server)
        try:
            shaped = core.spawn_child("root", "shaped work MARK-HINT", owner="probe", idempotency_key="k-hint", result_schema=dict(_SCHEMA))
            plain = core.spawn_child("root", "plain work MARK-PLAIN", owner="probe", idempotency_key="k-plain2")
            assert core.join_child("root", shaped, timeout_seconds=30).status == "completed"
            assert core.join_child("root", plain, timeout_seconds=30).status == "completed"
            birth = next(e for e in core.store.events(shaped) if e.event_type == EventType.CHILD_RESERVED).payload
            birth_schema = birth.get("result_schema")
            assert isinstance(birth_schema, Mapping) and json_wire_object(birth_schema) == _SCHEMA
            spawned = [
                e.payload
                for e in core.store.events("root")
                if e.event_type == EventType.CHILD_SPAWNED and e.payload.get("child_session_id") == shaped
            ]
            assert spawned
            spawned_schema = spawned[0].get("result_schema")
            assert isinstance(spawned_schema, Mapping) and json_wire_object(spawned_schema) == _SCHEMA
            finished = {
                str(e.payload.get("child_session_id")): dict(e.payload) for e in core.store.events("root") if e.event_type == EventType.CHILD_FINISHED
            }
            assert set(finished[shaped]) == set(finished[plain])
            assert "result_schema" not in finished[shaped]
        finally:
            core.close()
    bodies = _request_texts(server)
    assert DELIVERY_SENTENCE in bodies
    assert "MARK-HINT" in bodies and '\\"files\\"' in bodies and '\\"required\\"' in bodies


def test_invalid_schema_fails_before_any_spawn(tmp_path: Path) -> None:
    with _server([]) as server:
        core = _open(tmp_path, server)
        try:
            with pytest.raises(ValueError, match="not a valid JSON schema"):
                core.spawn_child("root", "work", owner="probe", result_schema={"type": "nonsense"})
            assert [e for e in core.store.events("root") if e.event_type == EventType.CHILD_SPAWNED] == []
        finally:
            core.close()
