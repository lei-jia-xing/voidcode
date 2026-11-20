"""Restart recovery for scripted workflows: no node reruns across a core reopen.

A two-node sequential workflow runs on CoreA: the first node completes (after its
approval is allowed), the second parks on a real approval. CoreA closes, CoreB opens
the same database, the test allows the second approval through the public resume,
and `workflow.resume` finishes the run. Both child ids are unchanged, the first
node makes no new model request, and its marker file was written exactly once.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from tests.fixtures.core_factory import discard_core, register_core
from tests.fixtures.polling import wait_for
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core

WORKFLOW_SOURCE = '''"""Sequential two-node restart fixture."""

MARKER_NAME = "restart-marker.txt"


async def run(ctx, task):
    first = await ctx.agent("execute", f"FIRST-NODE appends one line to {MARKER_NAME} with {task}", key="first")
    second = await ctx.agent("execute", f"SECOND-NODE writes second.txt with {task}", key="second")
    return f"summary: {first} + {second}"
'''


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _configure(tmp_path: Path, server: OpenAILoopbackServer) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "restart_flow.py").write_text(WORKFLOW_SOURCE, encoding="utf-8")
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "ask", "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {
            "files": {"enabled": True},
            "exec": {"enabled": True},
            "subagents": {"enabled": True},
            "workflows": {"enabled": True, "config": {"workflows": {"restart_flow": "restart_flow.py"}}},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    return workspace


def _open(workspace: Path, state_dir: Path) -> Core:
    return register_core(open_core(workspace, state_dir=state_dir))


def _nodes(core: Core, session: str, run_id: str) -> dict[str, dict[str, object]]:
    result = core.invoke_command(session, "workflow.status", {"run_id": run_id}, expected_revision=core.revision_id)
    assert result.status == "ok", result
    assert isinstance(result.data, Mapping)
    nodes = result.data["nodes"]
    assert isinstance(nodes, (list, tuple))
    return {str(node["key"]): dict(node) for node in nodes}


def _run_status(core: Core, session: str, run_id: str) -> str:
    result = core.invoke_command(session, "workflow.status", {"run_id": run_id}, expected_revision=core.revision_id)
    assert result.status == "ok", result
    assert isinstance(result.data, Mapping)
    return str(result.data["status"])


def _approval_request_id(core: Core, session_id: str) -> str:
    for event in core.store.events(session_id):
        if event.event_type == "core.approval_requested":
            request_id = event.payload.get("request_id")
            assert isinstance(request_id, str) and request_id
            return request_id
    raise AssertionError(f"no approval request found on {session_id}")


def _provider_prompt_count(server: OpenAILoopbackServer, needle: str) -> int:
    count = 0
    for payload in server.requests:
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            continue
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) and needle in content:
                count += 1
                break
    return count


def test_workflow_resume_after_restart_reuses_both_children(tmp_path: Path) -> None:
    replies = [
        # First child turn 1: asks to append its marker line -> parks on approval.
        _turn(_call("call-first-append", "shell_exec", command="echo first-marker >> restart-marker.txt", description="append marker line")),
        # First child turn 2 (after allow): done.
        _turn(output="first done"),
        # Second child turn 1: asks to write its file -> parks on approval.
        _turn(_call("call-second-write", "write", path="second.txt", content="second-marker\n")),
        # Second child turn 2 (after allow, post-restart): done.
        _turn(output="second done"),
    ]
    with _server(replies) as server:
        workspace = _configure(tmp_path, server)
        state_dir = tmp_path / "state"
        core_a = _open(workspace, state_dir)
        core_a.create_session("parent")
        started = core_a.invoke_command(
            "parent", "workflow.run", {"workflow": "restart_flow", "task": "restart-task"}, expected_revision=core_a.revision_id
        )
        assert started.status == "ok", started
        assert isinstance(started.data, Mapping)
        run_id = str(started.data["run_id"])

        # First node parks on its real approval; allow it publicly, resume the parked
        # run, and wait for the node to complete through key reuse (no respawn).
        wait_for(lambda: _nodes(core_a, "parent", run_id).get("first", {}).get("status") == "waiting", what="first node approval park", timeout=30.0)
        first_child = str(_nodes(core_a, "parent", run_id)["first"]["child_session_id"])
        first_request = _approval_request_id(core_a, first_child)
        assert core_a.resume(first_child, approval_request_id=first_request, decision="allow").status == "completed"
        resumed_first = core_a.invoke_command("parent", "workflow.resume", {"run_id": run_id}, expected_revision=core_a.revision_id)
        assert resumed_first.status == "ok", resumed_first
        wait_for(lambda: _nodes(core_a, "parent", run_id).get("first", {}).get("status") == "completed", what="first node completion", timeout=90.0)
        assert (workspace / "restart-marker.txt").read_text(encoding="utf-8") == "first-marker\n"

        # Second node parks on its real approval; leave it parked and close CoreA.
        wait_for(
            lambda: _nodes(core_a, "parent", run_id).get("second", {}).get("status") == "waiting", what="second node approval park", timeout=30.0
        )
        second_child = str(_nodes(core_a, "parent", run_id)["second"]["child_session_id"])
        requests_before_close = len(server.requests)
        assert requests_before_close == 3, "first turn, first resume, second turn"
        core_a.close()
        discard_core(core_a)

        # CoreB reopens the same database; the parked approval survives the restart.
        core_b = _open(workspace, state_dir)
        second_request = _approval_request_id(core_b, second_child)
        assert core_b.resume(second_child, approval_request_id=second_request, decision="allow").status == "completed"
        resumed = core_b.invoke_command("parent", "workflow.resume", {"run_id": run_id}, expected_revision=core_b.revision_id)
        assert resumed.status == "ok", resumed
        wait_for(lambda: _run_status(core_b, "parent", run_id) == "completed", what="workflow completion after resume", timeout=30.0)
        final = core_b.invoke_command("parent", "workflow.status", {"run_id": run_id}, expected_revision=core_b.revision_id)
        assert isinstance(final.data, Mapping)
        assert final.data["status"] == "completed"
        run_result = final.data.get("run_result", {})
        assert isinstance(run_result, Mapping)
        assert str(run_result.get("text", "")) == "summary: first done + second done"

        # Both children are the original sessions; neither node reran its model work.
        nodes = _nodes(core_b, "parent", run_id)
        assert nodes["first"]["child_session_id"] == first_child
        assert nodes["second"]["child_session_id"] == second_child
        assert len(server.requests) == 4, "exactly the second resume turn after the restart"
        assert _provider_prompt_count(server, "FIRST-NODE") == 2, "first turn plus its approval resume, never again"
        assert _provider_prompt_count(server, "SECOND-NODE") == 2, "second turn plus its approval resume, never again"
        # The append side effect ran exactly once: a rerun would have appended a second line.
        assert (workspace / "restart-marker.txt").read_text(encoding="utf-8") == "first-marker\n"
        assert (workspace / "second.txt").read_text(encoding="utf-8") == "second-marker\n"
