"""Stable facade for the scripted-workflow plugin.

The implementation is split into cohesive modules: ``constants``, ``config``,
``state``, ``source``, ``notices``, ``artifacts``, ``nodes``, ``driver``,
``tool``, ``commands``, ``views``, and ``lifecycle``. Modules depend only
downward (nodes -> artifacts/state; driver -> nodes/notices/source; surfaces ->
the modules they render), and this facade is the single import surface for the
plugin factory, tests, and any historical caller.

No behavior lives here. Public names are listed in ``__all__``; private names
remain importable because the existing unit/integration suites exercise the
orchestration helpers directly and monkeypatch the module-level state store.
"""

from __future__ import annotations

# ruff: noqa: F401 - compatibility re-exports are the point of this facade.
from .artifacts import (
    _read_artifact_page,
    _read_full_artifact,
    _result_page,
    _run_result_page,
    _utf8_page,
)
from .commands import (
    WorkflowCancelCommand,
    WorkflowPauseCommand,
    WorkflowResumeCommand,
    WorkflowRunCommand,
    WorkflowStatusCommand,
    _run_receipt,
    _WorkflowRunInput,
)
from .config import parse_workflows_config
from .constants import (
    _ACTIVATION_YIELD_SECONDS,
    _NO_CONFIG,
    _NODE_KEYS,
    _NODE_REQUIRED_KEYS,
    _NODE_STATUSES,
    _RUN_KEYS,
    _RUN_STATUSES,
    _STATE_CONFLICT_ATTEMPTS,
    _TERMINAL_RUN,
    INLINE_OUTPUT_BYTES,
    JOIN_SLICE_SECONDS,
    MAX_CONCURRENT_NODES,
    RESULT_PAGE_BYTES,
    SCHEMA_VERSION,
    STATUS_NODE_LIMIT,
    WORKFLOW_STATE_SCHEMA,
    NodeStatus,
    RunStatus,
)
from .driver import (
    _cancel_children,
    _CompleteRun,
    _describe_source,
    _describe_task,
    _encode_result,
    _group_failed,
    _invoke_mark,
    _invoke_state,
    _load_script_for_action,
    _LoadFailed,
    _mark_run,
    _ReadSource,
    _refuse_live_driver,
    _settle_group,
    _split_group,
    _task_for_action,
    _TaskFailed,
    drive_workflow_action,
)
from .lifecycle import prepare_workflows_deactivation
from .nodes import (
    WorkflowContext,
    _AbandonedNode,
    _attach_node_child,
    _cancel_run_children,
    _cancellation_note,
    _CancelledNode,
    _check_topology,
    _checked_node_args,
    _ChildNotRecorded,
    _commit_lock_factory,
    _EffectivePlan,
    _finish_node,
    _FinishedNode,
    _FinishNode,
    _JoinNode,
    _node_semaphore_factory,
    _NodePlan,
    _park_node,
    _ParkedNode,
    _ParkNode,
    _PausedRun,
    _require_run,
    _resolve_effective_plan,
    _settled_open_nodes,
    _settled_structured,
    _shutdown_executor,
    _stage_pending_node,
    _StagedNode,
    _unavailable_output_message,
)
from .notices import _notify_park, _notify_terminal
from .source import WorkflowScript, load_workflow_script, snapshot_workflow_source
from .state import (
    _STATE_LOCKS,
    _STATE_LOCKS_GUARD,
    WorkflowNode,
    WorkflowRun,
    WorkflowState,
    _clock_ms,
    _digest,
    _is_node_status,
    _is_run_status,
    _node_optional_id,
    _node_optional_sha256,
    _node_optional_text,
    _reject_node,
    _run_sha256,
    _run_text,
    _state_lock,
    _store,
    _structured_parse_error_from_payload,
)
from .tool import (
    _WORKFLOW_RUN_DESCRIPTION,
    WorkflowRunBody,
    WorkflowRunTool,
    _parse_run_arguments,
    _run_row,
    _start_run,
    _StartedRun,
)
from .views import WorkflowRunFormView, WorkflowStatusView

__all__ = [
    "INLINE_OUTPUT_BYTES",
    "JOIN_SLICE_SECONDS",
    "MAX_CONCURRENT_NODES",
    "NodeStatus",
    "RESULT_PAGE_BYTES",
    "RunStatus",
    "SCHEMA_VERSION",
    "STATUS_NODE_LIMIT",
    "WORKFLOW_STATE_SCHEMA",
    "WorkflowCancelCommand",
    "WorkflowContext",
    "WorkflowNode",
    "WorkflowPauseCommand",
    "WorkflowResumeCommand",
    "WorkflowRun",
    "WorkflowRunBody",
    "WorkflowRunCommand",
    "WorkflowRunFormView",
    "WorkflowRunTool",
    "WorkflowScript",
    "WorkflowState",
    "WorkflowStatusCommand",
    "WorkflowStatusView",
    "drive_workflow_action",
    "load_workflow_script",
    "parse_workflows_config",
    "prepare_workflows_deactivation",
    "snapshot_workflow_source",
]
