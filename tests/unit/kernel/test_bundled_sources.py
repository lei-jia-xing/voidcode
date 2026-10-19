"""P4 cutover gate: the whole bundled plugin set admits, registers, and dispatches together.

The bundled list is the product surface — every mechanism ships as an ordinary plugin — so
this is the bundle's integrity proof: one real host generation admits every bundled id,
registers exactly the tools/commands/providers its manifests declare, and routes a real tool
call to each tool-providing plugin (a success where the subject is local, a *governed* failure
where the tool needs a server or a child that does not exist).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import _bundled_sources, open_kernel
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ToolFailure, ToolSuccess

#: One representative call per tool-providing bundled plugin. Locally executable where the tool
#: has a local subject; a call that must come back as a governed ``ToolFailure`` (never an
#: "unknown tool" refusal) where the tool needs a server, a skill, or a child.
_REPRESENTATIVE_CALLS: tuple[tuple[str, dict[str, object], type[object]], ...] = (
    ("read", {"path": "bundle.txt"}, ToolSuccess),
    ("shell_exec", {"command": "printf bundle-exec"}, ToolSuccess),
    ("ast_grep", {"mode": "search", "pattern": "bundle", "path": "bundle.txt"}, ToolSuccess),
    ("todo", {"op": "view"}, ToolSuccess),
    ("cron_list", {}, ToolSuccess),
    ("get_goal", {}, ToolSuccess),
    ("skill", {"name": "missing-skill"}, ToolFailure),
    ("task", {"operation": "output", "task_id": "missing-task"}, ToolFailure),
    ("mcp", {"server": "missing-server", "tool": "missing-tool"}, ToolFailure),
    ("lsp", {"operation": "diagnostics", "path": "bundle.txt"}, ToolFailure),
)


def _manifests() -> dict[str, dict[str, object]]:
    return {plugin_id: json.loads((source / "voidcode-plugin.json").read_text(encoding="utf-8")) for plugin_id, source in _bundled_sources().items()}


def _call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(calls: list[dict[str, object]], output: str | None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": calls or None},
        finish="tool_calls" if calls else "stop",
    )


def _workspace(tmp_path: Path, server: OpenAILoopbackServer | None) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "bundle.txt").write_text("bundle-read\n", encoding="utf-8")
    # A configured provider always needs an explicit base_url; without a loopback server it
    # points nowhere, because the boot proof makes no provider request.
    base_url = f"http://127.0.0.1:{server.server_port}/v1" if server is not None else "http://127.0.0.1:1/v1"
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {"endpoint": {"api_key": "bundle-secret", "base_url": base_url}},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def test_bundle_admits_and_registers_every_bundled_declaration(tmp_path: Path) -> None:
    manifests = _manifests()
    workspace = _workspace(tmp_path, None)
    with open_kernel(workspace, state_dir=tmp_path / "state") as kernel:
        assert {plugin.manifest.plugin_id for plugin in kernel.host.loaded_plugins} == set(manifests)
        tools = {tool.name for tool in kernel.host.tools}
        commands = {command.name for command in kernel.command_catalog().commands}
        declared_tools = {tool["name"] for manifest in manifests.values() for tool in manifest["tools"]}  # type: ignore[union-attr]
        declared_commands = {command["name"] for manifest in manifests.values() for command in manifest["commands"]}  # type: ignore[union-attr]
        declared_providers = {provider["name"] for manifest in manifests.values() for provider in manifest.get("providers", [])}  # type: ignore[union-attr,arg-type]
        assert declared_tools <= tools, sorted(declared_tools - tools)
        assert declared_commands <= commands, sorted(declared_commands - commands)
        assert set(kernel.host.providers) >= declared_providers, sorted(declared_providers - set(kernel.host.providers))


def test_bundle_dispatches_one_tool_per_tool_plugin(tmp_path: Path) -> None:
    calls = [_call(f"bundle-{name}", name, arguments) for name, arguments, _expected in _REPRESENTATIVE_CALLS]
    server = OpenAILoopbackServer([_turn(calls, None), _turn([], "bundle dispatch complete")])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    workspace = _workspace(tmp_path, server)
    try:
        with open_kernel(workspace, state_dir=tmp_path / "state") as kernel:
            result = kernel.run("Exercise every bundled tool provider.")
            assert result.status == "completed", result.error
            assert result.output == "bundle dispatch complete"
            completed = [event for event in kernel.store.events(result.session_id) if event.event_type == "kernel.tool_completed"]
            assert len(completed) == len(_REPRESENTATIVE_CALLS)
            expected_by_tool = {name: expected for name, _arguments, expected in _REPRESENTATIVE_CALLS}
            for event in completed:
                decoded = decode_tool_result(event.payload["result"])
                assert decoded.tool_name in expected_by_tool, decoded.tool_name
                assert isinstance(decoded, expected_by_tool[decoded.tool_name]), f"{decoded.tool_name}: {decoded}"
            # Every result reached the model as a tool message, so the dispatch was real work,
            # not a catalog entry that never ran.
            reports = [message for message in server.requests[1]["messages"] if message.get("role") == "tool"]
            assert [report["tool_call_id"] for report in reports] == [f"bundle-{name}" for name, _a, _e in _REPRESENTATIVE_CALLS]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
