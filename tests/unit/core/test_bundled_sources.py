"""Cutover gate: the whole bundled plugin set boots and dispatches together.

The bundled list is the product surface — every mechanism ships as an ordinary plugin — so
this is the bundle's behavioral proof: one real core boots the default set and routes a real
tool call to each tool-providing plugin (a success where the subject is local, a *governed*
failure where the tool needs a server or a child that does not exist).
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.activation import BUNDLED_PLUGIN_IDS, bundled_sources
from voidcode.core.bootstrap import open_core
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


def test_bundle_dispatches_one_tool_per_tool_plugin(tmp_path: Path) -> None:
    calls = [_call(f"bundle-{name}", name, arguments) for name, arguments, _expected in _REPRESENTATIVE_CALLS]
    server = OpenAILoopbackServer([_turn(calls, None), _turn([], "bundle dispatch complete")])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    workspace = _workspace(tmp_path, server)
    try:
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("Exercise every bundled tool provider.")
            assert result.status == "completed", result.error
            assert result.output == "bundle dispatch complete"
            completed = [event for event in core.store.events(result.session_id) if event.event_type == "core.tool_completed"]
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


def test_every_bundled_id_resolves_to_a_plugin_package() -> None:
    """Each bundled id names a shippable package: a directory with a `create_plugin` factory.

    Guards list edits (a dropped or duplicated id breaks admission), without pinning the
    product surface: adding a plugin never touches this test.
    """
    assert len(set(BUNDLED_PLUGIN_IDS)) == len(BUNDLED_PLUGIN_IDS), "bundled ids must be unique"
    sources = bundled_sources()
    assert set(sources) == set(BUNDLED_PLUGIN_IDS)
    for plugin_id, root in sorted(sources.items()):
        init = root / "__init__.py"
        assert root.is_dir(), f"bundled plugin {plugin_id} is missing from this installation: {root}"
        assert init.is_file(), f"bundled plugin {plugin_id} is not a package: {root}"
        tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
        assert any(
            (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "create_plugin")
            or (isinstance(node, ast.ImportFrom) and any(alias.asname == "create_plugin" or alias.name == "create_plugin" for alias in node.names))
            for node in ast.walk(tree)
        ), f"bundled plugin {plugin_id} exports no create_plugin factory: {init}"
