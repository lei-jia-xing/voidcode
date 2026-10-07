from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ...security.json_values import json_wire_object
from .rules import (
    RuleCatalog,
    build_rule_catalog,
    rulebook_prompt_context,
    rulebook_snapshot_from_payload,
    runtime_file_rule_contexts,
)

if TYPE_CHECKING:
    from ...core.transcript import ToolResultView
    from ...core.turns import ReportedCall

type RuntimeContextTransformProviderId = str
type RuntimeContextTransformFailurePolicy = Literal["ignore", "warn", "block"]
type RuntimeContextTransformScope = Literal["provider_context"]
type RuntimeContextTransformVersion = str

_MAX_TRACE_ITEMS = 32


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformDeclaration:
    provider_id: str
    provider_version: str
    scope: RuntimeContextTransformScope
    priority: int
    failure_policy: RuntimeContextTransformFailurePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("context transform provider id must be non-empty")
        if not isinstance(self.provider_version, str) or not self.provider_version.strip():
            raise ValueError("context transform provider version must be non-empty")
        if self.scope != "provider_context":
            raise ValueError("unsupported context transform scope")
        if type(self.priority) is not int:
            raise ValueError("context transform priority must be an integer")
        if self.failure_policy not in ("ignore", "warn", "block"):
            raise ValueError("unsupported context transform failure policy")

    @classmethod
    def from_provider(
        cls,
        provider: RuntimeContextTransformProvider | type[RuntimeContextTransformProvider],
    ) -> RuntimeContextTransformDeclaration:
        p: Any = provider
        return cls(
            p.provider_id,
            p.provider_version,
            p.scope,
            p.priority,
            p.failure_policy,
        )


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformInjection:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformTrace:
    provider_id: str
    provider_version: RuntimeContextTransformVersion
    scope: RuntimeContextTransformScope
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
            "provider_version": self.provider_version,
            "scope": self.scope,
            "failure_policy": self.failure_policy,
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
            "version": 2,
            "failure_policy": self.failure_policy,
            "applied": [trace.metadata_payload() for trace in self.traces],
        }


@dataclass(frozen=True, slots=True)
class RuntimeContextTransformRequest:
    workspace: Path | None
    tool_results: tuple[ReportedCall | ToolResultView, ...]
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
                    provider_version=self.provider_version,
                    scope=self.scope,
                    failure_policy=self.failure_policy,
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
                    provider_version=self.provider_version,
                    scope=self.scope,
                    failure_policy=self.failure_policy,
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
                    provider_version=self.provider_version,
                    scope=self.scope,
                    failure_policy=self.failure_policy,
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


class RuntimeContextTransformRegistry:
    """One pure declaration catalogue and its genuinely bound providers."""

    def __init__(
        self,
        providers: Iterable[RuntimeContextTransformProvider] = (),
        *,
        declarations: Iterable[RuntimeContextTransformDeclaration] | None = None,
    ) -> None:
        bound = tuple(providers)
        observed = tuple(RuntimeContextTransformDeclaration.from_provider(provider) for provider in bound)
        declared = tuple(declarations) if declarations is not None else observed
        by_id: dict[str, RuntimeContextTransformDeclaration] = {}
        for declaration in declared:
            if type(declaration) is not RuntimeContextTransformDeclaration:
                raise ValueError("context declarations must be pure RuntimeContextTransformDeclaration records")
            if declaration.provider_id in by_id:
                raise ValueError("context transform provider ids must be unique")
            by_id[declaration.provider_id] = declaration
        bound_by_id: dict[str, RuntimeContextTransformProvider] = {}
        for provider, declaration in zip(bound, observed, strict=True):
            name = declaration.provider_id
            if name in bound_by_id or by_id.get(name) != declaration:
                raise ValueError(f"context transform binding does not match unique declaration: {name}")
            bound_by_id[name] = provider
        self._declarations = MappingProxyType(by_id)
        self._bound = MappingProxyType(bound_by_id)

    @classmethod
    def from_declarations(cls, declarations: Iterable[RuntimeContextTransformDeclaration]) -> RuntimeContextTransformRegistry:
        return cls(declarations=declarations)

    @property
    def declarations(self) -> Mapping[str, RuntimeContextTransformDeclaration]:
        return self._declarations

    @property
    def providers(self) -> tuple[RuntimeContextTransformProvider, ...]:
        return tuple(self._bound.values())

    def bind(self, materialize: Callable[[str], RuntimeContextTransformProvider]) -> RuntimeContextTransformRegistry:
        if len(self._bound) == len(self._declarations):
            return self
        bound = dict(self._bound)
        for name, declaration in self._declarations.items():
            if name in bound:
                continue
            provider = materialize(name)
            if RuntimeContextTransformDeclaration.from_provider(provider) != declaration:
                raise ValueError(f"materialized context transform does not match declaration: {name}")
            bound[name] = provider
        registry = RuntimeContextTransformRegistry.from_declarations(self._declarations.values())
        registry._bound = MappingProxyType(bound)
        return registry

    def ordered_providers(self) -> tuple[RuntimeContextTransformProvider, ...]:
        if len(self._bound) != len(self._declarations):
            raise RuntimeError("context transform providers must be bound after activation before dispatch")
        return tuple(self._bound[name] for name in self.provider_ids())

    def filtered(
        self,
        provider_ids: tuple[RuntimeContextTransformProviderId, ...],
    ) -> RuntimeContextTransformRegistry:
        if not provider_ids:
            return self
        unknown = set(provider_ids) - self._declarations.keys()
        if unknown:
            raise ValueError(f"unknown context transform providers: {sorted(unknown)}")
        selected = tuple(dict.fromkeys(provider_ids))
        registry = RuntimeContextTransformRegistry.from_declarations(self._declarations[name] for name in selected)
        registry._bound = MappingProxyType({name: self._bound[name] for name in selected if name in self._bound})
        return registry

    def provider_ids(self) -> tuple[RuntimeContextTransformProviderId, ...]:
        return tuple(
            declaration.provider_id for declaration in sorted(self._declarations.values(), key=lambda item: (item.priority, item.provider_id))
        )

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult:
        injections: list[RuntimeContextTransformInjection] = []
        traces: list[RuntimeContextTransformTrace] = []
        ordered_provider_ids = self.provider_ids()
        ordered_providers = self.ordered_providers()
        for execution_index, (name, provider) in enumerate(zip(ordered_provider_ids, ordered_providers, strict=True), start=1):
            declaration = self._declarations[name]
            try:
                result = provider.build_result(request)
            except Exception as exc:
                result = RuntimeContextTransformResult(
                    failure_policy=request.failure_policy,
                    traces=(
                        RuntimeContextTransformTrace(
                            provider_id=name,
                            provider_version=declaration.provider_version,
                            scope=declaration.scope,
                            failure_policy=declaration.failure_policy,
                            status="error",
                            priority=declaration.priority,
                            diagnostics=(f"context transform provider '{name}' failed",),
                            error=str(exc),
                        ),
                    ),
                )
            injections.extend(result.injections)
            traces.extend(
                RuntimeContextTransformTrace(
                    provider_id=name,
                    provider_version=declaration.provider_version,
                    scope=declaration.scope,
                    status=trace.status,
                    priority=declaration.priority,
                    execution_index=execution_index,
                    injection_count=trace.injection_count,
                    provider_order=ordered_provider_ids,
                    sources=trace.sources,
                    diagnostics=trace.diagnostics,
                    failure_policy=declaration.failure_policy,
                    error=trace.error,
                )
                for trace in result.traces
            )
        return RuntimeContextTransformResult(
            injections=tuple(injections),
            traces=tuple(traces),
            failure_policy=request.failure_policy,
        )


_BUILTIN_CONTEXT_TRANSFORMS = (
    HookPresetGuidanceTransformProvider,
    ModeGuidanceTransformProvider,
    RuntimeFileRulesTransformProvider,
)


def builtin_runtime_context_transform_declarations() -> tuple[RuntimeContextTransformDeclaration, ...]:
    return tuple(RuntimeContextTransformDeclaration.from_provider(owner) for owner in _BUILTIN_CONTEXT_TRANSFORMS)


def materialize_builtin_context_transform(provider_id: str) -> RuntimeContextTransformProvider:
    for owner in _BUILTIN_CONTEXT_TRANSFORMS:
        if owner.provider_id == provider_id:
            return owner()
    raise ValueError(f"unknown builtin context transform provider: {provider_id}")


def build_provider_context_transform_result(
    *,
    workspace: Path | None,
    tool_results: tuple[ReportedCall | ToolResultView, ...],
    hook_preset_context: str,
    mode_guidance_context: str = "",
    failure_policy: RuntimeContextTransformFailurePolicy = "warn",
    rulebook_snapshot: object | None = None,
    registry: RuntimeContextTransformRegistry | None = None,
) -> RuntimeContextTransformResult:
    active_registry = (
        registry if registry is not None else RuntimeContextTransformRegistry(providers=tuple(owner() for owner in _BUILTIN_CONTEXT_TRANSFORMS))
    )
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
    active_registry = (
        registry if registry is not None else RuntimeContextTransformRegistry.from_declarations(builtin_runtime_context_transform_declarations())
    )
    valid_refs = frozenset(active_registry.provider_ids())
    for ref in refs:
        if not ref.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")
        if ref not in valid_refs:
            allowed = ", ".join(sorted(valid_refs))
            raise ValueError(f"{field_path} references unknown context transform provider: {ref}; valid providers are: {allowed}")
    return refs


def context_transform_applied_payloads(
    *,
    context_metadata: Mapping[str, object],
    tool_result_count: int,
) -> tuple[tuple[str, dict[str, object]], ...]:
    """Build event payloads and fingerprints from provider transform metadata."""
    if "context_transforms" not in context_metadata:
        return ()
    raw_transforms = context_metadata["context_transforms"]
    if not isinstance(raw_transforms, Mapping) or set(raw_transforms) != {"version", "failure_policy", "applied"}:
        raise ValueError("invalid current context transform metadata")
    transforms = json_wire_object(raw_transforms)
    if type(transforms["version"]) is not int or transforms["version"] != 2:
        raise ValueError("unsupported context transform metadata version")
    if transforms["failure_policy"] not in ("ignore", "warn", "block") or not isinstance(transforms["applied"], list):
        raise ValueError("invalid current context transform metadata")
    required = {
        "provider_id",
        "provider_version",
        "scope",
        "failure_policy",
        "status",
        "priority",
        "execution_index",
        "injection_count",
        "provider_order",
        "sources",
    }
    payloads: list[tuple[str, dict[str, object]]] = []
    for trace in transforms["applied"]:
        if not isinstance(trace, dict) or required - trace.keys() or trace.keys() - required - {"diagnostics", "error"}:
            raise ValueError("invalid current context transform trace fields")
        if any(not isinstance(trace[key], str) or not trace[key].strip() for key in ("provider_id", "provider_version", "status")):
            raise ValueError("invalid context transform identity/version/status")
        if trace["scope"] != "provider_context" or trace["failure_policy"] not in ("ignore", "warn", "block"):
            raise ValueError("invalid context transform scope/policy")
        for key in ("priority", "execution_index", "injection_count"):
            if type(trace[key]) is not int or (key != "priority" and trace[key] < 0):
                raise ValueError("invalid context transform integer metadata")
        for key in ("provider_order", "sources", "diagnostics"):
            values = trace.get(key, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError("invalid context transform list metadata")
        if "error" in trace and not isinstance(trace["error"], str):
            raise ValueError("invalid context transform error metadata")
        if trace["provider_id"] == "hook_preset_guidance":
            continue
        payload: dict[str, object] = {
            "version": 2,
            **trace,
            "request_failure_policy": transforms["failure_policy"],
            "tool_result_count": tool_result_count,
        }
        fingerprint_payload = {key: value for key, value in payload.items() if key != "tool_result_count"}
        payloads.append((json.dumps(fingerprint_payload, sort_keys=True, allow_nan=False), payload))
    return tuple(payloads)
