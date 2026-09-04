from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from ...tools.contracts import ToolResult
from .rules import (
    RuleCatalog,
    build_rule_catalog,
    rulebook_prompt_context,
    rulebook_snapshot_from_payload,
    runtime_file_rule_contexts,
)

if TYPE_CHECKING:
    from .window import ToolResultView

type RuntimeContextTransformProviderId = str
type RuntimeContextTransformFailurePolicy = Literal["ignore", "warn", "block"]
type RuntimeContextTransformScope = Literal["provider_context"]
type RuntimeContextTransformVersion = str

_MAX_TRACE_TEXT_CHARS = 256
_MAX_TRACE_ITEMS = 32


def _bounded_trace_text(value: str) -> str:
    text = value.strip()
    return text if len(text) <= _MAX_TRACE_TEXT_CHARS else f"{text[:_MAX_TRACE_TEXT_CHARS]}…"


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformInjection:
    role: str
    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformTrace:
    provider_id: str
    provider_version: RuntimeContextTransformVersion = "1"
    scope: RuntimeContextTransformScope = "provider_context"
    status: str = "ok"
    priority: int = 100
    execution_index: int = 0
    injection_count: int = 0
    provider_order: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    failure_policy: RuntimeContextTransformFailurePolicy = "warn"
    error: str | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider_id": self.provider_id,
            "status": self.status,
            "priority": self.priority,
            "execution_index": self.execution_index,
            "injection_count": self.injection_count,
            "provider_order": list(self.provider_order[:_MAX_TRACE_ITEMS]),
            "sources": list(self.sources[:_MAX_TRACE_ITEMS]),
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
    tool_results: tuple[ToolResult | ToolResultView, ...]
    hook_preset_context: str
    mode_guidance_context: str = ""
    failure_policy: RuntimeContextTransformFailurePolicy = "warn"
    rulebook_snapshot: object | None = None


class RuntimeContextTransformProvider(Protocol):
    provider_id: str
    provider_version: RuntimeContextTransformVersion
    scope: RuntimeContextTransformScope
    priority: int
    failure_policy: RuntimeContextTransformFailurePolicy

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult: ...


class HookPresetGuidanceTransformProvider:
    provider_id = "hook_preset_guidance"
    provider_version = "1"
    scope: RuntimeContextTransformScope = "provider_context"
    priority = 100
    failure_policy: RuntimeContextTransformFailurePolicy = "warn"

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


class ModeGuidanceTransformProvider:
    provider_id = "mode_guidance"
    provider_version = "1"
    scope: RuntimeContextTransformScope = "provider_context"
    priority = 150
    failure_policy: RuntimeContextTransformFailurePolicy = "warn"

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult:
        normalized_mode_guidance = request.mode_guidance_context.strip()
        if not normalized_mode_guidance:
            return RuntimeContextTransformResult()
        return RuntimeContextTransformResult(
            injections=(
                RuntimeContextTransformInjection(
                    role="system",
                    content=normalized_mode_guidance,
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
    provider_version = "1"
    scope: RuntimeContextTransformScope = "provider_context"
    priority = 200
    failure_policy: RuntimeContextTransformFailurePolicy = "warn"

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
                        f"Runtime file rules are active for touched workspace paths.\nRule file: {rule_context.path}\n{rule_context.content}"
                    ).strip(),
                    metadata=rule_context.metadata_payload(),
                )
            )
        rulebook_catalog = _rulebook_catalog_for_request(request)
        if rulebook_catalog.entries:
            rulebook_context = rulebook_prompt_context(rulebook_catalog)
            if rulebook_context:
                rule_segments.append(
                    RuntimeContextTransformInjection(
                        role="system",
                        content=rulebook_context,
                        metadata={
                            "source": "runtime_rulebook",
                            "snapshot_hash": rulebook_catalog.snapshot.snapshot_hash,
                            "always_apply_count": len(rulebook_catalog.always_apply),
                            "discoverable_count": len(rulebook_catalog.discoverable),
                        },
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


def _rulebook_catalog_for_request(request: RuntimeContextTransformRequest) -> RuleCatalog:
    catalog = build_rule_catalog(request.workspace)
    if request.rulebook_snapshot is None:
        return catalog
    snapshot = rulebook_snapshot_from_payload(request.rulebook_snapshot)
    expected = {entry.name: entry.content_hash for entry in snapshot.entries}
    stable_entries = tuple(entry for entry in catalog.entries if expected.get(entry.metadata.name) == entry.metadata.content_hash)
    from dataclasses import replace

    return RuleCatalog(
        entries=stable_entries,
        snapshot=replace(snapshot, entries=tuple(entry.metadata for entry in stable_entries)),
    )


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformRegistry:
    providers: tuple[RuntimeContextTransformProvider, ...] = ()

    def __post_init__(self) -> None:
        provider_ids = [provider.provider_id for provider in self.providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("context transform provider ids must be unique")
        for provider in self.providers:
            version = getattr(provider, "provider_version", "1")
            scope = getattr(provider, "scope", "provider_context")
            policy = getattr(provider, "failure_policy", "warn")
            if not isinstance(version, str) or not version.strip():
                raise ValueError(f"context transform provider '{provider.provider_id}' version must be non-empty")
            if scope != "provider_context":
                raise ValueError(f"context transform provider '{provider.provider_id}' has unsupported scope: {scope}")
            if policy not in {"ignore", "warn", "block"}:
                raise ValueError(f"context transform provider '{provider.provider_id}' has unsupported failure policy: {policy}")

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
        return RuntimeContextTransformRegistry(providers=tuple(provider for provider in self.providers if provider.provider_id in allowed))

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
                            diagnostics=(f"context transform provider '{provider.provider_id}' failed",),
                            error=str(exc),
                        ),
                    ),
                )
            injections.extend(result.injections)
            traces.extend(
                RuntimeContextTransformTrace(
                    provider_id=trace.provider_id,
                    provider_version=getattr(provider, "provider_version", "1"),
                    scope=getattr(provider, "scope", "provider_context"),
                    status=trace.status,
                    priority=provider.priority,
                    execution_index=execution_index,
                    injection_count=trace.injection_count,
                    provider_order=ordered_provider_ids,
                    sources=trace.sources,
                    diagnostics=trace.diagnostics,
                    failure_policy=getattr(provider, "failure_policy", request.failure_policy),
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
            ModeGuidanceTransformProvider(),
            RuntimeFileRulesTransformProvider(),
        )
    )


def build_provider_context_transform_result(
    *,
    workspace: Path | None,
    tool_results: tuple[ToolResult | ToolResultView, ...],
    hook_preset_context: str,
    mode_guidance_context: str = "",
    failure_policy: RuntimeContextTransformFailurePolicy = "warn",
    rulebook_snapshot: object | None = None,
    registry: RuntimeContextTransformRegistry | None = None,
) -> RuntimeContextTransformResult:
    active_registry = registry or default_runtime_context_transform_registry()
    return active_registry.build_result(
        RuntimeContextTransformRequest(
            workspace=workspace,
            tool_results=tool_results,
            hook_preset_context=hook_preset_context,
            mode_guidance_context=mode_guidance_context,
            failure_policy=failure_policy,
            rulebook_snapshot=rulebook_snapshot,
        )
    )


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
            raise ValueError(f"{field_path} references unknown context transform provider: {ref}; valid providers are: {allowed}")
    return refs
