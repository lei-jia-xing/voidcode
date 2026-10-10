from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from ..core.tool_context import LspDocumentState, ToolContext
from ..formatter import FormatterExecutor, formatter_diagnostics, formatter_payload
from ..hook.config import RuntimeHooksConfig
from ..security.json_values import json_wire_object, own_json_object
from ._post_edit_diagnostics import post_edit_lsp_diagnostics
from ._pydantic_args import parse_tool_args
from ._repair import raise_tool_diagnostic
from .contracts import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess
from .guards import enforce_seen_lines


class _TextEdit(BaseModel):
    path: str
    startLine: int = Field(ge=1)
    startCharacter: int = Field(ge=1)
    endLine: int = Field(ge=1)
    endCharacter: int = Field(ge=1)
    newText: str
    expectedHash: str
    expectedVersion: int | None = None
    version: int | None = None


class _WorkspaceEditArgs(BaseModel):
    positionEncoding: str | None = None
    edits: list[_TextEdit] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ApplyWorkspaceEditResultBody:
    paths: tuple[str, ...]
    files: tuple[Mapping[str, object], ...] = ()
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(own_json_object(item) for item in self.files))
        object.__setattr__(self, "diagnostics", tuple(own_json_object(item) for item in self.diagnostics))

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"paths": list(self.paths)}
        if self.files:
            payload["files"] = [json_wire_object(item) for item in self.files]
        if self.diagnostics:
            payload["diagnostics"] = [json_wire_object(item) for item in self.diagnostics]
        return payload


class ApplyWorkspaceEditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="apply_workspace_edit",
        description="Apply validated LSP text edits with document-version checks; runtime approval governs filesystem access.",
        input_schema={
            "type": "object",
            "properties": {
                "positionEncoding": {
                    "type": "string",
                    "enum": ["utf-8", "utf-16", "utf-32"],
                    "description": "LSP position encoding; defaults to the opened document's negotiated encoding or UTF-16.",
                },
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "Text edits with 1-based line/character ranges. Every edit requires expectedHash from read. "
                        "expectedVersion, when provided, must match the currently opened LSP document."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["path", "startLine", "startCharacter", "endLine", "endCharacter", "newText", "expectedHash"],
                        "properties": {
                            "path": {"type": "string"},
                            "startLine": {"type": "integer", "minimum": 1},
                            "startCharacter": {"type": "integer", "minimum": 1},
                            "endLine": {"type": "integer", "minimum": 1},
                            "endCharacter": {"type": "integer", "minimum": 1},
                            "newText": {"type": "string"},
                            "expectedHash": {"type": "string"},
                            "expectedVersion": {
                                "type": "integer",
                                "description": "Optional open-document version from the LSP result; checked against runtime document state.",
                            },
                            "version": {"type": "integer", "description": "LSP documentChanges version; checked when supplied."},
                        },
                    },
                },
            },
            "required": ["edits"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("edits[].path",),
    )

    def __init__(self, *, hooks_config: RuntimeHooksConfig | None = None) -> None:
        self._hooks_config = hooks_config

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        raw_edits = call.arguments.get("edits")
        if isinstance(raw_edits, list):
            for index, item in enumerate(raw_edits):
                if isinstance(item, dict) and not isinstance(item.get("expectedHash"), str):
                    raw_path = item.get("path")
                    raise_tool_diagnostic(
                        message=f"apply_workspace_edit edit #{index + 1} requires a string expectedHash argument.",
                        error_kind="tool_input_mismatch",
                        reason="missing_expected_hash",
                        retry_guidance=(
                            "Use read on each target path and copy its SHA-256 content hash from the output, "
                            "then retry apply_workspace_edit with expectedHash on every edit."
                        ),
                        details={
                            "edit_index": index + 1,
                            "path": raw_path if isinstance(raw_path, str) else None,
                        },
                    )

        args = parse_tool_args(_WorkspaceEditArgs, call.arguments, tool_name=self.definition.name)
        requested_encoding = args.positionEncoding.lower() if args.positionEncoding is not None else None
        if requested_encoding is not None and requested_encoding not in ("utf-8", "utf-16", "utf-32"):
            raise ValueError(f"apply_workspace_edit unsupported positionEncoding: {args.positionEncoding}")

        raw_contents: dict[Path, bytes] = {}
        originals: dict[Path, str] = {}
        document_states: dict[Path, LspDocumentState | None] = {}
        grouped: dict[Path, list[tuple[int, int, str]]] = {}
        display_by_path: dict[Path, str] = {}
        workspace_root = workspace.resolve()
        for edit in args.edits:
            requested_path = Path(edit.path)
            path = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
            is_external = not path.is_relative_to(workspace_root)
            display_path = path.as_posix() if is_external else path.relative_to(workspace_root).as_posix()
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
                raise_tool_diagnostic(
                    message=f"apply_workspace_edit rejected because {edit.path} changed since it was read (stale edit).",
                    error_kind="stale_edit",
                    reason="content_hash_mismatch",
                    retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry apply_workspace_edit.",
                    details={"path": edit.path, "expected_hash": edit.expectedHash, "actual_hash": actual_hash},
                )

            if path not in document_states:
                reader = context.lsp_document_state
                document_states[path] = reader(file_path=path, workspace=workspace) if reader is not None else None
            document_state = document_states[path]
            requested_version = edit.expectedVersion if edit.expectedVersion is not None else edit.version
            if requested_version is not None and document_state is None:
                raise ValueError(
                    f"apply_workspace_edit requested expectedVersion {requested_version} for {edit.path}, "
                    "but runtime document state is unavailable (fail-closed)"
                )
            if document_state is not None and document_state.content_hash != actual_hash:
                raise_tool_diagnostic(
                    message=f"apply_workspace_edit rejected because the opened LSP document changed for {edit.path}.",
                    error_kind="stale_edit",
                    reason="document_content_mismatch",
                    retry_guidance="Refresh the LSP result and read the current file before retrying the workspace edit.",
                    details={
                        "path": edit.path,
                        "document_hash": document_state.content_hash,
                        "actual_hash": actual_hash,
                    },
                )
            if requested_version is not None and document_state is not None and requested_version != document_state.version:
                raise_tool_diagnostic(
                    message=(
                        f"apply_workspace_edit rejected because {edit.path} is at opened LSP version "
                        f"{document_state.version}, not requested version {requested_version}."
                    ),
                    error_kind="stale_edit",
                    reason="document_version_mismatch",
                    retry_guidance="Refresh the LSP result and retry the complete workspace edit against the current document version.",
                    details={
                        "path": edit.path,
                        "expected_version": requested_version,
                        "actual_version": document_state.version,
                    },
                )

            encoding = requested_encoding or (document_state.position_encoding if document_state is not None else "utf-16")
            if document_state is not None and encoding != document_state.position_encoding:
                raise ValueError(
                    f"apply_workspace_edit positionEncoding {encoding} does not match the opened LSP document "
                    f"encoding {document_state.position_encoding} for {edit.path}"
                )

            lines = current.splitlines(keepends=True)
            if not lines:
                lines = [""]
            elif lines[-1].endswith(("\r", "\n")):
                lines.append("")
            if edit.startLine < 1 or edit.startLine > len(lines):
                raise ValueError(f"apply_workspace_edit startLine {edit.startLine} is out of bounds in {edit.path}")
            if edit.endLine < 1 or edit.endLine > len(lines):
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

            enforce_seen_lines(
                context=context,
                tool_name=self.definition.name,
                raw_path=edit.path,
                candidate=path,
                display_path=display_path,
                start_line=edit.startLine,
                end_line=edit.endLine,
            )
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
            for start, end, new_text in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
                content = content[:start] + new_text + content[end:]
            staged[path] = content

        applied_paths: list[str] = []
        uncommitted_paths: list[str] = [display_by_path[path] for path in staged]
        try:
            for path, content in staged.items():
                path.write_bytes(content.encode("utf-8"))
                display_path = display_by_path[path]
                applied_paths.append(display_path)
                uncommitted_paths.remove(display_path)
        except OSError as exc:
            raise RuntimeError(
                f"apply_workspace_edit failed during write on {path}: {exc}. "
                f"Applied prefix: {applied_paths}; uncommitted remaining: {uncommitted_paths}"
            ) from exc

        diagnostics: list[Mapping[str, object]] = []
        file_results: list[Mapping[str, object]] = []
        for path in staged:
            formatter_result = FormatterExecutor(self._hooks_config, workspace).run(path) if self._hooks_config is not None else None
            diagnostics.extend(formatter_diagnostics(formatter_result))
            final_bytes = path.read_bytes()
            final_content = final_bytes.decode("utf-8")
            display_path = display_by_path[path]
            file_result: dict[str, object] = {
                "path": display_path,
                "byte_count": len(final_bytes),
                "content_hash": hashlib.sha256(final_bytes).hexdigest(),
                "diff": "".join(
                    difflib.unified_diff(
                        originals[path].splitlines(keepends=True),
                        final_content.splitlines(keepends=True),
                        fromfile=f"a/{display_path}",
                        tofile=f"b/{display_path}",
                    )
                ),
            }
            if formatter_result is not None and formatter_result.status != "not_configured":
                file_result["formatter"] = formatter_payload(formatter_result)
            file_results.append(file_result)

        display_paths = list(display_by_path.values())
        diagnostics.extend(post_edit_lsp_diagnostics(context=context, workspace=workspace, paths=display_paths))
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Applied {len(args.edits)} workspace edit(s)."),
            body=ApplyWorkspaceEditResultBody(
                paths=tuple(sorted(set(display_paths))),
                files=tuple(file_results),
                diagnostics=tuple(diagnostics),
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
