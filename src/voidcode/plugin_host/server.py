"""Executable stdio plugin host server.

Manages isolated plugin lifecycles, validates admitted artifacts, and
dispatches tool and provider invocations over bounded JSONL.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast
from uuid import uuid4

import jsonschema

from voidcode.sdk.codec import (
    decode_budget,
    decode_context,
    decode_provider_request,
    decode_session_info,
    decode_session_usage,
    decode_tool_call,
    encode_command_descriptor,
    encode_command_result,
    encode_provider_error,
    encode_provider_result,
    encode_tool_definition,
    encode_tool_result,
    encode_view_descriptor,
)
from voidcode.sdk.commands import (
    CommandDescriptor,
    CommandInvocation,
    CommandResult,
)
from voidcode.sdk.hooks import (
    ContextContribution,
    InvocationFinishedFact,
    PluginFact,
    RunFinishedFact,
    RunLifecycle,
    StopCause,
    ToolIntent,
    ToolOutcome,
    UsageFact,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.manifest import (
    PluginCommandDeclaration,
    PluginFileIdentity,
    PluginManifest,
    parse_plugin_manifest,
)
from voidcode.sdk.plugin import Plugin, command_definition, tool_definition
from voidcode.sdk.provider import ProviderExecutionError, ProviderTokenUsage
from voidcode.sdk.services import (
    ArtifactPage,
    ArtifactReference,
    ChildOutcome,
    EventView,
    PluginBudget,
    ProcessHandle,
    ProcessOutput,
    ProcessResult,
    SessionInfo,
    SessionUsageReport,
    ToolInvocationReceipt,
)
from voidcode.sdk.tools import ConfirmedStop, TextOutput, ToolFailure

from .processes import ProcessRegistry

MAX_FRAME_SIZE: int = 1024 * 1024  # 1 MiB

#: One artifact spill chunk, sized so its base64 form always fits one frame.
_ARTIFACT_CHUNK_BYTES: int = 256 * 1024


def _decode_event_views(response: Mapping[str, object]) -> tuple[EventView, ...]:
    raw = response.get("events")
    if not isinstance(raw, list):
        raise RuntimeError("host returned malformed event views")
    views: list[EventView] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise RuntimeError("host returned a malformed event view")
        payload = item.get("payload")
        views.append(
            EventView(
                sequence=_required_int(item, "sequence"),
                event_type=_required_text(item, "event_type"),
                run_id=_required_text(item, "run_id"),
                revision_id=_required_text(item, "revision_id"),
                source=_required_text(item, "source"),
                actor=_required_text(item, "actor"),
                payload=payload if isinstance(payload, Mapping) else None,
                payload_truncated=bool(item.get("payload_truncated", False)),
            )
        )
    return tuple(views)


def _decode_process_handle(payload: object) -> ProcessHandle:
    if not isinstance(payload, Mapping):
        raise RuntimeError("host returned a malformed process handle")
    return ProcessHandle(
        process_id=str(payload.get("process_id", "")),
        pid=int(payload.get("pid", 0)),
        start_time=str(payload.get("start_time", "")),
        stdout_path=str(payload.get("stdout_path", "")),
        stderr_path=str(payload.get("stderr_path", "")),
        running=bool(payload.get("running", False)),
        exit_code=None if payload.get("exit_code") is None else int(str(payload["exit_code"])),
    )


def _background_spool(server: PluginHostServer) -> Path:
    """Host-owned spool directory for a background process with no caller-chosen logs."""
    return server.background_spool


def _artifact_sha256(reference: ArtifactReference | Mapping[str, object] | str) -> str:
    """Accept the reference shapes a plugin legitimately holds and extract its digest."""
    if isinstance(reference, ArtifactReference):
        return reference.sha256
    if isinstance(reference, str):
        return reference
    if isinstance(reference, Mapping):
        value = reference.get("sha256")
        if isinstance(value, str):
            return value
    raise ValueError("artifact reference must carry a sha256 digest")


ALLOWED_REQUEST_KEYS: frozenset[str] = frozenset({"protocol_version", "id", "method", "params"})
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "init",
        "invoke",
        "propose_turn",
        "cancel",
        "close",
        "invoke_command",
        "render_view",
        "contribute_context",
        "run_lifecycle",
        "pre_tool",
        "post_tool",
        "notify_fact",
        "stop_background",
        "state_migrate",
        "state_prepare_deactivate",
    }
)
RESERVED_COMMAND_NAMES: frozenset[str] = frozenset({"run", "resume", "show", "cancel", "close", "init", "help", "status", "sessions", "catalog"})
RESERVED_COMMAND_PREFIXES: tuple[str, ...] = ("core.", "kernel.", "voidcode.")
#: Names the kernel itself registers, which a plugin can never shadow.
RESERVED_TOOL_NAMES: frozenset[str] = frozenset({"kernel_reload"})


class HostAbortSignal:
    """Thread-safe cancellation handle backed by a threading.Event."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event
        self._reason: str | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        with self._lock:
            self._reason = reason if value else None
        if value:
            self._event.set()
        else:
            self._event.clear()


def _report_internal_error(scope: str, exc: BaseException) -> None:
    """Log an unexpected host-side failure to stderr for operators.

    The wire response stays a redacted internal error, but the host process
    inherits the kernel's stderr so a real traceback (no API keys) is visible
    for debugging instead of only the exception class name.
    """
    print(f"[plugin-host] internal error in {scope}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()


def _required_text(params: Mapping[str, Any], key: str) -> str:
    """Read a required non-empty text parameter, failing visibly instead of defaulting."""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' parameter required as non-empty string")
    return value


def _optional_text(params: Mapping[str, Any], key: str, default: str = "") -> str:
    """Read an optional text parameter; an empty string is an explicit 'no value'."""
    value = params.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' parameter must be a string")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    """Read a required integer field without coercing strings or booleans."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{key}' must be an integer")
    return value


def _optional_number(payload: Mapping[str, Any], key: str) -> float | None:
    """Read an optional numeric field without coercing strings or booleans."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{key}' must be a number or null")
    return float(value)


def _check_arity(fn: Any, expected_arity: int, name: str) -> None:
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        pos_params = [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        var_args = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if not var_args and len(pos_params) != expected_arity:
            raise ValueError(f"{name} must accept {expected_arity} positional arguments, got {len(pos_params)}")
    except (ValueError, TypeError) as exc:
        if "must accept" in str(exc):
            raise


class ServerPluginServices:
    """Implementation of PluginServices running inside the plugin host server."""

    def __init__(
        self,
        server: PluginHostServer,
        lease_token: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._server = server
        self._lease_token = lease_token
        self._cancel_event = cancel_event
        self._stance: Mapping[str, Any] | None = None

    def _lease_stance(self) -> Mapping[str, Any]:
        """The kernel's stance for this lease; fetched once per invocation."""
        if self._stance is None:
            self._stance = self._server._send_reverse_rpc("lease_stance", {"lease_token": self._lease_token})
        return self._stance

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        stance = self._lease_stance()
        if stance.get("hook") or stance.get("readonly"):
            raise PermissionError("this lease may not run processes: read-only and hook leases do not touch the machine")
        workspace = self._server.workspace
        resolved_cwd = workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"run_process cwd is not a directory: {resolved_cwd}")
        return self._server.processes.run(
            argv,
            cwd=resolved_cwd,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancel_event=self._cancel_event,
            on_output=on_output,
        )

    def spill_artifact(
        self,
        data: bytes | str,
        *,
        media_type: str = "text/plain; charset=utf-8",
    ) -> ArtifactReference:
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        media_type = str(media_type)
        if not media_type:
            raise ValueError("spill_artifact media_type must not be empty")
        spill_id: str | None = None
        total = 0
        # One chunk per RPC keeps every frame well inside the protocol's bound.
        for start in range(0, max(len(payload), 1), _ARTIFACT_CHUNK_BYTES):
            chunk = payload[start : start + _ARTIFACT_CHUNK_BYTES]
            final = start + len(chunk) >= len(payload)
            resp = self._server._send_reverse_rpc(
                "artifact_spill",
                {
                    "lease_token": self._lease_token,
                    "spill_id": spill_id,
                    "media_type": media_type,
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "final": final,
                },
                timeout=60.0,
            )
            if final:
                return ArtifactReference(
                    sha256=str(resp.get("sha256", "")),
                    bytes=int(resp.get("bytes", total)),
                    media_type=str(resp.get("media_type", media_type)),
                )
            spill_id = str(resp.get("spill_id", ""))
            total = int(resp.get("bytes", 0))
        raise RuntimeError("artifact spill produced no chunks")

    def read_artifact(
        self,
        reference: ArtifactReference | Mapping[str, object] | str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        resp = self._server._send_reverse_rpc(
            "artifact_read",
            {
                "lease_token": self._lease_token,
                "sha256": _artifact_sha256(reference),
                "offset": int(offset),
                "limit": int(limit),
            },
            timeout=60.0,
        )
        try:
            decoded = base64.b64decode(str(resp.get("data", "")), validate=True)
        except Exception as exc:  # pragma: no cover - malformed host payload
            raise RuntimeError(f"host returned an unreadable artifact page: {exc}") from exc
        return ArtifactPage(
            sha256=str(resp.get("sha256", "")),
            offset=int(resp.get("offset", offset)),
            total_bytes=int(resp.get("total_bytes", 0)),
            data=decoded,
            eof=bool(resp.get("eof", False)),
            media_type=str(resp.get("media_type", "")),
        )

    def spawn_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bool = False,
        stdout_path: Path | str | None = None,
        stderr_path: Path | str | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        self._require_writable_stance("run background processes")
        identifier = process_id or f"proc-{uuid4().hex[:16]}"
        spool = self._server.background_spool
        resolved_cwd = self._server.workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"spawn_process cwd is not a directory: {resolved_cwd}")
        return self._server.processes.spawn(
            argv,
            session_id=str(self._lease_stance().get("session_id", "")),
            cwd=resolved_cwd,
            env=env,
            stdin=bool(stdin),
            stdout_path=Path(stdout_path) if stdout_path is not None else spool / f"{identifier}.stdout.log",
            stderr_path=Path(stderr_path) if stderr_path is not None else spool / f"{identifier}.stderr.log",
            process_id=identifier,
        )

    def write_process(self, process_id: str, data: bytes | str) -> int:
        self._require_writable_stance("write to background processes")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return self._server.processes.write(str(process_id), payload, session_id=self._session_id())

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        data, total, eof, running = self._server.processes.read_output(
            str(process_id),
            stream=stream,
            offset=int(offset),
            limit=int(limit),
            session_id=self._session_id(),
        )
        return ProcessOutput(
            process_id=str(process_id),
            stream=stream,
            offset=int(offset),
            total_bytes=total,
            data=data,
            eof=eof,
            running=running,
        )

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        self._require_writable_stance("stop background processes")
        del reason  # the stop is by process group; a reason would only be a log line we do not own
        return self._server.processes.cancel(str(process_id), session_id=self._session_id())

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        return self._server.processes.list_background(session_id=self._session_id())

    def _session_id(self) -> str:
        return str(self._lease_stance().get("session_id", ""))

    def _require_writable_stance(self, action: str) -> None:
        """Starting or stopping a process is machine work: a restrictive lease may not do it."""
        stance = self._lease_stance()
        if stance.get("hook") or stance.get("readonly"):
            raise PermissionError(f"this lease may not {action}: read-only and hook leases do not touch the machine")

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        resp = self._server._send_reverse_rpc("state_get", {"lease_token": self._lease_token, "scope": scope})
        state = resp.get("state")
        version = int(resp.get("version", 0))
        return (own_json_object(state) if isinstance(state, Mapping) else None, version)

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        resp = self._server._send_reverse_rpc("state_schema_version", {"lease_token": self._lease_token, "scope": scope})
        return int(resp.get("schema_version", 0))

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        resp = self._server._send_reverse_rpc(
            "state_replace",
            {
                "lease_token": self._lease_token,
                "scope": scope,
                "state": json_wire_object(state),
                "expected_version": expected_version,
            },
        )
        return int(resp.get("version", expected_version + 1))

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int = 32,
        readonly: bool = False,
    ) -> str:
        resp = self._server._send_reverse_rpc(
            "child_spawn",
            {
                "lease_token": self._lease_token,
                "prompt": prompt,
                "label": label,
                "max_steps": max_steps,
                "tool_allowlist": list(tool_allowlist) if tool_allowlist is not None else None,
                "readonly": readonly,
            },
        )
        return str(resp.get("child_session_id", ""))

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        resp = self._server._send_reverse_rpc(
            "child_join",
            {"lease_token": self._lease_token, "child_session_id": child_session_id, "timeout_seconds": timeout_seconds},
            timeout=timeout_seconds + 30.0,
        )
        payload = resp.get("outcome")
        if not isinstance(payload, Mapping):
            raise RuntimeError("host returned a malformed child outcome")
        status = str(payload.get("status", "failed"))
        if status not in ("completed", "failed", "interrupted", "cancelled", "running", "waiting"):
            raise RuntimeError(f"host returned an unknown child status: {status!r}")
        return ChildOutcome(
            child_session_id=str(payload.get("child_session_id", child_session_id)),
            status=status,
            output=None if payload.get("output") is None else str(payload["output"]),
            error=None if payload.get("error") is None else str(payload["error"]),
            stop_cause=None if payload.get("stop_cause") is None else str(payload["stop_cause"]),
        )

    def project_window(
        self,
        *,
        covered_from_sequence: int,
        covered_to_sequence: int,
        replacement: str,
    ) -> None:
        self._server._send_reverse_rpc(
            "window_project",
            {
                "lease_token": self._lease_token,
                "covered_from_sequence": covered_from_sequence,
                "covered_to_sequence": covered_to_sequence,
                "replacement": replacement,
            },
        )

    def window_projection(self) -> Mapping[str, object] | None:
        resp = self._server._send_reverse_rpc("window_projection_get", {"lease_token": self._lease_token})
        projection = resp.get("projection")
        return projection if isinstance(projection, Mapping) else None

    def clear_window_projection(self) -> bool:
        resp = self._server._send_reverse_rpc("window_projection_clear", {"lease_token": self._lease_token})
        return bool(resp.get("cleared", False))

    def set_overlay(self, tier: Literal["read-only"] = "read-only") -> None:
        self._server._send_reverse_rpc("overlay_set", {"lease_token": self._lease_token, "tier": tier})

    def clear_overlay(self) -> bool:
        resp = self._server._send_reverse_rpc("overlay_clear", {"lease_token": self._lease_token})
        return bool(resp.get("cleared", False))

    def schedule_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        resp = self._server._send_reverse_rpc(
            "wakeup_schedule",
            {
                "lease_token": self._lease_token,
                "payload": json_wire_object(payload),
                "due_at_ms": due_at_ms,
                "idempotency_key": idempotency_key,
                "role": role,
            },
        )
        return str(resp.get("wakeup_id", ""))

    def cancel_wakeup(self, wakeup_id: str) -> bool:
        resp = self._server._send_reverse_rpc(
            "wakeup_cancel",
            {"lease_token": self._lease_token, "wakeup_id": wakeup_id},
        )
        return bool(resp.get("cancelled", False))

    def read_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        resp = self._server._send_reverse_rpc(
            "events_read",
            {"lease_token": self._lease_token, "after_sequence": after_sequence, "limit": limit, "include_payload": include_payload},
        )
        return _decode_event_views(resp)

    def session_info(self) -> SessionInfo:
        response = self._server._send_reverse_rpc("session_info", {"lease_token": self._lease_token})
        payload = response.get("session")
        if not isinstance(payload, Mapping):
            raise RuntimeError("host returned malformed session information")
        return decode_session_info(payload)

    def read_child_events(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        response = self._server._send_reverse_rpc(
            "child_events_read",
            {
                "lease_token": self._lease_token,
                "child_session_id": child_session_id,
                "after_sequence": after_sequence,
                "limit": limit,
                "include_payload": include_payload,
            },
        )
        return _decode_event_views(response)

    def invoke_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        expected_revision: str,
    ) -> ToolInvocationReceipt:
        response = self._server._send_reverse_rpc(
            "invoke_tool",
            {
                "lease_token": self._lease_token,
                "name": name,
                "arguments": json_wire_object(arguments if arguments is not None else {}),
                "expected_revision": expected_revision,
            },
        )
        return ToolInvocationReceipt(
            request_id=_required_text(response, "request_id"),
            tool_name=_required_text(response, "tool_name"),
            depth=_required_int(response, "depth"),
        )

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None) -> str:
        resp = self._server._send_reverse_rpc(
            "model_query",
            {"lease_token": self._lease_token, "prompt": prompt, "model": model, "max_output_tokens": max_output_tokens},
            timeout=180.0,
        )
        return str(resp.get("output", ""))

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        resp = self._server._send_reverse_rpc(
            "child_cancel",
            {"lease_token": self._lease_token, "child_session_id": child_session_id, "reason": reason},
        )
        return bool(resp.get("cancelled", False))

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self._server._send_reverse_rpc(
            "event_stage",
            {
                "lease_token": self._lease_token,
                "event_type": event_type,
                "payload": json_wire_object(payload),
            },
        )

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        resp = self._server._send_reverse_rpc(
            "continuation_enqueue",
            {
                "role": role,
                "lease_token": self._lease_token,
                "payload": json_wire_object(payload),
                "idempotency_key": idempotency_key,
            },
        )
        return str(resp.get("continuation_id", ""))

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        self._server._send_reverse_rpc(
            "continuations_cancel",
            {
                "lease_token": self._lease_token,
                "continuation_id": continuation_id,
            },
        )

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        resp = self._server._send_reverse_rpc(
            "get_usage",
            {"lease_token": self._lease_token, "since_sequence": since_sequence, "until_sequence": until_sequence},
        )
        return decode_session_usage(resp)

    def get_budget(self) -> PluginBudget | None:
        resp = self._server._send_reverse_rpc("get_budget", {"lease_token": self._lease_token})
        raw = resp.get("budget")
        return decode_budget(raw) if isinstance(raw, Mapping) else None

    def set_budget(
        self,
        *,
        since_sequence: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        self._server._send_reverse_rpc(
            "set_budget",
            {
                "lease_token": self._lease_token,
                "since_sequence": since_sequence,
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "max_total_tokens": max_total_tokens,
                "max_cost_usd": max_cost_usd,
                "max_elapsed_seconds": max_elapsed_seconds,
            },
        )

    def clear_budget(self) -> None:
        self._server._send_reverse_rpc("clear_budget", {"lease_token": self._lease_token})


class PluginHostServer:
    """Stdio-driven plugin host executing plugins in an isolated process."""

    def __init__(self, in_stream: BinaryIO, out_stream: BinaryIO, *, standalone: bool = True) -> None:
        self._in_stream = in_stream
        self._out_stream = out_stream
        self._standalone = standalone
        self._write_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="plugin-worker")
        self._active_tasks: dict[str, threading.Event] = {}
        self._tasks_lock = threading.Lock()
        self._shutdown_event = threading.Event()

        self._workspace: Path | None = None
        self._revision_id: str | None = None
        self._plugins: list[Plugin] = []
        self._tools: dict[str, tuple[Any, Any, str]] = {}  # name -> (tool, decl, plugin_id)
        self._providers: dict[str, tuple[Any, Any, str]] = {}  # name -> (provider, decl, plugin_id)
        self._commands: dict[str, tuple[Any, PluginCommandDeclaration, str]] = {}
        self._views: dict[str, tuple[Any, str]] = {}
        self._context_callbacks: dict[str, Callable[[ToolContext, ServerPluginServices], ContextContribution | None]] = {}
        self._post_tool_hooks: list[tuple[Callable[[object, ToolContext, ServerPluginServices], str | None], tuple[str, ...], bool, str]] = []
        self._pre_tool_hooks: list[tuple[Callable[[object, ToolContext, ServerPluginServices], str | None], tuple[str, ...], bool, str]] = []
        self._lifecycle_hooks: dict[str, tuple[Callable[[object, ToolContext, ServerPluginServices], str | None], str]] = {}
        self._observers: list[tuple[Callable[[PluginFact, ToolContext, ServerPluginServices], None], tuple[str, ...], str]] = []
        self._state_migrators: dict[str, Callable[[Mapping[str, Any], int, int], Mapping[str, Any]]] = {}
        self._state_deactivators: dict[str, Callable[[Mapping[str, Any], str, str, int | None], Mapping[str, Any]]] = {}
        self._pending_rpc: dict[str, Future[dict[str, Any]]] = {}
        self._rpc_counter: int = 0
        self._rpc_lock = threading.Lock()
        #: Every child process this host started for a lease; reaped on shutdown.
        self.processes = ProcessRegistry()
        #: Where a background process spools its output when the caller names no log path.
        self.background_spool = Path(tempfile.mkdtemp(prefix="voidcode-process-"))

    @property
    def workspace(self) -> Path:
        """The workspace this host generation was initialized for."""
        if self._workspace is None:
            raise RuntimeError("plugin host has no workspace yet")
        return self._workspace

    def _send_progress(self, lease_token: str, payload: Mapping[str, object]) -> None:
        """Use the existing reverse-RPC writer without making a reporting tool wait for an ack."""
        try:
            with self._rpc_lock:
                self._rpc_counter += 1
                rpc_id = f"progress-{self._rpc_counter}"
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": rpc_id,
                    "method": "progress_notify",
                    "params": {"lease_token": lease_token, "payload": json_wire_object(payload)},
                }
            )
        except Exception:
            # Progress is observational; loss of a report must not replace the tool's result.
            return

    def _send_reverse_rpc(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        with self._rpc_lock:
            self._rpc_counter += 1
            rpc_id = f"rpc-{self._rpc_counter}"
            fut: Future[dict[str, Any]] = Future()
            self._pending_rpc[rpc_id] = fut
        try:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": rpc_id,
                    "method": method,
                    "params": params,
                }
            )
            return fut.result(timeout=timeout)
        finally:
            with self._rpc_lock:
                self._pending_rpc.pop(rpc_id, None)

    def _send_response(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload, allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME_SIZE:
            err_payload = {
                "protocol_version": 1,
                "id": str(payload.get("id", "unknown")),
                "ok": False,
                "error": {"kind": "frame_too_large", "message": "Response exceeds 1MiB frame limit"},
            }
            data = json.dumps(err_payload, allow_nan=False).encode("utf-8") + b"\n"
        with self._write_lock:
            self._out_stream.write(data)
            self._out_stream.flush()

    def run(self) -> int:
        """Main control loop reading commands from binary input stream."""
        try:
            while not self._shutdown_event.is_set():
                raw_line = self._in_stream.readline(MAX_FRAME_SIZE + 1)
                if not raw_line:
                    # EOF
                    break
                if len(raw_line) > MAX_FRAME_SIZE or not raw_line.endswith(b"\n"):
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": "unknown",
                            "ok": False,
                            "error": {"kind": "frame_too_large", "message": "Frame exceeds 1MiB limit or missing newline"},
                        }
                    )
                    break

                line = raw_line.strip()
                if not line:
                    continue

                try:
                    message = json.loads(line.decode("utf-8"))
                except Exception as e:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": "unknown",
                            "ok": False,
                            "error": {"kind": "invalid_json", "message": f"Malformed JSON: {type(e).__name__}"},
                        }
                    )
                    break

                if not isinstance(message, dict):
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": "unknown",
                            "ok": False,
                            "error": {"kind": "invalid_envelope", "message": "Frame must be a JSON object"},
                        }
                    )
                    break

                if "ok" in message and "method" not in message:
                    rpc_id = str(message.get("id", ""))
                    with self._rpc_lock:
                        fut = self._pending_rpc.get(rpc_id)
                    if fut is not None and not fut.done():
                        if message.get("ok"):
                            fut.set_result(message.get("result", {}))
                        else:
                            err_info = message.get("error", {})
                            msg = err_info.get("message", "Reverse RPC failed") if isinstance(err_info, dict) else "Reverse RPC failed"
                            fut.set_exception(RuntimeError(msg))
                    continue

                if not self._dispatch(message):
                    break
        finally:
            self._cleanup()
        return 0

    def _dispatch(self, message: dict[str, Any]) -> bool:
        """Validate envelope and dispatch command. Returns False if fatal envelope error."""
        if set(message.keys()) != ALLOWED_REQUEST_KEYS:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": str(message.get("id", "unknown")),
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Request envelope keys mismatch"},
                }
            )
            return False

        version = message["protocol_version"]
        if type(version) is not int or version != 1:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": str(message.get("id", "unknown")),
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Unsupported or non-integer protocol_version"},
                }
            )
            return False

        req_id = message["id"]
        if not isinstance(req_id, str) or not req_id:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": "unknown",
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Request id must be a non-empty string"},
                }
            )
            return False

        method = message["method"]
        if not isinstance(method, str) or method not in ALLOWED_METHODS:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": False,
                    "error": {"kind": "unknown_method", "message": f"Unknown or disallowed method: {method}"},
                }
            )
            return True

        params = message["params"]
        if not isinstance(params, dict):
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Request params must be a JSON object"},
                }
            )
            return False

        if method == "init":
            if set(params.keys()) != {"workspace", "revision_id", "plugins"}:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Init params must contain workspace, revision_id, plugins"},
                    }
                )
                return False
            self._handle_init(req_id, params)
        elif method == "cancel":
            if set(params.keys()) != {"target_id"}:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Cancel params must contain only target_id"},
                    }
                )
                return True
            self._handle_cancel(req_id, params)
        elif method == "close":
            if set(params.keys()) != set():
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Close params must be empty"},
                    }
                )
                return False
            self._handle_close(req_id)
            return False
        elif method == "invoke":
            if (
                set(params.keys()) not in ({"call", "context"}, {"call", "context", "lease_token"})
                or not isinstance(params["call"], dict)
                or not isinstance(params["context"], dict)
            ):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Invoke params must contain call and context objects"},
                    }
                )
                return True
            self._handle_invoke(req_id, params)
        elif method == "propose_turn":
            if set(params.keys()) not in ({"request"}, {"request", "provider"}) or not isinstance(params["request"], dict):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Propose turn params must contain request object"},
                    }
                )
                return True
            self._handle_propose_turn(req_id, params)
        elif method == "invoke_command":
            if (
                set(params.keys())
                not in ({"name", "arguments", "context", "lease_token"}, {"name", "arguments", "context", "lease_token", "client_capabilities"})
                or not isinstance(params.get("arguments"), dict)
                or not isinstance(params.get("context"), dict)
            ):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {
                            "kind": "invalid_params",
                            "message": "Invoke command params must contain name, arguments, context, and lease_token",
                        },
                    }
                )
                return True
            self._handle_invoke_command(req_id, params)
        elif method == "render_view":
            if set(params.keys()) not in (
                {"name", "session_id", "lease_token", "run_id", "invocation_id"},
                {"name", "session_id", "lease_token", "run_id", "invocation_id", "client_capabilities"},
            ):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Render view params must contain name, session_id, and lease_token"},
                    }
                )
                return True
            self._handle_render_view(req_id, params)
        elif method == "run_lifecycle":
            self._handle_run_lifecycle(req_id, params)
        elif method == "pre_tool":
            self._handle_pre_tool(req_id, params)
        elif method == "post_tool":
            self._handle_post_tool(req_id, params)
        elif method == "contribute_context":
            if set(params.keys()) not in (
                {"session_id", "logical_turn", "lease_token", "run_id", "invocation_id"},
                {"session_id", "owner", "logical_turn", "lease_token", "run_id", "invocation_id"},
            ):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {
                            "kind": "invalid_params",
                            "message": "Contribute context params must contain session_id, logical_turn, and lease_token",
                        },
                    }
                )
                return True
            self._handle_contribute_context(req_id, params)
        elif method == "stop_background":
            if set(params.keys()) != set():
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "stop_background params must be empty"},
                    }
                )
                return True
            self._handle_stop_background(req_id)
        elif method == "notify_fact":
            if set(params.keys()) not in (
                {"fact_type", "fact", "logical_turn", "lease_token", "invocation_id"},
                {"fact_type", "fact", "owner", "logical_turn", "lease_token", "invocation_id"},
            ) or not isinstance(params.get("fact"), dict):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {
                            "kind": "invalid_params",
                            "message": "Notify fact params must contain fact_type, fact object, logical_turn, and lease_token",
                        },
                    }
                )
                return True
            self._handle_notify_fact(req_id, params)
        elif method == "state_migrate":
            if set(params.keys()) != {"owner", "payload", "from_version", "to_version"} or not isinstance(params.get("payload"), dict):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {
                            "kind": "invalid_params",
                            "message": "state_migrate params must contain owner, payload, from_version, and to_version",
                        },
                    }
                )
                return True
            self._handle_state_migrate(req_id, params)
        elif method == "state_prepare_deactivate":
            if set(params.keys()) != {"owner", "payload", "scope", "scope_key", "tip_sequence"} or not isinstance(params.get("payload"), dict):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {
                            "kind": "invalid_params",
                            "message": "state_prepare_deactivate params must contain owner, payload, scope, scope_key, and tip_sequence",
                        },
                    }
                )
                return True
            self._handle_state_prepare_deactivate(req_id, params)
        return True

    def _handle_init(self, req_id: str, params: dict[str, Any]) -> None:
        try:
            workspace_raw = params.get("workspace")
            if not workspace_raw or not isinstance(workspace_raw, str):
                raise ValueError("workspace parameter required as non-empty string")
            self._workspace = Path(workspace_raw).resolve()
            if not self._workspace.is_dir():
                raise ValueError(f"Workspace directory does not exist: {self._workspace}")

            revision_id_raw = params.get("revision_id")
            if not revision_id_raw or not isinstance(revision_id_raw, str):
                raise ValueError("revision_id parameter required as non-empty string")
            self._revision_id = revision_id_raw

            plugins_spec = params.get("plugins")
            if not isinstance(plugins_spec, list):
                raise ValueError("plugins parameter must be a list")

            declared_modules: set[str] = set()

            for spec in plugins_spec:
                if not isinstance(spec, dict):
                    raise ValueError("plugin spec must be a dictionary")

                root_raw = spec.get("root")
                if not root_raw or not isinstance(root_raw, str):
                    raise ValueError("plugin root required")
                root = Path(root_raw).resolve()
                if not root.is_dir():
                    raise ValueError(f"Plugin root is not a directory: {root}")

                manifest = parse_plugin_manifest(spec.get("manifest"))
                config = spec.get("config", {})
                granted_caps = frozenset(str(c) for c in spec.get("capabilities", []))

                for cmd_decl in manifest.commands:
                    if cmd_decl.name in RESERVED_COMMAND_NAMES or any(cmd_decl.name.startswith(p) for p in RESERVED_COMMAND_PREFIXES):
                        raise ValueError(f"Command '{cmd_decl.name}' in plugin {manifest.plugin_id} is a reserved command name or prefix")
                    req_caps = frozenset(cmd_decl.capabilities)
                    if not req_caps.issubset(granted_caps):
                        missing = req_caps - granted_caps
                        raise ValueError(f"Plugin {manifest.plugin_id} command {cmd_decl.name} requires capabilities {set(missing)} not granted")
                # Verify files and hashes
                self._check_plugin_files(root, manifest.files)

                # Validate entrypoint
                module_name, func_name = self._validate_entrypoint(manifest)

                # Check top-level module collision
                top_module = module_name.split(".", 1)[0]
                if module_name != "__init__":
                    if top_module in declared_modules:
                        raise ValueError(f"Duplicate plugin top-level module name across plugins: {top_module}")
                    declared_modules.add(top_module)
                # Verify all required tool capabilities are granted before import
                for tool_decl in manifest.tools:
                    if tool_decl.name in RESERVED_TOOL_NAMES:
                        raise ValueError(f"Tool '{tool_decl.name}' in plugin {manifest.plugin_id} is a reserved kernel tool name")
                    req_caps = frozenset(tool_decl.capabilities)
                    if not req_caps.issubset(granted_caps):
                        missing = req_caps - granted_caps
                        raise ValueError(f"Plugin {manifest.plugin_id} tool {tool_decl.name} requires capabilities {set(missing)} not granted")

                # Validate config schema using wire-safe objects
                if manifest.config_schema is not None:
                    try:
                        schema_wire = json_wire_object(manifest.config_schema)
                        config_wire = json_wire_object(config)
                        jsonschema.validate(instance=config_wire, schema=schema_wire)
                    except jsonschema.ValidationError as err:
                        raise ValueError(f"Plugin config violates schema: {err.message}") from err

                # Check top-level module conflict with pre-imported modules
                if top_module in sys.modules:
                    existing = sys.modules[top_module]
                    existing_file = getattr(existing, "__file__", None)
                    if existing_file:
                        try:
                            Path(existing_file).resolve().relative_to(root)
                        except ValueError as err:
                            raise ValueError(f"Top-level module conflict: module {top_module} already imported from outside root") from err

                if module_name == "__init__":
                    pkg_name = f"voidcode_plugin_{manifest.plugin_id.replace('-', '_')}"
                    spec = importlib.util.spec_from_file_location(
                        pkg_name,
                        root / "__init__.py",
                        submodule_search_locations=[str(root)],
                    )
                    if spec is None or spec.loader is None:
                        raise ValueError(f"Failed to create package spec for plugin {manifest.plugin_id}")
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[pkg_name] = mod
                    spec.loader.exec_module(mod)
                else:
                    # Add root to sys.path and import module
                    root_str = str(root)
                    if root_str not in sys.path:
                        sys.path.insert(0, root_str)
                    mod = importlib.import_module(module_name)
                mod_file = getattr(mod, "__file__", None)
                if not mod_file:
                    raise ValueError(f"Imported module {module_name} has no __file__")
                try:
                    Path(mod_file).resolve().relative_to(root)
                except ValueError as err:
                    raise ValueError(f"Module {module_name} was imported from outside artifact root: {mod_file}") from err

                factory = getattr(mod, func_name, None)
                if factory is None or not callable(factory):
                    raise ValueError(f"Factory callable {func_name} not found in {module_name}")

                if hasattr(factory, "__code__"):
                    factory_file = Path(factory.__code__.co_filename).resolve()
                    try:
                        factory_file.relative_to(root)
                    except ValueError as err:
                        raise ValueError(f"Factory callable code origin is outside artifact root: {factory_file}") from err

                try:
                    plugin_instance = factory(config)
                except Exception as exc:
                    raise ValueError(f"Plugin factory failed with {type(exc).__name__}") from exc
                if not isinstance(plugin_instance, Plugin):
                    raise ValueError(f"Factory returned {type(plugin_instance).__name__}, expected Plugin")

                # Verify registered tools match manifest exactly without duplicate registered names
                seen_reg_tools: set[str] = set()
                reg_tools_dict: dict[str, Any] = {}
                for t in plugin_instance.tools:
                    name = t.definition.name
                    if name in seen_reg_tools:
                        raise ValueError(f"Plugin {manifest.plugin_id} registered duplicate tool name: {name}")
                    seen_reg_tools.add(name)
                    reg_tools_dict[name] = t

                manifest_tools_dict = {t.name: t for t in manifest.tools}
                reg_names = set(reg_tools_dict)
                manifest_names = set(manifest_tools_dict)
                if reg_names != manifest_names:
                    raise ValueError(f"Plugin {manifest.plugin_id} tools mismatch: registered {reg_names} vs manifest {manifest_names}")

                # Full definition comparison
                for name, t in reg_tools_dict.items():
                    decl = manifest_tools_dict[name]
                    expected_def = tool_definition(decl)
                    if t.definition != expected_def:
                        raise ValueError(f"Tool {name} definition mismatch with manifest: registered {t.definition} vs {expected_def}")

                # Verify registered providers match manifest exactly without duplicate registered names
                seen_reg_provs: set[str] = set()
                reg_provs_dict: dict[str, Any] = {}
                for p in plugin_instance.providers:
                    name = p.name
                    if name in seen_reg_provs:
                        raise ValueError(f"Plugin {manifest.plugin_id} registered duplicate provider name: {name}")
                    seen_reg_provs.add(name)
                    reg_provs_dict[name] = p

                manifest_provs_dict = {p.name: p for p in manifest.providers}
                reg_prov_names = set(reg_provs_dict)
                manifest_prov_names = set(manifest_provs_dict)
                if reg_prov_names != manifest_prov_names:
                    raise ValueError(f"Plugin {manifest.plugin_id} providers mismatch: registered {reg_prov_names} vs manifest {manifest_prov_names}")

                # Verify registered commands match manifest exactly
                seen_reg_cmds: set[str] = set()
                reg_cmds_dict: dict[str, Any] = {}
                for c in plugin_instance.commands:
                    name = c.definition.name
                    if name in seen_reg_cmds:
                        raise ValueError(f"Plugin {manifest.plugin_id} registered duplicate command name: {name}")
                    seen_reg_cmds.add(name)
                    reg_cmds_dict[name] = c

                manifest_cmds_dict = {c.name: c for c in manifest.commands}
                reg_cmd_names = set(reg_cmds_dict)
                manifest_cmd_names = set(manifest_cmds_dict)
                if reg_cmd_names != manifest_cmd_names:
                    raise ValueError(f"Plugin {manifest.plugin_id} commands mismatch: registered {reg_cmd_names} vs manifest {manifest_cmd_names}")

                for name, c in reg_cmds_dict.items():
                    decl = manifest_cmds_dict[name]
                    expected_def = command_definition(decl)
                    if c.definition != expected_def:
                        raise ValueError(f"Command {name} definition mismatch with manifest: registered {c.definition} vs {expected_def}")

                # Verify registered views
                seen_reg_views: set[str] = set()
                reg_views_dict: dict[str, Any] = {}
                for v in plugin_instance.views:
                    name = v.name
                    if name in seen_reg_views:
                        raise ValueError(f"Plugin {manifest.plugin_id} registered duplicate view name: {name}")
                    seen_reg_views.add(name)
                    reg_views_dict[name] = v

                for name, cmd in reg_cmds_dict.items():
                    if name in self._commands:
                        raise ValueError(f"Duplicate command name registered across plugins: {name}")
                    self._commands[name] = (cmd, manifest_cmds_dict[name], manifest.plugin_id)

                for name, view in reg_views_dict.items():
                    if name in self._views:
                        raise ValueError(f"Duplicate view name registered across plugins: {name}")
                    self._views[name] = (view, manifest.plugin_id)
                if manifest.context and plugin_instance.context is None:
                    raise ValueError(f"Plugin {manifest.plugin_id} declared context=True but provided no context callback")
                if not manifest.context and plugin_instance.context is not None:
                    raise ValueError(f"Plugin {manifest.plugin_id} provided context callback but declared context=False")
                if plugin_instance.context is not None:
                    _check_arity(plugin_instance.context, 2, f"Plugin {manifest.plugin_id} context callback")
                    self._context_callbacks[manifest.plugin_id] = plugin_instance.context

                if manifest.observed_facts and plugin_instance.observer is None:
                    raise ValueError(
                        f"Plugin {manifest.plugin_id} declared observed_facts={manifest.observed_facts} but provided no observer callback"
                    )
                if not manifest.observed_facts and plugin_instance.observer is not None:
                    raise ValueError(f"Plugin {manifest.plugin_id} provided observer callback but declared no observed_facts")
                if plugin_instance.observer is not None:
                    _check_arity(plugin_instance.observer, 3, f"Plugin {manifest.plugin_id} observer callback")
                    self._observers.append((plugin_instance.observer, manifest.observed_facts, manifest.plugin_id))
                post_tool_decls = [decl for decl in manifest.hooks if decl.kind == "post_tool"]
                if bool(post_tool_decls) != (plugin_instance.post_tool is not None):
                    raise ValueError(f"Plugin {manifest.plugin_id} must declare a post_tool hook exactly when it provides the callback")
                if plugin_instance.post_tool is not None:
                    _check_arity(plugin_instance.post_tool, 3, f"Plugin {manifest.plugin_id} post_tool callback")
                    for decl in post_tool_decls:
                        self._post_tool_hooks.append((plugin_instance.post_tool, decl.match, decl.mutating, manifest.plugin_id))
                for kind, attribute in (("session_start", "session_start"), ("session_end", "session_end")):
                    declares = any(decl.kind == kind for decl in manifest.hooks)
                    callback = getattr(plugin_instance, attribute)
                    if declares != (callback is not None):
                        raise ValueError(f"Plugin {manifest.plugin_id} must declare a {kind} hook exactly when it provides the callback")
                    if callback is not None:
                        _check_arity(callback, 3, f"Plugin {manifest.plugin_id} {kind} callback")
                        self._lifecycle_hooks[f"{manifest.plugin_id}\0{kind}"] = (callback, manifest.plugin_id)
                pre_tool_decls = [decl for decl in manifest.hooks if decl.kind == "pre_tool"]
                if bool(pre_tool_decls) != (plugin_instance.pre_tool is not None):
                    raise ValueError(f"Plugin {manifest.plugin_id} must declare a pre_tool hook exactly when it provides the callback")
                if plugin_instance.pre_tool is not None:
                    _check_arity(plugin_instance.pre_tool, 3, f"Plugin {manifest.plugin_id} pre_tool callback")
                    for decl in pre_tool_decls:
                        self._pre_tool_hooks.append((plugin_instance.pre_tool, decl.match, decl.mutating, manifest.plugin_id))
                if plugin_instance.migrate_state is not None:
                    _check_arity(plugin_instance.migrate_state, 3, f"Plugin {manifest.plugin_id} migrate_state callback")
                    self._state_migrators[manifest.plugin_id] = plugin_instance.migrate_state
                if plugin_instance.prepare_deactivate is not None:
                    _check_arity(plugin_instance.prepare_deactivate, 4, f"Plugin {manifest.plugin_id} prepare_deactivate callback")
                    self._state_deactivators[manifest.plugin_id] = plugin_instance.prepare_deactivate
                # Store registrations
                for name, tool in reg_tools_dict.items():
                    if name in self._tools:
                        raise ValueError(f"Duplicate tool name registered across plugins: {name}")
                    self._tools[name] = (tool, manifest_tools_dict[name], manifest.plugin_id)

                for name, prov in reg_provs_dict.items():
                    if name in self._providers:
                        raise ValueError(f"Duplicate provider name registered across plugins: {name}")
                    self._providers[name] = (prov, manifest_provs_dict[name], manifest.plugin_id)

                self._plugins.append(plugin_instance)

            # Build catalog
            catalog_tools = [encode_tool_definition(tool.definition) for tool, _, _ in self._tools.values()]
            catalog_providers = list(self._providers.keys())
            catalog_commands = [
                encode_command_descriptor(
                    CommandDescriptor(
                        name=decl.name,
                        owner=plugin_id,
                        description=decl.description,
                        argument_schema=decl.argument_schema,
                        effects=decl.effects,
                        capabilities=decl.capabilities,
                        busy_gate=decl.busy_gate,
                        client_capabilities=decl.client_capabilities,
                        direct_user=decl.direct_user,
                        client_syntax=decl.client_syntax,
                    )
                )
                for _, decl, plugin_id in self._commands.values()
            ]
            owners = {name: plugin_id for name, (_, _, plugin_id) in self._tools.items()}
            capabilities = {name: list(decl.capabilities) for name, (_, decl, _) in self._tools.items()}

            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": True,
                    "result": {
                        "tools": catalog_tools,
                        "providers": catalog_providers,
                        "commands": catalog_commands,
                        "views": list(self._views.keys()),
                        "view_owners": {name: plugin_id for name, (_, plugin_id) in self._views.items()},
                        "tool_owners": owners,
                        "tool_capabilities": capabilities,
                        "context_owners": list(self._context_callbacks.keys()),
                        "post_tool_hooks": [
                            {"owner": pid, "match": list(match), "mutating": mutating} for _, match, mutating, pid in self._post_tool_hooks
                        ],
                        "pre_tool_hooks": [
                            {"owner": pid, "match": list(match), "mutating": mutating} for _, match, mutating, pid in self._pre_tool_hooks
                        ],
                        "lifecycle_hooks": sorted(f"{pid}\0{kind}" for pid, kind in (key.split("\0") for key in self._lifecycle_hooks)),
                        "observer_owners": {
                            fact_type: [pid for _, obs_facts, pid in self._observers if fact_type in obs_facts]
                            for fact_type in ("usage", "invocation_finished", "run_finished")
                        },
                    },
                }
            )
        except Exception as e:
            err_msg = str(e) if isinstance(e, ValueError) else f"Initialization failed with {type(e).__name__}: {e}"
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": False,
                    "error": {"kind": "init_failed", "message": err_msg},
                }
            )

    def _check_plugin_files(self, root: Path, declared_files: tuple[PluginFileIdentity, ...]) -> None:
        declared_paths: set[str] = set()
        for f in declared_files:
            declared_paths.add(f.path)
            file_path = root / f.path
            if not file_path.is_file():
                raise ValueError(f"Declared plugin file missing: {f.path}")
            computed = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if computed != f.sha256:
                raise ValueError(f"File hash mismatch for {f.path}: expected {f.sha256}, got {computed}")

        for py_path in root.rglob("*.py"):
            rel = py_path.relative_to(root).as_posix()
            if rel not in declared_paths:
                raise ValueError(f"Undeclared python file in artifact root: {rel}")

    def _validate_entrypoint(self, manifest: PluginManifest) -> tuple[str, str]:
        if ":" not in manifest.entrypoint:
            raise ValueError(f"Invalid entrypoint format: {manifest.entrypoint}")
        module_name, func_name = manifest.entrypoint.split(":", 1)
        if not module_name or not func_name:
            raise ValueError(f"Invalid entrypoint format: {manifest.entrypoint}")

        declared_paths = {f.path for f in manifest.files}
        expected_file = f"{module_name.replace('.', '/')}.py"
        expected_package = f"{module_name.replace('.', '/')}/__init__.py"
        if module_name == "__init__" and "__init__.py" in declared_paths:
            pass
        elif expected_file not in declared_paths and expected_package not in declared_paths:
            raise ValueError(f"Entrypoint module {module_name} not found in declared files (expected {expected_file} or {expected_package})")
        return module_name, func_name

    def _handle_cancel(self, req_id: str, params: dict[str, Any]) -> None:
        target_id = params.get("target_id")
        with self._tasks_lock:
            event = self._active_tasks.get(str(target_id))
        if event is not None:
            event.set()
        self._send_response(
            {
                "protocol_version": 1,
                "id": req_id,
                "ok": True,
                "result": {"cancelled": True},
            }
        )

    def _handle_close(self, req_id: str) -> None:
        self._send_response(
            {
                "protocol_version": 1,
                "id": req_id,
                "ok": True,
                "result": {"closed": True},
            }
        )
        self._shutdown_event.set()

    def _handle_stop_background(self, req_id: str) -> None:
        """The owner is shutting down: supervised processes end with it, by group."""
        self.processes.stop_background()
        self._send_response(
            {
                "protocol_version": 1,
                "id": req_id,
                "ok": True,
                "result": {"stopped": True},
            }
        )

    def _handle_invoke(self, req_id: str, params: dict[str, Any]) -> None:
        cancel_event = threading.Event()
        with self._tasks_lock:
            if req_id in self._active_tasks:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "duplicate_id", "message": f"Request id '{req_id}' is already active"},
                    }
                )
                return
            self._active_tasks[req_id] = cancel_event

        def worker() -> None:
            try:
                try:
                    call = decode_tool_call(params["call"])
                    context = decode_context(params["context"])
                except Exception as dec_err:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "invalid_params", "message": f"Invalid params: {type(dec_err).__name__}"},
                        }
                    )
                    return

                # Context check
                if self._workspace is None or context.workspace.resolve() != self._workspace.resolve():
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "context_mismatch", "message": "Context workspace mismatch"},
                        }
                    )
                    return

                if self._revision_id is None or context.revision_id != self._revision_id:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "context_mismatch", "message": "Context revision mismatch"},
                        }
                    )
                    return

                tool_entry = self._tools.get(call.tool_name)
                if tool_entry is None:
                    res = ToolFailure(
                        tool_name=call.tool_name,
                        error=f"Unknown tool: {call.tool_name}",
                        output=TextOutput(text=f"Unknown tool: {call.tool_name}"),
                        execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                    )
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": encode_tool_result(res),
                        }
                    )
                    return

                tool_instance, tool_decl, _ = tool_entry

                # Schema validation of tool arguments
                if tool_decl.input_schema:
                    try:
                        args_wire = json_wire_object(call.arguments)
                        schema_wire = json_wire_object(tool_decl.input_schema)
                        jsonschema.validate(instance=args_wire, schema=schema_wire)
                    except jsonschema.ValidationError as val_err:
                        res = ToolFailure(
                            tool_name=call.tool_name,
                            error=f"Input validation error: {val_err.message}",
                            output=TextOutput(text=f"Validation error: {val_err.message}"),
                            execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                        )
                        self._send_response(
                            {
                                "protocol_version": 1,
                                "id": req_id,
                                "ok": True,
                                "result": encode_tool_result(res),
                            }
                        )
                        return

                # Grants check
                required_caps = frozenset(tool_decl.capabilities)
                if not required_caps.issubset(context.capabilities):
                    missing = required_caps - context.capabilities
                    res = ToolFailure(
                        tool_name=call.tool_name,
                        error=f"Missing capability grants: {set(missing)}",
                        output=TextOutput(text=f"Denied: missing capability grants {set(missing)}"),
                        execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                    )
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": encode_tool_result(res),
                        }
                    )
                    return

                # Swap in the live abort signal without dropping any captured
                # field (logical_turn, capabilities, and future additions).
                abort_signal = HostAbortSignal(cancel_event)

                lease_token = str(params.get("lease_token", ""))
                inv_context = replace(
                    context,
                    abort_signal=abort_signal,
                    emit_tool_progress=lambda payload: self._send_progress(lease_token, payload),
                )
                services = ServerPluginServices(server=self, lease_token=lease_token, cancel_event=cancel_event)
                tool_threw = False
                try:
                    result = tool_instance.invoke(call, context=inv_context, services=services)
                except Exception as exc:
                    tool_threw = True
                    result = ToolFailure(
                        tool_name=call.tool_name,
                        error=f"Tool invocation failed with {type(exc).__name__}",
                        output=TextOutput(text=f"Tool invocation failed with {type(exc).__name__}"),
                        execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                    )
                if cancel_event.is_set() and isinstance(result, ToolFailure) and result.execution is None:
                    result = ToolFailure(
                        tool_name=result.tool_name,
                        error=result.error,
                        output=result.output,
                        body=result.body,
                        diagnostics=result.diagnostics,
                        execution=ConfirmedStop(cancellation_signalled=True),
                        timeout_seconds=result.timeout_seconds,
                    )

                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": {
                            "tool_result": encode_tool_result(result),
                            "threw": tool_threw,
                        },
                    }
                )
            except Exception as unexp_err:
                _report_internal_error(f"{req_id} handler", unexp_err)
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Internal error: {type(unexp_err).__name__}"},
                    }
                )
            finally:
                with self._tasks_lock:
                    self._active_tasks.pop(req_id, None)

        self._executor.submit(worker)

    def _handle_propose_turn(self, req_id: str, params: dict[str, Any]) -> None:
        cancel_event = threading.Event()
        with self._tasks_lock:
            if req_id in self._active_tasks:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "duplicate_id", "message": f"Request id '{req_id}' is already active"},
                    }
                )
                return
            self._active_tasks[req_id] = cancel_event

        def worker() -> None:
            try:
                try:
                    request = decode_provider_request(params["request"])
                    provider_name = str(params.get("provider", ""))
                except Exception as dec_err:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "invalid_params", "message": f"Invalid params: {type(dec_err).__name__}"},
                        }
                    )
                    return

                prov_entry = self._providers.get(provider_name)
                if prov_entry is None:
                    err = ProviderExecutionError(
                        kind="not_configured",
                        provider_name=provider_name,
                        model_name=request.model_name or "unknown",
                        message=f"Provider '{provider_name}' not configured",
                    )
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "kind": "provider_error",
                                "message": err.message,
                                "provider_error": encode_provider_error(err),
                            },
                        }
                    )
                    return

                provider_instance, _, _ = prov_entry
                abort_signal = HostAbortSignal(cancel_event)
                prov_req = replace(request, abort_signal=abort_signal)

                try:
                    res = provider_instance.propose_turn(prov_req)
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": encode_provider_result(res),
                        }
                    )
                except ProviderExecutionError as p_err:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "kind": "provider_error",
                                "message": p_err.message,
                                "provider_error": encode_provider_error(p_err),
                            },
                        }
                    )
                except Exception as exc:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "kind": "provider_error",
                                "message": f"Provider turn failed with {type(exc).__name__}",
                            },
                        }
                    )
            except Exception as unexp_err:
                _report_internal_error(f"{req_id} handler", unexp_err)
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Internal error: {type(unexp_err).__name__}"},
                    }
                )
            finally:
                with self._tasks_lock:
                    self._active_tasks.pop(req_id, None)

        self._executor.submit(worker)

    def _handle_invoke_command(self, req_id: str, params: dict[str, Any]) -> None:
        cancel_event = threading.Event()
        with self._tasks_lock:
            if req_id in self._active_tasks:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "duplicate_id", "message": f"Request id '{req_id}' is already active"},
                    }
                )
                return
            self._active_tasks[req_id] = cancel_event

        def worker() -> None:
            try:
                try:
                    name = str(params["name"])
                    args = own_json_object(params["arguments"])
                    context = decode_context(params["context"])
                    client_caps = frozenset(str(c) for c in params.get("client_capabilities", ()))
                except Exception as dec_err:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "invalid_params", "message": f"Invalid params: {type(dec_err).__name__}"},
                        }
                    )
                    return

                # Context check
                if self._workspace is None or context.workspace.resolve() != self._workspace.resolve():
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "context_mismatch", "message": "Context workspace mismatch"},
                        }
                    )
                    return

                if self._revision_id is None or context.revision_id != self._revision_id:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "context_mismatch", "message": "Context revision mismatch"},
                        }
                    )
                    return

                cmd_entry = self._commands.get(name)
                if cmd_entry is None:
                    res = CommandResult(status="error", error=f"Unknown command: {name}")
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": {
                                "command_result": encode_command_result(res),
                                "staged_state": [],
                                "staged_events": [],
                                "staged_continuations": [],
                            },
                        }
                    )
                    return

                cmd_instance, cmd_decl, plugin_id = cmd_entry

                # Schema validation of command arguments
                if cmd_decl.argument_schema:
                    try:
                        args_wire = json_wire_object(args)
                        schema_wire = json_wire_object(cmd_decl.argument_schema)
                        jsonschema.validate(instance=args_wire, schema=schema_wire)
                    except jsonschema.ValidationError as val_err:
                        res = CommandResult(status="error", error=f"Input validation error: {val_err.message}")
                        self._send_response(
                            {
                                "protocol_version": 1,
                                "id": req_id,
                                "ok": True,
                                "result": {
                                    "command_result": encode_command_result(res),
                                    "staged_state": [],
                                    "staged_events": [],
                                    "staged_continuations": [],
                                },
                            }
                        )
                        return

                # Capabilities check
                required_caps = frozenset(cmd_decl.capabilities)
                if not required_caps.issubset(context.capabilities):
                    missing = required_caps - context.capabilities
                    res = CommandResult(status="error", error=f"Missing capability grants: {set(missing)}")
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": {
                                "command_result": encode_command_result(res),
                                "staged_state": [],
                                "staged_events": [],
                                "staged_continuations": [],
                            },
                        }
                    )
                    return

                # Client capabilities check
                req_client_caps = frozenset(cmd_decl.client_capabilities)
                if not req_client_caps.issubset(client_caps):
                    missing_client = req_client_caps - client_caps
                    res = CommandResult(status="error", error=f"Client lacks required capabilities: {set(missing_client)}")
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": {
                                "command_result": encode_command_result(res),
                                "staged_state": [],
                                "staged_events": [],
                                "staged_continuations": [],
                            },
                        }
                    )
                    return

                lease_token = str(params["lease_token"])
                services = ServerPluginServices(server=self, lease_token=lease_token)

                invocation = CommandInvocation(
                    context=context,
                    name=name,
                    arguments=args,
                    client_capabilities=client_caps,
                )

                cmd_threw = False
                try:
                    result = cmd_instance.invoke(invocation, services)
                except Exception as exc:
                    cmd_threw = True
                    result = CommandResult(status="error", error=f"Command invocation failed with {type(exc).__name__}")

                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": {
                            "command_result": encode_command_result(result),
                            "threw": cmd_threw,
                        },
                    }
                )
            except Exception as unexp_err:
                _report_internal_error(f"{req_id} handler", unexp_err)
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Internal error: {type(unexp_err).__name__}"},
                    }
                )
            finally:
                with self._tasks_lock:
                    self._active_tasks.pop(req_id, None)

        self._executor.submit(worker)

    def _handle_render_view(self, req_id: str, params: dict[str, Any]) -> None:
        def worker() -> None:
            try:
                view_name = str(params["name"])
                session_id = str(params["session_id"])
                lease_token = str(params["lease_token"])
                client_caps = frozenset(str(c) for c in params.get("client_capabilities", ()))

                view_entry = self._views.get(view_name)
                if view_entry is None:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "unknown_view", "message": f"Unknown view: {view_name}"},
                        }
                    )
                    return

                view_instance, plugin_id = view_entry
                services = ServerPluginServices(server=self, lease_token=lease_token)
                try:
                    desc = view_instance.render(session_id, services, client_capabilities=client_caps)
                except Exception as exc:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "render_failed", "message": f"View render failed with {type(exc).__name__}"},
                        }
                    )
                    return

                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": encode_view_descriptor(desc),
                    }
                )
            except Exception as unexp_err:
                _report_internal_error(f"{req_id} handler", unexp_err)
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Internal error: {type(unexp_err).__name__}"},
                    }
                )

        self._executor.submit(worker)

    def _handle_run_lifecycle(self, req_id: str, params: dict[str, Any]) -> None:
        def worker() -> None:
            try:
                owner = _required_text(params, "owner")
                kind = _required_text(params, "kind")
                lease_token = _required_text(params, "lease_token")
                if self._workspace is None or self._revision_id is None:
                    raise RuntimeError("Plugin host is not initialized")
                entry = self._lifecycle_hooks.get(f"{owner}\0{kind}")
                if entry is None:
                    self._send_response({"protocol_version": 1, "id": req_id, "ok": True, "result": {"note": None}})
                    return
                callback, _pid = entry
                lifecycle = RunLifecycle(
                    phase="start" if kind == "session_start" else "end",
                    session_id=_required_text(params, "session_id"),
                    run_id=_required_text(params, "run_id"),
                    logical_turn=_optional_text(params, "logical_turn"),
                    status=cast("Literal['completed', 'failed', 'interrupted', '']", _optional_text(params, "status")),
                    error=(params.get("error") if isinstance(params.get("error"), str) and params.get("error") else None),
                )
                context = ToolContext(
                    workspace=self._workspace,
                    session_id=lifecycle.session_id,
                    run_id=lifecycle.run_id,
                    invocation_id=_required_text(params, "invocation_id"),
                    revision_id=self._revision_id,
                    actor="plugin",
                    logical_turn=lifecycle.logical_turn,
                )
                services = ServerPluginServices(server=self, lease_token=lease_token)
                note = callback(lifecycle, context, services)
                if note is not None and not isinstance(note, str):
                    raise ValueError(f"{kind} hook of {owner} must return str or None")
                self._send_response({"protocol_version": 1, "id": req_id, "ok": True, "result": {"note": note}})
            except Exception as exc:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "hook_failed", "message": f"Lifecycle hook failed: {type(exc).__name__}: {exc}"},
                    }
                )

        self._executor.submit(worker)

    def _handle_pre_tool(self, req_id: str, params: dict[str, Any]) -> None:
        def worker() -> None:
            try:
                owner = _required_text(params, "owner")
                lease_token = _required_text(params, "lease_token")
                if self._workspace is None or self._revision_id is None:
                    raise RuntimeError("Plugin host is not initialized")
                intent = ToolIntent(
                    tool_name=_required_text(params, "tool_name"),
                    tool_call_id=_optional_text(params, "tool_call_id") or None,
                    arguments=json_wire_object(params.get("arguments") or {}),
                )
                context = ToolContext(
                    workspace=self._workspace,
                    session_id=_required_text(params, "session_id"),
                    run_id=_optional_text(params, "run_id"),
                    invocation_id=_required_text(params, "invocation_id"),
                    revision_id=self._revision_id,
                    actor="plugin",
                    logical_turn=_optional_text(params, "logical_turn"),
                )
                services = ServerPluginServices(server=self, lease_token=lease_token)
                for callback, _, _, plugin_id in self._pre_tool_hooks:
                    if plugin_id != owner:
                        continue
                    reason = callback(intent, context, services)
                    if reason is not None:
                        if not isinstance(reason, str) or not reason.strip():
                            raise ValueError(f"pre_tool gate of {plugin_id} must return a non-empty reason or None")
                        self._send_response({"protocol_version": 1, "id": req_id, "ok": True, "result": {"reason": reason}})
                        return
                self._send_response({"protocol_version": 1, "id": req_id, "ok": True, "result": {"reason": None}})
            except Exception as exc:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "hook_failed", "message": f"Gate failed: {type(exc).__name__}: {exc}"},
                    }
                )

        self._executor.submit(worker)

    def _handle_post_tool(self, req_id: str, params: dict[str, Any]) -> None:
        def worker() -> None:
            try:
                owner = _required_text(params, "owner")
                lease_token = _required_text(params, "lease_token")
                if self._workspace is None or self._revision_id is None:
                    raise RuntimeError("Plugin host is not initialized")
                outcome = ToolOutcome(
                    tool_name=_required_text(params, "tool_name"),
                    tool_call_id=_optional_text(params, "tool_call_id") or None,
                    status=cast("Literal['ok', 'error', 'cancelled']", _required_text(params, "status")),
                    text=_optional_text(params, "text"),
                    arguments=json_wire_object(params.get("arguments") or {}),
                )
                context = ToolContext(
                    workspace=self._workspace,
                    session_id=_required_text(params, "session_id"),
                    run_id=_optional_text(params, "run_id"),
                    invocation_id=_required_text(params, "invocation_id"),
                    revision_id=self._revision_id,
                    actor="plugin",
                    logical_turn=_optional_text(params, "logical_turn"),
                )
                services = ServerPluginServices(server=self, lease_token=lease_token)
                note: str | None = None
                for callback, _, _, plugin_id in self._post_tool_hooks:
                    if plugin_id != owner:
                        continue
                    returned = callback(outcome, context, services)
                    if returned is not None:
                        if not isinstance(returned, str):
                            raise ValueError(f"post_tool hook of {plugin_id} must return str or None")
                        note = returned
                self._send_response({"protocol_version": 1, "id": req_id, "ok": True, "result": {"note": note}})
            except Exception as exc:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "hook_failed", "message": f"Hook failed: {type(exc).__name__}: {exc}"},
                    }
                )

        self._executor.submit(worker)

    def _handle_contribute_context(self, req_id: str, params: dict[str, Any]) -> None:
        def worker() -> None:
            try:
                session_id = _required_text(params, "session_id")
                lease_token = _required_text(params, "lease_token")
                owner_target = params.get("owner")
                logical_turn = _optional_text(params, "logical_turn")
                if self._workspace is None or self._revision_id is None:
                    raise RuntimeError("Plugin host is not initialized")
                context = ToolContext(
                    workspace=self._workspace,
                    session_id=session_id,
                    run_id=_optional_text(params, "run_id"),
                    invocation_id=_required_text(params, "invocation_id"),
                    revision_id=self._revision_id,
                    actor="plugin",
                    logical_turn=logical_turn,
                )
                services = ServerPluginServices(server=self, lease_token=lease_token)
                contributions: list[dict[str, object]] = []

                if owner_target is not None:
                    cb = self._context_callbacks.get(str(owner_target))
                    targets = [(str(owner_target), cb)] if cb is not None else []
                else:
                    targets = list(self._context_callbacks.items())

                for plugin_id, cb in targets:
                    try:
                        c = cb(context, services)
                        if isinstance(c, ContextContribution):
                            contributions.append({"type": "text", "content": c.content, "owner": plugin_id, "role": c.role})
                    except Exception as exc:
                        self._send_response(
                            {
                                "protocol_version": 1,
                                "id": req_id,
                                "ok": False,
                                "error": {"kind": "context_failed", "message": f"Context provider {plugin_id} failed: {type(exc).__name__}: {exc}"},
                            }
                        )
                        return

                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": {"contributions": contributions},
                    }
                )
            except Exception as unexp_err:
                _report_internal_error(f"{req_id} handler", unexp_err)
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Internal error: {type(unexp_err).__name__}"},
                    }
                )

        self._executor.submit(worker)

    def _handle_notify_fact(self, req_id: str, params: dict[str, Any]) -> None:
        def worker() -> None:
            try:
                fact_type = _required_text(params, "fact_type")
                lease_token = _required_text(params, "lease_token")
                logical_turn = _optional_text(params, "logical_turn")
                fact_data = params["fact"]
                if not isinstance(fact_data, Mapping):
                    raise ValueError("'fact' parameter must be an object")
                if self._workspace is None or self._revision_id is None:
                    raise RuntimeError("Plugin host is not initialized")
                session_id = _required_text(fact_data, "session_id")
                run_id = _required_text(fact_data, "run_id")
                revision_id = _required_text(fact_data, "revision_id")
                context = ToolContext(
                    workspace=self._workspace,
                    session_id=session_id,
                    run_id=run_id,
                    invocation_id=_required_text(params, "invocation_id"),
                    revision_id=revision_id,
                    actor="plugin",
                    logical_turn=str(fact_data.get("logical_turn", logical_turn)),
                )
                fact: PluginFact
                if fact_type == "usage":
                    usage_payload = fact_data.get("usage")
                    usage_obj = None
                    if isinstance(usage_payload, Mapping):
                        usage_obj = ProviderTokenUsage(
                            input_tokens=usage_payload.get("input_tokens"),
                            output_tokens=usage_payload.get("output_tokens"),
                            cache_read_tokens=usage_payload.get("cache_read_tokens"),
                            cache_write_tokens=usage_payload.get("cache_write_tokens"),
                            uncached_input_tokens=usage_payload.get("uncached_input_tokens"),
                            cost_usd=_optional_number(usage_payload, "cost_usd"),
                        )
                    fact = UsageFact(
                        session_id=session_id,
                        run_id=run_id,
                        revision_id=revision_id,
                        logical_turn=context.logical_turn,
                        event_sequence=_required_int(fact_data, "event_sequence"),
                        dispatch_sequence=_required_int(fact_data, "dispatch_sequence"),
                        provider_name=_required_text(fact_data, "provider_name"),
                        model_name=_required_text(fact_data, "model_name"),
                        usage=usage_obj,
                        elapsed_seconds=_optional_number(fact_data, "elapsed_seconds"),
                        cost_usd=_optional_number(fact_data, "cost_usd"),
                    )
                elif fact_type == "invocation_finished":
                    fact = InvocationFinishedFact(
                        session_id=session_id,
                        run_id=run_id,
                        revision_id=revision_id,
                        logical_turn=context.logical_turn,
                        event_sequence=_required_int(fact_data, "event_sequence"),
                        tool_name=_required_text(fact_data, "tool_name"),
                        tool_call_id=fact_data.get("tool_call_id"),
                        status=fact_data["status"],
                        error=fact_data.get("error"),
                        owner=_optional_text(fact_data, "owner"),
                        effects=tuple(str(item) for item in fact_data.get("effects", ())),
                    )
                elif fact_type == "run_finished":
                    stop_cause_payload = fact_data.get("stop_cause")
                    if stop_cause_payload is not None and not isinstance(stop_cause_payload, str):
                        raise ValueError("'stop_cause' must be a string or null")
                    status_payload = _required_text(fact_data, "status")
                    if status_payload not in ("completed", "failed", "interrupted"):
                        raise ValueError(f"Unknown run status: {status_payload!r}")
                    fact = RunFinishedFact(
                        session_id=session_id,
                        run_id=run_id,
                        revision_id=revision_id,
                        logical_turn=context.logical_turn,
                        event_sequence=_required_int(fact_data, "event_sequence"),
                        status=status_payload,
                        stop_cause=cast(StopCause | None, stop_cause_payload),
                        elapsed_seconds=_optional_number(fact_data, "elapsed_seconds"),
                    )
                else:
                    raise ValueError(f"Unknown fact_type: {fact_type}")

                services = ServerPluginServices(server=self, lease_token=lease_token)
                owner_target = params.get("owner")
                observer_errors: list[dict[str, str]] = []
                for obs, observed_kinds, plugin_id in self._observers:
                    if owner_target is not None and plugin_id != owner_target:
                        continue
                    if fact_type in observed_kinds:
                        try:
                            obs(fact, context, services)
                        except Exception as exc:
                            observer_errors.append({"owner": plugin_id, "error": f"{type(exc).__name__}: {exc}"})
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": {"observed": True, "errors": observer_errors},
                    }
                )
            except Exception as exc:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Notify fact failed: {type(exc).__name__}"},
                    }
                )

        self._executor.submit(worker)

    def _handle_state_migrate(self, req_id: str, params: dict[str, Any]) -> None:
        """Run one owner's pure old->new state transform; no services, no lease, no side channel."""

        def worker() -> None:
            try:
                owner = _required_text(params, "owner")
                from_version = _required_int(params, "from_version")
                to_version = _required_int(params, "to_version")
                payload = params["payload"]
                migrator = self._state_migrators.get(owner)
                if migrator is None:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "state_migration_unsupported", "message": f"plugin {owner} declares no migrate_state transform"},
                        }
                    )
                    return
                migrated = migrator(payload, from_version, to_version)
                if not isinstance(migrated, Mapping):
                    raise ValueError("migrate_state must return a JSON object")
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": {"payload": json_wire_object(migrated)},
                    }
                )
            except ValueError as exc:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "state_migration_failed", "message": str(exc)},
                    }
                )
            except Exception as exc:
                _report_internal_error(f"{req_id} state_migrate", exc)
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "state_migration_failed", "message": f"state migration failed: {type(exc).__name__}"},
                    }
                )

        self._executor.submit(worker)

    def _handle_state_prepare_deactivate(self, req_id: str, params: dict[str, Any]) -> None:
        """Run one owner's pure deactivation transform in the host that still holds its schema."""

        def worker() -> None:
            try:
                owner = _required_text(params, "owner")
                scope = _required_text(params, "scope")
                scope_key = _required_text(params, "scope_key")
                tip = params.get("tip_sequence")
                if scope not in ("session", "workspace"):
                    raise ValueError("scope must be 'session' or 'workspace'")
                if scope == "session" and (isinstance(tip, bool) or not isinstance(tip, int)):
                    raise ValueError("session-scope deactivation requires the owning session's event tip as an integer")
                if scope == "workspace" and tip is not None:
                    raise ValueError("workspace-scope deactivation has no single session event tip")
                payload = params["payload"]
                transform = self._state_deactivators.get(owner)
                if transform is None:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "kind": "state_deactivation_unsupported",
                                "message": f"plugin {owner} declares no prepare_deactivate transform",
                            },
                        }
                    )
                    return
                prepared = transform(payload, scope, scope_key, tip)
                if not isinstance(prepared, Mapping):
                    raise ValueError("prepare_deactivate must return a JSON object")
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": {"payload": json_wire_object(prepared)},
                    }
                )
            except ValueError as exc:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "state_deactivation_failed", "message": str(exc)},
                    }
                )
            except Exception as exc:
                _report_internal_error(f"{req_id} state_prepare_deactivate", exc)
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "state_deactivation_failed", "message": f"state deactivation failed: {type(exc).__name__}"},
                    }
                )

        self._executor.submit(worker)

    def _cleanup(self) -> None:
        self._shutdown_event.set()
        # 1. Signal cancellation to all running tasks
        with self._tasks_lock:
            events = list(self._active_tasks.values())
        for ev in events:
            ev.set()

        # 2. Every child process this host started dies with the generation that started it.
        self.processes.reap_all()

        # 3. Call plugin.close() with bounded timeout per plugin
        for plugin in self._plugins:
            if plugin.close is not None and callable(plugin.close):
                th = threading.Thread(target=plugin.close, daemon=True)
                th.start()
                th.join(timeout=0.5)

        # 4. Give running workers brief cooperative grace
        self._executor.shutdown(wait=False, cancel_futures=True)
        time.sleep(0.2)

        # 5. If standalone server process, exit hard so stubborn callbacks cannot hang process
        if self._standalone:
            os._exit(0)


def run_server(in_stream: BinaryIO | None = None, out_stream: BinaryIO | None = None) -> int:
    """Entry point configuring reserved stdout protocol stream and running server."""
    if out_stream is None:
        # Reserve real stdout for protocol by duplicating fd 1, and redirect sys.stdout to sys.stderr
        real_stdout_fd = os.dup(sys.stdout.fileno())
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        protocol_out = os.fdopen(real_stdout_fd, "wb", buffering=0)
        sys.stdout = sys.stderr
    else:
        protocol_out = out_stream

    protocol_in = sys.stdin.buffer if in_stream is None else in_stream
    server = PluginHostServer(in_stream=protocol_in, out_stream=protocol_out, standalone=True)
    return server.run()
