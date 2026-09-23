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

# ── Tool-kind table ────────────────────────────────────────────────────────

_TOOL_KIND_TABLE: dict[str, tuple[str, str]] = {
    "shell_exec": ("shell", "Shell"),
    "read": ("read", "Read"),
    "write": ("write", "Write"),
    "edit": ("edit", "Edit"),
    "multi_edit": ("edit", "Edit"),
    "apply_patch": ("edit", "Edit"),
    "ast_grep": ("search", "Search"),
    "grep": ("search", "Search"),
    "glob": ("context", "Context"),
    "web_search": ("search", "Search"),
    "web_fetch": ("fetch", "Fetch"),
    "task": ("task", "Task"),
    "background_task": ("background", "Background"),
    "skill": ("skill", "Skill"),
    "question": ("question", "Question"),
    "lsp": ("lsp", "LSP"),
    "todo": ("generic", "Todo"),
}

_MAX_SUMMARY_LENGTH = 120
_MAX_ARG_LENGTH = 200
_MAX_ARGS_COUNT = 3

#: tool name -> (summary key order, arg key order, fallback summary).
_TOOL_DISPLAY_TABLE: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "read": (("path",), ("path",), "Read file"),
    "write": (("path",), ("path",), "Write file"),
    "grep": (("pattern", "query"), ("pattern", "query"), "Search"),
    "ast_grep": (("pattern", "query"), ("pattern", "query"), "Search"),
    "glob": (("pattern",), ("pattern",), "Context"),
    "web_search": (("query", "url"), ("query", "url"), "Search"),
    "web_fetch": (("query", "url"), ("query", "url"), "Fetch"),
    "task": (("description",), ("subagent_type", "description"), "Task"),
    "background_task": (("task_id", "operation"), ("operation", "task_id", "prompt"), "Background"),
    "skill": (("name",), ("name",), "Skill"),
    "question": (("header",), ("header",), "Question"),
    "lsp": (("operation",), ("operation",), "LSP"),
}

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
    path_key = "path"
    path = _first_primitive(arguments, path_key)
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

    elif tool_name in {"edit", "multi_edit", "apply_patch"}:
        path = _first_primitive(arguments, "path")
        edit_count = 0
        if result_data is not None:
            raw_edits = result_data.get("edit_count")
            if isinstance(raw_edits, int) and not isinstance(raw_edits, bool):
                edit_count = raw_edits
        if path and edit_count:
            summary = f"{path} ({edit_count} change{'s' if edit_count != 1 else ''})"
        else:
            summary = path or "Edit"
        args = _extract_primitive_args(arguments, "path")
        if path:
            copyable = {"path": path}

    elif tool_name == "todo":
        summary = "Update todo list"
        hidden = True

    elif (entry := _TOOL_DISPLAY_TABLE.get(tool_name)) is not None:
        summary_keys, args_keys, fallback = entry
        matched = _first_primitive(arguments, *summary_keys)
        summary = matched if matched else fallback
        args = _extract_primitive_args(arguments, *args_keys)
        if "path" in args_keys:
            path = _first_primitive(arguments, "path")
            if path:
                copyable = {"path": path}
        if tool_name == "write" and result_data is not None:
            byte_count = result_data.get("byte_count")
            if isinstance(byte_count, int):
                summary = f"{summary} ({byte_count}B)"

    else:
        # Unknown / MCP fallback: safe generic with summary from first
        # non-empty descriptive argument.
        raw_summary = _first_primitive(
            arguments,
            "description",
            "query",
            "url",
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
    tool_call_id: str,
    *,
    phase: str,
    status: str,
    display: dict[str, object],
) -> dict[str, object]:
    """Build an additive ``ToolStatusPayload`` for a tool event.

    Args:
        tool_name: The runtime-resolved tool name.
        tool_call_id: The invocation ID.
        phase: Lifecycle phase (``"requested"``, ``"running"``,
               ``"completed"``, ``"failed"``).
        status: Execution status (``"pending"``, ``"running"``,
                ``"completed"``, ``"failed"``).
        display: ``ToolDisplay`` to nest inside ``tool_status``.

    Returns:
        A dict conforming to the ``ToolStatusPayload`` schema.
    """
    payload: dict[str, object] = {
        "tool_name": tool_name,
        "invocation_id": tool_call_id,
        "phase": phase,
        "status": status,
    }
    label = display["summary"]
    if label:
        payload["label"] = label
    payload["display"] = display
    return payload
