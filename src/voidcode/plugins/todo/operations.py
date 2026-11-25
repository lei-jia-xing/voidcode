"""Typed todo operations and their single handler map."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from . import limits, text
from .identity import new_task_id
from .models import TERMINAL_STATUSES, TodoPhase, TodoStatus, TodoTask
from .state import parse_phases


class TodoOp(StrEnum):
    INIT = "init"
    START = "start"
    DONE = "done"
    DROP = "drop"
    BLOCK = "block"
    UNBLOCK = "unblock"
    APPEND = "append"
    RM = "rm"
    VIEW = "view"


#: Ops that only need a task/phase target and never create text.
TARGET_OPS: frozenset[TodoOp] = frozenset({TodoOp.START, TodoOp.DONE, TodoOp.DROP, TodoOp.BLOCK, TodoOp.UNBLOCK, TodoOp.RM})
#: Ops exposed as per-task operations in the interactive editor.
TASK_UPDATE_OPS: tuple[TodoOp, ...] = (TodoOp.START, TodoOp.DONE, TodoOp.DROP, TodoOp.BLOCK, TodoOp.UNBLOCK, TodoOp.RM)
#: Ops that explicitly replace or remove plan structure, so queued nudges are stale.
RESETTING_OPS: frozenset[TodoOp] = frozenset({TodoOp.INIT, TodoOp.RM})


@dataclass(frozen=True, slots=True)
class _TodoArgs:
    op: TodoOp
    list_: tuple[object, ...] | None = None
    task: str | None = None
    phase: str | None = None
    items: tuple[str, ...] | None = None
    reason: str | None = None


def _optional_str(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"todo {field} must be a string")
    return value


def _item_strings(raw_items: object) -> tuple[str, ...]:
    if not isinstance(raw_items, tuple):
        raise ValueError("todo items must be an array")
    items: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            raise ValueError("todo item must be a string")
        items.append(item)
    return tuple(items)


def parse_args(arguments: Mapping[str, object]) -> _TodoArgs:
    # Snapshot arguments: arrays arrive as tuples.
    raw_op = arguments.get("op")
    if not isinstance(raw_op, str):
        raise ValueError(f"todo op must be one of {', '.join(op.value for op in TodoOp)}")
    try:
        op = TodoOp(raw_op)
    except ValueError as exc:
        raise ValueError(f"todo op must be one of {', '.join(op.value for op in TodoOp)}") from exc
    raw_list = arguments.get("list")
    if raw_list is not None and not isinstance(raw_list, tuple):
        raise ValueError("todo list must be an array")
    raw_items = arguments.get("items")
    return _TodoArgs(
        op=op,
        list_=raw_list,
        task=_optional_str(arguments.get("task"), field="task"),
        phase=_optional_str(arguments.get("phase"), field="phase"),
        items=None if raw_items is None else _item_strings(raw_items),
        reason=_optional_str(arguments.get("reason"), field="reason"),
    )


def _normalize_in_progress(phases: list[TodoPhase]) -> None:
    active: TodoTask | None = None
    for phase in phases:
        for task in phase["tasks"]:
            if task["status"] != "in_progress":
                continue
            if active is None:
                active = task
            else:
                task["status"] = "pending"
    if active is not None:
        return
    for phase in phases:
        for task in phase["tasks"]:
            if task["status"] == "pending":
                task["status"] = "in_progress"
                return


def _task_locations(phases: list[TodoPhase], content: str) -> list[TodoTask]:
    return [task for phase in phases for task in phase["tasks"] if task["content"] == content]


def _phase_by_name(phases: list[TodoPhase], name: str) -> TodoPhase | None:
    return next((phase for phase in phases if phase["name"] == name), None)


def _target_tasks(
    phases: list[TodoPhase],
    *,
    task: str | None,
    phase: str | None,
) -> list[TodoTask]:
    """Resolve one explicit target; there is intentionally no implicit all-task scope."""
    if task is not None and phase is not None:
        raise ValueError("todo operation accepts either task or phase, not both")
    if task is not None:
        matches = _task_locations(phases, task)
        if not matches:
            raise ValueError(f'Task "{task}" not found')
        if len(matches) > 1:
            raise ValueError(f'Task "{task}" is not unique')
        return matches
    if phase is not None:
        target = _phase_by_name(phases, phase)
        if target is None:
            raise ValueError(f'Phase "{phase}" not found')
        return target["tasks"]
    raise ValueError("todo operation requires a task or phase target")


def _new_task(content: str, status: TodoStatus = "pending") -> TodoTask:
    text.require_text_bytes(content, field="task content", limit=limits.MAX_TODO_TASK_CONTENT_BYTES)
    return {"id": new_task_id(), "content": content, "status": status}


def _init_phases(args: _TodoArgs) -> list[TodoPhase]:
    if args.list_ is not None and args.items is not None:
        raise ValueError("init accepts list or items, not both")
    if args.list_ is None and (args.items is None or not args.items):
        raise ValueError("init requires a non-empty list or items")
    if args.list_ is not None:
        if len(args.list_) > limits.MAX_TODO_PHASES:
            raise ValueError(f"init permits at most {limits.MAX_TODO_PHASES} phases")
        phases: list[TodoPhase] = []
        for raw_phase in args.list_:
            if not isinstance(raw_phase, Mapping):
                raise ValueError("init phase must be an object")
            unknown = sorted(str(key) for key in raw_phase if key not in {"phase", "items"})
            if unknown:
                raise ValueError(f"init phase field '{unknown[0]}' is not supported")
            name = raw_phase.get("phase")
            raw_items = raw_phase.get("items")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("init phase name must be non-empty")
            if not isinstance(raw_items, (list, tuple)) or not raw_items:
                raise ValueError(f'init phase "{name.strip()}" requires items')
            if len(raw_items) > limits.MAX_TODO_TASKS_PER_PHASE:
                raise ValueError(f'init phase "{name.strip()}" permits at most {limits.MAX_TODO_TASKS_PER_PHASE} tasks')
            tasks: list[TodoTask] = []
            for item in raw_items:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("todo item must be a non-empty string")
                tasks.append(_new_task(item.strip()))
            phases.append({"name": name.strip(), "tasks": tasks})
        return list(parse_phases(phases))
    assert args.items is not None
    phase_name = (args.phase or "Tasks").strip()
    if not phase_name:
        raise ValueError("init phase name must be non-empty")
    if len(args.items) > limits.MAX_TODO_TASKS_PER_PHASE:
        raise ValueError(f"init permits at most {limits.MAX_TODO_TASKS_PER_PHASE} tasks per phase")
    tasks = [_new_task(item.strip()) for item in args.items if isinstance(item, str) and item.strip()]
    if len(tasks) != len(args.items):
        raise ValueError("todo item must be a non-empty string")
    return list(parse_phases([{"name": phase_name, "tasks": tasks}]))


def _op_init(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    phases[:] = _init_phases(args)


def _op_view(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    _ = (args, phases)


def _op_start(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    task_name = args.task.strip() if args.task is not None else None
    if task_name is None:
        raise ValueError("start requires task")
    target = _target_tasks(phases, task=task_name, phase=None)[0]
    status = target["status"]
    if status == "blocked":
        raise ValueError(f'Task "{task_name}" is blocked; unblock it before starting')
    if status in TERMINAL_STATUSES:
        raise ValueError(f'Task "{task_name}" is already {status} and cannot be started')
    for phase in phases:
        for task in phase["tasks"]:
            if task is not target and task["status"] == "in_progress":
                task["status"] = "pending"
    target["status"] = "in_progress"
    target.pop("blocker", None)


def _terminal_transition(args: _TodoArgs, phases: list[TodoPhase], *, desired: TodoStatus) -> None:
    for target in _target_tasks(phases, task=args.task, phase=args.phase):
        status = target["status"]
        if status == desired:
            continue
        if status in TERMINAL_STATUSES:
            raise ValueError(f'Task "{target["content"]}" is already {status} and cannot become {desired}')
        target["status"] = desired
        target.pop("blocker", None)


def _op_done(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    _terminal_transition(args, phases, desired="completed")


def _op_drop(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    _terminal_transition(args, phases, desired="abandoned")


def _op_block(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    targets = _target_tasks(phases, task=args.task, phase=args.phase)
    reason = " ".join((args.reason or "").split())
    text.require_text_bytes(reason, field="task blocker", limit=limits.MAX_TODO_BLOCKER_BYTES)
    for target in targets:
        if target["status"] in {"pending", "in_progress", "blocked"}:
            target["status"] = "blocked"
            if reason:
                target["blocker"] = reason
            else:
                target.pop("blocker", None)


def _op_unblock(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    for target in _target_tasks(phases, task=args.task, phase=args.phase):
        if target["status"] == "blocked":
            target["status"] = "pending"
            target.pop("blocker", None)


def _op_rm(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    if args.task is not None and args.phase is not None:
        raise ValueError("rm accepts either task or phase, not both")
    if args.task is not None:
        target = _target_tasks(phases, task=args.task, phase=None)[0]
        for phase in phases:
            phase["tasks"] = [item for item in phase["tasks"] if item is not target]
        return
    if args.phase is not None:
        _target_tasks(phases, task=None, phase=args.phase)
        target_phase = _phase_by_name(phases, args.phase)
        assert target_phase is not None
        target_phase["tasks"] = []
        return
    raise ValueError("rm requires a task or phase target")


def _op_append(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    if args.phase is None or not args.phase.strip():
        raise ValueError("append requires phase")
    phase_name = args.phase.strip()
    text.require_text_bytes(phase_name, field="phase name", limit=limits.MAX_TODO_PHASE_NAME_BYTES)
    if args.items is None or not args.items:
        raise ValueError("append requires non-empty items")
    target = _phase_by_name(phases, phase_name)
    if target is None and len(phases) >= limits.MAX_TODO_PHASES:
        raise ValueError(f"todo state permits at most {limits.MAX_TODO_PHASES} phases")
    if target is not None and len(target["tasks"]) + len(args.items) > limits.MAX_TODO_TASKS_PER_PHASE:
        raise ValueError(f'Phase "{phase_name}" permits at most {limits.MAX_TODO_TASKS_PER_PHASE} tasks')
    current_tasks = sum(len(phase["tasks"]) for phase in phases)
    if current_tasks + len(args.items) > limits.MAX_TODO_TASKS:
        raise ValueError(f"todo state permits at most {limits.MAX_TODO_TASKS} tasks")
    new_tasks: list[TodoTask] = []
    for item in args.items:
        content = item.strip()
        if not content:
            raise ValueError("todo item must be a non-empty string")
        new_tasks.append(_new_task(content))
    if target is None:
        new_phase: TodoPhase = {"name": phase_name, "tasks": []}
        phases.append(new_phase)
        target = new_phase
    target["tasks"].extend(new_tasks)


_TODO_HANDLERS: Mapping[TodoOp, Callable[[_TodoArgs, list[TodoPhase]], None]] = {
    TodoOp.INIT: _op_init,
    TodoOp.START: _op_start,
    TodoOp.DONE: _op_done,
    TodoOp.DROP: _op_drop,
    TodoOp.BLOCK: _op_block,
    TodoOp.UNBLOCK: _op_unblock,
    TodoOp.APPEND: _op_append,
    TodoOp.RM: _op_rm,
    TodoOp.VIEW: _op_view,
}
if set(_TODO_HANDLERS) != set(TodoOp):
    raise RuntimeError("todo operation handlers must cover every TodoOp")


def apply(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    _TODO_HANDLERS[args.op](args, phases)
    if args.op is not TodoOp.VIEW:
        _normalize_in_progress(phases)
