from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.turn_producers import model_turn_fact
from voidcode.core.turns import FinalTurn, ToolTurn, TurnRequest, TurnSession
from voidcode.runtime.config.loading import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.tools.contracts import ToolCall
from voidcode.tools.shell_exec import ShellExecTool


class _ShellCall:
    def __init__(self, command: str) -> None:
        self._command = command

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        fact = model_turn_fact(request)
        _ = session
        if tool_results:
            return FinalTurn(output="finished", facts=(fact,))
        return ToolTurn(calls=(ToolCall("shell_exec", {"command": self._command}),), facts=(fact,))


def test_runtime_passes_raw_shell_command_to_native_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "npm"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "print('ARGS_HEX=' + b'\\0'.join(arg.encode() for arg in sys.argv[1:]).hex())\n"
        "print('ENV_HEX=' + (os.environ.get('CI', '') + '\\0' + os.environ.get('NPM_CONFIG_YES', '')).encode().hex())\n"
        "print('STDIN_HEX=' + sys.stdin.buffer.read().hex())\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CI", "runtime-caller-value")
    monkeypatch.setenv("NPM_CONFIG_YES", "runtime-caller-no")
    command = "\nnpm 'arg with spaces' <<'EOF'\nfirst\nsecond\nEOF\n"
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        turn_producer=_ShellCall(command),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="yolo",
            tool_timeout_seconds=None,
        ),
    )

    response = runtime.run(RuntimeRequest(prompt="run raw shell command"))
    completed = [event for event in response.events if event.event_type == "runtime.tool_completed"]

    assert len(completed) == 1
    payload = completed[0].payload
    assert payload["command"] == command
    assert payload["exit_code"] == 0
    assert payload["stdout"] == (
        "ARGS_HEX=" + b"arg with spaces".hex() + "\n"
        "ENV_HEX=" + b"runtime-caller-value\0runtime-caller-no".hex() + "\n"
        "STDIN_HEX=" + b"first\nsecond\n".hex() + "\n"
    )
    assert payload["stderr"] == ""
