from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest

from voidcode.core.tool_context import ArtifactMissing, ArtifactPage, ToolContext
from voidcode.core.transcript import ToolResultView, tool_result_output
from voidcode.core.turns import FinalTurn, ToolTurn, TurnSession
from voidcode.runtime import VoidCodeRuntime
from voidcode.runtime.bundle import write_session_bundle
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.execution.delegation.task_batch import TaskBatchCommand
from voidcode.runtime.execution.delegation.task_control import TaskControlTool
from voidcode.runtime.execution.delegation.task_output import TaskOutputTool
from voidcode.runtime.execution.report_codec import report_payload
from voidcode.runtime.fact_codec import reported_call_from_event
from voidcode.runtime.permission import PermissionPolicy
from voidcode.tools.contracts import ToolCall

PARENT = "task-artifact-parent"
DATA = {"answer": "large payload", "payload": "x" * 18000}
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "payload": {"type": "string"}},
    "required": ["answer", "payload"],
    "additionalProperties": False,
}
BATCH_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(name, arguments)


def _text(results: tuple[object, ...]) -> str:
    text = tool_result_output(cast(ToolResultView, results[-1]))
    if text is None:
        raise AssertionError("tool result has no text output")
    return text


def _prompt(request: object) -> str:
    prompt = getattr(request, "prompt", None)
    if isinstance(prompt, str):
        return prompt
    assembled = getattr(request, "assembled_context", None)
    prompt = getattr(assembled, "prompt", "")
    return prompt if isinstance(prompt, str) else ""


def _task_output_body(response: RuntimeResponse) -> dict[str, object]:
    reports = (
        report
        for event in response.events
        if (report := reported_call_from_event(event)) is not None
        and report.final_tool_name == "task"
        and report.authorized_arguments.get("operation") == "output"
    )
    report = next(reports)
    payload = cast(dict[str, object], report_payload(report)["result"])
    return cast(dict[str, object], payload["body"])


class _SingleTaskGraph:
    def __init__(self) -> None:
        self.task_id: str | None = None
        self.parent_uri: str | None = None

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> ToolTurn | FinalTurn:
        if session.metadata.get("parent_session_id") is not None:
            yield_count = sum(cast(ToolResultView, result).tool_name == "yield" for result in tool_results)
            if yield_count == 0:
                return ToolTurn(calls=(_call("yield", {"type": "analysis", "result": "progress checkpoint", "data": {"stage": "prepared"}}),))
            if yield_count == 1:
                return ToolTurn(calls=(_call("yield", {"summary": "large child complete", "data": DATA}),))
            return FinalTurn(output="large child complete")

        if not tool_results:
            return ToolTurn(
                calls=(
                    _call(
                        "task",
                        {
                            "prompt": "large structured child",
                            "run_in_background": True,
                            "load_skills": [],
                            "subagent_type": "worker",
                            "description": "Large structured child",
                            "outputSchema": SCHEMA,
                            "schemaMode": "strict",
                        },
                    ),
                )
            )

        last = cast(ToolResultView, tool_results[-1])
        text = _text(tool_results)
        if last.tool_name == "task" and "operation" not in last.arguments:
            match = re.search(r"Started background task ([^ .]+)", text)
            if match is None:
                raise AssertionError(f"task start output did not include a task id: {text!r}")
            self.task_id = match.group(1)
            return ToolTurn(calls=(_call("task", {"operation": "output", "task_id": self.task_id, "block": True, "timeout": 15000}),))
        if last.tool_name == "task":
            match = re.search(r'read\(path="(voidcode://artifact/[^"]+)"\)', text)
            if match is None:
                raise AssertionError(f"task output did not return a readable parent artifact: {text!r}")
            self.parent_uri = match.group(1)
            return ToolTurn(calls=(_call("read", {"path": self.parent_uri}),))
        if last.tool_name == "read":
            return FinalTurn(output=text)
        return FinalTurn(output=text)


class _ForeignGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> FinalTurn:
        _ = request, tool_results, session
        return FinalTurn(output="foreign ready")


class _BatchGraph:
    def __init__(self) -> None:
        self.group_id: str | None = None
        self.parent_uri: str | None = None

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> ToolTurn | FinalTurn:
        if session.metadata.get("parent_session_id") is not None:
            invalid = "invalid" in _prompt(request)
            yield_count = sum(cast(ToolResultView, result).tool_name == "yield" for result in tool_results)
            if yield_count == 0:
                return ToolTurn(calls=(_call("yield", {"type": "analysis", "result": "child progress"}),))
            data = {"unexpected": 7} if invalid else {"answer": "valid result"}
            if yield_count == 1:
                return ToolTurn(calls=(_call("yield", {"summary": "child terminal result", "data": data}),))
            return FinalTurn(output="child terminal yield handled")

        if not tool_results:
            children = [
                ("valid batch child", "valid strict child"),
                ("invalid batch child", "invalid strict child"),
            ]
            return ToolTurn(
                calls=(
                    _call(
                        "task_batch",
                        {
                            "tasks": [
                                {
                                    "prompt": prompt,
                                    "load_skills": [],
                                    "subagent_type": "worker",
                                    "description": description,
                                    "outputSchema": BATCH_SCHEMA,
                                    "schemaMode": "strict",
                                }
                                for prompt, description in children
                            ]
                        },
                    ),
                )
            )

        last = cast(ToolResultView, tool_results[-1])
        text = _text(tool_results)
        if last.tool_name == "task_batch":
            match = re.search(r"Started task batch (batch-[^ ]+)", text)
            if match is None:
                raise AssertionError(f"batch start did not return group id: {text!r}")
            self.group_id = match.group(1)
            return ToolTurn(
                calls=(
                    _call(
                        "task",
                        {"operation": "output", "parallel_group_id": self.group_id, "block": True, "timeout": 30000},
                    ),
                )
            )
        if last.tool_name == "task":
            match = re.search(r"(voidcode://artifact/artifact_[^\s]+)", text)
            if match is None:
                raise AssertionError(f"task group output lacks its parent artifact reference: {text!r}")
            self.parent_uri = match.group(1)
            return ToolTurn(calls=(_call("read", {"path": self.parent_uri}),))
        if last.tool_name == "read":
            return FinalTurn(output=text)
        return FinalTurn(output=text)


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(execution_engine="deterministic", approval_mode="yolo", mcp=RuntimeMcpConfig(enabled=False))


def _prepare_runtime_paths(monkeypatch: pytest.MonkeyPatch, root: Path, workspace: Path) -> None:
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(workspace / "sessions.sqlite3"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "xdg-cache"))


def test_task_output_copies_full_yield_to_parent_and_cold_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_workspace = tmp_path / "source"
    target_workspace = tmp_path / "target"
    source_workspace.mkdir()
    target_workspace.mkdir()
    _prepare_runtime_paths(monkeypatch, tmp_path, source_workspace)
    graph = _SingleTaskGraph()
    bundle_path = tmp_path / "task-parent.zip"

    with VoidCodeRuntime(workspace=source_workspace, turn_producer=graph, config=_runtime_config()) as runtime:
        response = runtime.run(RuntimeRequest(prompt="copy full task output", session_id=PARENT))
        assert response.session.status == "completed"
        assert graph.task_id is not None and graph.parent_uri is not None
        result = runtime.load_background_task_result(graph.task_id)
        assert result.status == "completed"
        assert result.schema_validation is not None and result.schema_validation.valid is True
        assert any(section.get("result") == "progress checkpoint" for section in result.progress)
        body = _task_output_body(response)
        artifacts = cast(list[dict[str, object]], body["artifacts"])
        progress = cast(list[dict[str, object]], body["progress"])
        child_artifact = cast(dict[str, object], result.structured_output_artifact)
        child_uri = cast(str, child_artifact["uri"])
        child_path = cast(str, child_artifact["path"])
        assert body["structured_output_reference"] == graph.parent_uri
        assert len(artifacts) == 1
        assert artifacts[0]["session_id"] == PARENT
        assert artifacts[0]["uri"] == graph.parent_uri
        serialized_body = json.dumps(body, ensure_ascii=False)
        assert child_uri not in serialized_body
        assert child_path not in serialized_body
        assert all("artifact" not in item and "data_reference" not in item for item in progress)
        assert DATA["payload"] in (response.output or "")

        bundle = runtime.export_session_bundle(session_id=PARENT)
        write_session_bundle(bundle, path=bundle_path)

    _prepare_runtime_paths(monkeypatch, tmp_path, target_workspace)
    with VoidCodeRuntime(workspace=target_workspace, turn_producer=_ForeignGraph(), config=_runtime_config()) as cold_runtime:
        imported = cold_runtime.import_session_bundle_file(bundle_path=bundle_path)
        assert imported.session_count == 2
        artifact_id = graph.parent_uri.rsplit("/", 1)[1]
        page = cold_runtime.read_tool_output_artifact(
            caller_session_id=PARENT,
            session_id=PARENT,
            artifact_id=artifact_id,
            offset=0,
            limit=20000,
        )
        assert isinstance(page, ArtifactPage)
        assert json.loads(page.text) == DATA
        foreign_response = cold_runtime.run(RuntimeRequest(prompt="foreign session", session_id="foreign-session"))
        assert foreign_response.session.status == "completed"
        denied = cold_runtime.read_tool_output_artifact(
            caller_session_id="foreign-session",
            session_id=PARENT,
            artifact_id=artifact_id,
            limit=0,
        )
        assert isinstance(denied, ArtifactMissing)
        with pytest.raises(ValueError, match="only its parent session"):
            _ = TaskOutputTool(runtime=cold_runtime).invoke(
                _call("task_output", {"task_id": graph.task_id}),
                context=ToolContext(session_id="foreign-session"),
            )


def test_task_batch_mixed_results_keep_error_and_readable_parent_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _prepare_runtime_paths(monkeypatch, tmp_path, workspace)
    graph = _BatchGraph()

    with VoidCodeRuntime(workspace=workspace, turn_producer=graph, config=_runtime_config()) as runtime:
        response = runtime.run(RuntimeRequest(prompt="mixed task batch", session_id="batch-parent"))
        assert response.session.status == "completed"
        assert graph.group_id is not None and graph.parent_uri is not None
        body = _task_output_body(response)
        results = cast(list[dict[str, object]], body["results"])
        counts = cast(dict[str, int], body["counts"])
        succeeded = next(item for item in results if item["status"] == "completed")
        failed = next(item for item in results if item["status"] == "failed")
        success_ref = succeeded["structured_output_reference"]
        failure_ref = failed["structured_output_reference"]
        assert body["status"] == "mixed" and body["complete"] is True
        assert counts["completed"] == 1 and counts["failed"] == 1
        assert "schema validation failed" in str(failed["error"])
        assert "unexpected" in str(failed["error"]) and "answer" in str(failed["error"])
        assert success_ref == graph.parent_uri
        assert isinstance(failure_ref, str) and failure_ref != success_ref
        artifacts = cast(list[dict[str, object]], body["artifacts"])
        assert {artifact["uri"] for artifact in artifacts} == {success_ref, failure_ref}
        assert all(artifact["session_id"] == "batch-parent" for artifact in artifacts)
        successful_result = runtime.load_background_task_result(cast(str, succeeded["task_id"]))
        failed_result = runtime.load_background_task_result(cast(str, failed["task_id"]))
        assert successful_result.status == "completed"
        assert successful_result.schema_validation is not None and successful_result.schema_validation.valid is True
        assert failed_result.status == "failed"
        assert failed_result.schema_validation is not None and failed_result.schema_validation.valid is False
        success_artifact = cast(dict[str, object], successful_result.structured_output_artifact)
        failure_artifact = cast(dict[str, object], failed_result.structured_output_artifact)
        serialized_body = json.dumps(body, ensure_ascii=False)
        for child_artifact in (success_artifact, failure_artifact):
            assert child_artifact["uri"] not in serialized_body
            assert child_artifact["path"] not in serialized_body
        for reference, child_artifact, expected in (
            (success_ref, success_artifact, {"answer": "valid result"}),
            (failure_ref, failure_artifact, {"unexpected": 7}),
        ):
            parent_artifact_id = cast(str, reference).rsplit("/", 1)[1]
            page = runtime.read_tool_output_artifact(
                caller_session_id="batch-parent",
                session_id="batch-parent",
                artifact_id=parent_artifact_id,
                offset=0,
                limit=2000,
            )
            assert isinstance(page, ArtifactPage)
            assert json.loads(page.text) == expected
            assert page.sha256 == child_artifact["sha256"]
            assert page.byte_count == child_artifact["byte_count"]


@pytest.mark.parametrize("batch_kind", ["oversized", "duplicate"])
def test_task_batch_rejects_invalid_batches_before_child_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    batch_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _prepare_runtime_paths(monkeypatch, tmp_path, workspace)
    with VoidCodeRuntime(
        workspace=workspace,
        turn_producer=_ForeignGraph(),
        config=_runtime_config(),
    ) as runtime:
        _ = runtime.run(RuntimeRequest(prompt="task batch preflight", session_id=PARENT))
        command = TaskBatchCommand(runtime=runtime)
        item = {"prompt": "identical child", "load_skills": [], "subagent_type": "worker"}
        tasks = (
            [dict(item) for _ in range(2)]
            if batch_kind == "duplicate"
            else [{"prompt": f"child {index}", "load_skills": [], "subagent_type": "worker"} for index in range(101)]
        )
        with pytest.raises(ValueError):
            _ = command.invoke(_call("task_batch", {"tasks": tasks}), context=ToolContext(session_id=PARENT))
        assert runtime.list_background_tasks_by_parent_session(parent_session_id=PARENT) == ()


def _started_task_id(response: RuntimeResponse) -> str:
    for event in response.events:
        report = reported_call_from_event(event)
        if report is None or report.final_tool_name != "task" or "operation" in report.authorized_arguments:
            continue
        result = cast(dict[str, object], report_payload(report)["result"])
        body = cast(dict[str, object], result["body"])
        return cast(str, body["task_id"])
    raise AssertionError("task start report was not recorded")


class _KeepAliveGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> ToolTurn | FinalTurn:
        if session.metadata.get("parent_session_id") is not None:
            if "finish" in _prompt(request):
                if not any(cast(ToolResultView, result).tool_name == "yield" for result in tool_results):
                    return ToolTurn(calls=(_call("yield", {"summary": "keep-alive task finished"}),))
                return FinalTurn(output="keep-alive task finished")
            return FinalTurn(output="parked until steered or cancelled")
        if not tool_results:
            return ToolTurn(
                calls=(
                    _call(
                        "task",
                        {
                            "prompt": "park keep-alive child",
                            "run_in_background": True,
                            "keep_alive": True,
                            "load_skills": [],
                            "subagent_type": "worker",
                        },
                    ),
                )
            )
        return FinalTurn(output=_text(tool_results))


def test_task_control_steers_and_cancels_idle_keep_alive_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _prepare_runtime_paths(monkeypatch, tmp_path, workspace)
    with VoidCodeRuntime(workspace=workspace, turn_producer=_KeepAliveGraph(), config=_runtime_config()) as runtime:
        control = TaskControlTool(runtime=runtime)

        steered = runtime.run(RuntimeRequest(prompt="start task", session_id="steer-parent"))
        steer_task_id = _started_task_id(steered)
        idle = runtime.wait_for_background_task(steer_task_id, timeout_seconds=3)
        assert idle.status == "idle" and idle.keep_alive is True
        steer_result = control.invoke(
            _call("task", {"operation": "steer", "task_id": steer_task_id, "prompt": "finish the task"}),
            context=ToolContext(session_id="steer-parent"),
        )
        assert steer_result.body is not None
        assert steer_result.body.as_payload()["task_id"] == steer_task_id
        completed = runtime.wait_for_background_task(steer_task_id, timeout_seconds=5)
        assert completed.status == "completed"

        cancelled = runtime.run(RuntimeRequest(prompt="start task", session_id="cancel-parent"))
        cancel_task_id = _started_task_id(cancelled)
        idle = runtime.wait_for_background_task(cancel_task_id, timeout_seconds=3)
        assert idle.status == "idle"
        cancel_result = control.invoke(
            _call("task", {"operation": "cancel", "task_id": cancel_task_id}),
            context=ToolContext(session_id="cancel-parent"),
        )
        assert cancel_result.body is not None
        assert cancel_result.body.as_payload()["status"] == "cancelled"
        assert runtime.load_background_task_result(cancel_task_id).status == "cancelled"


class _MixedControlGraph:
    def __init__(self) -> None:
        self.group_id: str | None = None

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> ToolTurn | FinalTurn:
        if session.metadata.get("parent_session_id") is not None:
            prompt = _prompt(request)
            tool_names = [cast(ToolResultView, result).tool_name for result in tool_results]
            if "approval child" in prompt and not tool_names:
                return ToolTurn(calls=(_call("write", {"path": "approval.txt", "content": "await approval"}),))
            if "question child" in prompt:
                if "question" not in tool_names:
                    return ToolTurn(
                        calls=(
                            _call(
                                "question",
                                {
                                    "questions": [
                                        {
                                            "question": "Which result?",
                                            "header": "Result",
                                            "options": [{"label": "one"}],
                                        }
                                    ]
                                },
                            ),
                        )
                    )
                if "yield" not in tool_names:
                    return ToolTurn(calls=(_call("yield", {"summary": "question policy denial observed"}),))
            return FinalTurn(output="child turn finished")

        if not tool_results:
            return ToolTurn(
                calls=(
                    _call(
                        "task_batch",
                        {
                            "tasks": [
                                {"prompt": "approval child", "load_skills": [], "subagent_type": "worker"},
                                {"prompt": "question child", "load_skills": [], "subagent_type": "worker"},
                            ]
                        },
                    ),
                )
            )
        last = cast(ToolResultView, tool_results[-1])
        text = _text(tool_results)
        if last.tool_name == "task_batch":
            match = re.search(r"Started task batch (batch-[^ ]+)", text)
            if match is None:
                raise AssertionError(f"batch start did not return group id: {text!r}")
            self.group_id = match.group(1)
            return ToolTurn(
                calls=(
                    _call(
                        "task",
                        {"operation": "output", "parallel_group_id": self.group_id, "block": True, "timeout": 5000},
                    ),
                )
            )
        return FinalTurn(output=text)


def test_group_output_keeps_child_approval_and_question_policy_failure_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _prepare_runtime_paths(monkeypatch, tmp_path, workspace)
    config = RuntimeConfig(execution_engine="deterministic", approval_mode="ask", mcp=RuntimeMcpConfig(enabled=False))
    graph = _MixedControlGraph()
    with VoidCodeRuntime(
        workspace=workspace,
        turn_producer=graph,
        config=config,
        permission_policy=PermissionPolicy(mode="ask"),
    ) as runtime:
        response = runtime.run(RuntimeRequest(prompt="mixed approval and question child states", session_id="control-parent"))
        assert response.session.status == "completed"
        assert graph.group_id is not None
        body = _task_output_body(response)
        items = cast(list[dict[str, object]], body["results"])
        approval = next(item for item in items if item["approval_blocked"] is True)
        question_denied = next(item for item in items if "delegation policy denied tool 'question'" in str(item.get("error")))
        assert approval["status"] == "running"
        assert approval["question_request_id"] is None
        assert approval["control_status"] == "approvalBlocked"
        assert question_denied["status"] == "failed"
        assert "question" in str(question_denied["error"])
        assert question_denied["approval_blocked"] is False
        assert question_denied["question_request_id"] is None
        assert question_denied["control_status"] is None
        assert body["complete"] is False
        counts = cast(dict[str, int], body["counts"])
        assert counts["failed"] == 1 and counts["running"] == 1
        artifacts = cast(list[dict[str, object]], body["artifacts"])
        assert all(artifact["session_id"] == "control-parent" for artifact in artifacts)
        artifact_uris = {artifact["uri"] for artifact in artifacts}
        for item in items:
            reference = item["structured_output_reference"]
            if reference is None:
                continue
            assert reference in artifact_uris
            task_result = runtime.load_background_task_result(cast(str, item["task_id"]))
            child_artifact = cast(dict[str, object], task_result.structured_output_artifact)
            serialized_body = json.dumps(body, ensure_ascii=False)
            assert child_artifact["uri"] not in serialized_body
            assert child_artifact["path"] not in serialized_body
            page = runtime.read_tool_output_artifact(
                caller_session_id="control-parent",
                session_id="control-parent",
                artifact_id=cast(str, reference).rsplit("/", 1)[1],
                offset=0,
                limit=2000,
            )
            assert isinstance(page, ArtifactPage)
            assert page.sha256 == child_artifact["sha256"]
            assert page.byte_count == child_artifact["byte_count"]
