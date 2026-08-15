"""Lineage-guarded transcript reads via ``voidcode://transcript/<session_id>``.

Covers:
(a) a leader session reads its child session's transcript through the URI,
(b) a child session reads its own transcript,
(c) an unrelated session (or an unknown session id) is rejected with a
    tool-level error,
(d) the returned transcript is bounded (limit clamp 1..100, default 20) and
    payload-stripped (per event only ``sequence``, ``event_type``, ``source``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import (
    RuntimeRequest,
    RuntimeResponse,
)
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.task import BackgroundTaskRef, BackgroundTaskRequestSnapshot, BackgroundTaskState
from voidcode.tools import ReadTool, ToolCall
from voidcode.tools.submit_result import SubmitResultTool


@dataclass(slots=True)
class _Step:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[Any, ...] = ()
    is_finished: bool = False


class _FinishGraph:
    """Runs a session to completion without any tool calls."""

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> _Step:
        _ = request, tool_results, session
        return _Step(output="done", is_finished=True)


class _ChildFinishGraph:
    """Runs a delegated child session, ending with submit_result."""

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> _Step:
        _ = request, session
        if not tool_results:
            return _Step(
                tool_call=ToolCall(tool_name="submit_result", arguments={"summary": "child done"}),
                output=None,
                is_finished=False,
            )
        return _Step(output="completed", is_finished=True)


class _TranscriptReadGraph:
    """Issues one read of a transcript URI, then finishes."""

    def __init__(self, path: str, limit: int | None = None) -> None:
        self._path = path
        self._limit = limit
        self._done = False

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> _Step:
        _ = request, tool_results, session
        if not self._done:
            self._done = True
            arguments: dict[str, object] = {"path": self._path}
            if self._limit is not None:
                arguments["limit"] = self._limit
            return _Step(
                tool_call=ToolCall(tool_name="read", arguments=arguments),
                output=None,
                is_finished=False,
            )
        return _Step(output="completed", is_finished=True)


class _ChildReadGraph:
    """Reads a transcript URI from a child session, then submits the result."""

    def __init__(self, path: str, limit: int | None = None) -> None:
        self._path = path
        self._limit = limit
        self._read_issued = False

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> _Step:
        _ = request, tool_results, session
        if not self._read_issued:
            self._read_issued = True
            arguments: dict[str, object] = {"path": self._path}
            if self._limit is not None:
                arguments["limit"] = self._limit
            return _Step(
                tool_call=ToolCall(tool_name="read", arguments=arguments),
                output=None,
                is_finished=False,
            )
        return _Step(
            tool_call=ToolCall(tool_name="submit_result", arguments={"summary": "read done"}),
            output=None,
            is_finished=False,
        )


def _runtime(workspace: Path, graph: Any, *, tools: tuple[Any, ...] = (ReadTool(),)) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=workspace,
        tool_registry=ToolRegistry.from_tools(list(tools)),
        graph=graph,
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
        ),
    )


def _run(
    runtime: VoidCodeRuntime,
    *,
    session_id: str,
    parent_session_id: str | None = None,
) -> None:
    _ = list(
        runtime.run_stream(
            RuntimeRequest(
                prompt="go",
                session_id=session_id,
                parent_session_id=parent_session_id,
            )
        )
    )


def _collect_events(runtime: VoidCodeRuntime, *, session_id: str) -> list[Any]:
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))
    return [chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None]


def _read_completed_payload(events: list[Any]) -> dict[str, object]:
    completed = [event.payload for event in events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "read"]
    assert len(completed) == 1, "expected exactly one read completion"
    return completed[0]


def _seed_sessions(tmp_path: Path) -> VoidCodeRuntime:
    seed = _runtime(tmp_path, _FinishGraph())
    _run(seed, session_id="leader")
    child_seed = _runtime(tmp_path, _ChildFinishGraph(), tools=(ReadTool(), SubmitResultTool()))
    _run(child_seed, session_id="child", parent_session_id="leader")
    # A second child run adds more persisted events so bounded reads truncate.
    _run(child_seed, session_id="child", parent_session_id="leader")
    return seed


# ---------------------------------------------------------------------------
# (a) leader reads its child's transcript
# ---------------------------------------------------------------------------


def test_leader_reads_child_transcript_via_uri(tmp_path: Path) -> None:
    _seed_sessions(tmp_path)

    reader = _runtime(tmp_path, _TranscriptReadGraph("voidcode://transcript/child", limit=5))
    events = _collect_events(reader, session_id="leader")
    payload = _read_completed_payload(events)

    assert payload["status"] == "ok"
    assert payload["type"] == "transcript"
    assert payload["session_id"] == "child"
    assert payload["message_limit"] == 5
    assert payload["transcript_count"] == 5
    assert payload["transcript_truncated"] is True
    assert len(payload["transcript"]) == 5
    for event in payload["transcript"]:
        assert set(event) == {"sequence", "event_type", "source"}
        assert "payload" not in event


# ---------------------------------------------------------------------------
# (b) child reads its own transcript
# ---------------------------------------------------------------------------


def test_child_reads_own_transcript_via_uri(tmp_path: Path) -> None:
    _seed_sessions(tmp_path)

    reader = _runtime(
        tmp_path,
        _ChildReadGraph("voidcode://transcript/child", limit=100),
        tools=(ReadTool(), SubmitResultTool()),
    )
    events = _collect_events(reader, session_id="child")
    payload = _read_completed_payload(events)

    assert payload["status"] == "ok"
    assert payload["session_id"] == "child"
    assert payload["transcript_truncated"] is False
    assert payload["transcript_count"] == len(payload["transcript"])
    assert payload["transcript"], "expected the child's own transcript events"
    for event in payload["transcript"]:
        assert set(event) == {"sequence", "event_type", "source"}


# ---------------------------------------------------------------------------
# (c) lineage isolation
# ---------------------------------------------------------------------------


def test_unrelated_session_cannot_read_foreign_child_transcript(tmp_path: Path) -> None:
    seed = _seed_sessions(tmp_path)
    _run(seed, session_id="unrelated")

    reader = _runtime(tmp_path, _TranscriptReadGraph("voidcode://transcript/child"))
    with pytest.raises(ValueError, match="transcript not accessible for session: child"):
        _run(reader, session_id="unrelated")


def test_transcript_uri_rejects_unknown_session(tmp_path: Path) -> None:
    _seed_sessions(tmp_path)

    reader = _runtime(tmp_path, _TranscriptReadGraph("voidcode://transcript/ghost-session"))
    with pytest.raises(ValueError, match="transcript not accessible for session: ghost-session"):
        _run(reader, session_id="leader")


# ---------------------------------------------------------------------------
# (d) bounded + payload-stripped (also asserted in the leader test above)
# ---------------------------------------------------------------------------


def test_transcript_uri_limit_is_clamped_to_100(tmp_path: Path) -> None:
    _seed_sessions(tmp_path)

    reader = _runtime(tmp_path, _TranscriptReadGraph("voidcode://transcript/child", limit=5000))
    events = _collect_events(reader, session_id="leader")
    payload = _read_completed_payload(events)

    # The requested limit is clamped to 100; with fewer persisted events the
    # transcript is not truncated.
    assert payload["message_limit"] == 100
    assert payload["transcript_truncated"] is False
    assert payload["transcript_count"] == len(payload["transcript"])
    assert 0 < len(payload["transcript"]) <= 100


# ---------------------------------------------------------------------------
# background-task parent/child linkage fallback
# ---------------------------------------------------------------------------


def test_transcript_uri_uses_background_task_lineage_fallback(tmp_path: Path) -> None:
    """A session without persisted parent linkage is readable via the task row."""
    seed = _runtime(tmp_path, _FinishGraph())
    _run(seed, session_id="leader")

    store = seed._session_store  # noqa: SLF001
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="delegate", session_id="child-task-linked"),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="child-task-linked"),
                status="completed",
                turn=1,
                metadata={"workspace": str(tmp_path)},
            ),
            events=(),
            output="done",
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-child-linked"),
            request=BackgroundTaskRequestSnapshot(prompt="delegate", parent_session_id="leader"),
            session_id="child-task-linked",
        ),
    )

    reader = _runtime(tmp_path, _TranscriptReadGraph("voidcode://transcript/child-task-linked"))
    events = _collect_events(reader, session_id="leader")
    payload = _read_completed_payload(events)

    assert payload["status"] == "ok"
    assert payload["session_id"] == "child-task-linked"
    assert payload["transcript"] == []


def test_transcript_uri_background_task_lineage_rejects_unrelated_session(tmp_path: Path) -> None:
    seed = _runtime(tmp_path, _FinishGraph())
    _run(seed, session_id="leader")
    _run(seed, session_id="stranger")

    store = seed._session_store  # noqa: SLF001
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="delegate", session_id="child-task-linked-2"),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="child-task-linked-2"),
                status="completed",
                turn=1,
                metadata={"workspace": str(tmp_path)},
            ),
            events=(),
            output="done",
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-child-linked-2"),
            request=BackgroundTaskRequestSnapshot(prompt="delegate", parent_session_id="leader"),
            session_id="child-task-linked-2",
        ),
    )

    reader = _runtime(tmp_path, _TranscriptReadGraph("voidcode://transcript/child-task-linked-2"))
    with pytest.raises(ValueError, match="transcript not accessible for session: child-task-linked-2"):
        _run(reader, session_id="stranger")
