from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from ..tools.contracts import Tool
from ..tools.local_custom import LocalCustomTool
from .tool_registry import ToolRegistry

type RuntimeToolSourceKind = Literal["base", "mcp", "local"]


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
        names = frozenset(registry.tools)
        return RuntimeToolMaterialization(
            registry=registry,
            provenance=tuple(item for item in self.provenance if item.tool_name in names),
        )


@dataclass(frozen=True, slots=True)
class RuntimeToolMaterializer:
    """Compose runtime-owned tool sources without owning their lifecycle."""

    base_registry: ToolRegistry

    def base(self) -> RuntimeToolMaterialization:
        return RuntimeToolMaterialization(
            registry=ToolRegistry(tools=dict(self.base_registry.tools)),
            provenance=tuple(_provenance(tool, source_kind="base") for _, tool in sorted(self.base_registry.tools.items())),
        )

    def materialize_mcp_tools(self, tools: Iterable[Tool]) -> RuntimeToolMaterialization:
        materialized = self.base()
        merged = dict(materialized.registry.tools)
        provenance = {item.tool_name: item for item in materialized.provenance}
        for tool in tools:
            name = tool.definition.name
            merged[name] = tool
            provenance[name] = _provenance(tool, source_kind="mcp")
        return RuntimeToolMaterialization(
            registry=ToolRegistry(tools=merged),
            provenance=tuple(provenance[name] for name in sorted(provenance)),
        )

    @staticmethod
    def materialize_local_tools(
        materialization: RuntimeToolMaterialization,
        tools: Iterable[Tool],
    ) -> RuntimeToolMaterialization:
        local_tools = tuple(tools)
        if not local_tools:
            return materialization
        registry = ToolRegistry.from_tools((*materialization.registry.tools.values(), *local_tools))
        provenance = {item.tool_name: item for item in materialization.provenance}
        provenance.update((tool.definition.name, _provenance(tool, source_kind="local")) for tool in local_tools)
        return RuntimeToolMaterialization(
            registry=registry,
            provenance=tuple(provenance[name] for name in sorted(provenance)),
        )


def _provenance(tool: Tool, *, source_kind: RuntimeToolSourceKind) -> RuntimeToolProvenance:
    definition = tool.definition
    capability_payload: dict[str, object] = {
        "description": definition.description,
        "input_schema": definition.input_schema,
        "name": definition.name,
        "path_argument_keys": list(definition.path_argument_keys),
        "read_only": definition.read_only,
    }
    if source_kind == "local" and isinstance(tool, LocalCustomTool):
        capability_payload["source_fingerprint"] = tool.source_fingerprint
    return RuntimeToolProvenance(
        tool_name=definition.name,
        source_kind=source_kind,
        source_id=f"{source_kind}:{definition.name}",
        fingerprint=_fingerprint(capability_payload),
    )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
