"""Session-scope touched-path state feeding the workspace rule hierarchy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from voidcode.sdk.hooks import InvocationFinishedFact, PluginFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import StateScope
from voidcode.sdk.services import HistoryServices, PluginServices, StateServices

TOUCHED_PATHS_LIMIT = 64
RULES_STATE_SCHEMA_VERSION = 1
_RULES_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "cursor_sequence", "touched_paths"})
_EVENT_PAGE_SIZE = 200
_MAX_EVENT_PAGES = 8


@dataclass(frozen=True, slots=True)
class RulesState:
    """Session-scope record of the workspace paths this session's tools touched.

    ``cursor_sequence`` is the last committed session event this owner consumed, so the
    public resource facts that feed ``touched_paths`` stay forward-only and bounded.
    """

    touched_paths: tuple[str, ...] = ()
    cursor_sequence: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, object] | None) -> RulesState:
        if payload is None:
            return cls()
        unknown = sorted(str(key) for key in payload if key not in _RULES_STATE_KEYS)
        if unknown:
            raise ValueError(f"skills state field '{unknown[0]}' is not supported")
        missing = _RULES_STATE_KEYS - payload.keys()
        if missing:
            raise ValueError("skills state is missing field(s): " + ", ".join(sorted(missing)))
        if type(payload["schema_version"]) is not int or payload["schema_version"] != RULES_STATE_SCHEMA_VERSION:
            raise ValueError(f"skills state schema_version must be {RULES_STATE_SCHEMA_VERSION}")
        cursor = payload["cursor_sequence"]
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("skills state cursor_sequence must be a non-negative integer")
        raw_paths = payload["touched_paths"]
        if not isinstance(raw_paths, (list, tuple)):
            raise ValueError("skills state touched_paths must be an array of strings")
        paths: list[str] = []
        for item in raw_paths:
            if not isinstance(item, str) or not item:
                raise ValueError("skills state touched_paths must contain non-empty strings")
            paths.append(item)
        if len(paths) > TOUCHED_PATHS_LIMIT:
            raise ValueError(f"skills state touched_paths must contain at most {TOUCHED_PATHS_LIMIT} entries")
        if len(paths) != len(set(paths)):
            raise ValueError("skills state touched_paths must not contain duplicates")
        return cls(touched_paths=tuple(paths), cursor_sequence=cursor)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": RULES_STATE_SCHEMA_VERSION,
            "cursor_sequence": self.cursor_sequence,
            "touched_paths": list(self.touched_paths),
        }


def _workspace_relative_path(raw_path: str, *, workspace_root: Path) -> str | None:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(workspace_root)
    except ValueError:
        return None
    return relative.as_posix()


def _record_touched_paths(state: RulesState, *, workspace_root: Path, raw_paths: Iterable[str]) -> RulesState:
    """Deduplicate into recency order, keep the most recent ``TOUCHED_PATHS_LIMIT`` entries."""
    touched = list(state.touched_paths)
    changed = False
    for raw_path in raw_paths:
        relative = _workspace_relative_path(raw_path, workspace_root=workspace_root)
        if relative is None:
            continue
        touched = [existing for existing in touched if existing != relative]
        touched.append(relative)
        changed = True
    if not changed:
        return state
    return replace(state, touched_paths=tuple(touched[-TOUCHED_PATHS_LIMIT:]))


def load_rules_state(services: StateServices) -> tuple[RulesState, int]:
    payload, version = services.get_state("session")
    return RulesState.from_payload(payload), version


def _consume_resource_facts(state: RulesState, services: HistoryServices) -> tuple[int, tuple[str, ...]]:
    """Read committed resource observations without depending on event or tool names."""
    cursor = state.cursor_sequence
    found: list[str] = []
    for _ in range(_MAX_EVENT_PAGES):
        page = services.read_facts(after_sequence=cursor, limit=_EVENT_PAGE_SIZE)
        for fact in page.facts:
            if isinstance(fact, InvocationFinishedFact):
                found.extend(fact.resource_paths)
        cursor = page.next_sequence
        if not page.has_more:
            break
    return cursor, tuple(found)


def skills_observer(fact: PluginFact, context: ToolContext, services: PluginServices) -> None:
    """Record the workspace paths each finished invocation touched, for the next dispatch."""
    if not isinstance(fact, InvocationFinishedFact):
        return
    state, version = load_rules_state(services)
    cursor, raw_paths = _consume_resource_facts(state, services)
    updated = _record_touched_paths(
        replace(state, cursor_sequence=cursor),
        workspace_root=context.workspace.resolve(strict=False),
        raw_paths=raw_paths,
    )
    if updated != state:
        services.replace_state(updated.to_payload(), expected_version=version, scope="session")


def prepare_skills_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: the observation cursor and recency list survive untouched.

    Both are plain history (paths, not leases) with no live resource behind them, so the
    parked copy keeps the payload as-is and re-enable resumes exactly where it left off.
    """
    _ = (scope, scope_key, tip_sequence)
    return state
