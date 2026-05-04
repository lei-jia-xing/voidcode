from __future__ import annotations

from ..tools.contracts import ToolCall
from .permission_context import extract_shell_path_candidates


def shell_command_for_tool_call(tool_call: ToolCall) -> str | None:
    if tool_call.tool_name != "shell_exec":
        return None
    command = tool_call.arguments.get("command")
    return command if isinstance(command, str) else None


def extract_paths_from_patch(patch_text: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("*** Add File: "):
            paths.append(line.removeprefix("*** Add File: ").strip())
        elif line.startswith("*** Update File: "):
            paths.append(line.removeprefix("*** Update File: ").strip())
        elif line.startswith("*** Delete File: "):
            paths.append(line.removeprefix("*** Delete File: ").strip())
        elif line.startswith("*** Move to: "):
            paths.append(line.removeprefix("*** Move to: ").strip())
    return tuple(path for path in paths if path)


def shell_path_candidates(command: str) -> tuple[str, ...]:
    return extract_shell_path_candidates(command)
