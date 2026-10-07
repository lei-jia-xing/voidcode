from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from .turns import CallSeed, ModelTurnFact, ReportedCall, StreamFact, ToolCompletedFact, ToolRequestedFact, TurnFact


@dataclass(frozen=True, slots=True)
class FactEntry:
    sequence: int
    parent_sequence: int | None
    fact: TurnFact


@dataclass(frozen=True, slots=True)
class FactPage:
    leaf_sequence: int | None
    max_sequence: int
    entries: tuple[FactEntry, ...]
    next_after_sequence: int | None


class FactStore(Protocol):
    def append(self, facts: tuple[TurnFact, ...], *, dedupe_keys: tuple[str | None, ...] = ()) -> tuple[int, ...]: ...

    def read(self, *, after_sequence: int = 0, limit: int, leaf_sequence: int | None = None) -> FactPage: ...

    def checkout(self, sequence: int) -> None: ...

    def fork(self, sequence: int | None = None) -> FactStore: ...

    def restore_batch(self) -> CallSeed: ...


def require_complete_tool_pairs(entries: tuple[FactEntry, ...]) -> None:
    pending: set[str] = set()
    for entry in entries:
        fact = entry.fact
        if not isinstance(fact, (ToolRequestedFact, ToolCompletedFact)):
            continue
        call_id = fact.call.tool_call_id if isinstance(fact, ToolRequestedFact) else fact.report.tool_call_id
        if call_id is None or not call_id:
            raise ValueError("execution facts require an original normalized call identity")
        if isinstance(fact, ToolRequestedFact):
            pending.add(call_id)
        elif call_id not in pending:
            raise ValueError("native completion has no matching original request")
        else:
            pending.remove(call_id)
    if pending:
        raise ValueError("checkout boundary splits a native request from its result")


class MemoryEventStore:
    """An isolated append-only fact tree with a movable leaf, not a runtime."""

    def __init__(self) -> None:
        self._entries: dict[int, FactEntry] = {}
        self._dedupe: dict[str, int] = {}
        self._leaf: int | None = None
        self._max_sequence = 0

    def append(self, facts: tuple[TurnFact, ...], *, dedupe_keys: tuple[str | None, ...] = ()) -> tuple[int, ...]:
        if dedupe_keys and len(dedupe_keys) != len(facts):
            raise ValueError("each fact requires its own dedupe slot")
        # Snapshot and validate the complete batch before modifying the tree.
        snapshots = deepcopy(facts)
        staged: list[tuple[FactEntry, str | None]] = []
        seen: set[str] = set()
        leaf, watermark = self._leaf, self._max_sequence
        for index, fact in enumerate(snapshots):
            if isinstance(fact, StreamFact):
                raise ValueError("provider stream and tool-call deltas are live-only facts")
            if isinstance(fact, (ToolRequestedFact, ToolCompletedFact)):
                call_id = fact.call.tool_call_id if isinstance(fact, ToolRequestedFact) else fact.report.tool_call_id
                if not call_id:
                    raise ValueError("execution facts require an original normalized call identity")
            key = dedupe_keys[index] if dedupe_keys else None
            if key is not None and (key in self._dedupe or key in seen):
                continue
            if key is not None:
                seen.add(key)
            watermark += 1
            entry = FactEntry(watermark, leaf, fact)
            staged.append((entry, key))
            leaf = watermark
        for entry, key in staged:
            self._entries[entry.sequence] = entry
            if key is not None:
                self._dedupe[key] = entry.sequence
        self._leaf, self._max_sequence = leaf, watermark
        return tuple(entry.sequence for entry, _key in staged)

    def _path(self, leaf: int | None) -> tuple[FactEntry, ...]:
        chain: list[FactEntry] = []
        while leaf is not None:
            entry = self._entries.get(leaf)
            if entry is None:
                raise ValueError("fact tree has an unknown ancestor")
            chain.append(entry)
            leaf = entry.parent_sequence
        chain.reverse()
        return tuple(chain)

    def read(self, *, after_sequence: int = 0, limit: int, leaf_sequence: int | None = None) -> FactPage:
        if isinstance(limit, bool) or limit < 1 or isinstance(after_sequence, bool) or after_sequence < 0:
            raise ValueError("fact page needs a positive limit and non-negative cursor")
        leaf = self._leaf if leaf_sequence is None else leaf_sequence
        path = self._path(leaf)
        if after_sequence != 0 and not any(entry.sequence == after_sequence for entry in path):
            raise ValueError("fact page cursor does not belong to its pinned lineage")
        selected = [entry for entry in path if entry.sequence > after_sequence]
        page = tuple(selected[:limit])
        next_cursor = page[-1].sequence if len(selected) > limit else None
        return FactPage(leaf, self._max_sequence, deepcopy(page), next_cursor)

    def checkout(self, sequence: int) -> None:
        path = self._path(sequence)
        require_complete_tool_pairs(path)
        self._leaf = sequence

    def restore_batch(self) -> CallSeed:
        path = self._path(self._leaf)
        completed = [entry for entry in path if isinstance(entry.fact, ToolCompletedFact)]
        if not completed:
            raise ValueError("recorded facts have no authentic native batch to continue")
        latest = completed[-1]
        assert isinstance(latest.fact, ToolCompletedFact)
        batch = latest.fact.batch
        if batch is None or batch.completed_reports:
            raise ValueError("recorded completion has no independent authentic batch snapshot")
        current: list[ReportedCall] = []
        for entry in reversed(completed):
            assert isinstance(entry.fact, ToolCompletedFact)
            if entry.fact.batch != batch:
                break
            current.append(entry.fact.report)
        current.reverse()
        ids = tuple(call.tool_call_id for call in batch.calls)
        if not ids or any(not call_id for call_id in ids) or len(set(ids)) != len(ids):
            raise ValueError("recorded native batch has missing or duplicate original identities")
        if tuple(report.tool_call_id for report in current) != ids[: len(current)]:
            raise ValueError("recorded native results are not the actual completed prefix")
        trailing = tuple(entry.fact for entry in path if entry.sequence > latest.sequence)
        if any(isinstance(fact, ToolRequestedFact) for fact in trailing):
            if any(isinstance(fact, ModelTurnFact) for fact in trailing) or any(
                isinstance(fact, ToolRequestedFact) and fact.call.tool_call_id not in ids[len(current) :] for fact in trailing
            ):
                raise ValueError("the newer requested batch has no authentic completion snapshot")
        return deepcopy(
            CallSeed(
                calls=batch.calls,
                reasoning=batch.reasoning,
                completed_reports=tuple(current),
                run_step=batch.run_step,
            )
        )

    def fork(self, sequence: int | None = None) -> MemoryEventStore:
        if sequence is not None and sequence < 1:
            raise ValueError("fork sequence must be positive")
        leaf = self._max_sequence if sequence is None else min(sequence, self._max_sequence)
        if not leaf:
            raise ValueError("cannot fork an empty fact log")
        prefix = {key: entry for key, entry in self._entries.items() if key <= leaf}
        require_complete_tool_pairs(self._path(leaf))
        branch = MemoryEventStore()
        branch._entries = deepcopy(prefix)
        branch._leaf = leaf
        branch._max_sequence = leaf
        return branch
