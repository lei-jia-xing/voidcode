from __future__ import annotations

from functools import cache
from pathlib import Path

_GUIDANCE_DIR = Path(__file__).resolve().parent

_TOOL_GUIDANCE_FILES = {
    "apply_workspace_edit": "apply_workspace_edit.txt",
    "ast_grep": "ast_grep.txt",
    "apply_patch": "apply_patch.txt",
    "background_cancel": "background_cancel.txt",
    "background_output": "background_output.txt",
    "background_ps": "background_ps.txt",
    "background_process_logs": "background_process_logs.txt",
    "background_process_send": "background_process_send.txt",
    "background_process_start": "background_process_start.txt",
    "background_process_stop": "background_process_stop.txt",
    "edit": "edit.txt",
    "glob": "glob.txt",
    "grep": "grep.txt",
    "invoke_tool": "invoke_tool.txt",
    "lsp": "lsp.txt",
    "multi_edit": "multi_edit.txt",
    "question": "question.txt",
    "read": "read.txt",
    "shell_exec": "shell_exec.txt",
    "skill": "skill.txt",
    "task": "task.txt",
    "task_batch": "task_batch.txt",
    "steer_task": "steer_task.txt",
    "todo_write": "todo_write.txt",
    "web_fetch": "web_fetch.txt",
    "web_search": "web_search.txt",
    "yield": "yield.txt",
    "write": "write.txt",
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
