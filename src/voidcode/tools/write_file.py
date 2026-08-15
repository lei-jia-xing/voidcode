from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, ValidationError

from ..formatter import FormatterExecutor, formatter_diagnostics, formatter_payload
from ..hook.config import RuntimeHooksConfig
from ..security.path_policy import resolve_workspace_path
from ._post_edit_diagnostics import post_edit_lsp_diagnostics
from ._pydantic_args import format_validation_error
from ._repair import raise_tool_diagnostic
from .contracts import ToolCall, ToolDefinition, ToolResult
from .guards import enforce_read_before_write, enforce_seen_whole_file


class WriteFileArgs(BaseModel):
    path: str
    content: str


@final
class WriteFileTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="write_file",
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
                    "content, taken from data.content_hash of a prior read result. Rejects stale "
                    "overwrites when the file changed since that read. Omit for brand-new files."
                ),
            },
            "required": ["path", "content"],
        },
        read_only=False,
        path_argument_keys=("path",),
    )

    def __init__(self, *, hooks_config: RuntimeHooksConfig | None = None) -> None:
        self._hooks_config = hooks_config

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = WriteFileArgs.model_validate(
                {
                    "path": call.arguments.get("path"),
                    "content": call.arguments.get("content"),
                }
            )
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        resolution = resolve_workspace_path(
            workspace=workspace,
            raw_path=args.path,
            containment_error="write_file only allows paths inside the workspace",
            allow_outside_workspace=True,
        )
        workspace_root = resolution.workspace_root
        candidate = resolution.candidate
        display_path = str(candidate.resolve()) if resolution.is_external else resolution.relative_path

        enforce_read_before_write(
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
                    message="write_file requires an expectedHash argument when overwriting an existing file.",
                    error_kind="tool_input_mismatch",
                    reason="missing_expected_hash",
                    retry_guidance=(
                        "Use read on the target path, copy data.content_hash from the result, then retry write_file with that expectedHash."
                    ),
                    details={"path": display_path, "raw_path": args.path},
                )
            actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if expected_hash != actual_hash:
                raise_tool_diagnostic(
                    message="write_file rejected because the file changed since it was read (stale write).",
                    error_kind="stale_edit",
                    reason="content_hash_mismatch",
                    retry_guidance="Read the file again, use the returned data.content_hash, then retry write_file.",
                    details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
                )
            enforce_seen_whole_file(
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

        data: dict[str, object] = {
            "path": display_path,
            "byte_count": candidate.stat().st_size,
            "diff": diff,
        }
        if formatter_result is not None and formatter_result.status != "not_configured":
            data["formatter"] = formatter_payload(formatter_result)
            data["byte_count"] = len(candidate.read_text(encoding="utf-8").encode("utf-8"))
        if diagnostics:
            data["diagnostics"] = diagnostics
        lsp_diagnostics = post_edit_lsp_diagnostics(
            workspace=workspace_root,
            paths=[display_path],
        )
        if lsp_diagnostics:
            current_diagnostics = data.get("diagnostics")
            existing = current_diagnostics if isinstance(current_diagnostics, list) else []
            data["diagnostics"] = [*existing, *lsp_diagnostics]

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=data,
        )
