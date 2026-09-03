from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.permission_context import RuntimePermissionContextResolver
from voidcode.tools import LocalCustomTool, ToolCall
from voidcode.tools.contracts import RuntimeToolTimeoutError
from voidcode.tools.local_custom import discover_local_custom_tools


def _write_manifest(
    workspace: Path,
    *,
    command: list[str] | None = None,
    read_only: bool = True,
    path_argument_keys: list[str] | None = None,
    name: str = "local/test",
) -> Path:
    tools_dir = workspace / ".voidcode" / "tools"
    tools_dir.mkdir(parents=True)
    script = tools_dir / "tool.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            args = json.loads(sys.stdin.read() or '{}')
            print(json.dumps({'args': args, 'ok': True}, sort_keys=True))
            """
        ),
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "name": name,
        "description": "test local custom tool",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "command": command or [sys.executable, "${manifest_dir}/tool.py"],
        "read_only": read_only,
    }
    if path_argument_keys is not None:
        payload["path_argument_keys"] = path_argument_keys
    manifest = tools_dir / "tool.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def _discover_one(workspace: Path, **kwargs: Any) -> LocalCustomTool:
    tools = discover_local_custom_tools(workspace, enabled=True, **kwargs)
    assert len(tools) == 1
    return tools[0]


def test_discovery_preserves_read_only_and_path_permission_metadata(tmp_path: Path) -> None:
    _write_manifest(tmp_path, read_only=False, path_argument_keys=["path"])
    tool = _discover_one(tmp_path)

    assert tool.definition.read_only is False
    assert tool.definition.path_argument_keys == ("path",)
    resolver = RuntimePermissionContextResolver(workspace=tmp_path)
    scope, external_path, operation, candidates = resolver.permission_context_for_tool_call(
        tool=tool.definition,
        tool_instance=tool,
        tool_call=ToolCall(tool_name="local/test", arguments={"path": "/tmp/outside.txt"}),
        patch_path_extractor=lambda _patch: (),
    )
    assert scope == "external"
    assert external_path == "/tmp/outside.txt"
    assert operation == "execute"
    assert candidates == ("/tmp/outside.txt",)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not-json", "invalid local custom tool manifest"),
        ("[]", "must be an object"),
        (json.dumps({"name": "local/test"}), "requires a non-empty description"),
    ],
)
def test_malformed_manifest_fails_closed(tmp_path: Path, payload: str, message: str) -> None:
    tools_dir = tmp_path / ".voidcode" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "bad.json").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        discover_local_custom_tools(tmp_path, enabled=True)


def test_manifest_dir_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    _write_manifest(tmp_path, command=[sys.executable, "${manifest_dir}/../../../outside.py"])

    with pytest.raises(ValueError, match="must stay inside workspace"):
        discover_local_custom_tools(tmp_path, enabled=True)
    assert not outside.exists()


def test_direct_invoke_passes_json_stdin_and_returns_success(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    result = _discover_one(tmp_path).invoke(
        ToolCall(tool_name="local/test", arguments={"message": "hello"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.source == "local_custom_tool"
    assert json.loads(result.content or "") == {"args": {"message": "hello"}, "ok": True}
    assert result.data["exit_code"] == 0


def test_nonzero_exit_is_a_diagnostic_error(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        command=[sys.executable, "-c", "import sys; print('failure', file=sys.stderr); sys.exit(3)"],
    )
    result = _discover_one(tmp_path).invoke(ToolCall(tool_name="local/test"), workspace=tmp_path)
    assert result.source == "local_custom_tool"
    assert result.status == "error"
    assert result.error == "failure"
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "local_custom_tool_failed"
    assert result.data["exit_code"] == 3


def test_runtime_timeout_kills_local_command(tmp_path: Path) -> None:
    _write_manifest(tmp_path, command=[sys.executable, "-c", "import time; time.sleep(2)"])

    with pytest.raises(RuntimeToolTimeoutError, match="timed out after 1 seconds"):
        _discover_one(tmp_path).invoke_with_runtime_timeout(ToolCall(tool_name="local/test"), workspace=tmp_path, timeout_seconds=1)


def test_stdout_and_stderr_are_bounded(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        command=[
            sys.executable,
            "-c",
            "import sys; print('o' * 100000); print('e' * 100000, file=sys.stderr)",
        ],
    )
    result = _discover_one(tmp_path).invoke(ToolCall(tool_name="local/test"), workspace=tmp_path)

    assert result.status == "ok"
    assert result.truncated is True
    assert len(result.content or "") <= 50 * 1024
    assert len(str(result.data["stderr"])) <= 50 * 1024
    assert result.data["stdout_truncated"] is True
    assert result.data["stderr_truncated"] is True
