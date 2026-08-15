from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from ..security.path_policy import resolve_workspace_path
from ._post_edit_diagnostics import post_edit_lsp_diagnostics
from ._pydantic_args import format_validation_error
from ._repair import raise_tool_diagnostic
from .contracts import ToolCall, ToolDefinition, ToolResult
from .guards import enforce_seen_lines


class _TextEdit(BaseModel):
    path: str
    startLine: int = Field(ge=1)
    startCharacter: int = Field(ge=1)
    endLine: int = Field(ge=1)
    endCharacter: int = Field(ge=1)
    newText: str
    expectedHash: str


class _WorkspaceEditArgs(BaseModel):
    edits: list[_TextEdit] = Field(min_length=1)


class ApplyWorkspaceEditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="apply_workspace_edit",
        description="Apply a validated set of LSP text edits atomically inside the workspace.",
        input_schema={
            "edits": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Text edits with 1-based line/character ranges. Every edit requires expectedHash: "
                    "the SHA-256 hash of the current file content, taken from data.content_hash of a "
                    "prior read_file result."
                ),
                "items": {
                    "type": "object",
                    "required": ["path", "startLine", "startCharacter", "endLine", "endCharacter", "newText", "expectedHash"],
                },
            },
            "required": ["edits"],
        },
        read_only=False,
        path_argument_keys=("edits[].path",),
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
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
                            "Use read_file on each target path, copy data.content_hash from the results, "
                            "then retry apply_workspace_edit with expectedHash on every edit."
                        ),
                        details={
                            "edit_index": index + 1,
                            "path": raw_path if isinstance(raw_path, str) else None,
                        },
                    )

        try:
            args = _WorkspaceEditArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        originals: dict[Path, str] = {}
        grouped: dict[Path, list[tuple[int, int, str]]] = {}
        display_by_path: dict[Path, str] = {}
        for edit in args.edits:
            resolution = resolve_workspace_path(
                workspace=workspace,
                raw_path=edit.path,
                containment_error="apply_workspace_edit only allows workspace paths",
            )
            path = resolution.candidate
            if not path.is_file():
                raise ValueError(f"apply_workspace_edit target does not exist: {edit.path}")
            current = originals.setdefault(path, path.read_text(encoding="utf-8"))
            actual_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if actual_hash != edit.expectedHash:
                raise_tool_diagnostic(
                    message=f"apply_workspace_edit rejected because {edit.path} changed since it was read (stale edit).",
                    error_kind="stale_edit",
                    reason="content_hash_mismatch",
                    retry_guidance="Read the file again, use the returned data.content_hash, then retry apply_workspace_edit.",
                    details={"path": edit.path, "expected_hash": edit.expectedHash, "actual_hash": actual_hash},
                )
            lines = current.splitlines(keepends=True)
            start = sum(len(line) for line in lines[: edit.startLine - 1]) + edit.startCharacter - 1
            end = sum(len(line) for line in lines[: edit.endLine - 1]) + edit.endCharacter - 1
            if start < 0 or end < start or end > len(current):
                raise ValueError(f"apply_workspace_edit range is out of bounds: {edit.path}")
            enforce_seen_lines(
                tool_name=self.definition.name,
                workspace=workspace,
                raw_path=edit.path,
                candidate=path,
                display_path=resolution.relative_path,
                is_external=resolution.is_external,
                start_line=edit.startLine,
                end_line=edit.endLine,
            )
            grouped.setdefault(path, []).append((start, end, edit.newText))
            display_by_path[path] = resolution.relative_path

        staged: dict[Path, str] = {}
        for path, edits in grouped.items():
            ordered = sorted(edits, key=lambda item: (item[0], item[1]), reverse=True)
            previous_start = len(originals[path]) + 1
            content = originals[path]
            for start, end, new_text in ordered:
                if end > previous_start:
                    raise ValueError(f"apply_workspace_edit contains overlapping edits: {display_by_path[path]}")
                content = content[:start] + new_text + content[end:]
                previous_start = start
            staged[path] = content

        try:
            for path, content in staged.items():
                path.write_text(content, encoding="utf-8")
        except OSError:
            for path, original in originals.items():
                try:
                    path.write_text(original, encoding="utf-8")
                except OSError:
                    pass
            raise
        display_paths = list(display_by_path.values())
        diagnostics = post_edit_lsp_diagnostics(workspace=workspace, paths=display_paths)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Applied {len(args.edits)} workspace edit(s).",
            data={
                "paths": sorted(set(display_paths)),
                "diagnostics": diagnostics,
            },
        )
