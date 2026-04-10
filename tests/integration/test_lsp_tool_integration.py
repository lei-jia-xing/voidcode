from __future__ import annotations

import importlib
import json
import socket
import threading
from pathlib import Path
from typing import cast

import pytest


def _read_framed_json(conn: socket.socket) -> dict[str, object] | None:
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = conn.recv(1)
        if not chunk:
            return None
        header += chunk

    header_text = header.decode("ascii", errors="ignore")
    content_length = None
    for line in header_text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break
    if content_length is None:
        return None

    payload = b""
    remaining = content_length
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            return None
        payload += chunk
        remaining -= len(chunk)

    return json.loads(payload.decode("utf-8"))


def _send_framed_json(conn: socket.socket, message: dict[str, object]) -> None:
    body = json.dumps(message).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    conn.sendall(header + body)


def _run_handshake_server(
    *,
    host: str,
    port: int,
    stop_event: threading.Event,
    received_methods: list[str],
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        server.settimeout(0.2)

        initialized = False
        while not stop_event.is_set():
            try:
                conn, _addr = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            with conn:
                message = _read_framed_json(conn)
                if message is None:
                    continue

                method = message.get("method")
                req_id = message.get("id")
                if isinstance(method, str):
                    received_methods.append(method)

                if method == "initialize":
                    initialized = True
                    _send_framed_json(
                        conn,
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"capabilities": {}},
                        },
                    )
                    continue

                if method == "initialized":
                    # notification; no response
                    continue

                if not initialized:
                    _send_framed_json(
                        conn,
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32002,
                                "message": "Server not initialized",
                            },
                        },
                    )
                    continue

                _send_framed_json(
                    conn,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"ok": True, "method": method},
                    },
                )


def _pick_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_lsp_tool_performs_initialize_handshake_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "sample.py"
    file_path.write_text("x = 1\n", encoding="utf-8")

    host = "127.0.0.1"
    port = _pick_tcp_port()
    stop_event = threading.Event()
    received_methods: list[str] = []
    server = threading.Thread(
        target=_run_handshake_server,
        kwargs={
            "host": host,
            "port": port,
            "stop_event": stop_event,
            "received_methods": received_methods,
        },
        daemon=True,
    )
    server.start()

    monkeypatch.setenv("VOIDCODE_LSP_HOST", host)
    monkeypatch.setenv("VOIDCODE_LSP_PORT", str(port))

    try:
        lsp_module = importlib.import_module("voidcode.tools.lsp")
        contracts_module = importlib.import_module("voidcode.tools.contracts")
        tool = lsp_module.LspTool()

        result = tool.invoke(
            contracts_module.ToolCall(
                tool_name="lsp",
                arguments={
                    "operation": "textDocument/definition",
                    "filePath": "sample.py",
                    "line": 1,
                    "character": 1,
                },
            ),
            workspace=tmp_path,
        )

        assert result.status == "ok"
        response = result.data.get("lsp_response")
        assert isinstance(response, dict)
        response_dict = cast(dict[str, object], response)
        assert response_dict.get("result") == {"ok": True, "method": "textDocument/definition"}
        assert received_methods[:3] == [
            "initialize",
            "initialized",
            "textDocument/definition",
        ]
    finally:
        stop_event.set()
        server.join(timeout=1)


def test_lsp_call_hierarchy_incoming_uses_prepare_item_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "sample.py"
    file_path.write_text("def f():\n    pass\n", encoding="utf-8")

    host = "127.0.0.1"
    port = _pick_tcp_port()
    stop_event = threading.Event()
    received_methods: list[str] = []
    server = threading.Thread(
        target=_run_handshake_server,
        kwargs={
            "host": host,
            "port": port,
            "stop_event": stop_event,
            "received_methods": received_methods,
        },
        daemon=True,
    )
    server.start()

    monkeypatch.setenv("VOIDCODE_LSP_HOST", host)
    monkeypatch.setenv("VOIDCODE_LSP_PORT", str(port))

    try:
        lsp_module = importlib.import_module("voidcode.tools.lsp")
        contracts_module = importlib.import_module("voidcode.tools.contracts")
        tool = lsp_module.LspTool()

        # Monkeypatch send_request to assert incomingCalls gets {item: ...}
        original_send = tool._send_request

        def _wrapped_send(method: str, params: dict[str, object]) -> dict[str, object] | None:
            if method == "textDocument/prepareCallHierarchy":
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": [
                        {
                            "name": "f",
                            "kind": 12,
                            "uri": file_path.resolve().as_uri(),
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5},
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 1},
                            },
                        }
                    ],
                }
            if method == "callHierarchy/incomingCalls":
                assert "item" in params
                assert "textDocument" not in params
                assert "position" not in params
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": [],
                }
            return original_send(method, params)

        monkeypatch.setattr(tool, "_send_request", _wrapped_send)

        result = tool.invoke(
            contracts_module.ToolCall(
                tool_name="lsp",
                arguments={
                    "operation": "callHierarchy/incomingCalls",
                    "filePath": "sample.py",
                    "line": 1,
                    "character": 1,
                },
            ),
            workspace=tmp_path,
        )

        assert result.status == "ok"
    finally:
        stop_event.set()
        server.join(timeout=1)
