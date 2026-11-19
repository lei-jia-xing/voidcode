from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from voidcode.capabilities.workspace_queries import DOCUMENT_STATES, DocumentState, DocumentStateRequest
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.services import PluginServices, QueryContractError, QueryUnavailable
from voidcode.sdk.tools import (
    ConfirmedStop,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from ._common import make_file_change, resolve_workspace_path, summarize_diff
from ._replace import replace_file


class _TextEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str
    startLine: int = Field(ge=1)
    startCharacter: int = Field(ge=1)
    endLine: int = Field(ge=1)
    endCharacter: int = Field(ge=1)
    newText: str
    expectedHash: str = Field(description="SHA-256 content hash of the target file from read.")
    version: int | None = Field(default=None, description="LSP document version; must match the active document-state provider when supplied.")


class _WorkspaceEditArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    positionEncoding: Literal["utf-8", "utf-16", "utf-32"] | None = Field(
        default=None, description="LSP position encoding; defaults to the document's negotiated encoding or UTF-16."
    )
    edits: list[_TextEdit] = Field(min_length=1, description="Text edits with 1-based line/character ranges, each checked against expectedHash.")


@dataclass(frozen=True, slots=True)
class ApplyWorkspaceEditResultBody:
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
    tool_name: str,
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
        tool_name=tool_name,
        error=message,
        output=TextOutput(text=message),
        diagnostics=ToolDiagnostics(kind=error_kind, summary=message, details=payload, guidance=retry_guidance),
    )


def _query_document_state(services: PluginServices, path: Path, *, tool_name: str) -> DocumentState | ToolFailure | None:
    """Ask any document-state provider; absence permits only hash-based guards."""
    try:
        return DOCUMENT_STATES.ask(services, DocumentStateRequest(path=path.as_posix()))
    except QueryUnavailable:
        return None
    except QueryContractError:
        return _diagnostic_failure(
            tool_name,
            message=f"unreadable document version answer for {path}: the provider broke its shape contract.",
            error_kind="tool_input_mismatch",
            reason="document_answer_shape_violation",
            retry_guidance="The document-state provider violated its SDK contract; fix the provider before retrying.",
            details={"path": path.as_posix()},
        )


class ApplyWorkspaceEditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="apply_workspace_edit",
        icon=Icon(glyph="✎"),
        description="Apply validated LSP text edits with document-version checks; runtime approval governs filesystem access.",
        input_schema=_WorkspaceEditArgs.model_json_schema(),
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("edits[].path",),
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        workspace = context.require_workspace()
        args = _WorkspaceEditArgs.model_validate(json_wire_object(call.arguments))
        requested_encoding = args.positionEncoding

        raw_contents: dict[Path, bytes] = {}
        originals: dict[Path, str] = {}
        document_states: dict[Path, DocumentState | None] = {}
        grouped: dict[Path, list[tuple[int, int, str]]] = {}
        display_by_path: dict[Path, str] = {}
        for edit in args.edits:
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return ToolFailure(
                    tool_name=self.definition.name,
                    error="apply_workspace_edit cancelled before committing files",
                    execution=ConfirmedStop(cancellation_signalled=True),
                )
            path, display_path = resolve_workspace_path(workspace, edit.path)
            if not path.is_file():
                raise ValueError(f"apply_workspace_edit target does not exist: {edit.path}")
            if path not in raw_contents:
                raw_content = path.read_bytes()
                try:
                    originals[path] = raw_content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("apply_workspace_edit only supports UTF-8 text files") from exc
                raw_contents[path] = raw_content
            current = originals[path]
            actual_hash = hashlib.sha256(raw_contents[path]).hexdigest()
            if actual_hash != edit.expectedHash:
                return _diagnostic_failure(
                    self.definition.name,
                    message=f"apply_workspace_edit rejected because {edit.path} changed since it was read (stale edit).",
                    error_kind="stale_edit",
                    reason="content_hash_mismatch",
                    retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry apply_workspace_edit.",
                    details={"path": edit.path, "expected_hash": edit.expectedHash, "actual_hash": actual_hash},
                )

            if path not in document_states:
                answer = _query_document_state(services, path, tool_name=self.definition.name)
                if isinstance(answer, ToolFailure):
                    return answer
                document_states[path] = answer
            document_state: DocumentState | None = document_states[path]
            requested_version = edit.version
            if requested_version is not None and document_state is None:
                return _diagnostic_failure(
                    self.definition.name,
                    message=(
                        f"apply_workspace_edit requested version {requested_version} for {edit.path}, "
                        "but no document-state provider has a version for it (fail-closed)."
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
                    self.definition.name,
                    message=f"apply_workspace_edit rejected because the provider's document content changed for {edit.path}.",
                    error_kind="stale_edit",
                    reason="document_content_mismatch",
                    retry_guidance="Refresh the document-state result and read the current file before retrying the workspace edit.",
                    details={
                        "path": edit.path,
                        "document_hash": document_state.content_hash,
                        "actual_hash": actual_hash,
                    },
                )
            if requested_version is not None and document_state is not None and requested_version != document_state.version:
                return _diagnostic_failure(
                    self.definition.name,
                    message=(
                        f"apply_workspace_edit rejected because {edit.path} is at document version "
                        f"{document_state.version}, not requested version {requested_version}."
                    ),
                    error_kind="stale_edit",
                    reason="document_version_mismatch",
                    retry_guidance="Refresh the document-state result and retry the complete workspace edit against the current document version.",
                    details={
                        "path": edit.path,
                        "expected_version": requested_version,
                        "actual_version": document_state.version,
                    },
                )

            # An explicit positionEncoding always wins: it names the coordinate system the caller's
            # ranges are expressed in, and UTF-8 coordinates can validly edit a file whose LSP
            # session negotiated UTF-16 (the version/hash guards are independent of coordinates).
            # The negotiated encoding only supplies the default when the caller omits it.
            encoding = requested_encoding or (document_state.position_encoding if document_state is not None else "utf-16")

            # LSP lines end only at CR, LF or CRLF; Unicode separators are text.
            lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", current)
            if edit.startLine > len(lines):
                raise ValueError(f"apply_workspace_edit startLine {edit.startLine} is out of bounds in {edit.path}")
            if edit.endLine > len(lines):
                raise ValueError(f"apply_workspace_edit endLine {edit.endLine} is out of bounds in {edit.path}")
            if edit.endLine < edit.startLine:
                raise ValueError(f"apply_workspace_edit endLine {edit.endLine} precedes startLine {edit.startLine} in {edit.path}")

            line_start_offset = sum(len(lines[i]) for i in range(edit.startLine - 1))
            line_end_offset = sum(len(lines[i]) for i in range(edit.endLine - 1))
            start_char_offset = _position_to_offset(
                lines[edit.startLine - 1].rstrip("\r\n"),
                edit.startCharacter,
                encoding=encoding,
                file_path=edit.path,
                line_number=edit.startLine,
            )
            end_char_offset = _position_to_offset(
                lines[edit.endLine - 1].rstrip("\r\n"),
                edit.endCharacter,
                encoding=encoding,
                file_path=edit.path,
                line_number=edit.endLine,
            )

            start = line_start_offset + start_char_offset
            end = line_end_offset + end_char_offset
            if start < 0 or end < start or end > len(current):
                raise ValueError(f"apply_workspace_edit range is out of bounds: {edit.path}")

            grouped.setdefault(path, []).append((start, end, edit.newText))
            display_by_path[path] = display_path

        staged: dict[Path, str] = {}
        for path, edits in grouped.items():
            sorted_edits = sorted(edits, key=lambda item: (item[0], item[1]))
            for i in range(len(sorted_edits) - 1):
                curr_s, curr_e, _ = sorted_edits[i]
                next_s, next_e, _ = sorted_edits[i + 1]
                if curr_e > next_s or (curr_s == next_s and curr_e == next_e):
                    raise ValueError(f"apply_workspace_edit contains overlapping edits: {display_by_path[path]}")

            content = originals[path]
            for start, end, new_text in reversed(sorted_edits):
                content = content[:start] + new_text + content[end:]
            staged[path] = content

        file_results: list[Mapping[str, object]] = []
        for path, final_content in staged.items():
            final_bytes = final_content.encode("utf-8")
            display_path = display_by_path[path]
            file_result: dict[str, object] = {
                "path": display_path,
                "byte_count": len(final_bytes),
                "content_hash": hashlib.sha256(final_bytes).hexdigest(),
                "diff": summarize_diff(path=Path(display_path), before=originals[path], after=final_content)[0],
            }
            file_results.append(file_result)

        applied_paths: list[str] = []
        error: str | None = None
        cancelled = False
        for path, content in staged.items():
            if context.abort_signal is not None and context.abort_signal.cancelled:
                cancelled = True
                error = "apply_workspace_edit cancelled before committing the next file"
                break
            try:
                replace_file(path, content.encode("utf-8"), expected_content=raw_contents[path])
            except OSError as exc:
                error = f"apply_workspace_edit failed during write on {path}: {exc}"
                break
            applied_paths.append(display_by_path[path])

        if error is not None:
            remaining = tuple(display_by_path.values())[len(applied_paths) :]
            message = f"{error}. Applied prefix: {applied_paths}; uncommitted remaining: {list(remaining)}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message, bounds=OutputBounds(partial=True)),
                body=ApplyWorkspaceEditResultBody(paths=tuple(applied_paths), files=tuple(file_results[: len(applied_paths)])),
                changes=tuple(make_file_change(path) for path in applied_paths),
                execution=ConfirmedStop(cancellation_signalled=cancelled),
            )

        display_paths = tuple(sorted(display_by_path.values()))
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Applied {len(args.edits)} workspace edit(s)."),
            changes=tuple(make_file_change(path) for path in display_paths),
            body=ApplyWorkspaceEditResultBody(
                paths=display_paths,
                files=tuple(file_results),
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
        raise ValueError(f"apply_workspace_edit character must be >= 1 at {file_path}:{line_number}")

    if encoding == "utf-16":
        curr_u16 = 0
        for char_idx, ch in enumerate(line_text):
            units = 2 if ord(ch) > 0xFFFF else 1
            if curr_u16 == col:
                return char_idx
            if curr_u16 < col < curr_u16 + units:
                raise ValueError(f"apply_workspace_edit character lands mid-surrogate at {file_path}:{line_number}:{character_1based}")
            curr_u16 += units
        if col == curr_u16:
            return len(line_text)
        raise ValueError(
            f"apply_workspace_edit character {character_1based} is out of bounds for line {line_number} in {file_path} (max {curr_u16 + 1})"
        )

    elif encoding == "utf-8":
        curr_u8 = 0
        for char_idx, ch in enumerate(line_text):
            ch_bytes = len(ch.encode("utf-8"))
            if curr_u8 == col:
                return char_idx
            if curr_u8 < col < curr_u8 + ch_bytes:
                raise ValueError(
                    f"apply_workspace_edit character lands inside multi-byte UTF-8 sequence at {file_path}:{line_number}:{character_1based}"
                )
            curr_u8 += ch_bytes
        if col == curr_u8:
            return len(line_text)
        raise ValueError(
            f"apply_workspace_edit character {character_1based} is out of bounds for line {line_number} in {file_path} (max {curr_u8 + 1})"
        )

    elif encoding == "utf-32":
        if col > len(line_text):
            raise ValueError(
                f"apply_workspace_edit character {character_1based} is out of bounds for line {line_number} in {file_path} (max {len(line_text) + 1})"
            )
        return col

    raise ValueError(f"Unsupported positionEncoding: {encoding}")
