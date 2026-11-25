"""Background driver: source/task loading, run settlement, failure lifetime, and notices."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, json_wire_value, own_json_value
from voidcode.sdk.services import BackgroundContext, PluginServices, ScheduledWorkServices, StateConflict, StateServices

from .constants import _NO_CONFIG, _STATE_CONFLICT_ATTEMPTS, _TERMINAL_RUN, INLINE_OUTPUT_BYTES, RunStatus
from .nodes import (
    WorkflowContext,
    _AbandonedNode,
    _cancel_run_children,
    _cancellation_note,
    _CancelledNode,
    _ParkedNode,
    _PausedRun,
    _settled_open_nodes,
)
from .notices import _notify_park, _notify_terminal
from .source import WorkflowScript, load_workflow_script, snapshot_workflow_source
from .state import WorkflowRun, _state_lock, _store


def drive_workflow_action(context: BackgroundContext, workflows: Mapping[str, str] = _NO_CONFIG) -> None:
    """Background entry: run one script to its next rest point, then persist honestly."""
    payload = json_wire_object(context.payload)
    run_id = payload.get("run_id")
    workflow = payload.get("workflow")
    kind = payload.get("kind")
    if kind != "workflow" or not isinstance(run_id, str) or not run_id or not isinstance(workflow, str) or not workflow:
        raise ValueError(f"workflows driver received a foreign action payload: {sorted(str(key) for key in payload)}")
    try:
        script = _load_script_for_action(context, run_id, workflow, workflows)
    except _LoadFailed as broken:
        raise RuntimeError(f"workflow run '{run_id}' failed: {broken}") from broken
    if script is None:
        return
    try:
        task = _task_for_action(context, run_id, workflow, script)
    except _TaskFailed as broken:
        raise RuntimeError(f"workflow run '{run_id}' failed: task is unreadable") from broken
    driver = WorkflowContext(
        invoke=context.invoke,
        run_id=run_id,
        task=task,
        session_id=context.session_id,
        activation=context.activation_requested,
        abort=context.abort_signal,
    )
    try:
        outcome = asyncio.run(script.entry(driver, task))
    except BaseExceptionGroup as group:  # noqa: BLE001 - TaskGroup packs node rests with real failures
        driver.stop_nodes()
        landed = _settle_group(context, run_id, group)
        if landed and _group_failed(group):
            raise RuntimeError(f"workflow run '{run_id}' failed; see run status for node errors") from group
        return
    except _ParkedNode as parked:
        driver.stop_nodes()
        _invoke_mark(context, run_id, "parked", f"node '{parked.key}' parked: {parked.status}")
        return
    except _PausedRun:
        driver.stop_nodes()
        _invoke_mark(context, run_id, "paused", None)
        return
    except _CancelledNode as cancelled:
        driver.stop_nodes()
        _cancel_children(context, context.session_id, run_id, "workflow cancellation")
        _invoke_mark(context, run_id, "cancelled", str(cancelled))
        return
    except InterruptedError:
        # The aborting command already committed pause/cancel; never overwrite it.
        driver.stop_nodes()
        return
    except Exception as failed:  # noqa: BLE001 - a script failure is a durable run failure, never a lost driver
        driver.stop_nodes()
        detail = f"{type(failed).__name__}: {failed}"
        note = _cancellation_note(_cancel_children(context, context.session_id, run_id, "workflow failure"))
        if _invoke_mark(context, run_id, "failed", detail + note):
            raise RuntimeError(f"workflow run '{run_id}' failed: {detail}") from failed
        return
    except BaseException as escaped:  # noqa: BLE001 - SystemExit/CancelledError must also settle the run row
        driver.stop_nodes()
        detail = f"{type(escaped).__name__}: {escaped}"
        note = _cancellation_note(_cancel_children(context, context.session_id, run_id, "workflow failure"))
        _invoke_mark(context, run_id, "failed", detail + note)
        raise
    finally:
        driver.close()
    if context.abort_signal.is_set():
        _cancel_children(context, context.session_id, run_id, "workflow cancellation")
        _invoke_mark(context, run_id, "cancelled", "owner cancellation")
        return
    try:
        result_text = _encode_result(outcome)
    except ValueError as broken:
        detail = f"workflow '{workflow}' returned a non-JSON result: {broken}"
        note = _cancellation_note(_cancel_children(context, context.session_id, run_id, "workflow failure"))
        if _invoke_mark(context, run_id, "failed", detail + note):
            raise RuntimeError(f"workflow run '{run_id}' failed: {detail}") from broken
        return
    try:
        completed = _invoke_state(context, context.session_id, _CompleteRun(run_id, result_text))
    except InterruptedError:
        return
    if completed is not None and completed[0] == "failed":
        raise RuntimeError(f"workflow run '{run_id}' failed: {completed[1]}")


def _invoke_state[T](context: BackgroundContext, session_id: str, callback: Any) -> T:
    """One workflow state commit under the shared session lock, retried on a lost CAS."""
    with _state_lock(session_id):
        for attempt in range(_STATE_CONFLICT_ATTEMPTS):
            try:
                return context.invoke(callback)
            except StateConflict:
                if attempt + 1 >= _STATE_CONFLICT_ATTEMPTS:
                    raise
        raise AssertionError("workflow state-conflict retry exhausted without a result")


def _cancel_children(context: BackgroundContext, session_id: str, run_id: str, reason: str) -> tuple[str, ...]:
    """Best-effort subtree cancellation for one run; returns failure notes."""

    def cancel(_context: ToolContext, services: PluginServices) -> tuple[str, ...]:
        run = _store(services).load()[0].runs.get(run_id)
        if run is None or run.status in _TERMINAL_RUN:
            return ()
        return _cancel_run_children(services, run, reason=reason)

    try:
        return _invoke_state(context, session_id, cancel)
    except Exception as exc:  # noqa: BLE001 - a cancellation failure must not lose the terminal mark
        return (f"{type(exc).__name__}: {exc}",)


def _invoke_mark(context: BackgroundContext, run_id: str, status: RunStatus, error: str | None) -> bool:
    """Mark the run; True when this call landed the mark, False when already terminal."""

    def mark(_context: ToolContext, services: PluginServices) -> bool:
        return _mark_run(services, run_id, status, error)

    try:
        return _invoke_state(context, context.session_id, mark)
    except InterruptedError:
        return False


def _encode_result(outcome: object) -> str:
    if isinstance(outcome, str):
        return outcome
    try:
        owned = own_json_value(outcome)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return json.dumps(json_wire_value(owned), sort_keys=True, separators=(",", ":"))


def _settle_group(context: BackgroundContext, run_id: str, group: BaseExceptionGroup) -> bool:
    """Classify a TaskGroup outcome: rests park or pause, anything else fails loudly."""
    rests, failures = _split_group(group)
    if failures:
        detail = "; ".join(f"{type(item).__name__}: {item}" for item in failures[:3])
        note = _cancellation_note(_cancel_children(context, context.session_id, run_id, "workflow failure"))
        return _invoke_mark(context, run_id, "failed", f"workflow nodes failed: {detail}{note}")
    kinds = {type(item) for item in rests}
    if _CancelledNode in kinds or InterruptedError in kinds:
        note = _cancellation_note(_cancel_children(context, context.session_id, run_id, "workflow cancellation"))
        return _invoke_mark(context, run_id, "cancelled", ("; ".join(str(item) for item in rests[:3]) or "owner cancellation") + note)
    elif _PausedRun in kinds:
        return _invoke_mark(context, run_id, "paused", None)
    elif rests:
        parked = next((item for item in rests if isinstance(item, _ParkedNode)), None)
        if parked is None:
            return False
        return _invoke_mark(context, run_id, "parked", f"node '{parked.key}' parked: {parked.status}")
    return False


def _group_failed(group: BaseExceptionGroup) -> bool:
    _rests, failures = _split_group(group)
    return bool(failures)


def _split_group(group: BaseExceptionGroup) -> tuple[list[BaseException], list[BaseException]]:
    rests: list[BaseException] = []
    failures: list[BaseException] = []
    for item in group.exceptions:
        if isinstance(item, BaseExceptionGroup):
            nested_rests, nested_failures = _split_group(item)
            rests.extend(nested_rests)
            failures.extend(nested_failures)
        elif isinstance(item, (_ParkedNode, _PausedRun, _CancelledNode, _AbandonedNode, InterruptedError)):
            rests.append(item)
        else:
            failures.append(item)
    return rests, failures


@dataclass(frozen=True, slots=True)
class _CompleteRun:
    run_id: str
    result_text: str

    def __call__(self, context: ToolContext, services: PluginServices) -> tuple[RunStatus, str | None] | None:
        _ = context
        state, version = _store(services).load()
        run = state.runs.get(self.run_id)
        if run is None or run.status in _TERMINAL_RUN:
            return None
        failed = [node.key for node in run.nodes if node.status == "failed"]
        if failed:
            note = _cancellation_note(_cancel_run_children(services, run, reason="workflow failure"))
            error = f"nodes failed: {', '.join(sorted(failed))}{note}"
            finished = _settled_open_nodes(run, "failed").with_status("failed", error)
            _store(services).save(state.with_run(finished), version)
            _notify_terminal(services, finished)
            return ("failed", error)
        pending = [node.key for node in run.nodes if node.status in ("pending", "running", "waiting", "interrupted")]
        if pending:
            error = f"nodes still open: {', '.join(sorted(pending))}"
            parked = run.with_status("parked", error)
            _store(services).save(state.with_run(parked), version)
            _notify_park(services, parked)
            return ("parked", error)
        cancelled = [node.key for node in run.nodes if node.status == "cancelled"]
        if cancelled:
            note = _cancellation_note(_cancel_run_children(services, run, reason="workflow cancellation"))
            error = f"nodes cancelled: {', '.join(sorted(cancelled))}{note}"
            finished = _settled_open_nodes(run, "cancelled").with_status("cancelled", error)
            _store(services).save(state.with_run(finished), version)
            _notify_terminal(services, finished)
            return ("cancelled", error)
        raw = self.result_text.encode("utf-8")
        if len(raw) > INLINE_OUTPUT_BYTES:
            reference = services.spill_artifact(self.result_text, media_type="text/plain; charset=utf-8")
            run = replace(run, result=None, result_sha256=reference.sha256, result_bytes=reference.bytes)
        else:
            run = replace(run, result=self.result_text, result_sha256=None, result_bytes=len(raw))
        finished = run.with_status("completed", None)
        _store(services).save(state.with_run(finished), version)
        _notify_terminal(services, finished)
        return ("completed", None)


class _LoadFailed(Exception):
    """The loader already marked the run; the driver must stop with a failed action."""


def _load_script_for_action(context: BackgroundContext, run_id: str, workflow: str, workflows: Mapping[str, str]) -> WorkflowScript | None:
    def describe(tool_context: ToolContext, services: PluginServices) -> tuple[Mapping[str, str], Path, str, str, str]:
        return _describe_source(tool_context, services, run_id, workflow, workflows)

    def park_unreadable(_context: ToolContext, services: PluginServices) -> None:
        _mark_run(services, run_id, "parked", "workflow state is unreadable")

    try:
        loaded = context.invoke(describe)
    except Exception:  # noqa: BLE001 - state failures park visibly, never silently
        _invoke_state(context, context.session_id, park_unreadable)
        return None
    workflows, workspace, expected_path, expected_digest, live = loaded
    if live != expected_digest:
        message = f"workflow '{workflow}' source changed; refusing to resume a different script"

        def mark_changed(_context: ToolContext, services: PluginServices) -> None:
            _mark_run(services, run_id, "failed", message)

        _invoke_state(context, context.session_id, mark_changed)
        raise _LoadFailed(message)
    try:
        source = context.invoke(_ReadSource(expected_digest))
    except (ValueError, RuntimeError, KeyError) as broken:
        detail = f"workflow '{workflow}' recorded source is unreadable: {broken}"

        def mark_broken(_context: ToolContext, services: PluginServices) -> None:
            _mark_run(services, run_id, "failed", detail)

        _invoke_state(context, context.session_id, mark_broken)
        raise _LoadFailed(detail) from broken
    if hashlib.sha256(source).hexdigest() != expected_digest:
        message = f"workflow '{workflow}' recorded source failed its digest check"

        def mark_tampered(_context: ToolContext, services: PluginServices) -> None:
            _mark_run(services, run_id, "failed", message)

        _invoke_state(context, context.session_id, mark_tampered)
        raise _LoadFailed(message)
    try:
        return load_workflow_script(workflow, expected_path, source)
    except ValueError as broken:
        detail = str(broken)

        def mark_invalid(_context: ToolContext, services: PluginServices) -> None:
            _mark_run(services, run_id, "failed", detail)

        _invoke_state(context, context.session_id, mark_invalid)
        raise _LoadFailed(detail) from broken


class _TaskFailed(Exception):
    """The task read already marked the run failed; the driver must stop, not run."""


def _task_for_action(context: BackgroundContext, run_id: str, workflow: str, script: WorkflowScript) -> object:
    def describe(_context: ToolContext, services: PluginServices) -> object:
        return _describe_task(services, run_id, workflow, script)

    try:
        return context.invoke(describe)
    except ValueError as broken:
        detail = str(broken)

        def mark_broken(_context: ToolContext, services: PluginServices) -> None:
            _mark_run(services, run_id, "failed", detail)

        _invoke_state(context, context.session_id, mark_broken)
        raise _TaskFailed from broken


@dataclass(frozen=True, slots=True)
class _ReadSource:
    sha256: str

    def __call__(self, context: ToolContext, services: PluginServices) -> bytes:
        _ = context
        chunks: list[bytes] = []
        offset = 0
        while True:
            page = services.read_artifact(self.sha256, offset=offset, limit=65536)
            if not page.data and not page.eof:
                raise ValueError(f"artifact {self.sha256} read made no progress at offset {offset}")
            chunks.append(bytes(page.data))
            if page.eof:
                break
            offset += len(page.data)
        return b"".join(chunks)


def _describe_source(
    context: ToolContext, services: StateServices, run_id: str, workflow: str, workflows: Mapping[str, str]
) -> tuple[Mapping[str, str], Path, str, str, str]:
    workspace = context.require_workspace()
    run = _store(services).load()[0].runs.get(run_id)
    if run is None or run.workflow != workflow:
        raise ValueError(f"unknown workflow run: {run_id!r}")
    try:
        _relative, _source, live = snapshot_workflow_source(workflows, workspace, workflow)
    except ValueError as exc:
        raise ValueError(f"workflow '{workflow}' configured source is unreadable: {exc}") from exc
    return workflows, workspace, run.source_path, run.source_sha256, live


def _describe_task(services: StateServices, run_id: str, workflow: str, script: WorkflowScript) -> object:
    run = _store(services).load()[0].runs.get(run_id)
    if run is None or run.workflow != workflow:
        raise ValueError(f"unknown workflow run: {run_id!r}")
    if run.source_sha256 != script.source_sha256:
        raise ValueError(f"workflow '{workflow}' source changed; refusing to resume a different script")
    return json_wire_value(run.task)


def _mark_run(services: PluginServices, run_id: str, status: RunStatus, error: str | None) -> bool:
    state, version = _store(services).load()
    run = state.runs.get(run_id)
    if run is None or run.status in _TERMINAL_RUN:
        return False
    finished = _settled_open_nodes(run, status).with_status(status, error)
    _store(services).save(state.with_run(finished), version)
    _notify_terminal(services, finished)
    _notify_park(services, finished)
    return True


def _refuse_live_driver(services: ScheduledWorkServices, run: WorkflowRun) -> str | None:
    """Refuse resume only when the recorded driver is genuinely still driving.

    A close/crash can leave the run row `running` while its action already
    settled; that stale row must allow an explicit resume, never a second
    driver alongside a live one.
    """
    if run.action_id is None:
        return f"workflow run '{run.run_id}' is already running"
    try:
        action = services.background_action_status(run.action_id)
    except (ValueError, RuntimeError) as exc:
        return f"workflow run '{run.run_id}' driver state is unreadable: {exc}"
    if action is None:
        return f"workflow run '{run.run_id}' names an unknown driver action '{run.action_id}'"
    if action.status in ("pending", "running"):
        return f"workflow run '{run.run_id}' is already running"
    if action.status == "failed":
        return f"workflow run '{run.run_id}' driver failed: {action.error or 'no error recorded'}"
    return None
