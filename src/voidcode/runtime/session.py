from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, cast

from .policy import runtime_policy_snapshot_from_session_metadata

type SessionStatus = Literal["idle", "running", "waiting", "completed", "failed"]
type SessionKind = Literal["top_level", "child"]

_PERSISTED_STRING_LIMIT = 1_000
_PERSISTED_LIST_LIMIT = 50
_PERSISTED_DICT_LIMIT = 100
_REDACTED = "<redacted>"
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "session_token",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{6,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^\s&]+)"),
)
_RUNTIME_MODES = frozenset({"normal", "analyze", "plan"})
_REDACTED_ENV_VALUE_KEYS = frozenset(
    {
        "env",
        "environment",
        "injected_env",
        "injected_env_values",
        "env_values",
    }
)


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _scrub_text(value: str) -> str:
    scrubbed = value
    for pattern in _SECRET_VALUE_PATTERNS:
        scrubbed = pattern.sub(_REDACTED, scrubbed)
    if len(scrubbed) <= _PERSISTED_STRING_LIMIT:
        return scrubbed
    return scrubbed[:_PERSISTED_STRING_LIMIT] + (
        f"\n... [truncated by runtime metadata: kept first "
        f"{_PERSISTED_STRING_LIMIT} of {len(scrubbed)} chars]"
    )


def _bounded_redacted(value: object, *, key: str | None = None) -> object:
    if key is not None and (_looks_secret_key(key) or key.lower() in _REDACTED_ENV_VALUE_KEYS):
        return _REDACTED
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for index, (raw_key, item) in enumerate(cast(dict[object, object], value).items()):
            if index >= _PERSISTED_DICT_LIMIT:
                result["__truncated__"] = True
                break
            item_key = str(raw_key)
            result[item_key] = _bounded_redacted(item, key=item_key)
        return result
    if isinstance(value, list):
        result = [
            _bounded_redacted(item) for item in cast(list[object], value[:_PERSISTED_LIST_LIMIT])
        ]
        if len(value) > _PERSISTED_LIST_LIMIT:
            result.append({"__truncated__": True, "original_length": len(value)})
        return result
    if isinstance(value, tuple):
        items = list(value)
        result = [_bounded_redacted(item) for item in items[:_PERSISTED_LIST_LIMIT]]
        if len(items) > _PERSISTED_LIST_LIMIT:
            result.append({"__truncated__": True, "original_length": len(items)})
        return result
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _runtime_mode(metadata: dict[str, object]) -> str:
    raw_mode = metadata.get("mode", "normal")
    if raw_mode in _RUNTIME_MODES:
        return cast(str, raw_mode)
    return "normal"


def _runtime_read_only(metadata: dict[str, object], *, mode: str) -> bool:
    if mode in {"analyze", "plan"}:
        return True
    raw_read_only = metadata.get("read_only", False)
    return raw_read_only if isinstance(raw_read_only, bool) else False


def _workflow_effective_read_only(metadata: dict[str, object], read_only: bool) -> bool:
    raw_workflow = metadata.get("workflow")
    if not isinstance(raw_workflow, dict):
        return read_only
    workflow = cast(dict[str, object], raw_workflow)
    if workflow.get("read_only_default") is True:
        read_only = True
    raw_effective = workflow.get("effective")
    if (
        isinstance(raw_effective, dict)
        and cast(dict[str, object], raw_effective).get("read_only_default") is True
    ):
        read_only = True
    return read_only


def session_metadata_for_replay(metadata: dict[str, object]) -> dict[str, object]:
    """Return session metadata projected for replay/resume without run-local markers."""

    projected = dict(metadata)
    projected.pop("_prompt_activation_this_run", None)
    raw_runtime_policy = projected.get("runtime_policy")
    if not isinstance(raw_runtime_policy, dict):
        return projected
    runtime_policy = dict(cast(dict[str, object], raw_runtime_policy))
    raw_prompt_activation = runtime_policy.get("prompt_activation")
    if isinstance(raw_prompt_activation, dict):
        runtime_policy["prompt_activation"] = {
            **cast(dict[str, object], raw_prompt_activation),
            "activated_this_turn": False,
        }
    projected["runtime_policy"] = runtime_policy
    return projected


def normalize_persisted_session_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Return persisted metadata with top-level runtime mode compatibility normalized."""

    normalized = dict(metadata)
    mode = _runtime_mode(normalized)
    if "mode" in normalized and normalized.get("mode") not in _RUNTIME_MODES:
        normalized["mode"] = mode
    raw_runtime_policy = normalized.get("runtime_policy")
    if isinstance(raw_runtime_policy, dict):
        runtime_policy = dict(cast(dict[str, object], raw_runtime_policy))
        if runtime_policy.get("mode") not in _RUNTIME_MODES:
            runtime_policy["mode"] = mode
        normalized["runtime_policy"] = runtime_policy
    return normalized


def _event_payload(event: object) -> dict[str, object]:
    payload = getattr(event, "payload", None)
    return cast(dict[str, object], payload) if isinstance(payload, dict) else {}


def _event_type(event: object) -> str:
    value = getattr(event, "event_type", "")
    return value if isinstance(value, str) else ""


def _event_sequence(event: object) -> int | None:
    value = getattr(event, "sequence", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _policy_observations(events: tuple[object, ...]) -> dict[str, object]:
    tool_policy_denial: dict[str, object] | None = None
    shell_policy_events: list[dict[str, object]] = []
    for event in events:
        event_type = _event_type(event)
        payload = _event_payload(event)
        if payload.get("kind") == "runtime_tool_policy_denied" and isinstance(
            payload.get("tool_policy"), dict
        ):
            tool_policy_denial = {
                "event_sequence": _event_sequence(event),
                **cast(dict[str, object], _bounded_redacted(payload["tool_policy"])),
            }
        if payload.get("policy_surface") == "shell_policy":
            shell_policy_events.append(
                {
                    "event_sequence": _event_sequence(event),
                    "event_type": event_type,
                    "tool": payload.get("tool"),
                    "path_scope": payload.get("path_scope"),
                    "operation_class": payload.get("operation_class"),
                    "matched_rule": _bounded_redacted(payload.get("matched_rule")),
                    "policy_surface": "shell_policy",
                }
            )
        if payload.get("tool") == "shell_exec" and "injected_env_keys" in payload:
            shell_policy_events.append(
                {
                    "event_sequence": _event_sequence(event),
                    "event_type": event_type,
                    "tool": "shell_exec",
                    "injected_env_keys": _bounded_redacted(payload.get("injected_env_keys")),
                }
            )
    observations: dict[str, object] = {}
    if tool_policy_denial is not None:
        observations["tool_policy_denial"] = tool_policy_denial
    if shell_policy_events:
        observations["shell_policy"] = shell_policy_events[-_PERSISTED_LIST_LIMIT:]
    return observations


def session_metadata_for_persistence(
    metadata: dict[str, object],
    *,
    events: tuple[object, ...] = (),
) -> dict[str, object]:
    """Return bounded, redacted session metadata safe for durable storage."""

    persisted = cast(dict[str, object], _bounded_redacted(metadata))
    persisted.pop("_prompt_activation_this_run", None)
    mode = _runtime_mode(persisted)
    read_only = _runtime_read_only(persisted, mode=mode)
    read_only = _workflow_effective_read_only(persisted, read_only)
    observations = _policy_observations(events)
    raw_persisted_runtime_policy = persisted.get("runtime_policy")
    if isinstance(raw_persisted_runtime_policy, dict):
        persisted["runtime_policy"] = runtime_policy_snapshot_from_session_metadata(persisted)
    has_policy_truth = (
        "mode" in metadata
        or "read_only" in metadata
        or "delegation" in metadata
        or "prompt_stack" in metadata
        or bool(observations)
        or mode != "normal"
        or read_only
    )
    if not has_policy_truth:
        return persisted

    persisted["mode"] = mode
    persisted["read_only"] = read_only

    if "runtime_policy" in persisted:
        raw_runtime_policy = persisted.get("runtime_policy")
        if observations and isinstance(raw_runtime_policy, dict):
            persisted["runtime_policy"] = {
                **cast(dict[str, object], raw_runtime_policy),
                **observations,
            }
    else:
        if observations:
            persisted["policy_observations"] = observations
        persisted["runtime_policy"] = runtime_policy_snapshot_from_session_metadata(persisted)
    return persisted


@dataclass(frozen=True, slots=True)
class SessionRef:
    id: str
    parent_id: str | None = None

    @property
    def kind(self) -> SessionKind:
        return "child" if self.parent_id is not None else "top_level"

    @property
    def is_child(self) -> bool:
        return self.parent_id is not None

    @property
    def is_top_level(self) -> bool:
        return self.parent_id is None


@dataclass(frozen=True, slots=True)
class SessionState:
    session: SessionRef
    status: SessionStatus = "idle"
    turn: int = 0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredSessionSummary:
    session: SessionRef
    status: SessionStatus
    turn: int
    prompt: str
    updated_at: int
