from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, ClassVar, cast

from pydantic import ValidationError

from ._pydantic_args import AstGrepReplaceArgs, AstGrepSearchArgs
from .contracts import ToolCall, ToolDefinition, ToolResult


def _resolve_candidate(*, workspace: Path, path_text: str) -> tuple[Path, str]:
    workspace_root = workspace.resolve()
    candidate = (workspace_root / Path(path_text)).resolve()
    if not candidate.is_relative_to(workspace_root):
        raise ValueError("ast_grep only allows paths inside the workspace")
    if not candidate.exists():
        raise ValueError(f"ast_grep target does not exist: {path_text}")
    return candidate, candidate.relative_to(workspace_root).as_posix()


def _parse_stream_output(stdout: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ast-grep returned invalid JSON stream output: {stripped}") from exc
        if isinstance(parsed, dict):
            matches.append(cast(dict[str, Any], parsed))
    return matches


def _run_ast_grep(
    *, cmd: list[str], workspace: Path
) -> ToolResult | subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=workspace.resolve(),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            tool_name=cmd[0].replace("-", "_"),
            status="error",
            error="ast-grep timed out after 30s",
        )
    except OSError as exc:
        return ToolResult(
            tool_name=cmd[0].replace("-", "_"),
            status="error",
            error=f"ast-grep not found or failed: {exc}",
        )
    return completed


def _raise_on_process_failure(
    *, completed: subprocess.CompletedProcess[str], fallback_message: str
) -> None:
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or fallback_message)


def _is_no_match_result(completed: subprocess.CompletedProcess[str]) -> bool:
    return (
        completed.returncode == 1 and not completed.stdout.strip() and not completed.stderr.strip()
    )


class AstGrepSearchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="ast_grep_search",
        description="Search code structurally with ast-grep patterns.",
        input_schema={
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "lang": {"type": "string"},
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = AstGrepSearchArgs.model_validate(
                {
                    "pattern": call.arguments.get("pattern"),
                    "path": call.arguments.get("path"),
                    "lang": call.arguments.get("lang"),
                }
            )
        except ValidationError as exc:
            first_error = exc.errors()[0]
            field_name = first_error.get("loc", (None,))[0]
            if field_name == "path":
                raise ValueError("ast_grep_search requires a string path argument") from exc
            if field_name == "lang":
                raise ValueError("ast_grep_search requires a string lang argument") from exc
            if first_error.get("type") == "value_error":
                raise ValueError("ast_grep_search pattern must not be empty") from exc
            raise ValueError("ast_grep_search requires a string pattern argument") from exc

        _, relative_path = _resolve_candidate(workspace=workspace, path_text=args.path)
        cmd = ["ast-grep", "run", "--json=stream", "-p", args.pattern]
        if args.lang:
            cmd.extend(["--lang", args.lang])
        cmd.append(relative_path)

        completed = _run_ast_grep(cmd=cmd, workspace=workspace)
        if isinstance(completed, ToolResult):
            return ToolResult(
                tool_name=self.definition.name,
                status=completed.status,
                error=completed.error,
            )

        if _is_no_match_result(completed):
            matches: list[dict[str, Any]] = []
        else:
            _raise_on_process_failure(
                completed=completed, fallback_message="ast-grep search failed"
            )
            matches = _parse_stream_output(completed.stdout)

        match_count = len(matches)
        summary = f"Found {match_count} AST match(es) in {relative_path}"
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=summary,
            data={
                "path": relative_path,
                "pattern": args.pattern,
                "lang": args.lang,
                "match_count": match_count,
                "matches": matches,
            },
        )


class AstGrepReplaceTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="ast_grep_replace",
        description="Preview or apply structural code rewrites with ast-grep.",
        input_schema={
            "pattern": {"type": "string"},
            "rewrite": {"type": "string"},
            "path": {"type": "string"},
            "lang": {"type": "string"},
            "apply": {"type": "boolean"},
        },
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = AstGrepReplaceArgs.model_validate(
                {
                    "pattern": call.arguments.get("pattern"),
                    "rewrite": call.arguments.get("rewrite"),
                    "path": call.arguments.get("path"),
                    "lang": call.arguments.get("lang"),
                    "apply": call.arguments.get("apply", False),
                }
            )
        except ValidationError as exc:
            first_error = exc.errors()[0]
            field_name = first_error.get("loc", (None,))[0]
            if field_name == "path":
                raise ValueError("ast_grep_replace requires a string path argument") from exc
            if field_name == "rewrite" and first_error.get("type") == "value_error":
                raise ValueError("ast_grep_replace rewrite must not be empty") from exc
            if field_name == "rewrite":
                raise ValueError("ast_grep_replace requires a string rewrite argument") from exc
            if field_name == "lang":
                raise ValueError("ast_grep_replace requires a string lang argument") from exc
            if field_name == "apply":
                raise ValueError("ast_grep_replace apply must be boolean") from exc
            if first_error.get("type") == "value_error":
                raise ValueError("ast_grep_replace pattern must not be empty") from exc
            raise ValueError("ast_grep_replace requires a string pattern argument") from exc

        _, relative_path = _resolve_candidate(workspace=workspace, path_text=args.path)
        preview_cmd = ["ast-grep", "run", "--json=stream", "-p", args.pattern, "-r", args.rewrite]
        if args.lang:
            preview_cmd.extend(["--lang", args.lang])

        if args.apply:
            preview_cmd.append(relative_path)
            preview_completed = _run_ast_grep(cmd=preview_cmd, workspace=workspace)
            if isinstance(preview_completed, ToolResult):
                return ToolResult(
                    tool_name=self.definition.name,
                    status=preview_completed.status,
                    error=preview_completed.error,
                )
            if _is_no_match_result(preview_completed):
                matches: list[dict[str, Any]] = []
            else:
                _raise_on_process_failure(
                    completed=preview_completed, fallback_message="ast-grep replace failed"
                )
                matches = _parse_stream_output(preview_completed.stdout)

            replacement_count = len(matches)
            apply_cmd = ["ast-grep", "run", "-p", args.pattern, "-r", args.rewrite]
            if args.lang:
                apply_cmd.extend(["--lang", args.lang])
            apply_cmd.extend(["-U", relative_path])
            completed = _run_ast_grep(cmd=apply_cmd, workspace=workspace)
            if isinstance(completed, ToolResult):
                return ToolResult(
                    tool_name=self.definition.name,
                    status=completed.status,
                    error=completed.error,
                )
            if not _is_no_match_result(completed):
                _raise_on_process_failure(
                    completed=completed, fallback_message="ast-grep replace failed"
                )
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=f"Applied {replacement_count} AST replacement(s) in {relative_path}",
                data={
                    "path": relative_path,
                    "pattern": args.pattern,
                    "rewrite": args.rewrite,
                    "lang": args.lang,
                    "replacement_count": replacement_count,
                    "matches": matches,
                    "applied": True,
                },
            )

        preview_cmd.append(relative_path)

        completed = _run_ast_grep(cmd=preview_cmd, workspace=workspace)
        if isinstance(completed, ToolResult):
            return ToolResult(
                tool_name=self.definition.name,
                status=completed.status,
                error=completed.error,
            )

        if _is_no_match_result(completed):
            matches = []
        else:
            _raise_on_process_failure(
                completed=completed, fallback_message="ast-grep replace failed"
            )
            matches = _parse_stream_output(completed.stdout)

        replacement_count = len(matches)
        action = "Applied" if args.apply else "Previewed"
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"{action} {replacement_count} AST replacement(s) in {relative_path}",
            data={
                "path": relative_path,
                "pattern": args.pattern,
                "rewrite": args.rewrite,
                "lang": args.lang,
                "replacement_count": replacement_count,
                "matches": matches,
                "applied": args.apply,
            },
        )
