from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.config import RuntimeConfig, RuntimeLspConfig, RuntimeLspServerConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import GraphRunRequest, SessionState, VoidCodeRuntime
from voidcode.tools.contracts import ToolCall
from voidcode.tools.lsp import LspTool


def _write_fake_lsp_server(script_path: Path) -> None:
    script_path.write_text(
        "import json\n"
        "import sys\n\n"
        "def read_message():\n"
        '    header = b""\n'
        '    while b"\\r\\n\\r\\n" not in header:\n'
        "        chunk = sys.stdin.buffer.read(1)\n"
        "        if not chunk:\n"
        "            return None\n"
        "        header += chunk\n"
        "    content_length = None\n"
        '    for line in header.decode("ascii", errors="ignore").split("\\r\\n"):\n'
        '        if line.lower().startswith("content-length:"):\n'
        '            content_length = int(line.split(":", 1)[1].strip())\n'
        "            break\n"
        "    if content_length is None:\n"
        "        return None\n"
        "    body = sys.stdin.buffer.read(content_length)\n"
        "    if not body:\n"
        "        return None\n"
        '    return json.loads(body.decode("utf-8"))\n\n'
        "def send_message(message):\n"
        '    body = json.dumps(message).encode("utf-8")\n'
        '    header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii")\n'
        "    sys.stdout.buffer.write(header + body)\n"
        "    sys.stdout.buffer.flush()\n\n"
        "while True:\n"
        "    message = read_message()\n"
        "    if message is None:\n"
        "        break\n"
        '    method = message.get("method")\n'
        '    request_id = message.get("id")\n'
        '    if method == "initialize":\n'
        "        send_message(\n"
        '            {"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {}}}\n'
        "        )\n"
        "        continue\n"
        '    if method in {"initialized", "shutdown", "exit"}:\n'
        '        if method == "shutdown":\n'
        '            send_message({"jsonrpc": "2.0", "id": request_id, "result": None})\n'
        '        if method == "exit":\n'
        "            break\n"
        "        continue\n"
        '    if method == "textDocument/prepareCallHierarchy":\n'
        "        send_message(\n"
        "            {\n"
        '                "jsonrpc": "2.0",\n'
        '                "id": request_id,\n'
        '                "result": [\n'
        "                    {\n"
        '                        "name": "f",\n'
        '                        "kind": 12,\n'
        '                        "uri": "file:///tmp/sample.py",\n'
        '                        "range": {\n'
        '                            "start": {"line": 0, "character": 0},\n'
        '                            "end": {"line": 0, "character": 1},\n'
        "                        },\n"
        '                        "selectionRange": {\n'
        '                            "start": {"line": 0, "character": 0},\n'
        '                            "end": {"line": 0, "character": 1},\n'
        "                        },\n"
        "                    }\n"
        "                ],\n"
        "            }\n"
        "        )\n"
        "        continue\n"
        "    send_message(\n"
        '        {"jsonrpc": "2.0", "id": request_id, "result": {"ok": True, "method": method}}\n'
        "    )\n",
        encoding="utf-8",
    )


def _build_runtime_with_lsp(tmp_path: Path, *, command: tuple[str, ...]) -> VoidCodeRuntime:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            lsp=RuntimeLspConfig(
                enabled=True,
                servers={"pyright": RuntimeLspServerConfig(command=command)},
            )
        ),
    )
    return runtime


def _build_runtime_with_lsp_script(tmp_path: Path, script_body: str) -> VoidCodeRuntime:
    server_script = tmp_path / "fake_lsp_server.py"
    server_script.write_text(script_body, encoding="utf-8")
    return _build_runtime_with_lsp(tmp_path, command=(sys.executable, "-u", str(server_script)))


@dataclass(slots=True)
class _StubLspStep:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[object, ...] = ()
    is_finished: bool = False


class _LspGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubLspStep:
        _ = request, session
        if not tool_results:
            return _StubLspStep(
                tool_call=ToolCall(
                    tool_name="lsp",
                    arguments={
                        "operation": "textDocument/definition",
                        "filePath": "sample.py",
                        "line": 1,
                        "character": 1,
                    },
                )
            )
        return _StubLspStep(output="done", is_finished=True)


def test_runtime_managed_lsp_tool_starts_server_and_returns_response(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    server_script = tmp_path / "fake_lsp_server.py"
    _write_fake_lsp_server(server_script)

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_LspGraph(),
        config=RuntimeConfig(
            lsp=RuntimeLspConfig(
                enabled=True,
                servers={
                    "pyright": RuntimeLspServerConfig(
                        command=(sys.executable, "-u", str(server_script))
                    )
                },
            )
        ),
    )

    result = runtime.run(RuntimeRequest(prompt="lsp please"))

    tool_completed = next(
        event for event in result.events if event.event_type == "runtime.tool_completed"
    )
    started_event = next(
        event for event in result.events if event.event_type == "runtime.lsp_server_started"
    )
    response = cast(dict[str, object], tool_completed.payload["lsp_response"])
    assert response["result"] == {"ok": True, "method": "textDocument/definition"}
    assert started_event.payload["workspace_root"] == str(tmp_path)
    assert runtime.current_lsp_state().servers["pyright"].status == "running"

    shutdown_events = runtime.shutdown_lsp()
    assert [event.event_type for event in shutdown_events] == ["runtime.lsp_server_stopped"]


def test_runtime_managed_lsp_tool_rejects_disabled_manager(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    runtime = VoidCodeRuntime(workspace=tmp_path, config=RuntimeConfig())

    tool = LspTool(requester=runtime.request_lsp)

    with pytest.raises(ValueError, match="disabled"):
        _ = tool.invoke(
            ToolCall(
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


def test_runtime_managed_lsp_tool_surfaces_failed_startup_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    runtime = _build_runtime_with_lsp(
        tmp_path,
        command=("definitely-not-a-real-lsp-binary",),
    )
    tool = LspTool(requester=runtime.request_lsp)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ValueError, match="failed to start LSP server pyright"),
    ):
        _ = tool.invoke(
            ToolCall(
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

    assert runtime.current_lsp_state().servers["pyright"].status == "failed"
    assert "failed to start LSP server pyright" in caplog.text
    assert "installed and available on PATH" in caplog.text


def test_runtime_managed_lsp_tool_uses_builtin_root_markers_for_workspace_selection(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "apps" / "demo"
    sample_file = project_root / "src" / "sample.py"
    project_root.mkdir(parents=True)
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text("x = 1\n", encoding="utf-8")
    (project_root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    server_script = tmp_path / "fake_lsp_server.py"
    _write_fake_lsp_server(server_script)

    @dataclass(slots=True)
    class _NestedLspGraph:
        def step(
            self,
            request: GraphRunRequest,
            tool_results: tuple[object, ...],
            *,
            session: SessionState,
        ) -> _StubLspStep:
            _ = request, session
            if not tool_results:
                return _StubLspStep(
                    tool_call=ToolCall(
                        tool_name="lsp",
                        arguments={
                            "operation": "textDocument/definition",
                            "filePath": "apps/demo/src/sample.py",
                            "line": 1,
                            "character": 1,
                        },
                    )
                )
            return _StubLspStep(output="done", is_finished=True)

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_NestedLspGraph(),
        config=RuntimeConfig(
            lsp=RuntimeLspConfig(
                enabled=True,
                servers={
                    "pyright": RuntimeLspServerConfig(
                        command=(sys.executable, "-u", str(server_script))
                    )
                },
            )
        ),
    )

    result = runtime.run(RuntimeRequest(prompt="lsp please"))

    started_event = next(
        event for event in result.events if event.event_type == "runtime.lsp_server_started"
    )

    assert started_event.payload["workspace_root"] == str(project_root)


def test_runtime_managed_lsp_tool_surfaces_protocol_failure_from_server(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    runtime = _build_runtime_with_lsp_script(
        tmp_path,
        "import json\n"
        "import sys\n\n"
        "def read_message():\n"
        '    header = b""\n'
        '    while b"\\r\\n\\r\\n" not in header:\n'
        "        chunk = sys.stdin.buffer.read(1)\n"
        "        if not chunk:\n"
        "            return None\n"
        "        header += chunk\n"
        "    content_length = None\n"
        '    for line in header.decode("ascii", errors="ignore").split("\\r\\n"):\n'
        '        if line.lower().startswith("content-length:"):\n'
        '            content_length = int(line.split(":", 1)[1].strip())\n'
        "            break\n"
        "    if content_length is None:\n"
        "        return None\n"
        "    body = sys.stdin.buffer.read(content_length)\n"
        "    if not body:\n"
        "        return None\n"
        '    return json.loads(body.decode("utf-8"))\n\n'
        "def send_raw(payload):\n"
        '    body = payload.encode("utf-8")\n'
        '    header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii")\n'
        "    sys.stdout.buffer.write(header + body)\n"
        "    sys.stdout.buffer.flush()\n\n"
        "while True:\n"
        "    message = read_message()\n"
        "    if message is None:\n"
        "        break\n"
        '    method = message.get("method")\n'
        '    request_id = message.get("id")\n'
        '    if method == "initialize":\n'
        "        send_raw(\n"
        "            json.dumps(\n"
        '                {"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {}}}\n'
        "            )\n"
        "        )\n"
        "        continue\n"
        '    if method in {"initialized", "shutdown", "exit"}:\n'
        '        if method == "shutdown":\n'
        '            send_raw(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": None}))\n'
        '        if method == "exit":\n'
        "            break\n"
        "        continue\n"
        '    send_raw("[]")\n',
    )
    tool = LspTool(requester=runtime.request_lsp)

    with pytest.raises(ValueError, match="LSP protocol error"):
        _ = tool.invoke(
            ToolCall(
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
