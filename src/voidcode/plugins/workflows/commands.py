"""Client commands: start, read, pause, resume, and cancel workflow runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import ClassVar
from uuid import uuid4

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult, ConfirmationRequest
from voidcode.sdk.services import BackgroundActionStatus, PluginServices
from voidcode.sdk.tools import ToolEffect

from .artifacts import _result_page, _run_result_page
from .constants import _NO_CONFIG, _TERMINAL_RUN, STATUS_NODE_LIMIT
from .driver import _refuse_live_driver
from .nodes import _cancel_run_children, _cancellation_note
from .notices import _notify_terminal
from .source import snapshot_workflow_source
from .state import _state_lock, _store
from .tool import _parse_run_arguments, _run_row, _start_run
from .views import WorkflowRunFormView


class WorkflowRunCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="workflow.run",
        text_argument="workflow",
        description="Start a configured workflow script in the background; returns a stable run receipt.",
        argument_schema={
            "type": "object",
            "properties": {
                "workflow": {"type": "string", "description": "Configured workflow name."},
                "task": {"description": "Fixed JSON task for this run; a plain string is passed through verbatim."},
            },
            "required": ["workflow"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN, ToolEffect.SESSION}),
        busy_gate=False,
        retry_state_conflict=True,
        client_syntax="/workflow run",
    )

    def __init__(self, workflows: Mapping[str, str] = _NO_CONFIG) -> None:
        self._workflows = workflows

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        if not self._workflows:
            return CommandPresentation(
                title="Run workflow",
                view_type="notice",
                data={"message": "No workflows are configured. Add a workflow script to voidcode.toml and reload configuration."},
            )
        view = WorkflowRunFormView(self._workflows).render(invocation.context.session_id, services)
        return CommandPresentation(title=view.title, view_type="form", data=view.data)

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        try:
            workflow, task = _parse_run_arguments(invocation.arguments)
            workspace = invocation.context.require_workspace()
            session_id = invocation.context.require_session_id()
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        try:
            started = _start_run(services, workspace, session_id, workflow, task, self._workflows, notify_on_completion=False)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return CommandResult(status="error", error=f"workflow run failed to start: {exc}")
        return CommandResult(
            status="ok",
            output=f"workflow '{started.workflow}' started as run {started.run_id}",
            data={"run_id": started.run_id, "action_id": started.action_id, "workflow": started.workflow, "status": "running"},
            presentation=_run_receipt(started.run_id, "running"),
        )


def _run_receipt(run_id: str, status: str) -> CommandPresentation:
    return CommandPresentation(
        title="Workflow run",
        view_type="markdown",
        data={
            "content": f"Run {run_id} · {status}",
            "actions": [{"label": "View run details", "action": "workflow.status", "arguments": {"run_id": run_id}}],
        },
    )


class _WorkflowRunInput:
    definition: ClassVar[CommandDefinition]

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = invocation
        state = _store(services).load()[0]
        action = self.definition.name
        runs = sorted(state.runs.values(), key=lambda run: run.updated_at, reverse=True)
        if action != "workflow.status":
            runs = [run for run in runs if run.status not in _TERMINAL_RUN]
        if action == "workflow.pause":
            runs = [run for run in runs if run.status != "paused"]
        if action == "workflow.resume":
            runs = [run for run in runs if run.status != "running"]
        if not runs:
            return CommandPresentation(title="Workflow runs", view_type="notice", data={"message": "No matching workflow runs."})
        return CommandPresentation(
            title=self.definition.description,
            view_type="form",
            data={
                "action": action,
                "fields": [
                    {
                        "name": "run_id",
                        "label": f"Workflow run (latest {min(len(runs), STATUS_NODE_LIMIT)} of {len(runs)})"
                        if len(runs) > STATUS_NODE_LIMIT
                        else "Workflow run",
                        "options": [{"id": run.run_id, "label": f"{run.workflow} · {run.status} · {run.run_id}"} for run in runs[:STATUS_NODE_LIMIT]],
                    }
                ],
            },
        )


class WorkflowStatusCommand(_WorkflowRunInput):
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="workflow.status",
        description="Read one workflow run's graph and bounded node results.",
        argument_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Workflow run id."},
                "node_offset": {"type": "integer", "minimum": 0, "description": "First node in the interactive graph page."},
                "result_key": {"type": "string", "description": "Optional node key whose result page to read."},
                "result_offset": {"type": "integer", "minimum": 0, "description": "Byte offset into the node result."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
        client_syntax="/workflow status",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw = invocation.arguments.get("run_id")
        if not isinstance(raw, str) or not raw:
            return CommandResult(status="error", error="workflow.status requires a non-empty 'run_id'")
        result_key = invocation.arguments.get("result_key")
        if result_key is not None and (not isinstance(result_key, str) or not result_key):
            return CommandResult(status="error", error="workflow.status 'result_key' must be a non-empty string")
        result_offset = invocation.arguments.get("result_offset", 0)
        if not isinstance(result_offset, int) or isinstance(result_offset, bool) or result_offset < 0:
            return CommandResult(status="error", error="workflow.status 'result_offset' must be a non-negative integer")
        try:
            state = _store(services).load()[0]
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        run = state.runs.get(raw)
        if run is None:
            return CommandResult(status="error", error=f"unknown workflow run: {raw!r}")
        data = _run_row(run)
        lines = [f"run {run.run_id} workflow={run.workflow} status={run.status}"]
        if run.error:
            lines.append(f"error: {run.error}")
        for node in run.nodes[:STATUS_NODE_LIMIT]:
            line = f"- {node.key}: {node.status}"
            if node.agent:
                line += f" agent={node.agent}"
            if node.error:
                line += f" error={node.error}"
            if node.budget_exhaustion is not None:
                budget = node.budget_exhaustion
                line += f" budget={budget.dimension}/{budget.source} limit={budget.limit} consumed={budget.consumed} remaining={budget.remaining}"
            lines.append(line)
        result_page_info: tuple[str, int, int] | None = None
        if result_key is not None:
            node = run.node(result_key)
            if node is None:
                return CommandResult(status="error", error=f"unknown node: {result_key!r}")
            try:
                page, total = _result_page(services, node, result_offset)
            except (ValueError, UnicodeDecodeError) as exc:
                return CommandResult(status="error", error=str(exc))
            consumed = len(page.encode("utf-8"))
            next_offset = result_offset + consumed
            result_page_info = (page, next_offset, total)
            data["result"] = {
                "key": result_key,
                "status": node.status,
                "offset": result_offset,
                "next_offset": next_offset,
                "total_bytes": total,
                "text": page,
                "budget_exhaustion": None if node.budget_exhaustion is None else node.budget_exhaustion.as_payload(),
            }
            lines.append(f"result {result_key} ({node.status}, {total} bytes):")
            lines.append(page)
            if node.budget_exhaustion is not None:
                budget = node.budget_exhaustion
                lines.append(
                    f"budget exhaustion: {budget.dimension}/{budget.source} "
                    f"limit={budget.limit} consumed={budget.consumed} remaining={budget.remaining}"
                )
        run_summary_info: tuple[str, int] | None = None
        if run.status == "completed":
            try:
                summary, total = _run_result_page(services, run, 0)
            except (ValueError, UnicodeDecodeError) as exc:
                return CommandResult(status="error", error=str(exc))
            run_summary_info = (summary, total)
            data["run_result"] = {"total_bytes": total, "text": summary}
            if total:
                lines.append(f"run result ({total} bytes):")
                lines.append(summary)
        action: BackgroundActionStatus | None = None
        if run.action_id is not None:
            try:
                action = services.background_action_status(run.action_id)
            except (ValueError, RuntimeError) as exc:
                return CommandResult(status="error", error=f"workflow run '{raw}' driver state is unreadable: {exc}")
            data["driver"] = None if action is None else action.as_payload()
            if action is not None:
                lines.append(f"driver: {action.status}" + (f" error={action.error}" if action.error else ""))
        node_offset = invocation.arguments.get("node_offset", 0)
        if type(node_offset) is not int or not 0 <= node_offset <= len(run.nodes):
            return CommandResult(status="error", error="Node offset is beyond the graph; reopen workflow status.")
        actions: list[dict[str, object]] = [{"label": "Refresh status", "action": "workflow.status", "arguments": {"run_id": raw}}]
        if run.status not in _TERMINAL_RUN:
            if run.status != "paused":
                actions.append({"label": "Pause run", "action": "workflow.pause", "arguments": {"run_id": raw}})
            if run.status != "running":
                actions.append({"label": "Resume run", "action": "workflow.resume", "arguments": {"run_id": raw}})
            actions.append({"label": "Cancel run", "action": "workflow.cancel", "arguments": {"run_id": raw}})
        if result_key is not None and result_page_info is not None and node is not None:
            page, next_offset, total = result_page_info
            content = f"Node {result_key[:100]} · {node.status}\n\nBytes {result_offset}–{next_offset} of {total}\n\n{page}"
            if result_offset:
                actions.append(
                    {
                        "label": "First result page",
                        "action": "workflow.status",
                        "arguments": {"run_id": raw, "result_key": result_key, "result_offset": 0},
                    }
                )
            if next_offset < total:
                actions.append(
                    {
                        "label": "Next result page",
                        "action": "workflow.status",
                        "arguments": {"run_id": raw, "result_key": result_key, "result_offset": next_offset},
                    }
                )
        else:
            detail = [f"Workflow: {run.workflow[:100]} · {run.status}"]
            if run.error:
                detail.append(f"Error: {run.error[:500]}")
            detail.append(f"Nodes {node_offset}–{min(node_offset + 10, len(run.nodes))} of {len(run.nodes)}")
            for graph_node in run.nodes[node_offset : node_offset + 10]:
                detail.append(f"- {graph_node.key[:80]} · {graph_node.status}" + (f" · {graph_node.error[:120]}" if graph_node.error else ""))
                actions.append(
                    {"label": f"Read: {graph_node.key[:60]}", "action": "workflow.status", "arguments": {"run_id": raw, "result_key": graph_node.key}}
                )
            if node_offset:
                actions.append(
                    {"label": "Previous nodes", "action": "workflow.status", "arguments": {"run_id": raw, "node_offset": max(0, node_offset - 10)}}
                )
            if node_offset + 10 < len(run.nodes):
                actions.append({"label": "Next nodes", "action": "workflow.status", "arguments": {"run_id": raw, "node_offset": node_offset + 10}})
            if run_summary_info is not None:
                summary, total = run_summary_info
                detail.append(f"Run summary preview ({total} total bytes):\n{summary[:1000]}")
            content = "\n\n".join(detail)
        return CommandResult(
            status="ok",
            output="\n".join(lines),
            data=data,
            presentation=CommandPresentation(title="Workflow run", view_type="markdown", data={"content": content, "actions": actions}),
        )


class WorkflowPauseCommand(_WorkflowRunInput):
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="workflow.pause",
        description="Park a workflow driver; running children keep running.",
        argument_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Workflow run id."}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
        retry_state_conflict=True,
        client_syntax="/workflow pause",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw = invocation.arguments.get("run_id")
        if not isinstance(raw, str) or not raw:
            return CommandResult(status="error", error="workflow.pause requires a non-empty 'run_id'")
        with _state_lock(invocation.context.session_id):
            try:
                state, version = _store(services).load()
            except ValueError as exc:
                return CommandResult(status="error", error=str(exc))
            run = state.runs.get(raw)
            if run is None:
                return CommandResult(status="error", error=f"unknown workflow run: {raw!r}")
            if run.status in _TERMINAL_RUN:
                return CommandResult(status="error", error=f"workflow run '{raw}' is {run.status}")
            if run.action_id is not None:
                try:
                    services.cancel_background_action(run.action_id, reason="workflow pause")
                except (ValueError, RuntimeError) as exc:
                    return CommandResult(status="error", error=f"workflow run '{raw}' failed to pause: {exc}")
            try:
                _store(services).save(state.with_run(run.with_status("paused", None)), version)
            except (ValueError, RuntimeError) as exc:
                return CommandResult(status="error", error=f"workflow run '{raw}' failed to pause: {exc}")
        return CommandResult(
            status="ok", output=f"run {raw} paused", data={"run_id": raw, "status": "paused"}, presentation=_run_receipt(raw, "paused")
        )


class WorkflowResumeCommand(_WorkflowRunInput):
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="workflow.resume",
        description="Resume a paused or parked run with the same script, input, and node keys.",
        argument_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Workflow run id."}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN, ToolEffect.SESSION}),
        busy_gate=False,
        retry_state_conflict=True,
        client_syntax="/workflow resume",
    )

    def __init__(self, workflows: Mapping[str, str] = _NO_CONFIG) -> None:
        self._workflows = workflows

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw = invocation.arguments.get("run_id")
        if not isinstance(raw, str) or not raw:
            return CommandResult(status="error", error="workflow.resume requires a non-empty 'run_id'")
        with _state_lock(invocation.context.session_id):
            try:
                workspace = invocation.context.require_workspace()
                state, version = _store(services).load()
            except (ValueError, RuntimeError) as exc:
                return CommandResult(status="error", error=str(exc))
            run = state.runs.get(raw)
            if run is None:
                return CommandResult(status="error", error=f"unknown workflow run: {raw!r}")
            if run.status in _TERMINAL_RUN:
                return CommandResult(status="error", error=f"workflow run '{raw}' is {run.status}")
            if run.status == "running":
                refusal = _refuse_live_driver(services, run)
                if refusal is not None:
                    return CommandResult(status="error", error=refusal)
            try:
                _relative, _source, live = snapshot_workflow_source(self._workflows, workspace, run.workflow)
            except ValueError as exc:
                return CommandResult(status="error", error=str(exc))
            if live != run.source_sha256:
                return CommandResult(status="error", error=f"workflow '{run.workflow}' source changed; refusing to resume a different script")
            try:
                action_id = services.enqueue_background_action(
                    {"kind": "workflow", "run_id": raw, "workflow": run.workflow},
                    idempotency_key=f"workflow:{raw}:resume:{run.updated_at}",
                )
            except (ValueError, RuntimeError, PermissionError) as exc:
                return CommandResult(status="error", error=f"workflow run '{raw}' failed to resume: {exc}")
            run = replace(run.with_status("running", None), action_id=action_id)
            try:
                _store(services).save(state.with_run(run), version)
            except (ValueError, RuntimeError) as exc:
                return CommandResult(status="error", error=f"workflow run '{raw}' failed to resume: {exc}")
        return CommandResult(
            status="ok",
            output=f"run {raw} resumed",
            data={"run_id": raw, "action_id": action_id, "status": "running"},
            presentation=_run_receipt(raw, "running"),
        )


class WorkflowCancelCommand(_WorkflowRunInput):
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="workflow.cancel",
        description="Stop a workflow driver and every child it owns; no new node is accepted afterward.",
        argument_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Workflow run id."}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
        retry_state_conflict=True,
        client_syntax="/workflow cancel",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw = invocation.arguments.get("run_id")
        if not isinstance(raw, str) or not raw:
            return CommandResult(status="error", error="workflow.cancel requires a non-empty 'run_id'")
        with _state_lock(invocation.context.session_id):
            try:
                state, version = _store(services).load()
            except ValueError as exc:
                return CommandResult(status="error", error=str(exc))
            run = state.runs.get(raw)
            if run is None:
                return CommandResult(status="error", error=f"unknown workflow run: {raw!r}")
            if run.status in _TERMINAL_RUN:
                return CommandResult(status="error", error=f"workflow run '{raw}' is {run.status}")
            live = [node.key for node in run.nodes if node.status in ("pending", "running", "waiting", "interrupted")]
            if live and invocation.context.actor != "user":
                request_id = uuid4().hex
                return CommandResult(
                    status="pending_confirmation",
                    confirmation=ConfirmationRequest(
                        request_id=request_id,
                        owner="workflows",
                        command_name=self.definition.name,
                        arguments=invocation.arguments,
                        ui_type="confirmation",
                        ui_data={
                            "request_id": request_id,
                            "message": f"Stop run {raw} ({run.workflow}) and {len(live)} live children ({', '.join(sorted(live)[:5])})?",
                            "actions": ["allow", "deny"],
                        },
                        expected_state_version=version,
                    ),
                )
            failures = _cancel_run_children(services, run, reason="workflow cancellation")
            if run.action_id is not None:
                cancel_action = services.cancel_background_action
                try:
                    cancel_action(run.action_id, reason="workflow cancellation")
                except (ValueError, RuntimeError) as exc:
                    return CommandResult(status="error", error=f"workflow run '{raw}' failed to cancel: {exc}")
            # Re-read after the slow RPCs: the driver may have committed while we cancelled.
            try:
                state, version = _store(services).load()
            except ValueError as exc:
                return CommandResult(status="error", error=str(exc))
            run = state.runs.get(raw)
            if run is None:
                return CommandResult(status="error", error=f"unknown workflow run: {raw!r}")
            if run.status in _TERMINAL_RUN:
                return CommandResult(status="ok", output=f"run {raw} already {run.status}", data={"run_id": raw, "status": run.status})
            nodes = tuple(
                replace(node, status="cancelled", budget_exhaustion=None) if node.status in ("pending", "running", "waiting", "interrupted") else node
                for node in run.nodes
            )
            error = "owner cancellation" + _cancellation_note(failures)
            run = replace(run.with_status("cancelled", error), nodes=nodes)
            try:
                _store(services).save(state.with_run(run), version)
            except (ValueError, RuntimeError) as exc:
                return CommandResult(status="error", error=f"workflow run '{raw}' failed to cancel: {exc}")
            _notify_terminal(services, run)
        return CommandResult(
            status="ok",
            output=f"run {raw} cancelled ({len(live)} stopped)",
            data={"run_id": raw, "status": "cancelled"},
            presentation=_run_receipt(raw, "cancelled"),
        )
