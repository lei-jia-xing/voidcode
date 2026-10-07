"""Pure run-scoped declaration layering before explicit activated binding."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from ..tools.local_custom import LocalCustomToolManifest, discover_local_custom_tool_manifests
from .config import RuntimeToolsLocalConfig
from .config_materializer import EffectiveRuntimeConfig
from .tool_materializer import RuntimeToolMaterialization, RuntimeToolMaterializer
from .tool_scope import RuntimeToolScopeResolver

type LocalToolManifestsProvider = Callable[[RuntimeToolsLocalConfig | None], tuple[LocalCustomToolManifest, ...]]


def workspace_local_tool_manifests_provider(workspace: Path) -> LocalToolManifestsProvider:
    def discover(local_config: RuntimeToolsLocalConfig | None) -> tuple[LocalCustomToolManifest, ...]:
        if local_config is None:
            return ()
        if not local_config.path:
            raise ValueError("local custom tools path must not be empty")
        relative_path = Path(local_config.path)
        if relative_path.is_absolute():
            raise ValueError("local custom tools path must be workspace-relative")
        if ".." in relative_path.parts:
            raise ValueError("local custom tools path must not contain '..'")
        return discover_local_custom_tool_manifests(workspace, enabled=local_config.enabled, relative_path=str(relative_path))

    return discover


def materialize_unscoped(
    effective_config: EffectiveRuntimeConfig,
    *,
    materialization: RuntimeToolMaterialization,
    materializer: RuntimeToolMaterializer,
    local_tool_manifests_provider: LocalToolManifestsProvider,
) -> RuntimeToolMaterialization:
    local_config = effective_config.tools.local if effective_config.tools is not None else None
    return materializer.materialize_local_manifests(materialization, local_tool_manifests_provider(local_config))


def materialize(
    effective_config: EffectiveRuntimeConfig,
    *,
    materialization: RuntimeToolMaterialization,
    materializer: RuntimeToolMaterializer,
    local_tool_manifests_provider: LocalToolManifestsProvider,
    scope_resolver: RuntimeToolScopeResolver,
    metadata: dict[str, object] | None = None,
    builtin_mcp_tool_names: Iterable[str] = (),
) -> RuntimeToolMaterialization:
    unscoped = materialize_unscoped(
        effective_config,
        materialization=materialization,
        materializer=materializer,
        local_tool_manifests_provider=local_tool_manifests_provider,
    )
    return unscoped.scoped(
        scope_resolver.scope(
            unscoped.registry,
            agent=effective_config.agent,
            metadata=metadata,
            builtin_mcp_tool_names=tuple(builtin_mcp_tool_names),
        )
    )
