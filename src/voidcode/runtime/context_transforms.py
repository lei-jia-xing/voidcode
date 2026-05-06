from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from ..tools.contracts import ToolResult
from .context_rules import runtime_file_rule_contexts

type RuntimeContextTransformProviderId = str
type RuntimeContextTransformFailurePolicy = str


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformInjection:
    role: str
    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformTrace:
    provider_id: str
    status: str = "ok"
    priority: int = 100
    execution_index: int = 0
    injection_count: int = 0
    provider_order: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    error: str | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider_id": self.provider_id,
            "status": self.status,
            "priority": self.priority,
            "execution_index": self.execution_index,
            "injection_count": self.injection_count,
            "provider_order": list(self.provider_order),
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
    failure_policy: RuntimeContextTransformFailurePolicy = "warn"

    def metadata_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "failure_policy": self.failure_policy,
            "applied": [trace.metadata_payload() for trace in self.traces],
        }


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformRequest:
    workspace: Path | None
    tool_results: tuple[ToolResult, ...]
    hook_preset_context: str
    failure_policy: RuntimeContextTransformFailurePolicy = "warn"


class RuntimeContextTransformProvider(Protocol):
    provider_id: str
    priority: int

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult: ...


class HookPresetGuidanceTransformProvider:
    provider_id = "hook_preset_guidance"
    priority = 100

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
                    priority=self.priority,
                    injection_count=1,
                    sources=(self.provider_id,),
                ),
            ),
        )


class RuntimeFileRulesTransformProvider:
    provider_id = "runtime_file_rules"
    priority = 200

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
                    priority=self.priority,
                    injection_count=len(rule_segments),
                    sources=(self.provider_id,),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformRegistry:
    providers: tuple[RuntimeContextTransformProvider, ...] = ()

    def ordered_providers(self) -> tuple[RuntimeContextTransformProvider, ...]:
        return tuple(
            sorted(
                self.providers,
                key=lambda provider: (provider.priority, provider.provider_id),
            )
        )

    def filtered(
        self,
        provider_ids: tuple[RuntimeContextTransformProviderId, ...],
    ) -> RuntimeContextTransformRegistry:
        if not provider_ids:
            return self
        allowed = frozenset(provider_ids)
        return RuntimeContextTransformRegistry(
            providers=tuple(
                provider for provider in self.providers if provider.provider_id in allowed
            )
        )

    def provider_ids(self) -> tuple[RuntimeContextTransformProviderId, ...]:
        return tuple(provider.provider_id for provider in self.ordered_providers())

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult:
        injections: list[RuntimeContextTransformInjection] = []
        traces: list[RuntimeContextTransformTrace] = []
        ordered_providers = self.ordered_providers()
        ordered_provider_ids = tuple(provider.provider_id for provider in ordered_providers)
        for execution_index, provider in enumerate(ordered_providers, start=1):
            try:
                result = provider.build_result(request)
            except Exception as exc:
                result = RuntimeContextTransformResult(
                    failure_policy=request.failure_policy,
                    traces=(
                        RuntimeContextTransformTrace(
                            provider_id=provider.provider_id,
                            status="error",
                            priority=provider.priority,
                            diagnostics=(
                                f"context transform provider '{provider.provider_id}' failed",
                            ),
                            error=str(exc),
                        ),
                    ),
                )
            injections.extend(result.injections)
            traces.extend(
                RuntimeContextTransformTrace(
                    provider_id=trace.provider_id,
                    status=trace.status,
                    priority=trace.priority,
                    execution_index=execution_index,
                    injection_count=trace.injection_count,
                    provider_order=ordered_provider_ids,
                    sources=trace.sources,
                    diagnostics=trace.diagnostics,
                    error=trace.error,
                )
                for trace in result.traces
            )
        return RuntimeContextTransformResult(
            injections=tuple(injections),
            traces=tuple(traces),
            failure_policy=request.failure_policy,
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
    failure_policy: RuntimeContextTransformFailurePolicy = "warn",
    registry: RuntimeContextTransformRegistry | None = None,
) -> RuntimeContextTransformResult:
    active_registry = registry or default_runtime_context_transform_registry()
    return active_registry.build_result(
        RuntimeContextTransformRequest(
            workspace=workspace,
            tool_results=tool_results,
            hook_preset_context=hook_preset_context,
            failure_policy=failure_policy,
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


def validate_runtime_context_transform_refs(
    refs: tuple[str, ...],
    *,
    field_path: str,
    registry: RuntimeContextTransformRegistry | None = None,
) -> tuple[str, ...]:
    if not refs:
        return ()
    active_registry = registry or default_runtime_context_transform_registry()
    valid_refs = frozenset(active_registry.provider_ids())
    for ref in refs:
        if not ref.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")
        if ref not in valid_refs:
            allowed = ", ".join(sorted(valid_refs))
            raise ValueError(
                f"{field_path} references unknown context transform provider: {ref}; "
                f"valid providers are: {allowed}"
            )
    return refs
