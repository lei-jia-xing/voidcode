from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

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


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformRequest:
    workspace: Path | None
    tool_results: tuple[ToolResult, ...]
    hook_preset_context: str


class RuntimeContextTransformProvider(Protocol):
    provider_id: str

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult: ...


class HookPresetGuidanceTransformProvider:
    provider_id = "hook_preset_guidance"

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult:
        normalized_hook_preset_context = request.hook_preset_context.strip()
        if not normalized_hook_preset_context:
            return RuntimeContextTransformResult()
        return RuntimeContextTransformResult(
            injections=(
                RuntimeContextTransformInjection(
                    role="system",
                    content=normalized_hook_preset_context,
                    metadata={"source": self.provider_id},
                ),
            ),
            traces=(
                RuntimeContextTransformTrace(
                    provider_id=self.provider_id,
                    injection_count=1,
                    sources=(self.provider_id,),
                ),
            ),
        )


class RuntimeFileRulesTransformProvider:
    provider_id = "runtime_file_rules"

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult:
        rule_segments: list[RuntimeContextTransformInjection] = []
        for rule_context in runtime_file_rule_contexts(
            workspace=request.workspace,
            tool_results=request.tool_results,
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
        if not rule_segments:
            return RuntimeContextTransformResult()
        return RuntimeContextTransformResult(
            injections=tuple(rule_segments),
            traces=(
                RuntimeContextTransformTrace(
                    provider_id=self.provider_id,
                    injection_count=len(rule_segments),
                    sources=(self.provider_id,),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformRegistry:
    providers: tuple[RuntimeContextTransformProvider, ...] = ()

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult:
        injections: list[RuntimeContextTransformInjection] = []
        traces: list[RuntimeContextTransformTrace] = []
        for provider in self.providers:
            result = provider.build_result(request)
            injections.extend(result.injections)
            traces.extend(result.traces)
        return RuntimeContextTransformResult(
            injections=tuple(injections),
            traces=tuple(traces),
        )


def default_runtime_context_transform_registry() -> RuntimeContextTransformRegistry:
    return RuntimeContextTransformRegistry(
        providers=(
            HookPresetGuidanceTransformProvider(),
            RuntimeFileRulesTransformProvider(),
        )
    )


def build_provider_context_transform_result(
    *,
    workspace: Path | None,
    tool_results: tuple[ToolResult, ...],
    hook_preset_context: str,
    registry: RuntimeContextTransformRegistry | None = None,
) -> RuntimeContextTransformResult:
    active_registry = registry or default_runtime_context_transform_registry()
    return active_registry.build_result(
        RuntimeContextTransformRequest(
            workspace=workspace,
            tool_results=tool_results,
            hook_preset_context=hook_preset_context,
        )
    )


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
