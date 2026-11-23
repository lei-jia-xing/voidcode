"""Shared mutation primitives for the file edit tools.

One implementation of the contract both edit modes rely on: read raw bytes,
verify the caller's SHA-256 evidence, decode strict UTF-8, refuse mixed line
endings, and encode committed bytes with the file's uniform ending. Atomic
replacement itself stays in the SDK's ``replace_file``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from voidcode.sdk.tools import TextOutput, ToolDiagnostics, ToolFailure

from ._common import line_ending_kinds
from .errors import raise_tool_diagnostic


@dataclass(frozen=True, slots=True)
class TextFileState:
    """One text target loaded before any mutation is committed."""

    path: Path
    display_path: str
    raw: bytes
    text: str


def outside_workspace_failure(tool_name: str, raw_path: str) -> ToolFailure:
    """A typed, model-visible refusal for a mutation resolving outside the workspace."""
    message = f"{tool_name} may only modify paths inside the workspace: {raw_path}"
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(message),
        diagnostics=ToolDiagnostics(
            kind="tool_input_mismatch",
            summary=message,
            details={"reason": "outside_workspace", "path": raw_path},
            guidance="Retry with a workspace-relative path; read, glob, and grep may still access external files.",
        ),
    )


def invalid_path_failure(tool_name: str, raw_path: str, *, detail: str) -> ToolFailure:
    """A typed refusal for a path the filesystem cannot resolve (for example an embedded NUL)."""
    message = f"{tool_name} could not resolve the target path {raw_path!r}: {detail}"
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(message),
        diagnostics=ToolDiagnostics(
            kind="tool_input_mismatch",
            summary=message,
            details={"reason": "invalid_path", "path": raw_path},
            guidance="Retry with a valid workspace-relative path.",
        ),
    )


def load_text_state(path: Path, display_path: str, *, tool_name: str) -> TextFileState:
    """Read raw bytes and strict-decode a text target; nothing is written."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise_tool_diagnostic(
            message=f"{tool_name} only supports UTF-8 text files",
            error_kind="tool_input_mismatch",
            reason="unsupported_encoding",
            retry_guidance="Retry with a UTF-8 text file, or use a binary-aware tool.",
            details={"path": display_path},
        )
    return TextFileState(path=path, display_path=display_path, raw=raw, text=text)


def check_expected_hash(state: TextFileState, expected_hash: str, *, tool_name: str) -> None:
    """Reject an edit whose caller evidence no longer matches the raw file bytes."""
    actual_hash = hashlib.sha256(state.raw).hexdigest()
    if expected_hash == actual_hash:
        return
    raise_tool_diagnostic(
        message=f"{tool_name} rejected because the file changed since it was read (stale edit).",
        error_kind="stale_edit",
        reason="content_hash_mismatch",
        retry_guidance=(
            "Read the file again (or call read with hash_only=true), use the SHA-256 content hash shown in read output, then retry the edit."
        ),
        details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": state.display_path},
    )


def uniform_line_ending(text: str, *, display_path: str, tool_name: str) -> str | None:
    """The file's one line ending, or ``None`` when it has no line ending.

    Mixed endings refuse the edit: silently normalizing the untouched bytes
    would change content the caller never named.
    """
    kinds = line_ending_kinds(text)
    if len(kinds) > 1:
        raise_tool_diagnostic(
            message=(f"{tool_name} rejected because {display_path} mixes line endings; normalize the file to LF, CRLF, or lone CR before editing."),
            error_kind="tool_input_mismatch",
            reason="mixed_line_endings",
            retry_guidance="Normalize the file to one line ending and retry; the edit preserves bytes it does not change.",
            details={"path": display_path, "line_endings": sorted(kinds)},
        )
    return next(iter(kinds), None)


__all__ = [
    "TextFileState",
    "check_expected_hash",
    "invalid_path_failure",
    "load_text_state",
    "outside_workspace_failure",
    "uniform_line_ending",
]
