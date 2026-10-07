from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from ..core.tool_context import ToolContext
from ..security.json_values import json_wire_object, own_json_object
from ..security.path_policy import resolve_workspace_path
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


class _WorkspaceEditArgs(BaseModel):
    edits: list[_TextEdit] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ApplyWorkspaceEditResultBody:
    paths: tuple[str, ...]
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(own_json_object(item) for item in self.diagnostics))

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"paths": list(self.paths)}
        if self.diagnostics:
            payload["diagnostics"] = [json_wire_object(item) for item in self.diagnostics]
        return payload


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
                    "the SHA-256 content hash shown in the read output."
                ),
                "items": {
                    "type": "object",
                    "required": ["path", "startLine", "startCharacter", "endLine", "endCharacter", "newText", "expectedHash"],
                },
            },
            "required": ["edits"],
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("edits[].path",),
    )

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
                    retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry apply_workspace_edit.",
                    details={"path": edit.path, "expected_hash": edit.expectedHash, "actual_hash": actual_hash},
                )
            lines = current.splitlines(keepends=True)
            start = sum(len(line) for line in lines[: edit.startLine - 1]) + edit.startCharacter - 1
            end = sum(len(line) for line in lines[: edit.endLine - 1]) + edit.endCharacter - 1
            if start < 0 or end < start or end > len(current):
                raise ValueError(f"apply_workspace_edit range is out of bounds: {edit.path}")
            enforce_seen_lines(
                context=context,
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
        diagnostics = post_edit_lsp_diagnostics(context=context, workspace=workspace, paths=display_paths)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Applied {len(args.edits)} workspace edit(s)."),
            body=ApplyWorkspaceEditResultBody(
                paths=tuple(sorted(set(display_paths))),
                diagnostics=tuple(diagnostics),
            ),
        )
