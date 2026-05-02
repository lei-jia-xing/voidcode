from __future__ import annotations

from functools import cache
from pathlib import Path

from .contracts import ToolDefinition

_GUIDANCE_DIR = Path(__file__).resolve().parent
_GUIDANCE_SEPARATOR = "\n\nAgent usage guidance:\n"

_TOOL_GUIDANCE_FILES = {
    "background_cancel": "background_cancel.txt",
    "background_output": "background_output.txt",
    "apply_patch": "apply_patch.txt",
    "ast_grep_preview": "ast_grep.txt",
    "ast_grep_replace": "ast_grep.txt",
    "ast_grep_search": "ast_grep.txt",
    "code_search": "code_search.txt",
    "edit": "edit.txt",
    "format_file": "format_file.txt",
    "glob": "glob.txt",
    "grep": "grep.txt",
    "lsp": "lsp.txt",
    "multi_edit": "multi_edit.txt",
    "question": "question.txt",
    "read_file": "read_file.txt",
    "shell_exec": "shell_exec.txt",
    "skill": "skill.txt",
    "task": "task.txt",
    "todo_write": "todo_write.txt",
    "web_fetch": "web_fetch.txt",
    "web_search": "web_search.txt",
    "write_file": "write_file.txt",
}


def guidance_filename_for_tool(tool_name: str) -> str | None:
    if tool_name.startswith("mcp/"):
        return "mcp.txt"
    return _TOOL_GUIDANCE_FILES.get(tool_name)


@cache
def load_tool_guidance(filename: str) -> str:
    path = _GUIDANCE_DIR / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def guidance_for_tool(tool_name: str) -> str:
    filename = guidance_filename_for_tool(tool_name)
    if filename is None:
        return ""
    return load_tool_guidance(filename)


def definition_with_guidance(definition: ToolDefinition) -> ToolDefinition:
    guidance = guidance_for_tool(definition.name)
    if not guidance:
        return definition
    if definition.name.startswith("mcp/"):
        if guidance in definition.description:
            return definition
        description = f"{definition.description.rstrip()}{_GUIDANCE_SEPARATOR}{guidance}"
    else:
        description = guidance
    return ToolDefinition(
        name=definition.name,
        description=description,
        input_schema=definition.input_schema,
        read_only=definition.read_only,
    )
