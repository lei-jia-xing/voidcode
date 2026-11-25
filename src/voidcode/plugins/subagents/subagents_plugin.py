"""Stable facade for the subagents plugin.

The implementation is split into cohesive modules: ``constants``, ``uris``,
``state``, ``profiles.resolution``, ``args``, ``bodies``, ``child``, ``notices``,
``output``, ``rendering``, ``tools``, and ``lifecycle``. Modules depend only
downward, and this facade is the single import surface for plugin registration,
tests, and historical callers.

No orchestration lives here. Public names are listed in ``__all__``; private
names remain importable because the existing suites exercise helpers directly
and monkeypatch the module-level output cap. The cap stays a facade attribute:
``output`` reads it through a provider so the historical monkeypatch path still
controls the bounded structured-output read.
"""

from __future__ import annotations

# ruff: noqa: F401 - compatibility re-exports are the point of this facade.
from .args import (
    BatchArgsModel,
    TaskArgsModel,
    WaitArgsModel,
    _BatchItem,
    _merge_batch_prompt,
    _model_args,
    _validate_allowlist,
    _validate_batch_item,
    _validate_label,
    _validate_max_steps,
    _validate_result_schema,
    _validate_subagent_type,
)
from .bodies import BatchBody, TaskBody, WaitBody, _batch_body, _BatchChild, _failure, _spawn_body, _stopped_failure
from .child import _is_reportable_current, _join_child_abortable, _parent_stopped, _spawn_child
from .constants import (
    _BATCH_KEYS,
    _BATCH_OPERATIONS,
    _CHILD_KEYS,
    _CONTROL_INPUT_KEYS,
    _FOREGROUND_SPAWN_INPUT_KEYS,
    _ITEM_KEYS,
    _SPAWN_INPUT_KEYS,
    _STRUCTURED_READ_PAGE_BYTES,
    _TASK_FIELD_INPUT_NAMES,
    _TASK_INPUT_KEYS,
    _TASK_OPERATIONS,
    CANCEL_REASON,
    DEFAULT_HISTORY_EVENTS,
    DEFAULT_JOIN_TIMEOUT_MS,
    DEFAULT_JOIN_TIMEOUT_SECONDS,
    JOIN_SLICE_SECONDS,
    MAX_BATCH_SIZE,
    MAX_HISTORY_EVENTS,
    MAX_OUTPUT_PAGE_BYTES,
    MAX_STRUCTURED_OUTPUT_BYTES,
    OUTPUT_PAGE_BYTES,
    SCHEMA_VERSION,
)
from .lifecycle import prepare_subagents_deactivation
from .notices import _acknowledge, stage_batch_spawned, stage_task_spawned
from .output import (
    _artifact_output_facts,
    _configure_structured_output_cap,
    _full_output_text,
    _FullOutput,
    _output_facts,
    _OutputFacts,
    _resource_caller,
    _structured_facts,
    _StructuredFacts,
)
from .profiles.resolution import _load_profile, _load_registry, _resolve_subagent, agent_definition
from .rendering import (
    _batch_text,
    _budget_exhaustion_payload,
    _group_text,
    _header,
    _outcome_entry,
    _outcome_text,
    _pending_entries,
    _pending_text,
    _render_outcomes,
    _structured_line,
)
from .state import ChildRecord, DelegationState, _store, _with_children
from .tools import TASK_BATCH_DESCRIPTION, TASK_DESCRIPTION, TaskBatchTool, TaskTool, WaitTool
from .uris import _agent_uri, _artifact_uri, _transcript_uri

_configure_structured_output_cap(lambda: MAX_STRUCTURED_OUTPUT_BYTES)

__all__ = [
    "CANCEL_REASON",
    "DEFAULT_HISTORY_EVENTS",
    "DEFAULT_JOIN_TIMEOUT_MS",
    "DEFAULT_JOIN_TIMEOUT_SECONDS",
    "JOIN_SLICE_SECONDS",
    "MAX_BATCH_SIZE",
    "MAX_HISTORY_EVENTS",
    "MAX_OUTPUT_PAGE_BYTES",
    "MAX_STRUCTURED_OUTPUT_BYTES",
    "OUTPUT_PAGE_BYTES",
    "SCHEMA_VERSION",
    "TASK_BATCH_DESCRIPTION",
    "TASK_DESCRIPTION",
    "BatchArgsModel",
    "BatchBody",
    "ChildRecord",
    "DelegationState",
    "TaskArgsModel",
    "TaskBatchTool",
    "TaskBody",
    "TaskTool",
    "WaitArgsModel",
    "WaitBody",
    "WaitTool",
    "agent_definition",
    "prepare_subagents_deactivation",
    "stage_batch_spawned",
    "stage_task_spawned",
]
