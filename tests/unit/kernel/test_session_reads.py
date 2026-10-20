"""S5 acceptance: one session's own facts, and its chain's child events, read through the SDK."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.s5_plugin import PLUGIN_ID, PROBE_TOOL, WRITE_TOOL, write_s5_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel
from voidcode.sdk.json_values import json_wire_object


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "session read reasoning", "tool_calls": list(calls)},
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


_CREATED: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_kernels() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _open(tmp_path: Path) -> Kernel:
    kernel = open_kernel(tmp_path / "workspace", state_dir=tmp_path / "state", trusted_plugins=frozenset({PLUGIN_ID}))
    _CREATED.append(kernel)
    return kernel


def _results(kernel: Kernel, session_id: str, tool_name: str) -> list[Mapping[str, object]]:
    """Every committed result one tool produced in this session, in commit order."""
    results: list[Mapping[str, object]] = []
    for event in kernel.events_after(session_id):
        if event.event_type != "kernel.tool_completed":
            continue
        call = event.payload.get("call")
        result = event.payload.get("result")
        if isinstance(call, Mapping) and call.get("tool_name") == tool_name and isinstance(result, Mapping):
            results.append(result)
    return results


def _body(kernel: Kernel, session_id: str, tool_name: str, index: int = 0) -> dict[str, Any]:
    """The JSON body of one committed result; every fixture action carries one."""
    result = _results(kernel, session_id, tool_name)[index]
    body = result.get("body")
    assert isinstance(body, Mapping), result
    return json_wire_object(body)


def _text(kernel: Kernel, session_id: str, tool_name: str, index: int = 0) -> str:
    """The model-visible text of one committed result."""
    result = _results(kernel, session_id, tool_name)[index]
    output = result.get("output")
    assert isinstance(output, Mapping) and isinstance(output.get("text"), str), result
    return str(output["text"])


def test_session_info_reports_one_running_session_and_its_chain_budget(tmp_path: Path) -> None:
    source = write_s5_plugin(tmp_path / "plugin")
    replies = [
        # The child reads its own facts first: a kernel-delegated session of this chain.
        _turn(_call("child-facts", PROBE_TOOL, action="session_info")),
        _turn(output="child facts read"),
        # The parent asks twice in one run, once through each lease.
        _turn(
            _call("facts-1", PROBE_TOOL, action="session_info"),
            _call("facts-2", WRITE_TOOL, action="session_info"),
        ),
        _turn(output="parent facts read"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess")
        child = kernel.spawn_child("sess", "read your own facts", tool_allowlist=(PROBE_TOOL,), max_steps=11)
        assert kernel.join_child("sess", child, timeout_seconds=30).status == "completed"
        assert kernel.run("read the session facts", session_id="sess", max_steps=16).status == "completed"

        facts = _body(kernel, "sess", PROBE_TOOL)["session"]
        writable = _body(kernel, "sess", WRITE_TOOL)["session"]
        record = kernel.session("sess")
        assert facts["session_id"] == "sess"
        assert facts["status"] == "running"
        assert isinstance(facts["run_id"], str) and facts["run_id"]
        assert facts["revision_id"] == record.active_revision_id
        assert facts["parent_session_id"] is None
        assert facts["root_session_id"] == "sess"
        assert facts["depth"] == 0
        assert facts["turn"] >= 1
        assert facts["last_event_sequence"] >= 1
        assert facts["tool_allowlist"] is None
        assert facts["pending_approval"] is None
        # The chain budget is the run's own checkpoint action budget, never the root session id.
        assert facts["chain_budget"] == 16
        assert type(facts["chain_budget"]) is int
        assert facts["chain_budget"] != facts["root_session_id"]
        assert 0 <= facts["chain_reserved"] < facts["chain_budget"]
        assert facts["budget"] is None
        # A writable lease answers from the same row of the same run.
        assert writable["session_id"] == facts["session_id"]
        assert writable["run_id"] == facts["run_id"]
        assert writable["revision_id"] == facts["revision_id"]
        assert writable["turn"] == facts["turn"]
        assert writable["chain_budget"] == facts["chain_budget"]

        child_facts = _body(kernel, child, PROBE_TOOL)["session"]
        assert child_facts["session_id"] == child
        assert child_facts["status"] == "running"
        assert isinstance(child_facts["run_id"], str) and child_facts["run_id"] != facts["run_id"]
        assert child_facts["parent_session_id"] == "sess"
        assert child_facts["root_session_id"] == "sess"
        assert child_facts["depth"] == 1
        assert child_facts["tool_allowlist"] == [PROBE_TOOL]
        assert child_facts["pending_approval"] is None
        assert child_facts["chain_budget"] == 11
        assert type(child_facts["chain_budget"]) is int
        assert child_facts["chain_budget"] != child_facts["root_session_id"]


def test_child_event_reads_are_bounded_cursor_scoped_and_descendant_checked(tmp_path: Path) -> None:
    source = write_s5_plugin(tmp_path / "plugin")
    big_prompt = "x" * 100_000
    replies: list[dict[str, object]] = []
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess")
        kernel.create_session("unrelated-root")

        # This chain's child: naming its parent (a root, so never its descendant) is the kernel's refusal.
        replies.append(_turn(_call("child-read", PROBE_TOOL, action="child_events", child_session_id="sess")))
        replies.append(_turn(output="child done"))
        child = kernel.spawn_child("sess", big_prompt, tool_allowlist=(PROBE_TOOL,))
        assert kernel.join_child("sess", child, timeout_seconds=30).status == "completed"

        # A grandchild: still the root's own descendant, and the root may read it.
        replies.append(_turn(output="grandchild done"))
        grandchild = kernel.spawn_child(child, "grandchild work", tool_allowlist=(PROBE_TOOL,))
        assert kernel.join_child(child, grandchild, timeout_seconds=30).status == "completed"

        # Another chain's child: a real child of a real parent, and still not this session's to read.
        replies.append(_turn(output="other chain child done"))
        stranger = kernel.spawn_child("unrelated-root", "other chain work", tool_allowlist=(PROBE_TOOL,))
        assert kernel.join_child("unrelated-root", stranger, timeout_seconds=30).status == "completed"
        assert kernel.session(stranger).parent_session_id == "unrelated-root"

        replies.append(
            _turn(
                _call("read-page", PROBE_TOOL, action="child_events", child_session_id=child, after_sequence=0, limit=2, include_payload=False),
                _call("read-bounded", PROBE_TOOL, action="child_events", child_session_id=child, after_sequence=0, limit=100),
                _call("read-rest", PROBE_TOOL, action="child_events", child_session_id=child, after_sequence=2, limit=100),
                _call("read-grandchild", PROBE_TOOL, action="child_events", child_session_id=grandchild, after_sequence=0, limit=100),
                _call("read-self", PROBE_TOOL, action="child_events", child_session_id="sess"),
                _call("read-root", PROBE_TOOL, action="child_events", child_session_id="unrelated-root"),
                _call("read-stranger", PROBE_TOOL, action="child_events", child_session_id=stranger),
            )
        )
        replies.append(_turn(output="parent read the child"))
        assert kernel.run("read the child's events", session_id="sess").status == "completed"

        page, bounded, rest, grandchild_read, self_read, root_read, stranger_read = [_body(kernel, "sess", PROBE_TOOL, index) for index in range(7)]

        # One bounded page: the count is the caller's limit, and no payload is carried.
        assert page["child_session_id"] == child
        assert page["after_sequence"] == 0 and page["limit"] == 2 and page["include_payload"] is False
        assert [view["sequence"] for view in page["views"]] == [1, 2]
        assert [view["payload"] for view in page["views"]] == [None, None]
        assert all(view["event_type"] for view in page["views"])

        # The page's payload budget: only the child's own oversized prompt is replaced by a summary,
        # and the events that follow it still carry theirs (the budget is shared, not per event).
        history = bounded["views"]
        assert [view["sequence"] for view in history] == list(range(1, len(history) + 1))
        assert len(history) == kernel.session(child).last_event_sequence
        assert history[0]["event_type"] == "kernel.user_message"
        assert history[0]["payload_truncated"] is True
        assert history[0]["payload"]["truncated"] is True
        assert history[0]["payload"]["original_bytes"] > 65536
        assert not any(view["payload_truncated"] for view in history[1:])
        assert any(view["event_type"] == "kernel.tool_completed" and "own descendants" in json.dumps(view["payload"]) for view in history), history

        # The cursor continues exactly where the first page stopped.
        sequences = [view["sequence"] for view in rest["views"]]
        assert sequences == list(range(3, 3 + len(sequences)))
        assert sequences[-1] == kernel.session(child).last_event_sequence

        # The child's own read of its parent (a root, so never its descendant) is the kernel's refusal.
        child_read = _body(kernel, child, PROBE_TOOL)
        assert "views" not in child_read
        assert "own descendants" in child_read["refused"]

        # The root reads its grandchild's own committed events.
        assert grandchild_read["child_session_id"] == grandchild
        assert grandchild_read["views"], grandchild_read

        # The kernel owns the lineage: this session, an unrelated root, and another chain's child.
        for refused in (self_read, root_read, stranger_read):
            assert "views" not in refused, refused
        assert "must name another session" in self_read["refused"]
        assert "own descendants" in root_read["refused"]
        assert "own descendants" in stranger_read["refused"]


def test_tool_actions_carry_json_bodies_and_failures_stay_failures(tmp_path: Path) -> None:
    source = write_s5_plugin(tmp_path / "plugin")
    replies = [
        _turn(
            _call("echo-probe", PROBE_TOOL, action="echo", text="probe echo"),
            _call("echo-write", WRITE_TOOL, action="echo", text="write echo"),
            _call("fail-probe", PROBE_TOOL, action="fail", text="fixture said no"),
        ),
        _turn(output="surface exercised"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess")
        assert kernel.run("exercise the fixture surface", session_id="sess").status == "completed"

        # An action's JSON is its body and its model-visible text; both tools dispatch the same way.
        assert _body(kernel, "sess", PROBE_TOOL, 0) == {"echo": "probe echo"}
        assert _text(kernel, "sess", PROBE_TOOL, 0) == json.dumps({"echo": "probe echo"}, sort_keys=True)
        assert _body(kernel, "sess", WRITE_TOOL, 0) == {"echo": "write echo"}

        failed = _results(kernel, "sess", PROBE_TOOL)[1]
        assert failed["status"] == "error"
        assert failed["error"] == "fixture said no"


def test_post_tool_hook_reads_the_session_row_under_a_hook_lease(tmp_path: Path) -> None:
    source = write_s5_plugin(tmp_path / "plugin", hooks=True)
    replies = [
        _turn(_call("echo-hooked", PROBE_TOOL, action="echo", text="hooked")),
        _turn(output="hook observed"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess")
        assert kernel.run("let the hook read the session", session_id="sess", max_steps=16).status == "completed"

        # The hook's note is appended to the settled result: a hook lease reads the session row.
        result_text, note_text = _text(kernel, "sess", PROBE_TOOL, 0).split("\n\n", 1)
        assert json.loads(result_text) == {"echo": "hooked"}
        note = json.loads(note_text)
        assert note["hook"] == "post_tool"
        assert note["tool"] == PROBE_TOOL
        assert note["session_id"] == "sess"
        assert note["status"] == "running"
        assert note["pending_approval"] is False
        assert note["chain_budget"] == 16
