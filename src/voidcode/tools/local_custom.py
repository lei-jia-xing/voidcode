from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from string import Template
from typing import final

from ..common.json_values import json_wire_object, own_json_object
from ..core.tool_context import ToolContext
from .contracts import (
    EmptyOutput,
    OutputBounds,
    OutputReference,
    RuntimeToolTimeoutError,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)
from .process_output import ProcessOutputCapture, artifact_uri, collect_process_pipe

LOCAL_CUSTOM_TOOL_SOURCE = "local_custom_tool"
LOCAL_CUSTOM_TOOL_DEFAULT_PATH = ".voidcode/tools"
LOCAL_CUSTOM_TOOL_MANIFEST_SUFFIX = ".json"
_VALID_TOOL_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/")


@dataclass(frozen=True, slots=True)
class LocalCustomResultBody:
    exit_code: int
    elapsed_ms: int
    manifest: str
    stderr: str | None = None
    stdout_truncated: bool = False
    stdout_artifact: dict[str, object] | None = None
    stderr_artifact: dict[str, object] | None = None
    output_limit_exceeded: bool = False
    stderr_truncated: bool = False
    stop_confirmed: bool | None = None
    cancelled: bool = False

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "exit_code": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
            "manifest": self.manifest,
        }
        if self.stderr:
            payload["stderr"] = self.stderr
        if self.stdout_artifact is not None:
            payload["stdout_artifact"] = self.stdout_artifact
        if self.stderr_artifact is not None:
            payload["stderr_artifact"] = self.stderr_artifact
        if self.output_limit_exceeded:
            payload["output_limit_exceeded"] = True
        if self.stdout_truncated or self.stderr_truncated:
            payload.update(
                truncated=True,
                stdout_truncated=self.stdout_truncated,
                stderr_truncated=self.stderr_truncated,
            )
        if self.cancelled:
            payload["cancelled"] = True
        if self.stop_confirmed is not None:
            payload["stop_confirmed"] = self.stop_confirmed
        return payload


@dataclass(frozen=True, slots=True)
class LocalCustomToolManifest:
    name: str
    description: str
    input_schema: Mapping[str, object]
    command: tuple[str, ...]
    read_only: bool
    manifest_path: Path
    path_argument_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", own_json_object(self.input_schema))


@dataclass(frozen=True, slots=True)
class _BoundedProcessOutput:
    text: str
    truncated: bool
    output_limit_exceeded: bool = False
    artifact: dict[str, object] | None = None
    stop_confirmed: bool | None = None


def _artifact_reference(artifact: dict[str, object] | None) -> OutputReference | None:
    artifact_id = artifact.get("artifact_id") if artifact is not None else None
    if not isinstance(artifact_id, str):
        return None
    return OutputReference(f"voidcode://artifact/{artifact_id}", artifact)


def discover_local_custom_tool_manifests(
    workspace: Path,
    *,
    enabled: bool | None,
    relative_path: str = LOCAL_CUSTOM_TOOL_DEFAULT_PATH,
) -> tuple[LocalCustomToolManifest, ...]:
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

    return tuple(
        _load_local_custom_tool_manifest(path, workspace=workspace_root)
        for path in sorted(root.glob(f"*{LOCAL_CUSTOM_TOOL_MANIFEST_SUFFIX}"))
        if path.is_file()
    )


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
    payload = raw_payload
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
    for index, item in enumerate(value):
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
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"local custom tool manifest {manifest_path} path_argument_keys[{index}] must be a non-empty string")
        path_argument_keys.append(item)
    return tuple(path_argument_keys)


def _parse_input_schema(value: object, *, manifest_path: Path) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"local custom tool manifest {manifest_path} input_schema must be an object")
    schema = value
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
    return schema


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


def local_custom_tool_definition(manifest: LocalCustomToolManifest) -> ToolDefinition:
    return ToolDefinition(
        name=manifest.name,
        description=manifest.description,
        input_schema=manifest.input_schema,
        effects=frozenset({ToolEffect.READ, ToolEffect.EXECUTE, ToolEffect.SPAWN})
        if manifest.read_only
        else frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN}),
        replay_policy="safe" if manifest.read_only else "never",
        path_argument_keys=manifest.path_argument_keys,
    )


def local_custom_tool_source_fingerprint(manifest: LocalCustomToolManifest) -> str:
    payload = {
        "command": list(manifest.command),
        "description": manifest.description,
        "input_schema": json_wire_object(manifest.input_schema),
        "name": manifest.name,
        "path_argument_keys": list(manifest.path_argument_keys),
        "read_only": manifest.read_only,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


@final
class LocalCustomTool:
    def __init__(self, manifest: LocalCustomToolManifest) -> None:
        self._manifest = manifest
        self.definition = local_custom_tool_definition(manifest)

    @property
    def source_fingerprint(self) -> str:
        return local_custom_tool_source_fingerprint(self._manifest)

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        return self._invoke(call, context=context, timeout_seconds=None)

    def invoke_with_runtime_timeout(self, call: ToolCall, *, context: ToolContext, timeout_seconds: int) -> ToolResult:
        return self._invoke(call, context=context, timeout_seconds=timeout_seconds)

    def _invoke(self, call: ToolCall, *, context: ToolContext, timeout_seconds: int | None) -> ToolResult:
        workspace = context.require_workspace()
        resolved_workspace = workspace.resolve()
        command = self._render_command(workspace=resolved_workspace)
        _validate_rendered_manifest_dir_command_parts(
            self._manifest.command,
            rendered_command=command,
            manifest_path=self._manifest.manifest_path,
            workspace=resolved_workspace,
        )
        env = self._build_environment(call=call, context=context, workspace=resolved_workspace)
        start = time.monotonic()
        completed, stdout, stderr = self._run_command(
            command=command,
            context=context,
            workspace=resolved_workspace,
            env=env,
            input_text=json.dumps(json_wire_object(call.arguments), allow_nan=False),
            timeout_seconds=timeout_seconds,
            tool_call_id=context.invocation_id or call.tool_call_id,
        )
        elapsed_ms = round((time.monotonic() - start) * 1000)
        cancelled = context.abort_signal is not None and context.abort_signal.cancelled
        output_truncated = stdout.truncated or stderr.truncated
        output_limit_exceeded = stdout.output_limit_exceeded or stderr.output_limit_exceeded
        body = LocalCustomResultBody(
            exit_code=completed.returncode,
            elapsed_ms=elapsed_ms,
            manifest=str(self._manifest.manifest_path),
            stderr=stderr.text or None,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            stdout_artifact=stdout.artifact,
            stderr_artifact=stderr.artifact,
            output_limit_exceeded=output_limit_exceeded,
            cancelled=cancelled,
            stop_confirmed=stdout.stop_confirmed,
        )
        artifact_refs = [uri for artifact in (stdout.artifact, stderr.artifact) if (uri := artifact_uri(artifact)) is not None]
        bounds = OutputBounds(
            truncated=output_truncated,
            partial=output_truncated,
            reference=_artifact_reference(stdout.artifact or stderr.artifact) if output_truncated else None,
            source=LOCAL_CUSTOM_TOOL_SOURCE,
        )
        if output_truncated and artifact_refs:
            output_text = "Full process output: " + ", ".join(artifact_refs)
            if stderr.text:
                output_text += f"\n\n[stderr]\n{stderr.text}"
            if stdout.text:
                output_text += f"\n\n{stdout.text}"
        else:
            output_text = stdout.text
            if stderr.text:
                output_text = f"{output_text}\n[stderr]\n{stderr.text}" if output_text else stderr.text
        output = TextOutput(output_text, bounds=bounds) if output_text else EmptyOutput(bounds=bounds)
        if output_limit_exceeded:
            return ToolFailure(
                tool_name=self._manifest.name,
                error="local custom tool output exceeded the 128 MiB capture limit; partial output is attached.",
                output=output,
                body=body,
            )
        if cancelled:
            message = "Local custom tool was cancelled."
            return ToolFailure(
                tool_name=self._manifest.name,
                error=message,
                output=TextOutput(f"{message}\n\n{output_text}", bounds=bounds),
                body=body,
            )
        if completed.returncode != 0:
            message = stderr.text.strip() or stdout.text.strip() or f"command exited with {completed.returncode}"
            return ToolFailure(
                tool_name=self._manifest.name,
                error=message,
                output=output,
                body=body,
                diagnostics=ToolDiagnostics(
                    kind="local_custom_tool_failed",
                    summary=message,
                    details={"tool_name": self._manifest.name},
                ),
            )
        return ToolSuccess(tool_name=self._manifest.name, output=output, body=body)

    def _run_command(
        self,
        *,
        context: ToolContext,
        command: tuple[str, ...],
        workspace: Path,
        env: dict[str, str],
        input_text: str,
        timeout_seconds: int | None,
        tool_call_id: str | None,
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

        stdout_capture = ProcessOutputCapture.create("stdout")
        stderr_capture = ProcessOutputCapture.create("stderr")
        readers = (
            threading.Thread(target=collect_process_pipe, args=(process.stdout, stdout_capture), daemon=True),
            threading.Thread(target=collect_process_pipe, args=(process.stderr, stderr_capture), daemon=True),
        )
        for reader in readers:
            reader.start()
        stop_confirmed: bool | None = None
        started_at = time.monotonic()
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        abort_signal = context.abort_signal
        pending_input = memoryview(input_text.encode("utf-8"))
        input_offset = 0
        try:
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
            while process.poll() is None:
                if abort_signal is not None and abort_signal.cancelled:
                    stop_confirmed = _kill_local_custom_process(process)
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stop_confirmed = _kill_local_custom_process(process)
                    for reader in readers:
                        reader.join(timeout=1)
                    stdout, stderr = self._process_outputs(
                        stdout_capture,
                        stderr_capture,
                        context,
                        tool_call_id,
                        stop_confirmed=stop_confirmed,
                    )
                    message = f"local custom tool '{self._manifest.name}' timed out after {timeout_seconds} seconds"
                    output_limit_exceeded = stdout.output_limit_exceeded or stderr.output_limit_exceeded
                    output_truncated = stdout.truncated or stderr.truncated
                    body = LocalCustomResultBody(
                        exit_code=process.returncode,
                        elapsed_ms=round((time.monotonic() - started_at) * 1000),
                        manifest=str(self._manifest.manifest_path),
                        stderr=stderr.text or None,
                        stdout_truncated=stdout.truncated,
                        stderr_truncated=stderr.truncated,
                        stdout_artifact=stdout.artifact,
                        stderr_artifact=stderr.artifact,
                        output_limit_exceeded=output_limit_exceeded,
                        stop_confirmed=stop_confirmed,
                    )
                    artifacts = (stdout.artifact, stderr.artifact)
                    refs = [uri for item in artifacts if (uri := artifact_uri(item)) is not None]
                    if output_truncated and refs:
                        text = "Full process output: " + ", ".join(refs)
                        if stderr.text:
                            text += f"\n\n[stderr]\n{stderr.text}"
                        if stdout.text:
                            text += f"\n\n{stdout.text}"
                    else:
                        text = stdout.text
                        if stderr.text:
                            text = f"{text}\n[stderr]\n{stderr.text}" if text else stderr.text
                    partial = ToolFailure(
                        tool_name=self._manifest.name,
                        error=message,
                        output=TextOutput(
                            text,
                            bounds=OutputBounds(
                                truncated=output_truncated,
                                partial=True,
                                reference=_artifact_reference(stdout.artifact or stderr.artifact),
                                source=LOCAL_CUSTOM_TOOL_SOURCE,
                            ),
                        ),
                        body=body,
                    )
                    raise RuntimeToolTimeoutError(
                        message,
                        partial_result=partial,
                        execution_stopped=stop_confirmed,
                    )
                if process.stdin is not None and not process.stdin.closed:
                    if input_offset < len(pending_input):
                        try:
                            input_offset += os.write(
                                process.stdin.fileno(),
                                pending_input[input_offset:],
                            )
                        except BlockingIOError:
                            pass
                        except BrokenPipeError:
                            process.stdin.close()
                    if input_offset == len(pending_input) and not process.stdin.closed:
                        process.stdin.close()
                time.sleep(0.01)
            process.wait()
        except OSError:
            stop_confirmed = _kill_local_custom_process(process)
            raise
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            for reader in readers:
                reader.join(timeout=1)
        stdout, stderr = self._process_outputs(
            stdout_capture,
            stderr_capture,
            context,
            tool_call_id,
            stop_confirmed=stop_confirmed,
        )
        return subprocess.CompletedProcess(command, process.returncode, b"", b""), stdout, stderr

    def _process_outputs(
        self,
        stdout_capture: ProcessOutputCapture,
        stderr_capture: ProcessOutputCapture,
        context: ToolContext,
        tool_call_id: str | None,
        *,
        stop_confirmed: bool | None = None,
    ) -> tuple[_BoundedProcessOutput, _BoundedProcessOutput]:
        outputs: list[_BoundedProcessOutput] = []
        for capture in (stdout_capture, stderr_capture):
            artifact = capture.artifact(
                session_id=context.session_id,
                tool_call_id=tool_call_id,
                tool_name=self._manifest.name,
            )
            outputs.append(
                _BoundedProcessOutput(
                    capture.preview(),
                    capture.preview_truncated or capture.limit_exceeded,
                    capture.limit_exceeded,
                    artifact,
                    stop_confirmed,
                )
            )
            capture.close()
        return outputs[0], outputs[1]

    def _render_command(self, *, workspace: Path) -> tuple[str, ...]:
        return tuple(
            Template(part).safe_substitute(manifest_dir=str(self._manifest.manifest_path.parent), workspace=str(workspace))
            for part in self._manifest.command
        )

    def _build_environment(self, *, call: ToolCall, context: ToolContext, workspace: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["VOIDCODE_WORKSPACE"] = str(workspace)
        env["VOIDCODE_TOOL_NAME"] = self._manifest.name
        if call.tool_call_id is not None:
            env["VOIDCODE_TOOL_CALL_ID"] = call.tool_call_id
        if context.session_id is not None:
            env["VOIDCODE_SESSION_ID"] = context.require_session_id()
            if context.parent_session_id is not None:
                env["VOIDCODE_PARENT_SESSION_ID"] = context.parent_session_id
            env["VOIDCODE_DELEGATION_DEPTH"] = str(context.delegation_depth)
        return env


def _local_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_local_custom_process(process: subprocess.Popen[bytes]) -> bool:
    if os.name == "nt":
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
        return process.poll() is not None
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=1)
        except OSError, subprocess.TimeoutExpired:
            return False
    deadline = time.monotonic() + 1
    while _local_process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _local_process_group_exists(process.pid)


__all__ = [
    "LocalCustomTool",
    "LocalCustomToolManifest",
    "discover_local_custom_tool_manifests",
    "local_custom_tool_definition",
    "local_custom_tool_source_fingerprint",
]
