"""User argv hook execution: the gate/execute/record loop for the four live surfaces.

The ``{action, diagnostic, guidance}`` stdout JSON protocol, fail-closed
``pre_tool`` versus fail-open everything else, the read-only policy skip, the
32-item diagnostic cap with sentinel, and the exact error/status strings.

Every command is trusted host-side code: it inherits the core environment,
runs in a new process session, gets ``stdin`` from ``/dev/null``, and reads its
bounded tool context from ``VOIDCODE_HOOK_PAYLOAD_JSON``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import IO, Literal, Protocol

from .hook_config import MAX_TIMEOUT_SECONDS, RuntimeHooksConfig, hook_tool_matches

logger = logging.getLogger(__name__)

#: Lifecycle surfaces never honor ``cancel``: session start/end are advisory
#: (diagnostic + guidance kept, loop continues). Only the tool gate/post loop
#: short-circuits on first cancel.

#: Diagnostic accumulation bound. The first 32 items are kept, then one
#: sentinel; newer items are dropped.
_MAX_DIAGNOSTICS = 32
_DIAGNOSTICS_OMITTED_SENTINEL = "[additional diagnostics omitted]"

#: Hard aggregate bound for one surface's whole command list. Per-command
#: timeouts are clamped to what remains of it.
MAX_SURFACE_TIMEOUT_SECONDS = 300.0
#: Per-stream capture cap (stdout and stderr independently).
MAX_CAPTURE_BYTES = 256 * 1024
#: stderr text included in one failure message. The capture cap bounds memory;
#: this bounds what can reach a tool failure or result note.
MAX_FAILURE_STDERR_CHARACTERS = 2_000
#: Tool-hook payload bounds. Arguments keep their JSON shape only up to these
#: limits; results are a bounded prefix, never arbitrary tool output.
MAX_ARGUMENT_STRING_CHARACTERS = 1_000
MAX_ARGUMENTS_JSON_CHARACTERS = 8_192
MAX_ARGUMENT_ITEMS = 32
MAX_ARGUMENT_DEPTH = 4
MAX_RESULT_CHARACTERS = 4_000
MAX_PAYLOAD_JSON_CHARACTERS = 16_384
_OUTPUT_TRUNCATION_SUFFIX = "\n[output truncated]"
_TEXT_TRUNCATION_SUFFIX = "[truncated]"
_ABORT_POLL_SECONDS = 0.05
_READER_JOIN_SECONDS = 5.0


class HookAbortSignal(Protocol):
    """Cancellation handle a caller may expose to a hook command."""

    @property
    def cancelled(self) -> bool: ...


def _append_diagnostic(diagnostics: list[str], diagnostic: str) -> None:
    if len(diagnostics) < _MAX_DIAGNOSTICS:
        diagnostics.append(diagnostic)
    elif len(diagnostics) == _MAX_DIAGNOSTICS:
        diagnostics.append(_DIAGNOSTICS_OMITTED_SENTINEL)


@dataclass(frozen=True, slots=True)
class HookExecutionEvent:
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class HookExecutionOutcome:
    events: tuple[HookExecutionEvent, ...]
    failed_error: str | None = None
    action: Literal["continue", "cancel"] | None = "continue"
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HookExecutionPolicy:
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class HookPolicyDecision:
    allowed: bool
    outcome: Literal["allowed", "denied", "skipped"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class HookExecutionRequest:
    hooks: RuntimeHooksConfig
    workspace: Path
    session_id: str
    tool_name: str
    phase: Literal["pre", "post"]
    recursion_env_var: str
    environment: Mapping[str, str]
    policy: HookExecutionPolicy = field(default_factory=HookExecutionPolicy)
    #: Tool-call identity and arguments for the bounded hook payload.
    tool_call_id: str | None = None
    arguments: Mapping[str, object] = field(default_factory=dict)
    #: Post-tool only: the settled status and the bounded result text prefix.
    tool_status: Literal["ok", "error", "cancelled"] | None = None
    tool_text: str | None = None
    #: Optional caller cancellation handle; the core does not currently expose one
    #: to hook leases, so this is ``None`` on the production path.
    abort_signal: HookAbortSignal | None = None


@dataclass(frozen=True, slots=True)
class LifecycleHookExecutionRequest:
    hooks: RuntimeHooksConfig
    workspace: Path
    session_id: str
    surface: Literal["session_start", "session_end"]
    recursion_env_var: str
    environment: Mapping[str, str]
    payload: Mapping[str, object] = field(default_factory=dict)
    policy: HookExecutionPolicy = field(default_factory=HookExecutionPolicy)
    abort_signal: HookAbortSignal | None = None


@dataclass(frozen=True, slots=True)
class _HookCommandLoop:
    """Per-surface adapter for the shared hook command gate/execute/record loop."""

    base_payload: Mapping[str, object]
    status_key: str
    environment: Mapping[str, str]
    surface: str
    session_id: str
    payload_json: str
    fail_closed: bool
    error_label: str
    honors_cancel: bool
    label: str
    abort_signal: HookAbortSignal | None = None


def run_tool_hooks(request: HookExecutionRequest) -> HookExecutionOutcome:
    hooks = request.hooks
    if hooks.enabled is not True:
        return HookExecutionOutcome(events=())
    if request.environment.get(request.recursion_env_var) == "1":
        return HookExecutionOutcome(events=())

    commands = hooks.pre_tool if request.phase == "pre" else hooks.post_tool
    timeout_seconds = hooks.timeout_seconds
    match_patterns = hooks.pre_tool_match if request.phase == "pre" else hooks.post_tool_match
    if not hook_tool_matches(match_patterns, request.tool_name):
        return HookExecutionOutcome(events=())
    return _run_hook_commands(
        _HookCommandLoop(
            base_payload={"phase": request.phase, "tool_name": request.tool_name, "session_id": request.session_id},
            status_key="status",
            environment=request.environment,
            surface=f"{request.phase}_tool",
            session_id=request.session_id,
            payload_json=_tool_payload_json(request),
            fail_closed=request.phase == "pre",
            error_label=f"tool {request.phase}-hook failed for {request.tool_name}",
            honors_cancel=True,
            label=request.phase,
            abort_signal=request.abort_signal,
        ),
        commands=commands,
        timeout_seconds=timeout_seconds,
        workspace=request.workspace,
        recursion_env_var=request.recursion_env_var,
        policy=request.policy,
    )


def run_lifecycle_hooks(request: LifecycleHookExecutionRequest) -> HookExecutionOutcome:
    hooks = request.hooks
    if hooks.enabled is not True:
        return HookExecutionOutcome(events=())
    if request.environment.get(request.recursion_env_var) == "1":
        return HookExecutionOutcome(events=())

    commands = hooks.on_session_start if request.surface == "session_start" else hooks.on_session_end
    timeout_seconds = hooks.timeout_seconds
    return _run_hook_commands(
        _HookCommandLoop(
            base_payload={"surface": request.surface, "session_id": request.session_id, **dict(request.payload)},
            status_key="hook_status",
            environment=request.environment,
            surface=request.surface,
            session_id=request.session_id,
            # Lifecycle payloads are exact and already core-bounded; keep them unchanged.
            payload_json=json.dumps(dict(request.payload), sort_keys=True),
            fail_closed=False,
            error_label=f"lifecycle hook failed for {request.surface}",
            honors_cancel=False,
            label=request.surface,
            abort_signal=request.abort_signal,
        ),
        commands=commands,
        timeout_seconds=timeout_seconds,
        workspace=request.workspace,
        recursion_env_var=request.recursion_env_var,
        policy=request.policy,
    )


def _tool_payload_json(request: HookExecutionRequest) -> str:
    """Encode the bounded tool context handed to a hook command.

    Arguments are capped per string, per container, and in total JSON size.
    A post-tool result is only ever a bounded prefix. If the whole document
    still exceeds its bound, the arguments are replaced by an omission marker
    and, in the last resort, the result too. The env value always stays valid
    JSON.
    """
    arguments, arguments_truncated = _bounded_json_value(dict(request.arguments))
    if len(json.dumps(arguments, sort_keys=True, ensure_ascii=False)) > MAX_ARGUMENTS_JSON_CHARACTERS:
        arguments = {"_omitted": f"arguments exceeded {MAX_ARGUMENTS_JSON_CHARACTERS} characters"}
        arguments_truncated = True

    payload: dict[str, object] = {
        "phase": request.phase,
        "tool_name": request.tool_name,
        "session_id": request.session_id,
        "tool_call_id": request.tool_call_id,
        "arguments": arguments,
        "arguments_truncated": arguments_truncated,
    }
    if request.phase == "post":
        text = request.tool_text or ""
        result_truncated = len(text) > MAX_RESULT_CHARACTERS
        payload["status"] = request.tool_status or "ok"
        payload["result"] = text[:MAX_RESULT_CHARACTERS] + (_TEXT_TRUNCATION_SUFFIX if result_truncated else "")
        payload["result_truncated"] = result_truncated
    return _bounded_payload_json(payload)


def _bounded_payload_json(payload: dict[str, object]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if len(text) <= MAX_PAYLOAD_JSON_CHARACTERS:
        return text
    payload = {
        **payload,
        "arguments": {"_omitted": "arguments exceeded the hook payload bound"},
        "arguments_truncated": True,
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if len(text) <= MAX_PAYLOAD_JSON_CHARACTERS:
        return text
    return json.dumps(
        {
            "phase": payload["phase"],
            "tool_name": _bounded_metadata_text(payload["tool_name"]),
            "session_id": _bounded_metadata_text(payload["session_id"]),
            "tool_call_id": _bounded_metadata_text(payload["tool_call_id"]),
            "status": payload.get("status"),
            "arguments": {"_omitted": "arguments exceeded the hook payload bound"},
            "arguments_truncated": True,
            "result": _TEXT_TRUNCATION_SUFFIX,
            "result_truncated": True,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _bounded_metadata_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > MAX_ARGUMENT_STRING_CHARACTERS:
        return text[:MAX_ARGUMENT_STRING_CHARACTERS] + _TEXT_TRUNCATION_SUFFIX
    return text


def _bounded_json_value(value: object, *, depth: int = 0) -> tuple[object, bool]:
    """Keep a JSON value's shape but cap string length, container size and depth."""
    if isinstance(value, str):
        if len(value) > MAX_ARGUMENT_STRING_CHARACTERS:
            return value[:MAX_ARGUMENT_STRING_CHARACTERS] + _TEXT_TRUNCATION_SUFFIX, True
        return value, False
    if isinstance(value, Mapping):
        if depth >= MAX_ARGUMENT_DEPTH:
            return _TEXT_TRUNCATION_SUFFIX, True
        items = list(value.items())
        bounded: dict[str, object] = {}
        truncated = len(items) > MAX_ARGUMENT_ITEMS
        for key, item in items[:MAX_ARGUMENT_ITEMS]:
            item_value, item_truncated = _bounded_json_value(item, depth=depth + 1)
            bounded[str(key)] = item_value
            truncated = truncated or item_truncated
        return bounded, truncated
    if isinstance(value, (list, tuple)):
        if depth >= MAX_ARGUMENT_DEPTH:
            return _TEXT_TRUNCATION_SUFFIX, True
        truncated = len(value) > MAX_ARGUMENT_ITEMS
        bounded_items: list[object] = []
        for item in value[:MAX_ARGUMENT_ITEMS]:
            item_value, item_truncated = _bounded_json_value(item, depth=depth + 1)
            bounded_items.append(item_value)
            truncated = truncated or item_truncated
        return bounded_items, truncated
    return value, False


def _run_hook_commands(
    adapter: _HookCommandLoop,
    *,
    commands: tuple[tuple[str, ...], ...],
    timeout_seconds: float,
    workspace: Path,
    recursion_env_var: str,
    policy: HookExecutionPolicy,
) -> HookExecutionOutcome:
    """Gate, execute and record one surface's hook commands."""
    events: list[HookExecutionEvent] = []
    diagnostics: list[str] = []
    surface_deadline = time.monotonic() + MAX_SURFACE_TIMEOUT_SECONDS
    for command in commands:
        policy_decision = _hook_policy_decision(policy)
        if not policy_decision.allowed:
            events.append(
                HookExecutionEvent(
                    payload={
                        **adapter.base_payload,
                        adapter.status_key: policy_decision.outcome,
                        "hook_policy": _hook_policy_payload(policy=policy, decision=policy_decision),
                    },
                )
            )
            if policy_decision.outcome == "skipped":
                continue
            return HookExecutionOutcome(
                events=tuple(events),
                failed_error=policy_decision.reason,
                diagnostics=tuple(diagnostics),
            )
        remaining = surface_deadline - time.monotonic()
        if remaining <= 0:
            return _hook_failure(
                adapter,
                events=events,
                diagnostics=diagnostics,
                error_text=f"{adapter.error_label}: hook surface timed out after {MAX_SURFACE_TIMEOUT_SECONDS:g} seconds",
            )
        configured_timeout = timeout_seconds if isfinite(timeout_seconds) and timeout_seconds > 0 else MAX_TIMEOUT_SECONDS
        command_timeout = min(configured_timeout, MAX_TIMEOUT_SECONDS, remaining)
        aggregate_bound = remaining <= min(configured_timeout, MAX_TIMEOUT_SECONDS)
        try:
            command_result = _run_hook_command(
                command=command,
                workspace=workspace,
                environment={
                    **adapter.environment,
                    "VOIDCODE_HOOK_SURFACE": adapter.surface,
                    "VOIDCODE_SESSION_ID": adapter.session_id,
                    "VOIDCODE_HOOK_PAYLOAD_JSON": adapter.payload_json,
                    recursion_env_var: "1",
                },
                timeout_seconds=command_timeout,
                abort_signal=adapter.abort_signal,
            )
        except OSError as exc:
            return _hook_failure(
                adapter,
                events=events,
                diagnostics=diagnostics,
                error_text=f"{adapter.error_label}: {exc}",
            )
        except subprocess.TimeoutExpired as exc:
            if aggregate_bound:
                error_text = f"{adapter.error_label}: hook surface timed out after {MAX_SURFACE_TIMEOUT_SECONDS:g} seconds"
                error_text = _attach_stderr(error_text, exc.stderr)
            else:
                error_text = _attach_stderr(f"{adapter.error_label}: {exc}", exc.stderr)
            return _hook_failure(
                adapter,
                events=events,
                diagnostics=diagnostics,
                error_text=error_text,
            )
        except _HookCommandAborted:
            return _hook_failure(
                adapter,
                events=events,
                diagnostics=diagnostics,
                error_text=f"{adapter.error_label}: hook command cancelled",
            )

        if command_result.returncode != 0:
            return _hook_failure(
                adapter,
                events=events,
                diagnostics=diagnostics,
                error_text=_attach_stderr(
                    f"{adapter.error_label}: {subprocess.CalledProcessError(command_result.returncode, command)}",
                    command_result.stderr,
                ),
            )
        try:
            action_payload = _hook_action_payload_from_stdout(command_result.stdout)
        except ValueError as exc:
            return _hook_failure(
                adapter,
                events=events,
                diagnostics=diagnostics,
                error_text=_attach_stderr(f"{adapter.error_label}: {exc}", command_result.stderr),
            )

        action = action_payload.action
        diagnostic = action_payload.diagnostic
        guidance = action_payload.guidance
        if diagnostic is not None:
            _append_diagnostic(diagnostics, diagnostic)
        if action == "cancel" and not adapter.honors_cancel:
            logger.debug("ignoring cancel from advisory surface %s", adapter.label)
            action = "continue"
        events.append(
            HookExecutionEvent(
                payload={
                    **adapter.base_payload,
                    adapter.status_key: "ok",
                    "hook_policy": _hook_policy_payload(policy=policy, decision=policy_decision),
                    **({"action": action} if action != "continue" else {}),
                    **({"diagnostic": diagnostic} if diagnostic is not None else {}),
                    **({"guidance": guidance} if guidance is not None else {}),
                },
            )
        )
        if action == "cancel":
            return HookExecutionOutcome(
                events=tuple(events),
                action=action,
                diagnostics=tuple(diagnostics),
            )

    return HookExecutionOutcome(
        events=tuple(events),
        diagnostics=tuple(diagnostics),
    )


def _hook_failure(
    adapter: _HookCommandLoop,
    *,
    events: list[HookExecutionEvent],
    diagnostics: list[str],
    error_text: str,
) -> HookExecutionOutcome:
    """Record a bounded command failure; fail closed only on the pre-tool gate."""
    _append_diagnostic(diagnostics, error_text)
    payload: dict[str, object] = {
        **adapter.base_payload,
        adapter.status_key: "error",
        "error": error_text,
        "diagnostic": error_text,
    }
    if adapter.fail_closed:
        payload["guidance"] = error_text
    return HookExecutionOutcome(
        events=tuple((*events, HookExecutionEvent(payload=payload))),
        failed_error=error_text,
        action="cancel" if adapter.fail_closed else None,
        diagnostics=tuple(diagnostics),
    )


def _attach_stderr(message: str, stderr: str | bytes | None) -> str:
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    text = text.strip()
    if not text:
        return message
    if len(text) > MAX_FAILURE_STDERR_CHARACTERS:
        text = text[:MAX_FAILURE_STDERR_CHARACTERS] + _TEXT_TRUNCATION_SUFFIX
    return f"{message}\nstderr:\n{text}"


@dataclass(frozen=True, slots=True)
class _HookActionPayload:
    action: Literal["continue", "cancel"] = "continue"
    diagnostic: str | None = None
    guidance: str | None = None


def _hook_action_payload_from_stdout(stdout: str) -> _HookActionPayload:
    text = stdout.strip()
    if not text:
        return _HookActionPayload()
    try:
        raw_payload = json.loads(text)
    except json.JSONDecodeError as exc:
        if text.startswith(("{", "[")):
            raise ValueError("hook command returned malformed JSON") from exc
        return _HookActionPayload()
    if not isinstance(raw_payload, dict):
        raise ValueError("hook JSON response must be an object")

    action = raw_payload.get("action", "continue")
    if action not in ("continue", "cancel"):
        raise ValueError("hook JSON action must be 'continue' or 'cancel'")
    diagnostic = raw_payload.get("diagnostic")
    guidance = raw_payload.get("guidance")
    if diagnostic is not None and not isinstance(diagnostic, str):
        raise ValueError("hook JSON diagnostic must be a string")
    if guidance is not None and not isinstance(guidance, str):
        raise ValueError("hook JSON guidance must be a string")
    return _HookActionPayload(
        action=action,
        diagnostic=diagnostic or None,
        guidance=guidance or None,
    )


def _hook_policy_decision(policy: HookExecutionPolicy) -> HookPolicyDecision:
    if policy.read_only:
        return HookPolicyDecision(
            allowed=False,
            outcome="skipped",
            reason="read-only runtime policy skips executable hook commands",
        )
    return HookPolicyDecision(allowed=True, outcome="allowed")


def _hook_policy_payload(
    *,
    policy: HookExecutionPolicy,
    decision: HookPolicyDecision,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "outcome": decision.outcome,
        "read_only": policy.read_only,
    }
    if decision.reason is not None:
        payload["reason"] = decision.reason
    return payload


class _HookCommandAborted(RuntimeError):
    """The caller's abort signal fired while a hook command was running."""


@dataclass(frozen=True, slots=True)
class _HookCommandResult:
    stdout: str
    stderr: str
    returncode: int


def _run_hook_command(
    *,
    command: tuple[str, ...],
    workspace: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    abort_signal: HookAbortSignal | None,
) -> _HookCommandResult:
    """Run one argv command in its own process session, capturing bounded output.

    ``stdin`` is never inherited; a hook that has no input protocol reads EOF.
    On timeout or cancellation the whole process group is killed, so a
    command's descendants cannot outlive it.
    """
    process = subprocess.Popen(
        list(command),
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **environment},
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    captured: dict[str, tuple[str, bool]] = {}
    readers = (
        threading.Thread(target=_capture_stream, args=("stdout", process.stdout, captured), daemon=True),
        threading.Thread(target=_capture_stream, args=("stderr", process.stderr, captured), daemon=True),
    )
    for reader in readers:
        reader.start()
    try:
        _wait_for_process(process, timeout_seconds, abort_signal)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        _join_readers(readers)
        stdout, _stdout_truncated = captured.get("stdout", ("", False))
        stderr, _stderr_truncated = captured.get("stderr", ("", False))
        raise subprocess.TimeoutExpired(process.args, timeout_seconds, output=stdout, stderr=stderr) from None
    except _HookCommandAborted:
        _kill_process_group(process)
        _join_readers(readers)
        raise
    _join_readers(readers)
    stdout, _stdout_truncated = captured.get("stdout", ("", False))
    stderr, _stderr_truncated = captured.get("stderr", ("", False))
    return _HookCommandResult(stdout=stdout, stderr=stderr, returncode=process.returncode)


def _wait_for_process(process: subprocess.Popen[bytes], timeout_seconds: float, abort_signal: HookAbortSignal | None) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if abort_signal is not None and abort_signal.cancelled:
            raise _HookCommandAborted
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        try:
            process.wait(timeout=min(remaining, _ABORT_POLL_SECONDS))
        except subprocess.TimeoutExpired:
            continue
        return


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=_READER_JOIN_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _join_readers(readers: tuple[threading.Thread, ...]) -> None:
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)


def _capture_stream(name: str, stream: IO[bytes], captured: dict[str, tuple[str, bool]]) -> None:
    buffer = bytearray()
    truncated = False
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            break
        room = MAX_CAPTURE_BYTES - len(buffer)
        if room > 0:
            buffer.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        else:
            truncated = True
    text = bytes(buffer).decode("utf-8", errors="replace")
    if truncated:
        text += _OUTPUT_TRUNCATION_SUFFIX
    captured[name] = (text, truncated)


__all__ = [
    "HookAbortSignal",
    "HookExecutionEvent",
    "HookExecutionOutcome",
    "HookExecutionPolicy",
    "HookExecutionRequest",
    "HookPolicyDecision",
    "LifecycleHookExecutionRequest",
    "MAX_CAPTURE_BYTES",
    "MAX_SURFACE_TIMEOUT_SECONDS",
    "run_lifecycle_hooks",
    "run_tool_hooks",
]
