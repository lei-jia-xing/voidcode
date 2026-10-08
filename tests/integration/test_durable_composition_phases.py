from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.runtime_storage import repositories_for_test_store
from voidcode.runtime.composition import ComponentSelection
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.execution_ownership import EXECUTION_OWNERSHIP, ExecutionOwnershipRevokedError
from voidcode.runtime.permission import ExternalDirectoryPermissionConfig, PatternPermissionRule
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage.sqlite import SqliteSessionStore

_PACKAGE_CODE = """from pathlib import Path
from dataclasses import dataclass
from voidcode.core.transcript import output_text
from voidcode.provider.models import ProviderDescriptor
from voidcode.provider.protocol import ProviderStreamEvent, ProviderTurnResult
from voidcode.runtime.composition import PhaseCompleted, PhaseFailed, ValidatedConfig
from voidcode.tools.contracts import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolSuccess

REVOKE = None
FAILURE = None

def validate_provider(config):
    return ValidatedConfig(value=ProviderDescriptor(provider_name="phase_provider", configuration=dict(config)),
                           snapshot=dict(config))

@dataclass(frozen=True)
class Definition(ToolDefinition):
    log_path: str = ""

def validate_tool(config):
    return ValidatedConfig(value=Definition(name="phase_tool", description="Stateful package tool",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        effects=frozenset({ToolEffect.WRITE}), log_path=config["log"]), snapshot=dict(config))

def log(instance, context, name):
    with Path(instance.log_path).open("a") as stream:
        stream.write(f"{context.session_id}:{name}\\n")

class Provider:
    name = "phase_provider"
    def __init__(self, config):
        self.log_path = config["log"]
        self.turn = 0
    def turn_provider(self):
        return self
    def propose_turn(self, request):
        assert self.turn > 0, "provider phase did not configure the actual provider"
        results = request.assembled_context.tool_results
        if self.turn == 1:
            return ProviderTurnResult(tool_calls=tuple(
                ToolCall(tool_call_id=name, tool_name="phase_tool", arguments={"name": name})
                for name in ("a", "b")
            ), done_reason="tool_calls", finish_reason_reported=True)
        outputs = [output_text(result.output) for result in results if result.tool_name == "phase_tool"][-2:]
        assert outputs == ["a:turn=1:completed=0:armed=a", "b:turn=1:completed=1:armed=b"], outputs
        assert self.turn == 2, self.turn
        return ProviderTurnResult(output="FINAL: " + " | ".join(outputs) + "; provider_turn=2",
                                  done_reason="stop", finish_reason_reported=True)
    def stream_turn(self, request):
        result = self.propose_turn(request)
        for call in result.tool_calls:
            yield ProviderStreamEvent(kind="tool_call_start", tool_call_id=call.tool_call_id, tool_name=call.tool_name)
            yield ProviderStreamEvent(kind="tool_call_end", tool_call_id=call.tool_call_id,
                                      tool_name=call.tool_name, parsed_arguments=dict(call.arguments))
        if result.output:
            yield ProviderStreamEvent(kind="content", text=result.output)
        yield ProviderStreamEvent(kind="done", done_reason=result.done_reason)

class Tool:
    def __init__(self, definition):
        self.log_path = definition.log_path
        self.turn = 0
        self.completed = 0
        self.armed = ""
        self.definition = definition
    def invoke(self, call, *, context):
        name = call.arguments["name"]
        assert self.turn == 1 and self.armed == name
        assert self.completed == (0 if name == "a" else 1)
        log(self, context, "invoke:" + name)
        return ToolSuccess(tool_name="phase_tool", output=TextOutput(
            f"{name}:turn={self.turn}:completed={self.completed}:armed={self.armed}"))

def create_provider(value, context):
    return Provider(value.configuration)

def create_tool(value, context):
    return Tool(value)

def validate_input(value):
    return value

def state(instance):
    result = {"turn": instance.turn}
    if isinstance(instance, Tool):
        result.update(completed=instance.completed, armed=instance.armed)
    return result

def before_turn(value, instance, context):
    instance.turn += 1
    # A fresh producer turn must not inherit an earlier run's tool state.
    if isinstance(instance, Tool):
        instance.completed = 0
        instance.armed = ""
    log(instance, context, type(instance).__name__ + ":before_turn:" + str(instance.turn))
    if REVOKE is not None:
        REVOKE()
    return state(instance)

def before_tool(value, instance, context):
    instance.armed = context.scope_id.rsplit(":", 1)[-1]
    log(instance, context, "before_tool:" + instance.armed)
    return state(instance)

def after_tool(value, instance, context):
    instance.completed += 1
    log(instance, context, "after_tool:" + instance.armed)
    return state(instance)

def lifecycle(name, instance, context):
    log(instance, context, name)
    if FAILURE == name:
        raise RuntimeError("intentional lifecycle failure")
    return {"lifecycle": name}

def session_start(value, instance, context):
    return lifecycle("session_start", instance, context)

def before_run(value, instance, context):
    return lifecycle("before_run", instance, context)

def after_turn(value, instance, context):
    return lifecycle("after_turn", instance, context)

def after_run(value, instance, context):
    return lifecycle("after_run", instance, context)

def session_end(value, instance, context):
    return lifecycle("session_end", instance, context)

def snapshot(outcome):
    if isinstance(outcome, PhaseFailed):
        return {"error": outcome.error}
    assert isinstance(outcome, PhaseCompleted)
    return outcome.value

def restore(payload, instance, context):
    if "error" in payload:
        return payload["error"]
    for key, value in payload.items():
        setattr(instance, key, value)
    return state(instance)
"""


@pytest.fixture
def installed_phases(tmp_path: Path, request: pytest.FixtureRequest | None = None) -> Iterator[Any]:
    wheel = tmp_path / "phase_pkg-1.0.0-py3-none-any.whl"
    info = "phase_pkg-1.0.0.dist-info"
    phases = []
    for name, trigger, slot, component, order in (
        ("provider_turn", "before_turn", "provider", "phase_provider", 10),
        ("tool_turn", "before_turn", "tool", "phase_tool", 20),
        ("tool_before", "before_tool", "tool", "phase_tool", 10),
        ("tool_after", "after_tool", "tool", "phase_tool", 10),
        ("session_start", "session_start", "provider", "phase_provider", 10),
        ("before_run", "before_run", "provider", "phase_provider", 10),
        ("after_turn", "after_turn", "provider", "phase_provider", 10),
        ("after_run", "after_run", "provider", "phase_provider", 10),
        ("session_end", "session_end", "provider", "phase_provider", 10),
    ):
        phases.append(
            {
                "name": name,
                "trigger": trigger,
                "slot": slot,
                "component": component,
                "callback": f"phase_pkg.components:{trigger}",
                "validate_input": "phase_pkg.components:validate_input",
                "snapshot": "phase_pkg.components:snapshot",
                "restore": "phase_pkg.components:restore",
                "scope": (
                    "session"
                    if trigger in {"session_start", "session_end"}
                    else "run"
                    if trigger in {"before_run", "after_run"}
                    else "turn"
                    if trigger in {"before_turn", "after_turn"}
                    else "tool"
                ),
                "order": order,
                "version": "1",
                "failure": (getattr(request, "param", "raise") if trigger == "after_turn" else "raise"),
                "replay": "once",
            }
        )
    manifest = {
        "schema_version": 1,
        "files": ["phase_pkg/__init__.py", "phase_pkg/components.py"],
        "dependencies": [],
        "phases": phases,
        "components": [
            {"slot": slot, "name": name, "factory": f"phase_pkg.components:create_{slot}", "validate_config": f"phase_pkg.components:validate_{slot}"}
            for slot, name in (("provider", "phase_provider"), ("tool", "phase_tool"))
        ],
    }
    contents = {
        "phase_pkg/__init__.py": "",
        "phase_pkg/components.py": _PACKAGE_CODE,
        f"{info}/METADATA": "Metadata-Version: 2.1\nName: phase-pkg\nVersion: 1.0.0\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/voidcode-components.json": json.dumps(manifest),
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        archive.writestr(f"{info}/RECORD", "".join(f"{name},,\n" for name in contents) + f"{info}/RECORD,,\n")
    site = tmp_path / "site"
    subprocess.run(["uv", "pip", "install", "--no-deps", "--target", str(site), str(wheel)], check=True, capture_output=True)
    sys.path.insert(0, str(site))
    try:
        yield importlib.import_module("phase_pkg.components")
    finally:
        sys.path.remove(str(site))
        for name in tuple(sys.modules):
            if name == "phase_pkg" or name.startswith("phase_pkg."):
                del sys.modules[name]


def _config(log: Path, decision: str = "allow") -> RuntimeConfig:
    return RuntimeConfig(
        model="phase_provider/test",
        execution_engine="provider",
        approval_mode="yolo",
        mcp=RuntimeMcpConfig(enabled=False),
        permission=ExternalDirectoryPermissionConfig(rules=(PatternPermissionRule(decision=decision, tool="phase_tool"),)),
        components=tuple(
            ComponentSelection(distribution="phase-pkg", slot=slot, name=name, configuration={"log": str(log)}, provenance=("test",))
            for slot, name in (("provider", "phase_provider"), ("tool", "phase_tool"))
        ),
    )


_FINAL = "FINAL: a:turn=1:completed=0:armed=a | b:turn=1:completed=1:armed=b; provider_turn=2"


def _expected(session: str, *, resumed: bool = False, session_lifecycle: bool = True) -> list[str]:
    entries = (["session_start"] if session_lifecycle else []) + [
        "before_run",
        "Provider:before_turn:1",
        "Tool:before_turn:1",
        "before_tool:a",
        "invoke:a",
        "after_tool:a",
    ]
    if resumed:
        entries.append("before_run")
    entries.extend(
        [
            "before_tool:b",
            "invoke:b",
            "after_tool:b",
            "after_turn",
            "Provider:before_turn:2",
            "Tool:before_turn:2",
            "after_turn",
            "after_run",
        ]
    )
    if session_lifecycle:
        entries.append("session_end")
    return [f"{session}:{entry}" for entry in entries]


def _log(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _interrupt_after_a(runtime: VoidCodeRuntime, session: str) -> None:
    stream = runtime.run_stream(RuntimeRequest(prompt="run a and b", session_id=session))
    try:
        for chunk in stream:
            if chunk.event is not None and chunk.event.event_type == "runtime.tool_completed":
                assert chunk.event.payload["tool_call_id"] == "a"
                runtime.interrupt_active_run(session, reason="test stops after durable a")
                break
        else:
            pytest.fail("the native provider never completed a")
    finally:
        stream.close()


def test_installed_phase_state_restores_pending_batch_and_advances_cursor(installed_phases: Any, tmp_path: Path) -> None:
    log = tmp_path / "callbacks.log"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "sessions.sqlite3"
    config = _config(log)
    store = SqliteSessionStore(database_path=database)
    repos = repositories_for_test_store(store)
    with VoidCodeRuntime(workspace=workspace, config=config, repositories=repos) as runtime:
        _interrupt_after_a(runtime, "first")
    assert _log(log) == _expected("first")[:7]
    interrupted = repos.sessions.load_session(workspace=workspace, session_id="first")
    assert interrupted.session.status == "interrupted"
    assert interrupted.session.metadata["runtime_state"]["composition_turn_occurrence"] == 1
    checkpoint = repos.recovery.load_resume_checkpoint(workspace=workspace, session_id="first")
    assert checkpoint is not None and checkpoint["kind"] == "interrupted"
    first_records = interrupted.session.metadata["phase_records"]
    assert all(record["state"] == "completed" for record in first_records.values())

    # Reopen the actual SQLite owner and construct new package instances.
    reopened = repositories_for_test_store(SqliteSessionStore(database_path=database))
    with VoidCodeRuntime(workspace=workspace, config=config, repositories=reopened) as runtime:
        chunks = list(runtime.resume_stream("first"))
        assert [chunk.output for chunk in chunks if chunk.output is not None] == [_FINAL]
        assert _log(log) == _expected("first", resumed=True)
        restored = reopened.sessions.load_session(workspace=workspace, session_id="first")
        assert restored.session.status == "completed"
        assert restored.session.metadata["runtime_state"]["composition_turn_occurrence"] == 2
        records = restored.session.metadata["phase_records"]
        assert all(record["state"] == "completed" for record in records.values())
        assert all(records[key] == value for key, value in first_records.items())
        assert [event.payload["tool_call_id"] for event in restored.events if event.event_type == "runtime.tool_completed"] == ["a", "b"]
        # Interleave sessions on the same runtime: advance interleaved_a through request_received,
        # run second to completion, then finish interleaved_a to verify session instances never collide.
        stream_a = runtime.run_stream(RuntimeRequest(prompt="run a and b", session_id="interleaved_a"))
        chunks_a_prefix = []
        for chunk in stream_a:
            chunks_a_prefix.append(chunk)
            if chunk.event is not None and chunk.event.event_type == "runtime.request_received":
                break
        else:
            pytest.fail("interleaved_a never emitted request_received")

        other = list(runtime.run_stream(RuntimeRequest(prompt="run a and b", session_id="second")))
        assert [chunk.output for chunk in other if chunk.output is not None] == [_FINAL]

        chunks_a_rest = list(stream_a)
        stream_a.close()
        assert [chunk.output for chunk in (chunks_a_prefix + chunks_a_rest) if chunk.output is not None] == [_FINAL]
        assert _log(log) == _expected("first", resumed=True) + _expected("second") + _expected("interleaved_a")
        second = reopened.sessions.load_session(workspace=workspace, session_id="second")
        assert second.session.metadata["composition_ref"]["binding_id"] == restored.session.metadata["composition_ref"]["binding_id"]
        # A new run in the same session must not reuse the old run's instances.
        fresh = list(runtime.run_stream(RuntimeRequest(prompt="run a and b again", session_id="first")))
        assert [chunk.output for chunk in fresh if chunk.output is not None] == [_FINAL]
        expected_fresh = (
            _expected("first", resumed=True) + _expected("second") + _expected("interleaved_a") + _expected("first", session_lifecycle=False)
        )
        assert _log(log) == expected_fresh
        latest = reopened.sessions.load_session(workspace=workspace, session_id="first")
        assert latest.session.metadata["runtime_state"]["composition_turn_occurrence"] == 4


@pytest.mark.parametrize("decision", ["deny", "ask"])
def test_tool_phases_do_not_run_before_governance_authorizes(installed_phases: Any, tmp_path: Path, decision: str) -> None:
    log = tmp_path / "callbacks.log"
    repos = repositories_for_test_store(SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3"))
    with VoidCodeRuntime(workspace=tmp_path, config=_config(log, decision), repositories=repos) as runtime:
        stream = runtime.run_stream(RuntimeRequest(prompt="run a and b", session_id="governance"))
        try:
            for chunk in stream:
                if (
                    chunk.event is not None
                    and chunk.event.event_type in {"runtime.approval_resolved", "runtime.approval_requested"}
                    and chunk.event.payload.get("decision") == decision
                ):
                    break
            else:
                pytest.fail(f"no {decision} governance decision was emitted")
        finally:
            stream.close()
        assert _log(log) == _expected("governance")[:4]
        if decision == "ask":
            assert chunk.event is not None
            resumed = runtime.resume_stream("governance", approval_request_id=chunk.event.payload["request_id"], approval_decision="deny")
            try:
                for resumed_chunk in resumed:
                    if resumed_chunk.event is not None and resumed_chunk.event.event_type == "runtime.approval_resolved":
                        assert resumed_chunk.event.payload["decision"] == "deny"
                        break
                else:
                    pytest.fail("approval resume did not apply the denial")
            finally:
                resumed.close()
    expected = _expected("governance")[:4]
    if decision == "ask":
        expected.append("governance:before_run")
    assert _log(log) == expected
    records = repos.sessions.load_session(workspace=tmp_path, session_id="governance").session.metadata["phase_records"]
    assert all(record["scope"] != "tool" for record in records.values())


def test_real_lease_revocation_refuses_callback_completion(installed_phases: Any, tmp_path: Path) -> None:
    log = tmp_path / "callbacks.log"
    repos = repositories_for_test_store(SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3"))
    task_id = "phase-owner"
    lease = EXECUTION_OWNERSHIP.grant(workspace=tmp_path, task_id=task_id)
    installed_phases.REVOKE = lambda: EXECUTION_OWNERSHIP.revoke(workspace=tmp_path, task_id=task_id, reason="phase callback revoked owner")
    try:
        with VoidCodeRuntime(workspace=tmp_path, config=_config(log), repositories=repos) as runtime:
            with EXECUTION_OWNERSHIP.bind(lease):
                with pytest.raises(ExecutionOwnershipRevokedError):
                    list(runtime.run_stream(RuntimeRequest(prompt="run a and b", session_id="revoked")))
    finally:
        installed_phases.REVOKE = None
    assert _log(log) == _expected("revoked")[:3]
    records = repos.sessions.load_session(workspace=tmp_path, session_id="revoked").session.metadata["phase_records"]
    assert len(records) == 3
    assert [record["state"] for record in records.values()].count("started") == 1
    assert next(record for record in records.values() if record["state"] == "started")["phase_key"] == "phase-pkg:provider_turn"


def test_storage_start_failure_never_invokes_callback(installed_phases: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "callbacks.log"
    repos = repositories_for_test_store(SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3"))
    original = repos.sessions.save_composition_phase_record
    attempts = []

    def fail_phase_start(*args: Any, **kwargs: Any) -> Any:
        record = kwargs["record"]
        if record.state == "started":
            attempts.append(record)
            raise OSError("phase start storage unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(repos.sessions, "save_composition_phase_record", fail_phase_start)
    with VoidCodeRuntime(workspace=tmp_path, config=_config(log), repositories=repos) as runtime:
        with pytest.raises(OSError, match="phase start storage unavailable"):
            list(runtime.run_stream(RuntimeRequest(prompt="run a and b", session_id="disk-failure")))
    assert attempts
    assert _log(log) == []


@pytest.mark.parametrize("corruption", ["mapping", "entry", "key", "binding", "version", "started", "ref", "ref-shape"])
def test_runtime_consumers_refuse_malformed_or_started_durable_records(installed_phases: Any, tmp_path: Path, corruption: str) -> None:
    log = tmp_path / "callbacks.log"
    repos = repositories_for_test_store(SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3"))
    config = _config(log)
    with VoidCodeRuntime(workspace=tmp_path, config=config, repositories=repos) as runtime:
        _interrupt_after_a(runtime, "corrupt")
    stored = repos.sessions.load_session(workspace=tmp_path, session_id="corrupt")
    metadata = dict(stored.session.metadata)
    records = {key: dict(value) for key, value in metadata["phase_records"].items()}
    key = next(key for key in records if ":provider_turn:" in key)
    record = records[key]
    if corruption == "mapping":
        metadata["phase_records"] = []
    elif corruption == "entry":
        records[key] = "malformed"
    elif corruption == "key":
        records["wrong-key"] = records.pop(key)
    elif corruption == "binding":
        record["binding_id"] = "wrong-binding"
    elif corruption == "version":
        record["version"] = "wrong-version"
    elif corruption == "started":
        record.update(state="started", payload={})
    elif corruption == "ref":
        metadata["composition_ref"] = {**metadata["composition_ref"], "workspace": "/not-the-owner"}
    else:
        metadata["composition_ref"] = "malformed"
    if corruption != "mapping":
        metadata["phase_records"] = records
    # Corrupt the checkpoint's copy too: admission must reach the real phase
    # consumer, not merely reject disagreement between two metadata copies.
    checkpoint = repos.recovery.load_resume_checkpoint(workspace=tmp_path, session_id="corrupt")
    assert checkpoint is not None
    checkpoint["session_metadata"] = {**checkpoint["session_metadata"], **metadata}
    checkpoint["session_metadata"].pop("execution_composition", None)
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as connection:
        connection.execute(
            "UPDATE sessions SET metadata_json = ?, resume_checkpoint_json = ? WHERE workspace_id = ? AND session_id = ?",
            (json.dumps(metadata), json.dumps(checkpoint), str(tmp_path), "corrupt"),
        )
    before = _log(log)
    with VoidCodeRuntime(workspace=tmp_path, config=config, repositories=repos) as runtime:
        with pytest.raises(ValueError):
            list(runtime.resume_stream("corrupt"))
    assert _log(log) == before
