"""Single entry point for run-scoped tool materialization.

Converges the service-level registry wrapper, scoped materialization, and
local-tools layering into one composition: layer effective-local tools over
the current materialization, then apply the agent scope. The policy-denial
path intentionally reasons over the unscoped registry, so that step stays
reachable as ``materialize_unscoped``; both share the same local layering.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..tools.contracts import Tool
from .config import RuntimeToolsLocalConfig
from .config_materializer import EffectiveRuntimeConfig
from .tool_materializer import RuntimeToolMaterialization, RuntimeToolMaterializer
from .tool_provider import LocalCustomToolProvider
from .tool_scope import RuntimeToolScopeResolver

type LocalToolsProviderFactory = Callable[[RuntimeToolsLocalConfig | None], tuple[Tool, ...]]


def workspace_local_tools_factory(workspace: Path) -> LocalToolsProviderFactory:
    def provide(local_config: RuntimeToolsLocalConfig | None) -> tuple[Tool, ...]:
        return LocalCustomToolProvider(workspace=workspace, config=local_config).provide_tools()

    return provide


def materialize_unscoped(
    effective_config: EffectiveRuntimeConfig,
    *,
    materialization: RuntimeToolMaterialization,
    materializer: RuntimeToolMaterializer,
    local_tools_provider_factory: LocalToolsProviderFactory,
) -> RuntimeToolMaterialization:
    local_config = effective_config.tools.local if effective_config.tools is not None else None
    return materializer.materialize_local_tools(materialization, local_tools_provider_factory(local_config))


def materialize(
    effective_config: EffectiveRuntimeConfig,
    *,
    materialization: RuntimeToolMaterialization,
    materializer: RuntimeToolMaterializer,
    local_tools_provider_factory: LocalToolsProviderFactory,
    scope_resolver: RuntimeToolScopeResolver,
    metadata: dict[str, object] | None = None,
) -> RuntimeToolMaterialization:
    unscoped = materialize_unscoped(
        effective_config,
        materialization=materialization,
        materializer=materializer,
        local_tools_provider_factory=local_tools_provider_factory,
    )
    return unscoped.scoped(scope_resolver.scope(unscoped.registry, agent=effective_config.agent, metadata=metadata))
