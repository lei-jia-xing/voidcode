"""Focused core regressions for delegated collaboration.

The first four behaviors are proven against the real core/storage/runtime stack:

- a join answers for the child's CURRENT run, never an older terminal fact;
- a completion-notice acknowledgement commits with the tool result that consumed it;
- a terminal child run whose parent fact a crash never committed is republished from
  durable events on restart, with its notice, and without rerunning the child;
- steering authorization follows the CURRENT stance of parent and child.

The remaining behaviors are the concurrency-fix regressions. Each forces one legitimate
interleaving with Event/Barrier gates that only schedule real calls, then asserts the
consumer-visible outcome: the run result, the joined outcome, durable events, and the
provider requests. A gate wrapper never fabricates events, sequences, or results.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.s5_plugin import PLUGIN_ID as S5_PLUGIN_ID
from tests.fixtures.s5_plugin import PROBE_TOOL as S5_PROBE_TOOL
from tests.fixtures.s5_plugin import write_s5_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import DELEGATION_SPAWN_BUDGET, Core
from voidcode.sdk.services import ChildOutcome, ChildSendReceipt, child_completion_notice_id

PLUGIN_ID = "collaboration-fixture"
ACK_TOOL = "collaboration_ack"
PROBE_TOOL = "collaboration_probe"

_PLUGIN_CODE = '''
"""Fixture: spawn and join one child, acknowledge its completion, or park on a write probe."""

from __future__ import annotations

import json

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class CollaborationTool:
    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        if call.tool_name == "{probe}":
            return ToolSuccess(call.tool_name, TextOutput("probe ran"))
        child = services.spawn_child(str(call.arguments["prompt"]), label="delegated", notify_on_completion=True)
        outcome = services.join_child(child, timeout_seconds=30)
        if outcome.run_id is not None:
            services.acknowledge_child_completion(child, outcome.run_id)
        return ToolSuccess(
            call.tool_name,
            TextOutput(json.dumps({{"child": child, "status": outcome.status, "run_id": outcome.run_id}}, sort_keys=True)),
        )


def create_plugin(config):
    return Plugin(
        tools=(
            CollaborationTool(
                ToolDefinition(
                    name="collaboration_ack",
                    description="Spawn one child, join it, and acknowledge the run it returned.",
                    input_schema={{
                        "type": "object",
                        "properties": {{"prompt": {{"type": "string"}}}},
                        "required": ["prompt"],
                        "additionalProperties": False,
                    }},
                    effects=frozenset({{ToolEffect.SESSION}}),
                )
            ),
            CollaborationTool(
                ToolDefinition(
                    name="{probe}",
                    description="A write-tier probe that parks under approval mode ask.",
                    input_schema={{"type": "object", "properties": {{}}, "additionalProperties": False}},
                    effects=frozenset({{ToolEffect.WRITE}}),
                )
            ),
        )
    )
'''


def _write_plugin(source: Path) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(_PLUGIN_CODE.format(probe=PROBE_TOOL), encoding="utf-8")


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


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _open(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    approval_mode: str = "yolo",
    auto_drain: bool = True,
) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "voidcode.json"
    if not config_path.exists():
        source = tmp_path / "plugin"
        _write_plugin(source)
        config = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": approval_mode, "read_only": False},
            "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=tmp_path / "state", auto_drain=auto_drain)
    _CREATED.append(core)
    return core


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 15.0, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _child_facts(core: Core, parent: str) -> list[dict[str, object]]:
    return [dict(event.payload) for event in core.events_after(parent) if event.event_type == "kernel.child_finished"]


def _join(core: Core, parent: str, child: str, *, timeout: float = 30.0) -> ChildOutcome:
    """Join the child's current run; publication wake-up makes one call enough."""
    outcome = core.join_child(parent, child, timeout_seconds=timeout)
    assert outcome.status in ("completed", "failed", "cancelled"), outcome.as_payload()
    return outcome


def test_join_answers_for_the_current_run_not_an_older_fact(tmp_path: Path) -> None:
    replies = [
        _turn(output="first run done"),
        # The follow-up run asks for a write under approval mode ask: it parks, not completes.
        _turn(_call("probe-1", PROBE_TOOL)),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, approval_mode="ask", auto_drain=False)
        core.create_session("parent")
        child = core.spawn_child("parent", "first task")
        first = _join(core, "parent", child)
        assert (first.status, first.output) == ("completed", "first run done")

        receipt = core.send_child("parent", child, "second task")
        assert receipt.delivery == "started"
        assert receipt.run_id is not None

        outcome = core.join_child("parent", child, timeout_seconds=15)
        assert outcome.run_id == receipt.run_id, "the join must belong to the current run"
        assert outcome.status == "waiting", "the parked follow-up is not the old completed run"
        assert outcome.output is None, "an older run's output must never satisfy a newer join"

        facts = _child_facts(core, "parent")
        assert [fact["run_id"] for fact in facts] == [first.run_id], "only the first run is terminal"


def test_completion_notice_is_acked_with_the_tool_result_that_consumed_it(tmp_path: Path) -> None:
    replies = [
        _turn(_call("ack-1", ACK_TOOL, prompt="child work")),
        _turn(output="child answer"),
        _turn(output="parent done"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        result = core.run("delegate and consume", session_id=parent)
        assert result.status == "completed", result.error
        assert result.output == "parent done"

        facts = _child_facts(core, parent)
        assert len(facts) == 1
        child = str(facts[0]["child_session_id"])
        run_id = str(facts[0]["run_id"])
        assert facts[0]["status"] == "completed"
        notice_id = child_completion_notice_id(parent, child, run_id)

        # The ack cancelled the notice in the same commit as the consuming tool result.
        assert core.store.continuation_status(notice_id) == "cancelled"
        assert core.store.pending_continuations(parent, limit=10) == ()
        started = [event for event in core.events_after(parent) if event.event_type == "kernel.continuation_started"]
        assert all(event.payload.get("continuation_id") != notice_id for event in started)

        # The next request never re-delivers the consumed notice.
        assert len(server.requests) == 3
        assert notice_id not in json.dumps(server.requests[2]["messages"])
        assert "child_finished" not in json.dumps(server.requests[2]["messages"])


def test_restart_republishes_an_uncommitted_child_fact_once_without_rerunning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        _turn(output="recovered output"),
        _turn(output="notice consumed"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id

        # Simulate a crash after the child's terminal commit and before its parent fact.
        monkeypatch.setattr(core, "_publish_child_terminal", lambda *args, **kwargs: None)
        child = core.spawn_child(parent, "child prompt", notify_on_completion=True)
        _wait_for(lambda: core.session(child).status == "completed", what="the child's terminal commit")
        run_id = core.session(child).run_id
        assert run_id is not None
        joined = core.join_child(parent, child, timeout_seconds=0)
        assert joined.status == "running", "an unpublished terminal run is not a completed outcome"
        assert joined.run_id == run_id
        assert _child_facts(core, parent) == []
        core.close()
        _CREATED.remove(core)

        reopened = _open(tmp_path, server)
        _wait_for(lambda: bool(_child_facts(reopened, parent)), what="recovered child publication")
        _wait_for(lambda: len(server.requests) == 2, what="the recovered notice delivery")

        facts = _child_facts(reopened, parent)
        assert len(facts) == 1
        fact = facts[0]
        assert fact["child_session_id"] == child
        assert fact["run_id"] == run_id
        assert fact["status"] == "completed"
        assert fact["output"] == "recovered output"
        assert fact["notify_on_completion"] is True
        history = fact["history"]
        assert isinstance(history, Mapping) and history["sha256"]

        # The child was not rerun: exactly one provider request carried its prompt.
        child_requests = [
            request
            for request in server.requests
            if (messages := request.get("messages")) and isinstance(messages, list) and messages[-1].get("content") == "child prompt"
        ]
        assert len(child_requests) == 1
        notice_id = child_completion_notice_id(parent, child, run_id)
        _wait_for(lambda: reopened.store.continuation_status(notice_id) == "completed", what="the recovered notice consumption")


def test_send_authorization_follows_the_current_stance(tmp_path: Path) -> None:
    replies = [
        _turn(output="child one done"),
        _turn(output="child two done"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id
        other = core.create_session("other").session_id
        child = core.spawn_child(parent, "first task")
        first = _join(core, parent, child)
        assert (first.status, first.output) == ("completed", "child one done")

        with pytest.raises(PermissionError, match="does not belong"):
            core.send_child(other, child, "steer")

        core.store.set_session_overlay(parent, "core")
        with pytest.raises(PermissionError, match="read-only parent cannot steer"):
            core.send_child(parent, child, "steer")
        assert core.store.pending_continuations(child, limit=10) == (), "a refused send enqueues nothing"

        core.store.set_session_overlay(child, "core")
        receipt = core.send_child(parent, child, "second task", notify_on_completion=False)
        assert receipt.delivery == "started"
        assert receipt.run_id is not None and receipt.run_id != first.run_id

        second = _join(core, parent, child)
        assert (second.status, second.output) == ("completed", "child two done")
        assert second.run_id == receipt.run_id


def _cron_turn(call_id: str) -> dict[str, object]:
    return _turn(_call(call_id, "cron_list"))


def _open_bundled(tmp_path: Path, server: OpenAILoopbackServer, *, auto_drain: bool = True) -> Core:
    """A real core with the whole bundled plugin set admitted, like production boot."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            }
        ),
        encoding="utf-8",
    )
    core = open_core(workspace, state_dir=tmp_path / "state", auto_drain=auto_drain)
    _CREATED.append(core)
    return core


def _completions(core: Core, session_id: str, tool: str) -> list[dict[str, object]]:
    return [
        dict(event.payload)
        for event in core.events_after(session_id)
        if event.event_type == "kernel.tool_completed"
        and isinstance(event.payload.get("call"), Mapping)
        and event.payload["call"].get("tool_name") == tool
    ]


def _assert_contiguous_prefix(core: Core, session_id: str) -> list[str]:
    """The durable session is one contiguous prefix; return its event kinds for ordering asserts."""
    events = list(core.events_after(session_id))
    assert [event.sequence for event in events] == list(range(1, len(events) + 1)), "durable prefix stays contiguous"
    return [event.event_type for event in events]


@contextmanager
def _gated_publication(core: Core, release: threading.Event) -> Iterator[threading.Event]:
    """Hold the next real child publication at entry; release it when the test's event fires."""
    ready = threading.Event()
    original = core._publish_child_terminal
    used = False

    def gated_publish(*args: object, **kwargs: object) -> None:
        nonlocal used
        if used:
            original(*args, **kwargs)
            return
        used = True
        ready.set()
        assert release.wait(timeout=30), "publication release never arrived"
        original(*args, **kwargs)

    core._publish_child_terminal = gated_publish  # type: ignore[method-assign]
    try:
        yield ready
    finally:
        core._publish_child_terminal = original  # type: ignore[method-assign]


@contextmanager
def _commit_signals(core: Core, session_id: str, *kinds: str) -> Iterator[dict[str, threading.Event]]:
    """Fire one event per committed kind on this session; the real commit always runs first."""
    signals = {kind: threading.Event() for kind in kinds}
    original = core.store.commit_staged

    def gated_commit_staged(candidate: str, **kwargs: object) -> object:
        record = original(candidate, **kwargs)  # type: ignore[arg-type]
        if candidate == session_id:
            for draft in kwargs.get("events") or ():
                signal = signals.get(getattr(draft, "event_type", ""))
                if signal is not None:
                    signal.set()
        return record

    core.store.commit_staged = gated_commit_staged  # type: ignore[method-assign]
    try:
        yield signals
    finally:
        core.store.commit_staged = original  # type: ignore[method-assign]


@contextmanager
def _gated_first_two_calls(store: object, method: str, barrier: threading.Barrier) -> Iterator[None]:
    """Rendezvous the next two real store calls at a barrier, then run them unwrapped."""
    original = getattr(store, method)
    calls = 0
    lock = threading.Lock()

    def gated(*args: object, **kwargs: object) -> object:
        nonlocal calls
        with lock:
            calls += 1
            call = calls
        if call <= 2:
            barrier.wait()
        return original(*args, **kwargs)  # type: ignore[no-any-return]

    setattr(store, method, gated)
    try:
        yield
    finally:
        setattr(store, method, original)


@contextmanager
def _gated_first_payload(core: Core, release: threading.Event) -> Iterator[threading.Event]:
    """Hold the first real fact-payload build (before its history snapshot read) at entry."""
    ready = threading.Event()
    original = core._child_fact_payload
    used = False

    def gated_payload(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal used
        if used:
            return original(*args, **kwargs)  # type: ignore[arg-type]
        used = True
        ready.set()
        assert release.wait(timeout=30), "payload release never arrived"
        return original(*args, **kwargs)  # type: ignore[arg-type]

    core._child_fact_payload = gated_payload  # type: ignore[method-assign]
    try:
        yield ready
    finally:
        core._child_fact_payload = original  # type: ignore[method-assign]


def test_concurrent_child_publication_does_not_duplicate_the_parent_prefix(tmp_path: Path) -> None:
    """The duplicate-tail schedule: the parent batch completes and the tool result commits once."""
    replies = [
        _turn(output="child done"),
        _cron_turn("cron-0"),
        _cron_turn("cron-1"),
        _turn(output="parent done"),
    ]
    with _server(replies) as server:
        core = _open_bundled(tmp_path, server)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        fact_committed = threading.Event()
        with _gated_publication(core, release) as publication_ready:
            child = core.spawn_child(parent, "CHILD TASK", notify_on_completion=False)
            assert publication_ready.wait(timeout=30), "child publication never reached the gate"

            original_commit_staged = core.store.commit_staged
            gated_once = False

            def gated_commit_staged(session_id: str, **kwargs: object) -> object:
                nonlocal gated_once
                record = original_commit_staged(session_id, **kwargs)  # type: ignore[arg-type]
                events = kwargs.get("events") or ()
                kinds = {getattr(event, "event_type", "") for event in events}  # type: ignore[union-attr]
                if session_id == parent and "kernel.child_finished" in kinds:
                    fact_committed.set()
                if not gated_once and session_id == parent and "kernel.model_turn" in kinds:
                    gated_once = True
                    # Block INSIDE the parent's rebase, between its own commit tip and the
                    # post-commit tail read, until the real publisher commits the next sequence.
                    release.set()
                    assert fact_committed.wait(timeout=30), "child fact never committed inside the rebase window"
                return record

            core.store.commit_staged = gated_commit_staged  # type: ignore[method-assign]
            try:
                result = core.run("PARENT TASK", session_id=parent)
            finally:
                core.store.commit_staged = original_commit_staged  # type: ignore[method-assign]
            assert fact_committed.is_set(), "the fact raced the parent's rebase as scheduled"

        assert result.status == "completed", result.error
        assert result.output == "parent done"

        # The tool result committed exactly once and reached the model as a tool message.
        reports = _completions(core, parent, "cron_list")
        assert len(reports) == 2
        assert reports[0]["result"]["output"]["text"]
        tool_messages = [message for request in server.requests for message in request["messages"] if message.get("role") == "tool"]
        assert {message["tool_call_id"] for message in tool_messages} == {"cron-0", "cron-1"}

        kinds = _assert_contiguous_prefix(core, parent)
        assert kinds.count("kernel.child_finished") == 1
        events = list(core.events_after(parent))
        first_turn = next(event.sequence for event in events if event.event_type == "kernel.model_turn")
        fact_seq = next(event.sequence for event in events if event.event_type == "kernel.child_finished")
        first_intent = next(event.sequence for event in events if event.event_type == "kernel.tool_intent")
        assert first_turn < fact_seq < first_intent, "the fact landed inside the parent's rebase window"
        facts = _child_facts(core, parent)
        assert len(facts) == 1 and facts[0]["child_session_id"] == child


def test_child_fact_between_batches_does_not_leave_a_merge_hole(tmp_path: Path) -> None:
    """The merge-hole schedule: a fact landing at merge entry is reconciled, not skipped."""
    replies = [
        _turn(output="child done"),
        _cron_turn("cron-0"),
        _cron_turn("cron-1"),
        _turn(output="parent done"),
    ]
    with _server(replies) as server:
        core = _open_bundled(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        fact_committed = threading.Event()
        first_batch_reported = threading.Event()
        with _gated_publication(core, release) as publication_ready:
            child = core.spawn_child(parent, "CHILD TASK", notify_on_completion=True)
            assert publication_ready.wait(timeout=30), "child publication never reached the gate"
            with _commit_signals(core, parent, "kernel.tool_completed", "kernel.child_finished") as signals:
                original_claim = core.store.claim_child_messages
                merge_gate_used = False

                def gated_claim(*args: object, **kwargs: object) -> tuple[object, ...]:
                    nonlocal merge_gate_used
                    if not first_batch_reported.is_set() or merge_gate_used:
                        return original_claim(*args, **kwargs)  # type: ignore[return-value,arg-type]
                    merge_gate_used = True
                    release.set()
                    assert signals["kernel.child_finished"].wait(timeout=30), "child fact never committed at merge entry"
                    fact_committed.set()
                    return original_claim(*args, **kwargs)  # type: ignore[return-value,arg-type]

                core.store.claim_child_messages = gated_claim  # type: ignore[method-assign]
                try:

                    def mark_first_batch() -> None:
                        assert signals["kernel.tool_completed"].wait(timeout=30), "first batch never reported"
                        first_batch_reported.set()

                    marker = threading.Thread(target=mark_first_batch, daemon=True)
                    marker.start()
                    result = core.run("PARENT TASK", session_id=parent)
                    marker.join(timeout=30)
                finally:
                    core.store.claim_child_messages = original_claim  # type: ignore[method-assign]
                assert fact_committed.is_set(), "the fact raced the merge as scheduled"

        assert result.status == "completed", result.error
        assert result.output == "parent done"
        assert len(_completions(core, parent, "cron_list")) == 2

        kinds = _assert_contiguous_prefix(core, parent)
        assert kinds.index("kernel.child_finished") < kinds.index("kernel.continuation_started"), "the fact precedes its claimed notice"
        facts = _child_facts(core, parent)
        assert len(facts) == 1 and facts[0]["child_session_id"] == child


def test_provider_intent_survives_a_concurrent_child_fact_commit(tmp_path: Path) -> None:
    """A legal control-event CAS race retries; the run succeeds and the provider is called."""
    replies = [
        _turn(output="child done"),
        _cron_turn("cron-0"),
        _cron_turn("cron-1"),
        _turn(output="parent done"),
    ]
    with _server(replies) as server:
        core = _open_bundled(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        fact_committed = threading.Event()
        with _gated_publication(core, release) as publication_ready:
            child = core.spawn_child(parent, "CHILD TASK", notify_on_completion=False)
            assert publication_ready.wait(timeout=30), "child publication never reached the gate"

            original_commit_staged = core.store.commit_staged
            gated = False

            def gated_commit_staged(session_id: str, **kwargs: object) -> object:
                nonlocal gated
                events = kwargs.get("events") or ()
                kinds = {getattr(event, "event_type", "") for event in events}  # type: ignore[union-attr]
                if session_id == parent and "kernel.child_finished" in kinds:
                    fact_committed.set()
                if not gated and session_id == parent and "kernel.provider_intent" in kinds:
                    gated = True
                    release.set()
                    assert fact_committed.wait(timeout=30), "child fact never committed inside the CAS window"
                return original_commit_staged(session_id, **kwargs)  # type: ignore[arg-type]

            core.store.commit_staged = gated_commit_staged  # type: ignore[method-assign]
            try:
                result = core.run("PARENT TASK", session_id=parent)
            finally:
                core.store.commit_staged = original_commit_staged  # type: ignore[method-assign]
            assert fact_committed.is_set(), "the fact raced the intent commit as scheduled"

        assert result.status == "completed", result.error
        assert result.output == "parent done"

        # The provider was really dispatched for the parent, on top of the child turn.
        parent_requests = [request for request in server.requests if "PARENT TASK" in json.dumps(request.get("messages", []))]
        assert len(parent_requests) == 3, "two cron batches plus the closing turn"
        kinds = [event.event_type for event in core.events_after(parent)]
        assert "kernel.provider_intent" in kinds
        facts = _child_facts(core, parent)
        assert len(facts) == 1 and facts[0]["child_session_id"] == child


def test_genuine_root_budget_exhaustion_still_reports_limit_reached(tmp_path: Path) -> None:
    """The recursive fixture spends the whole root chain: the stop is limit_reached, not a race."""
    source = write_s5_plugin(tmp_path / "source")
    with _server([_turn(_call("recurse", S5_PROBE_TOOL, action="recurse"))]) as server:
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
                    "plugins": {"files": {"enabled": False}, S5_PLUGIN_ID: {"path": str(source)}},
                }
            ),
            encoding="utf-8",
        )
        core = open_core(workspace, state_dir=tmp_path / "state")
        _CREATED.append(core)
        parent = core.create_session("parent").session_id

        result = core.run("recurse until the root gives out", session_id=parent, max_steps=4)
        assert result.status == "interrupted", result.error
        assert result.stop_cause == "limit_reached"
        assert result.error == "root action budget exhausted"
        checkpoint = core.store.checkpoint(parent)
        assert checkpoint is not None
        assert core.store.root_budget_state(str(checkpoint.payload["root_id"])) == (4, 4)
        assert len(server.requests) == 1, "one dispatch; the chain dies on the durable budget, not a race"


def test_join_during_publication_waits_and_never_returns_an_empty_completed(tmp_path: Path) -> None:
    """A terminal child whose fact is in flight: join waits; timeout says running, not completed."""
    replies = [
        _turn(output="child answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        with _gated_first_payload(core, release) as gated:
            child = core.spawn_child(parent, "child work", notify_on_completion=False)
            assert gated.wait(timeout=30), "publication never reached the payload gate"

            outcome = core.join_child(parent, child, timeout_seconds=0)
            assert outcome.status == "running", "an unpublished run is not a completed outcome"
            assert outcome.output is None
            assert outcome.run_id == core.session(child).run_id

            release.set()

        joined = _join(core, parent, child)
        assert (joined.status, joined.output) == ("completed", "child answer")
        assert joined.history_sha256, "the published outcome carries its history snapshot"


def test_join_after_restart_returns_the_durable_outcome_without_rerunning(tmp_path: Path) -> None:
    """Close/reopen with a persisted parent fact: join returns it and the child never reruns."""
    replies = [
        _turn(output="only answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id
        child = core.spawn_child(parent, "single child task", notify_on_completion=False)
        first = _join(core, parent, child)
        assert (first.status, first.output) == ("completed", "only answer")
        assert first.history_sha256
        requests_before = len(server.requests)
        assert requests_before == 1
        core.close()
        _CREATED.remove(core)

        reopened = _open(tmp_path, server, auto_drain=False)
        recovered = reopened.join_child(parent, child, timeout_seconds=15)
        assert (recovered.status, recovered.output) == ("completed", "only answer")
        assert recovered.run_id == first.run_id
        assert recovered.history_sha256 == first.history_sha256
        assert len(server.requests) == requests_before, "join reads the durable fact; the child never reruns"
        facts = _child_facts(reopened, parent)
        assert len(facts) == 1 and facts[0]["run_id"] == first.run_id


def _history_text(core: Core, fact: dict[str, object]) -> str:
    history = fact["history"]
    assert isinstance(history, Mapping) and isinstance(history.get("sha256"), str)
    data, _total, _eof, _media = core._artifacts.read(str(history["sha256"]), offset=0, limit=256 * 1024)
    return data.decode("utf-8", errors="replace")


def test_late_same_run_control_event_does_not_extend_the_history_snapshot(tmp_path: Path) -> None:
    """A real grandchild fact landing after run_completed is excluded from the saved history."""
    replies = [
        _turn(output="A answer"),
        _turn(output="LATE DESCENDANT OUTPUT MARKER"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        with _gated_first_payload(core, release) as gated:
            child = core.spawn_child(parent, "child task A", notify_on_completion=False)
            assert gated.wait(timeout=30), "A publication never reached the payload gate"
            run_a = core.session(child).run_id
            assert run_a is not None and core.session(child).status == "completed"

            # A real descendant run completes on A while the payload build is still gated:
            # its kernel.child_spawned/child_finished control facts land after run_completed.
            grandchild = core.spawn_child(child, "late descendant task", notify_on_completion=False)
            descendant = core.join_child(child, grandchild, timeout_seconds=30)
            assert descendant.status == "completed" and descendant.output == "LATE DESCENDANT OUTPUT MARKER"
            release.set()

        outcome = _join(core, parent, child)
        assert (outcome.status, outcome.output) == ("completed", "A answer")
        facts = _child_facts(core, parent)
        assert len(facts) == 1 and facts[0]["run_id"] == run_a

        events = list(core.events_after(child))
        terminal_seq = next(event.sequence for event in events if event.event_type == "kernel.run_completed")
        late_seq = next(event.sequence for event in events if event.event_type == "kernel.child_finished")
        assert terminal_seq < late_seq, "the grandchild fact genuinely landed after the terminal event"

        history = _history_text(core, facts[0])
        assert "child task A" in history, "the snapshot keeps the prior session prefix"
        assert "A answer" in history
        assert "LATE DESCENDANT OUTPUT MARKER" not in history, "history ends at run_completed"


def test_delayed_publication_keeps_each_run_history_at_its_terminal_prefix(tmp_path: Path) -> None:
    """A is published after B completes: each fact's history ends at its own run."""
    replies = [
        _turn(output="A answer"),
        _turn(output="B answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        with _gated_first_payload(core, release) as gated:
            child = core.spawn_child(parent, "child task A", notify_on_completion=False)
            assert gated.wait(timeout=30), "A publication never reached the payload gate"

            receipt = core.send_child(parent, child, "followup B", notify_on_completion=False)
            assert receipt.delivery == "started" and receipt.run_id is not None
            second = _join(core, parent, child)
            assert (second.status, second.output) == ("completed", "B answer")
            assert second.run_id == receipt.run_id

            release.set()
            core.join_child(parent, child, timeout_seconds=15)

        _wait_for(lambda: len(_child_facts(core, parent)) == 2, what="the delayed A publication")
        facts = {str(fact["run_id"]): fact for fact in _child_facts(core, parent)}
        assert len(facts) == 2 and second.run_id in facts, "both runs published exactly once"
        fact_a = next(fact for fact in facts.values() if fact["output"] == "A answer")
        fact_b = next(fact for fact in facts.values() if fact["output"] == "B answer")

        history_a = _history_text(core, fact_a)
        assert "child task A" in history_a, "A history keeps the prior session prefix"
        assert "followup B" not in history_a, "A history excludes the later run"
        assert "B answer" not in history_a
        history_b = _history_text(core, fact_b)
        assert "child task A" in history_b, "B history retains the prior prefix"
        assert "followup B" in history_b


def test_restart_with_out_of_order_facts_publishes_nothing_twice(tmp_path: Path) -> None:
    """Facts committed B-then-A: restart recovery adds no duplicate and delivers no notice twice."""
    replies = [
        _turn(output="A answer"),
        _turn(output="B answer"),
        _turn(output="parent after restart"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        with _gated_first_payload(core, release) as gated:
            child = core.spawn_child(parent, "child task A", notify_on_completion=True)
            assert gated.wait(timeout=30), "A publication never reached the payload gate"

            receipt = core.send_child(parent, child, "followup B", notify_on_completion=True)
            assert receipt.delivery == "started" and receipt.run_id is not None
            second = _join(core, parent, child)
            assert (second.status, second.output) == ("completed", "B answer")
            release.set()
            core.join_child(parent, child, timeout_seconds=15)

        _wait_for(lambda: len(_child_facts(core, parent)) == 2, what="the delayed A publication")
        facts_before = _child_facts(core, parent)
        run_ids = {str(fact["run_id"]) for fact in facts_before}
        core.close()
        _CREATED.remove(core)

        reopened = _open(tmp_path, server)
        _wait_for(lambda: len(_child_facts(reopened, parent)) == 2, what="restart recovery to settle")
        facts_after = _child_facts(reopened, parent)
        assert {str(fact["run_id"]) for fact in facts_after} == run_ids
        assert sorted(str(fact["run_id"]) for fact in facts_after) == sorted({str(fact["run_id"]) for fact in facts_after}), (
            "no (child, run) fact duplicated"
        )

        notice_ids = {child_completion_notice_id(parent, child, run_id) for run_id in run_ids}
        statuses = {notice_id: reopened.store.continuation_status(notice_id) for notice_id in notice_ids}
        assert all(status in ("completed", "claimed", "pending") for status in statuses.values()), statuses
        started = [
            str(event.payload.get("continuation_id")) for event in reopened.events_after(parent) if event.event_type == "kernel.continuation_started"
        ]
        for notice_id in notice_ids:
            assert started.count(notice_id) <= 1, "no notice delivered twice"
        joined = reopened.join_child(parent, child, timeout_seconds=15)
        assert (joined.status, joined.output) == ("completed", "B answer")
        assert joined.run_id == receipt.run_id


def test_concurrent_sends_to_a_finished_child_start_exactly_one_run(tmp_path: Path) -> None:
    """Both sends enqueue; one starts the run, the loser stays queued; no receipt names a phantom."""
    replies = [
        _turn(output="initial answer"),
        _turn(output="merged answer TOKEN-ALPHA TOKEN-BETA"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id
        child = core.spawn_child(parent, "initial child task", notify_on_completion=False)
        initial = _join(core, parent, child)
        assert (initial.status, initial.output) == ("completed", "initial answer")

        store = core.store
        enqueue_barrier = threading.Barrier(2, timeout=30)
        claim_barrier = threading.Barrier(2, timeout=30)
        start = threading.Barrier(3, timeout=30)
        receipts: list[ChildSendReceipt] = []
        errors: list[str] = []

        def send(prompt: str) -> None:
            start.wait()
            try:
                receipts.append(core.send_child(parent, child, prompt, notify_on_completion=False))
            except Exception as exc:  # noqa: BLE001 - the test records the real failure
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=send, args=(f"follow-up TOKEN-{name}",)) for name in ("ALPHA", "BETA")]
        with (
            _gated_first_two_calls(store, "enqueue_continuation", enqueue_barrier),
            _gated_first_two_calls(store, "claim_and_start_continuation", claim_barrier),
        ):
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=30)
        assert not [thread for thread in threads if thread.is_alive()], "a send thread hung"
        assert not errors, errors
        assert len(receipts) == 2

        started = [receipt for receipt in receipts if receipt.delivery == "started"]
        queued = [receipt for receipt in receipts if receipt.delivery == "queued"]
        assert len(started) == 1, "exactly one sender reserves the run"
        assert len(queued) == 1, "the loser stays queued"
        assert started[0].run_id is not None
        assert queued[0].run_id is None, "a queued receipt names no run it never claimed"
        assert queued[0].message_id is not None, "the loser stays visible through its receipt"

        outcome = _join(core, parent, child)
        assert outcome.status == "completed"
        assert outcome.run_id == started[0].run_id, "the executed run is the started receipt's run"
        assert started[0].run_id != initial.run_id, "the follow-up is a new run, not the initial one"
        child_events = list(core.events_after(child))
        started_runs = {event.run_id for event in child_events if event.event_type == "kernel.continuation_started"}
        assert started_runs == {started[0].run_id}, "exactly one follow-up run was claimed"
        followup_intents = [event for event in child_events if event.event_type == "kernel.provider_intent" and event.run_id != initial.run_id]
        assert followup_intents, "the follow-up run really dispatched its provider turn"
        assert {event.run_id for event in followup_intents} == {started[0].run_id}
        facts = [fact for fact in _child_facts(core, parent) if fact["run_id"] == started[0].run_id]
        assert len(facts) == 1, "the started run has exactly one terminal fact"


def test_concurrent_spawns_at_budget_accept_exactly_one_child(tmp_path: Path) -> None:
    """Two contenders at count 3 / limit 4: one wins, one is refused, no phantom child runs."""
    replies = [
        _turn(output="child done"),
        _turn(output="child done"),
        _turn(output="child done"),
        _turn(output="child done"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id
        assert DELEGATION_SPAWN_BUDGET == 4
        for index in range(3):
            child = core.spawn_child(parent, f"setup child {index}", notify_on_completion=False)
            assert _join(core, parent, child).status == "completed"
        assert core.store.count_root_children(parent) == 3

        contender_barrier = threading.Barrier(2, timeout=30)
        original_count = core.store.count_root_children
        mainstream = threading.get_ident()
        captured: dict[str, int] = {}

        def counting_count(root_session_id: str) -> int:
            real = original_count(root_session_id)
            if threading.get_ident() != mainstream and real == 3:
                captured[threading.current_thread().name] = real
                contender_barrier.wait()
            return real

        core.store.count_root_children = counting_count  # type: ignore[method-assign]
        outcomes: dict[str, str] = {}
        start = threading.Barrier(3, timeout=30)

        def contend(name: str) -> None:
            start.wait()
            try:
                outcomes[name] = f"accepted:{core.spawn_child(parent, f'contender {name}', notify_on_completion=False)}"
            except ValueError as exc:
                outcomes[name] = f"refused:{exc}"

        threads = [threading.Thread(target=contend, args=(name,), name=name) for name in ("contender-A", "contender-B")]
        try:
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            core.store.count_root_children = original_count  # type: ignore[method-assign]
        assert not [thread for thread in threads if thread.is_alive()], "a contender hung"
        assert set(captured.values()) == {3} and len(captured) == 2, "both contenders raced on the real count 3"

        accepted = [value.split(":", 1)[1] for value in outcomes.values() if value.startswith("accepted:")]
        refused = [value for value in outcomes.values() if value.startswith("refused:")]
        assert len(accepted) == 1, f"exactly one contender wins: {outcomes}"
        assert len(refused) == 1 and "spawn budget exhausted" in refused[0]
        assert _join(core, parent, accepted[0]).status == "completed"

        children = [record for record in core.sessions() if record.parent_session_id == parent]
        assert len(children) == 4, "exactly the budget, with lineage intact"
        assert {record.root_session_id for record in children} == {parent}
        assert {record.depth for record in children} == {1}
        assert len(server.requests) == 4, "three setup turns plus the one winning contender"


def test_cancelled_child_accepts_a_later_explicit_followup_with_clean_attribution(tmp_path: Path) -> None:
    """Accepted cancel of a parked run settles it; a later explicit send starts a new run."""
    replies = [
        _turn(output="first answer"),
        # The follow-up asks for a write under approval mode ask: it parks, not completes.
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="followup answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, approval_mode="ask", auto_drain=False)
        parent = core.create_session("parent").session_id
        child = core.spawn_child(parent, "first task", notify_on_completion=False)
        first = _join(core, parent, child)
        assert (first.status, first.output) == ("completed", "first answer")

        receipt = core.send_child(parent, child, "parked task", notify_on_completion=False)
        assert receipt.delivery == "started" and receipt.run_id is not None
        parked = core.join_child(parent, child, timeout_seconds=30)
        assert parked.status == "waiting" and parked.run_id == receipt.run_id

        assert core.cancel_child(parent, child, reason="owner stops here") is True, "parked work is accepted for cancel"
        settled = core.join_child(parent, child, timeout_seconds=30)
        assert settled.status == "cancelled", settled.as_payload()
        assert settled.run_id == receipt.run_id, "cancel settles the observed run, never a newer one"

        followup = core.send_child(parent, child, "explicit followup", notify_on_completion=False)
        assert followup.delivery == "started"
        assert followup.run_id is not None and followup.run_id != receipt.run_id

        second = _join(core, parent, child)
        assert (second.status, second.output) == ("completed", "followup answer")
        assert second.run_id == followup.run_id

        facts = _child_facts(core, parent)
        assert [(fact["run_id"], fact["output"], fact["status"]) for fact in facts] == [
            (first.run_id, "first answer", "completed"),
            (receipt.run_id, None, "cancelled"),
            (followup.run_id, "followup answer", "completed"),
        ], "each run's fact attributes its own outcome"
        assert len(server.requests) == 3, "no run replayed; the cancel settled instead of rerunning"


def test_send_overlapping_cancel_linearizes_with_observed_run_attribution(tmp_path: Path) -> None:
    """A send racing cancel's sweep may start new work; cancel never publishes the newer run."""
    replies = [
        _turn(output="first answer"),
        _turn(output="overlap answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id
        child = core.spawn_child(parent, "first task", notify_on_completion=False)
        first = _join(core, parent, child)
        assert (first.status, first.output) == ("completed", "first answer")
        run_before = core.session(child).run_id

        swept = threading.Event()
        send_done = threading.Event()
        original_cancel_pending = core.store.cancel_pending_continuations

        def gated_cancel_pending(session_id: str, owner: str, **kwargs: object) -> object:
            result = original_cancel_pending(session_id, owner, **kwargs)  # type: ignore[arg-type]
            if session_id == child:
                swept.set()
                assert send_done.wait(timeout=30), "overlapping send never ran inside the cancel window"
            return result

        core.store.cancel_pending_continuations = gated_cancel_pending  # type: ignore[method-assign]
        cancelled: list[bool] = []
        try:
            cancel_thread = threading.Thread(target=lambda: cancelled.append(core.cancel_child(parent, child)))
            cancel_thread.start()
            assert swept.wait(timeout=30), "cancel never reached the pending sweep"
            # The send linearizes after the sweep: it may legitimately start new work.
            receipt = core.send_child(parent, child, "overlapping followup", notify_on_completion=False)
            send_done.set()
            cancel_thread.join(timeout=30)
        finally:
            core.store.cancel_pending_continuations = original_cancel_pending  # type: ignore[method-assign]
        assert not cancel_thread.is_alive(), "cancel hung"

        second = _join(core, parent, child)
        assert (second.status, second.output) == ("completed", "overlap answer")
        assert second.run_id == receipt.run_id and receipt.run_id != run_before
        facts = _child_facts(core, parent)
        assert {str(fact["run_id"]) for fact in facts} == {first.run_id, receipt.run_id}
        assert not [fact for fact in facts if fact["status"] == "cancelled" and fact["run_id"] == receipt.run_id], (
            "cancel never publishes the newer run it did not observe"
        )
        assert len(server.requests) == 2, "the overlapping follow-up ran once; nothing replayed"


def test_restart_recovers_an_older_missing_fact_beside_a_newer_run_without_replay(tmp_path: Path) -> None:
    """A's publication gated away, B published: restart publishes A from durable events; no rerun."""
    replies = [
        _turn(output="A answer"),
        _turn(output="B answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server, auto_drain=False)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        with _gated_first_payload(core, release) as gated:
            try:
                child = core.spawn_child(parent, "child task A", notify_on_completion=False)
                assert gated.wait(timeout=30), "A publication never reached the payload gate"
                run_a = core.session(child).run_id
                assert run_a is not None and core.session(child).status == "completed"

                receipt = core.send_child(parent, child, "followup B", notify_on_completion=False)
                assert receipt.delivery == "started" and receipt.run_id is not None
                second = _join(core, parent, child)
                assert (second.status, second.output) == ("completed", "B answer")
                assert second.run_id == receipt.run_id

                # Close with A's publication still gated: it never commits, and the absence
                # is asserted on the closed durable state before recovery runs below.
                assert {str(fact["run_id"]) for fact in _child_facts(core, parent)} == {receipt.run_id}
                core.close()
                _CREATED.remove(core)
            finally:
                # Free the parked publisher only after close, so it observes the closed
                # core instead of committing A.
                release.set()

        reopened = _open(tmp_path, server, auto_drain=False)
        assert {str(fact["run_id"]) for fact in _child_facts(reopened, parent)} == {receipt.run_id}, (
            "A's fact is genuinely absent before recovery runs"
        )
        reopened._recover_child_publications()
        facts_after = {str(fact["run_id"]): fact for fact in _child_facts(reopened, parent)}
        assert set(facts_after) == {run_a, receipt.run_id}, "A recovered; B untouched"
        assert facts_after[run_a]["output"] == "A answer"
        assert facts_after[receipt.run_id]["output"] == "B answer"
        assert len(server.requests) == 2, "recovery republishes facts; it never reruns the child"
        joined = reopened.join_child(parent, child, timeout_seconds=15)
        assert (joined.status, joined.output) == ("completed", "B answer")
        assert joined.run_id == receipt.run_id, "join answers for the current run"
