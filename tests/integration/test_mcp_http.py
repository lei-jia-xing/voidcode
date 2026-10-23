"""Real SDK streamable-HTTP sessions, teardown, refusal and reconnect."""

import socket
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer

from voidcode.plugins.mcp import _mcp_config
from voidcode.plugins.mcp.mcp_manager import build_mcp_manager


@contextmanager
def server():
    mcp = MCPServer("http-audit")

    @mcp.tool()
    def echo(text: str) -> str:
        return "echo: " + text

    app = mcp.streamable_http_app(json_response=True)
    failures = {"posts": False}
    responses = []

    async def application(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def record(message):
            if message["type"] == "http.response.start":
                responses.append((scope["method"], message["status"]))
            await send(message)

        if failures["posts"] and scope["method"] == "POST":
            await record({"type": "http.response.start", "status": 503, "headers": []})
            await record({"type": "http.response.body", "body": b"unavailable"})
            return
        await app(scope, receive, record)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        http = uvicorn.Server(uvicorn.Config(application, log_level="critical", timeout_graceful_shutdown=2))
        worker = Thread(target=http.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not http.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert http.started, "HTTP fixture did not start"
            yield f"http://127.0.0.1:{port}/mcp", failures, responses
        finally:
            http.should_exit = True
            worker.join(timeout=5)
            assert not worker.is_alive(), "HTTP fixture did not stop"


def manager_for(url):
    return build_mcp_manager(_mcp_config({"servers": {"demo": {"transport": "remote-http", "url": url}}}))


def test_http_call_and_shutdown_delete_the_server_session(tmp_path: Path):
    with server() as (url, _, responses):
        manager = manager_for(url)
        try:
            assert manager.cached_surface(workspace=tmp_path).servers == {}
            assert [tool.tool_name for tool in manager.list_tools(workspace=tmp_path)] == ["echo"]
            result = manager.call_tool(server_name="demo", tool_name="echo", arguments={"text": "http"}, workspace=tmp_path)
            assert result.content[0]["text"] == "echo: http"
            assert manager.current_state().servers["demo"].status == "running"
        finally:
            manager.shutdown()
        assert manager.current_state().servers["demo"].status == "stopped"
        assert any(method == "DELETE" and 200 <= status < 300 for method, status in responses)
        manager.shutdown()


def test_refused_http_connection_keeps_failure_inspectable(tmp_path: Path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        manager = manager_for(f"http://127.0.0.1:{reserved.getsockname()[1]}/mcp")
        try:
            with pytest.raises(ValueError, match="failed to connect to remote server"):
                manager.list_tools(workspace=tmp_path)
            state = manager.current_state().servers["demo"]
            assert state.status == "failed"
            assert state.error is not None
            assert manager.cached_surface(workspace=tmp_path).servers == {}
        finally:
            manager.shutdown()


def test_http_call_failure_is_retained_and_next_call_reconnects(tmp_path: Path):
    with server() as (url, failures, responses):
        manager = manager_for(url)
        try:
            manager.list_tools(workspace=tmp_path)
            failures["posts"] = True
            with pytest.raises(ValueError):
                manager.call_tool(server_name="demo", tool_name="echo", arguments={"text": "fail"}, workspace=tmp_path)
            failed = manager.current_state().servers["demo"]
            assert failed.status == "failed"
            assert failed.error
            assert ("POST", 503) in responses
            failures["posts"] = False
            recovered = manager.call_tool(server_name="demo", tool_name="echo", arguments={"text": "recovered"}, workspace=tmp_path)
            assert recovered.content[0]["text"] == "echo: recovered"
            assert manager.current_state().servers["demo"].status == "running"
        finally:
            manager.shutdown()
