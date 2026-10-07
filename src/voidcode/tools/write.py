from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, final

from pydantic import BaseModel

from ..core.tool_context import ToolContext
from ..formatter import FormatterExecutor, formatter_diagnostics, formatter_payload
from ..hook.config import RuntimeHooksConfig
from ..security.json_values import json_wire_object, own_json_object
from ..security.path_policy import resolve_workspace_path
from ._post_edit_diagnostics import post_edit_lsp_diagnostics
from ._pydantic_args import parse_tool_args
from ._repair import raise_tool_diagnostic
from .contracts import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess
from .guards import enforce_read_before_write, enforce_seen_whole_file


@dataclass(frozen=True, slots=True)
class WriteResultBody:
    path: str
    byte_count: int
    diff: str
    formatter: Mapping[str, object] | None = None
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.formatter is not None:
            object.__setattr__(self, "formatter", own_json_object(self.formatter))
        object.__setattr__(self, "diagnostics", tuple(own_json_object(item) for item in self.diagnostics))

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "byte_count": self.byte_count,
            "diff": self.diff,
        }
        if self.formatter is not None:
            payload["formatter"] = json_wire_object(self.formatter)
        if self.diagnostics:
            payload["diagnostics"] = [json_wire_object(item) for item in self.diagnostics]
        return payload


class WriteArgs(BaseModel):
    path: str
    content: str


@final
class WriteTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="write",
        description="Write a UTF-8 text file inside the current workspace.",
        input_schema={
            "path": {
                "type": "string",
                "description": "Path relative to the workspace; parent directories are created when needed.",
            },
            "content": {
                "type": "string",
                "description": "Complete UTF-8 file contents; this replaces the existing file.",
            },
            "expectedHash": {
                "type": "string",
                "description": (
                    "Required when the target file already exists: SHA-256 hash of the current file "
                    "content, taken from the SHA-256 content hash shown in read output. Rejects stale "
                    "overwrites when the file changed since that read. Omit for brand-new files."
                ),
            },
            "required": ["path", "content"],
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path",),
    )

    def __init__(self, *, hooks_config: RuntimeHooksConfig | None = None) -> None:
        self._hooks_config = hooks_config

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        args = parse_tool_args(
            WriteArgs,
            {
                "path": call.arguments.get("path"),
                "content": call.arguments.get("content"),
            },
            tool_name=self.definition.name,
        )

        resolution = resolve_workspace_path(
            workspace=workspace,
            raw_path=args.path,
            containment_error="write only allows paths inside the workspace",
            allow_outside_workspace=True,
        )
        workspace_root = resolution.workspace_root
        candidate = resolution.candidate
        display_path = str(candidate.resolve()) if resolution.is_external else resolution.relative_path

        enforce_read_before_write(
            context=context,
            tool_name=self.definition.name,
            workspace=workspace_root,
            raw_path=args.path,
            candidate=candidate,
            display_path=display_path,
            is_external=resolution.is_external,
        )

        if candidate.exists():
            expected_hash = call.arguments.get("expectedHash")
            if not isinstance(expected_hash, str):
                raise_tool_diagnostic(
                    message="write requires an expectedHash argument when overwriting an existing file.",
                    error_kind="tool_input_mismatch",
                    reason="missing_expected_hash",
                    retry_guidance=(
                        "Use read on the target path, copy its SHA-256 content hash from the output, then retry write with that expectedHash."
                    ),
                    details={"path": display_path, "raw_path": args.path},
                )
            actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if expected_hash != actual_hash:
                raise_tool_diagnostic(
                    message="write rejected because the file changed since it was read (stale write).",
                    error_kind="stale_edit",
                    reason="content_hash_mismatch",
                    retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry write.",
                    details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
                )
            enforce_seen_whole_file(
                context=context,
                tool_name=self.definition.name,
                workspace=workspace_root,
                raw_path=args.path,
                candidate=candidate,
                display_path=display_path,
                is_external=resolution.is_external,
            )

        candidate.parent.mkdir(parents=True, exist_ok=True)
        old_content = candidate.read_text(encoding="utf-8") if candidate.exists() else ""
        candidate.write_text(args.content, encoding="utf-8")

        formatter_result = None
        if self._hooks_config is not None:
            formatter_result = FormatterExecutor(self._hooks_config, workspace_root).run(candidate)

        diagnostics = formatter_diagnostics(formatter_result)
        content = f"Wrote file successfully: {display_path}"
        if diagnostics:
            content += f" Formatter warning: {diagnostics[0]['message']}"

        new_content = candidate.read_text(encoding="utf-8")
        relative_output_path = candidate.relative_to(workspace_root).as_posix() if not resolution.is_external else candidate.as_posix()
        diff = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{relative_output_path}",
                tofile=f"b/{relative_output_path}",
            )
        )

        byte_count = candidate.stat().st_size
        formatter = None
        if formatter_result is not None and formatter_result.status != "not_configured":
            formatter = formatter_payload(formatter_result)
            byte_count = len(candidate.read_text(encoding="utf-8").encode("utf-8"))
        lsp_diagnostics = post_edit_lsp_diagnostics(
            context=context,
            workspace=workspace_root,
            paths=[display_path],
        )
        return ToolSuccess(
            self.definition.name,
            output=TextOutput(content),
            body=WriteResultBody(
                path=display_path,
                byte_count=byte_count,
                diff=diff,
                formatter=formatter,
                diagnostics=tuple([*diagnostics, *lsp_diagnostics]),
            ),
        )
