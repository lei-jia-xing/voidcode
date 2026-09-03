from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from string import Template
from typing import BinaryIO, cast, final

from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolDiagnostics, ToolResult
from .runtime_context import current_runtime_tool_context

LOCAL_CUSTOM_TOOL_SOURCE = "local_custom_tool"
LOCAL_CUSTOM_TOOL_DEFAULT_PATH = ".voidcode/tools"
LOCAL_CUSTOM_TOOL_MANIFEST_SUFFIX = ".json"
_MAX_LOCAL_CUSTOM_OUTPUT_BYTES = 50 * 1024
_LOCAL_CUSTOM_OUTPUT_CHUNK_BYTES = 8192
_VALID_TOOL_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/")


@dataclass(frozen=True, slots=True)
class LocalCustomToolManifest:
    name: str
    description: str
    input_schema: dict[str, object]
    command: tuple[str, ...]
    read_only: bool
    manifest_path: Path
    path_argument_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _BoundedProcessOutput:
    text: str
    truncated: bool


@dataclass(slots=True)
class _OutputCollector:
    chunks: list[bytes]
    byte_count: int = 0
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        remaining = max(0, _MAX_LOCAL_CUSTOM_OUTPUT_BYTES - self.byte_count)
        if remaining:
            retained = chunk[:remaining]
            self.chunks.append(retained)
            self.byte_count += len(retained)
        if len(chunk) > remaining:
            self.truncated = True

    def result(self) -> _BoundedProcessOutput:
        return _BoundedProcessOutput(
            b"".join(self.chunks).decode("utf-8", errors="replace").replace("\r\n", "\n"),
            self.truncated,
        )


def discover_local_custom_tools(
    workspace: Path,
    *,
    enabled: bool | None,
    relative_path: str = LOCAL_CUSTOM_TOOL_DEFAULT_PATH,
) -> tuple[LocalCustomTool, ...]:
    if enabled is not True:
        return ()
    workspace_root = workspace.resolve()
    root = (workspace_root / relative_path).resolve()
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
    manifest_path = path.resolve()
    try:
        manifest_path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"local custom tool manifest must stay inside workspace: {path}") from exc
    try:
        raw_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid local custom tool manifest at {path}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError(f"local custom tool manifest must be an object: {path}")
    payload = cast(dict[str, object], raw_payload)
    allowed_keys = {"name", "description", "input_schema", "command", "read_only", "path_argument_keys"}
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if unknown_keys:
        raise ValueError(f"local custom tool manifest {path} has unsupported field: {unknown_keys[0]}")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"local custom tool manifest {path} requires a non-empty name")
    normalized_name = name.strip()
    if any(char not in _VALID_TOOL_NAME_CHARS for char in normalized_name):
        raise ValueError(f"local custom tool manifest {path} has invalid name {normalized_name!r}; use letters, numbers, '_', '-', or '/'")

    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"local custom tool manifest {path} requires a non-empty description")

    input_schema = _parse_input_schema(payload.get("input_schema", {}), manifest_path=path)
    command = _parse_manifest_command(payload.get("command"), manifest_path=path)
    read_only = payload.get("read_only", True)
    if not isinstance(read_only, bool):
        raise ValueError(f"local custom tool manifest {path} read_only must be a boolean")
    path_argument_keys = _parse_path_argument_keys(payload.get("path_argument_keys", []), manifest_path=path)

    _validate_command_entrypoint(command, manifest_path=manifest_path, workspace=workspace)
    return LocalCustomToolManifest(
        name=normalized_name,
        description=description.strip(),
        input_schema=input_schema,
        command=command,
        read_only=read_only,
        manifest_path=manifest_path,
        path_argument_keys=path_argument_keys,
    )


def _parse_manifest_command(value: object, *, manifest_path: Path) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"local custom tool manifest {manifest_path} command must be an array")
    command: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str) or item == "":
            raise ValueError(f"local custom tool manifest {manifest_path} command[{index}] must be a non-empty string")
        command.append(item)
    if not command:
        raise ValueError(f"local custom tool manifest {manifest_path} command must contain at least one string")
    return tuple(command)


def _parse_path_argument_keys(value: object, *, manifest_path: Path) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"local custom tool manifest {manifest_path} path_argument_keys must be an array")
    path_argument_keys: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str) or not item:
            raise ValueError(f"local custom tool manifest {manifest_path} path_argument_keys[{index}] must be a non-empty string")
        path_argument_keys.append(item)
    return tuple(path_argument_keys)


def _parse_input_schema(value: object, *, manifest_path: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema must be an object")
    schema = cast(dict[str, object], value)
    try:
        json.dumps(schema)
    except TypeError as exc:
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema must be JSON serializable") from exc
    if schema.get("type") != "object":
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema.type must be 'object'")
    if not all(isinstance(key, str) for key in schema):
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema keys must be strings")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema.properties must be an object")
    if isinstance(properties, dict) and not all(isinstance(key, str) for key in properties):
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema.properties keys must be strings")
    required = schema.get("required")
    if required is not None and (not isinstance(required, list) or not all(isinstance(item, str) for item in required)):
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema.required must be strings")
    return dict(schema)


def _validate_command_entrypoint(command: tuple[str, ...], *, manifest_path: Path, workspace: Path) -> None:
    rendered_command = tuple(Template(part).safe_substitute(manifest_dir=str(manifest_path.parent)) for part in command)
    _validate_rendered_manifest_dir_command_parts(
        command,
        rendered_command=rendered_command,
        manifest_path=manifest_path,
        workspace=workspace,
    )


def _validate_rendered_manifest_dir_command_parts(
    command: tuple[str, ...],
    *,
    rendered_command: tuple[str, ...],
    manifest_path: Path,
    workspace: Path,
) -> None:
    manifest_dir = str(manifest_path.parent)
    token = "${manifest_dir}"
    for part, rendered_part in zip(command, rendered_command, strict=True):
        token_start = 0
        rendered_cursor = 0
        while True:
            token_start = part.find(token, token_start)
            if token_start < 0:
                break
            rendered_token_start = rendered_part.find(manifest_dir, rendered_cursor)
            if rendered_token_start < 0:
                break
            suffix = rendered_part[rendered_token_start + len(manifest_dir) :]
            candidate = Path(manifest_dir + suffix).expanduser().resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError as exc:
                raise ValueError(f"local custom tool manifest {manifest_path} command part using manifest_dir must stay inside workspace") from exc
            token_start += len(token)
            rendered_cursor = rendered_token_start + len(manifest_dir)


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
            read_only=self._manifest.read_only,
            path_argument_keys=self._manifest.path_argument_keys,
        )

    @property
    def source_fingerprint(self) -> str:
        payload = {
            "command": list(self._manifest.command),
            "description": self._manifest.description,
            "input_schema": self._manifest.input_schema,
            "name": self._manifest.name,
            "path_argument_keys": list(self._manifest.path_argument_keys),
            "read_only": self._manifest.read_only,
        }
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return sha256(encoded).hexdigest()

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        return self._invoke(call, workspace=workspace, timeout_seconds=None)

    def invoke_with_runtime_timeout(self, call: ToolCall, *, workspace: Path, timeout_seconds: int) -> ToolResult:
        return self._invoke(call, workspace=workspace, timeout_seconds=timeout_seconds)

    def _invoke(self, call: ToolCall, *, workspace: Path, timeout_seconds: int | None) -> ToolResult:
        resolved_workspace = workspace.resolve()
        command = self._render_command(workspace=resolved_workspace)
        _validate_rendered_manifest_dir_command_parts(
            self._manifest.command,
            rendered_command=command,
            manifest_path=self._manifest.manifest_path,
            workspace=resolved_workspace,
        )
        env = self._build_environment(call=call, workspace=resolved_workspace)
        start = time.monotonic()
        completed, stdout, stderr = self._run_command(
            command=command,
            workspace=resolved_workspace,
            env=env,
            input_text=json.dumps(call.arguments),
            timeout_seconds=timeout_seconds,
        )
        elapsed_ms = round((time.monotonic() - start) * 1000)
        output_truncated = stdout.truncated or stderr.truncated
        data: dict[str, object] = {
            "exit_code": completed.returncode,
            "elapsed_ms": elapsed_ms,
            "manifest": str(self._manifest.manifest_path),
        }
        if stderr.text:
            data["stderr"] = stderr.text
        if output_truncated:
            data["truncated"] = True
            data["stdout_truncated"] = stdout.truncated
            data["stderr_truncated"] = stderr.truncated
        if completed.returncode != 0:
            message = stderr.text.strip() or stdout.text.strip() or f"command exited with {completed.returncode}"
            return ToolResult(
                tool_name=self._manifest.name,
                status="error",
                content=stdout.text or None,
                data=data,
                error=message,
                diagnostics=ToolDiagnostics(kind="local_custom_tool_failed", summary=message, details={"tool_name": self._manifest.name}),
                truncated=output_truncated,
                partial=output_truncated,
                source=LOCAL_CUSTOM_TOOL_SOURCE,
            )
        return ToolResult(
            tool_name=self._manifest.name,
            status="ok",
            content=stdout.text or None,
            data=data,
            source=LOCAL_CUSTOM_TOOL_SOURCE,
            truncated=output_truncated,
            partial=output_truncated,
        )

    def _run_command(
        self,
        *,
        command: tuple[str, ...],
        workspace: Path,
        env: dict[str, str],
        input_text: str,
        timeout_seconds: int | None,
    ) -> tuple[subprocess.CompletedProcess[bytes], _BoundedProcessOutput, _BoundedProcessOutput]:
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ValueError(f"local custom tool '{self._manifest.name}' failed to execute: {exc}") from exc

        stdout_collector = _OutputCollector([])
        stderr_collector = _OutputCollector([])
        readers = (
            threading.Thread(target=_collect_output, args=(process.stdout, stdout_collector), daemon=True),
            threading.Thread(target=_collect_output, args=(process.stderr, stderr_collector), daemon=True),
        )
        for reader in readers:
            reader.start()
        try:
            if process.stdin is not None:
                try:
                    process.stdin.write(input_text.encode("utf-8"))
                    process.stdin.close()
                except OSError:
                    pass
            context = current_runtime_tool_context()
            abort_signal = context.abort_signal if context is not None else None
            deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
            while process.poll() is None:
                if abort_signal is not None and abort_signal.cancelled:
                    _kill_local_custom_process(process)
                    process.wait()
                    for reader in readers:
                        reader.join(timeout=1)
                    stdout, stderr = stdout_collector.result(), stderr_collector.result()
                    return subprocess.CompletedProcess(command, process.returncode, b"", b""), stdout, stderr
                if deadline is not None and time.monotonic() >= deadline:
                    _kill_local_custom_process(process)
                    process.wait()
                    for reader in readers:
                        reader.join(timeout=1)
                    stdout, stderr = stdout_collector.result(), stderr_collector.result()
                    raise RuntimeToolTimeoutError(
                        f"local custom tool '{self._manifest.name}' timed out after {timeout_seconds} seconds",
                        partial_result={"stdout": stdout.text, "stderr": stderr.text, "truncated": stdout.truncated or stderr.truncated},
                    )
                time.sleep(0.01)
            process.wait()
        finally:
            for reader in readers:
                reader.join(timeout=1)
        stdout, stderr = stdout_collector.result(), stderr_collector.result()
        return subprocess.CompletedProcess(command, process.returncode, b"", b""), stdout, stderr

    def _render_command(self, *, workspace: Path) -> tuple[str, ...]:
        return tuple(
            Template(part).safe_substitute(manifest_dir=str(self._manifest.manifest_path.parent), workspace=str(workspace))
            for part in self._manifest.command
        )

    def _build_environment(self, *, call: ToolCall, workspace: Path) -> dict[str, str]:
        env = dict(os.environ)
        context = current_runtime_tool_context()
        env["VOIDCODE_WORKSPACE"] = str(workspace)
        env["VOIDCODE_TOOL_NAME"] = self._manifest.name
        if call.tool_call_id is not None:
            env["VOIDCODE_TOOL_CALL_ID"] = call.tool_call_id
        if context is not None:
            env["VOIDCODE_SESSION_ID"] = context.session_id
            if context.parent_session_id is not None:
                env["VOIDCODE_PARENT_SESSION_ID"] = context.parent_session_id
            env["VOIDCODE_DELEGATION_DEPTH"] = str(context.delegation_depth)
        return env


def _collect_output(pipe: BinaryIO | None, collector: _OutputCollector) -> None:
    if pipe is None:
        return
    try:
        while True:
            chunk = pipe.read(_LOCAL_CUSTOM_OUTPUT_CHUNK_BYTES)
            if not chunk:
                return
            collector.append(chunk)
    finally:
        pipe.close()


def _kill_local_custom_process(process: subprocess.Popen[bytes]) -> None:
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


__all__ = ["LocalCustomTool", "LocalCustomToolManifest", "discover_local_custom_tools"]
