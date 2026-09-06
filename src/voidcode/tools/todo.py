from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context

TodoStatus = Literal["pending", "in_progress", "completed", "abandoned", "blocked"]
_TODO_STATUSES: tuple[TodoStatus, ...] = (
    "pending",
    "in_progress",
    "completed",
    "abandoned",
    "blocked",
)


class _TodoArgsModel(BaseModel):
    op: Literal["init", "start", "done", "drop", "block", "unblock", "append", "rm", "view"]
    model_config = ConfigDict(extra="forbid")
    list_: list[dict[str, object]] | None = Field(default=None, alias="list")
    task: str | None = None
    phase: str | None = None
    items: list[str] | None = None
    reason: str | None = None

    @field_validator("task", "phase", "reason", mode="before")
    @classmethod
    def _validate_optional_strings(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("must be a string")
        return value


def _copy_phases(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, (tuple, list)):
        raise ValueError("runtime todo state must contain phases")
    phases: list[dict[str, object]] = []
    for raw_phase in raw:
        if set(raw_phase) - {"name", "tasks"}:
            raise ValueError("runtime todo phase has unsupported fields")
        name = raw_phase.get("name")
        raw_tasks = raw_phase.get("tasks")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("runtime todo phase name must be non-empty")
        if not isinstance(raw_tasks, (tuple, list)):
            raise ValueError("runtime todo phase tasks must be an array")
        tasks: list[dict[str, object]] = []
        for raw_task in raw_tasks:
            if set(raw_task) - {"content", "status", "blocker"}:
                raise ValueError("runtime todo task has unsupported fields")
            content = raw_task.get("content")
            status = raw_task.get("status")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("runtime todo task content must be non-empty")
            if status not in _TODO_STATUSES:
                raise ValueError(f"runtime todo task has invalid status: {status}")
            task: dict[str, object] = {"content": content.strip(), "status": status}
            blocker = raw_task.get("blocker")
            if status == "blocked":
                if blocker is not None and not isinstance(blocker, str):
                    raise ValueError("blocked todo reason must be a string")
                if isinstance(blocker, str) and blocker:
                    task["blocker"] = blocker
            tasks.append(task)
        phases.append({"name": name.strip(), "tasks": tasks})
    return phases


def _state_from_runtime() -> list[dict[str, object]]:
    context = current_runtime_tool_context()
    return _copy_phases(context.todo_phases if context is not None else ())


def _normalize_in_progress(phases: list[dict[str, object]]) -> None:
    active: dict[str, object] | None = None
    for phase in phases:
        for task in cast(list[dict[str, object]], phase["tasks"]):
            if task["status"] != "in_progress":
                continue
            if active is None:
                active = task
            else:
                task["status"] = "pending"
    if active is not None:
        return
    for phase in phases:
        for task in cast(list[dict[str, object]], phase["tasks"]):
            if task["status"] == "pending":
                task["status"] = "in_progress"
                return


def _summary(phases: list[dict[str, object]]) -> dict[str, int]:
    counts = {status: 0 for status in _TODO_STATUSES}
    for phase in phases:
        for task in cast(list[dict[str, object]], phase["tasks"]):
            counts[cast(TodoStatus, task["status"])] += 1
    return {
        "total": sum(counts.values()),
        "pending": counts["pending"],
        "in_progress": counts["in_progress"],
        "completed": counts["completed"],
        "abandoned": counts["abandoned"],
        "blocked": counts["blocked"],
        "active": counts["pending"] + counts["in_progress"],
    }


def _task_locations(phases: list[dict[str, object]], content: str) -> list[dict[str, object]]:
    return [task for phase in phases for task in cast(list[dict[str, object]], phase["tasks"]) if task["content"] == content]


def _phase_by_name(phases: list[dict[str, object]], name: str) -> dict[str, object] | None:
    return next((phase for phase in phases if phase["name"] == name), None)


def _target_tasks(
    phases: list[dict[str, object]],
    *,
    task: str | None,
    phase: str | None,
    required: bool = False,
) -> list[dict[str, object]]:
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
        return cast(list[dict[str, object]], target["tasks"])
    if required:
        raise ValueError("todo operation requires a task or phase target")
    return [item for current in phases for item in cast(list[dict[str, object]], current["tasks"])]


def _init_phases(args: _TodoArgsModel) -> list[dict[str, object]]:
    if args.list_ is not None and args.items is not None:
        raise ValueError("init accepts list or items, not both")
    if args.list_ is None and (args.items is None or not args.items):
        raise ValueError("init requires a non-empty list or items")
    if args.list_ is not None:
        phases: list[dict[str, object]] = []
        seen_phases: set[str] = set()
        seen_tasks: set[str] = set()
        for raw_phase in args.list_:
            name = raw_phase.get("phase")
            raw_items = raw_phase.get("items")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("init phase name must be non-empty")
            if name.strip() in seen_phases:
                raise ValueError(f'Duplicate phase "{name.strip()}" in init list')
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError(f'init phase "{name.strip()}" requires items')
            seen_phases.add(name.strip())
            tasks: list[dict[str, object]] = []
            for item in raw_items:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("todo item must be a non-empty string")
                content = item.strip()
                if content in seen_tasks:
                    raise ValueError(f'Duplicate task "{content}" in init list')
                seen_tasks.add(content)
                tasks.append({"content": content, "status": "pending"})
            phases.append({"name": name.strip(), "tasks": tasks})
        return phases
    assert args.items is not None
    phase_name = (args.phase or "Tasks").strip()
    if not phase_name:
        raise ValueError("init phase name must be non-empty")
    tasks: list[dict[str, object]] = []
    seen_tasks: set[str] = set()
    for item in args.items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("todo item must be a non-empty string")
        content = item.strip()
        if content in seen_tasks:
            raise ValueError(f'Duplicate task "{content}" in init list')
        seen_tasks.add(content)
        tasks.append({"content": content, "status": "pending"})
    return [{"name": phase_name, "tasks": tasks}]


def _apply(args: _TodoArgsModel, phases: list[dict[str, object]]) -> None:
    if args.op == "init":
        phases[:] = _init_phases(args)
        _normalize_in_progress(phases)
        return
    if args.op == "view":
        return
    if args.op == "start":
        if args.task is None or not args.task.strip():
            raise ValueError("start requires task")
        target = _target_tasks(phases, task=args.task.strip(), phase=None, required=True)[0]
        for phase in phases:
            for task in cast(list[dict[str, object]], phase["tasks"]):
                if task is not target and task["status"] == "in_progress":
                    task["status"] = "pending"
        target["status"] = "in_progress"
        target.pop("blocker", None)
    elif args.op in {"done", "drop"}:
        for target in _target_tasks(phases, task=args.task, phase=args.phase):
            target["status"] = "completed" if args.op == "done" else "abandoned"
            target.pop("blocker", None)
    elif args.op == "block":
        targets = _target_tasks(phases, task=args.task, phase=args.phase, required=True)
        reason = " ".join((args.reason or "").split())
        for target in targets:
            if target["status"] in {"pending", "in_progress", "blocked"}:
                target["status"] = "blocked"
                if reason:
                    target["blocker"] = reason
                else:
                    target.pop("blocker", None)
    elif args.op == "unblock":
        for target in _target_tasks(phases, task=args.task, phase=args.phase, required=True):
            if target["status"] == "blocked":
                target["status"] = "pending"
                target.pop("blocker", None)
    elif args.op == "rm":
        if args.task is not None and args.phase is not None:
            raise ValueError("rm accepts either task or phase, not both")
        if args.task is not None:
            matches = _task_locations(phases, args.task)
            if not matches:
                raise ValueError(f'Task "{args.task}" not found')
            if len(matches) > 1:
                raise ValueError(f'Task "{args.task}" is not unique')
            target = matches[0]
            for phase in phases:
                tasks = cast(list[dict[str, object]], phase["tasks"])
                phase["tasks"] = [item for item in tasks if item is not target]
        elif args.phase is not None:
            target = _phase_by_name(phases, args.phase)
            if target is None:
                raise ValueError(f'Phase "{args.phase}" not found')
            target["tasks"] = []
        else:
            for phase in phases:
                phase["tasks"] = []
    elif args.op == "append":
        if args.phase is None or not args.phase.strip():
            raise ValueError("append requires phase")
        if args.items is None or not args.items:
            raise ValueError("append requires non-empty items")
        existing = {cast(str, task["content"]) for phase in phases for task in cast(list[dict[str, object]], phase["tasks"])}
        normalized_items: list[str] = []
        for item in args.items:
            content = item.strip()
            if not content:
                raise ValueError("todo item must be a non-empty string")
            if content in existing:
                raise ValueError(f'Task "{content}" already exists')
            existing.add(content)
            normalized_items.append(content)
        target = _phase_by_name(phases, args.phase.strip())
        if target is None:
            target = {"name": args.phase.strip(), "tasks": cast(list[dict[str, object]], [])}
            phases.append(cast(dict[str, object], target))
        target_tasks = cast(list[dict[str, object]], target["tasks"])
        target_tasks.extend({"content": content, "status": "pending"} for content in normalized_items)
    else:
        raise ValueError(f"unsupported todo operation: {args.op}")
    _normalize_in_progress(phases)


def _render(phases: list[dict[str, object]], op: str) -> str:
    if not any(cast(list[dict[str, object]], phase["tasks"]) for phase in phases):
        return "Todo list is empty." if op == "view" else "Todo list cleared."
    lines = [f"Todo {op} applied."]
    for phase in phases:
        lines.append(f"{phase['name']}:")
        for task in cast(list[dict[str, object]], phase["tasks"]):
            blocker = f" — {task['blocker']}" if task.get("blocker") else ""
            lines.append(f"- [{task['status']}] {task['content']}{blocker}")
    return "\n".join(lines)


_TODO_DESCRIPTION = (
    "Apply one operation to the runtime-owned todo list. The operation is one of "
    "init, start, done, drop, block, unblock, append, rm, or view. Use init with "
    "list=[{phase, items}] (or items plus optional phase) to create a plan. Use "
    "exact task content and phase names for targeting. Start a task before starting "
    "implementation and mark it done immediately after finishing. Only one task can "
    "be in_progress; the runtime normalizes that invariant. block accepts an optional "
    "reason, and view never mutates state. This tool does not write workspace files."
)


class TodoTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="todo",
        description=_TODO_DESCRIPTION,
        input_schema={
            "op": {"type": "string", "enum": ["init", "start", "done", "drop", "block", "unblock", "append", "rm", "view"]},
            "list": {"type": "array", "description": "init phases, each with phase and non-empty items"},
            "task": {"type": "string", "description": "Exact task content target"},
            "phase": {"type": "string", "description": "Exact phase name target or append destination"},
            "items": {"type": "array", "description": "Task strings for init or append"},
            "reason": {"type": "string", "description": "Optional reason for block"},
            "required": ["op"],
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _TodoArgsModel.model_validate(call.arguments)
        except ValidationError as exc:
            first = exc.errors()[0]
            field = first.get("loc", ("argument",))[0]
            raise ValueError(f"todo invalid {field}: {first.get('msg', 'invalid value')}") from exc
        phases = _state_from_runtime()
        _apply(args, phases)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=_render(phases, args.op),
            data={
                "phases": phases,
                "summary": _summary(phases),
                "op": args.op,
                "mutated": args.op != "view",
            },
        )
