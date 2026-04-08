from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

from .contracts import ToolCall, ToolDefinition, ToolResult


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
        pattern_value = call.arguments.get("pattern")
        if not isinstance(pattern_value, str):
            raise ValueError("grep requires a string pattern argument")

        path_value = call.arguments.get("path")
        if not isinstance(path_value, str):
            raise ValueError("grep requires a string path argument")

        if pattern_value == "":
            raise ValueError("grep pattern must not be empty")

        relative_path = Path(path_value)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()

        if not candidate.is_relative_to(workspace_root):
            raise ValueError("grep only allows paths inside the workspace")

        if not candidate.is_file():
            raise ValueError(f"grep target does not exist: {path_value}")

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
                found_index = line_text.find(pattern_value, start_index)
                if found_index < 0:
                    break
                columns.append(found_index + 1)
                total_occurrences += 1
                start_index = found_index + len(pattern_value)

            if columns:
                matches.append(
                    {
                        "line": line_number,
                        "text": line_text,
                        "columns": columns,
                    }
                )

        relative_result_path = candidate.relative_to(workspace_root).as_posix()
        if matches:
            preview_lines = [f"{match['line']}: {match['text']}" for match in matches[:10]]
            summary = (
                f"Found {total_occurrences} match(es) for {pattern_value!r} in "
                f"{relative_result_path}\n" + "\n".join(preview_lines)
            )
        else:
            summary = f"Found 0 match(es) for {pattern_value!r} in {relative_result_path}"

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=summary,
            data={
                "path": relative_result_path,
                "pattern": pattern_value,
                "match_count": total_occurrences,
                "matches": matches,
            },
        )
