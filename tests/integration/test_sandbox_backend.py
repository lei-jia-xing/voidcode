"""Consumer-visible sandbox backend tests: confinement is real, merge is transparent.

Proves on the real core stack: confined exec observes accurate effects that land in
durable fact changes with ZERO tool edits, outside-root writes never touch the host,
custom log paths refuse under confinement, and the absent section preserves today's
behavior exactly. Skips loudly when the helper or bwrap is unavailable (locals without
the toolchain), never silently.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import locate_sandbox_helper, open_core
from voidcode.core.engine import Core
from voidcode.core.plugins import LeaseContext
from voidcode.core.services import RuntimeServices
from voidcode.sdk.json_values import json_wire_object

_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _helper_available() -> bool:
    helper = locate_sandbox_helper()
    return helper is not None and shutil.which("bwrap") is not None


needs_helper = pytest.mark.skipif(not _helper_available(), reason="sandbox helper or bwrap unavailable on this host")


def _core(tmp_path: Path, *, sandbox: dict | None, extra: dict | None = None) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config: dict[str, object] = {"schema_version": 1}
    if sandbox is not None:
        config["sandbox"] = sandbox
    if extra:
        config.update(extra)
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _services(core: Core) -> RuntimeServices:
    runtime = core._snapshot.plugin_runtime
    lease = LeaseContext(
        token="test",
        owner="test",
        session_id="s",
        workspace=core.workspace,
        run_id="r",
        revision_id=runtime.revision_id,
        invocation_id="i",
        readonly=False,
    )
    return RuntimeServices(runtime, lease)


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@needs_helper
def test_confined_exec_changes_land_in_facts_with_zero_tool_edits(tmp_path: Path) -> None:
    replies = [
        _turn(_call("c1", "shell_exec", command="echo one > merged.txt && echo DONE")),
        _turn(output="done"),
    ]
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        core = _core(
            tmp_path,
            sandbox={"mode": "workspace-write"},
            extra={
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {"endpoint": {"api_key": "x", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            },
        )
        assert core.run("make the file", session_id="s1").status == "completed"
        completed = [e for e in core.store.events("s1") if e.event_type == "core.tool_completed"]
        assert len(completed) == 1
        metadata = json_wire_object(completed[0].payload.get("fact_metadata", {}))
        assert {"old_path": None, "operation": "create", "path": str(core.workspace / "merged.txt")} in metadata["changes"]
        assert str(core.workspace / "merged.txt") in metadata["resource_paths"]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


@needs_helper
def test_confined_shell_command_preserves_observed_changes(tmp_path: Path) -> None:
    core = _core(tmp_path, sandbox={"mode": "workspace-write"})
    core.create_session("shell")
    result = core.invoke_command("shell", "shell", {"command": "echo changed > command.txt"}, expected_revision=core.revision_id)
    assert result.status == "ok"
    assert (core.workspace / "command.txt").read_text() == "changed\n"
    completed = [e for e in core.store.events("shell") if e.event_type == "core.command_completed"]
    assert len(completed) == 1
    changes = json_wire_object(completed[0].payload)["observed_changes"]
    assert any(change["path"] == str(core.workspace / "command.txt") and change["operation"] == "create" for change in changes)


@needs_helper
def test_outside_root_write_never_touches_host(tmp_path: Path) -> None:
    core = _core(tmp_path, sandbox={"mode": "workspace-write"})
    services = _services(core)
    marker = tmp_path / "host-evil.txt"
    result = services.run_process(
        ["/bin/sh", "-c", f"echo evil > {marker} && echo WROTE"],
        cwd=core.workspace,
        timeout_seconds=60,
    )
    assert not marker.exists()
    assert result.effects is not None
    assert result.effects.backend == "bubblewrap"


@needs_helper
def test_custom_log_path_refused_under_confinement(tmp_path: Path) -> None:
    core = _core(tmp_path, sandbox={"mode": "workspace-write"})
    services = _services(core)
    with pytest.raises(ValueError, match="sandbox refuses custom log paths"):
        services.spawn_process(["/bin/sleep", "30"], stdout_path=tmp_path / "custom.log")
    handle = services.spawn_process(["/bin/sh", "-c", "echo hi"], process_id="spool-ok")
    assert "process-spool" in handle.stdout_path
    assert services.cancel_process("spool-ok") is True


@needs_helper
def test_danger_mode_labels_observation_best_effort(tmp_path: Path) -> None:
    core = _core(tmp_path, sandbox={"mode": "danger-full-access"})
    services = _services(core)
    result = services.run_process(["/bin/sh", "-c", "echo hi > d.txt"], cwd=core.workspace, timeout_seconds=60)
    assert result.exit_code == 0
    assert result.effects is not None
    assert result.effects.backend == "unconfined"
    assert result.effects.network is not None and result.effects.network.allowed is True
    assert "best-effort" in result.effects.network.detail


def test_absent_section_preserves_host_behavior(tmp_path: Path) -> None:
    core = _core(tmp_path, sandbox=None)
    assert core._snapshot.plugin_runtime.sandbox_backend is None
    services = _services(core)
    result = services.run_process(["/bin/sh", "-c", "echo HOST"], cwd=core.workspace, timeout_seconds=30)
    assert result.exit_code == 0
    assert result.stdout.strip() == "HOST"
    assert result.effects is None
    handle = services.spawn_process(["/bin/sh", "-c", "echo hi"], stdout_path=tmp_path / "custom.log")
    assert handle.stdout_path == str(tmp_path / "custom.log")
    assert services.cancel_process(handle.process_id) is True
