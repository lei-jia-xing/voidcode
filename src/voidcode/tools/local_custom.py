from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import cast, final

from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context

LOCAL_CUSTOM_TOOL_SOURCE = "local_custom_tool"
LOCAL_CUSTOM_TOOL_DEFAULT_PATH = ".voidcode/tools"
LOCAL_CUSTOM_TOOL_MANIFEST_SUFFIX = ".json"
_VALID_TOOL_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/"
)


@dataclass(frozen=True, slots=True)
class LocalCustomToolManifest:
    name: str
    description: str
    input_schema: dict[str, object]
    command: tuple[str, ...]
    read_only: bool
    manifest_path: Path


def discover_local_custom_tools(
    workspace: Path,
    *,
    enabled: bool | None,
    relative_path: str = LOCAL_CUSTOM_TOOL_DEFAULT_PATH,
) -> tuple[LocalCustomTool, ...]:
    if enabled is not True:
        return ()
    root = (workspace / relative_path).resolve()
    workspace_root = workspace.resolve()
    try:
        root.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("local custom tools path must stay inside the workspace") from exc
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ValueError(f"local custom tools path is not a directory: {relative_path}")

    manifests = tuple(
        _load_local_custom_tool_manifest(path, workspace=workspace_root)
        for path in sorted(root.glob(f"*{LOCAL_CUSTOM_TOOL_MANIFEST_SUFFIX}"))
        if path.is_file()
    )
    return tuple(LocalCustomTool(manifest) for manifest in manifests)


def _load_local_custom_tool_manifest(path: Path, *, workspace: Path) -> LocalCustomToolManifest:
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid local custom tool manifest JSON at {path}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError(f"local custom tool manifest must be an object: {path}")
    payload = cast(dict[str, object], raw_payload)
    allowed_keys = {"name", "description", "input_schema", "command", "read_only"}
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"local custom tool manifest {path} has unsupported field: {unknown_keys[0]}"
        )

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"local custom tool manifest {path} requires a non-empty name")
    normalized_name = name.strip()
    if any(char not in _VALID_TOOL_NAME_CHARS for char in normalized_name):
        raise ValueError(
            f"local custom tool manifest {path} has invalid name {normalized_name!r}; "
            "use letters, numbers, '_', '-', or '/'"
        )

    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"local custom tool manifest {path} requires a non-empty description")

    input_schema = _parse_input_schema(payload.get("input_schema", {}), manifest_path=path)

    command = _parse_manifest_command(payload.get("command"), manifest_path=path)
    read_only = payload.get("read_only", True)
    if not isinstance(read_only, bool):
        raise ValueError(f"local custom tool manifest {path} read_only must be a boolean")

    _validate_command_entrypoint(command, manifest_path=path, workspace=workspace)
    return LocalCustomToolManifest(
        name=normalized_name,
        description=description.strip(),
        input_schema=input_schema,
        command=command,
        read_only=read_only,
        manifest_path=path,
    )


def _parse_manifest_command(value: object, *, manifest_path: Path) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"local custom tool manifest {manifest_path} command must be an array")
    command: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str) or item == "":
            raise ValueError(
                "local custom tool manifest "
                f"{manifest_path} command[{index}] must be a non-empty string"
            )
        command.append(item)
    if not command:
        raise ValueError(
            f"local custom tool manifest {manifest_path} command must contain at least one string"
        )
    return tuple(command)


def _parse_input_schema(value: object, *, manifest_path: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(
            f"local custom tool manifest {manifest_path} input_schema must be an object"
        )
    schema = cast(dict[str, object], value)
    try:
        json.dumps(schema)
    except TypeError as exc:
        raise ValueError(
            f"local custom tool manifest {manifest_path} input_schema must be JSON serializable"
        ) from exc
    schema_type = schema.get("type")
    if schema_type is not None and schema_type != "object":
        raise ValueError(
            f"local custom tool manifest {manifest_path} input_schema.type must be 'object'"
        )
    if not all(isinstance(key, str) for key in schema):
        raise ValueError(
            f"local custom tool manifest {manifest_path} input_schema keys must be strings"
        )
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise ValueError(
            f"local custom tool manifest {manifest_path} input_schema.properties must be an object"
        )
    if isinstance(properties, dict) and not all(isinstance(key, str) for key in properties):
        raise ValueError(
            "local custom tool manifest "
            f"{manifest_path} input_schema.properties keys must be strings"
        )
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(item, str) for item in required)
    ):
        raise ValueError(
            f"local custom tool manifest {manifest_path} input_schema.required must be strings"
        )
    return {"type": "object", **schema}


def _validate_command_entrypoint(
    command: tuple[str, ...], *, manifest_path: Path, workspace: Path
) -> None:
    entrypoint = command[-1]
    if "${manifest_dir}" not in entrypoint:
        return
    rendered = Template(entrypoint).safe_substitute(manifest_dir=str(manifest_path.parent))
    resolved_entrypoint = Path(rendered).expanduser().resolve()
    try:
        resolved_entrypoint.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            "local custom tool manifest "
            f"{manifest_path} command entrypoint must stay inside workspace"
        ) from exc


@final
class LocalCustomTool:
    def __init__(self, manifest: LocalCustomToolManifest) -> None:
        self._manifest = manifest

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._manifest.name,
            description=self._manifest.description,
            input_schema=self._manifest.input_schema,
            read_only=False,
        )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        return self._invoke(call, workspace=workspace, timeout_seconds=None)

    def invoke_with_runtime_timeout(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult:
        return self._invoke(call, workspace=workspace, timeout_seconds=timeout_seconds)

    def _invoke(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        timeout_seconds: int | None,
    ) -> ToolResult:
        command = self._render_command(workspace=workspace.resolve())
        env = self._build_environment(call=call, workspace=workspace.resolve())
        start = time.monotonic()
        completed = self._run_command(
            command=command,
            workspace=workspace.resolve(),
            env=env,
            input_text=json.dumps(call.arguments),
            timeout_seconds=timeout_seconds,
        )
        stdout = completed.stdout.replace("\r\n", "\n")
        stderr = completed.stderr.replace("\r\n", "\n")
        elapsed_ms = round((time.monotonic() - start) * 1000)
        data: dict[str, object] = {
            "exit_code": completed.returncode,
            "elapsed_ms": elapsed_ms,
            "manifest": str(self._manifest.manifest_path),
        }
        if stderr:
            data["stderr"] = stderr
        if completed.returncode != 0:
            message = (
                stderr.strip() or stdout.strip() or f"command exited with {completed.returncode}"
            )
            return ToolResult(
                tool_name=self._manifest.name,
                status="error",
                content=stdout or None,
                data=data,
                error=message,
                source=LOCAL_CUSTOM_TOOL_SOURCE,
                error_kind="local_custom_tool_failed",
            )
        return ToolResult(
            tool_name=self._manifest.name,
            status="ok",
            content=stdout or None,
            data=data,
            source=LOCAL_CUSTOM_TOOL_SOURCE,
        )

    def _run_command(
        self,
        *,
        command: tuple[str, ...],
        workspace: Path,
        env: dict[str, str],
        input_text: str,
        timeout_seconds: int | None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise ValueError(
                f"local custom tool '{self._manifest.name}' failed to execute: {exc}"
            ) from exc

        context = current_runtime_tool_context()
        abort_signal = context.abort_signal if context is not None else None
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        pending_input: str | None = input_text
        while True:
            if abort_signal is not None and abort_signal.cancelled:
                _kill_local_custom_process(process)
                stdout, stderr = _communicate_after_kill(process)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if deadline is not None and time.monotonic() >= deadline:
                _kill_local_custom_process(process)
                stdout, stderr = _communicate_after_kill(process)
                raise RuntimeToolTimeoutError(
                    "local custom tool "
                    f"'{self._manifest.name}' timed out after {timeout_seconds} seconds",
                    partial_result={"stdout": stdout, "stderr": stderr},
                )
            try:
                timeout = (
                    0.05 if deadline is None else max(0.01, min(0.05, deadline - time.monotonic()))
                )
                stdout, stderr = process.communicate(input=pending_input, timeout=timeout)
            except subprocess.TimeoutExpired:
                pending_input = None
                continue
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _render_command(self, *, workspace: Path) -> tuple[str, ...]:
        return tuple(
            Template(part).safe_substitute(
                manifest_dir=str(self._manifest.manifest_path.parent),
                workspace=str(workspace),
            )
            for part in self._manifest.command
        )

    def _build_environment(self, *, call: ToolCall, workspace: Path) -> dict[str, str]:
        env = dict(os.environ)
        context = current_runtime_tool_context()
        env["VOIDCODE_WORKSPACE"] = str(workspace)
        env["VOIDCODE_TOOL_NAME"] = self._manifest.name
        env["VOIDCODE_TOOL_ARGUMENTS"] = json.dumps(call.arguments)
        if call.tool_call_id is not None:
            env["VOIDCODE_TOOL_CALL_ID"] = call.tool_call_id
        if context is not None:
            env["VOIDCODE_SESSION_ID"] = context.session_id
            if context.parent_session_id is not None:
                env["VOIDCODE_PARENT_SESSION_ID"] = context.parent_session_id
            env["VOIDCODE_DELEGATION_DEPTH"] = str(context.delegation_depth)
        return env


def _kill_local_custom_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _communicate_after_kill(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()
