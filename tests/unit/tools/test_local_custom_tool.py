from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from voidcode.core.memory_host import MemoryAbortSignal
from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import (
    RuntimeToolTimeoutError,
    TextOutput,
    ToolCall,
    ToolFailure,
    ToolSuccess,
)
from voidcode.tools.local_custom import LocalCustomTool, LocalCustomToolManifest


def test_local_custom_retains_full_split_stream_bytes_and_shows_stderr(tmp_path: Path) -> None:
    stdout = b"x" * 8191 + "雪".encode() + b"\xff\n" + b"stdout-line\n" * 12_000
    stderr = b"warning\n" * 12_000
    script = (
        "import sys; sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(b'x'*8191 + '雪'.encode() + b'\\xff\\n' + b'stdout-line\\n'*12000); "
        "sys.stderr.buffer.write(b'warning\\n'*12000)"
    )
    manifest = LocalCustomToolManifest(
        name="native/local",
        description="native output test",
        input_schema={"type": "object"},
        command=(sys.executable, "-c", script),
        read_only=True,
        manifest_path=tmp_path / "native.json",
    )

    result = LocalCustomTool(manifest).invoke(
        ToolCall(tool_name="native/local", tool_call_id="capture", arguments={}),
        context=ToolContext(workspace=tmp_path),
    )

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.output, TextOutput)
    assert "[stderr]" in result.output.text
    assert "warning" in result.output.text
    assert result.body is not None
    body = result.body.as_payload()
    for key, expected in (("stdout_artifact", stdout), ("stderr_artifact", stderr)):
        artifact = body[key]
        assert isinstance(artifact, dict)
        path = Path(str(artifact["path"]))
        assert path.read_bytes() == expected
        assert artifact["sha256"] == hashlib.sha256(expected).hexdigest()
        assert artifact["byte_count"] == len(expected)


def test_local_custom_timeout_carries_partial_failure_and_full_stream_refs(tmp_path: Path) -> None:

    marker = tmp_path / "local-child-survived-timeout"
    child = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('alive')"
    script = (
        "import subprocess,sys,time; "
        "sys.stdin.buffer.read(); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print('partial', flush=True); time.sleep(30)"
    )
    manifest = LocalCustomToolManifest(
        name="native/timeout",
        description="native timeout test",
        input_schema={"type": "object"},
        command=(sys.executable, "-c", script),
        read_only=True,
        manifest_path=tmp_path / "timeout.json",
    )
    try:
        LocalCustomTool(manifest).invoke_with_runtime_timeout(
            ToolCall(tool_name="native/timeout", tool_call_id="timeout", arguments={}),
            context=ToolContext(workspace=tmp_path),
            timeout_seconds=1,
        )
    except RuntimeToolTimeoutError as exc:
        assert exc.execution_stopped
        assert isinstance(exc.partial_result, ToolFailure)
        assert exc.partial_result.body is not None
        body = exc.partial_result.body.as_payload()
        artifact = body["stdout_artifact"]
        assert isinstance(artifact, dict)
        path = Path(str(artifact["path"]))
        assert path.read_bytes() == b"partial\n"
    else:
        raise AssertionError("local custom timeout returned a normal result")
    time.sleep(1.7)
    assert not marker.exists()


def test_local_custom_large_stdin_keeps_timeout_and_cancel_responsive(tmp_path: Path) -> None:
    arguments = {"payload": "x" * (4 * 1024 * 1024)}
    for mode in ("timeout", "cancel"):
        marker = tmp_path / f"{mode}-child-survived"
        pid_file = tmp_path / f"{mode}-pids.json"
        pid_tmp = tmp_path / f"{mode}-pids.tmp"
        child = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w', encoding='utf-8').write('alive')"
        script = (
            "import json, os, subprocess, sys, time; "
            f"child = subprocess.Popen([sys.executable, '-u', '-c', {child!r}], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "print('partial', flush=True); "
            f"open({str(pid_tmp)!r}, 'w', encoding='utf-8').write("
            "json.dumps({'parent': os.getpid(), 'child': child.pid})); "
            f"os.replace({str(pid_tmp)!r}, {str(pid_file)!r}); "
            "time.sleep(3)"
        )
        manifest = LocalCustomToolManifest(
            name=f"native/large-{mode}",
            description="large stdin timeout and cancellation test",
            input_schema={"type": "object"},
            command=(sys.executable, "-u", "-c", script),
            read_only=True,
            manifest_path=tmp_path / f"{mode}.json",
        )
        tool = LocalCustomTool(manifest)
        call = ToolCall(
            tool_name=manifest.name,
            tool_call_id=f"large-{mode}",
            arguments=arguments,
        )
        abort_signal = MemoryAbortSignal()
        context = ToolContext(workspace=tmp_path, abort_signal=abort_signal)
        try:
            if mode == "timeout":
                try:
                    tool.invoke_with_runtime_timeout(call, context=context, timeout_seconds=1)
                except RuntimeToolTimeoutError as exc:
                    assert exc.execution_stopped
                    assert isinstance(exc.partial_result, ToolFailure)
                    assert isinstance(exc.partial_result.output, TextOutput)
                    assert "partial" in exc.partial_result.output.text
                    assert exc.partial_result.body is not None
                    assert exc.partial_result.body.as_payload()["stop_confirmed"] is True
                else:
                    raise AssertionError("large local custom input bypassed runtime timeout")
            else:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(tool.invoke, call, context=context)
                    deadline = time.monotonic() + 5
                    while not pid_file.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert pid_file.exists()
                    abort_signal.set_cancelled(True, reason="large local custom stdin cancellation")
                    result = future.result(timeout=5)
                assert isinstance(result, ToolFailure)
                assert result.body is not None
                payload = result.body.as_payload()
                assert payload["cancelled"] is True
                assert payload["stop_confirmed"] is True
                assert isinstance(result.output, TextOutput)
                assert "partial" in result.output.text

            time.sleep(1.7)
            assert not marker.exists()
        finally:
            if pid_file.exists() and os.name != "nt":
                pids = json.loads(pid_file.read_text(encoding="utf-8"))
                try:
                    os.killpg(pids["parent"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
