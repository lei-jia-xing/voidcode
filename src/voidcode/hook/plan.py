from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from .config import RuntimeHookFailureMode, RuntimeHooksConfig, RuntimeHookSurface
from .presets import resolve_hook_preset_refs

HOOK_PLAN_SCHEMA_VERSION = 2
HOOK_PLAN_REVISION = 1
HOOK_PAYLOAD_SCHEMA = "runtime.lifecycle.v1"
HookPlanScope = Literal["session"]
HookPlanPhase = Literal["foreground", "background"]

# This is the one descriptor used by materialization.  RuntimeHooksConfig remains
# the source of command declarations; no command is inferred from a preset.
_HOOK_SURFACE_PHASES: tuple[tuple[RuntimeHookSurface, HookPlanPhase], ...] = (
    ("pre_tool", "foreground"),
    ("post_tool", "foreground"),
    ("session_start", "foreground"),
    ("session_end", "foreground"),
    ("session_idle", "foreground"),
    ("background_task_registered", "background"),
    ("background_task_started", "background"),
    ("background_task_progress", "background"),
    ("background_task_completed", "background"),
    ("background_task_failed", "background"),
    ("background_task_cancelled", "background"),
    ("background_task_interrupted", "background"),
    ("background_task_notification_enqueued", "background"),
    ("background_task_result_read", "background"),
    ("delegated_result_available", "background"),
    ("turn_progress", "foreground"),
    ("stuck_detected", "foreground"),
)
_VALID_SURFACES = frozenset(surface for surface, _ in _HOOK_SURFACE_PHASES)
_BACKGROUND_SURFACES = frozenset(surface for surface, phase in _HOOK_SURFACE_PHASES if phase == "background")
_REMOVED_HOOK_BINDING_FIELDS = frozenset({"handler_ref", "priority"})


class HookPlanValidationError(ValueError):
    """Raised when declarative hook input cannot be made runtime-owned."""


@dataclass(frozen=True, slots=True)
class HookPlanBinding:
    binding_id: str
    event: RuntimeHookSurface
    command: tuple[str, ...]
    order: int
    scope: HookPlanScope = "session"
    source: str = "runtime_config"
    agent_source: str | None = None
    failure_mode: RuntimeHookFailureMode = "warn"
    timeout_seconds: float | None = 30.0
    payload_schema: str = HOOK_PAYLOAD_SCHEMA
    phase: HookPlanPhase = "foreground"
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command

    def as_payload(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "event": self.event,
            "command": list(self.command),
            "argv": list(self.command),
            "order": self.order,
            "scope": self.scope,
            "source": self.source,
            "agent_source": self.agent_source,
            "failure_mode": self.failure_mode,
            "timeout_seconds": self.timeout_seconds,
            "payload_schema": self.payload_schema,
            "phase": self.phase,
            "metadata": _json_safe_mapping(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ResolvedHookPlan:
    schema_version: int
    plan_id: str
    revision: int
    bindings: tuple[HookPlanBinding, ...]
    enabled: bool
    failure_mode: RuntimeHookFailureMode
    timeout_seconds: float | None
    metadata: Mapping[str, object] = field(default_factory=dict)
    plan_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != HOOK_PLAN_SCHEMA_VERSION:
            raise HookPlanValidationError("unsupported hook plan schema_version")
        if not self.plan_id.strip():
            raise HookPlanValidationError("hook plan plan_id must be non-empty")
        if self.revision < 1:
            raise HookPlanValidationError("hook plan revision must be >= 1")
        _validate_plan_bindings(self.bindings)
        expected_hash = _plan_hash(self._unsigned_payload())
        if self.plan_hash and self.plan_hash != expected_hash:
            raise HookPlanValidationError("hook plan hash does not match canonical snapshot")
        object.__setattr__(self, "plan_hash", expected_hash)

    def _unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "enabled": self.enabled,
            "failure_mode": self.failure_mode,
            "timeout_seconds": self.timeout_seconds,
            "bindings": [binding.as_payload() for binding in self.bindings],
            "metadata": _json_safe_mapping(self.metadata),
        }

    def to_payload(self) -> dict[str, object]:
        payload = self._unsigned_payload()
        payload["plan_hash"] = self.plan_hash
        return payload

    def commands_for_surface(self, surface: str) -> tuple[tuple[str, ...], ...]:
        _validate_surface(surface)
        return tuple(binding.command for binding in self.bindings if binding.event == surface)

    def binding_for_execution(self, surface: str) -> tuple[HookPlanBinding, ...]:
        _validate_surface(surface)
        return tuple(binding for binding in self.bindings if binding.event == surface)

    @classmethod
    def from_payload(cls, payload: object) -> ResolvedHookPlan:
        if not isinstance(payload, Mapping):
            raise HookPlanValidationError("persisted hook plan must be an object")
        raw = cast(Mapping[object, object], payload)
        required = {"schema_version", "plan_id", "revision", "bindings", "enabled", "failure_mode", "timeout_seconds", "metadata", "plan_hash"}
        missing = sorted(key for key in required if key not in raw)
        if missing:
            raise HookPlanValidationError("persisted hook plan missing required field(s): " + ", ".join(missing))
        raw_bindings = raw["bindings"]
        if not isinstance(raw_bindings, Sequence) or isinstance(raw_bindings, (str, bytes)):
            raise HookPlanValidationError("hook plan bindings must be an array")
        bindings = tuple(_binding_from_payload(item) for item in raw_bindings)
        metadata = raw["metadata"]
        if not isinstance(metadata, Mapping):
            raise HookPlanValidationError("hook plan metadata must be an object")
        if not isinstance(raw["schema_version"], int) or isinstance(raw["schema_version"], bool):
            raise HookPlanValidationError("hook plan schema_version must be an integer")
        if not isinstance(raw["plan_id"], str) or not isinstance(raw["revision"], int):
            raise HookPlanValidationError("hook plan identity fields are invalid")
        if not isinstance(raw["enabled"], bool) or raw["failure_mode"] not in {"warn", "fail"}:
            raise HookPlanValidationError("hook plan execution policy is invalid")
        timeout = raw["timeout_seconds"]
        if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0):
            raise HookPlanValidationError("hook plan timeout_seconds is invalid")
        plan_hash = raw["plan_hash"]
        if not isinstance(plan_hash, str) or not plan_hash:
            raise HookPlanValidationError("hook plan plan_hash must be a non-empty string")
        return cls(
            schema_version=cast(int, raw["schema_version"]),
            plan_id=raw["plan_id"],
            revision=raw["revision"],
            bindings=bindings,
            enabled=raw["enabled"],
            failure_mode=cast(RuntimeHookFailureMode, raw["failure_mode"]),
            timeout_seconds=cast(float | None, timeout),
            metadata=cast(Mapping[str, object], metadata),
            plan_hash=plan_hash,
        )


def materialize_hook_plan(
    hooks: RuntimeHooksConfig | None,
    *,
    agent_hook_refs: Sequence[str] = (),
    agent_source: str | None = None,
    scope: str = "session",
    plan_id: str = "runtime-hooks",
    revision: int = HOOK_PLAN_REVISION,
) -> ResolvedHookPlan:
    """Resolve config commands and advisory preset metadata into a frozen plan."""
    if scope != "session":
        raise HookPlanValidationError("hook plan scope must be 'session'; implicit scope inheritance is not supported")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise HookPlanValidationError("hook plan plan_id must be non-empty")
    if revision < 1:
        raise HookPlanValidationError("hook plan revision must be >= 1")
    hooks = hooks or RuntimeHooksConfig(enabled=False)
    try:
        preset_snapshot = resolve_hook_preset_refs(tuple(agent_hook_refs))
    except ValueError as exc:
        raise HookPlanValidationError(str(exc)) from exc
    bindings: list[HookPlanBinding] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    order = 0
    for surface, phase in _HOOK_SURFACE_PHASES:
        try:
            commands = hooks.commands_for_surface(surface)
        except (KeyError, ValueError) as exc:
            raise HookPlanValidationError(f"unknown hook surface: {surface}") from exc
        for command in commands:
            if not command or not all(isinstance(arg, str) and arg for arg in command):
                raise HookPlanValidationError(f"hook command for {surface} must be a non-empty argv")
            key = (surface, command)
            if key in seen:
                raise HookPlanValidationError(f"duplicate hook binding for {surface}: {command!r}")
            seen.add(key)
            order += 1
            bindings.append(
                HookPlanBinding(
                    binding_id=f"{scope}:{surface}:{order}",
                    event=surface,
                    command=tuple(command),
                    order=order,
                    scope=scope,
                    source="runtime_config",
                    agent_source=agent_source,
                    failure_mode=hooks.failure_mode,
                    timeout_seconds=hooks.timeout_seconds,
                    phase=phase,
                    metadata={"authority": "runtime", "preset_refs": list(preset_snapshot.refs)},
                )
            )
    metadata: dict[str, object] = {
        "scope": scope,
        "authority": "runtime",
        "agent_source": agent_source or "current session",
        "agent_hook_refs": list(preset_snapshot.refs),
        "preset_metadata": [
            {
                "ref": preset["ref"],
                "kind": preset["kind"],
                "source": preset["source"],
                "event_scopes": list(cast(tuple[str, ...], preset["event_scopes"])),
                "allowed_actions": list(cast(tuple[str, ...], preset["allowed_actions"])),
                "authority": "non_authoritative",
            }
            for preset in preset_snapshot.presets
        ],
        "preset_materialization": "guidance_only",
        "execution": "existing_hook_executor",
    }
    return ResolvedHookPlan(
        schema_version=HOOK_PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        revision=revision,
        bindings=tuple(bindings),
        enabled=hooks.enabled is True,
        failure_mode=hooks.failure_mode,
        timeout_seconds=hooks.timeout_seconds,
        metadata=metadata,
    )


def hook_plan_from_session_metadata(metadata: Mapping[str, object]) -> ResolvedHookPlan | None:
    raw = metadata.get("resolved_hook_plan")
    if isinstance(raw, Mapping):
        return ResolvedHookPlan.from_payload(raw)
    return None


def _validate_surface(surface: str) -> None:
    if surface not in _VALID_SURFACES:
        raise HookPlanValidationError(f"unknown hook event/surface: {surface}")


def _validate_plan_bindings(bindings: Sequence[HookPlanBinding]) -> None:
    seen_ids: set[str] = set()
    seen_bindings: set[tuple[str, tuple[str, ...]]] = set()
    expected_order = 1
    for binding in bindings:
        _validate_surface(binding.event)
        if not binding.binding_id.strip() or binding.binding_id in seen_ids:
            raise HookPlanValidationError(f"duplicate or empty hook binding id: {binding.binding_id!r}")
        if binding.scope != "session":
            raise HookPlanValidationError(f"unsupported hook binding scope: {binding.scope!r}")
        if binding.failure_mode not in {"warn", "fail"}:
            raise HookPlanValidationError("invalid hook binding failure policy")
        if binding.phase not in {"foreground", "background"}:
            raise HookPlanValidationError("invalid hook binding phase")
        if binding.payload_schema != HOOK_PAYLOAD_SCHEMA:
            raise HookPlanValidationError("unsupported hook payload schema")
        if binding.order != expected_order:
            raise HookPlanValidationError("hook bindings must have deterministic contiguous order")
        if not binding.command or not all(isinstance(arg, str) and arg for arg in binding.command):
            raise HookPlanValidationError("hook binding command must be a non-empty argv")
        duplicate_key = (binding.event, binding.command)
        if duplicate_key in seen_bindings:
            raise HookPlanValidationError(f"duplicate hook binding for {binding.event}: {binding.command!r}")
        seen_ids.add(binding.binding_id)
        seen_bindings.add(duplicate_key)
        expected_order += 1


def _binding_from_payload(payload: object) -> HookPlanBinding:
    if not isinstance(payload, Mapping):
        raise HookPlanValidationError("hook plan binding must be an object")
    raw = cast(Mapping[object, object], payload)
    removed = sorted(field for field in _REMOVED_HOOK_BINDING_FIELDS if field in raw)
    if removed:
        raise HookPlanValidationError("hook plan binding contains removed field(s): " + ", ".join(removed))
    command = raw.get("command", raw.get("argv"))
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
        raise HookPlanValidationError("hook plan binding command must be an argv array")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise HookPlanValidationError("hook plan binding metadata must be an object")
    event = raw.get("event")
    if not isinstance(event, str):
        raise HookPlanValidationError("hook plan binding event must be a string")
    return HookPlanBinding(
        binding_id=cast(str, raw.get("binding_id")),
        event=cast(RuntimeHookSurface, event),
        command=tuple(cast(str, arg) for arg in command),
        order=cast(int, raw.get("order")),
        scope=cast(HookPlanScope, raw.get("scope", "session")),
        source=cast(str, raw.get("source", "runtime_config")),
        agent_source=cast(str | None, raw.get("agent_source")),
        failure_mode=cast(RuntimeHookFailureMode, raw.get("failure_mode", "warn")),
        timeout_seconds=cast(float | None, raw.get("timeout_seconds")),
        payload_schema=cast(str, raw.get("payload_schema", HOOK_PAYLOAD_SCHEMA)),
        phase=cast(HookPlanPhase, raw.get("phase", "foreground")),
        metadata=cast(Mapping[str, object], metadata),
    )


def _plan_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
        parsed = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise HookPlanValidationError("hook plan metadata must be JSON-serializable") from exc
    if not isinstance(parsed, dict):
        raise HookPlanValidationError("hook plan metadata must be an object")
    return cast(dict[str, object], parsed)


__all__ = [
    "HOOK_PAYLOAD_SCHEMA",
    "HOOK_PLAN_REVISION",
    "HOOK_PLAN_SCHEMA_VERSION",
    "HookPlanBinding",
    "HookPlanPhase",
    "HookPlanScope",
    "HookPlanValidationError",
    "ResolvedHookPlan",
    "hook_plan_from_session_metadata",
    "materialize_hook_plan",
]
