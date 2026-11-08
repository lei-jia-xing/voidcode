"""Real WebSocket RPC: multiplexing, reconnect, revisions, and durable effects."""

from __future__ import annotations

import asyncio
import json
import shlex
import socket
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect
from websockets.typing import Subprotocol

from voidcode.bootstrap import bundled_sources
from voidcode.sdk.client import CoreClientError, ErrorFrame, EventFrame, HeadFrame
from voidcode.sdk.rpc import RPC_SUBPROTOCOL
from voidcode.transport.http import CoreTransportApp, create_core_app
from voidcode.tui.client import CoreRpcClient

TOKEN = "rpc-test-secret"


@pytest.fixture
def remote(tmp_path: Path) -> Iterator[tuple[str, CoreTransportApp, Path]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins["user_shell"] = {"enabled": True}
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": plugins}), encoding="utf-8")
    app = create_core_app(workspace=workspace, state_dir=tmp_path / "state", bearer_token=TOKEN)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", app, workspace
    finally:
        server.should_exit = True
        worker.join(timeout=15)
        assert not worker.is_alive()


def _socket(base: str):
    return connect(base.replace("http:", "ws:") + "/api/rpc", subprotocols=[Subprotocol(RPC_SUBPROTOCOL)], proxy=None)


def _call(socket, method: str, params: object, identity: str = "call") -> dict:
    socket.send(json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}))
    return json.loads(socket.recv(timeout=10))


def _authenticate(socket) -> None:
    assert _call(socket, "connection.authenticate", {"token": TOKEN})["result"] == {"protocol_version": 1}


def _marker(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


def test_rpc_authentication_and_retired_http_routes(remote) -> None:
    base, app, _workspace = remote
    with _socket(base) as socket:
        reply = _call(socket, "session.create", {})
        assert reply["error"]["code"] == -32001
        assert TOKEN not in json.dumps(reply)
        with pytest.raises(ConnectionClosed):
            socket.recv(timeout=2)
    with _socket(base) as socket:
        assert _call(socket, "connection.authenticate", {"token": "wrong"})["error"]["data"]["status"] == 401
    with pytest.raises(InvalidStatus):
        connect(base.replace("http:", "ws:") + "/api/rpc", proxy=None)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with httpx.Client(base_url=base, headers=headers, trust_env=False) as client:
        assert client.get("/api/health").status_code == 200
        for path in ("/api/commands", "/api/sessions", "/api/sessions/s/events"):
            assert client.get(path).status_code == 404
        document = client.get("/api/openapi.json").json()
    assert set(document["paths"]) == {"/api/health"}
    assert set(document["x-rpc-methods"]) >= {"session.run", "session.cancel", "session.subscribe", "command.invoke", "action.confirm"}
    assert app._core is not None and app._core.sessions() == ()


def test_rpc_errors_batch_notifications_and_revision_binding(remote) -> None:
    base, _app, _workspace = remote
    with _socket(base) as socket:
        _authenticate(socket)
        socket.send("{")
        assert json.loads(socket.recv(timeout=2))["error"]["code"] == -32700
        socket.send("[]")
        assert json.loads(socket.recv(timeout=2))["error"]["code"] == -32600
        for invalid in (1, {}, {"jsonrpc": "1.0", "id": 1, "method": "session.list"}):
            socket.send(json.dumps(invalid))
            assert json.loads(socket.recv(timeout=2))["error"]["code"] == -32600
        assert _call(socket, "missing", {})["error"]["code"] == -32601
        assert _call(socket, "session.list", {"limit": True})["error"]["code"] == -32602
        assert _call(socket, "session.create", {"session_id": "../bad"})["error"]["code"] == -32602
        assert _call(socket, "session.list", {"extra": 1})["error"]["code"] == -32602
        socket.send(
            json.dumps(
                [
                    {"jsonrpc": "2.0", "id": "one", "method": "session.create", "params": {"session_id": "one"}},
                    {"jsonrpc": "2.0", "id": "two", "method": "command.catalog"},
                    {"jsonrpc": "2.0", "method": "session.list"},
                ]
            )
        )
        replies = json.loads(socket.recv(timeout=10))
        assert {reply["id"] for reply in replies} == {"one", "two"}
        socket.send(json.dumps({"jsonrpc": "2.0", "method": "session.create", "params": {"session_id": "notification"}}))
        # Notifications have no response, but their side effect is real.
        deadline = time.monotonic() + 5
        while True:
            sessions = _call(socket, "session.list", {})["result"]["sessions"]
            if any(row["session_id"] == "notification" for row in sessions):
                break
            assert time.monotonic() < deadline
        missing_revision = _call(socket, "command.invoke", {"session_id": "one", "name": "shell"})
        assert missing_revision["error"]["code"] == -32602
        stale = _call(
            socket,
            "command.invoke",
            {"session_id": "one", "name": "shell", "expected_revision": "stale", "arguments": {"command": "echo wrong > wrong.txt"}},
        )
        assert stale["error"]["data"]["code"] == "stale_revision"


def test_long_command_can_be_cancelled_on_the_same_connection(remote) -> None:
    base, _app, workspace = remote
    with CoreRpcClient(base, token=TOKEN) as client, ThreadPoolExecutor() as pool:
        client.create_session("long")
        marker = workspace / "started"
        script = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30); Path('late').touch()"
        command = shlex.join([sys.executable, "-c", script])
        future = pool.submit(client.invoke_command, "long", "shell", {"command": command}, client.command_catalog().revision_id)
        _marker(marker)
        assert client.get_session("long").session_id == "long"
        assert client.cancel("long").cancellation_requested
        result = future.result(timeout=5)
        assert result.status == "error" and "cancelled" in (result.error or "")
        assert not (workspace / "late").exists()


def test_disconnect_does_not_cancel_or_resend_mutations(remote) -> None:
    base, _app, workspace = remote
    client = CoreRpcClient(base, token=TOKEN)
    with CoreRpcClient(base, token=TOKEN) as observer, ThreadPoolExecutor() as pool:
        client.create_session("durable")
        revision = client.command_catalog().revision_id
        script = "from pathlib import Path; import time; p=Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        script += "exec(\"while not Path('release').exists(): time.sleep(0.02)\")"
        future = pool.submit(client.invoke_command, "durable", "shell", {"command": shlex.join([sys.executable, "-c", script])}, revision)
        try:
            _marker(workspace / "count")
            client.close()
            with pytest.raises(CoreClientError, match="outcome may be unknown"):
                future.result(timeout=5)
            assert observer.get_session("durable").session_id == "durable"
        finally:
            (workspace / "release").touch()
        deadline = time.monotonic() + 10
        while True:
            events = observer.show("durable", events=True)["events"]
            if any(event["type"] == "core.command_completed" for event in events):
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert (workspace / "count").read_text() == "x"
        assert len([event for event in events if event["type"] == "core.command_completed"]) == 1


def test_subscription_replays_in_bounded_pages_and_resumes_from_applied_cursor(remote) -> None:
    base, app, _workspace = remote
    with CoreRpcClient(base, token=TOKEN) as client:
        client.create_session("history")
        assert app._core is not None
        # Use genuine commands to create more events than one replay page.
        for _ in range(120):
            client.invoke_command("history", "shell", {"command": "true"}, client.command_catalog().revision_id)

        async def replay(cursor: int):
            events = []
            async for frame in client.events_stream("history", after_sequence=cursor, follow=False):
                assert not isinstance(frame, ErrorFrame)
                if isinstance(frame, HeadFrame):
                    events.extend(frame.events)
                elif isinstance(frame, EventFrame):
                    events.append(frame.event)
            return events

        first = asyncio.run(replay(0))
        assert len(first) > 100
        sequences = [event.sequence for event in first]
        assert sequences == sorted(set(sequences))
        mid = sequences[60]
        second = asyncio.run(replay(mid))
        assert [event.sequence for event in second] == [sequence for sequence in sequences if sequence > mid]


def test_subscription_credit_pauses_replay_without_blocking_other_calls(remote) -> None:
    from voidcode.core.storage.store import EventDraft
    from voidcode.sdk.rpc import RPC_EVENT_WINDOW

    base, app, _workspace = remote
    core = app._core
    assert core is not None
    session = core.create_session("credit")
    core.store.commit_staged(
        "credit",
        expected_sequence=session.last_event_sequence,
        expected_run_id=session.run_id,
        run_id="credit-run",
        revision_id=core.revision_id,
        events=tuple(EventDraft("plugin.test.progress", "plugin.test", "plugin", {"index": index}) for index in range(RPC_EVENT_WINDOW + 10)),
        status=session.status,
        checkpoint_kind=None,
        checkpoint_payload=None,
    )
    with _socket(base) as socket:
        _authenticate(socket)
        receipt = _call(socket, "session.subscribe", {"subscription_id": "credit", "session_id": "credit", "follow": False})
        assert receipt["result"]["subscription_id"] == "credit"
        head = json.loads(socket.recv(timeout=5))["params"]["frame"]
        assert len(head["events"]) == RPC_EVENT_WINDOW
        with pytest.raises(TimeoutError):
            socket.recv(timeout=0.2)
        # A stalled consumer does not stall the request/response side of its connection.
        assert "commands" in _call(socket, "command.catalog", {})["result"]
        invalid = _call(socket, "session.ack", {"subscription_id": "credit", "through_sequence": head["events"][-1]["sequence"] + 1})
        assert "error" in invalid
        socket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session.ack",
                    "params": {
                        "subscription_id": "credit",
                        "through_sequence": head["events"][-1]["sequence"],
                    },
                }
            )
        )
        tail = []
        while True:
            notice = json.loads(socket.recv(timeout=5))
            if notice["method"] == "subscription.closed":
                break
            tail.append(notice["params"]["frame"]["event"])
        assert len(tail) == 10
        assert tail[0]["sequence"] == head["events"][-1]["sequence"] + 1


def test_command_input_returns_dynamic_values_without_invoking(remote) -> None:
    from voidcode.sdk.views import FormViewData

    base, app, workspace = remote
    path = workspace / "voidcode.json"
    config = json.loads(path.read_text())
    config["plugins"]["subagents"] = {"enabled": True}
    config["plugins"]["workflows"] = {"enabled": True, "config": {"workflows": {"review": "flows/review.py"}}}
    path.write_text(json.dumps(config))
    client = CoreRpcClient(base, token=TOKEN)
    try:
        client.create_session("input")
        catalog = client.command_catalog()
        assert client.invoke_command("input", "core.reload", {}, catalog.revision_id).status == "ok"
        catalog = client.command_catalog()
        assert next(command for command in catalog.commands if command.name == "workflow.run").has_input
        before = client.get_session("input").last_event_sequence
        workflow = client.command_input("input", "workflow.run", catalog.revision_id)
        assert workflow.revision_id == catalog.revision_id
        form = FormViewData.from_payload(workflow.presentation.data)
        assert [option.id for option in form.fields[0].options] == ["review"]
        assert client.get_session("input").last_event_sequence == before
        assert app._core is not None
        assert app._core.store.plugin_state("workflows", "session", "input")[0] is None
        with pytest.raises(CoreClientError, match="stale"):
            client.command_input("input", "workflow.run", "stale-revision")
    finally:
        client.close()
