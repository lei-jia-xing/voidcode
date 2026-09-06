from __future__ import annotations

from typing import Literal, TypedDict, cast

TodoStatus = Literal["pending", "in_progress", "completed", "abandoned", "blocked"]
TODO_STATUSES: tuple[TodoStatus, ...] = (
    "pending",
    "in_progress",
    "completed",
    "abandoned",
    "blocked",
)


class RuntimeTodoTask(TypedDict, total=False):
    content: str
    status: TodoStatus
    blocker: str


class RuntimeTodoPhase(TypedDict):
    name: str
    tasks: list[RuntimeTodoTask]


class RuntimeTodoSummary(TypedDict):
    total: int
    pending: int
    in_progress: int
    completed: int
    abandoned: int
    blocked: int
    active: int


_TODO_STATE_KEYS = frozenset({"version", "revision", "phases", "summary"})


def runtime_todo_state_from_payload(raw_state: object) -> dict[str, object]:
    if not isinstance(raw_state, dict):
        raise ValueError("runtime todo state must be an object")
    state = cast(dict[object, object], raw_state)
    unknown = sorted(str(key) for key in state if key not in _TODO_STATE_KEYS)
    if unknown:
        raise ValueError(f"runtime todo state field '{unknown[0]}' is not supported")
    version = state.get("version")
    if version != 2 or isinstance(version, bool):
        raise ValueError(f"runtime todo state version must be 2, got {version!r}")
    revision = state.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("runtime todo revision must be a non-negative integer")
    phases = _parse_phases(state.get("phases"))
    summary = state.get("summary")
    if not isinstance(summary, dict) or dict(summary) != dict(todo_summary(phases)):
        raise ValueError("runtime todo state summary does not match phases")
    return todo_state_payload(phases, revision=revision)


def _parse_status(value: object) -> TodoStatus:
    if value in TODO_STATUSES:
        return cast(TodoStatus, value)
    raise ValueError(f"runtime todo task has invalid status: {value}")


def _parse_phases(raw_phases: object) -> tuple[RuntimeTodoPhase, ...]:
    if not isinstance(raw_phases, list):
        raise ValueError("runtime todo state requires phases array")
    phases: list[RuntimeTodoPhase] = []
    phase_names: set[str] = set()
    task_contents: set[str] = set()
    active_count = 0
    for raw_phase in raw_phases:
        if not isinstance(raw_phase, dict):
            raise ValueError("runtime todo phase must be an object")
        phase = cast(dict[object, object], raw_phase)
        name = phase.get("name")
        raw_tasks = phase.get("tasks")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("runtime todo phase name must be non-empty")
        name = name.strip()
        if name in phase_names:
            raise ValueError(f'Duplicate todo phase "{name}"')
        if not isinstance(raw_tasks, list):
            raise ValueError(f'todo phase "{name}" requires tasks array')
        phase_names.add(name)
        tasks: list[RuntimeTodoTask] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                raise ValueError("runtime todo task must be an object")
            task = cast(dict[object, object], raw_task)
            content = task.get("content")
            status = _parse_status(task.get("status"))
            blocker = task.get("blocker")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("runtime todo task content must be non-empty")
            content = content.strip()
            if content in task_contents:
                raise ValueError(f'Duplicate todo task "{content}"')
            if status == "blocked":
                if blocker is not None and (not isinstance(blocker, str) or not blocker.strip()):
                    raise ValueError("blocked todo reason must be a non-empty string")
            elif blocker is not None:
                raise ValueError("only blocked todo tasks may include blocker")
            normalized: RuntimeTodoTask = {"content": content, "status": status}
            if isinstance(blocker, str) and blocker.strip():
                normalized["blocker"] = " ".join(blocker.split())
            tasks.append(normalized)
            task_contents.add(content)
            active_count += status == "in_progress"
        phases.append({"name": name, "tasks": tasks})
    if active_count > 1:
        raise ValueError("runtime todo state permits only one in_progress task")
    return tuple(phases)


def todo_summary(phases: tuple[RuntimeTodoPhase, ...]) -> RuntimeTodoSummary:
    counts = {status: 0 for status in TODO_STATUSES}
    for phase in phases:
        for task in phase["tasks"]:
            counts[task["status"]] += 1
    return {
        "total": sum(counts.values()),
        "pending": counts["pending"],
        "in_progress": counts["in_progress"],
        "completed": counts["completed"],
        "abandoned": counts["abandoned"],
        "blocked": counts["blocked"],
        "active": counts["pending"] + counts["in_progress"],
    }


def runtime_todo_phases_from_payload(raw_phases: object) -> tuple[RuntimeTodoPhase, ...]:
    return _parse_phases(raw_phases)


def runtime_todo_phases_equal(
    current: tuple[RuntimeTodoPhase, ...],
    *,
    raw_phases: object,
) -> bool:
    try:
        candidate = _parse_phases(raw_phases)
    except ValueError:
        return False
    return candidate == current


def todo_state_payload(
    phases: tuple[RuntimeTodoPhase, ...],
    *,
    revision: int,
) -> dict[str, object]:
    return {
        "version": 2,
        "revision": revision,
        "phases": [{"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]} for phase in phases],
        "summary": todo_summary(phases),
    }


def todo_event_payload(
    *,
    session_id: str,
    phases: tuple[RuntimeTodoPhase, ...],
    revision: int,
) -> dict[str, object]:
    summary = todo_summary(phases)
    return {
        "session_id": session_id,
        "todo_count": summary["total"],
        "active_count": summary["active"],
        "pending_count": summary["pending"],
        "in_progress_count": summary["in_progress"],
        "completed_count": summary["completed"],
        "abandoned_count": summary["abandoned"],
        "blocked_count": summary["blocked"],
        "revision": revision,
        "phases": [{"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]} for phase in phases],
        "summary": dict(summary),
    }


def todo_state_from_session_metadata(session_metadata: dict[str, object]) -> dict[str, object] | None:
    from .session_metadata_helpers import runtime_state_todos

    todo_state = runtime_state_todos(session_metadata)
    if todo_state is None:
        return None
    return runtime_todo_state_from_payload(todo_state)


def render_provider_todo_state(session_metadata: dict[str, object]) -> str | None:
    todo_state = todo_state_from_session_metadata(session_metadata)
    if todo_state is None:
        return None
    phases = _parse_phases(todo_state["phases"])
    active_phases = tuple(
        {"name": phase["name"], "tasks": [task for task in phase["tasks"] if task["status"] in {"pending", "in_progress", "blocked"}]}
        for phase in phases
    )
    active_phases = tuple(phase for phase in active_phases if phase["tasks"])
    if not active_phases:
        return None
    lines = [
        "Runtime-managed todo state is active for this session.",
        "Use this as the current plan truth; do not recreate it from older tool results.",
        "Use one todo operation at a time; view is read-only and mutations are persisted by the runtime.",
        "Current active todos:",
    ]
    for phase in active_phases:
        lines.append(f"{phase['name']}:")
        for task in cast(list[RuntimeTodoTask], phase["tasks"]):
            blocker = f" (blocked: {task['blocker']})" if task.get("blocker") else ""
            lines.append(f"- [{task['status']}] {task['content']}{blocker}")
    return "\n".join(lines)
