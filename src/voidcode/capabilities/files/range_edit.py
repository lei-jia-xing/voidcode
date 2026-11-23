"""Range-edit preflight and commit logic for the ``edit`` tool.

The public surface is one ``edit`` tool: exact-text replacement and LSP-style
range edits share its ``expectedHash`` evidence and one commit path. Keeping
range logic here (not in a ``Tool`` class) prevents a second mutation engine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from voidcode.capabilities.workspace_queries import DOCUMENT_STATES, DocumentState, DocumentStateRequest
from voidcode.sdk.atomic_file import replace_file
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.services import PluginServices, QueryContractError, QueryUnavailable
from voidcode.sdk.tools import (
    ConfirmedStop,
    OutputBounds,
    TextOutput,
    ToolDiagnostics,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from ..tool_errors import raise_tool_diagnostic
from ._common import (
    PathEscapesWorkspaceError,
    encode_line_endings,
    make_file_change,
    normalize_line_endings,
    resolve_workspace_path,
    summarize_diff,
)
from .mutation import (
    TextFileState,
    check_expected_hash,
    invalid_path_failure,
    load_text_state,
    outside_workspace_failure,
    uniform_line_ending,
)

_TOOL_NAME = "edit"


class _RangeTextEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, pattern=r"\S")
    startLine: int = Field(ge=1)
    startCharacter: int = Field(ge=1)
    endLine: int = Field(ge=1)
    endCharacter: int = Field(ge=1)
    newText: str
    expectedHash: str = Field(description="SHA-256 content hash of the target file from read or read(hash_only=true).")
    version: int | None = Field(default=None, description="LSP document version; must match the active document-state provider when supplied.")


class _RangeEditArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    mode: Literal["range"]
    positionEncoding: Literal["utf-8", "utf-16", "utf-32"] | None = Field(
        default=None, description="LSP position encoding; defaults to the document's negotiated encoding or UTF-16."
    )
    edits: list[_RangeTextEdit] = Field(
        min_length=1, description="Multi-file text edits with 1-based line/character ranges, each checked against expectedHash."
    )


@dataclass(frozen=True, slots=True)
class RangeEditResultBody:
    paths: tuple[str, ...]
    files: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(own_json_object(item) for item in self.files))

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"paths": list(self.paths)}
        if self.files:
            payload["files"] = [json_wire_object(item) for item in self.files]
        return payload


def _diagnostic_failure(
    *,
    message: str,
    error_kind: str,
    reason: str,
    retry_guidance: str,
    details: dict[str, object] | None = None,
) -> ToolFailure:
    """Build a model-visible failure using the shared SDK diagnostics slot."""
    payload: dict[str, object] = {"reason": reason}
    if details:
        payload.update(details)
    return ToolFailure(
        tool_name=_TOOL_NAME,
        error=message,
        output=TextOutput(text=message),
        diagnostics=ToolDiagnostics(kind=error_kind, summary=message, details=payload, guidance=retry_guidance),
    )


def _query_document_state(services: PluginServices, path: Path) -> DocumentState | ToolFailure | None:
    """Ask any document-state provider; absence permits only hash-based guards."""
    try:
        return DOCUMENT_STATES.ask(services, DocumentStateRequest(path=path.as_posix()))
    except QueryUnavailable:
        return None
    except QueryContractError:
        return _diagnostic_failure(
            message=f"edit got an unreadable document version answer for {path}: the provider broke its shape contract.",
            error_kind="tool_input_mismatch",
            reason="document_answer_shape_violation",
            retry_guidance="The document-state provider violated its SDK contract; fix the provider before retrying.",
            details={"path": path.as_posix()},
        )


def _cancelled() -> ToolFailure:
    return ToolFailure(
        tool_name=_TOOL_NAME,
        error="edit range cancelled before committing files",
        execution=ConfirmedStop(cancellation_signalled=True),
    )


def _is_cancelled(context: ToolContext) -> bool:
    return context.abort_signal is not None and context.abort_signal.cancelled


def apply_range_edits(
    args: _RangeEditArgs,
    *,
    context: ToolContext,
    services: PluginServices,
) -> ToolResult:
    """Validate every path/range, then commit each changed file atomically."""
    workspace = context.require_workspace()

    # Resolve and existence-check every path before reading or writing anything.
    requested: list[tuple[_RangeTextEdit, Path, str]] = []
    for edit in args.edits:
        if _is_cancelled(context):
            return _cancelled()
        try:
            path, display_path = resolve_workspace_path(workspace, edit.path, allow_outside_workspace=False)
        except PathEscapesWorkspaceError:
            return outside_workspace_failure(_TOOL_NAME, edit.path)
        except (OSError, ValueError) as exc:
            return invalid_path_failure(_TOOL_NAME, edit.path, detail=str(exc))
        if not path.is_file():
            raise_tool_diagnostic(
                message=f"edit range target does not exist: {edit.path}",
                error_kind="tool_input_mismatch",
                reason="target_not_found",
                retry_guidance="Retry with an existing workspace file path.",
                details={"path": edit.path},
            )
        requested.append((edit, path, display_path))

    states: dict[Path, TextFileState] = {}
    for _, path, display_path in requested:
        if path not in states:
            states[path] = load_text_state(path, display_path, tool_name=_TOOL_NAME)

    for edit, path, _ in requested:
        check_expected_hash(states[path], edit.expectedHash, tool_name=_TOOL_NAME)

    endings: dict[Path, str | None] = {}
    lf_texts: dict[Path, str] = {}
    line_starts: dict[Path, list[int]] = {}
    for path, state in states.items():
        endings[path] = uniform_line_ending(state.text, display_path=state.display_path, tool_name=_TOOL_NAME)
        lf_texts[path] = normalize_line_endings(state.text)
        lines = lf_texts[path].split("\n")
        starts = [0]
        for line in lines[:-1]:
            starts.append(starts[-1] + len(line) + 1)
        line_starts[path] = starts

    document_states: dict[Path, DocumentState | None] = {}
    grouped: dict[Path, list[tuple[int, int, str]]] = {}
    display_by_path = {path: state.display_path for path, state in states.items()}
    hashes = {path: hashlib.sha256(state.raw).hexdigest() for path, state in states.items()}
    for edit, path, _ in requested:
        actual_hash = hashes[path]
        if path not in document_states:
            answer = _query_document_state(services, path)
            if isinstance(answer, ToolFailure):
                return answer
            document_states[path] = answer
        document_state = document_states[path]
        requested_version = edit.version
        if requested_version is not None and document_state is None:
            return _diagnostic_failure(
                message=(
                    f"edit requested version {requested_version} for {edit.path}, but no document-state provider has a version for it (fail-closed)."
                ),
                error_kind="stale_edit",
                reason="document_version_unavailable",
                retry_guidance=(
                    "Obtain a current document version from an enabled document-state provider before retrying this version-constrained edit."
                ),
                details={"path": edit.path, "expected_version": requested_version},
            )
        if document_state is not None and document_state.content_hash != actual_hash:
            return _diagnostic_failure(
                message=f"edit rejected because the provider's document content changed for {edit.path}.",
                error_kind="stale_edit",
                reason="document_content_mismatch",
                retry_guidance="Refresh the document-state result and read the current file before retrying the range edit.",
                details={"path": edit.path, "document_hash": document_state.content_hash, "actual_hash": actual_hash},
            )
        if requested_version is not None and document_state is not None and requested_version != document_state.version:
            return _diagnostic_failure(
                message=(
                    f"edit rejected because {edit.path} is at document version {document_state.version}, not requested version {requested_version}."
                ),
                error_kind="stale_edit",
                reason="document_version_mismatch",
                retry_guidance="Refresh the document-state result and retry the complete range edit against the current document version.",
                details={"path": edit.path, "expected_version": requested_version, "actual_version": document_state.version},
            )

        # An explicit positionEncoding always wins: it names the coordinate system the caller's
        # ranges are expressed in. The negotiated encoding only supplies the default.
        encoding = args.positionEncoding or (document_state.position_encoding if document_state is not None else "utf-16")

        lines = lf_texts[path].split("\n")
        if edit.startLine > len(lines):
            raise_tool_diagnostic(
                message=f"edit range startLine {edit.startLine} is out of bounds in {edit.path}",
                error_kind="tool_input_mismatch",
                reason="start_line_out_of_range",
                retry_guidance="Retry with a 1-based start line within the file.",
                details={"path": edit.path, "start_line": edit.startLine, "line_count": len(lines)},
            )
        if edit.endLine > len(lines):
            raise_tool_diagnostic(
                message=f"edit range endLine {edit.endLine} is out of bounds in {edit.path}",
                error_kind="tool_input_mismatch",
                reason="end_line_out_of_range",
                retry_guidance="Retry with a 1-based end line within the file.",
                details={"path": edit.path, "end_line": edit.endLine, "line_count": len(lines)},
            )
        if edit.endLine < edit.startLine:
            raise_tool_diagnostic(
                message=f"edit range endLine {edit.endLine} precedes startLine {edit.startLine} in {edit.path}",
                error_kind="tool_input_mismatch",
                reason="range_reversed",
                retry_guidance="Retry with endLine at or after startLine.",
                details={"path": edit.path, "start_line": edit.startLine, "end_line": edit.endLine},
            )

        starts = line_starts[path]
        start = starts[edit.startLine - 1] + _position_to_offset(
            lines[edit.startLine - 1],
            edit.startCharacter,
            encoding=encoding,
            file_path=edit.path,
            line_number=edit.startLine,
        )
        end = starts[edit.endLine - 1] + _position_to_offset(
            lines[edit.endLine - 1],
            edit.endCharacter,
            encoding=encoding,
            file_path=edit.path,
            line_number=edit.endLine,
        )
        if start < 0 or end < start or end > len(lf_texts[path]):
            raise_tool_diagnostic(
                message=f"edit range is out of bounds: {edit.path}",
                error_kind="tool_input_mismatch",
                reason="range_out_of_bounds",
                retry_guidance="Retry with a range inside the current file.",
                details={"path": edit.path, "start_line": edit.startLine, "end_line": edit.endLine},
            )
        grouped.setdefault(path, []).append((start, end, normalize_line_endings(edit.newText)))

    staged: dict[Path, str] = {}
    for path, edits in grouped.items():
        sorted_edits = sorted(edits, key=lambda item: (item[0], item[1]))
        for i in range(len(sorted_edits) - 1):
            current_start, current_end, _ = sorted_edits[i]
            next_start, next_end, _ = sorted_edits[i + 1]
            if current_end > next_start or (current_start == next_start and current_end == next_end):
                raise_tool_diagnostic(
                    message=f"edit range contains overlapping edits: {display_by_path[path]}",
                    error_kind="tool_input_mismatch",
                    reason="overlapping_edits",
                    retry_guidance="Retry with non-overlapping ranges.",
                    details={"path": display_by_path[path]},
                )

        content = lf_texts[path]
        for start, end, new_text in reversed(sorted_edits):
            content = content[:start] + new_text + content[end:]
        staged[path] = content

    prepared: list[tuple[Path, bytes, str, dict[str, object]]] = []
    for path, final_lf in staged.items():
        state = states[path]
        final_text = encode_line_endings(final_lf, endings[path])
        final_bytes = final_text.encode("utf-8")
        display_path = display_by_path[path]
        prepared.append(
            (
                path,
                final_bytes,
                display_path,
                {
                    "path": display_path,
                    "byte_count": len(final_bytes),
                    "content_hash": hashlib.sha256(final_bytes).hexdigest(),
                    "diff": summarize_diff(path=display_path, before=state.text, after=final_text)[0],
                },
            )
        )

    applied_paths: list[str] = []
    applied_files: list[dict[str, object]] = []
    error: str | None = None
    cancelled = False
    for path, final_bytes, display_path, file_result in prepared:
        if _is_cancelled(context):
            cancelled = True
            error = "edit range cancelled before committing the next file"
            break
        try:
            replace_file(path, final_bytes, expected_content=states[path].raw)
        except OSError as exc:
            error = f"edit range failed during write on {display_path}: {exc}"
            break
        applied_paths.append(display_path)
        applied_files.append(file_result)

    if error is not None:
        applied_set = set(applied_paths)
        remaining = [display for _, _, display, _ in prepared if display not in applied_set]
        applied_labels = [f"{item['path']} SHA-256 {item['content_hash']}" for item in applied_files]
        message = f"{error}. Applied prefix: {applied_labels}; uncommitted remaining: {remaining}"
        return ToolFailure(
            tool_name=_TOOL_NAME,
            error=message,
            output=TextOutput(message, bounds=OutputBounds(partial=True)),
            body=RangeEditResultBody(paths=tuple(applied_paths), files=tuple(applied_files)),
            changes=tuple(make_file_change(path) for path in applied_paths),
            execution=ConfirmedStop(cancellation_signalled=cancelled),
        )

    applied_files_by_path = {str(item["path"]): item for item in applied_files}
    ordered_paths = tuple(sorted(applied_files_by_path))
    content = [f"Applied {len(args.edits)} range edit(s) across {len(ordered_paths)} file(s)."]
    content.extend(f"SHA-256 content hash ({path}): {applied_files_by_path[path]['content_hash']}" for path in ordered_paths)
    return ToolSuccess(
        tool_name=_TOOL_NAME,
        output=TextOutput("\n".join(content)),
        changes=tuple(make_file_change(path) for path in ordered_paths),
        body=RangeEditResultBody(
            paths=ordered_paths,
            files=tuple(applied_files_by_path[path] for path in ordered_paths),
        ),
    )


def _position_to_offset(
    line_text: str,
    character_1based: int,
    *,
    encoding: str,
    file_path: str,
    line_number: int,
) -> int:
    col = character_1based - 1
    if col < 0:
        raise ValueError(f"edit range character must be >= 1 at {file_path}:{line_number}")

    if encoding == "utf-16":
        curr_u16 = 0
        for char_idx, ch in enumerate(line_text):
            units = 2 if ord(ch) > 0xFFFF else 1
            if curr_u16 == col:
                return char_idx
            if curr_u16 < col < curr_u16 + units:
                raise_tool_diagnostic(
                    message=f"edit range character lands mid-surrogate at {file_path}:{line_number}:{character_1based}",
                    error_kind="tool_input_mismatch",
                    reason="character_mid_surrogate",
                    retry_guidance="Retry with a UTF-16 character boundary.",
                    details={"path": file_path, "line": line_number, "character": character_1based},
                )
            curr_u16 += units
        if col == curr_u16:
            return len(line_text)
        raise_tool_diagnostic(
            message=f"edit range character {character_1based} is out of bounds for line {line_number} in {file_path} (max {curr_u16 + 1})",
            error_kind="tool_input_mismatch",
            reason="character_out_of_range",
            retry_guidance="Retry with a character within the line.",
            details={"path": file_path, "line": line_number, "character": character_1based},
        )

    elif encoding == "utf-8":
        curr_u8 = 0
        for char_idx, ch in enumerate(line_text):
            ch_bytes = len(ch.encode("utf-8"))
            if curr_u8 == col:
                return char_idx
            if curr_u8 < col < curr_u8 + ch_bytes:
                raise_tool_diagnostic(
                    message=f"edit range character lands inside multi-byte UTF-8 sequence at {file_path}:{line_number}:{character_1based}",
                    error_kind="tool_input_mismatch",
                    reason="character_mid_utf8",
                    retry_guidance="Retry with a UTF-8 character boundary.",
                    details={"path": file_path, "line": line_number, "character": character_1based},
                )
            curr_u8 += ch_bytes
        if col == curr_u8:
            return len(line_text)
        raise_tool_diagnostic(
            message=f"edit range character {character_1based} is out of bounds for line {line_number} in {file_path} (max {curr_u8 + 1})",
            error_kind="tool_input_mismatch",
            reason="character_out_of_range",
            retry_guidance="Retry with a character within the line.",
            details={"path": file_path, "line": line_number, "character": character_1based},
        )

    elif encoding == "utf-32":
        if col > len(line_text):
            raise_tool_diagnostic(
                message=f"edit range character {character_1based} is out of bounds for line {line_number} in {file_path} (max {len(line_text) + 1})",
                error_kind="tool_input_mismatch",
                reason="character_out_of_range",
                retry_guidance="Retry with a character within the line.",
                details={"path": file_path, "line": line_number, "character": character_1based},
            )
        return col

    raise ValueError(f"Unsupported positionEncoding: {encoding}")


__all__ = ["RangeEditResultBody", "_RangeEditArgs", "apply_range_edits"]
