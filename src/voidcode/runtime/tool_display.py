"""Additive display metadata for runtime tool events.

This module is runtime-owned (see ``runtime/AGENTS.md``).  It derives curated
``display`` and ``tool_status`` payloads from tool name, arguments, and result
data.  Unknown/MCP tools receive a safe generic fallback that never exposes raw
JSON blobs.

The returned dicts follow the additive schema described in the opencode-style
tool UI plan:

* ``ToolDisplay`` – ``kind``, ``title``, ``summary``, optional ``args``,
  optional ``copyable``, optional ``hidden``.
* ``ToolStatusPayload`` – ``invocation_id``, ``tool_name``, ``phase``,
  ``status``, optional ``label``, optional ``display``.
"""

from __future__ import annotations

from typing import cast

# ── Tool-kind table ────────────────────────────────────────────────────────

_TOOL_KIND_TABLE: dict[str, tuple[str, str]] = {
    "shell_exec": ("shell", "Shell"),
    "read_file": ("read", "Read"),
    "write_file": ("write", "Write"),
    "edit": ("edit", "Edit"),
    "multi_edit": ("edit", "Edit"),
    "apply_patch": ("edit", "Edit"),
    "ast_grep_replace": ("edit", "Edit"),
    "format_file": ("edit", "Edit"),
    "grep": ("search", "Search"),
    "glob": ("context", "Context"),
    "list": ("context", "Context"),
    "code_search": ("search", "Search"),
    "ast_grep_search": ("search", "Search"),
    "ast_grep_preview": ("search", "Search"),
    "web_search": ("search", "Search"),
    "web_fetch": ("fetch", "Fetch"),
    "task": ("task", "Task"),
    "background_output": ("background", "Background"),
    "background_cancel": ("background", "Background"),
    "skill": ("skill", "Skill"),
    "question": ("question", "Question"),
    "lsp": ("lsp", "LSP"),
    "todo_write": ("generic", "Todo"),
}

_MAX_SUMMARY_LENGTH = 120
_MAX_ARG_LENGTH = 200
_MAX_ARGS_COUNT = 3

# ── Helpers ─────────────────────────────────────────────────────────────────


def _first_primitive(
    arguments: dict[str, object],
    *keys: str,
) -> str | None:
    """Return the first non-empty string value for the given keys."""
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _truncate_summary(text: str) -> str:
    """Truncate a summary string to a safe display length."""
    if len(text) <= _MAX_SUMMARY_LENGTH:
        return text
    return text[: _MAX_SUMMARY_LENGTH - 3] + "..."


def _synthesize_shell_summary(arguments: dict[str, object]) -> str:
    """Synthesize a shell summary from description or command fallback."""
    description = _first_primitive(arguments, "description")
    if description is not None:
        return _truncate_summary(description)

    command = _first_primitive(arguments, "command")
    if command is not None:
        return _truncate_summary(command)

    return "Shell command"


def _extract_primitive_args(
    arguments: dict[str, object],
    *preferred_keys: str,
) -> list[str]:
    """Extract max 3 primitive string values from tool arguments.

    Prefers the given key order, then fills remaining slots with other
    primitive (str/int/float/bool) values.
    """
    result: list[str] = []

    # Preferred keys first
    for key in preferred_keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            result.append(_truncate_arg(value))
            if len(result) >= _MAX_ARGS_COUNT:
                return result

    # Fill remaining with other primitive values
    skip_keys = set(preferred_keys) | {
        "todos",
        "edits",
        "content",
        "patch",
        "oldString",
        "newString",
        "data_uri",
        "description",
        "command",
        "modify",
    }
    for key, value in arguments.items():
        if key in skip_keys:
            continue
        if isinstance(value, str):
            if value.strip():
                result.append(_truncate_arg(value))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result.append(str(value))
        if len(result) >= _MAX_ARGS_COUNT:
            break

    return result


def _truncate_arg(value: str) -> str:
    """Truncate a single argument value for display."""
    if len(value) <= _MAX_ARG_LENGTH:
        return value
    return value[: _MAX_ARG_LENGTH - 3] + "..."


def _build_copyable(
    tool_name: str,
    arguments: dict[str, object],
    result_data: dict[str, object] | None,
) -> dict[str, object] | None:
    """Build optional copyable payload for the tool."""
    payload: dict[str, object] = {}

    if tool_name == "shell_exec":
        command = _first_primitive(arguments, "command")
        if command:
            payload["command"] = _truncate_arg(command)
        if result_data is not None:
            output = result_data.get("stdout")
            if isinstance(output, str) and output:
                payload["output"] = output
        return payload if payload else None

    # Path-based tools
    path = _first_primitive(arguments, "filePath", "path")
    if path is not None:
        payload["path"] = path

    return payload if payload else None


# ── Public API ──────────────────────────────────────────────────────────────


def build_tool_display(
    tool_name: str,
    arguments: dict[str, object],
    *,
    result_data: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build an additive ``ToolDisplay`` payload for a tool invocation.

    Args:
        tool_name: The runtime-resolved tool name (e.g. ``"shell_exec"``).
        arguments: Sanitized tool arguments.
        result_data: Sanitized tool result data (only available at completion).

    Returns:
        A dict conforming to the ``ToolDisplay`` schema.
    """
    kind, title = _TOOL_KIND_TABLE.get(tool_name, ("generic", tool_name))

    summary: str
    args: list[str] | None = None
    copyable: dict[str, object] | None = None
    hidden: bool = False

    if tool_name == "shell_exec":
        summary = _synthesize_shell_summary(arguments)
        args = _extract_primitive_args(arguments, "command")
        copyable = _build_copyable(tool_name, arguments, result_data)

    elif tool_name in {"read_file"}:
        path = _first_primitive(arguments, "filePath")
        summary = path if path else "Read file"
        args = _extract_primitive_args(arguments, "filePath")
        if path:
            copyable = {"path": path}

    elif tool_name in {"write_file"}:
        path = _first_primitive(arguments, "path")
        summary = path if path else "Write file"
        args = _extract_primitive_args(arguments, "path")
        if result_data is not None:
            bc = result_data.get("byte_count")
            if isinstance(bc, int):
                summary = f"{summary} ({bc}B)"
        if path:
            copyable = {"path": path}

    elif tool_name in {"edit", "multi_edit", "apply_patch", "ast_grep_replace", "format_file"}:
        path = _first_primitive(arguments, "filePath", "path")
        file_path_label = _first_primitive(arguments, "filePath", "path")
        edit_count = 0
        if result_data is not None:
            raw_edits = result_data.get("edit_count")
            if isinstance(raw_edits, int) and not isinstance(raw_edits, bool):
                edit_count = raw_edits
        if file_path_label and edit_count:
            summary = f"{file_path_label} ({edit_count} change{'s' if edit_count != 1 else ''})"
        elif file_path_label:
            summary = file_path_label
        else:
            summary = "Edit"
        args = _extract_primitive_args(arguments, "filePath", "path")
        if path:
            copyable = {"path": path}

    elif tool_name in {
        "grep",
        "glob",
        "list",
        "code_search",
        "ast_grep_search",
        "ast_grep_preview",
    }:
        query_keys: tuple[str, ...]
        if tool_name in {"grep", "code_search", "ast_grep_search", "ast_grep_preview"}:
            query_keys = ("pattern", "query")
        elif tool_name == "glob":
            query_keys = ("pattern",)
        else:
            query_keys = ("path",)
        query = _first_primitive(arguments, *query_keys)
        summary = query if query else title
        args = _extract_primitive_args(arguments, *query_keys)

    elif tool_name in {"web_search", "web_fetch"}:
        query = _first_primitive(arguments, "query", "url")
        summary = query if query else title
        args = _extract_primitive_args(arguments, "query", "url")

    elif tool_name == "task":
        desc = _first_primitive(arguments, "description")
        summary = desc if desc else "Task"
        args = _extract_primitive_args(arguments, "category", "subagent_type", "description")
        if desc:
            # description is already shown as summary; keep args cleaner
            pass

    elif tool_name == "background_output":
        task_id = _first_primitive(arguments, "task_id")
        summary = task_id if task_id else title
        args = _extract_primitive_args(arguments, "task_id")

    elif tool_name == "background_cancel":
        task_id = _first_primitive(arguments, "taskId", "task_id")
        summary = task_id if task_id else title
        args = _extract_primitive_args(arguments, "taskId", "task_id")

    elif tool_name == "skill":
        skill_name = _first_primitive(arguments, "name", "skill")
        summary = skill_name if skill_name else "Skill"
        args = _extract_primitive_args(arguments, "name", "skill")

    elif tool_name == "question":
        header = _first_primitive(arguments, "header")
        summary = header if header else "Question"
        args = _extract_primitive_args(arguments, "header")

    elif tool_name == "todo_write":
        summary = "Update todo list"
        hidden = True

    elif tool_name == "lsp":
        operation = _first_primitive(arguments, "operation")
        summary = operation if operation else "LSP"
        args = _extract_primitive_args(arguments, "operation")

    else:
        # Unknown / MCP fallback: safe generic with summary from first
        # non-empty descriptive argument.
        raw_summary = _first_primitive(
            arguments,
            "description",
            "query",
            "url",
            "filePath",
            "path",
            "pattern",
            "name",
        )
        if raw_summary is not None and len(raw_summary) > _MAX_SUMMARY_LENGTH:
            summary = _truncate_summary(raw_summary)
        else:
            summary = raw_summary or tool_name
        args = _extract_primitive_args(arguments)
        if not args:
            args = None

    display: dict[str, object] = {
        "kind": kind,
        "title": title,
        "summary": summary,
    }
    if args:
        display["args"] = args
    if copyable is not None:
        display["copyable"] = copyable
    if hidden:
        display["hidden"] = hidden

    return display


def build_tool_status(
    tool_name: str,
    tool_call_id: str | None,
    *,
    phase: str,
    status: str,
    display: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build an additive ``ToolStatusPayload`` for a tool event.

    Args:
        tool_name: The runtime-resolved tool name.
        tool_call_id: The invocation ID (may be None for deterministic).
        phase: Lifecycle phase (``"requested"``, ``"running"``,
               ``"completed"``, ``"failed"``).
        status: Execution status (``"pending"``, ``"running"``,
                ``"completed"``, ``"failed"``).
        display: Optional ``ToolDisplay`` to nest inside ``tool_status``.

    Returns:
        A dict conforming to the ``ToolStatusPayload`` schema.
    """
    payload: dict[str, object] = {
        "tool_name": tool_name,
        "phase": phase,
        "status": status,
    }
    if tool_call_id is not None:
        payload["invocation_id"] = tool_call_id
    if display is not None:
        label = cast(str, display.get("summary", ""))
        if label:
            payload["label"] = label
        payload["display"] = display
    return payload
