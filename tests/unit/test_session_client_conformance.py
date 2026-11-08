"""Both client arms drive identical observable behavior over real cores.

Parametrized over the local arm (direct Core) and the remote arm (loopback
HTTP): the same tally-plugin workspace, the same loopback provider script,
the same assertions on durable effects — never on shapes or copies.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.storage.store import SqliteStore
from voidcode.sdk import SessionClient
from voidcode.sdk.client import CoreClientError, RunSpec
from voidcode.transport.http import create_core_app
from voidcode.tui.client import CoreRpcClient
from voidcode.tui.local_client import CoreLocalClient

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tally-counter"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _replies() -> list[dict[str, object]]:
    return [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_call("tally-1", "tally_add", delta=5)],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "tally updated"}, finish="stop"),
    ]


@contextmanager
def _provider() -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(_replies())
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _configure(workspace: Path, server: OpenAILoopbackServer, plugin: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    disabled = {
        name: {"enabled": False}
        for name in (
            "ast_grep",
            "btw",
            "compaction",
            "cron",
            "delegation",
            "effectiveness",
            "exec",
            "files",
            "goal",
            "hooks",
            "lsp",
            "mcp",
            "plan",
            "review",
            "skills",
            "todo",
            "user_shell",
            "web",
        )
    }
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/test-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {"endpoint": {"api_key": "loopback-test-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": {**disabled, "tally-counter": {"path": str(plugin)}},
            }
        ),
        encoding="utf-8",
    )


@contextmanager
def _client(arm: str, workspace: Path, state: Path) -> Iterator[SessionClient]:
    if arm == "local":
        with open_core(workspace, state_dir=state) as core:
            yield CoreLocalClient(core, workspace=workspace)
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_core_app(workspace=workspace, state_dir=state, bearer_token="conformance-token")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    client = CoreRpcClient(f"http://127.0.0.1:{port}", token="conformance-token")
    try:
        yield client
    finally:
        client.close()
        server.should_exit = True
        worker.join(timeout=10)


@pytest.fixture(params=["local", "remote"])
def arm_client(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[str, SessionClient, Path, Path]]:
    plugin = tmp_path / "tally-counter"
    shutil.copytree(_EXAMPLE, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    with _provider() as server:
        _configure(workspace, server, plugin)
        with _client(request.param, workspace, state) as client:
            yield request.param, client, workspace, state


def _stored_total(state: Path, session_id: str) -> int | None:
    from collections.abc import Mapping

    store = SqliteStore(state / "voidcode.db")
    try:
        payload, _ = store.plugin_state("tally-counter", "session", session_id)
    finally:
        store.close()
    if payload is None:
        return None
    assert isinstance(payload, Mapping)
    total = payload["total"]
    assert type(total) is int
    return total


def test_command_mutates_durable_state_and_stale_revision_has_no_effect(
    arm_client: tuple[str, SessionClient, Path, Path],
) -> None:
    _, client, workspace, state = arm_client
    session = client.create_session("tally-session")
    catalog = client.command_catalog()
    first = client.invoke_command(session.session_id, "tally", {"delta": 3}, expected_revision=catalog.revision_id)
    assert first.status == "ok"
    assert _stored_total(state, session.session_id) == 3
    with pytest.raises(CoreClientError) as stale:
        client.invoke_command(session.session_id, "tally", {"delta": 100}, expected_revision="not-the-displayed-revision")
    assert stale.value.status_code == 409 and stale.value.code == "stale_revision"
    assert _stored_total(state, session.session_id) == 3


def test_run_drives_tool_and_history_pages_advance(arm_client: tuple[str, SessionClient, Path, Path]) -> None:
    from voidcode.sdk.provider import ProviderMessage
    from voidcode.sdk.transcript import TranscriptEntry, TranscriptTool

    _, client, _, state = arm_client
    session = client.create_session("tally-session")
    catalog = client.command_catalog()
    client.invoke_command(session.session_id, "tally", {"delta": 3}, expected_revision=catalog.revision_id)
    result = client.run(session.session_id, RunSpec(prompt="add five to the tally"))
    assert result.status == "completed"
    assert result.output == "tally updated"
    assert _stored_total(state, session.session_id) == 8
    # limit=1 forces single-source steps including bookkeeping-only pages:
    # the paged union must equal one full page: no missing, dup, or reorder.
    from voidcode.sdk.tools import TextOutput

    seen: list[TranscriptEntry] = []
    cursor = 0
    saw_empty = False
    for _ in range(40):
        page = client.transcript(session.session_id, after_sequence=cursor, limit=1)
        assert page.next_sequence > cursor
        assert page.source_events >= 1
        if not page.entries:
            saw_empty = True
        seen.extend(page.entries)
        cursor = page.next_sequence
        if not page.has_more:
            break
    else:
        raise AssertionError("transcript walk never terminated")
    assert saw_empty
    full = client.transcript(session.session_id, after_sequence=0)
    assert [entry.sequence for entry in seen] == [entry.sequence for entry in full.entries]
    assert [type(entry.content).__name__ for entry in seen] == [type(entry.content).__name__ for entry in full.entries]
    user = next(entry for entry in seen if isinstance(entry.content, ProviderMessage))
    assert user.content.content == "add five to the tally"
    tool = next(entry for entry in seen if isinstance(entry.content, TranscriptTool))
    assert isinstance(tool.content, TranscriptTool)
    assert tool.content.call.tool_name == "tally_add"
    assert isinstance(tool.content.result.output, TextOutput)
    assert "tally is now 8" in tool.content.result.output.text
    history = client.context_history(session.session_id)
    assert history.reached_end
    assert history.records[0].sequence == 1


def test_stream_frames_carry_typed_transcript(arm_client: tuple[str, SessionClient, Path, Path]) -> None:
    from voidcode.sdk.client import EventFrame, HeadFrame
    from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult

    _, client, _, _ = arm_client
    session = client.create_session("tally-session")
    client.run(session.session_id, RunSpec(prompt="add five to the tally"))

    async def collect_events() -> list[tuple[int, str, object]]:
        out: list[tuple[int, str, object]] = []
        async for frame in client.events_stream(session.session_id, follow=False):
            events = frame.events if isinstance(frame, HeadFrame) else (frame.event,) if isinstance(frame, EventFrame) else ()
            for event in events:
                out.append((event.sequence, event.type, None if event.transcript is None else event.transcript.content))
        return out

    rows = asyncio.run(collect_events())
    assert [sequence for sequence, _, _ in rows] == sorted(sequence for sequence, _, _ in rows)
    by_type = {event_type: content for _, event_type, content in rows}
    user = by_type["core.user_message"]
    assert isinstance(user, ProviderMessage)
    assert user.content == "add five to the tally"
    turn = by_type["core.model_turn"]
    assert isinstance(turn, ProviderTurnResult)
    assert turn.output == "tally updated"


def test_invalid_inputs_refused_identically(arm_client: tuple[str, SessionClient, Path, Path]) -> None:
    _, client, _, _ = arm_client
    session = client.create_session()
    catalog = client.command_catalog()
    with pytest.raises(CoreClientError) as bad_id:
        client.get_session("has/slash")
    assert bad_id.value.status_code == 400 and bad_id.value.code == "invalid_params"
    with pytest.raises(CoreClientError) as empty_id:
        client.create_session("")
    assert empty_id.value.status_code == 400 and empty_id.value.code == "invalid_params"
    with pytest.raises(ValueError, match="max_steps"):
        client.run(session.session_id, RunSpec(prompt="hi", max_steps=0))
    with pytest.raises(ValueError, match="max_steps"):
        client.run(session.session_id, RunSpec(prompt="hi", max_steps=True))  # type: ignore[arg-type]
    with pytest.raises(CoreClientError) as bad_decision:
        client.confirm(session.session_id, "req-1", "maybe", expected_revision=catalog.revision_id)  # type: ignore[arg-type]
    assert bad_decision.value.status_code == 400 and bad_decision.value.code == "invalid_params"
    with pytest.raises(ValueError, match="prompt"):
        client.run(session.session_id, RunSpec(prompt="   "))


def test_sse_preserves_business_tool_body() -> None:
    """Consumer regression: an opaque tool body survives the RPC wire unwrapped."""
    from voidcode.core.storage.store import StoredEvent
    from voidcode.sdk.client import StreamEvent
    from voidcode.sdk.codec import encode_tool_call, encode_tool_result
    from voidcode.sdk.tools import OpaqueToolBody, TextOutput, ToolCall, ToolSuccess
    from voidcode.sdk.transcript import TranscriptTool
    from voidcode.transport.projection import event_payload

    body = OpaqueToolBody({"marker": 1})
    call = ToolCall("read", {"path": "x"}, "c1")
    completed = StoredEvent(
        "s",
        9,
        "r",
        "core.tool_completed",
        "kernel",
        "agent",
        "v",
        {"batch_sequence": 4, "call": encode_tool_call(call), "result": encode_tool_result(ToolSuccess("read", TextOutput("hi"), body))},
    )
    stream_entry = StreamEvent.from_payload(event_payload("s", completed)).transcript
    assert stream_entry is not None and isinstance(stream_entry.content, TranscriptTool)
    assert stream_entry.content.result.body is not None
    assert dict(stream_entry.content.result.body.structured_content) == {"marker": 1}
    bookkeeping = StoredEvent("s", 10, "r", "plugin.probe.tick", "kernel", "agent", "v", {})
    assert StreamEvent.from_payload(event_payload("s", bookkeeping)).transcript is None


@pytest.mark.parametrize("stopped_args", [{"confirmed": True}, {"confirmed": False}])
@pytest.mark.parametrize("event_type", ["core.tool_completed", "core.tool_interrupted"])
def test_cancelled_tool_records_decode_through_sse_and_keep_execution_flags(event_type: str, stopped_args: dict[str, bool]) -> None:
    """Cancelled tools keep their confirmed/unconfirmed stop across the RPC wire."""
    from voidcode.core.storage.store import StoredEvent
    from voidcode.sdk.client import StreamEvent
    from voidcode.sdk.codec import encode_tool_call, encode_tool_result
    from voidcode.sdk.tools import ConfirmedStop, TextOutput, ToolCall, ToolFailure, UnconfirmedStop
    from voidcode.sdk.transcript import TranscriptTool
    from voidcode.transport.projection import event_payload

    stopped = ConfirmedStop(True) if stopped_args["confirmed"] else UnconfirmedStop(True)
    failure = ToolFailure("read", "cancelled", TextOutput("no"), None, None, stopped, None, ())
    event = StoredEvent(
        "s",
        11,
        "r",
        event_type,
        "kernel",
        "agent",
        "v",
        {"batch_sequence": 4, "call": encode_tool_call(ToolCall("read", {}, "c1")), "result": encode_tool_result(failure)},
    )
    content = StreamEvent.from_payload(event_payload("s", event)).transcript
    assert content is not None and isinstance(content.content, TranscriptTool)
    result = content.content.result
    assert isinstance(result, ToolFailure) and result.execution is not None
    assert result.execution.execution_stopped == stopped.execution_stopped
    assert result.execution.cancellation_signalled is True
    assert result.execution.side_effect_state == ("settled" if stopped.execution_stopped else "unknown")
