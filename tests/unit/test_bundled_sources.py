"""Product test: representative tool-providing plugins boot and dispatch together."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.coverage import read_artifact_content
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import ToolFailure, ToolSuccess

_REPRESENTATIVE_CALLS: tuple[tuple[str, dict[str, object], type[object]], ...] = (
    ("read", {"path": "subject.txt"}, ToolSuccess),
    ("find", {"query": "initial content", "mode": "literal"}, ToolSuccess),
    ("shell_exec", {"command": "echo bundle-ok"}, ToolSuccess),
    ("cron_list", {}, ToolSuccess),
    ("todo", {"op": "view"}, ToolSuccess),
    ("task", {"tasks": [{"task": "delegate", "solutionSpace": "test"}]}, ToolFailure),
    ("mcp", {"server": "absent", "tool": "test", "arguments": {}}, ToolFailure),
    ("web_search", {"query": "test search"}, ToolFailure),
)


def _call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _workspace(tmp_path: Path, server: OpenAILoopbackServer | None) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "subject.txt").write_text("initial content\n", encoding="utf-8")
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "yolo"},
        "providers": {
            "endpoint": {
                "base_url": f"http://127.0.0.1:{server.server_port}/v1" if server is not None else "http://127.0.0.1:9/v1",
                "api_key": "test-key",
            }
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    return workspace


def test_bundle_dispatches_one_tool_per_tool_plugin(tmp_path: Path) -> None:
    calls = [_call(f"bundle-{name}", name, arguments) for name, arguments, _expected in _REPRESENTATIVE_CALLS]
    server = OpenAILoopbackServer([_turn(*calls, output=None), _turn(output="bundle dispatch complete")])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    workspace = _workspace(tmp_path, server)
    try:
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("Exercise representative bundled tool providers.")
            assert result.status == "completed", result.error
            assert result.output == "bundle dispatch complete"
            completed = [event for event in core.store.events(result.session_id) if event.event_type == "core.tool_completed"]
            assert len(completed) == len(_REPRESENTATIVE_CALLS)
            expected_by_tool = {name: expected for name, _arguments, expected in _REPRESENTATIVE_CALLS}
            for event in completed:
                decoded = decode_tool_result(event.payload["result"])
                assert decoded.tool_name in expected_by_tool, decoded.tool_name
                assert isinstance(decoded, expected_by_tool[decoded.tool_name]), f"{decoded.tool_name}: {decoded}"

            # Verify local tools produced real output
            tool_outputs: dict[str, str] = {}
            shell_decoded = None
            for e in completed:
                decoded_r = decode_tool_result(e.payload["result"])
                text = getattr(getattr(decoded_r, "output", None), "text", None)
                if text:
                    tool_outputs[decoded_r.tool_name] = text
                if decoded_r.tool_name == "shell_exec":
                    shell_decoded = decoded_r
            assert "initial content" in tool_outputs.get("read", "")
            assert shell_decoded is not None and shell_decoded.body is not None
            shell_body = shell_decoded.body.structured_content
            assert shell_body["exit_code"] == 0
            stdout = shell_body["stdout"]
            reference = ArtifactReference(sha256=str(stdout["sha256"]), bytes=int(stdout["bytes"]), media_type=str(stdout["media_type"]))
            captured = read_artifact_content(reference, core.artifact_page).decode("utf-8")
            assert "bundle-ok" in captured

            # Tool results reached model in message history
            reports = [message for message in server.requests[1]["messages"] if message.get("role") == "tool"]
            assert [report["tool_call_id"] for report in reports] == [f"bundle-{name}" for name, _a, _e in _REPRESENTATIVE_CALLS]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
