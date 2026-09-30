"""Runtime-owned temporary provider-context reminder channel.

A reminder is a tail-appended provider segment for one context assembly. The
assembly is transient and is not the persisted transcript; only the cycle
counters (session metadata) and one ``runtime.reminder_injected`` event survive
the call.

The first reminder kind is the todo completion reminder: when a terminal
assistant turn ends while ``pending``/``in_progress`` todos remain, the runtime
nudges the agent to continue (or to mark the list complete) and lets the loop
run one more turn. Semantics mirror the upstream pi-coding-agent todo tracker:
per-cycle attempt budget (default 3), no repeat while the previous reminder is
still awaiting agent progress, and silence while the session is parked on a
user answer or a background task that will re-wake the loop.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from ..core.transcript import ContextSegment
from .todos import RuntimeTodoPhase

#: ``session.metadata["runtime_state"]["reminders"]`` section key.
REMINDERS_RUNTIME_STATE_KEY: Final[str] = "reminders"
#: Reminder kind name; also the ``runtime_state.reminders`` sub-section key.
TODO_REMINDER_KIND: Final[str] = "todo"
#: Mid-run nudge kind: same per-call channel, fired between provider calls when
#: the todo list has gone stale (upstream ``takeMidRunNudge``).
TODO_MID_RUN_KIND: Final[str] = "todo_mid_run"
#: Segment metadata source for the per-call reminder channel.
TODO_REMINDER_SOURCE: Final[str] = "runtime_reminder"
#: Default per-user-cycle budget for todo completion reminders.
DEFAULT_TODO_REMINDER_MAX_PER_CYCLE: Final[int] = 3
#: Role the reminder is appended with.
#:
#: A trailing ``user`` message keeps the provider-agnostic wire shape the
#: adapters already render and, unlike a ``system`` segment, leaves the cached
#: system/tools prefix byte-identical on reminder turns.
TODO_REMINDER_ROLE: Final[Literal["user"]] = "user"

#: Mutation-tool calls since the last todo touch that trigger a mid-run nudge
#: (upstream ``MID_RUN_NUDGE_MUTATION_THRESHOLD``).
TODO_MID_RUN_MUTATION_THRESHOLD: Final[int] = 12
#: Mid-run nudges per cycle (upstream ``MID_RUN_NUDGE_MAX_PER_CYCLE``).
TODO_MID_RUN_MAX_PER_CYCLE: Final[int] = 2

_INCOMPLETE_TODO_STATUSES: Final[frozenset[str]] = frozenset({"pending", "in_progress"})
_TODO_REMINDER_STATE_KEYS: Final[frozenset[str]] = frozenset({"attempts", "awaiting_progress", "progress_watermark", "cycle_run_id"})
_TODO_MID_RUN_STATE_KEYS: Final[frozenset[str]] = frozenset({"attempts", "mutations", "mutation_baseline", "cycle_run_id"})


@dataclass(frozen=True, slots=True)
class TodoReminderState:
    """Per-cycle reminder bookkeeping persisted in session metadata.

    ``cycle_run_id`` identifies the user-prompt cycle (the session's current
    ``runtime_state.run_id``) the counters belong to; a mismatch starts a fresh
    cycle. ``progress_watermark`` is the tool-result count observed when the last
    reminder was sent, so a later turn can tell "the agent acted" from "the agent
    stopped again without touching anything".
    """

    attempts: int = 0
    awaiting_progress: bool = False
    progress_watermark: int = 0
    cycle_run_id: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "awaiting_progress": self.awaiting_progress,
            "progress_watermark": self.progress_watermark,
            "cycle_run_id": self.cycle_run_id,
        }


@dataclass(frozen=True, slots=True)
class ReminderSuppression:
    """Reasons a reminder is withheld, shared by every reminder kind.

    One predicate for the completion reminder and the mid-run nudge, so the two
    kinds cannot drift apart on "the loop is already parked".
    """

    delegated_child: bool = False
    runtime_waits_for_user: bool = False
    pending_background_task: bool = False
    plan_mode: bool = False
    todo_tool_available: bool = True

    @property
    def blocked(self) -> bool:
        return self.delegated_child or self.runtime_waits_for_user or self.pending_background_task or self.plan_mode or not self.todo_tool_available


@dataclass(frozen=True, slots=True)
class TodoMidRunState:
    """Per-cycle mid-run nudge bookkeeping (``runtime_state.reminders.todo_mid_run``).

    ``mutations`` counts mutation-tool results since the last todo touch;
    ``mutation_baseline`` is the value the counter was reset at (a nudge, or the
    todo touch that cleared it).
    """

    attempts: int = 0
    mutations: int = 0
    mutation_baseline: int = 0
    cycle_run_id: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "mutations": self.mutations,
            "mutation_baseline": self.mutation_baseline,
            "cycle_run_id": self.cycle_run_id,
        }


@dataclass(frozen=True, slots=True)
class ReminderDecision[S]:
    """One verdict from either reminder kind, plus the state to persist for it.

    ``text`` is the only difference that matters to callers: whether it injects.
    ``mutation_count`` only carries a value for the mid-run nudge.
    """

    state: S
    max_attempts: int
    attempt: int = 0
    incomplete_count: int = 0
    mutation_count: int = 0
    text: str | None = None

    @property
    def injects(self) -> bool:
        return self.text is not None


def _cycle_run_id(value: object) -> str | None:
    """The validated ``cycle_run_id`` (shape checked by :func:`_state_from_payload`)."""
    return value if isinstance(value, str) and value else None


def _state_from_payload(raw: object, *, kind: str, keys: frozenset[str]) -> dict[str, object]:
    """Validate one persisted reminder sub-state's shape and shared fields.

    Returns the raw mapping plus the normalized ``cycle_run_id``. Callers add
    their own counters; unknown or missing fields are rejected here.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"persisted runtime_state.reminders.{kind} must be an object")
    unknown = sorted(str(key) for key in raw if key not in keys)
    if unknown:
        raise ValueError(f"persisted runtime_state.reminders.{kind} field '{unknown[0]}' is not supported")
    missing = sorted(keys - raw.keys())
    if missing:
        raise ValueError(f"persisted runtime_state.reminders.{kind} is missing field(s): " + ", ".join(missing))
    cycle_run_id = raw["cycle_run_id"]
    if cycle_run_id is not None and (not isinstance(cycle_run_id, str) or not cycle_run_id):
        raise ValueError(f"persisted runtime_state.reminders.{kind} field 'cycle_run_id' must be a non-empty string or null")
    return dict(raw)


def todo_reminder_state_from_payload(raw: object) -> TodoReminderState:
    """Parse a persisted ``runtime_state.reminders.todo`` payload (absent = fresh)."""
    if raw is None:
        return TodoReminderState()
    state = _state_from_payload(raw, kind=TODO_REMINDER_KIND, keys=_TODO_REMINDER_STATE_KEYS)
    awaiting = state["awaiting_progress"]
    if not isinstance(awaiting, bool):
        raise ValueError(f"persisted runtime_state.reminders.{TODO_REMINDER_KIND} field 'awaiting_progress' must be a boolean")
    return TodoReminderState(
        attempts=_non_negative_int(state["attempts"], field="attempts"),
        awaiting_progress=awaiting,
        progress_watermark=_non_negative_int(state["progress_watermark"], field="progress_watermark"),
        cycle_run_id=_cycle_run_id(state["cycle_run_id"]),
    )


def _reminders_section(metadata: Mapping[str, object]) -> dict[str, object]:
    """The present ``runtime_state.reminders`` section, validated for kind keys."""
    raw_runtime_state = metadata.get("runtime_state")
    if not isinstance(raw_runtime_state, dict):
        return {}
    raw_reminders = raw_runtime_state.get(REMINDERS_RUNTIME_STATE_KEY)
    if raw_reminders is None:
        return {}
    if not isinstance(raw_reminders, dict):
        raise ValueError("persisted runtime_state.reminders must be an object")
    unknown = sorted(str(key) for key in raw_reminders if key not in {TODO_REMINDER_KIND, TODO_MID_RUN_KIND})
    if unknown:
        raise ValueError(f"persisted runtime_state.reminders field '{unknown[0]}' is not supported")
    return dict(raw_reminders)


def todo_reminder_state_from_metadata(metadata: Mapping[str, object]) -> TodoReminderState:
    """Read the todo completion reminder state; absent means fresh."""
    section = _reminders_section(metadata)
    if TODO_REMINDER_KIND not in section:
        return TodoReminderState()
    return todo_reminder_state_from_payload(section[TODO_REMINDER_KIND])


def todo_mid_run_state_from_payload(raw: object) -> TodoMidRunState:
    """Parse a persisted ``runtime_state.reminders.todo_mid_run`` payload (absent = fresh)."""
    if raw is None:
        return TodoMidRunState()
    state = _state_from_payload(raw, kind=TODO_MID_RUN_KIND, keys=_TODO_MID_RUN_STATE_KEYS)
    return TodoMidRunState(
        attempts=_non_negative_int(state["attempts"], field="attempts", kind=TODO_MID_RUN_KIND),
        mutations=_non_negative_int(state["mutations"], field="mutations", kind=TODO_MID_RUN_KIND),
        mutation_baseline=_non_negative_int(state["mutation_baseline"], field="mutation_baseline", kind=TODO_MID_RUN_KIND),
        cycle_run_id=_cycle_run_id(state["cycle_run_id"]),
    )


def todo_mid_run_state_from_metadata(metadata: Mapping[str, object]) -> TodoMidRunState:
    """Read the mid-run nudge state; absent means fresh."""
    section = _reminders_section(metadata)
    if TODO_MID_RUN_KIND not in section:
        return TodoMidRunState()
    return todo_mid_run_state_from_payload(section[TODO_MID_RUN_KIND])


def reminders_runtime_state_payload(
    *,
    todo: TodoReminderState | None = None,
    mid_run: TodoMidRunState | None = None,
) -> dict[str, object]:
    """Build the provided ``runtime_state.reminders`` kind payloads."""
    payload: dict[str, object] = {}
    if todo is not None:
        payload[TODO_REMINDER_KIND] = todo.payload()
    if mid_run is not None:
        payload[TODO_MID_RUN_KIND] = mid_run.payload()
    return payload


def decide_todo_mid_run_nudge(
    *,
    max_per_cycle: int = TODO_MID_RUN_MAX_PER_CYCLE,
    state: TodoMidRunState,
    run_id: str | None,
    mutations: int,
    incomplete_count: int,
    suppression: ReminderSuppression,
) -> ReminderDecision[TodoMidRunState]:
    """Decide whether the next provider call earns a mid-run todo nudge.

    ``mutations`` is the number of successful mutation-tool results since the
    last todo touch (already derived by the caller from one source). Trigger,
    budget and reset follow upstream ``takeMidRunNudge``: at least
    ``TODO_MID_RUN_MUTATION_THRESHOLD`` mutations, at most
    ``max_per_cycle`` nudges per cycle, and the counter resets when a nudge is
    sent or when the todo list is touched again.
    """
    fresh = TodoMidRunState(cycle_run_id=run_id)
    cycle_state = state if state.cycle_run_id == run_id else fresh
    # A todo touch since the baseline clears the mutation counter.
    baseline = min(cycle_state.mutation_baseline, mutations)
    pending = mutations - baseline
    counters = TodoMidRunState(
        attempts=cycle_state.attempts,
        mutations=pending,
        mutation_baseline=baseline,
        cycle_run_id=run_id,
    )
    silent: ReminderDecision[TodoMidRunState] = ReminderDecision(
        state=counters,
        max_attempts=max_per_cycle,
        mutation_count=pending,
        incomplete_count=incomplete_count,
    )
    if incomplete_count == 0 or suppression.blocked:
        return silent
    if pending < TODO_MID_RUN_MUTATION_THRESHOLD or counters.attempts >= max_per_cycle:
        return silent
    attempt = counters.attempts + 1
    return ReminderDecision[TodoMidRunState](
        state=TodoMidRunState(attempts=attempt, mutations=0, mutation_baseline=mutations, cycle_run_id=run_id),
        max_attempts=max_per_cycle,
        attempt=attempt,
        mutation_count=pending,
        incomplete_count=incomplete_count,
        text=todo_mid_run_nudge_text(
            mutation_count=pending,
            incomplete_count=incomplete_count,
            attempt=attempt,
            max_attempts=max_per_cycle,
        ),
    )


def todo_mid_run_nudge_text(*, mutation_count: int, incomplete_count: int, attempt: int, max_attempts: int) -> str:
    """Render the bounded mid-run nudge body."""
    return "\n".join(
        [
            '<system-reminder reason="todo_stale">',
            f"{mutation_count} mutating tool call(s) have run since the last todo update, and {incomplete_count} todo item(s) are still open.",
            "Reconcile the list before continuing: mark finished work done and record what is left.",
            f"(Mid-run nudge {attempt}/{max_attempts})",
            "</system-reminder>",
        ]
    )


def todo_mid_run_segment(decision: ReminderDecision[TodoMidRunState]) -> ContextSegment:
    """Bind an injecting mid-run decision to its per-call tail segment."""
    return _decision_segment(decision, reminder_type=TODO_MID_RUN_KIND)


def todo_mutation_count(
    tool_results: Sequence[Any],
    *,
    read_only_tool_names: frozenset[str],
) -> int:
    """Successful mutation-tool results since the last successful todo call.

    Mutability has one source: ``ToolDefinition.read_only`` (the same flag the
    permission policy uses to auto-allow reads). ``todo`` results are the reset
    signal (upstream clears ``mutationsSinceLastTouch`` on a todo result), and a
    failed call does not count (upstream counts only successful results).
    """
    last_todo_touch = -1
    for index, result in enumerate(tool_results):
        if result.tool_name == TODO_REMINDER_KIND and result.status == "ok":
            last_todo_touch = index
    return sum(
        1
        for result in tool_results[last_todo_touch + 1 :]
        if result.status == "ok" and result.tool_name != TODO_REMINDER_KIND and result.tool_name not in read_only_tool_names
    )


def incomplete_todo_phases(phases: Iterable[RuntimeTodoPhase]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Project todo phases down to the incomplete tasks a reminder lists."""
    incomplete: list[tuple[str, tuple[str, ...]]] = []
    for phase in phases:
        contents = tuple(task["content"] for task in phase["tasks"] if task["status"] in _INCOMPLETE_TODO_STATUSES)
        if contents:
            incomplete.append((phase["name"], contents))
    return tuple(incomplete)


def decide_todo_reminder(
    *,
    max_per_cycle: int,
    state: TodoReminderState,
    run_id: str | None,
    incomplete_phases: tuple[tuple[str, tuple[str, ...]], ...],
    tool_result_count: int,
    suppression: ReminderSuppression,
) -> ReminderDecision[TodoReminderState]:
    """Decide whether a terminal turn earns a todo completion reminder.

    Callers check ``reminders.enabled`` first. Every branch returns the state to
    persist: the attempt budget and the awaiting-progress flag survive a skip,
    while a finished todo list (or a new cycle) resets them.
    """
    fresh = TodoReminderState(cycle_run_id=run_id)
    cycle_state = state if state.cycle_run_id == run_id else fresh
    incomplete_count = sum(len(contents) for _name, contents in incomplete_phases)
    silent: ReminderDecision[TodoReminderState] = ReminderDecision(state=cycle_state, max_attempts=max_per_cycle, incomplete_count=incomplete_count)
    if not incomplete_phases:
        return replace(silent, state=fresh)
    if suppression.blocked:
        return silent
    if cycle_state.awaiting_progress and tool_result_count <= cycle_state.progress_watermark:
        return silent
    progressed = replace(cycle_state, awaiting_progress=False)
    if progressed.attempts >= max_per_cycle:
        return replace(silent, state=progressed)
    attempt = progressed.attempts + 1
    return ReminderDecision[TodoReminderState](
        state=TodoReminderState(
            attempts=attempt,
            awaiting_progress=True,
            progress_watermark=tool_result_count,
            cycle_run_id=run_id,
        ),
        max_attempts=max_per_cycle,
        attempt=attempt,
        incomplete_count=incomplete_count,
        text=todo_reminder_text(incomplete_phases, attempt=attempt, max_attempts=max_per_cycle),
    )


def todo_reminder_text(
    incomplete_phases: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    attempt: int,
    max_attempts: int,
) -> str:
    """Render the bounded reminder body sent to the provider."""
    incomplete_count = sum(len(contents) for _name, contents in incomplete_phases)
    lines = ["<system-reminder>", f"You stopped with {incomplete_count} incomplete todo item(s):"]
    for name, contents in incomplete_phases:
        lines.append(f"- {name}")
        lines.extend(f"  - {content}" for content in contents)
    lines.append("")
    lines.append("Please continue working on these tasks or mark them complete if finished.")
    lines.append(f"(Reminder {attempt}/{max_attempts})")
    lines.append("</system-reminder>")
    return "\n".join(lines)


def _decision_segment[S](
    decision: ReminderDecision[S],
    *,
    reminder_type: str,
) -> ContextSegment:
    """Build the ephemeral tail segment for this provider-context assembly."""
    text = decision.text
    if text is None:
        raise ValueError(f"{reminder_type} segment requires an injecting decision")
    metadata: dict[str, object] = {
        "source": TODO_REMINDER_SOURCE,
        "tier": "recent",
        "reminder_type": reminder_type,
        "attempt": decision.attempt,
        "max_attempts": decision.max_attempts,
        "incomplete_count": decision.incomplete_count,
    }
    if reminder_type == TODO_MID_RUN_KIND:
        metadata["mutation_count"] = decision.mutation_count
    return ContextSegment(role=TODO_REMINDER_ROLE, content=text, metadata=metadata)


def todo_reminder_segment(decision: ReminderDecision[TodoReminderState]) -> ContextSegment:
    """Bind an injecting completion decision to its per-call tail segment."""
    return _decision_segment(decision, reminder_type=TODO_REMINDER_KIND)


def _non_negative_int(value: object, *, field: str, kind: str = TODO_REMINDER_KIND) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"persisted runtime_state.reminders.{kind} field '{field}' must be a non-negative integer")
    return value


__all__ = [
    "DEFAULT_TODO_REMINDER_MAX_PER_CYCLE",
    "TODO_MID_RUN_KIND",
    "TODO_MID_RUN_MAX_PER_CYCLE",
    "TODO_MID_RUN_MUTATION_THRESHOLD",
    "REMINDERS_RUNTIME_STATE_KEY",
    "TODO_REMINDER_KIND",
    "TODO_REMINDER_ROLE",
    "TODO_REMINDER_SOURCE",
    "ReminderSuppression",
    "TodoMidRunState",
    "ReminderDecision",
    "TodoReminderState",
    "decide_todo_mid_run_nudge",
    "decide_todo_reminder",
    "incomplete_todo_phases",
    "reminders_runtime_state_payload",
    "todo_mid_run_segment",
    "todo_mutation_count",
    "todo_mid_run_state_from_metadata",
    "todo_mid_run_state_from_payload",
    "todo_reminder_segment",
    "todo_reminder_state_from_metadata",
    "todo_reminder_state_from_payload",
    "todo_reminder_text",
]
