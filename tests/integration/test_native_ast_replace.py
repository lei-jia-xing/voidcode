from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from voidcode.core.tool_context import ArtifactRead, ToolContext
from voidcode.core.transcript import output_text
from voidcode.core.turns import ReportedCall
from voidcode.formatter.config import RuntimeFormatterPresetConfig
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.runtime.tool_execution import RuntimeToolExecutor
from voidcode.runtime.tool_provider import materialize_builtin_tool
from voidcode.tools.contracts import Tool, ToolCall, ToolInvocation, ToolResult
from voidcode.tools.guards import read_tracking_for_tool_results
from voidcode.tools.output import read_tool_output_artifact


def _invoke(executor: RuntimeToolExecutor, tool: Tool, call: ToolCall, context: ToolContext) -> ToolResult:
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


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep CLI is required")
def test_native_ast_replace_returns_formatted_hash_diff_and_readback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "sample.py"
    original = b"print('hello')\n"
    target.write_bytes(original)
    formatter = tmp_path / "formatter.py"
    formatter.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path(sys.argv[1])\n"
        "path.write_bytes(path.read_bytes().replace(b'logger.info', b'LOGGER.info'))\n",
        encoding="utf-8",
    )
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
    executor = RuntimeToolExecutor(workspace=tmp_path)
    context = ToolContext(workspace=tmp_path, session_id="native-ast")
    read_call = ToolCall(tool_name="read", arguments={"path": "sample.py"}, tool_call_id="read-before")
    read_result = _invoke(executor, materialize_builtin_tool("read"), read_call, context)
    assert read_result.body is not None
    expected_hash = str(read_result.body.as_payload()["content_hash"])
    read_report = ReportedCall(
        tool_call_id="read-before",
        final_tool_name="read",
        authorized_arguments={"path": "sample.py"},
        result=read_result,
    )
    tracking = read_tracking_for_tool_results(tool_results=(read_report,), workspace=tmp_path)
    context = ToolContext(
        workspace=tmp_path,
        session_id="native-ast",
        read_paths=tracking.read_paths,
        read_lines=tracking.read_lines,
        read_whole_files=tracking.read_whole_files,
    )
    result = _invoke(
        executor,
        materialize_builtin_tool("ast_grep", hooks_config=hooks),
        ToolCall(
            tool_name="ast_grep",
            arguments={
                "mode": "replace",
                "pattern": "print($X)",
                "rewrite": "logger.info($X)",
                "path": "sample.py",
                "lang": "python",
                "apply": True,
                "expectedHash": expected_hash,
            },
            tool_call_id="ast-replace",
        ),
        context,
    )

    assert result.status == "ok"
    assert result.body is not None
    body = result.body.as_payload()
    final_bytes = b"LOGGER.info('hello')\n"
    assert target.read_bytes() == final_bytes
    assert body["applied"] is True
    assert body["final_hash"] == hashlib.sha256(final_bytes).hexdigest()
    assert body["diff"] == ("--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-print('hello')\n+LOGGER.info('hello')\n")
    formatter_result = body["formatter"]
    assert isinstance(formatter_result, dict)
    assert formatter_result["status"] == "formatted"

    readback = _invoke(
        executor,
        materialize_builtin_tool("read"),
        ToolCall(tool_name="read", arguments={"path": "sample.py"}, tool_call_id="read-after"),
        context,
    )
    assert readback.body is not None
    assert readback.body.as_payload()["raw_content"] == "LOGGER.info('hello')\n"


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep CLI is required")
def test_native_ast_replace_preflights_and_formats_directory_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source_files = {
        "src/A.py": source_dir / "A.py",
        "src/B.py": source_dir / "B.py",
    }
    for path in source_files.values():
        path.write_text("print('same')\n", encoding="utf-8")
    formatter = tmp_path / "formatter.py"
    formatter.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path(sys.argv[1])\n"
        "path.write_bytes(path.read_bytes().replace(b'logger.info', b'LOGGER.info'))\n",
        encoding="utf-8",
    )
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
    artifact_refs: dict[str, dict[str, object]] = {}

    def read_artifact(
        *,
        session_id: str,
        artifact_id: str,
        offset: int,
        limit: int,
        byte_offset: int | None = None,
        byte_limit: int | None = None,
    ) -> ArtifactRead:
        artifact = artifact_refs.get(artifact_id)
        if artifact is None or artifact.get("session_id") != session_id:
            raise ValueError("artifact is not owned by the invoking session")
        return read_tool_output_artifact(
            artifact,
            offset=offset,
            limit=limit,
            byte_offset=byte_offset,
            byte_limit=byte_limit,
        )

    executor = RuntimeToolExecutor(workspace=tmp_path, read_artifact=read_artifact)
    base_context = ToolContext(workspace=tmp_path, session_id="native-ast-directory")

    def read_report(relative_path: str, call_id: str) -> tuple[ReportedCall, str]:
        result = _invoke(
            executor,
            materialize_builtin_tool("read"),
            ToolCall(tool_name="read", arguments={"path": relative_path}, tool_call_id=call_id),
            base_context,
        )
        assert result.body is not None
        content_hash = str(result.body.as_payload()["content_hash"])
        return (
            ReportedCall(
                tool_call_id=call_id,
                final_tool_name="read",
                authorized_arguments={"path": relative_path},
                result=result,
            ),
            content_hash,
        )

    read_a, hash_a = read_report("src/A.py", "read-a")

    def read_context(reports: tuple[ReportedCall, ...]) -> ToolContext:
        tracking = read_tracking_for_tool_results(tool_results=reports, workspace=tmp_path)
        return ToolContext(
            workspace=tmp_path,
            session_id="native-ast-directory",
            read_paths=tracking.read_paths,
            read_lines=tracking.read_lines,
            read_whole_files=tracking.read_whole_files,
        )

    context_a = read_context((read_a,))
    replace_tool = materialize_builtin_tool("ast_grep", hooks_config=hooks)
    replace_call = ToolCall(
        tool_name="ast_grep",
        arguments={
            "mode": "replace",
            "pattern": "print($X)",
            "rewrite": "logger.info($X)",
            "path": "src",
            "lang": "python",
            "apply": True,
        },
        tool_call_id="ast-directory-replace",
    )
    before = {relative_path: path.read_bytes() for relative_path, path in source_files.items()}
    with pytest.raises(ValueError, match="src/B.py"):
        _invoke(executor, replace_tool, replace_call, context_a)
    assert {relative_path: path.read_bytes() for relative_path, path in source_files.items()} == before

    read_b, hash_b = read_report("src/B.py", "read-b")
    context_all = read_context((read_a, read_b))
    result = _invoke(executor, replace_tool, replace_call, context_all)
    assert result.status == "ok"
    assert result.body is not None
    body = result.body.as_payload()
    assert body["replacement_count"] == 2
    assert body["matches_truncated"] is False
    matches = body["matches"]
    assert isinstance(matches, list) and len(matches) == 2
    assert all("range" not in match and "replacement" not in match for match in matches if isinstance(match, dict))
    files = body["files"]
    assert isinstance(files, list) and len(files) == 2
    read_hashes = {"src/A.py": hash_a, "src/B.py": hash_b}
    file_results = {str(file["path"]): file for file in files if isinstance(file, dict)}
    for relative_path, path in source_files.items():
        final_bytes = b"LOGGER.info('same')\n"
        file_result = file_results[relative_path]
        assert path.read_bytes() == final_bytes
        assert file_result["source_hash"] == read_hashes[relative_path]
        assert file_result["byte_count"] == len(final_bytes)
        assert file_result["content_hash"] == hashlib.sha256(final_bytes).hexdigest()
        assert f"--- a/{relative_path}\n" in str(file_result["diff"])
        assert "-print('same')\n" in str(file_result["diff"])
        assert "+LOGGER.info('same')\n" in str(file_result["diff"])
        formatter_result = file_result["formatter"]
        assert isinstance(formatter_result, dict)
        assert formatter_result["status"] == "formatted"

    artifact = body["body_artifact"]
    assert isinstance(artifact, dict)
    artifact_id = artifact["artifact_id"]
    response_uri = body["response_reference"]
    assert isinstance(artifact_id, str) and isinstance(response_uri, str)
    assert artifact["mime"] == "text/x-ndjson; charset=utf-8"
    assert response_uri in (output_text(result.output) or "")
    artifact_refs[artifact_id] = artifact
    artifact_read = _invoke(
        executor,
        materialize_builtin_tool("read"),
        ToolCall(
            tool_name="read",
            arguments={"path": response_uri, "offset": 1, "limit": 100},
            tool_call_id="read-native-ast-jsonl",
        ),
        context_all,
    )
    assert artifact_read.body is not None
    artifact_page = artifact_read.body.as_payload()
    native_matches = [json.loads(line) for line in str(artifact_page["raw_content"]).splitlines()]
    assert len(native_matches) == 2
    assert all("range" in match and "replacement" in match for match in native_matches)
    assert artifact_page["content_hash"] == artifact["sha256"]
    assert artifact_page["artifact_byte_count"] == artifact["byte_count"]
