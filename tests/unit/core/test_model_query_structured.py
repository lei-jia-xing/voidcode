"""Structured governed queries: validated data or a callable failure, never a pose."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.core_factory import register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.provider import ProviderExecutionError

_SCHEMA = {
    "type": "object",
    "required": ["path", "line"],
    "properties": {"path": {"type": "string"}, "line": {"type": "integer"}},
    "additionalProperties": False,
}

_STRUCTURED_SUFFIX = "Reply with exactly one JSON object and nothing else."


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
    register_core(core)
    core.create_session("sess-structured")
    return core


def _request_messages(server: OpenAILoopbackServer, index: int) -> list[Mapping[str, object]]:
    raw = server.requests[index].get("messages")
    assert isinstance(raw, list)
    return [message for message in raw if isinstance(message, Mapping)]


def test_structured_extraction_returns_validated_data(tmp_path: Path) -> None:
    with _server([_turn('{"path": "a.py", "line": 3}')]) as server:
        core = _open(tmp_path, server)
        result = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert result.data is not None and dict(result.data) == {"path": "a.py", "line": 3}
    assert result.parse_error is None
    assert result.raw_text == '{"path": "a.py", "line": 3}'
    assert result.done_reason == "stop"
    request = server.requests[0]
    assert not request.get("tools")
    messages = _request_messages(server, 0)
    assert len(messages) == 1 and str(messages[0].get("content", "")).endswith(_STRUCTURED_SUFFIX)
    facts = [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]
    assert facts and facts[0].payload["model"] == "exact-model" and "usage" in facts[0].payload


def test_unparseable_reply_is_callable_failure(tmp_path: Path) -> None:
    with _server([_turn("I cannot answer that.")]) as server:
        core = _open(tmp_path, server)
        result = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert result.data is None
    assert result.parse_error is not None
    assert result.parse_error.kind == "invalid_json"
    assert result.parse_error.message
    assert result.raw_text == "I cannot answer that."
    assert [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]


def test_schema_violation_reports_location(tmp_path: Path) -> None:
    with _server([_turn('{"path": "a.py"}')]) as server:
        core = _open(tmp_path, server)
        result = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert result.data is None
    assert result.parse_error is not None
    assert result.parse_error.kind == "schema_violation"
    assert result.parse_error.path == "$"
    assert "line" in result.parse_error.message


def test_non_object_and_empty_replies_never_pose(tmp_path: Path) -> None:
    with _server([_turn("[1, 2]"), _turn(""), _turn(None)]) as server:
        core = _open(tmp_path, server)
        array = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
        empty = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
        missing = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert array.data is None and array.parse_error is not None and array.parse_error.kind == "schema_violation"
    assert empty.data is None and empty.parse_error is not None and empty.parse_error.kind == "invalid_json"
    assert missing.data is None and missing.parse_error is not None and missing.parse_error.kind == "invalid_json"
    assert len(server.requests) == 3


def test_provider_error_propagates_exactly_like_bare(tmp_path: Path) -> None:
    malformed = {"choices": []}
    with _server([dict(malformed), dict(malformed)]) as server:
        core = _open(tmp_path, server)
        with pytest.raises(ProviderExecutionError):
            core.governed_model_query("sess-structured", "bare")
        with pytest.raises(ProviderExecutionError):
            core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert len(server.requests) == 2


def test_batch_locates_the_failing_item(tmp_path: Path) -> None:
    with _server([_turn('{"path": "a.py", "line": 1}'), _turn("nope"), _turn('{"path": "b.py", "line": 2}')]) as server:
        core = _open(tmp_path, server)
        results = [core.governed_model_query_structured("sess-structured", f"extract {index}", dict(_SCHEMA)) for index in range(3)]
    assert results[0].data is not None and results[2].data is not None
    assert results[1].data is None and results[1].parse_error is not None
    assert len(server.requests) == 3


def test_invalid_result_schema_fails_before_dispatch(tmp_path: Path) -> None:
    with _server([_turn('{"path": "a.py", "line": 1}')]) as server:
        core = _open(tmp_path, server)
        with pytest.raises(ValueError, match="not a valid JSON schema"):
            core.governed_model_query_structured("sess-structured", "extract", {"type": 42})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="must be an object"):
            core.governed_model_query_structured("sess-structured", "extract", ["not", "an", "object"])  # type: ignore[arg-type]
    assert server.requests == []
