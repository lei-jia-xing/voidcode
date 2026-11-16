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

import hashlib
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
from voidcode.bootstrap import open_core
from voidcode.core.engine import DELEGATION_SPAWN_BUDGET, Core
from voidcode.core.events import CHILD_OUTCOME_RECEIPT_OWNER, child_completion_notice_id
from voidcode.core.storage.store import StoreConflict
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.services import ChildOutcome, ChildSendReceipt

PLUGIN_ID = "collaboration-fixture"
ACK_TOOL = "collaboration_ack"
PROBE_TOOL = "collaboration_probe"
WAIT_TOOL = "collaboration_wait"

_PLUGIN_CODE = '''
"""Fixture: spawn and join one child, acknowledge its completion, or park on a write probe."""

from __future__ import annotations

import json

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess


class FixtureBody:
    def __init__(self, payload):
        self.payload = payload

    def as_payload(self):
        return self.payload


class CollaborationTool:
    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        if call.tool_name == "{probe}":
            return ToolSuccess(call.tool_name, TextOutput("probe ran"))
        if call.tool_name == "{wait}":
            action = call.arguments["action"]
            if action == "spawn":
                child = services.spawn_child(
                    str(call.arguments["prompt"]),
                    label="wait fixture",
                    max_steps=call.arguments.get("max_steps"),
                    notify_on_completion=False,
                )
                return ToolSuccess(call.tool_name, TextOutput(child), body=FixtureBody({{"child_id": child}}))
            child_id = str(call.arguments["child_id"])
            outcomes = services.wait_children((child_id,))
            mode = call.arguments.get("mode", "success")
            if mode == "raise":
                raise RuntimeError("consumer failed after waiting")
            if mode == "invalid":
                return object()
            payload = [outcome.as_payload() for outcome in outcomes]
            if mode == "failure":
                return ToolFailure(
                    call.tool_name,
                    error="consumer reported a child result",
                    output=TextOutput("child result observed"),
                    body=FixtureBody({{"outcomes": payload}}),
                )
            return ToolSuccess(call.tool_name, TextOutput(json.dumps(payload, sort_keys=True)), body=FixtureBody({{"outcomes": payload}}))
        child = services.spawn_child(str(call.arguments["prompt"]), label="delegated", notify_on_completion=True)
        outcome = services.join_child(child, timeout_seconds=30)
        if outcome.run_id is not None:
            services.acknowledge_child_completion(child, outcome.run_id)
        return ToolSuccess(
            call.tool_name,
            TextOutput(json.dumps({{"child": child, "status": outcome.status, "run_id": outcome.run_id}}, sort_keys=True)),
        )
def create_plugin(config):
    _ = config
    return Plugin(
        tools=(
            CollaborationTool(
                ToolDefinition(
                    name="{ack}",
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
            CollaborationTool(
                ToolDefinition(
                    name="{wait}",
                    description="Spawn an unnotified child or wait for its exact outcome.",
                    input_schema={{
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {{
                            "action": {{"type": "string", "enum": ["spawn", "wait"]}},
                            "prompt": {{"type": "string"}},
                            "child_id": {{"type": "string"}},
                            "max_steps": {{"type": "integer", "minimum": 1}},
                            "mode": {{"type": "string", "enum": ["success", "raise", "invalid", "failure"]}},
                        }},
                        "required": ["action"],
                    }},
                    effects=frozenset({{ToolEffect.READ, ToolEffect.SPAWN, ToolEffect.SESSION}}),
                )
            ),
        )
    )
'''


def _write_plugin(source: Path) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(
        _PLUGIN_CODE.format(probe=PROBE_TOOL, wait=WAIT_TOOL, ack=ACK_TOOL),
        encoding="utf-8",
    )


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
    core = open_core(workspace, state_dir=tmp_path / "state")
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
    return [dict(event.payload) for event in core.events_after(parent) if event.event_type == "core.child_finished"]


def _tool_result_body(core: Core, request: Mapping[str, object], call_id: str) -> dict[str, object]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise AssertionError("provider request has no messages")
    content = next(
        (
            message.get("content")
            for message in messages
            if isinstance(message, Mapping) and message.get("role") == "tool" and message.get("tool_call_id") == call_id
        ),
        None,
    )
    if not isinstance(content, str):
        raise AssertionError(f"provider request has no result for tool call {call_id!r}")
    projection = json.loads(content)
    if not isinstance(projection, Mapping):
        raise AssertionError(f"tool projection is not an object: {call_id!r}")
    uri = projection.get("result_uri")
    if not isinstance(uri, str) or not uri.startswith("artifact://"):
        raise AssertionError(f"tool projection has no canonical result URI: {projection}")
    digest = uri.removeprefix("artifact://")
    chunks: list[bytes] = []
    offset = 0
    total: int | None = None
    while True:
        page = core.read_artifact(digest, offset=offset)
        if page.offset != offset or page.artifact.sha256 != digest:
            raise AssertionError(f"canonical result page identity changed at offset {offset}")
        if total is None:
            total = page.artifact.bytes
        if page.artifact.bytes != total or (not page.data and not page.eof):
            raise AssertionError(f"canonical result page did not advance at offset {offset}")
        chunks.append(page.data)
        offset += len(page.data)
        if page.eof:
            if offset != total:
                raise AssertionError(f"canonical result ended at {offset} of {total} bytes")
            break
    canonical = b"".join(chunks)
    if hashlib.sha256(canonical).hexdigest() != digest:
        raise AssertionError("canonical result artifact digest does not match its URI")
    payload = json.loads(canonical.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise AssertionError("canonical tool result is not an object")
    result = decode_tool_result(payload)
    if result.body is None:
        raise AssertionError(f"canonical tool result has no body: {call_id!r}")
    return dict(result.body.as_payload())


def _last_user_text(request: Mapping[str, object]) -> str | None:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, Mapping) and message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else None
    return None


def _last_message_role(request: Mapping[str, object]) -> str | None:
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], Mapping):
        return None
    role = messages[-1].get("role")
    return role if isinstance(role, str) else None


def test_wait_receipts_bind_exact_pause_then_resume_and_survive_failed_consumers_and_reopen(tmp_path: Path) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "limited child":
                if last_role == "user":
                    return _turn(_call("budget-probe", PROBE_TOOL))
                return _turn(output="resumed child result")
            if prompt in ("create", "raise consumer", "invalid consumer", "valid failure consumer", "consume pause", "consume terminal", "reopen"):
                if last_role == "tool":
                    return _turn(output="parent complete")
                if prompt == "create":
                    arguments: dict[str, object] = {"action": "spawn", "prompt": "limited child", "max_steps": 1}
                else:
                    mode = (
                        "raise"
                        if prompt == "raise consumer"
                        else "invalid"
                        if prompt == "invalid consumer"
                        else "failure"
                        if prompt == "valid failure consumer"
                        else "success"
                    )
                    arguments = {"action": "wait", "child_id": child_holder["child"], "mode": mode}
                call_id = f"wait-{prompt.replace(' ', '-')}"
                return _turn(_call(call_id, WAIT_TOOL, **arguments))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        paused = core.join_child(parent, child, timeout_seconds=15)
        assert paused.status == "interrupted"
        assert paused.run_id is not None
        assert paused.budget_exhaustion is not None
        assert paused.budget_exhaustion.as_payload() == {
            "dimension": "provider_turns",
            "source": "max_steps",
            "limit": 1,
            "consumed": 1,
            "remaining": 0,
            "continue_with": "resume_with_max_steps",
        }
        assert _child_facts(core, parent) == []
        stop_event = next(event for event in core.store.events(child) if event.event_type == "core.run_interrupted")
        assert stop_event.run_id == paused.run_id

        for prompt in ("raise consumer", "invalid consumer"):
            failed_consumer = core.run(prompt, session_id=parent)
            assert failed_consumer.status == "completed", failed_consumer.error
            assert not core.store.has_observer_receipt(child, stop_event.sequence, CHILD_OUTCOME_RECEIPT_OWNER)

        valid_failure = core.run("valid failure consumer", session_id=parent)
        assert valid_failure.status == "completed", valid_failure.error
        failure_body = _tool_result_body(core, server.requests[-1], "wait-valid-failure-consumer")
        pause_outcomes = failure_body.get("outcomes")
        assert isinstance(pause_outcomes, list) and len(pause_outcomes) == 1
        assert pause_outcomes[0]["run_id"] == paused.run_id
        assert pause_outcomes[0]["budget_exhaustion"] == paused.budget_exhaustion.as_payload()
        assert core.store.has_observer_receipt(child, stop_event.sequence, CHILD_OUTCOME_RECEIPT_OWNER)
        repeated = core.run("consume pause", session_id=parent)
        assert repeated.status == "completed", repeated.error
        repeated_body = _tool_result_body(core, server.requests[-1], "wait-consume-pause")
        assert repeated_body.get("outcomes") == []

        resumed = core.resume(child, max_steps=3)
        assert resumed.status == "completed", resumed.error
        assert resumed.run_id != paused.run_id
        terminal = next(event for event in core.store.events(parent) if event.event_type == "core.child_finished")
        assert terminal.payload["run_id"] == resumed.run_id
        assert terminal.payload["output"] == "resumed child result"

        terminal_delivery = core.run("consume terminal", session_id=parent)
        assert terminal_delivery.status == "completed", terminal_delivery.error
        terminal_body = _tool_result_body(core, server.requests[-1], "wait-consume-terminal")
        terminal_outcomes = terminal_body.get("outcomes")
        assert isinstance(terminal_outcomes, list) and len(terminal_outcomes) == 1
        assert terminal_outcomes[0]["run_id"] == resumed.run_id
        assert "budget_exhaustion" not in terminal_outcomes[0]
        assert core.store.has_observer_receipt(parent, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)
        notice_id = child_completion_notice_id(parent, child, str(resumed.run_id))
        assert core.store.continuation_status(notice_id) is None

        core.close()
        reopened = _open(tmp_path, server)
        reopened.auto_drain = False
        replay = reopened.run("reopen", session_id=parent)
        assert replay.status == "completed", replay.error
        replay_body = _tool_result_body(reopened, server.requests[-1], "wait-reopen")
        assert replay_body.get("outcomes") == []


def test_pause_receipt_commits_for_old_event_after_resume_and_restop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}
        child_turns = 0

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            nonlocal child_turns
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "limited child":
                child_turns += 1
                if child_turns <= 2:
                    return _turn(_call(f"budget-probe-{child_turns}", PROBE_TOOL))
                return _turn(output="resumed after old pause receipt")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-child", WAIT_TOOL, action="spawn", prompt="limited child", max_steps=1))
            if prompt in ("deliver first pause", "deliver second pause", "deliver completion"):
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(
                    _call(
                        prompt.replace(" ", "-"),
                        WAIT_TOOL,
                        action="wait",
                        child_id=child_holder["child"],
                    )
                )
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        first_pause = core.join_child(parent, child, timeout_seconds=15)
        assert first_pause.status == "interrupted" and first_pause.run_id is not None
        first_stop = next(event for event in core.store.events(child) if event.event_type == "core.run_interrupted")

        entered = threading.Event()
        release = threading.Event()
        original_commit = core.store.commit_staged

        def gate_commit(session_id: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            receipts = kwargs.get("observer_receipts", ())
            if (
                session_id == parent
                and not entered.is_set()
                and any(receipt.session_id == child and receipt.event_sequence == first_stop.sequence for receipt in receipts)
            ):
                entered.set()
                if not release.wait(timeout=30):
                    raise TimeoutError("parent receipt commit gate was not released")
            return original_commit(session_id, *args, **kwargs)

        monkeypatch.setattr(core.store, "commit_staged", gate_commit)
        parent_results = []
        parent_errors: list[str] = []

        def consume_first_pause() -> None:
            try:
                parent_results.append(core.run("deliver first pause", session_id=parent))
            except Exception as exc:  # noqa: BLE001 - record the actual Core invocation failure
                parent_errors.append(f"{type(exc).__name__}: {exc}")

        parent_thread = threading.Thread(target=consume_first_pause, daemon=True)
        parent_thread.start()
        try:
            assert entered.wait(timeout=15), "wait delivery never reached its atomic receipt commit"
            second_pause = core.resume(child, max_steps=1)
            assert second_pause.status == "interrupted"
            assert second_pause.run_id is not None and second_pause.run_id != first_pause.run_id
            second_stop = [
                event for event in core.store.events(child) if event.event_type == "core.run_interrupted" and event.run_id == second_pause.run_id
            ][-1]
            assert second_stop.sequence > first_stop.sequence
            assert not core.store.has_observer_receipt(child, first_stop.sequence, CHILD_OUTCOME_RECEIPT_OWNER)
        finally:
            release.set()
            parent_thread.join(timeout=30)
        assert not parent_thread.is_alive(), "wait tool result commit did not finish"
        assert not parent_errors, parent_errors
        assert len(parent_results) == 1 and parent_results[0].status == "completed"
        first_body = _tool_result_body(core, server.requests[-1], "deliver-first-pause")
        first_outcomes = first_body.get("outcomes")
        assert isinstance(first_outcomes, list) and [outcome["run_id"] for outcome in first_outcomes] == [first_pause.run_id]
        assert core.store.has_observer_receipt(child, first_stop.sequence, CHILD_OUTCOME_RECEIPT_OWNER)
        assert not core.store.has_observer_receipt(child, second_stop.sequence, CHILD_OUTCOME_RECEIPT_OWNER)

        second_delivery = core.run("deliver second pause", session_id=parent)
        assert second_delivery.status == "completed", second_delivery.error
        second_body = _tool_result_body(core, server.requests[-1], "deliver-second-pause")
        second_outcomes = second_body.get("outcomes")
        assert isinstance(second_outcomes, list) and [outcome["run_id"] for outcome in second_outcomes] == [second_pause.run_id]
        assert core.store.has_observer_receipt(child, second_stop.sequence, CHILD_OUTCOME_RECEIPT_OWNER)

        resumed = core.resume(child, max_steps=3)
        assert resumed.status == "completed", resumed.error
        assert resumed.run_id is not None and resumed.run_id != second_pause.run_id
        terminal = next(event for event in core.store.events(parent) if event.event_type == "core.child_finished")
        assert terminal.payload["run_id"] == resumed.run_id
        completion_delivery = core.run("deliver completion", session_id=parent)
        assert completion_delivery.status == "completed", completion_delivery.error
        terminal_body = _tool_result_body(core, server.requests[-1], "deliver-completion")
        terminal_outcomes = terminal_body.get("outcomes")
        assert isinstance(terminal_outcomes, list) and [outcome["run_id"] for outcome in terminal_outcomes] == [resumed.run_id]
        assert core.store.has_observer_receipt(parent, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)


def test_wait_rescans_completion_committed_after_parent_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}
        child_started = threading.Event()
        release_child = threading.Event()
        wait_scan_taken = threading.Event()
        release_wait_scan = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "gated child":
                child_started.set()
                if not release_child.wait(timeout=30):
                    raise TimeoutError("child completion gate was not released")
                return _turn(output="exact raced completion")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-race-child", WAIT_TOOL, action="spawn", prompt="gated child"))
            if prompt in ("wait", "wait replay"):
                if last_role == "tool":
                    return _turn(output="parent complete")
                call_id = "wait-race" if prompt == "wait" else "wait-replay"
                return _turn(_call(call_id, WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        assert child_started.wait(timeout=15), "child never reached its gated provider turn"
        run_id = core.session(child).run_id
        assert run_id is not None and core.session(child).status == "running"
        parent_tip = max(event.sequence for event in core.store.events(parent))

        original_events = core.store.events
        original_registrations = core.store.child_wait_registrations
        wait_registered = threading.Event()
        scan_gated = False

        def track_registrations(child_ids):
            rows = original_registrations(child_ids)
            wait_registered.set()
            return rows

        def gate_parent_snapshot(session_id: str, after_sequence: int = 0, limit: int | None = None):
            nonlocal scan_gated
            rows = original_events(session_id, after_sequence, limit)
            if wait_registered.is_set() and session_id == parent and after_sequence > parent_tip and not scan_gated:
                scan_gated = True
                assert rows == (), rows
                wait_scan_taken.set()
                if not release_wait_scan.wait(timeout=30):
                    raise TimeoutError("stale parent event snapshot gate was not released")
            return rows

        monkeypatch.setattr(core.store, "child_wait_registrations", track_registrations)
        monkeypatch.setattr(core.store, "events", gate_parent_snapshot)
        results: list[object] = []
        errors: list[str] = []

        def wait_for_child() -> None:
            try:
                results.append(core.run("wait", session_id=parent))
            except Exception as exc:  # noqa: BLE001 - preserve the real Core invocation failure
                errors.append(f"{type(exc).__name__}: {exc}")

        parent_thread = threading.Thread(target=wait_for_child, daemon=True)
        parent_thread.start()
        try:
            assert wait_scan_taken.wait(timeout=15), "wait did not reach the empty parent-event snapshot"
            release_child.set()
            outcome = core.join_child(parent, child, timeout_seconds=15)
            assert outcome.status == "completed" and outcome.run_id == run_id
            terminal = next(
                event for event in core.store.events(parent) if event.event_type == "core.child_finished" and event.payload.get("run_id") == run_id
            )
            release_wait_scan.set()
            parent_thread.join(timeout=30)
            assert not parent_thread.is_alive(), "wait did not return after the committed child fact"
            assert not errors, errors
            assert len(results) == 1 and getattr(results[0], "status", None) == "completed", results
            body = _tool_result_body(core, server.requests[-1], "wait-race")
            outcomes = body.get("outcomes")
            assert isinstance(outcomes, list) and len(outcomes) == 1, body
            assert outcomes[0]["child_session_id"] == child and outcomes[0]["run_id"] == run_id, outcomes[0]
            assert outcomes[0]["output"] == "exact raced completion", outcomes[0]
            assert core.store.has_observer_receipt(parent, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)

            replay = core.run("wait replay", session_id=parent)
            assert replay.status == "completed", replay.error
            replay_body = _tool_result_body(core, server.requests[-1], "wait-replay")
            assert replay_body.get("outcomes") == []
        finally:
            release_child.set()
            release_wait_scan.set()
            parent_thread.join(timeout=30)


def test_wait_binds_reserved_run_after_child_event_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}
        driver_entered = threading.Event()
        release_driver = threading.Event()
        child_provider_started = threading.Event()
        release_child_provider = threading.Event()
        child_scan_taken = threading.Event()
        release_child_scan = threading.Event()
        condition_waiting = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "reserved child":
                child_provider_started.set()
                if not release_child_provider.wait(timeout=30):
                    raise TimeoutError("reserved child provider gate was not released")
                return _turn(output="reserved run completed")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-reserved-child", WAIT_TOOL, action="spawn", prompt="reserved child"))
            if prompt in ("wait", "wait replay"):
                if last_role == "tool":
                    return _turn(output="parent complete")
                call_id = "wait-reserved" if prompt == "wait" else "wait-reserved-replay"
                return _turn(_call(call_id, WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        original_run_child = core._delegation._run_child

        def gate_child_driver(*args, **kwargs):
            driver_entered.set()
            if not release_driver.wait(timeout=30):
                raise TimeoutError("reserved child driver gate was not released")
            return original_run_child(*args, **kwargs)

        monkeypatch.setattr(core._delegation, "_run_child", gate_child_driver)
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        assert driver_entered.wait(timeout=15), "child reservation never reached its gated driver"
        assert core.session(child).run_id is None

        original_events = core.store.events
        original_registrations = core.store.child_wait_registrations
        wait_registered = threading.Event()
        child_event_reads = 0

        def track_registrations(child_ids):
            rows = original_registrations(child_ids)
            wait_registered.set()
            return rows

        def gate_child_snapshot(session_id: str, after_sequence: int = 0, limit: int | None = None):
            nonlocal child_event_reads
            rows = original_events(session_id, after_sequence, limit)
            if wait_registered.is_set() and session_id == child:
                child_event_reads += 1
                if child_event_reads == 2:
                    assert rows == (), rows
                    child_scan_taken.set()
                    if not release_child_scan.wait(timeout=30):
                        raise TimeoutError("reserved child event snapshot gate was not released")
            return rows

        monkeypatch.setattr(core.store, "child_wait_registrations", track_registrations)
        monkeypatch.setattr(core.store, "events", gate_child_snapshot)
        condition = core._delegation._child_finished_lock
        original_condition_wait = condition.wait

        def observe_condition_wait(timeout: float | None = None):
            condition_waiting.set()
            return original_condition_wait(timeout)

        monkeypatch.setattr(condition, "wait", observe_condition_wait)
        results: list[object] = []
        errors: list[str] = []

        def wait_for_child() -> None:
            try:
                results.append(core.run("wait", session_id=parent))
            except Exception as exc:  # noqa: BLE001 - preserve the real Core invocation failure
                errors.append(f"{type(exc).__name__}: {exc}")

        parent_thread = threading.Thread(target=wait_for_child, daemon=True)
        parent_thread.start()
        try:
            assert child_scan_taken.wait(timeout=15), "wait did not reach the empty child-event snapshot"
            release_driver.set()
            assert child_provider_started.wait(timeout=15), "reserved child did not start its captured run"
            run_id = core.session(child).run_id
            assert run_id is not None and core.session(child).status == "running"
            release_child_scan.set()
            assert condition_waiting.wait(timeout=15), "wait failed to bind and await the newly started child run"
            assert parent_thread.is_alive()
            release_child_provider.set()
            parent_thread.join(timeout=30)
            assert not parent_thread.is_alive(), "wait did not return after the bound child run completed"
            assert not errors, errors
            assert len(results) == 1 and getattr(results[0], "status", None) == "completed", results
            body = _tool_result_body(core, server.requests[-1], "wait-reserved")
            outcomes = body.get("outcomes")
            assert isinstance(outcomes, list) and len(outcomes) == 1, body
            assert outcomes[0]["child_session_id"] == child and outcomes[0]["run_id"] == run_id, outcomes[0]
            assert outcomes[0]["output"] == "reserved run completed", outcomes[0]
        finally:
            release_driver.set()
            release_child_scan.set()
            release_child_provider.set()
            parent_thread.join(timeout=30)


def test_core_cancel_wakes_waiting_sdk_child_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}
        child_started = threading.Event()
        release_child = threading.Event()
        condition_waiting = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "blocked child":
                child_started.set()
                if not release_child.wait(timeout=30):
                    raise TimeoutError("blocked child provider gate was not released")
                return _turn(output="cancelled child must not be reported as completed")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-blocked-child", WAIT_TOOL, action="spawn", prompt="blocked child"))
            if prompt == "wait":
                if last_role == "tool":
                    return _turn(output="wait ended")
                return _turn(_call("wait-cancelled", WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        assert child_started.wait(timeout=15)

        condition = core._delegation._child_finished_lock
        original_wait = condition.wait

        def observe_condition_wait(timeout: float | None = None):
            condition_waiting.set()
            return original_wait(timeout)

        monkeypatch.setattr(condition, "wait", observe_condition_wait)
        results: list[object] = []
        errors: list[str] = []

        def wait_for_child() -> None:
            try:
                results.append(core.run("wait", session_id=parent))
            except Exception as exc:  # noqa: BLE001 - preserve the real Core cancellation result
                errors.append(f"{type(exc).__name__}: {exc}")

        parent_thread = threading.Thread(target=wait_for_child, daemon=True)
        parent_thread.start()
        try:
            assert condition_waiting.wait(timeout=15), "SDK child wait did not block on the running child"
            core.cancel("cancel blocked child wait", cause="user_stop")
            parent_thread.join(timeout=15)
            assert not parent_thread.is_alive(), "Core.cancel did not wake the SDK child wait"
            assert not errors, errors
            assert len(results) == 1 and getattr(results[0], "status", None) == "interrupted", results
            assert getattr(results[0], "stop_cause", None) == "user_stop", results
        finally:
            release_child.set()
            parent_thread.join(timeout=30)
        cancelled = core.join_child(parent, child, timeout_seconds=15)
        assert cancelled.status == "cancelled"
        stop_event = next(event for event in core.store.events(child) if event.event_type == "core.run_interrupted")
        assert not core.store.has_observer_receipt(child, stop_event.sequence, CHILD_OUTCOME_RECEIPT_OWNER)


def test_wait_refuses_foreign_child_before_result_leak(tmp_path: Path) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "secret child":
                return _turn(output="secret foreign result")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-secret-child", WAIT_TOOL, action="spawn", prompt="secret child"))
            if prompt == "foreign selection":
                if last_role == "tool":
                    return _turn(output="foreign request rejected")
                return _turn(_call("foreign-wait", WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        owner = core.create_session("owner").session_id
        created = core.run("create", session_id=owner)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(owner) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        completed = core.join_child(owner, child, timeout_seconds=15)
        assert completed.status == "completed" and completed.output == "secret foreign result"
        terminal = next(event for event in core.store.events(owner) if event.event_type == "core.child_finished")

        foreign_parent = core.create_session("foreign").session_id
        result = core.run("foreign selection", session_id=foreign_parent)
        assert result.status == "completed", result.error
        request = server.requests[-1]
        messages = request.get("messages")
        assert isinstance(messages, list), request
        tool_content = next(
            (
                message.get("content")
                for message in messages
                if isinstance(message, Mapping) and message.get("role") == "tool" and message.get("tool_call_id") == "foreign-wait"
            ),
            None,
        )
        assert isinstance(tool_content, str), "foreign wait produced no model-visible tool result"
        projection = json.loads(tool_content)
        assert isinstance(projection, Mapping), projection
        assert projection.get("status") == "error", projection
        assert "secret foreign result" not in tool_content
        assert not core.store.has_observer_receipt(owner, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)


def test_user_steering_wins_over_racing_child_completion_without_consuming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}
        child_started = threading.Event()
        release_child = threading.Event()
        condition_waiting = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "racing child":
                child_started.set()
                if not release_child.wait(timeout=30):
                    raise TimeoutError("racing child provider gate was not released")
                return _turn(output="completion must survive steering")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-racing-child", WAIT_TOOL, action="spawn", prompt="racing child"))
            if prompt == "wait":
                if last_role == "tool":
                    return _turn(output="wait interrupted")
                return _turn(_call("wait-steering", WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            if prompt == "user steering":
                return _turn(output="steering processed")
            if prompt == "wait replay":
                if last_role == "tool":
                    return _turn(output="child delivered")
                return _turn(_call("wait-steering-replay", WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        assert child_started.wait(timeout=15)
        run_id = core.session(child).run_id
        assert run_id is not None

        condition = core._delegation._child_finished_lock
        original_wait = condition.wait

        def observe_condition_wait(timeout: float | None = None):
            condition_waiting.set()
            return original_wait(timeout)

        monkeypatch.setattr(condition, "wait", observe_condition_wait)
        results: list[object] = []
        errors: list[str] = []

        def wait_for_child() -> None:
            try:
                results.append(core.run("wait", session_id=parent))
            except Exception as exc:  # noqa: BLE001 - preserve the real Core invocation failure
                errors.append(f"{type(exc).__name__}: {exc}")

        parent_thread = threading.Thread(target=wait_for_child, daemon=True)
        parent_thread.start()
        try:
            assert condition_waiting.wait(timeout=15), "SDK wait did not block before steering"
            queued = core.run("user steering", session_id=parent)
            assert queued.status == "queued", queued
            release_child.set()
            parent_thread.join(timeout=30)
            assert not parent_thread.is_alive(), "queued steering did not wake the SDK wait"
            assert not errors, errors
            assert len(results) == 1, results
            terminal = next(
                event for event in core.store.events(parent) if event.event_type == "core.child_finished" and event.payload.get("run_id") == run_id
            )
            assert not core.store.has_observer_receipt(parent, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)

            replay = core.run("wait replay", session_id=parent)
            assert replay.status == "completed", replay.error
            body = _tool_result_body(core, server.requests[-1], "wait-steering-replay")
            outcomes = body.get("outcomes")
            assert isinstance(outcomes, list) and len(outcomes) == 1, body
            assert outcomes[0]["child_session_id"] == child and outcomes[0]["run_id"] == run_id, outcomes[0]
            assert outcomes[0]["output"] == "completion must survive steering", outcomes[0]
            assert core.store.has_observer_receipt(parent, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)
        finally:
            release_child.set()
            parent_thread.join(timeout=30)


def test_core_shutdown_wakes_blocked_sdk_child_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}
        child_started = threading.Event()
        release_child = threading.Event()
        condition_waiting = threading.Event()
        shutdown_entered = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "blocked shutdown child":
                child_started.set()
                if not release_child.wait(timeout=30):
                    raise TimeoutError("shutdown child provider gate was not released")
                return _turn(output="shutdown must not report a child result")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-shutdown-child", WAIT_TOOL, action="spawn", prompt="blocked shutdown child"))
            if prompt == "wait":
                if last_role == "tool":
                    return _turn(output="wait interrupted")
                return _turn(_call("wait-shutdown", WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child
        assert child_started.wait(timeout=15)

        condition = core._delegation._child_finished_lock
        original_wait = condition.wait

        def observe_condition_wait(timeout: float | None = None):
            condition_waiting.set()
            return original_wait(timeout)

        monkeypatch.setattr(condition, "wait", observe_condition_wait)
        original_close = core._delegation.close

        def observe_delegation_close() -> None:
            shutdown_entered.set()
            original_close()

        monkeypatch.setattr(core._delegation, "close", observe_delegation_close)
        results: list[object] = []
        errors: list[str] = []

        def wait_for_child() -> None:
            try:
                results.append(core.run("wait", session_id=parent))
            except Exception as exc:  # noqa: BLE001 - shutdown must propagate, not strand the wait
                errors.append(f"{type(exc).__name__}: {exc}")

        parent_thread = threading.Thread(target=wait_for_child, daemon=True)

        def close_core() -> None:
            try:
                core.close()
            except Exception as exc:  # noqa: BLE001 - preserve shutdown failures for the assertion
                errors.append(f"close {type(exc).__name__}: {exc}")

        close_thread = threading.Thread(target=close_core, daemon=True)
        parent_thread.start()
        try:
            assert condition_waiting.wait(timeout=15), "SDK child wait did not block before shutdown"
            close_thread.start()
            assert shutdown_entered.wait(timeout=15), "Core.close did not reach delegation shutdown"
            release_child.set()
            parent_thread.join(timeout=30)
            close_thread.join(timeout=30)
            assert not parent_thread.is_alive(), "Core shutdown stranded the SDK child wait"
            assert not close_thread.is_alive(), "Core shutdown did not finish after releasing the child"
            assert len(results) + len(errors) == 1, (results, errors)
            if results:
                assert getattr(results[0], "status", None) == "interrupted", results
            else:
                assert errors[0].startswith(("InterruptedError:", "RuntimeError:")), errors
        finally:
            release_child.set()
            parent_thread.join(timeout=30)
            if close_thread.ident is not None:
                close_thread.join(timeout=30)


def test_wait_delivers_failed_child_run_once(tmp_path: Path) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "failed child":
                raise ConnectionError("provider disconnected")
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-failed-child", WAIT_TOOL, action="spawn", prompt="failed child"))
            if prompt in ("wait", "wait again"):
                if last_role == "tool":
                    return _turn(output="wait complete")
                call_id = "wait-failed" if prompt == "wait" else "wait-failed-again"
                return _turn(_call(call_id, WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server)
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child

        failed = core.join_child(parent, child, timeout_seconds=30)
        assert failed.status == "failed", failed.as_payload()
        assert failed.run_id is not None
        delivered = core.run("wait", session_id=parent)
        assert delivered.status == "completed", delivered.error
        body = _tool_result_body(core, server.requests[-1], "wait-failed")
        outcomes = body.get("outcomes")
        assert isinstance(outcomes, list) and len(outcomes) == 1, body
        assert outcomes[0]["child_session_id"] == child
        assert outcomes[0]["run_id"] == failed.run_id
        assert outcomes[0]["status"] == "failed"
        assert isinstance(outcomes[0]["error"], str) and outcomes[0]["error"]

        repeated = core.run("wait again", session_id=parent)
        assert repeated.status == "completed", repeated.error
        repeated_body = _tool_result_body(core, server.requests[-1], "wait-failed-again")
        assert repeated_body.get("outcomes") == [], repeated_body
        terminal = next(
            event for event in core.store.events(parent) if event.event_type == "core.child_finished" and event.payload.get("run_id") == failed.run_id
        )
        assert core.store.has_observer_receipt(parent, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)


def test_approval_parked_child_stays_pending_until_authorized_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([]) as server:
        child_holder: dict[str, str] = {}
        condition_waiting = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            prompt = _last_user_text(request)
            last_role = _last_message_role(request)
            if prompt == "approval child":
                if last_role == "tool":
                    return _turn(output="approval child complete")
                return _turn(_call("approval-probe", PROBE_TOOL))
            if prompt == "create":
                if last_role == "tool":
                    return _turn(output="parent complete")
                return _turn(_call("create-approval-child", WAIT_TOOL, action="spawn", prompt="approval child"))
            if prompt == "wait":
                if last_role == "tool":
                    return _turn(output="wait complete")
                return _turn(_call("wait-approval", WAIT_TOOL, action="wait", child_id=child_holder["child"]))
            return _turn(output=f"unexpected request: {prompt!r}")

        server.before_reply = reply
        core = _open(tmp_path, server, approval_mode="ask")
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        created = core.run("create", session_id=parent)
        assert created.status == "waiting", created
        parent_approval = next(event for event in core.store.events(parent) if event.event_type == "core.approval_requested")
        created = core.resume(parent, approval_request_id=str(parent_approval.payload["request_id"]), decision="allow")
        assert created.status == "completed", created.error
        child = str(next(event.payload["child_session_id"] for event in core.store.events(parent) if event.event_type == "core.child_spawned"))
        child_holder["child"] = child

        parked = core.join_child(parent, child, timeout_seconds=15)
        assert parked.status == "waiting", parked.as_payload()
        approval = next(event for event in core.store.events(child) if event.event_type == "core.approval_requested")

        wait_pending = core.run("wait", session_id=parent)
        assert wait_pending.status == "waiting", wait_pending
        wait_approval = [event for event in core.store.events(parent) if event.event_type == "core.approval_requested"][-1]

        condition = core._delegation._child_finished_lock
        original_wait = condition.wait

        def observe_condition_wait(timeout: float | None = None):
            condition_waiting.set()
            return original_wait(timeout)

        monkeypatch.setattr(condition, "wait", observe_condition_wait)
        results: list[object] = []
        errors: list[str] = []

        def resume_wait_tool() -> None:
            try:
                results.append(
                    core.resume(
                        parent,
                        approval_request_id=str(wait_approval.payload["request_id"]),
                        decision="allow",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserve real invocation failures
                errors.append(f"{type(exc).__name__}: {exc}")

        parent_thread = threading.Thread(target=resume_wait_tool, daemon=True)
        parent_thread.start()
        try:
            assert condition_waiting.wait(timeout=15), "wait did not remain parked on approval"
            assert not results and not errors, (results, errors)
            resumed = core.resume(
                child,
                approval_request_id=str(approval.payload["request_id"]),
                decision="allow",
            )
            assert resumed.status == "completed", resumed.error
            assert resumed.run_id is not None
            parent_thread.join(timeout=30)
            assert not parent_thread.is_alive(), "authorized resume did not wake the blocked wait"
            assert not errors, errors
            assert len(results) == 1 and getattr(results[0], "status", None) == "completed", results
            wait_request = next(
                request
                for request in reversed(server.requests)
                if isinstance(request.get("messages"), list)
                and any(
                    isinstance(message, Mapping) and message.get("role") == "tool" and message.get("tool_call_id") == "wait-approval"
                    for message in request["messages"]
                )
            )
            body = _tool_result_body(core, wait_request, "wait-approval")
            outcomes = body.get("outcomes")
            assert isinstance(outcomes, list) and len(outcomes) == 1, body
            assert outcomes[0]["run_id"] == resumed.run_id
            assert outcomes[0]["status"] == "completed"
            assert outcomes[0]["output"] == "approval child complete"
            terminal = next(
                event
                for event in core.store.events(parent)
                if event.event_type == "core.child_finished" and event.payload.get("run_id") == resumed.run_id
            )
            assert core.store.has_observer_receipt(parent, terminal.sequence, CHILD_OUTCOME_RECEIPT_OWNER)
        finally:
            if parent_thread.is_alive():
                core.cancel("abort approval wait test", cause="user_stop")
            parent_thread.join(timeout=30)


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
        core = _open(tmp_path, server, approval_mode="ask")
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
        core = _open(tmp_path, server)
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
        started = [event for event in core.events_after(parent) if event.event_type == "core.continuation_started"]
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
        monkeypatch.setattr(core._delegation, "_publish_child_terminal", lambda *args, **kwargs: None)
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


def test_legacy_child_without_captured_root_publishes_fact_without_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rootless birth suppresses only the wake-up: the fact still publishes, join reads it."""
    replies = [_turn(output="legacy output")]
    with _server(replies) as server:
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id

        # Real terminal run first (publication stubbed, as in the restart test above).
        monkeypatch.setattr(core._delegation, "_publish_child_terminal", lambda *args, **kwargs: None)
        child = core.spawn_child(parent, "child prompt", notify_on_completion=True)
        _wait_for(lambda: core.session(child).status == "completed", what="the child's terminal commit")
        run_id = core.session(child).run_id
        assert run_id is not None
        assert _child_facts(core, parent) == []
        monkeypatch.undo()

        # The store refuses to persist a rootless birth, so the legacy signal is
        # staged at the exact seam the branch handles: root resolution raises.
        def _legacy_root(child_id: str) -> str:
            raise StoreConflict(f"child {child_id!r} has no captured root action identity")

        monkeypatch.setattr(core._delegation, "_captured_child_root_id", _legacy_root)
        core._delegation._recover_child_publications()

        facts = _child_facts(core, parent)
        assert len(facts) == 1
        fact = facts[0]
        assert fact["run_id"] == run_id
        assert fact["status"] == "completed"
        assert fact["output"] == "legacy output"
        assert fact["notify_on_completion"] is False
        assert "notice_id" not in fact
        assert core.store.continuation_status(child_completion_notice_id(parent, child, run_id)) is None
        assert len(server.requests) == 1, "no notice turn ran and the child never reran"

        joined = core.join_child(parent, child, timeout_seconds=15)
        assert (joined.status, joined.output, joined.run_id) == ("completed", "legacy output", run_id)


def test_send_authorization_follows_the_current_stance(tmp_path: Path) -> None:
    replies = [
        _turn(output="child one done"),
        _turn(output="child two done"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server)
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


def _open_bundled(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
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
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _completions(core: Core, session_id: str, tool: str) -> list[dict[str, object]]:
    return [
        dict(event.payload)
        for event in core.events_after(session_id)
        if event.event_type == "core.tool_completed"
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
    original = core._delegation._publish_child_terminal
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

    core._delegation._publish_child_terminal = gated_publish  # type: ignore[method-assign]
    try:
        yield ready
    finally:
        core._delegation._publish_child_terminal = original  # type: ignore[method-assign]


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
    original = core._delegation._child_fact_payload
    used = False

    def gated_payload(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal used
        if used:
            return original(*args, **kwargs)  # type: ignore[arg-type]
        used = True
        ready.set()
        assert release.wait(timeout=30), "payload release never arrived"
        return original(*args, **kwargs)  # type: ignore[arg-type]

    core._delegation._child_fact_payload = gated_payload  # type: ignore[method-assign]
    try:
        yield ready
    finally:
        core._delegation._child_fact_payload = original  # type: ignore[method-assign]


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
                if session_id == parent and "core.child_finished" in kinds:
                    fact_committed.set()
                if not gated_once and session_id == parent and "core.model_turn" in kinds:
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
        assert kinds.count("core.child_finished") == 1
        events = list(core.events_after(parent))
        first_turn = next(event.sequence for event in events if event.event_type == "core.model_turn")
        fact_seq = next(event.sequence for event in events if event.event_type == "core.child_finished")
        first_intent = next(event.sequence for event in events if event.event_type == "core.tool_intent")
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
        core = _open_bundled(tmp_path, server)
        parent = core.create_session("parent").session_id

        release = threading.Event()
        fact_committed = threading.Event()
        first_batch_reported = threading.Event()
        with _gated_publication(core, release) as publication_ready:
            child = core.spawn_child(parent, "CHILD TASK", notify_on_completion=True)
            assert publication_ready.wait(timeout=30), "child publication never reached the gate"
            with _commit_signals(core, parent, "core.tool_completed", "core.child_finished") as signals:
                original_claim = core.store.claim_child_messages
                merge_gate_used = False

                def gated_claim(*args: object, **kwargs: object) -> tuple[object, ...]:
                    nonlocal merge_gate_used
                    if not first_batch_reported.is_set() or merge_gate_used:
                        return original_claim(*args, **kwargs)  # type: ignore[return-value,arg-type]
                    merge_gate_used = True
                    release.set()
                    assert signals["core.child_finished"].wait(timeout=30), "child fact never committed at merge entry"
                    fact_committed.set()
                    return original_claim(*args, **kwargs)  # type: ignore[return-value,arg-type]

                core.store.claim_child_messages = gated_claim  # type: ignore[method-assign]
                try:

                    def mark_first_batch() -> None:
                        assert signals["core.tool_completed"].wait(timeout=30), "first batch never reported"
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
        assert kinds.index("core.child_finished") < kinds.index("core.continuation_started"), "the fact precedes its claimed notice"
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
        core = _open_bundled(tmp_path, server)
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
                if session_id == parent and "core.child_finished" in kinds:
                    fact_committed.set()
                if not gated and session_id == parent and "core.provider_intent" in kinds:
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
        assert "core.provider_intent" in kinds
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

        result = core.run("recurse until the root gives out", session_id=parent, root_action_budget=4)
        assert result.status == "interrupted", result.error
        assert result.stop_cause == "limit_reached"
        assert result.error == "root action budget exhausted: consumed 4 of 4, remaining 0"
        assert result.budget_exhaustion is not None
        assert result.budget_exhaustion.dimension == "root_actions"
        checkpoint = core.store.checkpoint(parent)
        assert checkpoint is not None
        assert core.store.root_budget_state(str(checkpoint.payload["root_id"])) == (4, 4)
        assert len(server.requests) == 1, "one dispatch; the chain dies on the durable budget, not a race"


def test_child_budget_stop_is_typed_from_exact_event_across_reopen_and_resume(tmp_path: Path) -> None:
    replies = [
        _turn(output="parent action completed"),
        _turn(output="child completed after the budget extension"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id
        parent_run = core.run("establish a finite root", session_id=parent, root_action_budget=1)
        assert parent_run.status == "completed", parent_run.error
        checkpoint = core.store.checkpoint(parent)
        assert checkpoint is not None
        root_action_id = checkpoint.payload.get("root_id")
        assert isinstance(root_action_id, str)
        assert core.store.root_budget_state(root_action_id) == (1, 1)
        child = core.spawn_child(
            parent,
            "budgeted child",
            notify_on_completion=False,
            root_action_id=root_action_id,
            initial_root_budget=1,
        )
        stopped = core.join_child(parent, child, timeout_seconds=30)

        assert stopped.status == "interrupted", stopped.as_payload()
        assert stopped.run_id is not None
        assert stopped.stop_cause == "limit_reached"
        budget = stopped.budget_exhaustion
        assert budget is not None
        assert budget.as_payload() == {
            "dimension": "root_actions",
            "source": "root_action_budget",
            "limit": 1,
            "consumed": 1,
            "remaining": 0,
            "continue_with": "extend_root_actions",
        }
        assert stopped.as_payload()["budget_exhaustion"] == budget.as_payload()
        stop_payload = core.store.latest_stop_payload(child, stopped.run_id)
        assert stop_payload is not None
        assert budget.as_payload() == stop_payload["budget_exhaustion"]
        assert core.store.root_budget_state(root_action_id) == (1, 1)
        assert stopped.error == stop_payload["reason"]
        assert _child_facts(core, parent) == [], "a recoverable stop is not a terminal child fact"

        run_id = stopped.run_id
        core.close()
        _CREATED.remove(core)

        reopened = _open(tmp_path, server)
        recovered = reopened.join_child(parent, child, timeout_seconds=0)
        assert recovered.status == "interrupted"
        assert recovered.run_id == run_id
        assert recovered.budget_exhaustion == budget
        assert recovered.error == stopped.error
        assert reopened.store.root_budget_state(root_action_id) == (1, 1)

        resumed = reopened.resume(child, extend_root_actions=1, root_action_grant_id="resume-child-budget")
        assert resumed.status == "completed", resumed.error
        assert resumed.run_id is not None and resumed.run_id != run_id, "public resume starts a new run identity"
        assert reopened.store.root_budget_state(root_action_id) == (2, 2)
        old_stop = reopened.store.latest_stop_payload(child, run_id)
        assert old_stop is not None and old_stop["budget_exhaustion"] == budget.as_payload()
        terminal = reopened.join_child(parent, child, timeout_seconds=0)
        assert terminal.status == "completed"
        assert terminal.run_id == resumed.run_id
        assert terminal.budget_exhaustion is None
        facts = _child_facts(reopened, parent)
        assert len(facts) == 1 and facts[0]["run_id"] == resumed.run_id
        assert "budget_exhaustion" not in facts[0], "the resumed terminal fact does not inherit the old pause"
        assert len(server.requests) == 2, "the child resumes at the committed checkpoint without replay"


def test_join_during_publication_waits_and_never_returns_an_empty_completed(tmp_path: Path) -> None:
    """A terminal child whose fact is in flight: join waits; timeout says running, not completed."""
    replies = [
        _turn(output="child answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server)
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


def test_join_returns_promptly_when_child_completes_mid_timeout(tmp_path: Path) -> None:
    """A completion wake ends the join; it never sleeps out the deadline."""
    with _server([]) as server:
        child_started = threading.Event()
        release_child = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            child_started.set()
            if not release_child.wait(timeout=60):
                raise TimeoutError("gated child provider gate was not released")
            return _turn(output="gated child answer")

        server.before_reply = reply
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id
        child = core.spawn_child(parent, "gated child task")
        assert child_started.wait(timeout=15), "child run never reached the provider"
        releaser = threading.Timer(2.0, release_child.set)
        releaser.daemon = True
        releaser.start()
        started = time.monotonic()
        try:
            outcome = core.join_child(parent, child, timeout_seconds=60)
        finally:
            releaser.cancel()
            release_child.set()
        elapsed = time.monotonic() - started
        assert (outcome.status, outcome.output) == ("completed", "gated child answer")
        assert elapsed < 30, f"join slept past the completion wake: {elapsed:.1f}s of a 60s deadline"


def test_join_timeout_returns_the_live_running_outcome(tmp_path: Path) -> None:
    """An unfinished child at the deadline reports running with its live run, never an error."""
    with _server([]) as server:
        child_started = threading.Event()
        release_child = threading.Event()

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "exact-model"}]}
            child_started.set()
            if not release_child.wait(timeout=60):
                raise TimeoutError("gated child provider gate was not released")
            return _turn(output="late child answer")

        server.before_reply = reply
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id
        child = core.spawn_child(parent, "slow child task")
        assert child_started.wait(timeout=15), "child run never reached the provider"
        # A hang-breaker the deadline must beat: reaching it fails the assertions below.
        breaker = threading.Timer(30.0, release_child.set)
        breaker.daemon = True
        breaker.start()
        started = time.monotonic()
        try:
            outcome = core.join_child(parent, child, timeout_seconds=3)
        finally:
            breaker.cancel()
        elapsed = time.monotonic() - started
        assert outcome.status == "running", outcome.as_payload()
        assert outcome.output is None
        assert outcome.run_id == core.session(child).run_id, "the expiry outcome names the live run"
        assert elapsed < 30, f"join slept past its deadline: {elapsed:.1f}s for a 3s timeout"
        assert elapsed >= 2.0, f"join returned without waiting out its deadline: {elapsed:.1f}s for a 3s timeout"
        release_child.set()
        done = _join(core, parent, child)
        assert (done.status, done.output) == ("completed", "late child answer")


def test_join_after_restart_returns_the_durable_outcome_without_rerunning(tmp_path: Path) -> None:
    """Close/reopen with a persisted parent fact: join returns it and the child never reruns."""
    replies = [
        _turn(output="only answer"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id
        child = core.spawn_child(parent, "single child task", notify_on_completion=False)
        first = _join(core, parent, child)
        assert (first.status, first.output) == ("completed", "only answer")
        assert first.history_sha256
        requests_before = len(server.requests)
        assert requests_before == 1
        core.close()
        _CREATED.remove(core)

        reopened = _open(tmp_path, server)
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
        core = _open(tmp_path, server)
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
        terminal_seq = next(event.sequence for event in events if event.event_type == "core.run_completed")
        late_seq = next(event.sequence for event in events if event.event_type == "core.child_finished")
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
        core = _open(tmp_path, server)
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
        _turn(output="parent second notice"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server)
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
            str(event.payload.get("continuation_id")) for event in reopened.events_after(parent) if event.event_type == "core.continuation_started"
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
        core = _open(tmp_path, server)
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
        started_runs = {event.run_id for event in child_events if event.event_type == "core.continuation_started"}
        assert started_runs == {started[0].run_id}, "exactly one follow-up run was claimed"
        followup_intents = [event for event in child_events if event.event_type == "core.provider_intent" and event.run_id != initial.run_id]
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
        core = _open(tmp_path, server)
        parent = core.create_session("parent").session_id
        assert DELEGATION_SPAWN_BUDGET == 4
        for index in range(3):
            child = core.spawn_child(parent, f"setup child {index}", notify_on_completion=False)
            assert _join(core, parent, child).status == "completed"
        assert core.store.count_root_children(parent) == 3

        contender_barrier = threading.Barrier(2, timeout=30)
        original_create = core.store.create_child_session
        mainstream = threading.get_ident()
        captured: dict[str, int] = {}

        def gated_create(session_id: str, *args: object, **kwargs: object) -> object:
            count = core.store.count_root_children(parent)
            if threading.get_ident() != mainstream and count == 3:
                captured[threading.current_thread().name] = count
                contender_barrier.wait()
            return original_create(session_id, *args, **kwargs)  # type: ignore[arg-type]

        core.store.create_child_session = gated_create  # type: ignore[method-assign]
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
            core.store.create_child_session = original_create  # type: ignore[method-assign]
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
        core = _open(tmp_path, server, approval_mode="ask")
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
        core = _open(tmp_path, server)
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
        core = _open(tmp_path, server)
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

        reopened = _open(tmp_path, server)
        assert {str(fact["run_id"]) for fact in _child_facts(reopened, parent)} == {receipt.run_id}, (
            "A's fact is genuinely absent before recovery runs"
        )
        reopened._delegation._recover_child_publications()
        facts_after = {str(fact["run_id"]): fact for fact in _child_facts(reopened, parent)}
        assert set(facts_after) == {run_a, receipt.run_id}, "A recovered; B untouched"
        assert facts_after[run_a]["output"] == "A answer"
        assert facts_after[receipt.run_id]["output"] == "B answer"
        assert len(server.requests) == 2, "recovery republishes facts; it never reruns the child"
        joined = reopened.join_child(parent, child, timeout_seconds=15)
        assert (joined.status, joined.output) == ("completed", "B answer")
        assert joined.run_id == receipt.run_id, "join answers for the current run"
