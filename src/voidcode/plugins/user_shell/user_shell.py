"""Bounded presentation and stable error vocabulary for the governed ``!`` command."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Final

from voidcode.sdk.json_values import json_wire_object

DEFAULT_TIMEOUT_SECONDS: Final[int] = 600
MAX_TIMEOUT_SECONDS: Final[int] = 600
#: The command result carries only a bounded preview; full captured streams live in artifacts.
PREVIEW_BYTES: Final[int] = 16 * 1024
#: A command is audit metadata, not a result body: keep one event row bounded even with escaping.
MAX_EVENT_COMMAND_BYTES: Final[int] = 4 * 1024
#: Conservative pre-commit ceiling for this plugin's event and result payloads (core bound is 4 MiB).
MAX_SHELL_EVENT_BYTES: Final[int] = 256 * 1024
CAPTURE_TRUNCATION_MARKER: Final[str] = "\n[process output exceeded the 128 MiB per-stream capture limit]"


class ShellErrorCode(StrEnum):
    """Stable command error codes; human prose never depends on an exception type."""

    EMPTY_COMMAND = "empty_command"
    INVALID_TIMEOUT = "invalid_timeout"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    START_FAILED = "start_failed"
    CAPTURE_FAILED = "capture_failed"
    SANDBOX_FAILED = "sandbox_failed"
    PROCESS_FAILED = "process_failed"
    ARTIFACT_FAILED = "artifact_failed"
    EVENT_TOO_LARGE = "event_too_large"
    NONZERO_EXIT = "nonzero_exit"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


_ERROR_MESSAGES: Final[dict[ShellErrorCode, str]] = {
    ShellErrorCode.EMPTY_COMMAND: "shell command must not be empty",
    ShellErrorCode.INVALID_TIMEOUT: f"shell timeout must be an integer from 1 to {MAX_TIMEOUT_SECONDS} seconds",
    ShellErrorCode.WORKSPACE_UNAVAILABLE: "shell command requires a workspace",
    ShellErrorCode.START_FAILED: "shell command could not start",
    ShellErrorCode.CAPTURE_FAILED: "shell command failed while capturing its output",
    ShellErrorCode.SANDBOX_FAILED: "shell command sandbox failed",
    ShellErrorCode.PROCESS_FAILED: "shell command failed before producing a result",
    ShellErrorCode.ARTIFACT_FAILED: "shell command finished but its output artifact could not be stored",
    ShellErrorCode.EVENT_TOO_LARGE: "shell command finished but its receipt exceeded the event bound",
    ShellErrorCode.NONZERO_EXIT: "shell command exited nonzero",
    ShellErrorCode.TIMED_OUT: "shell command timed out",
    ShellErrorCode.CANCELLED: "shell command was cancelled",
}

_ERROR_GUIDANCE: Final[dict[ShellErrorCode, str]] = {
    ShellErrorCode.EMPTY_COMMAND: "Enter a non-empty command after `!`.",
    ShellErrorCode.INVALID_TIMEOUT: f"Use an integer timeout from 1 to {MAX_TIMEOUT_SECONDS} seconds.",
    ShellErrorCode.WORKSPACE_UNAVAILABLE: "Open a workspace-backed session before running a shell command.",
    ShellErrorCode.START_FAILED: "Check the command and workspace permissions, then retry.",
    ShellErrorCode.CAPTURE_FAILED: "The command may have run; inspect the workspace before retrying.",
    ShellErrorCode.SANDBOX_FAILED: "The command may have run; inspect the workspace before retrying.",
    ShellErrorCode.PROCESS_FAILED: "The command may have run; inspect the workspace before retrying.",
    ShellErrorCode.ARTIFACT_FAILED: "The command ran; the bounded preview is still shown. Inspect the workspace before retrying.",
    ShellErrorCode.EVENT_TOO_LARGE: "The command ran; its receipt could not be stored. Inspect the workspace before retrying.",
    ShellErrorCode.NONZERO_EXIT: "The full captured stdout and stderr are in the referenced artifacts.",
    ShellErrorCode.TIMED_OUT: "The full captured prefix is in the referenced artifacts.",
    ShellErrorCode.CANCELLED: "Only the bounded partial preview is retained for a cancelled command.",
}


def error_message(code: ShellErrorCode) -> str:
    return _ERROR_MESSAGES[code]


def error_guidance(code: ShellErrorCode) -> str:
    return _ERROR_GUIDANCE[code]


def clip_text(text: str, limit: int = PREVIEW_BYTES) -> tuple[str, bool]:
    """Return at most ``limit`` UTF-8 bytes without splitting a code point."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def bounded_command(command: str, limit: int = MAX_EVENT_COMMAND_BYTES) -> tuple[str, bool]:
    """Bound command metadata independently from the command the shell actually ran."""
    return clip_text(command, limit)


def event_payload_size(payload: dict[str, object]) -> int:
    """Canonical-ish size of one event/result payload, matching the storage accounting intent."""
    return len(json.dumps(json_wire_object(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def bounded_event_payload(payload: dict[str, object]) -> dict[str, object]:
    """Drop optional prose before an event can approach the core's committed-event bound."""
    if event_payload_size(payload) <= MAX_SHELL_EVENT_BYTES:
        return payload
    compact = dict(payload)
    compact["command"] = ""
    compact["command_truncated"] = True
    compact.pop("detail", None)
    if event_payload_size(compact) <= MAX_SHELL_EVENT_BYTES:
        return compact
    fallback: dict[str, object] = {
        "command": "",
        "command_truncated": True,
        "exit_code": payload.get("exit_code"),
        "status": payload.get("status"),
        "cancelled": payload.get("cancelled", False),
        "timed_out": payload.get("timed_out", False),
        "capture_truncated": payload.get("capture_truncated", False),
        "preview_truncated": payload.get("preview_truncated", False),
        "stdout_artifact": payload.get("stdout_artifact"),
        "stderr_artifact": payload.get("stderr_artifact"),
        "error": payload.get("error", ShellErrorCode.EVENT_TOO_LARGE.value),
    }
    if event_payload_size(fallback) > MAX_SHELL_EVENT_BYTES:
        raise ValueError("shell event fallback exceeds the plugin event bound")
    return fallback


def preview_text(stdout: str, stderr: str) -> tuple[str, bool]:
    """One bounded stdout+stderr preview; full streams are recoverable from artifacts."""
    return clip_text(stdout + stderr, PREVIEW_BYTES)


__all__ = [
    "CAPTURE_TRUNCATION_MARKER",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_EVENT_COMMAND_BYTES",
    "MAX_SHELL_EVENT_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "PREVIEW_BYTES",
    "ShellErrorCode",
    "bounded_command",
    "bounded_event_payload",
    "clip_text",
    "error_guidance",
    "error_message",
    "event_payload_size",
    "preview_text",
]
