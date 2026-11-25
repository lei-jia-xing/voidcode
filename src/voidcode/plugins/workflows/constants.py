"""Workflow limits, statuses, persisted keys, and the owner-state schema contract."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

SCHEMA_VERSION = 1
#: Inline outcome bound; anything larger is spilled to an artifact and referenced.
INLINE_OUTPUT_BYTES = 8 * 1024
#: Bounded page when reading one node result through the status command.
RESULT_PAGE_BYTES = 8 * 1024
#: Bounded nodes shown inline by one status call; the state always holds the rest.
STATUS_NODE_LIMIT = 50
#: Internal driver bounds; the admitted max_root_children quota still governs total children.
#: The join slice is short so pause/cancel land promptly; longer runs simply re-drive.
JOIN_SLICE_SECONDS = 1.0
MAX_CONCURRENT_NODES = 8
#: Bounded re-runs of one pure workflow-state callback after a concurrent writer won the CAS.
_STATE_CONFLICT_ATTEMPTS = 5
#: Activation drain poll while a driver yields its dispatch slot (outside every invoke).
_ACTIVATION_YIELD_SECONDS = 0.02
#: This plugin's owner-state contract; the version is also the payload version.
WORKFLOW_STATE_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "required": ["schema_version", "runs"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "runs": {"type": "object", "additionalProperties": {"type": "object"}},
        },
    }
)
_NO_CONFIG: Mapping[str, str] = MappingProxyType({})


RunStatus = Literal["running", "paused", "parked", "completed", "failed", "cancelled"]
NodeStatus = Literal["pending", "running", "waiting", "interrupted", "completed", "failed", "cancelled", "skipped"]

_RUN_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "session_id",
        "workflow",
        "source_path",
        "source_sha256",
        "task",
        "input_sha256",
        "notify_on_completion",
        "status",
        "error",
        "action_id",
        "nodes",
        "result",
        "result_sha256",
        "result_bytes",
        "created_at",
        "updated_at",
    }
)
_NODE_KEYS = frozenset(
    {
        "key",
        "status",
        "depends_on",
        "agent",
        "child_session_id",
        "run_id",
        "effective_input_sha256",
        "output",
        "output_sha256",
        "output_bytes",
        "output_preview",
        "output_error",
        "history_error",
        "error",
        "budget_exhaustion",
        "result_schema",
        "data",
        "parse_error",
    }
)
_NODE_REQUIRED_KEYS = _NODE_KEYS - {"budget_exhaustion", "output_preview", "output_error", "history_error", "result_schema", "data", "parse_error"}
_RUN_STATUSES: frozenset[str] = frozenset({"running", "paused", "parked", "completed", "failed", "cancelled"})
_NODE_STATUSES: frozenset[str] = frozenset({"pending", "running", "waiting", "interrupted", "completed", "failed", "cancelled", "skipped"})
_TERMINAL_RUN: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
