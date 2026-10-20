"""Process integration tests for HostClient and plugin host."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from voidcode.kernel.host import HostClient, HostTimeout, HostUnavailable, InvocationCancelled
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.manifest import (
    PluginCapabilityRequest,
    PluginFileIdentity,
    PluginManifest,
    PluginProviderDeclaration,
    PluginToolDeclaration,
)
from voidcode.sdk.plugin import HostPlugin
from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderMessage,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolSuccess,
)


class StubAbortSignal:
    """Simple ProviderAbortSignal for testing cancellation."""

    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        with self._lock:
            self._cancelled = value
            self._reason = reason


def make_scratch_plugin(
    tmp_path: Path,
    *,
    plugin_id: str = "scratch-test",
    module_name: str = "scratch_plugin",
    tool_names: tuple[str, ...] = ("echo_tool",),
    provider_names: tuple[str, ...] = ("scratch_provider",),
    config_schema: Mapping[str, object] | None = None,
    corrupt_hash: bool = False,
    include_rogue_file: bool = False,
    extra_code: str = "",
    omit_tool_in_factory: bool = False,
    stubborn_tool: bool = False,
    noisy_print_tool: bool = False,
) -> tuple[Path, PluginManifest]:
    """Create a temporary plugin directory and manifest."""
    root = tmp_path / plugin_id
    root.mkdir(parents=True, exist_ok=True)

    stubborn_code = ""
    if stubborn_tool:
        stubborn_code = """
class StubbornTool:
    definition = ToolDefinition(
        name="stubborn_tool",
        description="Ignores abort and sleeps",
        input_schema={"type": "object"},
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, call, *, context, services):
        while True:
            time.sleep(0.1)
"""

    noisy_code = ""
    if noisy_print_tool:
        noisy_code = """
class NoisyTool:
    definition = ToolDefinition(
        name="noisy_tool",
        description="Prints directly to stdout",
        input_schema={"type": "object"},
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, call, *, context, services):
        print("POLLUTING STDOUT VIA PRINT()")
        sys.stdout.write("POLLUTING STDOUT VIA SYS.STDOUT.WRITE()\\n")
        sys.stdout.flush()
        return ToolSuccess(tool_name="noisy_tool", output=TextOutput(text="Print succeeded"))
"""

    code = f"""
import sys
import time
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolDefinition, ToolEffect, ToolSuccess, ToolFailure, TextOutput
from voidcode.sdk.provider import ProviderTurnResult, ProviderExecutionError

class EchoTool:
    definition = ToolDefinition(
        name="echo_tool",
        description="Echoes input",
        input_schema={{"type": "object", "properties": {{"msg": {{"type": "string"}}}}}},
        effects=frozenset({{ToolEffect.READ}}),
    )

    def invoke(self, call, *, context, services):
        msg = call.arguments.get("msg", "")
        return ToolSuccess(tool_name="echo_tool", output=TextOutput(text=f"echo: {{msg}}"))

class FailingTool:
    definition = ToolDefinition(
        name="failing_tool",
        description="Fails with zero division",
        input_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.READ}}),
    )

    def invoke(self, call, *, context, services):
        return 1 / 0

class CooperativeCancelTool:
    definition = ToolDefinition(
        name="coop_cancel_tool",
        description="Loops until cancelled",
        input_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.READ}}),
    )

    def invoke(self, call, *, context, services):
        for _ in range(100):
            if context.abort_signal and context.abort_signal.cancelled:
                return ToolFailure(
                    tool_name="coop_cancel_tool",
                    error="Cancelled cooperatively",
                    output=TextOutput(text="Cancelled"),
                )
            time.sleep(0.05)
        return ToolSuccess(tool_name="coop_cancel_tool", output=TextOutput(text="Done"))

class HangTool:
    definition = ToolDefinition(
        name="hang_tool",
        description="Sleeps unconditionally ignoring signals",
        input_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.READ}}),
    )

    def invoke(self, call, *, context, services):
        time.sleep(60.0)
        return ToolSuccess(tool_name="hang_tool", output=TextOutput(text="Awake"))

class ScratchProvider:
    name = "scratch_provider"

    def propose_turn(self, request):
        if request.model_name == "trigger-rate-limit":
            raise ProviderExecutionError(
                kind="rate_limit",
                provider_name="scratch_provider",
                model_name=request.model_name or "trigger-rate-limit",
                message="Rate limit exceeded on upstream",
            )
        return ProviderTurnResult(
            output="sample reply",
            done_reason="stop",
            finish_reason_reported=True,
        )

{stubborn_code}
{noisy_code}
class SecretTool:
    definition = ToolDefinition(
        name="secret_tool",
        description="Fails with arbitrary secret in exception",
        input_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.READ}}),
    )

    def invoke(self, call, *, context, services):
        raise RuntimeError("database auth failed: secret_token=XYZZY_SECRET_UNMATCHED_VALUE_992288")

{extra_code}

def create_plugin(config):
    tools = []
    if "secret_tool" in {list(tool_names)!r}:
        tools.append(SecretTool())
    if "echo_tool" in {list(tool_names)!r} and not {omit_tool_in_factory}:
        tools.append(EchoTool())
    if "failing_tool" in {list(tool_names)!r}:
        tools.append(FailingTool())
    if "coop_cancel_tool" in {list(tool_names)!r}:
        tools.append(CooperativeCancelTool())
    if "hang_tool" in {list(tool_names)!r}:
        tools.append(HangTool())
    if "stubborn_tool" in {list(tool_names)!r}:
        tools.append(StubbornTool())
    if "noisy_tool" in {list(tool_names)!r}:
        tools.append(NoisyTool())

    providers = []
    if "scratch_provider" in {list(provider_names)!r}:
        providers.append(ScratchProvider())

    return Plugin(tools=tuple(tools), providers=tuple(providers))
"""
    py_path = root / f"{module_name}.py"
    py_path.write_text(code, encoding="utf-8")

    if corrupt_hash:
        py_hash = "0" * 64
    else:
        py_hash = hashlib.sha256(py_path.read_bytes()).hexdigest()

    if include_rogue_file:
        rogue_path = root / "rogue.py"
        rogue_path.write_text("# rogue undeclared file\n", encoding="utf-8")

    tool_defs_lookup = {
        "echo_tool": ("Echoes input", {"type": "object", "properties": {"msg": {"type": "string"}}}),
        "failing_tool": ("Fails with zero division", {"type": "object"}),
        "coop_cancel_tool": ("Loops until cancelled", {"type": "object"}),
        "hang_tool": ("Sleeps unconditionally ignoring signals", {"type": "object"}),
        "stubborn_tool": ("Ignores abort and sleeps", {"type": "object"}),
        "noisy_tool": ("Prints directly to stdout", {"type": "object"}),
        "secret_tool": ("Fails with arbitrary secret in exception", {"type": "object"}),
    }
    tool_declarations: list[PluginToolDeclaration] = []
    for name in tool_names:
        desc, schema = tool_defs_lookup.get(name, (f"Tool {name}", {"type": "object"}))
        tool_declarations.append(
            PluginToolDeclaration(
                name=name,
                description=desc,
                input_schema=schema,
                effects=(ToolEffect.READ,),
                capabilities=("test.read",),
            )
        )

    provider_declarations: list[PluginProviderDeclaration] = []
    for name in provider_names:
        provider_declarations.append(
            PluginProviderDeclaration(
                name=name,
                description=f"Provider {name}",
            )
        )

    manifest = PluginManifest(
        plugin_id=plugin_id,
        version="1.0.0",
        entrypoint=f"{module_name}:create_plugin",
        files=(PluginFileIdentity(path=f"{module_name}.py", sha256=py_hash),),
        tools=tuple(tool_declarations),
        providers=tuple(provider_declarations),
        capabilities=(PluginCapabilityRequest(name="test.read", reason="read access"),),
        config_schema=config_schema if config_schema is not None else {},
    )
    return root, manifest


def test_scratch_plugin_registration_and_invocation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1") as client:
        assert client.pid > 0
        assert len(client.tools) == 1
        assert client.tools[0].name == "echo_tool"
        assert client.providers == ("scratch_provider",)
        assert client.owner("echo_tool") == manifest.plugin_id
        assert client.capabilities("echo_tool") == frozenset({"test.read"})

        # Successful tool invocation
        context = ToolContext(
            workspace=workspace,
            session_id="s-1",
            run_id="r-1",
            invocation_id="i-1",
            revision_id="rev-1",
            actor="model",
            capabilities=frozenset({"test.read"}),
        )
        call = ToolCall(tool_name="echo_tool", arguments={"msg": "hello process host"})
        result = client.invoke(call, context=context)

        assert isinstance(result, ToolSuccess)
        assert isinstance(result.output, TextOutput)
        assert result.output.text == "echo: hello process host"

        # Successful provider propose_turn
        req = ProviderTurnRequest(messages=(ProviderMessage(role="user", content="hello"),))
        turn_result = client.propose_turn(req, provider="scratch_provider")
        assert isinstance(turn_result, ProviderTurnResult)
        assert turn_result.output == "sample reply"
        assert turn_result.done_reason == "stop"

    # Verify child process reaped on close
    assert client._proc.poll() is not None


def test_plugin_stdout_redirection_does_not_corrupt_protocol(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, tool_names=("noisy_tool",), noisy_print_tool=True)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1") as client:
        context = ToolContext(
            workspace=workspace,
            session_id="s-1",
            run_id="r-1",
            invocation_id="i-1",
            revision_id="rev-1",
            actor="model",
            capabilities=frozenset({"test.read"}),
        )
        call = ToolCall(tool_name="noisy_tool", arguments={})
        result = client.invoke(call, context=context)

        # Invocations succeed cleanly despite prints because stdout was reserved and prints went to stderr
        assert isinstance(result, ToolSuccess)
        assert isinstance(result.output, TextOutput)
        assert result.output.text == "Print succeeded"


def test_schema_mismatch_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    schema = {"type": "object", "required": ["api_key"], "properties": {"api_key": {"type": "string"}}}
    root, manifest = make_scratch_plugin(tmp_path, config_schema=schema)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with pytest.raises(HostUnavailable) as exc_info:
        HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")
    assert exc_info.value.execution_stopped is True


def test_grant_mismatch_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset(),
    )

    with pytest.raises(ValueError) as exc_info:
        HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")
    assert "requires capabilities" in str(exc_info.value)


def test_catalog_mismatch_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, omit_tool_in_factory=True)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with pytest.raises(HostUnavailable) as exc_info:
        HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")
    assert exc_info.value.execution_stopped is True


def test_undeclared_python_file_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, include_rogue_file=True)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with pytest.raises(HostUnavailable) as exc_info:
        HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")
    assert exc_info.value.execution_stopped is True


def test_hash_mismatch_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, corrupt_hash=True)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with pytest.raises(HostUnavailable) as exc_info:
        HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")
    assert exc_info.value.execution_stopped is True


def test_context_spoof_detection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-valid") as client:
        call = ToolCall(tool_name="echo_tool", arguments={"msg": "test"})

        # Mismatched workspace
        spoofed_ws_context = ToolContext(
            workspace=other_workspace,
            session_id="s",
            run_id="r",
            invocation_id="i",
            revision_id="rev-valid",
            actor="model",
            capabilities=frozenset({"test.read"}),
        )
        with pytest.raises(ValueError) as ws_err:
            client.invoke(call, context=spoofed_ws_context)
        assert "Context mismatch" in str(ws_err.value)

        # Mismatched revision
        spoofed_rev_context = ToolContext(
            workspace=workspace,
            session_id="s",
            run_id="r",
            invocation_id="i",
            revision_id="rev-forged",
            actor="model",
            capabilities=frozenset({"test.read"}),
        )
        with pytest.raises(ValueError) as rev_err:
            client.invoke(call, context=spoofed_rev_context)
        assert "Context mismatch" in str(rev_err.value)

        # Missing capability grant
        missing_cap_context = ToolContext(
            workspace=workspace,
            session_id="s",
            run_id="r",
            invocation_id="i",
            revision_id="rev-valid",
            actor="model",
            capabilities=frozenset(),
        )
        with pytest.raises(PermissionError) as cap_err:
            client.invoke(call, context=missing_cap_context)
        assert "capability grants" in str(cap_err.value)


def test_worker_exception_handled_as_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, tool_names=("echo_tool", "failing_tool"))
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1") as client:
        context = ToolContext(
            workspace=workspace,
            session_id="s",
            run_id="r",
            invocation_id="i",
            revision_id="rev-1",
            actor="model",
            capabilities=frozenset({"test.read"}),
        )

        call = ToolCall(tool_name="failing_tool", arguments={})
        result = client.invoke(call, context=context)

        # Uncaught exception returned as ToolFailure, not process crash
        assert isinstance(result, ToolFailure)
        assert result.status == "error"
        assert "ZeroDivisionError" in result.error

        # Host remains healthy for subsequent calls
        echo_call = ToolCall(tool_name="echo_tool", arguments={"msg": "still working"})
        echo_result = client.invoke(echo_call, context=context)
        assert isinstance(echo_result, ToolSuccess)
        assert isinstance(echo_result.output, TextOutput)
        assert echo_result.output.text == "echo: still working"


def test_cooperative_cancellation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, tool_names=("coop_cancel_tool",))
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1") as client:
        abort_signal = StubAbortSignal()
        context = ToolContext(
            workspace=workspace,
            session_id="s",
            run_id="r",
            invocation_id="i",
            revision_id="rev-1",
            actor="model",
            capabilities=frozenset({"test.read"}),
            abort_signal=abort_signal,
        )

        def cancel_soon() -> None:
            time.sleep(0.1)
            abort_signal.set_cancelled(True, reason="User cancelled")

        canceller = threading.Thread(target=cancel_soon)
        canceller.start()

        call = ToolCall(tool_name="coop_cancel_tool", arguments={})
        with pytest.raises(InvocationCancelled) as exc_info:
            client.invoke(call, context=context, timeout_seconds=5.0)
        canceller.join()

        assert exc_info.value.execution_stopped is True


def test_blocked_callback_timeout_kills_host(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, tool_names=("hang_tool",))
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1") as client:
        pid = client.pid
        context = ToolContext(
            workspace=workspace,
            session_id="s",
            run_id="r",
            invocation_id="i",
            revision_id="rev-1",
            actor="model",
            capabilities=frozenset({"test.read"}),
        )

        call = ToolCall(tool_name="hang_tool", arguments={})
        with pytest.raises(HostTimeout) as exc_info:
            client.invoke(call, context=context, timeout_seconds=0.3)

        assert exc_info.value.execution_stopped is True

        # Process killed and reaped; no ghost process
        time.sleep(0.2)
        assert client._proc.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_provider_execution_error_preserved(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1") as client:
        req = ProviderTurnRequest(
            messages=(ProviderMessage(role="user", content="hello"),),
            model_name="trigger-rate-limit",
        )
        with pytest.raises(ProviderExecutionError) as exc_info:
            client.propose_turn(req, provider="scratch_provider")

        assert exc_info.value.kind == "rate_limit"
        assert "Rate limit exceeded" in exc_info.value.message


def test_eof_and_cleanup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    client = HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")
    pid = client.pid
    assert client._proc.poll() is None

    # Normal close
    client.close()
    time.sleep(0.1)
    assert client._proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_eof_with_stubborn_callback_terminates(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path, tool_names=("stubborn_tool",), stubborn_tool=True)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    client = HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")
    pid = client.pid

    # Start invocation on worker
    context = ToolContext(
        workspace=workspace,
        session_id="s",
        run_id="r",
        invocation_id="i",
        revision_id="rev-1",
        actor="model",
        capabilities=frozenset({"test.read"}),
    )

    def invoke_in_bg() -> None:
        try:
            client.invoke(ToolCall(tool_name="stubborn_tool", arguments={}), context=context, timeout_seconds=60)
        except Exception:
            pass

    t = threading.Thread(target=invoke_in_bg, daemon=True)
    t.start()
    time.sleep(0.2)

    # Client close closes stdin (EOF) and terminates process group
    client.close()
    time.sleep(0.3)
    assert client._proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_lease_fd_passed_to_popen(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    root, manifest = make_scratch_plugin(tmp_path)
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={},
        capabilities=frozenset({"test.read"}),
    )

    r_fd, w_fd = os.pipe()
    try:
        with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1", lease_fd=r_fd) as client:
            assert client.pid > 0
    finally:
        os.close(r_fd)
        os.close(w_fd)


def test_unexpected_exception_does_not_leak_config_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "XYZZY_SECRET_UNMATCHED_VALUE_992288"

    root, manifest = make_scratch_plugin(tmp_path, tool_names=("secret_tool",))
    host_plugin = HostPlugin(
        root=root,
        manifest=manifest,
        config={"secret_key": secret},
        capabilities=frozenset({"test.read"}),
    )

    with HostClient((host_plugin,), workspace=workspace, revision_id="rev-1") as client:
        context = ToolContext(
            workspace=workspace,
            session_id="s",
            run_id="r",
            invocation_id="i",
            revision_id="rev-1",
            actor="model",
            capabilities=frozenset({"test.read"}),
        )
        res = client.invoke(ToolCall(tool_name="secret_tool", arguments={}), context=context)
        assert res.status == "error"
        assert secret not in res.error
        assert "Tool invocation failed with RuntimeError" in res.error
        if isinstance(res.output, TextOutput):
            assert secret not in res.output.text


def test_pid_only_host_sigkill_still_sweeps_the_owned_group(tmp_path: Path) -> None:
    """Sharing the host pgid is not the guarantee: the kernel must sweep the tracked group.

    A service child ignores SIGTERM and parents a grandchild in the host's group; killing only
    the host leader with PID SIGKILL must still remove both via the EOF/failure group sweep,
    while a detached background job in its own group survives.
    """
    import signal
    import subprocess
    import sys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    leader_file = workspace / "svc-leader.pid"
    grandchild_file = workspace / "svc-grandchild.pid"
    detached_file = workspace / "detached.pid"

    grandchild_script = (
        "import os,pathlib,signal,time;"
        f"pathlib.Path({str(grandchild_file)!r}).write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
    )
    leader_script = (
        "import os,pathlib,signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"subprocess.Popen([sys.executable,'-c',{grandchild_script!r}]);"
        f"pathlib.Path({str(leader_file)!r}).write_text(str(os.getpid()));time.sleep(120)"
    )
    detached_script = f"import os,pathlib,time;pathlib.Path({str(detached_file)!r}).write_text(str(os.getpid()));time.sleep(120)"
    extra = f"""
class SvcTool:
    definition = ToolDefinition(
        name="svc_tool",
        description="Spawn a service and a detached job",
        input_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.EXECUTE}}),
    )

    def invoke(self, call, *, context, services):
        import sys as _sys
        leader = {leader_script!r}
        detached_code = {detached_script!r}
        svc = services.spawn_service_process([_sys.executable, "-c", leader])
        detached = services.spawn_process(
            [_sys.executable, "-c", detached_code],
            stdout_path=services._server.background_spool / "detached.log",
            stderr_path=services._server.background_spool / "detached.err",
        )
        return ToolSuccess(tool_name="svc_tool", output=TextOutput(text=f"{{svc.process_id}} {{detached.process_id}}"))
"""
    root, _ = make_scratch_plugin(tmp_path, tool_names=("svc_tool",), provider_names=(), extra_code=extra)
    manifest = PluginManifest(
        plugin_id="scratch-test",
        version="1.0.0",
        entrypoint="scratch_plugin:create_plugin",
        files=(PluginFileIdentity(path="scratch_plugin.py", sha256=hashlib.sha256((root / "scratch_plugin.py").read_bytes()).hexdigest()),),
        tools=(
            PluginToolDeclaration(
                name="svc_tool",
                description="Spawn a service and a detached job",
                input_schema={"type": "object"},
                effects=(ToolEffect.EXECUTE,),
                capabilities=("test.read",),
            ),
        ),
        providers=(),
        capabilities=(PluginCapabilityRequest(name="test.read", reason="read access"),),
        config_schema={},
    )
    host_plugin = HostPlugin(root=root, manifest=manifest, config={}, capabilities=frozenset({"test.read"}))

    def _make_client() -> HostClient:
        return HostClient((host_plugin,), workspace=workspace, revision_id="rev-1")

    code = (root / "scratch_plugin.py").read_text(encoding="utf-8")
    code = code.replace("    providers = []", "    tools.append(SvcTool())\n\n    providers = []")
    (root / "scratch_plugin.py").write_text(code, encoding="utf-8")
    manifest = PluginManifest(
        plugin_id="scratch-test",
        version="1.0.0",
        entrypoint="scratch_plugin:create_plugin",
        files=(PluginFileIdentity(path="scratch_plugin.py", sha256=hashlib.sha256((root / "scratch_plugin.py").read_bytes()).hexdigest()),),
        tools=manifest.tools,
        providers=(),
        capabilities=manifest.capabilities,
        config_schema={},
    )
    host_plugin = HostPlugin(root=root, manifest=manifest, config={}, capabilities=frozenset({"test.read"}))

    client = _make_client()
    context = ToolContext(
        workspace=workspace,
        session_id="s",
        run_id="r",
        invocation_id="i",
        revision_id="rev-1",
        actor="model",
        capabilities=frozenset({"test.read"}),
    )
    result = client.invoke(ToolCall(tool_name="svc_tool", arguments={}), context=context)
    assert result.status == "ok", result
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (leader_file.exists() and grandchild_file.exists() and detached_file.exists()):
        time.sleep(0.05)
    leader = int(leader_file.read_text(encoding="utf-8"))
    grandchild = int(grandchild_file.read_text(encoding="utf-8"))
    detached = int(detached_file.read_text(encoding="utf-8"))
    assert os.getpgid(leader) == os.getpgid(client.pid)
    assert os.getpgid(detached) != os.getpgid(client.pid)

    try:
        os.kill(client.pid, signal.SIGKILL)
        assert _wait_pid_gone(leader, timeout=10.0), "owned service leader survived a PID-only host SIGKILL"
        assert _wait_pid_gone(grandchild, timeout=10.0), "SIGTERM-ignoring owned grandchild survived a PID-only host SIGKILL"
        assert _pid_alive(detached), "detached background job must survive the owned-group sweep"
    finally:
        client.close()
        for pid in (leader, grandchild, detached):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        subprocess.run([sys.executable, "-c", "pass"], check=False)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_pid_gone(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = Path(f"/proc/{pid}/stat")
        if not state.exists():
            return True
        try:
            if state.read_text(encoding="utf-8").split()[2] == "Z":
                return True
        except OSError, IndexError:
            return True
        time.sleep(0.05)
    return False
