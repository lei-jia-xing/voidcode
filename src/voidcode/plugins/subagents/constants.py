"""Wire constants and accepted-input vocabularies for the subagents plugin."""

from __future__ import annotations

from collections.abc import Mapping

SCHEMA_VERSION = 1
DEFAULT_JOIN_TIMEOUT_MS = 300_000
DEFAULT_JOIN_TIMEOUT_SECONDS = DEFAULT_JOIN_TIMEOUT_MS / 1000.0
#: One join slice keeps a parent stop observable without waiting out the caller's whole bound.
JOIN_SLICE_SECONDS = 1.0
MAX_BATCH_SIZE = 100
CANCEL_REASON = "parent cancellation via task tool"
#: One output page keeps the result body + text well under the core's 50 KiB projection.
OUTPUT_PAGE_BYTES = 8 * 1024
MAX_OUTPUT_PAGE_BYTES = 16 * 1024
#: Full-output structured validation never reads more than this many artifact bytes.
MAX_STRUCTURED_OUTPUT_BYTES = 4 * 1024 * 1024
_STRUCTURED_READ_PAGE_BYTES = 64 * 1024
#: Live history is read in event pages; the renderer bounds the rendered page independently.
DEFAULT_HISTORY_EVENTS = 100
MAX_HISTORY_EVENTS = 200

_TASK_OPERATIONS: tuple[str, ...] = ("output", "cancel", "send", "history")
_BATCH_OPERATIONS: tuple[str, ...] = ("output",)
_TASK_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "prompt",
        "label",
        "subagent_type",
        "tool_allowlist",
        "max_steps",
        "result_schema",
        "run_in_background",
        "operation",
        "task_id",
        "timeout",
        "output_offset",
        "output_limit",
        "after_sequence",
        "history_offset",
        "limit",
    }
)
_TASK_FIELD_INPUT_NAMES: Mapping[str, str] = {"timeout_seconds": "timeout", "history_limit": "limit"}
_SPAWN_INPUT_KEYS: frozenset[str] = frozenset(
    {"prompt", "label", "tool_allowlist", "max_steps", "run_in_background", "subagent_type", "result_schema", "operation"}
)
_FOREGROUND_SPAWN_INPUT_KEYS: frozenset[str] = _SPAWN_INPUT_KEYS | {"timeout", "output_offset", "output_limit"}
_CONTROL_INPUT_KEYS: Mapping[str, frozenset[str]] = {
    "output": frozenset({"operation", "task_id", "timeout", "output_offset", "output_limit"}),
    "cancel": frozenset({"operation", "task_id"}),
    "send": frozenset({"operation", "task_id", "prompt"}),
    "history": frozenset({"operation", "task_id", "after_sequence", "history_offset", "limit"}),
}
_CHILD_KEYS: frozenset[str] = frozenset({"label", "group_id", "subagent_type", "result_schema"})
_ITEM_KEYS: frozenset[str] = frozenset({"prompt", "label", "tool_allowlist", "max_steps", "subagent_type", "result_schema"})
_BATCH_KEYS: frozenset[str] = frozenset({"tasks", "operation", "parallel_group_id", "context"})
