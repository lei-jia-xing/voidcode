from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from ..tools.contracts import ToolResult
from .context_rules import runtime_file_rule_contexts


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformInjection:
    role: str
    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformTrace:
    provider_id: str
    status: str = "ok"
    injection_count: int = 0
    sources: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    error: str | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider_id": self.provider_id,
            "status": self.status,
            "injection_count": self.injection_count,
            "sources": list(self.sources),
        }
        if self.diagnostics:
            payload["diagnostics"] = list(self.diagnostics)
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformResult:
    injections: tuple[RuntimeContextTransformInjection, ...] = ()
    traces: tuple[RuntimeContextTransformTrace, ...] = ()

    def metadata_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "applied": [trace.metadata_payload() for trace in self.traces],
        }


def build_provider_context_transform_result(
    *,
    workspace: Path | None,
    tool_results: tuple[ToolResult, ...],
    hook_preset_context: str,
) -> RuntimeContextTransformResult:
    injections: list[RuntimeContextTransformInjection] = []
    traces: list[RuntimeContextTransformTrace] = []

    normalized_hook_preset_context = hook_preset_context.strip()
    if normalized_hook_preset_context:
        injections.append(
            RuntimeContextTransformInjection(
                role="system",
                content=normalized_hook_preset_context,
                metadata={"source": "hook_preset_guidance"},
            )
        )
        traces.append(
            RuntimeContextTransformTrace(
                provider_id="hook_preset_guidance",
                injection_count=1,
                sources=("hook_preset_guidance",),
            )
        )

    rule_segments: list[RuntimeContextTransformInjection] = []
    for rule_context in runtime_file_rule_contexts(
        workspace=workspace,
        tool_results=tool_results,
    ):
        rule_segments.append(
            RuntimeContextTransformInjection(
                role="system",
                content=(
                    "Runtime file rules are active for touched workspace paths.\n"
                    f"Rule file: {rule_context.path}\n"
                    f"{rule_context.content}"
                ).strip(),
                metadata=rule_context.metadata_payload(),
            )
        )
    injections.extend(rule_segments)
    if rule_segments:
        traces.append(
            RuntimeContextTransformTrace(
                provider_id="runtime_file_rules",
                injection_count=len(rule_segments),
                sources=("runtime_file_rules",),
            )
        )

    return RuntimeContextTransformResult(injections=tuple(injections), traces=tuple(traces))


def context_transform_metadata_from_payload(
    payload: object,
) -> Mapping[str, object] | None:
    if not isinstance(payload, dict):
        return None
    typed_payload = cast(dict[str, object], payload)
    applied = typed_payload.get("applied")
    if not isinstance(applied, list):
        return None
    return typed_payload
