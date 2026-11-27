"""Real-Core regressions for lease-free background tool waits."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.polling import wait_for
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.core.events import EventType
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ConfirmedStop, ToolFailure

PLUGIN_ID = "background-tool-wait-fixture"

_PLUGIN_CODE = """
from __future__ import annotations

import threading
import time
from pathlib import Path

from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import ToolInvocationReceipt
from voidcode.sdk.tools import (
    ConfirmedStop,
    TextOutput,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolSuccess,
)

_ACTIVE_CONTEXT = None
_PRIOR_RECEIPT = None


def _state(services):
    value, version = services.get_state("session")
    return (dict(value) if value else {}), version


def _summary(outcome):
    result = outcome.result
    budget = outcome.budget_exhaustion
    reference = None if result is None else result.result_reference
    return {
        "status": outcome.status,
        "tool_name": outcome.tool_name,
        "stop_cause": outcome.stop_cause,
        "result_status": None if result is None else result.status,
        "error": result.error if isinstance(result, ToolFailure) else None,
        "reference_bytes": None if reference is None else reference.bytes,
        "budget": None if budget is None else {
            "dimension": budget.dimension,
            "source": budget.source,
            "limit": budget.limit,
            "consumed": budget.consumed,
            "remaining": budget.remaining,
        },
    }


def _record(context, outcome, **extra):
    value = _summary(outcome)
    value.update(extra)

    def save(_ctx, services):
        state, version = _state(services)
        state["last_await"] = value
        services.replace_state(state, expected_version=version)

    context.invoke(save)


def _record_refusal(context, error):
    def save(_ctx, services):
        state, version = _state(services)
        state["last_refusal"] = error
        services.replace_state(state, expected_version=version)

    context.invoke(save)


def _request_read(context, path):
    return context.invoke(
        lambda ctx, services: services.invoke_tool(
            "read", {"path": path}, expected_revision=ctx.revision_id
        )
    )


def _read_text(context, path):
    receipt = _request_read(context, path)
    outcome = context.wait_tool(receipt)
    result = outcome.result
    if not isinstance(result, ToolSuccess) or not isinstance(result.output, TextOutput):
        return receipt, outcome, None, None
    digest = result.coverage[0].content_hash if result.coverage else None
    return receipt, outcome, result, digest


def _request_edit(ctx, services, path, old, new, digest):
    return services.invoke_tool(
        "edit",
        {"path": path, "oldString": old, "newString": new, "expectedHash": digest},
        expected_revision=ctx.revision_id,
    )


class EnqueueCommand:
    definition = CommandDefinition(
        name="wait_enqueue",
        description="Enqueue a background SDK action.",
        argument_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string"},
                "path": {"type": "string"},
                "path2": {"type": "string"},
                "completed_path": {"type": "string"},
                "pending_path": {"type": "string"},
                "started_path": {"type": "string"},
                "release_path": {"type": "string"},
                "waiting_path": {"type": "string"},
            },
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation, services):
        action_id = services.enqueue_background_action(dict(invocation.arguments))
        return CommandResult(status="ok", output=action_id, data={"action_id": action_id})


class StatusCommand:
    definition = CommandDefinition(
        name="wait_status",
        description="Read one background action status.",
        argument_schema={
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, invocation, services):
        status = services.background_action_status(str(invocation.arguments["action_id"]))
        if status is None:
            return CommandResult(status="error", error="unknown action")
        return CommandResult(status="ok", output=status.status, data=status.as_payload())


class CancelCommand:
    definition = CommandDefinition(
        name="wait_cancel",
        description="Cancel one background action.",
        argument_schema={
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation, services):
        cancelled = services.cancel_background_action(str(invocation.arguments["action_id"]))
        return CommandResult(status="ok", output=str(cancelled), data={"cancelled": cancelled})


class RootProbe:
    definition = ToolDefinition(
        name="root_probe",
        description="Report the captured root authorization.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, call, *, context, services):
        _ = call, services
        return ToolSuccess("root_probe", TextOutput(str(context.root_action_id)))


class ReentrantProbe:
    definition = ToolDefinition(
        name="reentrant_probe",
        description="Exercise the active callback wait guard.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, call, *, context, services):
        _ = call, context, services
        try:
            _ACTIVE_CONTEXT.wait_tool(_PRIOR_RECEIPT)
        except RuntimeError as exc:
            return ToolFailure("reentrant_probe", str(exc))
        return ToolSuccess("reentrant_probe", TextOutput("wait unexpectedly returned"))


class GateWrite:
    definition = ToolDefinition(
        name="gate_write",
        description="Wait for cancellation before a test-only file write.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "started_path": {"type": "string"},
                "release_path": {"type": "string"},
            },
            "required": ["path", "started_path", "release_path"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path",),
    )

    def invoke(self, call, *, context, services):
        _ = services
        args = call.arguments
        started = Path(str(args["started_path"]))
        release = Path(str(args["release_path"]))
        started.touch()
        while not context.abort_signal.cancelled and not release.exists():
            threading.Event().wait(0.02)
        if context.abort_signal.cancelled:
            return ToolFailure(
                "gate_write",
                "stopped before write",
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        Path(str(args["path"])).write_text("late write", encoding="utf-8")
        return ToolSuccess("gate_write", TextOutput("written"))


def drive(context):
    global _ACTIVE_CONTEXT, _PRIOR_RECEIPT
    action = dict(context.payload)
    mode = action.get("mode")
    if mode in ("read-edit", "approval-edit", "crash-edit"):
        path = str(action["completed_path"]) if mode == "crash-edit" else str(action["path"])
        receipt, read_outcome, read_result, digest = _read_text(context, path)
        selected = read_result.output.text if read_result is not None else ""
        if read_result is None or digest is None or "branch-token=ALPHA" not in selected:
            _record(context, read_outcome, selected_marker="missing", selected_chars=len(selected))
            return
        edit_receipt = context.invoke(
            lambda ctx, services: _request_edit(
                ctx, services, path, "branch-token=ALPHA", "branch-token=BRANCHED", digest
            )
        )
        if mode == "crash-edit":
            completed_edit = context.wait_tool(edit_receipt)
            if (
                completed_edit.status != "completed"
                or completed_edit.result is None
                or completed_edit.result.status != "ok"
            ):
                _record(context, completed_edit, phase="completed-edit")
                return
            pending_read_receipt, pending_read_outcome, pending_read, pending_digest = _read_text(
                context, str(action["pending_path"])
            )
            if pending_read is None or pending_digest is None:
                _record(context, pending_read_outcome)
                return
            pending_edit_receipt = context.invoke(
                lambda ctx, services: _request_edit(
                    ctx,
                    services,
                    str(action["pending_path"]),
                    "branch-token=ALPHA",
                    "branch-token=PENDING",
                    pending_digest,
                )
            )
            pending_edit = context.wait_tool(pending_edit_receipt)
            _record(
                context,
                pending_edit,
                completed_edit_status=completed_edit.status,
                pending_read_request_id=pending_read_receipt.request_id,
            )
            return
        edit_outcome = context.wait_tool(edit_receipt)
        _record(
            context,
            edit_outcome,
            read_request_id=receipt.request_id,
            selected_marker="branch-token=ALPHA",
            selected_chars=len(selected),
            canonical_bytes=read_result.result_reference.bytes if read_result.result_reference else None,
        )
        return
    if mode == "queued-edits":
        _first_receipt, first_read_outcome, first_read, first_digest = _read_text(context, str(action["path"]))
        _second_receipt, second_read_outcome, second_read, second_digest = _read_text(context, str(action["path2"]))
        if first_read is None or second_read is None or first_digest is None or second_digest is None:
            _record(context, second_read_outcome if second_read is None else first_read_outcome)
            return
        if "branch-token=ALPHA" not in first_read.output.text or "branch-token=ALPHA" not in second_read.output.text:
            _record(context, second_read_outcome, selected_marker="missing")
            return

        def stage_edits(ctx, services):
            first_receipt = _request_edit(
                ctx, services, str(action["path"]), "branch-token=ALPHA", "branch-token=ONE", first_digest
            )
            second_receipt = _request_edit(
                ctx, services, str(action["path2"]), "branch-token=ALPHA", "branch-token=TWO", second_digest
            )
            return first_receipt, second_receipt

        first_edit, second_edit = context.invoke(stage_edits)
        first_outcome = context.wait_tool(first_edit)
        second_outcome = context.wait_tool(second_edit)
        _record(
            context,
            second_outcome,
            first_status=first_outcome.status,
            first_result_status=first_outcome.result.status if first_outcome.result else None,
        )
        return
    if mode in ("scope-probe", "stale-probe", "missing-authority"):
        try:
            receipt = context.invoke(
                lambda ctx, services: services.invoke_tool(
                    "root_probe",
                    {},
                    expected_revision="stale-revision" if mode == "stale-probe" else ctx.revision_id,
                )
            )
        except (PermissionError, ValueError) as exc:
            _record_refusal(context, str(exc))
            return
        _record(context, context.wait_tool(receipt))
        return
    if mode == "root-gate":
        Path(str(action["started_path"])).touch()
        while not Path(str(action["release_path"])).exists():
            threading.Event().wait(0.02)
        receipt = context.invoke(
            lambda ctx, services: services.invoke_tool(
                "root_probe", {}, expected_revision=ctx.revision_id
            )
        )
        _record(context, context.wait_tool(receipt))
        return
    if mode == "reentrant":
        receipt, read_outcome, read_result, _digest = _read_text(context, str(action["path"]))
        if read_result is None:
            _record(context, read_outcome)
            return
        _ACTIVE_CONTEXT = context
        _PRIOR_RECEIPT = receipt
        await_receipt = context.invoke(
            lambda ctx, services: services.invoke_tool(
                "reentrant_probe", {}, expected_revision=ctx.revision_id
            )
        )
        _record(context, context.wait_tool(await_receipt))
        return
    if mode == "lifecycle-gate":
        receipt = context.invoke(
            lambda ctx, services: services.invoke_tool(
                "gate_write",
                {
                    "path": str(action["path"]),
                    "started_path": str(action["started_path"]),
                    "release_path": str(action["release_path"]),
                },
                expected_revision=ctx.revision_id,
            )
        )
        context.wait_tool(receipt)


def create_plugin(config):
    _ = config
    return Plugin(
        commands=(EnqueueCommand(), StatusCommand(), CancelCommand()),
        tools=(RootProbe(), ReentrantProbe(), GateWrite()),
        background_action=drive,
    )
"""


def _turn(output: str) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": []}, finish="stop")


@contextmanager
def _server(replies: list[dict[str, object]] | None = None) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies or [])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _configure(tmp_path: Path, server: OpenAILoopbackServer, *, approval: str = "yolo") -> Path:
    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(_PLUGIN_CODE, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": approval, "read_only": False},
                "providers": {
                    "endpoint": {
                        "api_key": "wait-fixture-secret",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": True}, PLUGIN_ID: {"path": str(source), "enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _open(tmp_path: Path) -> Core:
    return open_core(tmp_path / "workspace", state_dir=tmp_path / "state")


_wait_for = partial(wait_for, timeout=30, interval=0.02)


def _enqueue(core: Core, session_id: str, **arguments: object) -> str:
    result = core.invoke_command(session_id, "wait_enqueue", dict(arguments), expected_revision=core.revision_id)
    assert result.status == "ok", result
    assert isinstance(result.data, Mapping)
    return str(result.data["action_id"])


def _plugin_state(core: Core, session_id: str) -> dict[str, object]:
    state, _version = core.store.plugin_state(PLUGIN_ID, "session", session_id)
    return {} if state is None else dict(state)


def _events(core: Core, session_id: str):
    return core.store.events(session_id)


def _pending_approval(core: Core, session_id: str):
    return next(event for event in _events(core, session_id) if event.event_type == EventType.APPROVAL_REQUESTED)


def test_external_sdk_wait_hydrates_large_read_and_edits_selected_page(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server)
        content = "branch-token=ALPHA\n" + "".join(f"line-{index:04d}:" + "x" * 130 + "\n" for index in range(200))
        (workspace / "large.txt").write_text(content, encoding="utf-8")
        with _open(tmp_path) as core:
            core.create_session("parent")
            action_id = _enqueue(core, "parent", mode="read-edit", path="large.txt")
            _wait_for(
                lambda: (
                    (core.store.background_action(action_id) is not None)
                    and core.store.background_action(action_id).status in ("completed", "failed", "cancelled", "interrupted")
                ),
                what="ordinary SDK read/branch/edit completion",
            )
            record = core.store.background_action(action_id)
            status = core.invoke_command("parent", "wait_status", {"action_id": action_id}, expected_revision=core.revision_id)
            assert record is not None and record.status == "completed", (
                status.data,
                _plugin_state(core, "parent"),
                [
                    (event.event_type, event.payload.get("status"), event.payload.get("error"))
                    for event in _events(core, "parent")
                    if event.event_type in (EventType.TOOL_DEFERRED_CANCELLED, EventType.TOOL_INTERRUPTED)
                ],
            )
            result = _plugin_state(core, "parent")["last_await"]
            assert result["status"] == "completed"
            assert result["result_status"] == "ok"
            assert result["selected_marker"] == "branch-token=ALPHA"
            assert result["canonical_bytes"] > 65_536
            assert (workspace / "large.txt").read_text(encoding="utf-8").startswith("branch-token=BRANCHED\n")
            requests = [event for event in _events(core, "parent") if event.event_type == EventType.TOOL_DEFERRED]
            assert len(requests) == 2
            assert all(event.payload.get("background_action_id") == action_id for event in requests)
            completed_results = [
                event for event in _events(core, "parent") if event.event_type == EventType.TOOL_COMPLETED and event.payload.get("deferred")
            ]
            assert len(completed_results) == 2


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_background_tool_approval_is_responsive_and_reports_actual_result(tmp_path: Path, decision: str) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server, approval="ask")
        target = workspace / "target.txt"
        target.write_text("branch-token=ALPHA\n", encoding="utf-8")
        with _open(tmp_path) as core:
            core.create_session("parent")
            action_id = _enqueue(core, "parent", mode="approval-edit", path="target.txt")
            _wait_for(lambda: any(event.event_type == EventType.APPROVAL_REQUESTED for event in _events(core, "parent")), what="edit approval")
            approval = _pending_approval(core, "parent")
            assert approval.payload["call"]["tool_name"] == "edit"
            # A normal Core command remains available while the action driver waits without a lease.
            ping = core.invoke_command("parent", "wait_status", {"action_id": action_id}, expected_revision=core.revision_id)
            assert ping.status == "ok"
            resumed = core.resume("parent", approval_request_id=str(approval.payload["request_id"]), decision=decision)
            assert resumed.status == "completed", resumed.error
            _wait_for(
                lambda: (
                    (core.store.background_action(action_id) is not None)
                    and core.store.background_action(action_id).status in ("completed", "cancelled")
                ),
                what="approval result delivery",
            )
            outcome = _plugin_state(core, "parent")["last_await"]
            if decision == "allow":
                assert target.read_text(encoding="utf-8") == "branch-token=BRANCHED\n"
                assert outcome["status"] == "completed" and outcome["result_status"] == "ok"
            else:
                assert target.read_text(encoding="utf-8") == "branch-token=ALPHA\n"
                assert outcome["status"] == "completed" and outcome["result_status"] == "error"
                assert "user denied" in outcome["error"]


def test_queued_background_request_progresses_after_earlier_approval(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server, approval="ask")
        first_file, second_file = workspace / "one.txt", workspace / "two.txt"
        first_file.write_text("branch-token=ALPHA\n", encoding="utf-8")
        second_file.write_text("branch-token=ALPHA\n", encoding="utf-8")
        with _open(tmp_path) as core:
            core.create_session("parent")
            action_id = _enqueue(core, "parent", mode="queued-edits", path="one.txt", path2="two.txt")
            _wait_for(
                lambda: len([event for event in _events(core, "parent") if event.event_type == EventType.APPROVAL_REQUESTED]) == 1,
                what="first queued approval",
            )
            approvals = [event for event in _events(core, "parent") if event.event_type == EventType.APPROVAL_REQUESTED]
            assert approvals[0].payload["call"]["arguments"]["path"] == "one.txt"
            first_resume = core.resume("parent", approval_request_id=str(approvals[0].payload["request_id"]), decision="allow")
            assert first_resume.status == "waiting", first_resume.error
            _wait_for(
                lambda: len([event for event in _events(core, "parent") if event.event_type == EventType.APPROVAL_REQUESTED]) == 2,
                what="queued second approval",
            )
            approvals = [event for event in _events(core, "parent") if event.event_type == EventType.APPROVAL_REQUESTED]
            assert approvals[1].payload["call"]["arguments"]["path"] == "two.txt"
            second_resume = core.resume("parent", approval_request_id=str(approvals[1].payload["request_id"]), decision="allow")
            assert second_resume.status == "completed", second_resume.error
            _wait_for(
                lambda: (core.store.background_action(action_id) is not None) and core.store.background_action(action_id).status == "completed",
                what="both queued tool results",
            )
            assert first_file.read_text(encoding="utf-8") == "branch-token=ONE\n"
            assert second_file.read_text(encoding="utf-8") == "branch-token=TWO\n"
            edit_intents = [
                event
                for event in _events(core, "parent")
                if event.event_type == EventType.TOOL_INTENT and event.payload.get("call", {}).get("tool_name") == "edit"
            ]
            assert len(edit_intents) == 2


def test_cancel_disarms_pending_background_approval_and_rejects_late_allow(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server, approval="ask")
        target = workspace / "target.txt"
        target.write_text("branch-token=ALPHA\n", encoding="utf-8")
        with _open(tmp_path) as core:
            core.create_session("parent")
            action_id = _enqueue(core, "parent", mode="approval-edit", path="target.txt")
            _wait_for(lambda: any(event.event_type == EventType.APPROVAL_REQUESTED for event in _events(core, "parent")), what="pending approval")
            approval = _pending_approval(core, "parent")
            cancelled = core.invoke_command("parent", "wait_cancel", {"action_id": action_id}, expected_revision=core.revision_id)
            assert cancelled.status == "ok", cancelled
            assert cancelled.data["cancelled"] is True
            _wait_for(
                lambda: (core.store.background_action(action_id) is not None) and core.store.background_action(action_id).status == "cancelled",
                what="action cancellation",
            )
            with pytest.raises(ValueError, match="no resumable checkpoint"):
                core.resume("parent", approval_request_id=str(approval.payload["request_id"]), decision="allow")
            assert target.read_text(encoding="utf-8") == "branch-token=ALPHA\n"
            terminal = [event for event in _events(core, "parent") if event.event_type == EventType.TOOL_DEFERRED_CANCELLED]
            assert any(event.payload.get("status") == "cancelled" for event in terminal)


def test_cancel_stale_proposal_preserves_other_background_approval(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server, approval="ask")
        first_path, second_path = workspace / "action-a.txt", workspace / "action-b.txt"
        first_path.write_text("branch-token=ALPHA\n", encoding="utf-8")
        second_path.write_text("branch-token=ALPHA\n", encoding="utf-8")
        with _open(tmp_path) as core:
            core.create_session("parent")
            action_a = _enqueue(core, "parent", mode="approval-edit", path="action-a.txt")
            _wait_for(
                lambda: any(
                    event.event_type == EventType.APPROVAL_REQUESTED
                    and event.payload.get("call", {}).get("arguments", {}).get("path") == "action-a.txt"
                    for event in _events(core, "parent")
                ),
                what="action A approval",
            )
            approval_a = _pending_approval(core, "parent")
            action_b = _enqueue(core, "parent", mode="approval-edit", path="action-b.txt")
            _wait_for(
                lambda: any(
                    event.event_type == EventType.TOOL_DEFERRED and event.payload.get("background_action_id") == action_b
                    for event in _events(core, "parent")
                ),
                what="queued action B request",
            )

            proposal_ready = threading.Event()
            release_proposal = threading.Event()
            original_commit = core._commit_tool_wait_events

            def hold_action_a_proposal(
                session_id: str,
                expected_session: Any,
                events: Sequence[Any],
                **kwargs: Any,
            ) -> None:
                is_action_a_cancel = session_id == "parent" and any(
                    event.event_type == EventType.TOOL_DEFERRED_CANCELLED and event.payload.get("background_action_id") == action_a
                    for event in events
                )
                if is_action_a_cancel:
                    proposal_ready.set()
                    if not release_proposal.wait(timeout=30):
                        raise TimeoutError("cancel proposal gate was not released")
                original_commit(session_id, expected_session, events, **kwargs)

            core._commit_tool_wait_events = hold_action_a_proposal  # type: ignore[method-assign]
            cancel_results: list[bool] = []
            cancel_errors: list[Exception] = []

            def cancel_action_a() -> None:
                try:
                    cancel_results.append(core.cancel_background_action("parent", PLUGIN_ID, action_a, "race regression"))
                except Exception as exc:
                    cancel_errors.append(exc)

            cancel_thread = threading.Thread(target=cancel_action_a, daemon=True)
            try:
                cancel_thread.start()
                assert proposal_ready.wait(timeout=30), "cancel proposal was not built"
                resumed_a = core.resume(
                    "parent",
                    approval_request_id=str(approval_a.payload["request_id"]),
                    decision="deny",
                )
                assert resumed_a.status != "waiting", resumed_a.error

                def action_b_approval():
                    return next(
                        (
                            event
                            for event in _events(core, "parent")
                            if event.event_type == EventType.APPROVAL_REQUESTED
                            and event.payload.get("call", {}).get("arguments", {}).get("path") == "action-b.txt"
                        ),
                        None,
                    )

                _wait_for(lambda: action_b_approval() is not None, what="action B approval")
                approval_b = action_b_approval()
                assert approval_b is not None
                assert first_path.read_text(encoding="utf-8") == "branch-token=ALPHA\n"
                assert second_path.read_text(encoding="utf-8") == "branch-token=ALPHA\n"

                release_proposal.set()
                cancel_thread.join(timeout=30)
                assert not cancel_thread.is_alive()
                assert cancel_results == [True]
                assert not cancel_errors
                assert second_path.read_text(encoding="utf-8") == "branch-token=ALPHA\n"

                resumed_b = core.resume(
                    "parent",
                    approval_request_id=str(approval_b.payload["request_id"]),
                    decision="allow",
                )
                assert resumed_b.status == "completed", resumed_b.error
                assert second_path.read_text(encoding="utf-8") == "branch-token=BRANCHED\n"
            finally:
                release_proposal.set()
                if cancel_thread.ident is not None:
                    cancel_thread.join(timeout=30)
                core._commit_tool_wait_events = original_commit  # type: ignore[method-assign]


def test_active_tool_callback_cannot_reenter_background_wait(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server)
        (workspace / "small.txt").write_text("guard target\n", encoding="utf-8")
        with _open(tmp_path) as core:
            core.create_session("parent")
            action_id = _enqueue(core, "parent", mode="reentrant", path="small.txt")
            _wait_for(
                lambda: (core.store.background_action(action_id) is not None) and core.store.background_action(action_id).status == "completed",
                what="reentrant guard outcome",
            )
            outcome = _plugin_state(core, "parent")["last_await"]
            assert outcome["result_status"] == "error"
            assert "inside an active plugin callback" in outcome["error"]


def test_delayed_tool_request_keeps_original_root_budget(tmp_path: Path) -> None:
    with _server([_turn("first finite root"), _turn("new unlimited root")]) as server:
        workspace = _configure(tmp_path, server)
        with _open(tmp_path) as core:
            first = core.run("first finite root", session_id="parent", root_action_budget=1)
            assert first.status == "completed", first.error
            checkpoint = core.store.checkpoint("parent")
            assert checkpoint is not None
            original_root = str(checkpoint.payload["root_id"])
            assert core.store.root_budget_state(original_root) == (1, 1)
            started = workspace / "started"
            release = workspace / "release"
            action_id = "delayed-original-root-wait"
            core.store.enqueue_background_action(
                action_id,
                PLUGIN_ID,
                "parent",
                core.revision_id,
                {"mode": "root-gate", "started_path": str(started), "release_path": str(release)},
                root_id=original_root,
                root_action_budget=1,
                authority={
                    "visible_tools": list(core.visible_tool_names_for_session("parent", core._snapshot)),
                    "readonly": False,
                    "revision_id": core.revision_id,
                },
            )
            core._background.notify()
            _wait_for(lambda: started.exists(), what="delayed driver gate")
            second = core.run("new unlimited root", session_id="parent")
            assert second.status == "completed", second.error
            second_checkpoint = core.store.checkpoint("parent")
            assert second_checkpoint is not None
            new_root = str(second_checkpoint.payload["root_id"])
            assert new_root != original_root
            release.touch()
            _wait_for(
                lambda: (core.store.background_action(action_id) is not None) and core.store.background_action(action_id).status == "completed",
                what="old-root tool wait completion",
            )
            request = next(
                event
                for event in _events(core, "parent")
                if event.event_type == EventType.TOOL_DEFERRED and event.payload.get("background_action_id") == action_id
            )
            assert request.payload["root_id"] == original_root
            outcome = _plugin_state(core, "parent")["last_await"]
            assert outcome["status"] == "budget_exhausted"
            assert outcome["budget"] == {
                "dimension": "root_actions",
                "source": "root_action_budget",
                "limit": 1,
                "consumed": 1,
                "remaining": 0,
            }
            terminal = next(
                event
                for event in _events(core, "parent")
                if event.event_type == EventType.TOOL_DEFERRED_CANCELLED and event.payload.get("request_id") == request.payload["request_id"]
            )
            assert terminal.payload["status"] == "budget_exhausted"
            assert terminal.payload["budget_exhaustion"]["source"] == "root_action_budget"
            assert core.store.root_budget_state(new_root) is None


def test_hidden_stale_and_missing_wait_authority_are_refused(tmp_path: Path) -> None:
    with _server() as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("parent")
            visible = list(core.visible_tool_names_for_session("parent", core._snapshot))
            visible.remove("root_probe")
            core.store.enqueue_background_action(
                "hidden-tool-wait",
                PLUGIN_ID,
                "parent",
                core.revision_id,
                {"mode": "scope-probe"},
                root_id="wait-scope-root",
                root_action_budget=None,
                authority={
                    "visible_tools": visible,
                    "readonly": False,
                    "revision_id": core.revision_id,
                },
            )
            core._background.notify()
            _wait_for(
                lambda: core.store.background_action("hidden-tool-wait").status == "completed",
                what="hidden tool refusal",
            )
            hidden = _plugin_state(core, "parent")["last_await"]
            assert hidden["result_status"] == "error" and "outside" in hidden["error"]
            assert not any(
                event.event_type == EventType.TOOL_INTENT and event.payload.get("call", {}).get("tool_name") == "root_probe"
                for event in _events(core, "parent")
            )

            stale_action = _enqueue(core, "parent", mode="stale-probe")
            _wait_for(
                lambda: core.store.background_action(stale_action).status == "completed",
                what="stale revision refusal",
            )
            assert "expected_revision differs from the active revision" in _plugin_state(core, "parent")["last_refusal"]

            core.store.enqueue_background_action(
                "legacy-missing-authority",
                PLUGIN_ID,
                "parent",
                core.revision_id,
                {"mode": "missing-authority"},
                root_id="legacy-root",
                root_action_budget=None,
            )
            core._background.notify()
            _wait_for(
                lambda: core.store.background_action("legacy-missing-authority").status == "completed",
                what="legacy missing authority refusal",
            )
            assert "require captured origin tool, stance, and root authorization" in _plugin_state(core, "parent")["last_refusal"]


def test_in_flight_cancel_preserves_confirmed_stop_without_late_write(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server)
        target = workspace / "late.txt"
        target.write_text("unchanged\n", encoding="utf-8")
        started, release = workspace / "started", workspace / "release"
        with _open(tmp_path) as core:
            core.create_session("parent")
            action_id = _enqueue(
                core,
                "parent",
                mode="lifecycle-gate",
                path=str(target),
                started_path=str(started),
                release_path=str(release),
            )
            _wait_for(lambda: started.exists(), what="in-flight cancellable tool")
            assert core.cancel_background_action("parent", PLUGIN_ID, action_id, "test cancellation")
            _wait_for(
                lambda: (core.store.background_action(action_id) is not None) and core.store.background_action(action_id).status == "cancelled",
                what="cooperative tool cancellation",
            )
            assert target.read_text(encoding="utf-8") == "unchanged\n"
            interrupted = next(
                event for event in _events(core, "parent") if event.event_type == EventType.TOOL_INTERRUPTED and event.payload.get("deferred") is True
            )
            assert interrupted.payload["stop_cause"] == "owner_cancel"
            result = decode_tool_result(interrupted.payload["result"])
            assert isinstance(result, ToolFailure)
            assert isinstance(result.execution, ConfirmedStop)
            assert result.execution.execution_stopped is True
            assert result.execution.side_effect_state == "settled"


def test_activation_retires_live_tool_wait_before_new_revision(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server)
        target = workspace / "late.txt"
        target.write_text("unchanged\n", encoding="utf-8")
        started, release = workspace / "started", workspace / "release"
        with _open(tmp_path) as core:
            core.create_session("parent")
            core.create_session("activation")
            action_id = _enqueue(
                core,
                "parent",
                mode="lifecycle-gate",
                path=str(target),
                started_path=str(started),
                release_path=str(release),
            )
            _wait_for(lambda: started.exists(), what="in-flight tool entry")
            # Public activation remains blocked while this session/action is live.
            before_revision = core.revision_id
            outcome = core.invoke_command(
                "activation",
                "core.reload",
                {},
                expected_revision=core.command_catalog().revision_id,
            )
            assert outcome.status == "error"
            assert "blocked" in (outcome.error or "")
            assert core.revision_id == before_revision
            assert core.store.background_action(action_id).status == "claimed"
            assert target.read_text(encoding="utf-8") == "unchanged\n"
            # Separately exercise the safe-boundary park transition before retirement.
            core.park_background_drivers(reason="capability activation")
            assert core.store.background_action(action_id).status == "cancelled"
            assert target.read_text(encoding="utf-8") == "unchanged\n"
            terminals = [event for event in _events(core, "parent") if event.event_type == EventType.TOOL_INTERRUPTED]
            assert terminals and terminals[-1].payload.get("stop_cause") == "shutdown"
            assert not release.exists()


def test_close_disarms_live_approval_wait_without_late_write(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server, approval="ask")
        target = workspace / "target.txt"
        target.write_text("branch-token=ALPHA\n", encoding="utf-8")
        core = _open(tmp_path)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", mode="approval-edit", path="target.txt")
        _wait_for(lambda: any(event.event_type == EventType.APPROVAL_REQUESTED for event in _events(core, "parent")), what="pending close approval")
        core.close()
        assert target.read_text(encoding="utf-8") == "branch-token=ALPHA\n"
        reopened = _open(tmp_path)
        try:
            terminals = [event for event in _events(reopened, "parent") if event.event_type == EventType.TOOL_DEFERRED_CANCELLED]
            assert any(event.payload.get("status") == "interrupted" and event.payload.get("stop_cause") == "shutdown" for event in terminals)
            assert reopened.store.background_action(action_id).status in ("cancelled", "interrupted")
        finally:
            reopened.close()


def test_abrupt_driver_loss_does_not_replay_completed_edit_or_pending_edit(tmp_path: Path) -> None:
    with _server() as server:
        workspace = _configure(tmp_path, server, approval="ask")
        completed = workspace / "completed.txt"
        pending = workspace / "pending.txt"
        completed.write_text("branch-token=ALPHA\n", encoding="utf-8")
        pending.write_text("branch-token=ALPHA\n", encoding="utf-8")
        launched, waiting = workspace / "launched.json", workspace / "waiting.marker"
        child_code = """
import time
from pathlib import Path
from voidcode.bootstrap import open_core
from voidcode.core.events import EventType
workspace = Path(__import__('sys').argv[1])
state = Path(__import__('sys').argv[2])
core = open_core(workspace, state_dir=state)
core.create_session('parent')
enqueued = core.invoke_command(
    'parent',
    'wait_enqueue',
    {
        'mode': 'crash-edit',
        'completed_path': 'completed.txt',
        'pending_path': 'pending.txt',
    },
    expected_revision=core.revision_id,
)
if enqueued.status != 'ok':
    raise RuntimeError(f'could not enqueue crash test action: {enqueued.error}')
Path(__import__('sys').argv[3]).write_text('launched', encoding='utf-8')
while True:
    approvals = [
        event for event in core.store.events('parent')
        if event.event_type == EventType.APPROVAL_REQUESTED
    ]
    if approvals:
        break
    time.sleep(0.02)
core.resume(
    'parent',
    approval_request_id=str(approvals[0].payload['request_id']),
    decision='allow',
)
while True:
    approvals = [
        event for event in core.store.events('parent')
        if event.event_type == EventType.APPROVAL_REQUESTED
    ]
    if len(approvals) >= 2:
        Path(__import__('sys').argv[4]).touch()
        break
    time.sleep(0.02)
while True:
    time.sleep(0.1)
"""
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3] / "src")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, env.get("PYTHONPATH", "")]))
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(workspace),
                str(tmp_path / "state"),
                str(launched),
                str(waiting),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for(lambda: launched.exists() or process.poll() is not None, what="child action launch")
            assert launched.exists(), f"child exited {process.returncode}: {process.stderr.read().decode(errors='replace')}"
            _wait_for(
                lambda: waiting.exists() or process.poll() is not None,
                what="second edit approval",
            )
            assert waiting.exists(), f"child exited {process.returncode}: {process.stderr.read().decode(errors='replace')}"
            process.kill()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        reopened = _open(tmp_path)
        try:
            _wait_for(
                lambda: any(
                    event.event_type == EventType.TOOL_DEFERRED_CANCELLED and event.payload.get("status") == "core_lost"
                    for event in _events(reopened, "parent")
                ),
                what="lost-driver reconciliation",
            )
            assert completed.read_text(encoding="utf-8") == "branch-token=BRANCHED\n"
            assert pending.read_text(encoding="utf-8") == "branch-token=ALPHA\n"
            events = _events(reopened, "parent")
            read_intents = [
                event for event in events if event.event_type == EventType.TOOL_INTENT and event.payload.get("call", {}).get("tool_name") == "read"
            ]
            edit_intents = [
                event for event in events if event.event_type == EventType.TOOL_INTENT and event.payload.get("call", {}).get("tool_name") == "edit"
            ]
            edit_results = [
                event for event in events if event.event_type == EventType.TOOL_COMPLETED and event.payload.get("call", {}).get("tool_name") == "edit"
            ]
            assert len(read_intents) == 2
            assert len(edit_intents) == 1
            assert len(edit_results) == 1
        finally:
            reopened.close()
