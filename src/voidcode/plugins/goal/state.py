"""Goal session state, strict schema, and typed parsing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from voidcode.capabilities.plugin_state import PluginStateStore
from voidcode.sdk.services import StateServices

SCHEMA_VERSION = 2

GoalStatus = Literal["active", "paused", "blocked", "usage-limited", "budget-limited", "complete"]
VALID_GOAL_STATUSES: frozenset[GoalStatus] = frozenset({"active", "paused", "blocked", "usage-limited", "budget-limited", "complete"})

#: Statuses whose goal is hurt by a disable, so a deactivation pauses them.
#: `complete` is a terminal user-visible fact and is never rewritten by a deactivation.
DEACTIVATABLE_GOAL_STATUSES: frozenset[GoalStatus] = frozenset({"active", "blocked", "usage-limited", "budget-limited"})

UNBOUND_RUN_PREFIX = "unbound:"


def unbound_run_id(goal_id: str) -> str:
    """A non-run sentinel that cannot match a real Core run id (UUID hex)."""
    return f"{UNBOUND_RUN_PREFIX}{goal_id}"


KNOWN_GOAL_STATE_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "goal_id",
        "active_run_id",
        "retired_run_id",
        "status",
        "objective",
        "objective_file_ref",
        "objective_file_sha256",
        "token_budget",
        "tokens_used",
        "window_tokens_used",
        "window_allowance",
        "has_unknown_usage",
        "time_used_seconds",
        "cost_usd",
        "created_at",
        "updated_at",
        "active_windows",
        "blocked_turns_count",
        "last_blocked_logical_turn",
        "objective_edited",
    }
)

#: Activation validates stored copies against this schema before admitting the same owner.
#: The cleared row is the explicit empty object the store writes for a cleared goal.
GOAL_STATE_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "anyOf": [
        {"type": "object", "maxProperties": 0},
        {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(KNOWN_GOAL_STATE_FIELDS),
            "properties": {
                "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                "goal_id": {"type": "string", "minLength": 1},
                "active_run_id": {"type": ["string", "null"]},
                "retired_run_id": {"type": ["string", "null"]},
                "status": {"type": "string", "enum": sorted(VALID_GOAL_STATUSES)},
                "objective": {"type": "string", "minLength": 1},
                "objective_file_ref": {"type": ["string", "null"], "pattern": "^artifact://"},
                "objective_file_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
                "token_budget": {"type": ["integer", "null"], "minimum": 1},
                "tokens_used": {"type": ["integer", "null"], "minimum": 0},
                "window_tokens_used": {"type": ["integer", "null"], "minimum": 0},
                "window_allowance": {"type": ["integer", "null"], "minimum": 0},
                "has_unknown_usage": {"type": "boolean"},
                "time_used_seconds": {"type": ["number", "null"], "minimum": 0},
                "cost_usd": {"type": ["number", "null"], "minimum": 0},
                "created_at": {"type": "integer"},
                "updated_at": {"type": "integer"},
                "active_windows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start_sequence", "end_sequence"],
                        "properties": {
                            "start_sequence": {"type": "integer", "minimum": 0},
                            "end_sequence": {"type": ["integer", "null"], "minimum": 0},
                        },
                    },
                },
                "blocked_turns_count": {"type": "integer", "minimum": 0},
                "last_blocked_logical_turn": {"type": ["string", "null"]},
                "objective_edited": {"type": "boolean"},
            },
        },
    ],
}


@dataclass(frozen=True, slots=True)
class ActiveWindow:
    """A contiguous range of dispatch sequences during which the goal was active."""

    start_sequence: int
    end_sequence: int | None = None

    def contains(self, dispatch_sequence: int) -> bool:
        if dispatch_sequence <= self.start_sequence:
            return False
        if self.end_sequence is not None and dispatch_sequence > self.end_sequence:
            return False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ActiveWindow:
        if not isinstance(data, Mapping):
            raise ValueError("ActiveWindow data must be an object")
        extra = set(data.keys()) - {"start_sequence", "end_sequence"}
        if extra:
            raise ValueError(f"ActiveWindow contains unknown extra keys: {sorted(extra)}")
        if "start_sequence" not in data:
            raise ValueError("ActiveWindow requires start_sequence")
        start = data["start_sequence"]
        if not isinstance(start, int) or isinstance(start, bool):
            raise ValueError(f"ActiveWindow start_sequence must be an integer: {start!r}")
        if "end_sequence" not in data:
            raise ValueError("ActiveWindow requires end_sequence key (null when open)")
        end = data["end_sequence"]
        if end is not None and (not isinstance(end, int) or isinstance(end, bool)):
            raise ValueError(f"ActiveWindow end_sequence must be an integer or None: {end!r}")
        if start < 0:
            raise ValueError(f"ActiveWindow start_sequence must be non-negative: {start!r}")
        if end is not None and end < 0:
            raise ValueError(f"ActiveWindow end_sequence must be non-negative or None: {end!r}")
        if end is not None and end < start:
            raise ValueError(f"ActiveWindow end_sequence {end!r} precedes start_sequence {start!r}")
        return cls(start_sequence=start, end_sequence=end)


def _parse_goal_metric_int(data: Mapping[str, object], name: str) -> int | None:
    if name not in data:
        raise ValueError(f"GoalState requires {name} key (null when unreported)")
    v = data[name]
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ValueError(f"{name} must be a non-negative integer or null: {v!r}")
    return v


def _parse_goal_metric_float(data: Mapping[str, object], name: str) -> float | None:
    if name not in data:
        raise ValueError(f"GoalState requires {name} key (null when unreported)")
    v = data[name]
    if v is None:
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or float(v) < 0.0:
        raise ValueError(f"{name} must be a non-negative number or null: {v!r}")
    return float(v)


def _parse_goal_windows(raw: object) -> tuple[ActiveWindow, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("active_windows must be a list")
    windows: list[ActiveWindow] = []
    previous: ActiveWindow | None = None
    for w in raw:
        if not isinstance(w, Mapping):
            raise ValueError("active_windows entries must be objects")
        window = ActiveWindow.from_dict(w)
        if previous is not None:
            if previous.end_sequence is None:
                raise ValueError("active_windows may contain at most one open window, and it must be last")
            if window.start_sequence < previous.start_sequence:
                raise ValueError("active_windows must be ordered by ascending start_sequence")
            if window.start_sequence < previous.end_sequence:
                raise ValueError(
                    f"active_windows overlap: window starting at {window.start_sequence} begins before "
                    f"the previous window closes at {previous.end_sequence}"
                )
        windows.append(window)
        previous = window
    return tuple(windows)


def _parse_goal_artifact_ref(data: Mapping[str, object]) -> tuple[str | None, str | None]:
    if "objective_file_ref" not in data:
        raise ValueError("GoalState requires objective_file_ref key (null when inline)")
    file_ref = data["objective_file_ref"]
    if file_ref is not None and (not isinstance(file_ref, str) or not file_ref.startswith("artifact://")):
        raise ValueError("objective_file_ref must be an artifact:// URI or null")

    if "objective_file_sha256" not in data:
        raise ValueError("GoalState requires objective_file_sha256 key (null when inline)")
    file_sha = data["objective_file_sha256"]
    if file_sha is not None and (not isinstance(file_sha, str) or len(file_sha) != 64):
        raise ValueError("objective_file_sha256 must be a 64-char hex string or null")
    if (file_ref is None) != (file_sha is None):
        raise ValueError("objective_file_ref and objective_file_sha256 must both be set or both be null")
    return file_ref, file_sha


@dataclass(frozen=True, slots=True)
class GoalState:
    """Strongly typed session state for an active, paused, or completed goal."""

    goal_id: str
    status: GoalStatus
    objective: str
    created_at: int
    updated_at: int
    active_run_id: str | None = None
    retired_run_id: str | None = None
    objective_file_ref: str | None = None
    objective_file_sha256: str | None = None
    token_budget: int | None = None
    tokens_used: int | None = 0
    window_tokens_used: int | None = 0
    window_allowance: int | None = None
    has_unknown_usage: bool = False
    time_used_seconds: float | None = 0.0
    cost_usd: float | None = 0.0
    active_windows: tuple[ActiveWindow, ...] = ()
    blocked_turns_count: int = 0
    last_blocked_logical_turn: str | None = None
    objective_edited: bool = False
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in VALID_GOAL_STATUSES:
            raise ValueError(f"Invalid goal status: {self.status!r}; must be one of {sorted(VALID_GOAL_STATUSES)}")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("Goal objective must be a non-empty string")
        if self.token_budget is not None:
            if not isinstance(self.token_budget, int) or isinstance(self.token_budget, bool) or self.token_budget <= 0:
                raise ValueError(f"Goal token_budget must be a positive integer: {self.token_budget!r}")
        if self.active_run_id is not None and (not isinstance(self.active_run_id, str) or not self.active_run_id):
            raise ValueError("Goal active_run_id must be a non-empty string or null")
        if self.retired_run_id is not None and (not isinstance(self.retired_run_id, str) or not self.retired_run_id):
            raise ValueError("Goal retired_run_id must be a non-empty string or null")

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def remaining_tokens(self) -> int | None:
        if self.token_budget is None or self.tokens_used is None:
            return None
        return max(0, self.token_budget - self.tokens_used)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "active_run_id": self.active_run_id,
            "retired_run_id": self.retired_run_id,
            "status": self.status,
            "objective": self.objective,
            "objective_file_ref": self.objective_file_ref,
            "objective_file_sha256": self.objective_file_sha256,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "window_tokens_used": self.window_tokens_used,
            "window_allowance": self.window_allowance,
            "has_unknown_usage": self.has_unknown_usage,
            "time_used_seconds": self.time_used_seconds,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_windows": [w.to_dict() for w in self.active_windows],
            "blocked_turns_count": self.blocked_turns_count,
            "last_blocked_logical_turn": self.last_blocked_logical_turn,
            "objective_edited": self.objective_edited,
        }

    def tool_payload(self, session_id: str | None) -> dict[str, object]:
        """Model-facing goal details shared by create, get and update."""
        return {
            "goal": {
                "threadId": session_id,
                "objective": self.objective,
                "status": self.status,
                "tokenBudget": self.token_budget,
                "tokensUsed": self.tokens_used,
                "timeUsedSeconds": self.time_used_seconds,
                "createdAt": self.created_at,
                "updatedAt": self.updated_at,
            },
            "remainingTokens": self.remaining_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> GoalState | None:
        if data is None:
            return None
        if not isinstance(data, Mapping):
            raise ValueError("GoalState data must be a mapping")
        if not data:
            # The cleared owner row is an explicit empty object, never a generic falsey value.
            return None
        if "schema_version" not in data:
            raise ValueError("GoalState requires schema_version")
        schema_ver = data["schema_version"]
        if not isinstance(schema_ver, int) or isinstance(schema_ver, bool) or schema_ver != SCHEMA_VERSION:
            raise ValueError(f"Unsupported goal schema_version: {schema_ver!r} (expected {SCHEMA_VERSION})")

        goal_id = data.get("goal_id")
        if not isinstance(goal_id, str) or not goal_id:
            raise ValueError("GoalState requires non-empty string goal_id")

        if "active_run_id" not in data:
            raise ValueError("GoalState requires active_run_id key (null when no run is bound)")
        raw_active_run = data["active_run_id"]
        if raw_active_run is not None and (not isinstance(raw_active_run, str) or not raw_active_run):
            raise ValueError("GoalState active_run_id must be a non-empty string or null")
        if "retired_run_id" not in data:
            raise ValueError("GoalState requires retired_run_id key (null when none)")
        raw_retired_run = data["retired_run_id"]
        if raw_retired_run is not None and (not isinstance(raw_retired_run, str) or not raw_retired_run):
            raise ValueError("GoalState retired_run_id must be a non-empty string or null")

        raw_status = data.get("status")
        if not isinstance(raw_status, str) or raw_status not in VALID_GOAL_STATUSES:
            raise ValueError(f"Invalid or missing goal status: {raw_status!r}")

        raw_obj = data.get("objective")
        if not isinstance(raw_obj, str) or not raw_obj.strip():
            raise ValueError("GoalState requires non-empty string objective")

        raw_created = data.get("created_at")
        if not isinstance(raw_created, int) or isinstance(raw_created, bool):
            raise ValueError(f"GoalState requires integer created_at: {raw_created!r}")

        raw_updated = data.get("updated_at")
        if not isinstance(raw_updated, int) or isinstance(raw_updated, bool):
            raise ValueError(f"GoalState requires integer updated_at: {raw_updated!r}")

        if "token_budget" not in data:
            raise ValueError("GoalState requires token_budget key (null when unbounded)")
        raw_budget = data["token_budget"]
        budget: int | None = None
        if raw_budget is not None:
            if not isinstance(raw_budget, int) or isinstance(raw_budget, bool) or raw_budget <= 0:
                raise ValueError(f"Goal token_budget must be a positive integer or null: {raw_budget!r}")
            budget = raw_budget

        file_ref, file_sha = _parse_goal_artifact_ref(data)

        if "active_windows" not in data:
            raise ValueError("GoalState requires active_windows key")
        windows = _parse_goal_windows(data["active_windows"])

        if "blocked_turns_count" not in data:
            raise ValueError("GoalState requires blocked_turns_count key")
        raw_blocked = data["blocked_turns_count"]
        if not isinstance(raw_blocked, int) or isinstance(raw_blocked, bool) or raw_blocked < 0:
            raise ValueError(f"blocked_turns_count must be a non-negative integer: {raw_blocked!r}")

        if "last_blocked_logical_turn" not in data:
            raise ValueError("GoalState requires last_blocked_logical_turn key (null when none)")
        last_turn = data["last_blocked_logical_turn"]
        if last_turn is not None and not isinstance(last_turn, str):
            raise ValueError("last_blocked_logical_turn must be a string or null")

        if "has_unknown_usage" not in data:
            raise ValueError("GoalState requires has_unknown_usage key")
        raw_unknown = data["has_unknown_usage"]
        if not isinstance(raw_unknown, bool):
            raise ValueError(f"has_unknown_usage must be a boolean: {raw_unknown!r}")

        if "objective_edited" not in data:
            raise ValueError("GoalState requires objective_edited key")
        raw_objective_edited = data["objective_edited"]
        if not isinstance(raw_objective_edited, bool):
            raise ValueError(f"objective_edited must be a boolean: {raw_objective_edited!r}")

        return cls(
            goal_id=goal_id,
            active_run_id=raw_active_run,
            retired_run_id=raw_retired_run,
            status=raw_status,
            objective=raw_obj,
            created_at=raw_created,
            updated_at=raw_updated,
            objective_file_ref=file_ref,
            objective_file_sha256=file_sha,
            token_budget=budget,
            tokens_used=_parse_goal_metric_int(data, "tokens_used"),
            window_tokens_used=_parse_goal_metric_int(data, "window_tokens_used"),
            window_allowance=_parse_goal_metric_int(data, "window_allowance"),
            has_unknown_usage=raw_unknown,
            time_used_seconds=_parse_goal_metric_float(data, "time_used_seconds"),
            cost_usd=_parse_goal_metric_float(data, "cost_usd"),
            active_windows=windows,
            blocked_turns_count=raw_blocked,
            last_blocked_logical_turn=last_turn,
            objective_edited=raw_objective_edited,
            schema_version=schema_ver,
        )


def _unknown_state_field(extra: list[str]) -> str:
    return f"GoalState contains unknown extra fields: {extra}"


def _store(services: StateServices) -> PluginStateStore[GoalState | None]:
    """This plugin's session-state store: strict keys, typed parse, CAS save."""
    return PluginStateStore(
        services,
        keys=KNOWN_GOAL_STATE_FIELDS,
        parse=GoalState.from_dict,
        serialize=lambda goal: goal.to_dict() if goal is not None else {},
        empty=lambda: None,
        unknown=_unknown_state_field,
    )
