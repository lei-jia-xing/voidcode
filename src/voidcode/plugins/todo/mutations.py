"""The single read-modify-write path shared by tools and commands."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace

from voidcode.sdk.services import PluginServices, StateConflict

from . import limits
from .models import TodoPhase
from .operations import RESETTING_OPS, _TodoArgs, apply
from .state import TodoState, _store, todo_event_payload


@dataclass(frozen=True, slots=True)
class TodoChange:
    state: TodoState
    original: list[TodoPhase]
    mutated: bool


def apply_and_store(args: _TodoArgs, services: PluginServices, *, expected_revision: int | None = None) -> TodoChange:
    """One mutation path for model tools and interactive user commands."""
    for attempt in range(limits.MAX_TODO_STATE_CONFLICT_ATTEMPTS):
        state, version = _store(services).load()
        if expected_revision is not None and state.revision != expected_revision:
            raise ValueError("Todo list changed while this editor was open; reopen /todo before updating")
        phases = [deepcopy(phase) for phase in state.phases]
        original = deepcopy(phases)
        apply(args, phases)
        mutated = phases != original
        updated = replace(state, revision=state.revision + int(mutated), phases=tuple(phases))
        if mutated:
            try:
                _store(services).save(updated, version)
            except StateConflict as exc:
                if attempt + 1 >= limits.MAX_TODO_STATE_CONFLICT_ATTEMPTS:
                    raise ValueError("Todo state changed concurrently; retry the operation") from exc
                continue
            services.stage_event("todo_updated", todo_event_payload(phases=phases, revision=updated.revision))
        if args.op in RESETTING_OPS:
            services.cancel_continuations()
        return TodoChange(state=updated, original=original, mutated=mutated)
    raise AssertionError("todo state-conflict retry exhausted without a result")
