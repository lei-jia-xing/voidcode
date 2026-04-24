from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ._pydantic_args import GrepArgs
from .contracts import ToolCall, ToolDefinition, ToolResult
from .workspace import resolve_workspace_path


@final
class GrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="grep",
        description=(
            "Search for a literal pattern in a UTF-8 text file inside the current workspace."
        ),
        input_schema={"pattern": {"type": "string"}, "path": {"type": "string"}},
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = GrepArgs.model_validate(
                {
                    "pattern": call.arguments.get("pattern"),
                    "path": call.arguments.get("path"),
                }
            )
        except ValidationError as exc:
            first_error = exc.errors()[0]
            field_name = first_error.get("loc", (None,))[0]
            if field_name == "path":
                raise ValueError("grep requires a string path argument") from exc
            if first_error.get("type") == "value_error":
                raise ValueError("grep pattern must not be empty") from exc
            raise ValueError("grep requires a string pattern argument") from exc

        candidate, relative_result_path = resolve_workspace_path(
            workspace=workspace,
            path_text=args.path,
            tool_name=self.definition.name,
            must_be_file=True,
        )

        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("grep only supports UTF-8 text files") from exc

        if "\x00" in content:
            raise ValueError("grep only supports UTF-8 text files")

        matches: list[dict[str, object]] = []
        total_occurrences = 0

        for line_number, line_text in enumerate(content.splitlines(), start=1):
            columns: list[int] = []
            start_index = 0

            while True:
                found_index = line_text.find(args.pattern, start_index)
                if found_index < 0:
                    break
                columns.append(found_index + 1)
                total_occurrences += 1
                start_index = found_index + len(args.pattern)

            if columns:
                matches.append(
                    {
                        "line": line_number,
                        "text": line_text,
                        "columns": columns,
                    }
                )

        if matches:
            preview_lines = [f"{match['line']}: {match['text']}" for match in matches[:10]]
            summary = (
                f"Found {total_occurrences} match(es) for {args.pattern!r} in "
                f"{relative_result_path}\n" + "\n".join(preview_lines)
            )
        else:
            summary = f"Found 0 match(es) for {args.pattern!r} in {relative_result_path}"

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=summary,
            data={
                "path": relative_result_path,
                "pattern": args.pattern,
                "match_count": total_occurrences,
                "matches": matches,
            },
        )
