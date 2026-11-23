"""Lifecycle hook outcomes are durable, bounded, and guarded once per run."""

from __future__ import annotations

from pathlib import Path

from voidcode.core.engine.governance import ToolGovernance
from voidcode.core.events import EventType
from voidcode.core.execution import _Cursor, _Execution
from voidcode.core.storage.store import EventDraft, SessionRecord, SessionStatus, StoredEvent


class _Runtime:
    lifecycle_hooks = (("fixture", "session_start"), ("fixture", "session_end"))

    def __init__(self, *, note: str = "hook note", error: str | None = None) -> None:
        self.note = note
        self.error = error
        self.calls: list[dict[str, object]] = []

    def run_lifecycle(self, owner, kind, **kwargs):
        self.calls.append({"owner": owner, "kind": kind, **kwargs})
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.note


class _Snapshot:
    def __init__(self, runtime: _Runtime) -> None:
        self.plugin_runtime = runtime


class _Host:
    policy_read_only = False
    approval_mode = "yolo"

    def __init__(self) -> None:
        self.committed: list[StoredEvent] = []

    def commit_hook_events(self, execution: _Execution, events: tuple[EventDraft, ...]) -> None:
        for draft in events:
            stored = StoredEvent(
                execution.session.session_id,
                len(execution.events) + 1,
                execution.run_id,
                draft.event_type,
                draft.source,
                draft.actor,
                execution.session.active_revision_id,
                draft.payload,
            )
            execution.events.append(stored)
            self.committed.append(stored)


def _execution(tmp_path: Path) -> _Execution:
    session = SessionRecord("sess-hook", str(tmp_path), SessionStatus.RUNNING, "rev", "run-1", 1, 0)
    return _Execution(session, _Cursor(logical_turn="turn"), [], "run-1")


def test_session_start_note_is_durable_and_not_refired_for_the_same_run(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    host = _Host()
    runtime = _Runtime()
    governance = ToolGovernance(host)

    assert governance.lifecycle_events(execution, _Snapshot(runtime), "session_start") == ()
    assert len(runtime.calls) == 1
    assert len(host.committed) == 1
    event = host.committed[0]
    assert event.event_type == EventType.HOOK_INVOKED
    assert event.run_id == "run-1"
    assert event.payload == {
        "kind": "session_start",
        "owner": "fixture",
        "logical_turn": "turn",
        "status": "",
        "outcome": "noted",
        "note": "hook note",
        "note_truncated": False,
    }

    # A same-run re-entry sees the durable record and never asks the owner again.
    governance.lifecycle_events(execution, _Snapshot(runtime), "session_start")
    assert len(runtime.calls) == 1
    assert len(host.committed) == 1


def test_session_end_drafts_ride_the_terminal_commit_and_failures_are_bounded(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    host = _Host()
    runtime = _Runtime()
    governance = ToolGovernance(host)

    drafts = governance.lifecycle_events(
        execution,
        _Snapshot(runtime),
        "session_end",
        status="interrupted",
        error="user stopped",
    )
    assert runtime.calls[0]["status"] == "interrupted"
    assert runtime.calls[0]["error"] == "user stopped"
    assert len(drafts) == 1 and not host.committed
    assert drafts[0].payload["status"] == "interrupted"
    assert drafts[0].payload["outcome"] == "noted"

    failing = ToolGovernance(host)
    runtime.error = "x" * 10_000
    failures = failing.lifecycle_events(execution, _Snapshot(runtime), "session_end", status="failed", error="provider failed")
    assert len(failures) == 1
    payload = failures[0].payload
    assert payload["outcome"] == "failed"
    assert payload["status"] == "failed"
    assert payload["failure"].startswith("RuntimeError: ")
    assert payload["failure_truncated"] is True
    assert len(payload["failure"].encode("utf-8")) <= 8192 + len("\n[truncated]")
