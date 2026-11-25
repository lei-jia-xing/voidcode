"""Durable session state: the children this parent started and their groups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from voidcode.capabilities.plugin_state import PluginStateStore
from voidcode.sdk.child_schema import canonical_result_schema
from voidcode.sdk.services import StateServices

from .constants import _CHILD_KEYS, SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ChildRecord:
    """One child session this session started."""

    label: str = ""
    group_id: str | None = None
    subagent_type: str | None = None
    #: Declared result schema for structured delivery; None means undeclared (unknown, never valid).
    result_schema: Mapping[str, object] | None = None

    @classmethod
    def parse(cls, child_id: str, raw: object) -> ChildRecord:
        if not isinstance(raw, Mapping):
            raise ValueError(f"delegation child {child_id!r} must be an object")
        unknown = sorted(str(key) for key in raw if key not in _CHILD_KEYS)
        if unknown:
            raise ValueError(f"delegation child {child_id!r} field '{unknown[0]}' is not supported")
        missing = sorted(_CHILD_KEYS - raw.keys())
        if missing:
            raise ValueError(f"delegation child {child_id!r} is missing field(s): " + ", ".join(missing))
        label = raw["label"]
        if not isinstance(label, str):
            raise ValueError(f"delegation child {child_id!r} label must be a string")
        group_id = raw["group_id"]
        if group_id is not None and (not isinstance(group_id, str) or not group_id):
            raise ValueError(f"delegation child {child_id!r} group_id must be a non-empty string or null")
        subagent_type = raw.get("subagent_type")
        if subagent_type is not None and (not isinstance(subagent_type, str) or not subagent_type):
            raise ValueError(f"delegation child {child_id!r} subagent_type must be a non-empty string or null")
        schema_raw = raw.get("result_schema")
        result_schema: Mapping[str, object] | None = None
        if schema_raw is not None:
            if not isinstance(schema_raw, Mapping):
                raise ValueError(f"delegation child {child_id!r} result_schema must be an object or null")
            try:
                result_schema = canonical_result_schema(schema_raw)
            except ValueError as exc:
                raise ValueError(f"delegation child {child_id!r} {exc}") from exc
        return cls(label=label, group_id=group_id, subagent_type=subagent_type, result_schema=result_schema)

    def as_payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "group_id": self.group_id,
            "subagent_type": self.subagent_type,
            "result_schema": self.result_schema,
        }


@dataclass(frozen=True, slots=True)
class DelegationState:
    """This plugin's session-scope state: the children it started and their groups."""

    children: Mapping[str, ChildRecord] = field(default_factory=dict)
    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> DelegationState:
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"delegation state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        raw_children = raw["children"]
        if not isinstance(raw_children, Mapping):
            raise ValueError("delegation state requires a children object")
        children: dict[str, ChildRecord] = {}
        for child_id, record in raw_children.items():
            if not isinstance(child_id, str) or not child_id:
                raise ValueError("delegation state child ids must be non-empty strings")
            children[child_id] = ChildRecord.parse(child_id, record)
        raw_groups = raw["groups"]
        if not isinstance(raw_groups, Mapping):
            raise ValueError("delegation state requires a groups object")
        groups: dict[str, tuple[str, ...]] = {}
        for group_id, members_raw in raw_groups.items():
            if not isinstance(group_id, str) or not group_id:
                raise ValueError("delegation state group ids must be non-empty strings")
            if not isinstance(members_raw, (list, tuple)):
                raise ValueError(f"delegation state group {group_id!r} requires an array of child ids")
            members: list[str] = []
            for member in members_raw:
                if not isinstance(member, str) or not member:
                    raise ValueError(f"delegation state group {group_id!r} members must be non-empty child ids")
                if member not in children:
                    raise ValueError(f"delegation state group {group_id!r} references unknown child {member!r}")
                if member in members:
                    raise ValueError(f"delegation state group {group_id!r} lists child {member!r} twice")
                if children[member].group_id != group_id:
                    raise ValueError(f"delegation state group {group_id!r} lists child {member!r} that belongs to another group")
                members.append(member)
            groups[group_id] = tuple(members)
        for child_id, record in children.items():
            if record.group_id is not None and child_id not in groups.get(record.group_id, ()):
                raise ValueError(f"delegation child {child_id!r} is not listed in its group {record.group_id!r}")
        return cls(children=children, groups=groups)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "children": {child_id: record.as_payload() for child_id, record in self.children.items()},
            "groups": {group_id: list(members) for group_id, members in self.groups.items()},
        }


def _store(services: StateServices) -> PluginStateStore[DelegationState]:
    """This plugin's session-state store: strict keys, typed parse, CAS save."""
    return PluginStateStore(
        services,
        keys=frozenset({"schema_version", "children", "groups"}),
        parse=DelegationState.from_payload,
        serialize=DelegationState.to_payload,
        empty=DelegationState,
        unknown=lambda extra: f"delegation state field '{extra[0]}' is not supported",
        missing=lambda absent: "delegation state is missing field(s): " + ", ".join(absent),
    )


def _with_children(state: DelegationState, created: Sequence[tuple[str, ChildRecord]], *, group_id: str | None) -> DelegationState:
    """Record freshly spawned children, as one group when the caller owns one."""
    children = {**state.children, **dict(created)}
    groups = dict(state.groups)
    if group_id is not None:
        groups[group_id] = tuple(child_id for child_id, _record in created)
    return DelegationState(children=children, groups=groups)
