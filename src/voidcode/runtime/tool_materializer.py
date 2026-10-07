from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from ..mcp import McpToolDescriptor
from ..security.json_values import json_wire_value, own_json_value
from ..tools.contracts import ToolDefinition, is_read_tier
from ..tools.local_custom import LocalCustomToolManifest, local_custom_tool_definition, local_custom_tool_source_fingerprint
from ..tools.mcp import mcp_tool_definition
from .tool_registry import ToolRegistry

type RuntimeToolSourceKind = Literal["base", "mcp", "local", "package"]


@dataclass(frozen=True, slots=True)
class RuntimeToolProvenance:
    tool_name: str
    source_kind: RuntimeToolSourceKind
    source_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RuntimeToolMaterialization:
    registry: ToolRegistry
    provenance: tuple[RuntimeToolProvenance, ...]

    @property
    def generation(self) -> str:
        payload = [
            {
                "fingerprint": item.fingerprint,
                "source_id": item.source_id,
                "source_kind": item.source_kind,
                "tool_name": item.tool_name,
            }
            for item in self.provenance
        ]
        return _fingerprint(payload)

    def scoped(self, registry: ToolRegistry) -> RuntimeToolMaterialization:
        names = frozenset(registry.declarations)
        return RuntimeToolMaterialization(
            registry=registry,
            provenance=tuple(item for item in self.provenance if item.tool_name in names),
        )


@dataclass(frozen=True, slots=True)
class RuntimeToolMaterializer:
    """Compose declared runtime-owned sources without constructing dispatch tools."""

    base_registry: ToolRegistry
    base_provenance: tuple[RuntimeToolProvenance, ...]

    def __post_init__(self) -> None:
        names = tuple(item.tool_name for item in self.base_provenance)
        if len(names) != len(set(names)) or set(names) != set(self.base_registry.declarations):
            raise ValueError("base tool provenance must cover each declaration exactly once")

    def base(self) -> RuntimeToolMaterialization:
        return RuntimeToolMaterialization(
            registry=ToolRegistry(declarations=self.base_registry.declarations, tools=dict(self.base_registry.tools)),
            provenance=tuple(sorted(self.base_provenance, key=lambda item: item.tool_name)),
        )

    def materialize_mcp_descriptors(self, descriptors: Iterable[McpToolDescriptor]) -> RuntimeToolMaterialization:
        """Layer enabled actual observations admitted by the root's frozen ceiling."""
        materialized = self.base()
        merged = dict(materialized.registry.declarations)
        provenance = {item.tool_name: item for item in materialized.provenance}
        observed_names: set[str] = set()
        for descriptor in descriptors:
            if not descriptor.enabled:
                continue
            definition = mcp_tool_definition(descriptor)
            name = definition.name
            if name in observed_names or (name in provenance and provenance[name].source_kind != "mcp"):
                raise ValueError(f"duplicate tool definition: {name}")
            observed_names.add(name)
            merged[name] = definition
            provenance[name] = tool_provenance(definition, source_kind="mcp", source_id=f"mcp:{name}")
        registry = ToolRegistry.from_definitions(merged.values())
        registry.tools.update({name: tool for name, tool in materialized.registry.tools.items() if name not in observed_names})
        return RuntimeToolMaterialization(
            registry=registry,
            provenance=tuple(provenance[name] for name in sorted(provenance)),
        )

    @staticmethod
    def materialize_local_manifests(
        materialization: RuntimeToolMaterialization,
        manifests: Iterable[LocalCustomToolManifest],
    ) -> RuntimeToolMaterialization:
        local_manifests = tuple(manifests)
        if not local_manifests:
            return materialization
        local_definitions = tuple(local_custom_tool_definition(manifest) for manifest in local_manifests)
        registry = ToolRegistry.from_definitions((*materialization.registry.definitions(), *local_definitions))
        registry.tools.update(materialization.registry.tools)
        provenance = {item.tool_name: item for item in materialization.provenance}
        for manifest, definition in zip(local_manifests, local_definitions, strict=True):
            provenance[definition.name] = tool_provenance(
                definition,
                source_kind="local",
                source_id=f"local:{definition.name}",
                source_fingerprint=local_custom_tool_source_fingerprint(manifest),
            )
        return RuntimeToolMaterialization(
            registry=registry,
            provenance=tuple(provenance[name] for name in sorted(provenance)),
        )


def tool_provenance(
    definition: ToolDefinition,
    *,
    source_kind: RuntimeToolSourceKind,
    source_id: str,
    source_fingerprint: str | None = None,
) -> RuntimeToolProvenance:
    """Hash one actual source-owned declaration, never a constructed tool."""
    if source_kind not in ("base", "mcp", "local", "package"):
        raise ValueError("unsupported tool provenance source kind")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("tool provenance requires an actual non-empty source ID")
    if source_kind == "local" and not source_fingerprint:
        raise ValueError("local tool provenance requires its manifest source fingerprint")
    if source_fingerprint is not None and not isinstance(source_fingerprint, str):
        raise ValueError("tool source fingerprint must be a string")
    capability_payload: dict[str, object] = {
        "description": definition.description,
        "effects": sorted(effect.value for effect in definition.effects),
        "input_schema": definition.input_schema,
        "name": definition.name,
        "path_argument_keys": list(definition.path_argument_keys),
        "read_only": is_read_tier(definition.effects),
        "replay_policy": definition.replay_policy,
    }
    if source_fingerprint is not None:
        capability_payload["source_fingerprint"] = source_fingerprint
    return RuntimeToolProvenance(
        tool_name=definition.name,
        source_kind=source_kind,
        source_id=source_id,
        fingerprint=_fingerprint(capability_payload),
    )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        json_wire_value(own_json_value(payload)),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
