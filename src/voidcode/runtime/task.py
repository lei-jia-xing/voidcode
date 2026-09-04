"""Compatibility exports for the split runtime task contracts.

New code should import routing, execution, and background-task models from their
focused modules directly. This module remains as a stable public import path.
"""

from .background_task_models import (
    BACKGROUND_TASK_TERMINAL_STATUSES,
    BackgroundTaskConcurrencyObservability,
    BackgroundTaskObservability,
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskRetryObservability,
    BackgroundTaskState,
    BackgroundTaskStatus,
    DelegatedReminderState,
    DelegatedReminderStopCondition,
    SchemaValidation,
    StoredBackgroundTaskSummary,
    is_background_task_terminal,
    is_background_task_transition_allowed,
    validate_background_task_id,
)
from .delegation_execution import (
    SubagentExecutionContract,
    SubagentExecutionCorrelation,
)
from .delegation_routing import (
    CALLABLE_SUBAGENT_PRESETS,
    ResolvedSubagentRoute,
    SubagentExecutablePreset,
    SubagentExecutionMode,
    SubagentRoutingIdentity,
    parse_subagent_routing_identity,
    resolve_subagent_route,
    subagent_routing_identity_from_metadata,
)

__all__ = [
    "BACKGROUND_TASK_TERMINAL_STATUSES",
    "BackgroundTaskConcurrencyObservability",
    "BackgroundTaskObservability",
    "BackgroundTaskRef",
    "BackgroundTaskRequestSnapshot",
    "BackgroundTaskRetryObservability",
    "BackgroundTaskState",
    "BackgroundTaskStatus",
    "DelegatedReminderState",
    "DelegatedReminderStopCondition",
    "SchemaValidation",
    "StoredBackgroundTaskSummary",
    "SubagentExecutionContract",
    "SubagentExecutionCorrelation",
    "CALLABLE_SUBAGENT_PRESETS",
    "ResolvedSubagentRoute",
    "SubagentExecutablePreset",
    "SubagentExecutionMode",
    "SubagentRoutingIdentity",
    "parse_subagent_routing_identity",
    "resolve_subagent_route",
    "subagent_routing_identity_from_metadata",
    "is_background_task_terminal",
    "is_background_task_transition_allowed",
    "validate_background_task_id",
]
