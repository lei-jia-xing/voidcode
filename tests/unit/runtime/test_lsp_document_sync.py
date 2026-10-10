from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import output_text
from voidcode.core.turns import ReportedCall
from voidcode.formatter.config import RuntimeFormatterPresetConfig
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.runtime.config import RuntimeLspConfig, RuntimeLspServerConfig
from voidcode.runtime.lsp import LspRequest, ManagedLspManager
from voidcode.runtime.tool_execution import RuntimeToolExecutor
from voidcode.runtime.tool_provider import materialize_builtin_tool
from voidcode.tools.contracts import Tool, ToolCall, ToolInvocation, ToolResult
from voidcode.tools.guards import read_tracking_for_tool_results
from voidcode.tools.output import read_tool_output_artifact


def _write_native_lsp_server(tmp_path: Path) -> tuple[Path, Path]:
    server = tmp_path / "server.py"
    events_path = tmp_path / "events.jsonl"
    server.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "events = Path(sys.argv[1])\n"
        "events.with_suffix('.pid').write_text(str(os.getpid()))\n"
        "document_version = 0\n"
        "def read_message():\n"
        "    header = bytearray()\n"
        "    while not header.endswith(b'\\r\\n\\r\\n'):\n"
        "        char = sys.stdin.buffer.read(1)\n"
        "        if not char:\n"
        "            return None\n"
        "        header.extend(char)\n"
        "    length = int(next(line.split(b':', 1)[1].strip() for line in header.split(b'\\r\\n') if line.lower().startswith(b'content-length:')))\n"
        "    return json.loads(sys.stdin.buffer.read(length))\n"
        "def write_message(message):\n"
        "    body = json.dumps(message).encode()\n"
        "    sys.stdout.buffer.write(f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode() + body)\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    message = read_message()\n"
        "    if message is None:\n"
        "        break\n"
        "    with events.open('a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(message) + '\\n')\n"
        "    method = message.get('method')\n"
        "    if method == 'textDocument/didOpen':\n"
        "        document_version = message['params']['textDocument']['version']\n"
        "    elif method == 'textDocument/didChange':\n"
        "        document_version = message['params']['textDocument']['version']\n"
        "    if method == 'exit':\n"
        "        break\n"
        "    if 'id' in message:\n"
        "        if method == 'initialize':\n"
        "            result = {'capabilities': {\n"
        "                'positionEncoding': 'utf-8',\n"
        "                'textDocumentSync': {'openClose': True, 'change': 2},\n"
        "                'hoverProvider': True, 'renameProvider': True,\n"
        "            }}\n"
        "        elif method == 'shutdown':\n"
        "            result = None\n"
        "        elif method == 'textDocument/rename':\n"
        "            params = message['params']\n"
        "            uri = params['textDocument']['uri']\n"
        "            if params.get('newName') == 'many':\n"
        "                edits = [\n"
        "                    {'range': {'start': {'line': index, 'character': 9 if index == 0 else 0},\n"
        "                              'end': {'line': index, 'character': 10 if index == 0 else 1}},\n"
        "                     'newText': 'Y'}\n"
        "                    for index in range(51)\n"
        "                ]\n"
        "            else:\n"
        "                edits = [{'range': {'start': {'line': 0, 'character': 9},\n"
        "                                   'end': {'line': 0, 'character': 10}},\n"
        "                          'newText': 'y'}]\n"
        "            result = {'documentChanges': [{\n"
        "                'textDocument': {'uri': uri, 'version': document_version},\n"
        "                'edits': edits,\n"
        "            }]}\n"
        "        elif method == 'textDocument/hover':\n"
        "            if message['params']['position']['line'] >= 99:\n"
        "                result = {'contents': {'kind': 'markdown', 'value': 'h' * 921600}}\n"
        "            else:\n"
        "                result = {'contents': 'ok'}\n"
        "        else:\n"
        "            result = {'contents': 'ok'}\n"
        "        write_message({'jsonrpc': '2.0', 'id': message['id'], 'result': result})\n",
        encoding="utf-8",
    )
    return server, events_path


def test_managed_lsp_negotiates_encoding_and_tracks_open_document_versions(tmp_path: Path) -> None:
    server, events_path = _write_native_lsp_server(tmp_path)
    target = tmp_path / "sample.py"
    first = "alpha 🚀\r\n".encode()
    second = "beta 🚀\r\n".encode()
    target.write_bytes(first)
    manager = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={
                "native-test": RuntimeLspServerConfig(
                    command=(sys.executable, str(server), str(events_path)),
                    extensions=(".py",),
                    languages=("python",),
                )
            },
        )
    )
    request = LspRequest(
        server_name="native-test",
        method="textDocument/hover",
        params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 0}},
        workspace=tmp_path,
    )
    try:
        opened = manager.request(request)
        assert opened.position_encoding == "utf-8"
        assert opened.document_state is not None
        assert opened.document_state.version == 1
        assert opened.document_state.content_hash == hashlib.sha256(first).hexdigest()
        assert manager.document_state(file_path=target, workspace=tmp_path) == opened.document_state

        target.write_bytes(second)
        changed = manager.request(request)
        assert changed.document_state is not None
        assert changed.document_state.version == 2
        assert changed.document_state.content_hash == hashlib.sha256(second).hexdigest()

        target.write_bytes(b"external\r\n")
        assert manager.document_state(file_path=target, workspace=tmp_path) is None
    finally:
        manager.shutdown()

    messages = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    initialize = next(message for message in messages if message.get("method") == "initialize")
    assert initialize["params"]["capabilities"]["general"]["positionEncodings"] == ["utf-16", "utf-8", "utf-32"]
    opened_message = next(message for message in messages if message.get("method") == "textDocument/didOpen")
    assert opened_message["params"]["textDocument"]["version"] == 1
    assert opened_message["params"]["textDocument"]["text"] == first.decode("utf-8")
    changed_message = next(message for message in messages if message.get("method") == "textDocument/didChange")
    assert changed_message["params"]["textDocument"]["version"] == 2
    assert changed_message["params"]["contentChanges"] == [{"text": second.decode("utf-8")}]


def _invoke(
    executor: RuntimeToolExecutor,
    tool: Tool,
    call: ToolCall,
    context: ToolContext,
) -> ToolResult:
    stream = executor.invoke(
        tool=tool,
        invocation=ToolInvocation(tool_call=call, tool_definition=tool.definition, context=context),
    )
    try:
        next(stream)
    except StopIteration as stopped:
        result = stopped.value
    else:
        raise AssertionError("unexpected tool execution progress")
    if isinstance(result, Exception):
        raise result
    assert result is not None
    return result


def test_runtime_lsp_rename_workspace_edit_uses_native_state_and_formatter(tmp_path: Path) -> None:
    server, events_path = _write_native_lsp_server(tmp_path)
    target = tmp_path / "sample.py"
    original = "name 🚀x\r\n".encode()
    target.write_bytes(original)
    formatter = tmp_path / "formatter.py"
    formatter.write_text(
        "from pathlib import Path\nimport sys\npath = Path(sys.argv[1])\npath.write_bytes(path.read_bytes().replace(b'y\\r\\n', b'Y\\r\\n'))\n",
        encoding="utf-8",
    )
    manager = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={
                "native-test": RuntimeLspServerConfig(
                    command=(sys.executable, str(server), str(events_path)),
                    extensions=(".py",),
                    languages=("python",),
                )
            },
        )
    )

    def request_lsp(*, server_name: str | None, method: str, params: dict[str, object], workspace: Path):
        return manager.request(LspRequest(server_name=server_name, method=method, params=params, workspace=workspace))

    hooks = RuntimeHooksConfig(
        format_on_write=True,
        formatter_presets={
            "python": RuntimeFormatterPresetConfig(
                command=(sys.executable, str(formatter)),
                extensions=(".py",),
                cwd_policy="workspace",
            )
        },
    )
    response_artifacts: dict[str, dict[str, object]] = {}

    def read_artifact(
        *,
        session_id: str,
        artifact_id: str,
        offset: int,
        limit: int,
        byte_offset: int | None = None,
        byte_limit: int | None = None,
    ):
        artifact = response_artifacts.get(artifact_id)
        if artifact is None or artifact.get("session_id") != session_id:
            raise ValueError("LSP response artifact is not owned by this session")
        return read_tool_output_artifact(
            artifact,
            offset=offset,
            limit=limit,
            byte_offset=byte_offset,
            byte_limit=byte_limit,
        )

    executor = RuntimeToolExecutor(
        workspace=tmp_path,
        lsp_request=request_lsp,
        lsp_document_state=manager.document_state,
        read_artifact=read_artifact,
    )
    edit_tool = materialize_builtin_tool("apply_workspace_edit", hooks_config=hooks)
    base_context = ToolContext(session_id="native-lsp-workspace-edit")
    try:
        read_call = ToolCall(tool_name="read", arguments={"path": "sample.py"}, tool_call_id="read-before")
        read_result = _invoke(executor, materialize_builtin_tool("read"), read_call, base_context)
        assert read_result.body is not None
        read_body = read_result.body.as_payload()
        expected_hash = str(read_body["content_hash"])
        report = ReportedCall(
            tool_call_id="read-before",
            final_tool_name="read",
            authorized_arguments={"path": "sample.py"},
            result=read_result,
        )
        tracking = read_tracking_for_tool_results(tool_results=(report,), workspace=tmp_path)
        context = replace(
            base_context,
            read_paths=tracking.read_paths,
            read_lines=tracking.read_lines,
            read_whole_files=tracking.read_whole_files,
            lsp_document_state=lambda *, file_path, workspace: None,
        )
        rename_result = _invoke(
            executor,
            materialize_builtin_tool("lsp"),
            ToolCall(
                tool_name="lsp",
                arguments={"operation": "rename", "path": "sample.py", "line": 1, "character": 1, "newName": "y"},
                tool_call_id="rename",
            ),
            context,
        )
        assert rename_result.body is not None
        rename_body = rename_result.body.as_payload()
        document_state = rename_body["document_state"]
        assert isinstance(document_state, dict)
        assert document_state["version"] == 1
        assert document_state["position_encoding"] == "utf-8"
        assert document_state["content_hash"] == expected_hash
        visible_rename = output_text(rename_result.output) or ""
        assert "line 1:10-1:11 -> y" in visible_rename
        assert "position encoding utf-8" in visible_rename

        artifact = rename_body["body_artifact"]
        assert isinstance(artifact, dict)
        assert rename_body["item_count"] == 1
        assert "lsp_response" not in rename_body
        assert artifact["mime"] == "application/json"
        assert artifact["content_type"] == "application/json"
        artifact_id = artifact["artifact_id"]
        assert isinstance(artifact_id, str)
        response_artifacts[artifact_id] = artifact
        response_uri = rename_body["response_reference"]
        assert isinstance(response_uri, str)
        assert response_uri in visible_rename
        response_read = _invoke(
            executor,
            materialize_builtin_tool("read"),
            ToolCall(
                tool_name="read",
                arguments={"path": response_uri, "offset": 1, "limit": 1000},
                tool_call_id="read-lsp-response",
            ),
            base_context,
        )
        assert response_read.body is not None
        response_page = response_read.body.as_payload()
        assert response_page["truncated"] is False
        assert response_page["next_offset"] is None
        response_text = response_page["raw_content"]
        assert isinstance(response_text, str)
        response = json.loads(response_text)
        assert isinstance(response, dict)
        response_result = response["result"]
        assert isinstance(response_result, dict)
        document_changes = response_result["documentChanges"]
        assert isinstance(document_changes, list)
        document_change = document_changes[0]
        assert isinstance(document_change, dict)
        text_document = document_change["textDocument"]
        assert isinstance(text_document, dict)
        edits = document_change["edits"]
        assert isinstance(edits, list)
        workspace_edit = edits[0]
        assert isinstance(workspace_edit, dict)
        position_range = workspace_edit["range"]
        assert isinstance(position_range, dict)
        start_position = position_range["start"]
        end_position = position_range["end"]
        assert isinstance(start_position, dict) and isinstance(end_position, dict)
        apply_result = _invoke(
            executor,
            edit_tool,
            ToolCall(
                tool_name="apply_workspace_edit",
                arguments={
                    "edits": [
                        {
                            "path": "sample.py",
                            "startLine": int(start_position["line"]) + 1,
                            "startCharacter": int(start_position["character"]) + 1,
                            "endLine": int(end_position["line"]) + 1,
                            "endCharacter": int(end_position["character"]) + 1,
                            "newText": workspace_edit["newText"],
                            "expectedHash": expected_hash,
                            "expectedVersion": text_document["version"],
                        }
                    ]
                },
                tool_call_id="apply-rename",
            ),
            context,
        )
        assert apply_result.body is not None
        applied = apply_result.body.as_payload()
        files = applied["files"]
        assert isinstance(files, list)
        final_file = files[0]
        assert isinstance(final_file, dict)
        final_bytes = b"name \xf0\x9f\x9a\x80Y\r\n"
        assert target.read_bytes() == final_bytes
        assert final_file["content_hash"] == hashlib.sha256(final_bytes).hexdigest()
        assert "-name 🚀x\r\n" in final_file["diff"]
        assert "+name 🚀Y\r\n" in final_file["diff"]
        formatter_result = final_file["formatter"]
        assert isinstance(formatter_result, dict)
        assert formatter_result["status"] == "formatted"

        read_after = _invoke(
            executor,
            materialize_builtin_tool("read"),
            ToolCall(tool_name="read", arguments={"path": "sample.py"}, tool_call_id="read-after"),
            base_context,
        )
        assert read_after.body is not None
        assert read_after.body.as_payload()["raw_content"] == "name 🚀Y\r\n"
        after_report = ReportedCall(
            tool_call_id="read-after",
            final_tool_name="read",
            authorized_arguments={"path": "sample.py"},
            result=read_after,
        )
        after_tracking = read_tracking_for_tool_results(tool_results=(after_report,), workspace=tmp_path)
        stale_context = replace(
            context,
            read_paths=after_tracking.read_paths,
            read_lines=after_tracking.read_lines,
            read_whole_files=after_tracking.read_whole_files,
        )
        with pytest.raises(ValueError, match="fail-closed"):
            _invoke(
                executor,
                edit_tool,
                ToolCall(
                    tool_name="apply_workspace_edit",
                    arguments={
                        "edits": [
                            {
                                "path": "sample.py",
                                "startLine": 1,
                                "startCharacter": 10,
                                "endLine": 1,
                                "endCharacter": 11,
                                "newText": "z",
                                "expectedHash": hashlib.sha256(final_bytes).hexdigest(),
                                "expectedVersion": 1,
                            }
                        ]
                    },
                    tool_call_id="stale-apply",
                ),
                stale_context,
            )
        assert target.read_bytes() == final_bytes
    finally:
        manager.shutdown()
