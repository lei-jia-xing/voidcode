from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from ..security.path_policy import resolve_workspace_path
from ._post_edit_diagnostics import post_edit_lsp_diagnostics
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult


class _TextEdit(BaseModel):
    path: str
    startLine: int = Field(ge=1)
    startCharacter: int = Field(ge=1)
    endLine: int = Field(ge=1)
    endCharacter: int = Field(ge=1)
    newText: str
    expectedHash: str | None = None


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
                "description": "Text edits with 1-based line/character ranges and optional expectedHash.",
                "items": {"type": "object", "required": ["path", "startLine", "startCharacter", "endLine", "endCharacter", "newText"]},
            },
            "required": ["edits"],
        },
        read_only=False,
        path_argument_keys=("edits[].path",),
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
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
            if edit.expectedHash is not None and hashlib.sha256(current.encode("utf-8")).hexdigest() != edit.expectedHash:
                raise ValueError(f"apply_workspace_edit stale edit: {edit.path}")
            lines = current.splitlines(keepends=True)
            start = sum(len(line) for line in lines[: edit.startLine - 1]) + edit.startCharacter - 1
            end = sum(len(line) for line in lines[: edit.endLine - 1]) + edit.endCharacter - 1
            if start < 0 or end < start or end > len(current):
                raise ValueError(f"apply_workspace_edit range is out of bounds: {edit.path}")
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
