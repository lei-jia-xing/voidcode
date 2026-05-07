from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tools import ToolCall


def test_background_process_tools_are_registered() -> None:
    runtime = VoidCodeRuntime(workspace=Path(tempfile.mkdtemp()))
    registry = runtime._base_tool_registry
    assert (
        registry.resolve("background_process_start").definition.name == "background_process_start"
    )
    assert registry.resolve("background_process_logs").definition.name == "background_process_logs"
    assert registry.resolve("background_process_stop").definition.name == "background_process_stop"
    runtime.__exit__(None, None, None)


def test_background_process_start_logs_and_stop(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    logs_tool = runtime._base_tool_registry.resolve("background_process_logs")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    command = f'"{sys.executable}" -c "import time; print(\'ready\', flush=True); time.sleep(5)"'
    start_result = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    process_id = str(start_result.data["process_id"])
    assert start_result.status == "ok"
    time.sleep(0.2)
    logs_result = logs_tool.invoke(
        ToolCall(tool_name="background_process_logs", arguments={"process_id": process_id}),
        workspace=tmp_path,
    )
    assert logs_result.status == "ok"
    assert isinstance(logs_result.content, str)
    assert "ready" in logs_result.content
    stop_result = stop_tool.invoke(
        ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
        workspace=tmp_path,
    )
    assert stop_result.status == "ok"
    assert stop_result.data["running"] is False
    runtime.__exit__(None, None, None)
