"""Stream-prep-bucket coordinator: run-start composition for streaming runs.

Owns what ``VoidCodeRuntime`` composes before the turn engine: request-level
config resolution, session routing, context-window policy helpers, and
provider context-window preparation.

Reads via constructor-injected collaborators plus a narrow ``RuntimeSurface``
for run/finalize-owned composition (effective config, reasoning capability).
Cross-bucket entry points that stay owned elsewhere (request agent override,
context-transform registry scoping) arrive as explicit callbacks so this
module never pierces runtime privates and adds no new storage write paths.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.transcript import ToolResultView
from ...core.turns import ReportedCall
from ...mcp import McpCachedToolSurface, McpToolDescriptor
from ...provider.model_catalog import static_catalog_metadata
from ...provider.protocol import ProviderAbortSignal
from ...skills import SkillRegistry, skill_registry_with_builtins
from ...tools.contracts import Tool
from ..config.loading import RuntimeSkillsConfig
from ..config.materializer import (
    RunConfig,
    apply_request_runtime_config_overrides,
)
from ..context.transforms import validate_runtime_context_transform_refs
from ..context.window import (
    BeforeCompactInput,
    ContextWindowPolicy,
    RuntimeContextWindow,
    prepare_provider_context,
)
from ..context.window_policy import (
    context_window_policy_from_config,
)
from ..contracts import (
    RuntimeRequest,
    RuntimeRequestError,
)
from ..lsp import LspManager
from ..mcp import McpManager
from ..providers.metadata import validate_reasoning_effort_capability

if TYPE_CHECKING:
    from ..context.transforms import RuntimeContextTransformRegistry
    from ..runtime_surface import RuntimeSurface

logger = logging.getLogger(__name__)


class StreamPrepCoordinator:
    """Run-start composition for streaming runs; see module docstring."""

    def __init__(
        self,
        surface: RuntimeSurface,
        *,
        default_context_window_policy: ContextWindowPolicy,
        config_with_request_agent_override: Callable[..., RunConfig],
        context_transform_registry_for_agent: Callable[..., RuntimeContextTransformRegistry],
        workspace: Path,
        global_skills_config: RuntimeSkillsConfig | None,
        skill_registry: SkillRegistry | None = None,
        skill_registry_is_injected: bool = False,
        mcp_manager: McpManager | None = None,
        mcp_manager_is_injected: bool = False,
        turn_producer_override_present: Callable[[], bool] | None = None,
    ) -> None:
        self._surface = surface
        self._default_context_window_policy = default_context_window_policy
        self._config_with_request_agent_override_fn = config_with_request_agent_override
        self._context_transform_registry_for_agent_fn = context_transform_registry_for_agent
        self._workspace = workspace
        self._global_skills_config = global_skills_config
        self._skill_registry = skill_registry
        self._skill_registry_is_injected = skill_registry_is_injected
        self._mcp_manager = mcp_manager
        self._mcp_manager_is_injected = mcp_manager_is_injected
        self._turn_producer_override_present_fn = turn_producer_override_present

    def run_config_for_request(self, request: RuntimeRequest) -> RunConfig:
        config = self._surface.run_config_from_metadata(None)
        request_agent = request.metadata.get("agent")
        if request_agent is not None:
            try:
                config = self._config_with_request_agent_override_fn(
                    config,
                    request_agent,
                    allow_subagent_presets=request.subagent_routing is not None,
                )
            except ValueError as exc:
                raise RuntimeRequestError(str(exc)) from exc
        request_context_transform_refs = request.metadata.get("context_transform_refs")
        context_transform_refs: tuple[str, ...] | None = None
        if request_context_transform_refs is not None:
            assert isinstance(request_context_transform_refs, list)
            context_transform_refs = tuple(request_context_transform_refs)
            validate_runtime_context_transform_refs(
                context_transform_refs,
                field_path="request metadata 'context_transform_refs'",
                registry=self._context_transform_registry_for_agent_fn(config.agent),
            )
        config = apply_request_runtime_config_overrides(
            config,
            reasoning_effort=request.metadata.get("reasoning_effort"),
            context_transform_refs=context_transform_refs,
        )
        try:
            validate_reasoning_effort_capability(config, self._surface.reasoning_effort_capability(config))
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        return config

    def prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
        abort_signal: ProviderAbortSignal | None = None,  # noqa: ARG002 — retained by RuntimeSurface protocol for abort-aware callers.
        before_compact: BeforeCompactInput | None = None,
    ) -> RuntimeContextWindow:
        config = self._surface.run_config_from_metadata(session_metadata)
        if policy is None:
            policy = context_window_policy_from_config(config.context_window)
        return prepare_provider_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy or self._default_context_window_policy,
            before_compact=before_compact,
            # Pruning needs a basis that covers every provider-visible section;
            # this seam cannot size the assembled payload, so the compaction
            # decision belongs to ``assemble_provider_context`` (which compiles
            # the window with ``compaction_budget_from_config``). Here the
            # runtime still owns the per-result char caps and the hook seam.
            payload_bytes=None,
        )

    def context_budget_for_config(self, config: RunConfig) -> int | None:
        """Catalog basis for compaction; this is not a fallback-comparison cap."""
        return self._context_budget_for(config)

    @staticmethod
    def _context_budget_for(config: RunConfig) -> int | None:
        """Size compaction from an input cap or aggregate window, if known.

        When only the aggregate context window is available, compaction applies
        its configured output reserve before sizing the request. This estimate is
        separate from provider fallback comparisons, which require explicit input
        caps.
        """
        selection = config.resolved_provider.active_target.selection
        provider_name = selection.provider
        model_name = selection.model
        if not provider_name or not model_name:
            return None
        metadata = static_catalog_metadata(provider_name, model_name)
        if metadata is None:
            return None
        return metadata.max_input_tokens or metadata.context_window

    @staticmethod
    def build_skill_registry_for_workspace(workspace: Path, skills_config: RuntimeSkillsConfig | None) -> SkillRegistry:
        if skills_config is None or skills_config.enabled is not True:
            registry = skill_registry_with_builtins(())
        elif skills_config.paths:
            discovered = SkillRegistry.discover(
                workspace=workspace,
                search_paths=skills_config.paths,
            )
            registry = skill_registry_with_builtins(discovered.all())
        else:
            discovered = SkillRegistry.discover(workspace=workspace)
            registry = skill_registry_with_builtins(discovered.all())
        if skills_config is not None and skills_config.config:
            registry.apply_config_rules(skills_config.config)
        return registry

    def build_skill_registry(self, skills_config: RuntimeSkillsConfig | None) -> SkillRegistry:
        return self.build_skill_registry_for_workspace(self._workspace, skills_config)

    @staticmethod
    def build_lsp_tool_for_manager(
        lsp_manager: LspManager | None,
    ) -> Tool | None:
        if lsp_manager is None or lsp_manager.current_state().mode != "managed":
            return None
        from ...tools.lsp import LspTool

        return LspTool()

    def skills_config_for_config(
        self,
        config: RunConfig,
    ) -> RuntimeSkillsConfig | None:
        if config.agent is not None and config.agent.skills is not None:
            return config.agent.skills
        return self._global_skills_config

    def skill_registry_for_config(
        self,
        config: RunConfig,
    ) -> SkillRegistry:
        if self._skill_registry_is_injected:
            assert self._skill_registry is not None
            return self._skill_registry
        return self.build_skill_registry(self.skills_config_for_config(config))

    def list_mcp_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        if self._mcp_manager is None or self._mcp_manager.current_state().mode != "managed":
            return ()
        return self._mcp_manager.list_tools(workspace=self._workspace)

    def mcp_cached_surface(self) -> McpCachedToolSurface:
        """Passively remembered MCP surface for the configured servers."""
        if self._mcp_manager is None:
            return McpCachedToolSurface()
        if self._mcp_manager.current_state().mode != "managed":
            return McpCachedToolSurface()
        return self._mcp_manager.cached_surface(workspace=self._workspace)

    @staticmethod
    def is_background_child_mcp_deferred(
        *,
        request_metadata: Mapping[str, object],
        config: RunConfig,
    ) -> bool:
        if request_metadata.get("background_run") is not True:
            return False
        agent_binding = config.agent.mcp_binding if config.agent is not None else None
        if agent_binding is not None:
            return False
        return True

    def should_skip_mcp_startup_for_request(self) -> bool:
        if self._mcp_manager_is_injected:
            return False
        if self._mcp_manager is None:
            return True
        configured_servers = set(self._mcp_manager.current_state().configuration.servers)
        builtin_servers = {"context7", "websearch", "grep_app"}
        if not configured_servers <= builtin_servers:
            return False
        assert self._turn_producer_override_present_fn is not None
        return self._turn_producer_override_present_fn()
