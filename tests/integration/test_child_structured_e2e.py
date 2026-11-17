"""Structured delivery over a real core fact: task and workflow read it identically."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tests.fixtures.fake_services import FakeServices
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.plugins.subagents.subagents_plugin import ChildRecord, DelegationState, TaskTool
from voidcode.plugins.workflows.workflows_plugin import WorkflowNode, WorkflowRun, WorkflowState, _FinishNode
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import ToolCall

_SCHEMA = {
    "type": "object",
    "required": ["files", "ok"],
    "properties": {"files": {"type": "array", "items": {"type": "string"}}, "ok": {"type": "boolean"}},
    "additionalProperties": False,
}

_DELIVERY = '{"files": ["a"], "ok": true}'


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


def _open(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    core = open_core(workspace, state_dir=tmp_path / "state")
    core.create_session("root")
    return core


def test_task_and_workflow_read_one_real_fact_identically(tmp_path: Path) -> None:
    reply = openai_response(message={"role": "assistant", "content": _DELIVERY}, finish="stop")
    with _server([reply]) as server:
        core = _open(tmp_path, server)
        try:
            child_id = core.spawn_child("root", "shaped work", owner="probe", result_schema=dict(_SCHEMA))
            outcome = core.join_child("root", child_id, timeout_seconds=30)
            assert outcome.status == "completed"
            assert outcome.output == _DELIVERY
        finally:
            core.close()

    expected = {"files": ["a"], "ok": True}

    task_services = FakeServices(session_id="session-1")
    task_services.seed_state(
        DelegationState(children={child_id: ChildRecord(label="", result_schema=dict(_SCHEMA))}, groups={}).to_payload(),
        version=1,
    )
    task_services.roster[child_id] = outcome
    task_context = ToolContext(
        workspace=tmp_path,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
    )
    task_result = TaskTool().invoke(
        ToolCall(tool_name="task", arguments={"operation": "output", "task_id": child_id}, tool_call_id="call-1"),
        context=task_context,
        services=task_services,
    )
    assert task_result.body is not None
    task_payload = task_result.body.as_payload()
    assert task_payload["data"] == expected
    assert "parse_error" not in task_payload

    flow_services = FakeServices(session_id="s1")
    node = WorkflowNode(
        key="a",
        status="running",
        agent="review",
        child_session_id=child_id,
        effective_input_sha256="d" * 64,
        prompt_sha256="e" * 64,
        result_schema=dict(_SCHEMA),
    )
    run = WorkflowRun(
        run_id="run-1",
        session_id="s1",
        workflow="review",
        source_path="flows/review.py",
        source_sha256="a" * 64,
        source_artifact="b" * 64,
        task="t",
        input_sha256="c" * 64,
        nodes=(node,),
    )
    flow_services.seed_state(WorkflowState.from_payload(None).with_run(run).to_payload(), version=1)
    flow_context = ToolContext(workspace=Path("/workspace"), session_id="s1", run_id="r1", invocation_id="i1", revision_id="rev", actor="model")
    finished = _FinishNode("run-1", "a", outcome)(flow_context, flow_services)
    assert finished.error is None and not finished.cancelled
    assert finished.structured is not None
    assert finished.structured.data is not None and finished.structured.as_payload()["data"] == expected
    assert finished.structured.raw_text == _DELIVERY

    flow_state = WorkflowState.from_payload(flow_services.get_state("session")[0])
    stored = flow_state.runs["run-1"].node("a")
    assert stored is not None and stored.data is not None
    assert dict(stored.data) == dict(task_payload["data"])  # type: ignore[arg-type]
