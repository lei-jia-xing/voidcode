"""Stream-prep-bucket coordinator: run-start composition for streaming runs.

Owns what ``VoidCodeRuntime`` composes before the graph loop: request-level
config resolution, session routing, context-window policy helpers, and
provider context-window preparation.

Reads via constructor-injected collaborators plus a narrow ``RuntimeSurface``
for run/finalize-owned composition (effective config, reasoning capability).
Cross-bucket entry points that stay owned elsewhere (request agent override,
context-transform registry scoping, provider-attempt policy projection
owned by the inspection coordinator) arrive as explicit callbacks so this
module never pierces runtime privates and adds no new storage write paths.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ...mcp import McpCachedToolSurface, McpToolDescriptor
from ...provider.model_catalog import static_catalog_metadata
from ...provider.protocol import ProviderAbortSignal
from ...skills import SkillRegistry, skill_registry_with_builtins
from ...tools.contracts import Tool, ToolResult
from ...tools.mcp import McpRequester
from ...tools.runtime_context import current_runtime_tool_context
from ..config import RuntimeSkillsConfig
from ..config_materializer import (
    EffectiveRuntimeConfig,
    apply_request_runtime_config_overrides,
)
from ..context.transforms import validate_runtime_context_transform_refs
from ..context.window import (
    BeforeCompactInput,
    ContextWindowPolicy,
    RuntimeContextWindow,
    ToolResultView,
    prepare_provider_context,
)
from ..context.window_policy import (
    context_window_policy_from_config,
)
from ..contracts import (
    RuntimeRequest,
    RuntimeRequestError,
)
from ..execution.provider_execution_metadata import provider_attempt_from_metadata
from ..lsp import LspManager, LspRequestResult
from ..mcp import McpManager
from ..provider_metadata import validate_reasoning_effort_capability

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
        config_with_request_agent_override: Callable[..., EffectiveRuntimeConfig],
        context_transform_registry_for_agent: Callable[..., RuntimeContextTransformRegistry],
        context_window_policy_for_provider_attempt: Callable[..., ContextWindowPolicy],
        workspace: Path,
        global_skills_config: RuntimeSkillsConfig | None,
        skill_registry: SkillRegistry | None = None,
        skill_registry_is_injected: bool = False,
        mcp_manager: McpManager | None = None,
        mcp_manager_is_injected: bool = False,
        graph_override_present: Callable[[], bool] | None = None,
        request_mcp_tool: McpRequester | None = None,
    ) -> None:
        self._surface = surface
        self._default_context_window_policy = default_context_window_policy
        self._config_with_request_agent_override_fn = config_with_request_agent_override
        self._context_transform_registry_for_agent_fn = context_transform_registry_for_agent
        self._context_window_policy_for_provider_attempt_fn = context_window_policy_for_provider_attempt
        self._workspace = workspace
        self._global_skills_config = global_skills_config
        self._skill_registry = skill_registry
        self._skill_registry_is_injected = skill_registry_is_injected
        self._mcp_manager = mcp_manager
        self._mcp_manager_is_injected = mcp_manager_is_injected
        self._graph_override_present_fn = graph_override_present
        self._request_mcp_tool_fn = request_mcp_tool

    def runtime_config_for_request(self, request: RuntimeRequest) -> EffectiveRuntimeConfig:
        resolved = self._surface.effective_runtime_config_from_metadata(None)
        request_agent = request.metadata.get("agent")
        if request_agent is not None:
            try:
                resolved = self._config_with_request_agent_override_fn(
                    resolved,
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
                registry=self._context_transform_registry_for_agent_fn(resolved.agent),
            )
        resolved = apply_request_runtime_config_overrides(
            resolved,
            reasoning_effort=request.metadata.get("reasoning_effort"),
            context_transform_refs=context_transform_refs,
        )
        try:
            validate_reasoning_effort_capability(resolved, self._surface.reasoning_effort_capability(resolved))
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        return resolved

    def prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
        abort_signal: ProviderAbortSignal | None = None,  # noqa: ARG002 — retained by RuntimeSurface protocol for abort-aware callers.
        before_compact: BeforeCompactInput | None = None,
    ) -> RuntimeContextWindow:
        effective_config = self._surface.effective_runtime_config_from_metadata(session_metadata)
        provider_attempt = provider_attempt_from_metadata(session_metadata)
        if policy is None:
            policy = context_window_policy_from_config(effective_config.context_window)
        policy = self._context_window_policy_for_provider_attempt_fn(
            policy,
            resolved_provider=effective_config.resolved_provider,
            provider_attempt=provider_attempt,
        )
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

    def context_budget_for_effective_config(self, effective_config: EffectiveRuntimeConfig) -> int | None:
        """The catalog-derived window that sizes this model's compaction."""
        return self._context_budget_for(effective_config)

    @staticmethod
    def _context_budget_for(effective_config: EffectiveRuntimeConfig) -> int | None:
        """The window that sizes this model's compaction, ``None`` when nothing sizes it.

        The model's own input cap wins over its whole window: the catalog derives
        ``max_input_tokens`` from ``limit.input`` when upstream carries one and from
        ``context_window - max_output_tokens`` otherwise, and that is the space a
        request may actually use. A model the shipped catalog does not describe
        leaves compaction unsized, exactly as before.
        """
        selection = effective_config.resolved_provider.active_target.selection
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
            return skill_registry_with_builtins(())
        if skills_config.paths:
            discovered = SkillRegistry.discover(
                workspace=workspace,
                search_paths=skills_config.paths,
            )
        else:
            discovered = SkillRegistry.discover(workspace=workspace)
        return skill_registry_with_builtins(discovered.all())

    def build_skill_registry(self, skills_config: RuntimeSkillsConfig | None) -> SkillRegistry:
        return self.build_skill_registry_for_workspace(self._workspace, skills_config)

    @staticmethod
    def build_lsp_tool_for_manager(
        lsp_manager: LspManager | None,
        *,
        request_lsp: Callable[..., LspRequestResult],
    ) -> Tool | None:
        if lsp_manager is None or lsp_manager.current_state().mode != "managed":
            return None
        from ...tools.lsp import LspTool

        return LspTool(requester=request_lsp)

    def skills_config_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeSkillsConfig | None:
        if effective_config.agent is not None and effective_config.agent.skills is not None:
            return effective_config.agent.skills
        return self._global_skills_config

    def skill_registry_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> SkillRegistry:
        if self._skill_registry_is_injected:
            assert self._skill_registry is not None
            return self._skill_registry
        return self.build_skill_registry(self.skills_config_for_effective_config(effective_config))

    @staticmethod
    def mcp_tools_from_descriptors(
        descriptors: Iterable[McpToolDescriptor],
        *,
        request_mcp_tool: McpRequester,
    ) -> tuple[Tool, ...]:
        from ...tools.mcp import McpTool

        return tuple(
            McpTool(
                server_name=tool.server_name,
                tool_name=tool.tool_name,
                description=tool.description,
                input_schema=tool.input_schema,
                safety=tool.safety,
                requester=request_mcp_tool,
            )
            for tool in descriptors
            if tool.enabled
        )

    def build_mcp_tools(self) -> tuple[Tool, ...]:
        context = current_runtime_tool_context()
        return self.build_mcp_tools_for_owner(owner_session_id=context.session_id if context is not None else None)

    def build_mcp_tools_for_owner(self, *, owner_session_id: str | None) -> tuple[Tool, ...]:
        if self._mcp_manager is None or self._mcp_manager.current_state().mode != "managed":
            return ()
        assert self._request_mcp_tool_fn is not None
        return self.mcp_tools_from_descriptors(
            self._mcp_manager.list_tools(
                workspace=self._workspace,
                owner_session_id=owner_session_id,
            ),
            request_mcp_tool=self._request_mcp_tool_fn,
        )

    def mcp_cached_surface(self, *, owner_session_id: str | None) -> McpCachedToolSurface:
        """Passively remembered MCP surface for the configured servers."""
        if self._mcp_manager is None:
            return McpCachedToolSurface()
        if self._mcp_manager.current_state().mode != "managed":
            return McpCachedToolSurface()
        return self._mcp_manager.cached_surface(workspace=self._workspace, owner_session_id=owner_session_id)

    @staticmethod
    def is_background_child_mcp_deferred(
        *,
        request_metadata: Mapping[str, object],
        effective_config: EffectiveRuntimeConfig,
    ) -> bool:
        if request_metadata.get("background_run") is not True:
            return False
        agent_binding = effective_config.agent.mcp_binding if effective_config.agent is not None else None
        if agent_binding is not None:
            return False
        return True

    def should_skip_mcp_startup_for_request(
        self,
        *,
        request_metadata: Mapping[str, object],
        effective_config: EffectiveRuntimeConfig,
    ) -> bool:
        _ = request_metadata
        if self._mcp_manager_is_injected:
            return False
        if self._mcp_manager is None:
            return True
        configured_servers = set(self._mcp_manager.current_state().configuration.servers)
        builtin_servers = {"context7", "websearch", "grep_app"}
        if not configured_servers <= builtin_servers:
            return False
        assert self._graph_override_present_fn is not None
        return effective_config.execution_engine == "deterministic" or self._graph_override_present_fn()
