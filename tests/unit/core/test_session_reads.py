"""S5 acceptance: one session's own facts, and its chain's child events, read through the SDK."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from tests.fixtures.core_factory import register_core
from tests.fixtures.s5_plugin import PLUGIN_ID, PROBE_TOOL, WRITE_TOOL, write_s5_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.coverage import read_artifact_content
from voidcode.core.engine import Core
from voidcode.sdk.codec import decode_tool_result
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


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    register_core(core)
    return core


def _results(core: Core, session_id: str, tool_name: str) -> list[Mapping[str, object]]:
    """Every committed result one tool produced in this session, in commit order."""
    results: list[Mapping[str, object]] = []
    for event in core.events_after(session_id):
        if event.event_type != "core.tool_completed":
            continue
        call = event.payload.get("call")
        result = event.payload.get("result")
        if isinstance(call, Mapping) and call.get("tool_name") == tool_name and isinstance(result, Mapping):
            results.append(result)
    return results


def _body(core: Core, session_id: str, tool_name: str, index: int = 0) -> dict[str, Any]:
    """The JSON body of one committed result; every fixture action carries one."""
    result = _results(core, session_id, tool_name)[index]
    body = result.get("body")
    if body is None:
        # Oversized results commit body-less by design; the complete JSON waits at the reference.
        reference = decode_tool_result(result).result_reference
        assert reference is not None, result
        body = json.loads(read_artifact_content(reference, core._artifact_page))["body"]
    assert isinstance(body, Mapping), result
    return json_wire_object(body)


def _text(core: Core, session_id: str, tool_name: str, index: int = 0) -> str:
    """The model-visible text of one committed result."""
    result = _results(core, session_id, tool_name)[index]
    output = result.get("output")
    assert isinstance(output, Mapping) and isinstance(output.get("text"), str), result
    return str(output["text"])


def test_session_info_reports_one_running_session_and_its_chain_budget(tmp_path: Path) -> None:
    source = write_s5_plugin(tmp_path / "plugin")
    replies = [
        # The child reads its own facts first: a core-delegated session of this chain.
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
        core = _open(tmp_path)
        core.create_session("sess")
        child_root = uuid4().hex
        core.store.ensure_root_budget(child_root, 11)
        child = core.spawn_child(
            "sess",
            "read your own facts",
            tool_allowlist=(PROBE_TOOL,),
            max_steps=11,
            root_action_id=child_root,
            initial_root_budget=11,
        )
        assert core.join_child("sess", child, timeout_seconds=30).status == "completed"
        assert core.run("read the session facts", session_id="sess", max_steps=16, root_action_budget=16).status == "completed"

        facts = _body(core, "sess", PROBE_TOOL)["session"]
        writable = _body(core, "sess", WRITE_TOOL)["session"]
        record = core.session("sess")
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

        child_facts = _body(core, child, PROBE_TOOL)["session"]
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
        core = _open(tmp_path)
        core.create_session("sess")
        core.create_session("unrelated-root")

        # This chain's child: naming its parent (a root, so never its descendant) is the core's refusal.
        replies.append(_turn(_call("child-read", PROBE_TOOL, action="child_events", child_session_id="sess")))
        replies.append(_turn(output="child done"))
        child = core.spawn_child("sess", big_prompt, tool_allowlist=(PROBE_TOOL,))
        assert core.join_child("sess", child, timeout_seconds=30).status == "completed"

        # A grandchild: still the root's own descendant, and the root may read it.
        replies.append(_turn(output="grandchild done"))
        grandchild = core.spawn_child(child, "grandchild work", tool_allowlist=(PROBE_TOOL,))
        assert core.join_child(child, grandchild, timeout_seconds=30).status == "completed"

        # Another chain's child: a real child of a real parent, and still not this session's to read.
        replies.append(_turn(output="other chain child done"))
        stranger = core.spawn_child("unrelated-root", "other chain work", tool_allowlist=(PROBE_TOOL,))
        assert core.join_child("unrelated-root", stranger, timeout_seconds=30).status == "completed"
        assert core.session(stranger).parent_session_id == "unrelated-root"

        replies.append(
            _turn(
                _call("read-page", PROBE_TOOL, action="child_events", child_session_id=child, after_sequence=0, limit=2),
                _call("read-bounded", PROBE_TOOL, action="child_events", child_session_id=child, after_sequence=0, limit=100),
                _call("read-rest", PROBE_TOOL, action="child_events", child_session_id=child, after_sequence=2, limit=100),
                _call("read-grandchild", PROBE_TOOL, action="child_events", child_session_id=grandchild, after_sequence=0, limit=100),
                _call("read-self", PROBE_TOOL, action="child_events", child_session_id="sess"),
                _call("read-root", PROBE_TOOL, action="child_events", child_session_id="unrelated-root"),
                _call("read-stranger", PROBE_TOOL, action="child_events", child_session_id=stranger),
            )
        )
        replies.append(_turn(output="parent read the child"))
        assert core.run("read the child's events", session_id="sess").status == "completed"

        page, bounded, rest, grandchild_read, self_read, root_read, stranger_read = [_body(core, "sess", PROBE_TOOL, index) for index in range(7)]

        # One bounded page: the oversized first block splits mid-block, so the cursor
        # is the last fully consumed sequence (the skipped seq-1 reservation) with a
        # byte offset into the same block.
        assert page["child_session_id"] == child
        assert page["after_sequence"] == 0 and page["limit"] == 2
        assert page["page"]["next_after_sequence"] == 1
        assert page["page"]["next_offset"] > 0
        assert page["page"]["truncated"] is True
        # The full history starts with the same split block: the 100 KiB prompt fills one
        # 40 KiB page alone, byte-identical, with more bytes still owed.
        assert bounded["page"]["text"].startswith("## user [2]\n")
        assert "x" * 100 in bounded["page"]["text"]
        assert bounded["page"]["next_offset"] > 0
        assert bounded["page"]["truncated"] is True

        # The cursor continues exactly where the first page stopped.
        assert rest["page"]["next_after_sequence"] >= 2
        assert rest["page"]["text"] != page["page"]["text"]

        # The child's own read of its parent (a root, so never its descendant) is the core's refusal.
        child_read = _body(core, child, PROBE_TOOL)
        assert "page" not in child_read
        assert "own descendants" in child_read["refused"]

        # The root reads its grandchild's own committed history.
        assert grandchild_read["child_session_id"] == grandchild
        assert grandchild_read["page"]["text"], grandchild_read

        # The core owns the lineage: this session, an unrelated root, and another chain's child.
        for refused in (self_read, root_read, stranger_read):
            assert "page" not in refused, refused
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
        core = _open(tmp_path)
        core.create_session("sess")
        assert core.run("exercise the fixture surface", session_id="sess").status == "completed"

        # An action's JSON is its body and its model-visible text; both tools dispatch the same way.
        assert _body(core, "sess", PROBE_TOOL, 0) == {"echo": "probe echo"}
        assert _text(core, "sess", PROBE_TOOL, 0) == json.dumps({"echo": "probe echo"}, sort_keys=True)
        assert _body(core, "sess", WRITE_TOOL, 0) == {"echo": "write echo"}

        failed = _results(core, "sess", PROBE_TOOL)[1]
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
        core = _open(tmp_path)
        core.create_session("sess")
        assert core.run("let the hook read the session", session_id="sess", max_steps=16, root_action_budget=16).status == "completed"

        # The hook's note is appended to the settled result: a hook lease reads the session row.
        result_text, note_text = _text(core, "sess", PROBE_TOOL, 0).split("\n\n", 1)
        assert json.loads(result_text) == {"echo": "hooked"}
        note = json.loads(note_text)
        assert note["hook"] == "post_tool"
        assert note["tool"] == PROBE_TOOL
        assert note["session_id"] == "sess"
        assert note["status"] == "running"
        assert note["pending_approval"] is False
        assert note["chain_budget"] == 16
