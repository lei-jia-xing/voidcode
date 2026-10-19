"""Delegation plugin: parent-owned child sessions behind the ``task`` tools.

Ported surface (``legacy tools/delegation``): spawning one child session, reading and cancelling a
child this session started, and dispatching bounded independent work as one background group. The
model-facing text keeps the legacy ``task.txt``/``task_batch.txt`` intent for the operations that
exist here.

Legacy features are deliberately absent because the kernel owns no primitive for them yet, and
nothing here fakes them: parent roster listing (``operation=ps``), steering (``operation=steer``),
keep-alive tasks, polling/sleep loops, automatic retry, whole-group cancellation, completion
reminders, ``outputSchema``/``schemaMode`` structured-output validation, ``load_skills``,
``full_session``/``message_limit`` transcript reads. Scheduling a child and awaiting it with
``operation=output`` is the whole control surface.

``subagent_type`` selects one of the builtin child presets in ``presets.py``: the child's prompt is
the preset's ``prompt_body`` followed by this call's prompt, its tools are the preset's allowlist,
and its stance is the preset's ``readonly``. The kernel has no per-child system prompt seam -- a
child receives exactly one user prompt -- so the profile text is materialized into that prompt
rather than sent as a separate system message. An allowlist the active revision does not admit (its
owning plugin can be disabled in a workspace) falls back once to the parent tool scope, with the
downgrade visible in the result; a preset model preference is reported as a note because children
always run the workspace model.

Child runs, depth, the chain's spawn budget, tool scope and termination stay kernel-owned; this
plugin only records which children its own session started, under which label and group.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar
from uuid import uuid4

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import ChildOutcome, PluginServices
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .presets import PRESET_IDS, ChildPreset, get_preset

SCHEMA_VERSION = 1
DEFAULT_MAX_STEPS = 32
DEFAULT_JOIN_TIMEOUT_MS = 300_000
DEFAULT_JOIN_TIMEOUT_SECONDS = DEFAULT_JOIN_TIMEOUT_MS / 1000.0
MAX_BATCH_SIZE = 100
CANCEL_REASON = "parent cancellation via task tool"

_TASK_OPERATIONS: tuple[str, ...] = ("output", "cancel")
_BATCH_OPERATIONS: tuple[str, ...] = ("output",)
_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "children", "groups"})
_CHILD_KEYS: frozenset[str] = frozenset({"label", "group_id"})
#: Optional child fields: absent in rows written before preset selection existed.
_CHILD_OPTIONAL_KEYS: frozenset[str] = frozenset({"subagent_type"})
_ITEM_KEYS: frozenset[str] = frozenset({"prompt", "label", "tool_allowlist", "max_steps", "subagent_type"})


# --- Durable session state ---


@dataclass(frozen=True, slots=True)
class ChildRecord:
    """One child session this session started."""

    label: str = ""
    group_id: str | None = None
    subagent_type: str | None = None

    @classmethod
    def parse(cls, child_id: str, raw: object) -> ChildRecord:
        if not isinstance(raw, Mapping):
            raise ValueError(f"delegation child {child_id!r} must be an object")
        allowed = _CHILD_KEYS | _CHILD_OPTIONAL_KEYS
        unknown = sorted(str(key) for key in raw if key not in allowed)
        if unknown:
            raise ValueError(f"delegation child {child_id!r} field '{unknown[0]}' is not supported")
        missing = sorted(_CHILD_KEYS - raw.keys())
        if missing:
            raise ValueError(f"delegation child {child_id!r} is missing field(s): " + ", ".join(missing))
        label = raw["label"]
        if not isinstance(label, str):
            raise ValueError(f"delegation child {child_id!r} label must be a string")
        group_id = raw["group_id"]
        if group_id is not None and (not isinstance(group_id, str) or not group_id):
            raise ValueError(f"delegation child {child_id!r} group_id must be a non-empty string or null")
        subagent_type = raw.get("subagent_type")
        if subagent_type is not None and (not isinstance(subagent_type, str) or not subagent_type):
            raise ValueError(f"delegation child {child_id!r} subagent_type must be a non-empty string or null")
        return cls(label=label, group_id=group_id, subagent_type=subagent_type)

    def as_payload(self) -> dict[str, object]:
        return {"label": self.label, "group_id": self.group_id, "subagent_type": self.subagent_type}


@dataclass(frozen=True, slots=True)
class DelegationState:
    """This plugin's session-scope state: the children it started and their groups."""

    children: Mapping[str, ChildRecord] = field(default_factory=dict)
    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, raw: Mapping[str, object] | None) -> DelegationState:
        if raw is None:
            return cls()
        unknown = sorted(str(key) for key in raw if key not in _STATE_KEYS)
        if unknown:
            raise ValueError(f"delegation state field '{unknown[0]}' is not supported")
        missing = sorted(_STATE_KEYS - raw.keys())
        if missing:
            raise ValueError("delegation state is missing field(s): " + ", ".join(missing))
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"delegation state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        raw_children = raw["children"]
        if not isinstance(raw_children, Mapping):
            raise ValueError("delegation state requires a children object")
        children: dict[str, ChildRecord] = {}
        for child_id, record in raw_children.items():
            if not isinstance(child_id, str) or not child_id:
                raise ValueError("delegation state child ids must be non-empty strings")
            children[child_id] = ChildRecord.parse(child_id, record)
        raw_groups = raw["groups"]
        if not isinstance(raw_groups, Mapping):
            raise ValueError("delegation state requires a groups object")
        groups: dict[str, tuple[str, ...]] = {}
        for group_id, members_raw in raw_groups.items():
            if not isinstance(group_id, str) or not group_id:
                raise ValueError("delegation state group ids must be non-empty strings")
            if not isinstance(members_raw, (list, tuple)):
                raise ValueError(f"delegation state group {group_id!r} requires an array of child ids")
            members: list[str] = []
            for member in members_raw:
                if not isinstance(member, str) or not member:
                    raise ValueError(f"delegation state group {group_id!r} members must be non-empty child ids")
                if member not in children:
                    raise ValueError(f"delegation state group {group_id!r} references unknown child {member!r}")
                if member in members:
                    raise ValueError(f"delegation state group {group_id!r} lists child {member!r} twice")
                if children[member].group_id != group_id:
                    raise ValueError(f"delegation state group {group_id!r} lists child {member!r} that belongs to another group")
                members.append(member)
            groups[group_id] = tuple(members)
        for child_id, record in children.items():
            if record.group_id is not None and child_id not in groups.get(record.group_id, ()):
                raise ValueError(f"delegation child {child_id!r} is not listed in its group {record.group_id!r}")
        return cls(children=children, groups=groups)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "children": {child_id: record.as_payload() for child_id, record in self.children.items()},
            "groups": {group_id: list(members) for group_id, members in self.groups.items()},
        }


def _load_state(services: PluginServices) -> tuple[DelegationState, int]:
    state_data, version = services.get_state("session")
    return DelegationState.from_payload(state_data), version


def _with_children(state: DelegationState, created: Sequence[tuple[str, ChildRecord]], *, group_id: str | None) -> DelegationState:
    """Record freshly spawned children, as one group when the caller owns one."""
    children = {**state.children, **dict(created)}
    groups = dict(state.groups)
    if group_id is not None:
        groups[group_id] = tuple(child_id for child_id, _record in created)
    return DelegationState(children=children, groups=groups)


# --- Argument parsing (the kernel schema stays the first gate; these are second-line messages) ---


@dataclass(frozen=True, slots=True)
class _TaskArgs:
    prompt: str | None = None
    label: str = ""
    tool_allowlist: tuple[str, ...] | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    run_in_background: bool = True
    operation: str | None = None
    task_id: str | None = None
    timeout_seconds: float = DEFAULT_JOIN_TIMEOUT_SECONDS
    subagent_type: str | None = None


@dataclass(frozen=True, slots=True)
class _BatchItem:
    prompt: str
    label: str = ""
    tool_allowlist: tuple[str, ...] | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    subagent_type: str | None = None


@dataclass(frozen=True, slots=True)
class _BatchArgs:
    tasks: tuple[_BatchItem, ...] = ()
    operation: str | None = None
    parallel_group_id: str | None = None


def _parse_label(raw: object, *, where: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{where} label must be a string")
    return raw


def _parse_allowlist(raw: object, *, where: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"{where} tool_allowlist must be a non-empty array of tool names")
    names: list[str] = []
    for name in raw:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where} tool_allowlist entries must be non-empty tool names")
        names.append(name)
    return tuple(names)


def _parse_subagent_type(raw: object, *, where: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in PRESET_IDS:
        raise ValueError(f"{where} subagent_type must be one of: {', '.join(PRESET_IDS)}")
    return raw


def _parse_preset_selection(
    raw_subagent_type: object,
    raw_allowlist: object,
    *,
    where: str,
) -> tuple[str | None, tuple[str, ...] | None]:
    """Parse an optional preset id, which is mutually exclusive with an explicit allowlist."""
    subagent_type = _parse_subagent_type(raw_subagent_type, where=where)
    tool_allowlist = _parse_allowlist(raw_allowlist, where=where)
    if subagent_type is not None and tool_allowlist is not None:
        raise ValueError(f"{where} accepts either subagent_type or tool_allowlist, not both: a preset defines the child scope")
    return subagent_type, tool_allowlist


def _parse_max_steps(raw: object, *, where: str) -> int:
    if raw is None:
        return DEFAULT_MAX_STEPS
    if type(raw) is not int or raw < 1:
        raise ValueError(f"{where} max_steps must be a positive integer")
    return raw


def _parse_timeout_seconds(raw: object, *, where: str) -> float:
    if raw is None:
        return DEFAULT_JOIN_TIMEOUT_SECONDS
    if type(raw) is not int or raw < 0:
        raise ValueError(f"{where} timeout must be a non-negative integer of milliseconds")
    return raw / 1000.0


def _parse_task_args(arguments: Mapping[str, object]) -> _TaskArgs:
    # Arguments arrive as an immutable JSON snapshot: arrays are tuples here.
    prompt = arguments.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ValueError("task prompt must be a string")
    if prompt is not None and not prompt.strip():
        raise ValueError("task prompt must be non-empty")
    operation = arguments.get("operation")
    if operation is not None and (not isinstance(operation, str) or operation not in _TASK_OPERATIONS):
        raise ValueError(f"task operation must be one of {', '.join(_TASK_OPERATIONS)}")
    task_id = arguments.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id):
        raise ValueError("task task_id must be a non-empty string")
    if prompt is not None and (operation is not None or task_id is not None):
        raise ValueError("task accepts one mode per call: a prompt to spawn a child, or operation=output/cancel with a task_id")
    if operation is None and task_id is not None:
        raise ValueError("task task_id requires operation=output or operation=cancel")
    if operation is not None and task_id is None:
        raise ValueError(f"task operation={operation} requires a task_id")
    if operation is None and prompt is None:
        raise ValueError("task requires a prompt to spawn a child, or operation=output/cancel with a task_id")
    run_in_background = arguments.get("run_in_background")
    if run_in_background is None:
        run_in_background = True
    elif type(run_in_background) is not bool:
        raise ValueError("task run_in_background must be a boolean")
    subagent_type, tool_allowlist = _parse_preset_selection(
        arguments.get("subagent_type"),
        arguments.get("tool_allowlist"),
        where="task",
    )
    if operation is not None and subagent_type is not None:
        raise ValueError("task subagent_type selects a child profile and applies to a spawn only")
    return _TaskArgs(
        prompt=prompt,
        label=_parse_label(arguments.get("label"), where="task"),
        tool_allowlist=tool_allowlist,
        max_steps=_parse_max_steps(arguments.get("max_steps"), where="task"),
        run_in_background=run_in_background,
        operation=operation,
        task_id=task_id,
        timeout_seconds=_parse_timeout_seconds(arguments.get("timeout"), where="task"),
        subagent_type=subagent_type,
    )


def _parse_batch_item(index: int, raw: object) -> _BatchItem:
    where = f"task_batch item {index}"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(str(key) for key in raw if key not in _ITEM_KEYS)
    if unknown:
        raise ValueError(f"{where} field '{unknown[0]}' is not supported")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{where} requires a non-empty prompt")
    subagent_type, tool_allowlist = _parse_preset_selection(
        raw.get("subagent_type"),
        raw.get("tool_allowlist"),
        where=where,
    )
    return _BatchItem(
        prompt=prompt,
        label=_parse_label(raw.get("label"), where=where),
        tool_allowlist=tool_allowlist,
        max_steps=_parse_max_steps(raw.get("max_steps"), where=where),
        subagent_type=subagent_type,
    )


def _parse_batch_args(arguments: Mapping[str, object]) -> _BatchArgs:
    operation = arguments.get("operation")
    if operation is not None and (not isinstance(operation, str) or operation not in _BATCH_OPERATIONS):
        raise ValueError(f"task_batch operation must be one of {', '.join(_BATCH_OPERATIONS)}")
    raw_tasks = arguments.get("tasks")
    group_id = arguments.get("parallel_group_id")
    if group_id is not None and (not isinstance(group_id, str) or not group_id):
        raise ValueError("task_batch parallel_group_id must be a non-empty string")
    if operation is None:
        if raw_tasks is None:
            raise ValueError("task_batch requires tasks to spawn a group, or operation=output with a parallel_group_id")
        if group_id is not None:
            raise ValueError("task_batch accepts one mode per call: tasks to spawn a group, or operation=output with a parallel_group_id")
    else:
        if raw_tasks is not None:
            raise ValueError("task_batch accepts one mode per call: tasks to spawn a group, or operation=output with a parallel_group_id")
        if group_id is None:
            raise ValueError(f"task_batch operation={operation} requires a parallel_group_id")
        return _BatchArgs(operation=operation, parallel_group_id=group_id)
    if not isinstance(raw_tasks, (list, tuple)):
        raise ValueError("task_batch tasks must be an array")
    if len(raw_tasks) > MAX_BATCH_SIZE:
        raise ValueError(f"task_batch accepts at most {MAX_BATCH_SIZE} tasks, got {len(raw_tasks)}")
    if not raw_tasks:
        raise ValueError("task_batch requires at least one task")
    return _BatchArgs(tasks=tuple(_parse_batch_item(index, item) for index, item in enumerate(raw_tasks)))


# --- Result bodies ---


@dataclass(frozen=True, slots=True)
class TaskBody:
    """One ``task`` outcome: a spawn, a join, or a cancellation request."""

    operation: str
    task_id: str | None = None
    label: str | None = None
    status: str | None = None
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None
    cancelled: bool | None = None
    subagent_type: str | None = None
    #: Visible caveats about how the child actually ran (preset scope or model downgrade).
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"operation": self.operation}
        for key in ("task_id", "status", "output", "error", "stop_cause", "cancelled", "subagent_type"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.label:
            payload["label"] = self.label
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


@dataclass(frozen=True, slots=True)
class _BatchChild:
    """One spawned batch item: where it went, the preset it ran under, and how it was downgraded."""

    index: int
    task_id: str
    subagent_type: str | None = None
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"index": self.index, "task_id": self.task_id}
        if self.subagent_type is not None:
            payload["subagent_type"] = self.subagent_type
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


@dataclass(frozen=True, slots=True)
class BatchBody:
    """One ``task_batch`` outcome: a group spawn (possibly partial) or a group read."""

    operation: str
    parallel_group_id: str
    task_ids: tuple[str, ...] = ()
    partial: bool = False
    created: tuple[dict[str, object], ...] = ()
    failed: tuple[dict[str, object], ...] = ()
    children: tuple[dict[str, object], ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"operation": self.operation, "parallel_group_id": self.parallel_group_id}
        if self.operation == "spawn":
            payload["task_ids"] = list(self.task_ids)
            payload["partial"] = self.partial
            if self.partial:
                payload["created"] = [dict(entry) for entry in self.created]
                payload["failed"] = [dict(entry) for entry in self.failed]
        else:
            payload["children"] = [dict(entry) for entry in self.children]
        return payload


# --- Rendering ---


def _header(task_id: str, label: str, subagent_type: str | None = None) -> str:
    header = f"task {task_id}"
    if subagent_type:
        header += f" preset={subagent_type}"
    return header + (f' label="{label}"' if label else "")


def _with_notes(text: str, notes: Sequence[str]) -> str:
    """Append the caveats about how a child actually ran, without hiding them in the body."""
    return "\n".join([text, *(f"note: {note}" for note in notes)])


def _preset_notes(preset: ChildPreset | None) -> tuple[str, ...]:
    """The visible caveats a preset adds: a model preference the kernel cannot honor."""
    if preset is None or preset.model_preference is None:
        return ()
    return (
        f"preset '{preset.id}' prefers model '{preset.model_preference}', but every child runs the workspace model; the preference was not applied",
    )


def _spawn_child(
    services: PluginServices,
    *,
    prompt: str,
    label: str,
    max_steps: int,
    preset: ChildPreset | None,
    tool_allowlist: tuple[str, ...] | None,
) -> tuple[str, tuple[str, ...]]:
    """Spawn one child, reporting the caveats that shaped how it runs.

    A preset materializes the child prompt as its profile text plus this call's prompt (the kernel
    has no per-child system prompt seam: a child receives exactly one user prompt) and passes the
    preset's allowlist and stance. An allowlist the active revision does not admit falls back once
    to the parent tool scope, with the downgrade visible in the result, so a disabled owning plugin
    degrades the child instead of failing the spawn.

    The kernel validates the requested scope before it creates anything, and its refusal reaches
    this process as a ``ValueError`` locally or as the host transport's wrapping ``RuntimeError``:
    both mean the same thing here, and a rejected spawn leaves no child behind to retry onto.
    """
    body = prompt if preset is None else f"{preset.prompt_body}\n\n{prompt}"
    allowlist = tool_allowlist if preset is None else preset.tool_allowlist
    readonly = False if preset is None else preset.readonly
    notes = _preset_notes(preset)
    try:
        child_id = services.spawn_child(body, label=label, tool_allowlist=allowlist, max_steps=max_steps, readonly=readonly)
    except (ValueError, RuntimeError) as exc:
        if preset is None:
            raise
        child_id = services.spawn_child(body, label=label, tool_allowlist=None, max_steps=max_steps, readonly=readonly)
        return child_id, (*notes, f"preset '{preset.id}' scope was unavailable ({exc}); the child inherited the parent tool scope instead")
    return child_id, notes


def _failure(tool_name: str, message: str) -> ToolResult:
    return ToolFailure(tool_name=tool_name, error=message, output=TextOutput(text=message))


def _outcome_text(task_id: str, label: str, outcome: ChildOutcome, subagent_type: str | None = None) -> str:
    lines = [f"{_header(task_id, label, subagent_type)} status={outcome.status}"]
    if outcome.stop_cause:
        lines.append(f"stop_cause: {outcome.stop_cause}")
    if outcome.error:
        lines.append(f"error: {outcome.error}")
    if outcome.output:
        lines.append(outcome.output)
    if outcome.status == "running":
        lines.append(f'Still running: await it with task(operation="output", task_id="{task_id}").')
    return "\n".join(lines)


def _batch_text(group_id: str, created: Sequence[_BatchChild], failed: Sequence[tuple[int, str]]) -> str:
    total = len(created) + len(failed)
    if failed:
        header = f"Started {len(created)} of {total} child task(s) in parallel group {group_id}; {len(failed)} item(s) failed."
    else:
        header = f"Started {len(created)} child task(s) in parallel group {group_id}."
    lines = [header]
    for entry in created:
        lines.append(f"- [{entry.index}] {entry.task_id}" + (f" preset={entry.subagent_type}" if entry.subagent_type else ""))
        lines.extend(f"- [{entry.index}] note: {note}" for note in entry.notes)
    lines.extend(f"- [{index}] failed: {message}" for index, message in failed)
    return "\n".join(lines)


def _group_text(group_id: str, entries: Sequence[Mapping[str, object]]) -> str:
    lines = [f"Parallel group {group_id}: {len(entries)} child task(s)."]
    for entry in entries:
        line = f"- {entry['task_id']} status={entry['status']}"
        if entry.get("subagent_type"):
            line += f" preset={entry['subagent_type']}"
        if entry.get("stop_cause"):
            line += f" stop_cause={entry['stop_cause']}"
        if entry.get("error"):
            line += f" error={entry['error']}"
        lines.append(line)
    return "\n".join(lines)


# --- Tools ---


TASK_DESCRIPTION = (
    "Delegate work to a child session, or read and cancel a child this session started.\n"
    "\n"
    "Spawn usage:\n"
    "- Pass prompt with the full delegated task text; label is an optional short tag for the child.\n"
    "- subagent_type runs the child under a builtin profile: execute, review, explore, research, or "
    "plan. The child's prompt becomes that profile's text plus your prompt, its tools become the "
    "profile's allowlist, and a read-only profile keeps the child read-only.\n"
    "- tool_allowlist narrows the child's tools and can never widen your own scope; pass it without "
    "subagent_type.\n"
    "- max_steps caps the child's steps (default 32).\n"
    "- run_in_background=true (default) starts the child and returns its task_id immediately. "
    "false spawns and waits, returning the child's status and output in this same call.\n"
    "- Depth, the chain's spawn budget, and your own tool scope always bound the child.\n"
    "\n"
    "Control usage:\n"
    '- operation="output" with task_id joins that child once and returns its status, output, error, '
    'and stop_cause. timeout bounds the wait in milliseconds (default 300000); status "running" '
    "means the child outlived it and is still working.\n"
    '- operation="cancel" with task_id requests cancellation of a child this session started.\n'
    "\n"
    'Scheduling a child and awaiting it with operation="output" is the whole control surface: there '
    "is no roster listing, steering, keep-alive, retry, or structured-output validation. Do not "
    "sleep, poll in a loop, or relaunch equivalent work."
)


TASK_BATCH_DESCRIPTION = (
    "Dispatch bounded independent child work as one background group.\n"
    "\n"
    "Usage:\n"
    "- Pass tasks with 1-100 items. Every item requires prompt and accepts optional label, "
    "tool_allowlist, max_steps, and subagent_type (a builtin child profile: execute, review, "
    "explore, research, or plan).\n"
    "- Every item is validated before anything is created; when all items are valid they all start "
    "in the background under one fresh group id.\n"
    "- A result with partial=true lists the created and the failed items. Created children stay "
    "recorded and are never retried or cancelled automatically.\n"
    "- Items must be independent: nested delegation, dependencies, keep-alive, and automatic "
    "whole-group cancellation are not supported.\n"
    '- Read the group with operation="output" and parallel_group_id: each child is joined once and '
    "reported as a bounded per-child status, without prompts, transcripts, or child output.\n"
    "\n"
    'Example: task_batch(tasks=[{"prompt":"Find the opening hours in the visitor guide"},'
    '{"prompt":"Summarize transport options from the visitor guide"}])'
)


class TaskTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="task",
        description=TASK_DESCRIPTION,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {"type": "string", "minLength": 1, "description": "Full delegated task prompt for the child session."},
                "label": {"type": "string", "description": "Optional short delegation label."},
                "subagent_type": {
                    "type": "string",
                    "enum": list(PRESET_IDS),
                    "description": (
                        "Optional builtin child profile; the child runs that profile's prompt text, tools, "
                        "and read-only stance. Omit to inherit your own tool scope instead."
                    ),
                },
                "tool_allowlist": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Optional tool names the child may use; never wider than your own scope.",
                },
                "max_steps": {"type": "integer", "minimum": 1, "default": DEFAULT_MAX_STEPS, "description": "Step cap for the child session."},
                "run_in_background": {
                    "type": "boolean",
                    "default": True,
                    "description": "true returns the task_id immediately; false waits for the child to finish.",
                },
                "operation": {
                    "type": "string",
                    "enum": list(_TASK_OPERATIONS),
                    "description": "output joins a child this session started; cancel requests its cancellation.",
                },
                "task_id": {"type": "string", "minLength": 1, "description": "Child session id returned by a spawn."},
                "timeout": {
                    "type": "integer",
                    "minimum": 0,
                    "default": DEFAULT_JOIN_TIMEOUT_MS,
                    "description": "Millisecond wait bound for a synchronous spawn or operation=output.",
                },
            },
            "required": [],
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.SPAWN, ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        context.require_session_id()
        try:
            args = _parse_task_args(call.arguments)
            state, version = _load_state(services)
        except ValueError as exc:
            return _failure(self.definition.name, str(exc))
        if args.operation is not None:
            return self._control(args, state, services)
        return self._spawn(args, state, version, services)

    def _spawn(self, args: _TaskArgs, state: DelegationState, version: int, services: PluginServices) -> ToolResult:
        prompt = args.prompt or ""
        preset = get_preset(args.subagent_type) if args.subagent_type is not None else None
        try:
            child_id, notes = _spawn_child(
                services,
                prompt=prompt,
                label=args.label,
                max_steps=args.max_steps,
                preset=preset,
                tool_allowlist=args.tool_allowlist,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            return _failure(self.definition.name, str(exc))
        services.replace_state(
            _with_children(
                state,
                ((child_id, ChildRecord(label=args.label, subagent_type=args.subagent_type)),),
                group_id=None,
            ).to_payload(),
            expected_version=version,
        )
        services.stage_event("task_spawned", {"task_id": child_id, "label": args.label, "group_id": None})
        if args.run_in_background:
            text = (
                f"{_header(child_id, args.label, args.subagent_type)} started in the background.\n"
                f'Await it with task(operation="output", task_id="{child_id}").'
            )
            return ToolSuccess(
                self.definition.name,
                TextOutput(_with_notes(text, notes)),
                body=TaskBody(operation="spawn", task_id=child_id, label=args.label, subagent_type=args.subagent_type, notes=notes),
            )
        try:
            outcome = services.join_child(child_id, timeout_seconds=args.timeout_seconds)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return _failure(self.definition.name, str(exc))
        return ToolSuccess(
            self.definition.name,
            TextOutput(_with_notes(_outcome_text(child_id, args.label, outcome, args.subagent_type), notes)),
            body=TaskBody(
                operation="spawn",
                task_id=child_id,
                label=args.label,
                status=outcome.status,
                output=outcome.output,
                error=outcome.error,
                stop_cause=outcome.stop_cause,
                subagent_type=args.subagent_type,
                notes=notes,
            ),
        )

    def _control(self, args: _TaskArgs, state: DelegationState, services: PluginServices) -> ToolResult:
        task_id = args.task_id or ""
        if task_id not in state.children:
            return _failure(self.definition.name, f"unknown task: {task_id}")
        record = state.children[task_id]
        label = record.label
        if args.operation == "cancel":
            try:
                cancelled = services.cancel_child(task_id, reason=CANCEL_REASON)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return _failure(self.definition.name, str(exc))
            if cancelled:
                text = f"{_header(task_id, label, record.subagent_type)} cancellation accepted."
            else:
                text = f"{_header(task_id, label, record.subagent_type)} cancellation not accepted: the child is neither executing nor parked."
            return ToolSuccess(
                self.definition.name,
                TextOutput(text),
                body=TaskBody(operation="cancel", task_id=task_id, label=label, cancelled=cancelled, subagent_type=record.subagent_type),
            )
        try:
            outcome = services.join_child(task_id, timeout_seconds=args.timeout_seconds)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return _failure(self.definition.name, str(exc))
        return ToolSuccess(
            self.definition.name,
            TextOutput(_outcome_text(task_id, label, outcome, record.subagent_type)),
            body=TaskBody(
                operation="output",
                task_id=task_id,
                label=label,
                status=outcome.status,
                output=outcome.output,
                error=outcome.error,
                stop_cause=outcome.stop_cause,
                subagent_type=record.subagent_type,
            ),
        )


class TaskBatchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="task_batch",
        description=TASK_BATCH_DESCRIPTION,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_BATCH_SIZE,
                    "description": "Independent child requests; all children start in the background under one fresh group.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "prompt": {"type": "string", "minLength": 1, "description": "Full delegated task prompt for this child session."},
                            "label": {"type": "string", "description": "Optional short delegation label."},
                            "subagent_type": {
                                "type": "string",
                                "enum": list(PRESET_IDS),
                                "description": (
                                    "Optional builtin child profile; this child runs that profile's prompt text, tools, and read-only stance."
                                ),
                            },
                            "tool_allowlist": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "minLength": 1},
                                "description": "Optional tool names this child may use; never wider than your own scope.",
                            },
                            "max_steps": {
                                "type": "integer",
                                "minimum": 1,
                                "default": DEFAULT_MAX_STEPS,
                                "description": "Step cap for this child session.",
                            },
                        },
                        "required": ["prompt"],
                    },
                },
                "operation": {
                    "type": "string",
                    "enum": list(_BATCH_OPERATIONS),
                    "description": "output reads a group this session started instead of spawning one.",
                },
                "parallel_group_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Group id returned by an earlier task_batch call.",
                },
            },
            "required": [],
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.SPAWN, ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        context.require_session_id()
        try:
            args = _parse_batch_args(call.arguments)
            state, version = _load_state(services)
        except ValueError as exc:
            return _failure(self.definition.name, str(exc))
        if args.operation is not None:
            return self._group_output(args.parallel_group_id or "", state, services)
        return self._spawn_group(args.tasks, state, version, services)

    def _spawn_group(
        self,
        items: Sequence[_BatchItem],
        state: DelegationState,
        version: int,
        services: PluginServices,
    ) -> ToolResult:
        group_id = uuid4().hex
        created: list[_BatchChild] = []
        failed: list[tuple[int, str]] = []
        for index, item in enumerate(items):
            preset = get_preset(item.subagent_type) if item.subagent_type is not None else None
            try:
                child_id, notes = _spawn_child(
                    services,
                    prompt=item.prompt,
                    label=item.label,
                    max_steps=item.max_steps,
                    preset=preset,
                    tool_allowlist=item.tool_allowlist,
                )
            except (ValueError, PermissionError, RuntimeError) as exc:
                failed.append((index, str(exc)))
                continue
            created.append(_BatchChild(index=index, task_id=child_id, subagent_type=item.subagent_type, notes=notes))
        if not created:
            return _failure(self.definition.name, f"task_batch created no child session: {failed[0][1]}")
        services.replace_state(
            _with_children(
                state,
                tuple(
                    (entry.task_id, ChildRecord(label=items[entry.index].label, group_id=group_id, subagent_type=entry.subagent_type))
                    for entry in created
                ),
                group_id=group_id,
            ).to_payload(),
            expected_version=version,
        )
        services.stage_event("task_batch_spawned", {"parallel_group_id": group_id, "task_ids": [entry.task_id for entry in created]})
        return ToolSuccess(
            self.definition.name,
            TextOutput(_batch_text(group_id, created, failed)),
            body=BatchBody(
                operation="spawn",
                parallel_group_id=group_id,
                task_ids=tuple(entry.task_id for entry in created),
                partial=bool(failed),
                created=tuple(entry.as_payload() for entry in created),
                failed=tuple({"index": index, "error": message} for index, message in failed),
            ),
        )

    def _group_output(self, group_id: str, state: DelegationState, services: PluginServices) -> ToolResult:
        members = state.groups.get(group_id)
        if members is None:
            return _failure(self.definition.name, f"unknown task group: {group_id}")
        children: list[dict[str, object]] = []
        for child_id in members:
            try:
                outcome = services.join_child(child_id, timeout_seconds=DEFAULT_JOIN_TIMEOUT_SECONDS)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return _failure(self.definition.name, str(exc))
            entry: dict[str, object] = {"task_id": child_id, "status": outcome.status}
            subagent_type = state.children[child_id].subagent_type
            if subagent_type:
                entry["subagent_type"] = subagent_type
            if outcome.stop_cause:
                entry["stop_cause"] = outcome.stop_cause
            if outcome.error:
                entry["error"] = outcome.error
            children.append(entry)
        return ToolSuccess(
            self.definition.name,
            TextOutput(_group_text(group_id, children)),
            body=BatchBody(operation="output", parallel_group_id=group_id, children=tuple(children)),
        )


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    """Plugin entrypoint registered in voidcode-plugin.json."""
    return Plugin(tools=(TaskTool(), TaskBatchTool()))
