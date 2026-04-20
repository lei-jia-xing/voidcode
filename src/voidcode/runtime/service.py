from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, final, runtime_checkable
from uuid import uuid4

from ..acp import AcpRequestEnvelope, AcpResponseEnvelope
from ..agent import get_builtin_agent_manifest
from ..graph.contracts import GraphEvent, GraphRunRequest
from ..graph.read_only_slice import DeterministicReadOnlyGraph
from ..graph.single_agent_slice import ProviderSingleAgentGraph
from ..hook.executor import HookExecutionOutcome, HookExecutionRequest, run_tool_hooks
from ..provider.auth import ProviderAuthResolver
from ..provider.errors import (
    SingleAgentContextLimitError,
    classify_provider_error,
    format_fallback_exhausted_error,
    format_invalid_provider_config_error,
)
from ..provider.models import (
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from ..provider.registry import ModelProviderRegistry
from ..provider.resolution import resolve_provider_config
from ..provider.snapshot import (
    parse_resolved_provider_snapshot,
    resolved_provider_snapshot,
)
from ..skills import SkillRegistry
from ..tools.contracts import Tool, ToolCall, ToolDefinition, ToolResult, ToolResultStatus
from .acp import AcpAdapter, AcpAdapterState, build_acp_adapter
from .config import (
    ExecutionEngineName,
    RuntimeAgentConfig,
    RuntimeConfig,
    RuntimeHooksConfig,
    RuntimePlanConfig,
    RuntimeProviderFallbackConfig,
    load_runtime_config,
    parse_provider_fallback_payload,
    parse_runtime_agent_payload,
    parse_runtime_plan_payload,
    serialize_provider_fallback_config,
    serialize_runtime_agent_config,
    serialize_runtime_plan_config,
)
from .context_window import (
    ContextWindowPolicy,
    RuntimeContextWindow,
    RuntimeContinuityState,
    prepare_single_agent_context,
)
from .contracts import (
    RuntimeNotification,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeResponse,
    RuntimeSessionResult,
    RuntimeStreamChunk,
    validate_session_id,
    validate_session_reference_id,
)
from .events import (
    RUNTIME_ACP_CONNECTED,
    RUNTIME_ACP_DISCONNECTED,
    RUNTIME_ACP_FAILED,
    RUNTIME_FAILED,
    RUNTIME_LSP_SERVER_FAILED,
    RUNTIME_LSP_SERVER_REUSED,
    RUNTIME_LSP_SERVER_STARTED,
    RUNTIME_LSP_SERVER_STARTUP_REJECTED,
    RUNTIME_LSP_SERVER_STOPPED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_SKILLS_LOADED,
    EventEnvelope,
)
from .lsp import LspManager, LspManagerState, LspRequest, LspRequestResult, build_lsp_manager
from .mcp import McpManager, build_mcp_manager
from .permission import (
    PendingApproval,
    PermissionDecision,
    PermissionPolicy,
    PermissionResolution,
    resolve_permission,
)
from .plan import PlanContributor, apply_plan_patch, build_plan_contributor
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .single_agent_provider import ProviderExecutionError
from .skills import (
    SkillRuntimeContext,
    build_runtime_context,
    build_runtime_contexts,
    build_skill_prompt_context,
    runtime_context_from_payload,
)
from .storage import SessionStore, SqliteSessionStore
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
    validate_background_task_id,
)
from .tool_provider import BuiltinToolProvider

if TYPE_CHECKING:
    from ..tools.lsp import FormatTool

logger = logging.getLogger(__name__)

_EXECUTABLE_AGENT_PRESETS = frozenset({"leader"})


@dataclass(frozen=True, slots=True)
class _ActiveSessionKey:
    workspace: Path
    session_id: str


class _ActiveSessionRegistry:
    def __init__(self) -> None:
        self._counts: dict[_ActiveSessionKey, int] = {}
        self._lock = threading.Lock()

    def register(self, *, workspace: Path, session_id: str) -> None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def unregister(self, *, workspace: Path, session_id: str) -> None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            count = self._counts.get(key)
            if count is None:
                return
            if count <= 1:
                self._counts.pop(key, None)
                return
            self._counts[key] = count - 1

    def contains(self, *, workspace: Path, session_id: str) -> bool:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            return key in self._counts


_ACTIVE_SESSION_REGISTRY = _ActiveSessionRegistry()


def _coerce_bool_like(value: object | None, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in {"false", "0", "no", "off", ""}


@runtime_checkable
class GraphStep(Protocol):
    @property
    def tool_call(self) -> ToolCall | None: ...

    @property
    def events(self) -> tuple[Any, ...]: ...

    @property
    def output(self) -> str | None: ...

    @property
    def is_finished(self) -> bool: ...


class RuntimeGraph(Protocol):
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep: ...


@dataclass(slots=True)
class ToolRegistry:
    """Small in-memory registry used by the runtime boundary."""

    tools: dict[str, Tool] = field(default_factory=dict)

    @classmethod
    def from_tools(cls, tools: Iterable[Tool]) -> ToolRegistry:
        return cls(tools={tool.definition.name: tool for tool in tools})

    @classmethod
    def with_defaults(
        cls,
        *,
        lsp_tool: Tool | None = None,
        format_tool: Tool | None = None,
        mcp_tools: tuple[Tool, ...] = (),
        hooks_config: RuntimeHooksConfig | None = None,
    ) -> ToolRegistry:
        return cls.from_tools(
            BuiltinToolProvider(
                lsp_tool=lsp_tool,
                format_tool=format_tool,
                mcp_tools=mcp_tools,
                hooks_config=hooks_config,
            ).provide_tools()
        )

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self.tools.values())

    def resolve(self, tool_name: str) -> Tool:
        try:
            return self.tools[tool_name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_name}") from exc

    def filtered(self, patterns: Iterable[str]) -> ToolRegistry:
        normalized_patterns = tuple(pattern for pattern in patterns if pattern)
        if not normalized_patterns:
            return self
        return ToolRegistry(
            tools={
                name: tool
                for name, tool in self.tools.items()
                if any(fnmatchcase(name, pattern) for pattern in normalized_patterns)
            }
        )


@final
class VoidCodeRuntime:
    """Headless runtime entrypoint for one local deterministic request."""

    _workspace: Path
    _base_tool_registry: ToolRegistry
    _tool_registry: ToolRegistry
    _graph: RuntimeGraph
    _graph_override: RuntimeGraph | None
    _config: RuntimeConfig
    _initial_effective_config: EffectiveRuntimeConfig
    _permission_policy: PermissionPolicy
    _session_store: SessionStore
    _model_provider_registry: ModelProviderRegistry
    _provider_model: ResolvedProviderModel
    _provider_chain: ResolvedProviderChain
    _provider_auth_resolver: ProviderAuthResolver
    _skill_registry: SkillRegistry
    _lsp_manager: LspManager
    _mcp_manager: McpManager
    _acp_adapter: AcpAdapter
    _graph_cache: dict[tuple[ExecutionEngineName, str], RuntimeGraph]
    _plan_contributor: PlanContributor
    _background_task_threads: dict[str, threading.Thread]
    _background_tasks_reconciled: bool
    _hook_recursion_env_var = "VOIDCODE_RUNNING_TOOL_HOOK"
    _default_context_window_policy = ContextWindowPolicy()

    def __init__(
        self,
        *,
        workspace: Path,
        tool_registry: ToolRegistry | None = None,
        graph: RuntimeGraph | None = None,
        config: RuntimeConfig | None = None,
        permission_policy: PermissionPolicy | None = None,
        session_store: SessionStore | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        lsp_manager: LspManager | None = None,
        mcp_manager: McpManager | None = None,
        acp_adapter: AcpAdapter | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._config = config or load_runtime_config(self._workspace)
        self._model_provider_registry = (
            model_provider_registry
            or ModelProviderRegistry.with_defaults(provider_configs=self._config.providers)
        )
        initial_agent = self._config.agent
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent),
                source="runtime config agent",
            )
            assert initial_agent is not None
            self._validate_runtime_agent_for_execution(
                initial_agent,
                source="runtime config agent",
            )
        initial_model = (
            initial_agent.model
            if initial_agent is not None and initial_agent.model is not None
            else self._config.model
        )
        initial_execution_engine = (
            initial_agent.execution_engine
            if initial_agent is not None and initial_agent.execution_engine is not None
            else self._config.execution_engine
        )
        initial_provider_fallback = (
            initial_agent.provider_fallback
            if initial_agent is not None and initial_agent.provider_fallback is not None
            else self._config.provider_fallback
        )
        self._resolved_provider_config = resolve_provider_config(
            initial_model,
            initial_provider_fallback,
            registry=self._model_provider_registry,
        )
        self._provider_model = self._resolved_provider_config.active_target
        self._provider_chain = self._resolved_provider_config.target_chain
        self._provider_auth_resolver = ProviderAuthResolver(
            providers=self._config.providers,
            env=os.environ,
        )
        self._lsp_manager = lsp_manager or build_lsp_manager(self._config.lsp)
        self._mcp_manager = mcp_manager or build_mcp_manager(self._config.mcp)
        self._base_tool_registry = tool_registry or ToolRegistry.with_defaults(
            lsp_tool=self._build_lsp_tool(),
            format_tool=self._build_format_tool(),
            hooks_config=self._config.hooks or RuntimeHooksConfig(),
        )
        self._tool_registry = self._base_tool_registry
        self._graph_override = graph
        self._graph_cache = {}
        self._initial_effective_config = EffectiveRuntimeConfig(
            approval_mode=self._config.approval_mode,
            model=initial_model,
            execution_engine=initial_execution_engine,
            max_steps=self._config.max_steps,
            provider_fallback=initial_provider_fallback,
            plan=self._config.plan,
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
        )
        self._graph = graph or self._build_graph_for_engine_from_config(
            self._initial_effective_config
        )
        self._permission_policy = permission_policy or PermissionPolicy(
            mode=self._config.approval_mode
        )
        self._session_store = session_store or SqliteSessionStore()
        self._skill_registry = skill_registry or self._build_skill_registry()
        self._acp_adapter = acp_adapter or build_acp_adapter(self._config.acp)
        self._plan_contributor = build_plan_contributor(self._workspace, self._config.plan)
        self._background_task_threads = {}
        self._background_tasks_reconciled = False
        self._default_context_window_policy = context_window_policy or ContextWindowPolicy()

    def __enter__(self) -> VoidCodeRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type, exc, tb
        _ = self.disconnect_acp()
        _ = self.shutdown_mcp()
        _ = self.shutdown_lsp()

    @staticmethod
    def _session_with_metadata(session: SessionState, metadata: dict[str, object]) -> SessionState:
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata=metadata,
        )

    def _runtime_state_metadata_with_acp_state(
        self,
        metadata: dict[str, object],
        acp_state: AcpAdapterState,
    ) -> dict[str, object]:
        runtime_state = metadata.get("runtime_state")
        if runtime_state is None:
            runtime_state_metadata: dict[str, object] = {}
        elif isinstance(runtime_state, dict):
            runtime_state_metadata = dict(cast(dict[str, object], runtime_state))
        else:
            runtime_state_metadata = {}
        runtime_state_metadata["acp"] = {
            "mode": acp_state.mode,
            "configured_enabled": acp_state.configuration.configured_enabled,
            "status": acp_state.status,
            "available": acp_state.available,
            "last_error": acp_state.last_error,
        }
        return {**metadata, "runtime_state": runtime_state_metadata}

    def _session_with_current_acp_metadata(self, session: SessionState) -> SessionState:
        return self._session_with_metadata(
            session,
            self._runtime_state_metadata_with_acp_state(
                session.metadata,
                self._acp_adapter.current_state(),
            ),
        )

    def _disconnect_acp_for_session_state(self, session: SessionState) -> SessionState:
        _ = self._acp_adapter.disconnect()
        return self._session_with_current_acp_metadata(session)

    def _reload_persisted_session(self, *, session_id: str) -> SessionState:
        response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        return response.session

    @staticmethod
    def _resequence_event(event: EventEnvelope, *, sequence: int) -> EventEnvelope:
        return EventEnvelope(
            session_id=event.session_id,
            sequence=sequence,
            event_type=event.event_type,
            source=event.source,
            payload=event.payload,
        )

    def _emit_acp_events(
        self,
        *,
        session: SessionState,
        start_sequence: int,
        acp_events: tuple[object, ...],
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        emitted: list[RuntimeStreamChunk] = []
        current_session = session
        sequence = start_sequence - 1
        for acp_event in self._envelopes_for_acp_events(
            session_id=session.session.id,
            start_sequence=start_sequence,
            acp_events=acp_events,
        ):
            sequence = acp_event.sequence
            current_session = self._session_with_current_acp_metadata(current_session)
            emitted.append(
                RuntimeStreamChunk(kind="event", session=current_session, event=acp_event)
            )
        return tuple(emitted), current_session, sequence

    def _emit_current_acp_drain(
        self,
        *,
        session: SessionState,
        start_sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        return self._emit_acp_events(
            session=session,
            start_sequence=start_sequence,
            acp_events=self._acp_adapter.drain_events(),
        )

    def _start_run_acp(
        self,
        *,
        session: SessionState,
        sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int, RuntimeStreamChunk | None]:
        if self.current_acp_state().configuration.configured_enabled is not True:
            return (), session, sequence, None
        try:
            acp_events = self._acp_adapter.connect()
        except Exception as exc:
            emitted, updated_session, last_sequence = self._emit_current_acp_drain(
                session=session,
                start_sequence=sequence + 1,
            )
            failed_session = self._session_with_current_acp_metadata(updated_session)
            failed_chunk = self._failed_chunk(
                session=failed_session,
                sequence=last_sequence + 1,
                error=str(exc),
                payload={"kind": "acp_startup_failed"},
            )
            return emitted, failed_session, last_sequence + 1, failed_chunk
        emitted, updated_session, last_sequence = self._emit_acp_events(
            session=session,
            start_sequence=sequence + 1,
            acp_events=acp_events,
        )
        if not emitted:
            updated_session = self._session_with_current_acp_metadata(updated_session)
        return emitted, updated_session, last_sequence or sequence, None

    def _finalize_run_acp(
        self,
        *,
        session: SessionState,
        sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        if self.current_acp_state().configuration.configured_enabled is not True:
            return (), session, sequence
        emitted, updated_session, last_sequence = self._emit_acp_events(
            session=session,
            start_sequence=sequence + 1,
            acp_events=self._acp_adapter.disconnect(),
        )
        if not emitted:
            updated_session = self._session_with_current_acp_metadata(updated_session)
        return emitted, updated_session, last_sequence or sequence

    @staticmethod
    def _build_graph_for_engine(
        engine_name: ExecutionEngineName,
        provider_model: ResolvedProviderModel,
        max_steps: int,
    ) -> RuntimeGraph:
        if engine_name == "deterministic":
            return DeterministicReadOnlyGraph(max_steps=max_steps)
        if engine_name == "single_agent":
            if provider_model.provider is None:
                raise ValueError("single_agent execution engine requires a configured model")
            return ProviderSingleAgentGraph(
                provider=provider_model.provider.single_agent_provider(),
                provider_model=provider_model,
                max_steps=max_steps,
            )

    def _build_graph_for_engine_from_config(self, config: EffectiveRuntimeConfig) -> RuntimeGraph:
        # Generate cache key from config
        model_str = config.model if config.model is not None else ""
        provider_fallback_key = (
            ""
            if config.provider_fallback is None
            else "|".join(
                (
                    config.provider_fallback.preferred_model,
                    *config.provider_fallback.fallback_models,
                )
            )
        )
        cache_key = (
            config.execution_engine,
            f"{model_str}::{provider_fallback_key}::{config.max_steps}",
        )

        # Check cache first
        if cache_key in self._graph_cache:
            return self._graph_cache[cache_key]

        # Build new graph and cache it
        provider_model = config.resolved_provider.active_target
        graph = self._build_graph_for_engine(
            config.execution_engine,
            provider_model,
            config.max_steps,
        )
        self._graph_cache[cache_key] = graph
        return graph

    def _runtime_config_for_request(self, request: RuntimeRequest) -> EffectiveRuntimeConfig:
        resolved = self._effective_runtime_config_from_metadata(None)
        request_agent = request.metadata.get("agent")
        if request_agent is not None:
            resolved = self._config_with_request_agent_override(resolved, request_agent)
        request_max_steps = request.metadata.get("max_steps")
        if request_max_steps is not None:
            if not isinstance(request_max_steps, int) or isinstance(request_max_steps, bool):
                raise ValueError(
                    "request metadata 'max_steps' must be an integer greater than or equal to 1"
                )
            if request_max_steps < 1:
                raise ValueError("request metadata 'max_steps' must be at least 1")
            return EffectiveRuntimeConfig(
                approval_mode=resolved.approval_mode,
                model=resolved.model,
                execution_engine=resolved.execution_engine,
                max_steps=request_max_steps,
                provider_fallback=resolved.provider_fallback,
                plan=resolved.plan,
                resolved_provider=resolved.resolved_provider,
                agent=resolved.agent,
            )
        return resolved

    def _build_skill_registry(self) -> SkillRegistry:
        skills_config = self._config.skills
        if skills_config is None or skills_config.enabled is not True:
            return SkillRegistry()
        if skills_config.paths:
            return SkillRegistry.discover(
                workspace=self._workspace,
                search_paths=skills_config.paths,
            )
        return SkillRegistry.discover(workspace=self._workspace)

    def _build_lsp_tool(self) -> Tool | None:
        if self._lsp_manager.current_state().mode != "managed":
            return None
        from ..tools.lsp import LspTool

        return LspTool(requester=self.request_lsp)

    def _build_format_tool(self) -> FormatTool:
        from ..tools.lsp import FormatTool

        return FormatTool(self._config.hooks or RuntimeHooksConfig(), self._workspace)

    def _build_mcp_tools(self) -> tuple[Tool, ...]:
        if self._mcp_manager.current_state().mode != "managed":
            return ()
        from ..tools.mcp import McpTool

        return tuple(
            McpTool(
                server_name=tool.server_name,
                tool_name=tool.tool_name,
                description=tool.description,
                input_schema=tool.input_schema,
                requester=self.request_mcp_tool,
            )
            for tool in self._mcp_manager.list_tools(workspace=self._workspace)
        )

    def _refresh_mcp_tools(self) -> None:
        if self._mcp_manager.current_state().mode != "managed":
            return
        merged_tools = dict(self._base_tool_registry.tools)
        for tool in self._build_mcp_tools():
            merged_tools[tool.definition.name] = tool
        self._tool_registry = ToolRegistry(tools=merged_tools)

    def _tool_registry_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> ToolRegistry:
        agent = effective_config.agent
        if agent is None:
            return self._tool_registry

        scoped_registry = self._tool_registry
        manifest = get_builtin_agent_manifest(agent.preset)
        if manifest is not None and manifest.tool_allowlist:
            scoped_registry = scoped_registry.filtered(manifest.tool_allowlist)

        if agent.tools is not None:
            if agent.tools.allowlist:
                scoped_registry = scoped_registry.filtered(agent.tools.allowlist)
            if agent.tools.default:
                scoped_registry = scoped_registry.filtered(agent.tools.default)

        return scoped_registry

    def current_lsp_state(self) -> LspManagerState:
        return self._lsp_manager.current_state()

    @property
    def provider_auth_resolver(self) -> ProviderAuthResolver:
        return self._provider_auth_resolver

    def request_lsp(
        self,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> LspRequestResult:
        return self._lsp_manager.request(
            LspRequest(
                server_name=server_name,
                method=method,
                params=params,
                workspace=workspace,
            )
        )

    def request_mcp_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ):
        return self._mcp_manager.call_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            workspace=workspace,
        )

    def shutdown_mcp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_mcp_events(
            session_id="runtime",
            start_sequence=1,
            mcp_events=self._mcp_manager.shutdown(),
        )

    def shutdown_lsp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_lsp_events(
            session_id="runtime",
            start_sequence=1,
            lsp_events=self._lsp_manager.shutdown(),
        )

    def current_acp_state(self):
        return self._acp_adapter.current_state()

    def connect_acp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_acp_events(
            session_id="runtime",
            start_sequence=1,
            acp_events=self._acp_adapter.connect(),
        )

    def disconnect_acp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_acp_events(
            session_id="runtime",
            start_sequence=1,
            acp_events=self._acp_adapter.disconnect(),
        )

    def request_acp(self, *, request_type: str, payload: dict[str, object]) -> AcpResponseEnvelope:
        return self._acp_adapter.request(
            AcpRequestEnvelope(request_type=request_type, payload=payload)
        )

    def fail_acp(self, message: str) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_acp_events(
            session_id="runtime",
            start_sequence=1,
            acp_events=self._acp_adapter.fail(message),
        )

    def _run_with_persistence(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        request = self._validated_request(request)
        session_id = self._resolve_session_id(request)
        self._register_active_session_id(session_id)
        try:
            events: list[EventEnvelope] = []
            output: str | None = None
            final_session: SessionState | None = None

            try:
                for chunk in self._stream_chunks(request, session_id=session_id):
                    final_session = chunk.session
                    if chunk.event is not None:
                        events.append(chunk.event)
                    if chunk.kind == "output":
                        if output is not None:
                            raise ValueError("runtime stream emitted multiple output chunks")
                        output = chunk.output
                    yield chunk
            except Exception:
                if final_session is not None and final_session.status == "failed":
                    final_session = self._disconnect_acp_for_session_state(final_session)
                    response = RuntimeResponse(
                        session=final_session, events=tuple(events), output=output
                    )
                    self._persist_response(request=request, response=response)
                raise

            if final_session is None:
                raise ValueError("runtime stream emitted no chunks")

            if final_session.status == "waiting":
                final_session = self._disconnect_acp_for_session_state(final_session)

            response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
            self._persist_response(request=request, response=response)
        finally:
            self._unregister_active_session_id(session_id)

    def run(self, request: RuntimeRequest) -> RuntimeResponse:
        events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self._run_with_persistence(request):
            final_session = chunk.session
            if chunk.event is not None:
                events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        if final_session.status == "waiting":
            final_session = self._reload_persisted_session(session_id=final_session.session.id)

        return RuntimeResponse(session=final_session, events=tuple(events), output=output)

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        if "provider_stream" in request.metadata:
            return self._run_with_persistence(request)

        request_with_stream = RuntimeRequest(
            prompt=request.prompt,
            session_id=request.session_id,
            parent_session_id=request.parent_session_id,
            metadata={**request.metadata, "provider_stream": True},
            allocate_session_id=request.allocate_session_id,
        )
        return self._run_with_persistence(request_with_stream)

    def _stream_chunks(
        self,
        request: RuntimeRequest,
        *,
        session_id: str | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        resolved_session_id = session_id or self._resolve_session_id(request)
        effective_config = self._runtime_config_for_request(request)
        request_metadata = self._fresh_request_metadata(request.metadata)
        self._refresh_mcp_tools()
        tool_registry = self._tool_registry_for_effective_config(effective_config)
        session = SessionState(
            session=SessionRef(id=resolved_session_id, parent_id=request.parent_session_id),
            status="running",
            turn=1,
            metadata={
                **request_metadata,
                "workspace": str(self._workspace),
                "runtime_config": self._runtime_config_metadata(effective_config),
                "runtime_state": self._runtime_state_metadata(),
            },
        )
        sequence = 1

        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": request.prompt},
            ),
        )

        sequence += 1
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type=RUNTIME_SKILLS_LOADED,
                source="runtime",
                payload={"skills": self._loaded_skill_names()},
            ),
        )

        startup_chunks, session, sequence, startup_failed_chunk = self._start_run_acp(
            session=session,
            sequence=sequence,
        )
        for chunk in startup_chunks:
            yield chunk
        if startup_failed_chunk is not None:
            yield startup_failed_chunk
            return

        applied_skill_contexts = self._applied_skill_contexts(session.metadata)
        frozen_applied_skills = self._frozen_applied_skill_payloads(applied_skill_contexts)
        skill_prompt_context = build_skill_prompt_context(applied_skill_contexts)
        if self._config.skills is not None and self._config.skills.enabled is True:
            session = SessionState(
                session=session.session,
                status=session.status,
                turn=session.turn,
                metadata={
                    **session.metadata,
                    "applied_skills": [skill.name for skill in applied_skill_contexts],
                    "applied_skill_payloads": list(frozen_applied_skills),
                },
            )
        if applied_skill_contexts:
            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type=RUNTIME_SKILLS_APPLIED,
                    source="runtime",
                    payload={
                        "skills": [skill.name for skill in applied_skill_contexts],
                        "count": len(applied_skill_contexts),
                        "prompt_context_built": bool(skill_prompt_context),
                        "prompt_context_length": len(skill_prompt_context),
                    },
                ),
            )

        planned_prompt, planned_metadata = apply_plan_patch(
            contributor=self._plan_contributor,
            prompt=request.prompt,
            metadata=request_metadata,
        )

        graph_request = GraphRunRequest(
            session=session,
            prompt=planned_prompt,
            available_tools=tool_registry.definitions(),
            applied_skills=frozen_applied_skills,
            skill_prompt_context=skill_prompt_context,
            context_window=self._prepare_single_agent_context_window(
                prompt=planned_prompt,
                tool_results=(),
                session_metadata=session.metadata,
            ),
            metadata={
                **planned_metadata,
                "agent_preset": serialize_runtime_agent_config(
                    self._effective_runtime_config_from_metadata(session.metadata).agent
                ),
                "provider_attempt": 0,
                "provider_stream": _coerce_bool_like(
                    planned_metadata.get("provider_stream", False),
                    False,
                ),
            },
        )
        tool_results: list[ToolResult] = []
        graph = self._graph_for_session_metadata(session.metadata)

        last_chunk: RuntimeStreamChunk | None = None
        last_sequence = sequence
        for chunk in self._execute_graph_loop(
            graph=graph,
            tool_registry=tool_registry,
            session=session,
            sequence=sequence,
            graph_request=graph_request,
            tool_results=tool_results,
            permission_policy=self._permission_policy,
        ):
            last_chunk = chunk
            if chunk.event is not None:
                last_sequence = chunk.event.sequence
            yield chunk

        if last_chunk is None:
            return

        if last_chunk.session.status == "waiting":
            return

        final_chunks, _, _ = self._finalize_run_acp(
            session=last_chunk.session,
            sequence=last_sequence,
        )
        for chunk in final_chunks:
            yield chunk

    def _execute_graph_loop(
        self,
        *,
        graph: RuntimeGraph,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None = None,
        permission_policy: PermissionPolicy | None = None,
        preserved_continuity_state: RuntimeContinuityState | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        active_permission_policy = permission_policy or self._permission_policy
        provider_attempt = cast(int, graph_request.metadata.get("provider_attempt", 0))
        active_preserved_continuity_state = preserved_continuity_state
        while True:
            context_window = self._prepare_single_agent_context_window(
                prompt=graph_request.prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            )
            if active_preserved_continuity_state is not None:
                context_window = RuntimeContextWindow(
                    prompt=context_window.prompt,
                    tool_results=context_window.tool_results,
                    compacted=context_window.compacted,
                    compaction_reason=context_window.compaction_reason,
                    original_tool_result_count=context_window.original_tool_result_count,
                    retained_tool_result_count=context_window.retained_tool_result_count,
                    max_tool_result_count=context_window.max_tool_result_count,
                    continuity_state=active_preserved_continuity_state,
                )
            session = self._session_with_context_window_metadata(session, context_window)
            graph_request = GraphRunRequest(
                session=session,
                prompt=graph_request.prompt,
                available_tools=graph_request.available_tools,
                applied_skills=graph_request.applied_skills,
                skill_prompt_context=graph_request.skill_prompt_context,
                context_window=context_window,
                metadata=graph_request.metadata,
            )
            if context_window.compacted and active_preserved_continuity_state is None:
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type=RUNTIME_MEMORY_REFRESHED,
                        source="runtime",
                        payload={
                            "reason": context_window.compaction_reason,
                            "original_tool_result_count": context_window.original_tool_result_count,
                            "retained_tool_result_count": context_window.retained_tool_result_count,
                            "compacted": True,
                            "continuity_state": (
                                context_window.continuity_state.metadata_payload()
                                if context_window.continuity_state is not None
                                else None
                            ),
                        },
                    ),
                )
            try:
                graph_step = graph.step(
                    graph_request,
                    tool_results=tuple(tool_results),
                    session=session,
                )
            except Exception as exc:
                if isinstance(exc, ProviderExecutionError):
                    if exc.kind == "cancelled":
                        yield self._failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=str(exc),
                            payload={
                                "provider_error_kind": exc.kind,
                                "provider": exc.provider_name,
                                "model": exc.model_name,
                                "cancelled": True,
                            },
                        )
                        return
                    next_attempt = provider_attempt + 1
                    provider_chain = self._provider_chain_for_session_metadata(session.metadata)
                    all_targets = provider_chain.all_targets
                    next_target = (
                        all_targets[next_attempt] if next_attempt < len(all_targets) else None
                    )
                    if (
                        exc.kind in {"rate_limit", "invalid_model", "transient_failure"}
                        and next_target is not None
                    ):
                        logger.info(
                            (
                                "provider fallback for session %s: %s/%s -> %s/%s "
                                "(reason=%s, attempt=%s)"
                            ),
                            session.session.id,
                            exc.provider_name,
                            exc.model_name,
                            next_target.selection.provider,
                            next_target.selection.model,
                            exc.kind,
                            next_attempt,
                        )
                        sequence += 1
                        yield RuntimeStreamChunk(
                            kind="event",
                            session=session,
                            event=EventEnvelope(
                                session_id=session.session.id,
                                sequence=sequence,
                                event_type="runtime.provider_fallback",
                                source="runtime",
                                payload={
                                    "reason": exc.kind,
                                    "from_provider": exc.provider_name,
                                    "from_model": exc.model_name,
                                    "to_provider": next_target.selection.provider,
                                    "to_model": next_target.selection.model,
                                    "attempt": next_attempt,
                                    **(
                                        {"provider_error_details": exc.details}
                                        if exc.details is not None
                                        else {}
                                    ),
                                },
                            ),
                        )
                        provider_attempt = next_attempt
                        session = SessionState(
                            session=session.session,
                            status=session.status,
                            turn=session.turn,
                            metadata={
                                **session.metadata,
                                "provider_attempt": provider_attempt,
                            },
                        )
                        effective_config = self._effective_runtime_config_from_metadata(
                            session.metadata
                        )
                        graph = self._build_graph_for_engine(
                            effective_config.execution_engine,
                            next_target,
                            effective_config.max_steps,
                        )
                        graph_request = GraphRunRequest(
                            session=session,
                            prompt=graph_request.prompt,
                            available_tools=graph_request.available_tools,
                            applied_skills=graph_request.applied_skills,
                            skill_prompt_context=graph_request.skill_prompt_context,
                            metadata={
                                **graph_request.metadata,
                                "provider_attempt": provider_attempt,
                            },
                        )
                        continue
                    if exc.kind in {"rate_limit", "invalid_model", "transient_failure"}:
                        yield self._failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=format_fallback_exhausted_error(
                                provider_name=exc.provider_name,
                                model_name=exc.model_name,
                                attempt=next_attempt,
                            ),
                            payload={
                                "provider_error_kind": exc.kind,
                                "provider": exc.provider_name,
                                "model": exc.model_name,
                                "fallback_exhausted": True,
                                **(
                                    {"provider_error_details": exc.details}
                                    if exc.details is not None
                                    else {}
                                ),
                            },
                        )
                        return
                if isinstance(exc, ProviderExecutionError):
                    yield self._failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=str(exc),
                        payload={
                            "provider_error_kind": exc.kind,
                            "provider": exc.provider_name,
                            "model": exc.model_name,
                            **(
                                {"provider_error_details": exc.details}
                                if exc.details is not None
                                else {}
                            ),
                        },
                    )
                    return
                classified_error = classify_provider_error(exc)
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=str(exc),
                    payload=(
                        {"kind": "provider_context_limit"}
                        if isinstance(classified_error, SingleAgentContextLimitError)
                        else None
                    ),
                )
                if isinstance(classified_error, SingleAgentContextLimitError):
                    return
                raise

            is_final_step = (
                getattr(graph_step, "is_finished", False)
                or getattr(graph_step, "output", None) is not None
            )
            current_chunk_session = session
            if is_final_step:
                current_chunk_session = SessionState(
                    session=session.session,
                    status="completed",
                    turn=session.turn,
                    metadata=session.metadata,
                )

            for event in self._renumber_events(
                getattr(graph_step, "events", ()),
                session_id=session.session.id,
                start_sequence=sequence + 1,
            ):
                sequence = event.sequence
                yield RuntimeStreamChunk(kind="event", session=current_chunk_session, event=event)

            if is_final_step:
                if getattr(graph_step, "output", None) is not None:
                    yield RuntimeStreamChunk(
                        kind="output",
                        session=current_chunk_session,
                        output=graph_step.output,
                    )
                break

            plan_tool_call = getattr(graph_step, "tool_call", None)
            if plan_tool_call is None:
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="graph step did not produce a tool call or output",
                )
                raise ValueError("graph step did not produce a tool call or output")

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="graph.tool_request_created",
                    source="graph",
                    payload={
                        "tool": plan_tool_call.tool_name,
                        "arguments": dict(plan_tool_call.arguments),
                        **(
                            {"path": path}
                            if isinstance((path := plan_tool_call.arguments.get("path")), str)
                            else {}
                        ),
                    },
                ),
            )

            try:
                tool = tool_registry.resolve(plan_tool_call.tool_name)
            except Exception as exc:
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_lookup_succeeded",
                    source="runtime",
                    payload={"tool": plan_tool_call.tool_name},
                ),
            )

            sequence += 1
            if approval_resolution is not None:
                pending, decision = approval_resolution
                if (
                    plan_tool_call.tool_name == pending.tool_name
                    and dict(plan_tool_call.arguments) == pending.arguments
                ):
                    sequence += 1
                    permission_chunks = self._approval_resolution_outcome(
                        session=session,
                        pending=pending,
                        decision=decision,
                        sequence=sequence,
                    )
                    approval_resolution = None
                else:
                    msg = (
                        f"graph step produced a different tool call "
                        f"({plan_tool_call.tool_name}) than the pending "
                        f"approval ({pending.tool_name})"
                    )
                    raise ValueError(msg)
            else:
                permission_chunks = self._resolve_permission(
                    session=session,
                    tool=tool.definition,
                    tool_call=plan_tool_call,
                    sequence=sequence,
                    permission_policy=active_permission_policy,
                )
            yield from permission_chunks.chunks
            if permission_chunks.pending_approval is not None:
                return
            if permission_chunks.denied:
                return

            sequence = permission_chunks.last_sequence

            pre_hook_outcome = self._run_tool_hooks(
                session=session,
                sequence=sequence,
                tool_name=plan_tool_call.tool_name,
                phase="pre",
            )
            yield from pre_hook_outcome.chunks
            sequence = pre_hook_outcome.last_sequence
            if pre_hook_outcome.failed_error is not None:
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=pre_hook_outcome.failed_error,
                )
                raise RuntimeError(pre_hook_outcome.failed_error)

            try:
                tool_result = tool.invoke(plan_tool_call, workspace=self._workspace)
            except Exception as exc:
                for acp_event in self._envelopes_for_acp_events(
                    session_id=session.session.id,
                    start_sequence=sequence + 1,
                    acp_events=self._acp_adapter.drain_events(),
                ):
                    sequence = acp_event.sequence
                    session = self._session_with_current_acp_metadata(session)
                    yield RuntimeStreamChunk(kind="event", session=session, event=acp_event)
                for mcp_event in self._envelopes_for_mcp_events(
                    session_id=session.session.id,
                    start_sequence=sequence + 1,
                    mcp_events=self._mcp_manager.drain_events(),
                ):
                    sequence = mcp_event.sequence
                    yield RuntimeStreamChunk(kind="event", session=session, event=mcp_event)
                for lsp_event in self._envelopes_for_lsp_events(
                    session_id=session.session.id,
                    start_sequence=sequence + 1,
                    lsp_events=self._lsp_manager.drain_events(),
                ):
                    sequence = lsp_event.sequence
                    yield RuntimeStreamChunk(kind="event", session=session, event=lsp_event)
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            for acp_event in self._envelopes_for_acp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                acp_events=self._acp_adapter.drain_events(),
            ):
                sequence = acp_event.sequence
                session = self._session_with_current_acp_metadata(session)
                yield RuntimeStreamChunk(kind="event", session=session, event=acp_event)

            for mcp_event in self._envelopes_for_mcp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                mcp_events=self._mcp_manager.drain_events(),
            ):
                sequence = mcp_event.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=mcp_event)

            for lsp_event in self._envelopes_for_lsp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                lsp_events=self._lsp_manager.drain_events(),
            ):
                sequence = lsp_event.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=lsp_event)

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        "tool": tool_result.tool_name,
                        "status": tool_result.status,
                        "content": tool_result.content,
                        "error": tool_result.error,
                        **tool_result.data,
                    },
                ),
            )

            post_hook_outcome = self._run_tool_hooks(
                session=session,
                sequence=sequence,
                tool_name=plan_tool_call.tool_name,
                phase="post",
            )
            yield from post_hook_outcome.chunks
            sequence = post_hook_outcome.last_sequence
            if post_hook_outcome.failed_error is not None:
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=post_hook_outcome.failed_error,
                )
                raise RuntimeError(post_hook_outcome.failed_error)

            tool_results.append(tool_result)
            active_preserved_continuity_state = None

    def _run_tool_hooks(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_name: str,
        phase: Literal["pre", "post"],
    ) -> _HookOutcome:
        outcome: HookExecutionOutcome = run_tool_hooks(
            HookExecutionRequest(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session_id=session.session.id,
                tool_name=tool_name,
                phase=phase,
                recursion_env_var=self._hook_recursion_env_var,
                environment=os.environ,
                sequence_start=sequence,
            )
        )
        emitted_chunks = tuple(
            RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    source="runtime",
                    payload=event.payload,
                ),
            )
            for event in outcome.events
        )
        return _HookOutcome(
            chunks=emitted_chunks,
            last_sequence=outcome.last_sequence,
            failed_error=outcome.failed_error,
        )

    def _failed_chunk(
        self,
        *,
        session: SessionState,
        sequence: int,
        error: str,
        payload: dict[str, object] | None = None,
    ) -> RuntimeStreamChunk:
        failed_session = SessionState(
            session=session.session,
            status="failed",
            turn=session.turn,
            metadata=session.metadata,
        )
        failure_payload = {"error": error, **(payload or {})}
        return RuntimeStreamChunk(
            kind="event",
            session=failed_session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type="runtime.failed",
                source="runtime",
                payload=failure_payload,
            ),
        )

    def _persist_response(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None:
        if response.session.status == "waiting":
            pending_approval = self._pending_approval_from_response(response)
            self._session_store.save_pending_approval(
                workspace=self._workspace,
                request=request,
                response=response,
                pending_approval=pending_approval,
            )
            return
        self._session_store.save_run(workspace=self._workspace, request=request, response=response)

    def _resolve_permission(
        self,
        *,
        session: SessionState,
        tool: ToolDefinition,
        tool_call: ToolCall,
        sequence: int,
        permission_policy: PermissionPolicy,
    ) -> _PermissionOutcome:
        permission = resolve_permission(tool, tool_call, policy=permission_policy)
        if tool.read_only:
            return _PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.permission_resolved",
                            source="runtime",
                            payload={"tool": tool_call.tool_name, "decision": "allow"},
                        ),
                    ),
                ),
                last_sequence=sequence,
            )

        pending_approval = permission.pending_approval
        if pending_approval is None:
            raise ValueError("non-read-only permission decisions require pending approval data")
        if permission.decision in ("allow", "deny"):
            return self._approval_resolution_outcome(
                session=session,
                pending=pending_approval,
                decision=permission.decision,
                sequence=sequence,
            )

        pending = pending_approval
        waiting_session = SessionState(
            session=session.session,
            status="waiting",
            turn=session.turn,
            metadata=session.metadata,
        )
        request_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_requested",
            source="runtime",
            payload={
                "request_id": pending.request_id,
                "tool": pending.tool_name,
                "decision": "ask",
                "arguments": pending.arguments,
                "target_summary": pending.target_summary,
                "reason": pending.reason,
                "policy": {"mode": pending.policy_mode},
            },
        )
        return _PermissionOutcome(
            chunks=(
                RuntimeStreamChunk(kind="event", session=waiting_session, event=request_event),
            ),
            last_sequence=sequence,
            pending_approval=pending_approval,
        )

    def _approval_resolution_outcome(
        self,
        *,
        session: SessionState,
        pending: PendingApproval,
        decision: PermissionResolution,
        sequence: int,
    ) -> _PermissionOutcome:
        resolution_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_resolved",
            source="runtime",
            payload={"request_id": pending.request_id, "decision": decision},
        )
        if decision == "deny":
            failed_session = SessionState(
                session=session.session,
                status="failed",
                turn=session.turn,
                metadata=session.metadata,
            )
            failed_event = EventEnvelope(
                session_id=session.session.id,
                sequence=sequence + 1,
                event_type="runtime.failed",
                source="runtime",
                payload={"error": f"permission denied for tool: {pending.tool_name}"},
            )
            return _PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(kind="event", session=session, event=resolution_event),
                    RuntimeStreamChunk(kind="event", session=failed_session, event=failed_event),
                ),
                last_sequence=sequence + 1,
                denied=True,
            )
        return _PermissionOutcome(
            chunks=(RuntimeStreamChunk(kind="event", session=session, event=resolution_event),),
            last_sequence=sequence,
        )

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        return self._session_store.list_sessions(workspace=self._workspace)

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        self._reconcile_background_tasks_if_needed()
        validated_request = self._validated_request(request)
        task_id = f"task-{uuid4().hex}"
        initial_state = BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="queued",
            request=BackgroundTaskRequestSnapshot(
                prompt=validated_request.prompt,
                session_id=validated_request.session_id,
                parent_session_id=validated_request.parent_session_id,
                metadata=dict(validated_request.metadata),
                allocate_session_id=validated_request.allocate_session_id,
            ),
        )
        self._session_store.create_background_task(workspace=self._workspace, task=initial_state)
        worker = threading.Thread(
            target=self._run_background_task_worker,
            args=(task_id,),
            name=f"voidcode-background-task-{task_id}",
            daemon=True,
        )
        self._background_task_threads[task_id] = worker
        worker.start()
        return self.load_background_task(task_id)

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        self._reconcile_background_tasks_if_needed()
        validate_background_task_id(task_id)
        return self._session_store.load_background_task(workspace=self._workspace, task_id=task_id)

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._reconcile_background_tasks_if_needed()
        return self._session_store.list_background_tasks(workspace=self._workspace)

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        validate_background_task_id(task_id)
        self._reconcile_background_tasks_if_needed()
        return self._session_store.request_background_task_cancel(
            workspace=self._workspace,
            task_id=task_id,
        )

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        validate_session_id(session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(result.session, session_id=session_id)
        return result

    def list_notifications(self) -> tuple[RuntimeNotification, ...]:
        notifications = self._session_store.list_notifications(workspace=self._workspace)
        return tuple(
            notification
            for notification in notifications
            if self._session_belongs_to_workspace(notification.session.id)
        )

    def acknowledge_notification(self, *, notification_id: str) -> RuntimeNotification:
        if not notification_id:
            raise ValueError("notification_id must be a non-empty string")
        notification = self._session_store.acknowledge_notification(
            workspace=self._workspace,
            notification_id=notification_id,
        )
        if not self._session_belongs_to_workspace(notification.session.id):
            raise ValueError(f"unknown notification: {notification_id}")
        return notification

    def effective_runtime_config(self, *, session_id: str | None = None) -> EffectiveRuntimeConfig:
        if session_id is None:
            return self._effective_runtime_config_from_metadata(None)
        validate_session_id(session_id)
        response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        self._validate_session_workspace(response.session, session_id=session_id)
        return self._effective_runtime_config_from_metadata(response.session.metadata)

    def refresh_provider_models(self, provider_name: str) -> tuple[str, ...]:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        _ = self._model_provider_registry.resolve(provider_name)
        return self._model_provider_registry.refresh_available_models(provider_name)

    def provider_models(self, provider_name: str) -> tuple[str, ...]:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        return self._model_provider_registry.available_models(provider_name)

    def provider_model_catalog(self, provider_name: str) -> dict[str, object] | None:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        catalog = self._model_provider_registry.provider_catalog(provider_name)
        if catalog is None:
            return None
        return {
            "provider": catalog.provider,
            "models": list(catalog.models),
            "refreshed": catalog.refreshed,
            "source": catalog.source,
            "last_refresh_status": catalog.last_refresh_status,
            "last_error": catalog.last_error,
        }

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            return self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        _, response = self._resume_pending_approval_response(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        )
        return response

    def resume_stream(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            response = self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
            yield from self._replay_response(response)
            return
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        yield from self._resume_pending_approval_stream(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        )

    def _pending_approval_from_response(self, response: RuntimeResponse) -> PendingApproval:
        if not response.events:
            raise ValueError("waiting runtime response must include an approval event")
        approval_event = response.events[-1]
        if approval_event.event_type != "runtime.approval_requested":
            raise ValueError("waiting runtime response must end with approval request")
        payload = approval_event.payload
        raw_policy = cast(dict[str, object], payload.get("policy", {}))
        raw_policy_mode = raw_policy.get("mode", "ask")
        if raw_policy_mode not in ("allow", "deny", "ask"):
            raise ValueError(f"invalid approval policy mode: {raw_policy_mode}")
        return PendingApproval(
            request_id=str(payload["request_id"]),
            tool_name=str(payload["tool"]),
            arguments=cast(dict[str, object], payload.get("arguments", {})),
            target_summary=str(payload.get("target_summary", "")),
            reason=str(payload.get("reason", "")),
            policy_mode=raw_policy_mode,
        )

    def _resume_pending_approval_stream(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> Iterator[RuntimeStreamChunk]:
        stored_response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        pending = self._session_store.load_pending_approval(
            workspace=self._workspace, session_id=session_id
        )
        checkpoint = self._load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")
        yield from self._resume_pending_approval_impl(
            stored=stored_response,
            pending=pending,
            approval_decision=approval_decision,
            checkpoint=checkpoint,
        )

    def _resume_pending_approval_response(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        stored_response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        pending = self._session_store.load_pending_approval(
            workspace=self._workspace, session_id=session_id
        )
        checkpoint = self._load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")

        stored_events = stored_response.events
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self._resume_pending_approval_impl(
            stored=stored_response,
            pending=pending,
            approval_decision=approval_decision,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        if final_session.status == "waiting":
            final_session = self._reload_persisted_session(session_id=final_session.session.id)

        return stored_events, RuntimeResponse(
            session=final_session,
            events=stored_events + tuple(streamed_events),
            output=output,
        )

    def _resume_pending_approval_impl(
        self,
        *,
        stored: RuntimeResponse,
        pending: PendingApproval,
        approval_decision: PermissionResolution,
        checkpoint: dict[str, object] | None,
    ) -> Iterator[RuntimeStreamChunk]:
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=stored.session.metadata,
        )

        max_stored_sequence = stored.events[-1].sequence if stored.events else 0
        loop_events: list[EventEnvelope] = []
        output: str | None = None

        checkpoint_state = self._approval_resume_state_from_checkpoint(
            checkpoint=checkpoint,
            pending=pending,
            stored_metadata=stored.session.metadata,
        )
        if checkpoint_state is not None:
            prompt = checkpoint_state.prompt
            session = SessionState(
                session=stored.session.session,
                status="running",
                turn=stored.session.turn,
                metadata=checkpoint_state.session_metadata,
            )
            tool_results: list[ToolResult] = list(checkpoint_state.tool_results)
        else:
            prompt = self._prompt_from_events(stored.events)
            tool_results = []
            for event in stored.events:
                if event.event_type == "runtime.tool_completed":
                    error_value = event.payload.get("error")
                    raw_content = event.payload.get("content")
                    is_err = error_value is not None
                    tool_results.append(
                        ToolResult(
                            tool_name=str(event.payload.get("tool", "unknown")),
                            content=(
                                str(raw_content) if raw_content is not None and not is_err else None
                            ),
                            status="error" if is_err else "ok",
                            data=event.payload,
                            error=str(error_value) if is_err else None,
                        )
                    )

        session = self._session_with_current_acp_metadata(session)
        preserved_continuity_state = self._continuity_state_from_session_metadata(session.metadata)
        self._refresh_mcp_tools()
        effective_config = self._effective_runtime_config_from_metadata(session.metadata)
        tool_registry = self._tool_registry_for_effective_config(effective_config)

        resumed_applied_skills = self._graph_applied_skills(session.metadata)
        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=tool_registry.definitions(),
            applied_skills=resumed_applied_skills,
            skill_prompt_context=build_skill_prompt_context(
                tuple(runtime_context_from_payload(payload) for payload in resumed_applied_skills)
            ),
            context_window=self._prepare_single_agent_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                "provider_attempt": cast(int, session.metadata.get("provider_attempt", 0)),
            },
        )
        provider_attempt = cast(int, graph_request.metadata.get("provider_attempt", 0))
        graph = self._graph_for_session_metadata(session.metadata)
        if provider_attempt > 0:
            effective_config = self._effective_runtime_config_from_metadata(session.metadata)
            resume_target = self._provider_chain_for_session_metadata(session.metadata).target_at(
                provider_attempt
            )
            if resume_target is not None:
                graph = self._build_graph_for_engine(
                    effective_config.execution_engine,
                    resume_target,
                    effective_config.max_steps,
                )

        deferred_startup_acp_events: tuple[object, ...] = ()
        if self.current_acp_state().configuration.configured_enabled is True:
            try:
                deferred_startup_acp_events = self._acp_adapter.connect()
            except Exception as exc:
                startup_chunks, session, last_sequence = self._emit_current_acp_drain(
                    session=session,
                    start_sequence=max_stored_sequence + 1,
                )
                startup_failed_chunk = self._failed_chunk(
                    session=self._session_with_current_acp_metadata(session),
                    sequence=last_sequence + 1,
                    error=str(exc),
                    payload={"kind": "acp_startup_failed"},
                )
            else:
                session = self._session_with_current_acp_metadata(session)
                startup_chunks = ()
                startup_failed_chunk = None
        else:
            startup_chunks = ()
            startup_failed_chunk = None
        emitted_sequence = max_stored_sequence
        for chunk in startup_chunks:
            emitted_sequence += 1
            resequenced_event = self._resequence_event(
                cast(EventEnvelope, chunk.event), sequence=emitted_sequence
            )
            resequenced_chunk = RuntimeStreamChunk(
                kind="event", session=chunk.session, event=resequenced_event
            )
            loop_events.append(resequenced_event)
            yield resequenced_chunk
        if startup_failed_chunk is not None:
            emitted_sequence += 1
            resequenced_failed = self._resequence_event(
                cast(EventEnvelope, startup_failed_chunk.event),
                sequence=emitted_sequence,
            )
            failed_chunk = RuntimeStreamChunk(
                kind="event",
                session=startup_failed_chunk.session,
                event=resequenced_failed,
            )
            loop_events.append(resequenced_failed)
            response = RuntimeResponse(
                session=startup_failed_chunk.session,
                events=stored.events + tuple(loop_events),
                output=output,
            )
            request = RuntimeRequest(
                prompt=prompt,
                session_id=stored.session.session.id,
                parent_session_id=stored.session.session.parent_id,
            )
            self._persist_response(request=request, response=response)
            yield failed_chunk
            return

        sequence = max_stored_sequence
        try:
            for chunk in self._execute_graph_loop(
                graph=graph,
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                graph_request=graph_request,
                tool_results=tool_results,
                approval_resolution=(pending, approval_decision),
                permission_policy=self._permission_policy_for_session(session.metadata),
                preserved_continuity_state=preserved_continuity_state,
            ):
                if deferred_startup_acp_events and (
                    (
                        chunk.event is not None
                        and chunk.event.event_type
                        in {"runtime.approval_resolved", "runtime.failed"}
                    )
                    or chunk.kind == "output"
                ):
                    startup_chunks, updated_session, _ = self._emit_acp_events(
                        session=chunk.session,
                        start_sequence=emitted_sequence + 1,
                        acp_events=deferred_startup_acp_events,
                    )
                    deferred_startup_acp_events = ()
                    for startup_chunk in startup_chunks:
                        startup_event = cast(EventEnvelope, startup_chunk.event)
                        emitted_sequence = startup_event.sequence
                        loop_events.append(startup_event)
                        yield startup_chunk
                    if chunk.event is not None:
                        chunk = RuntimeStreamChunk(
                            kind="event",
                            session=updated_session,
                            event=chunk.event,
                        )
                    elif chunk.kind == "output":
                        chunk = RuntimeStreamChunk(
                            kind="output",
                            session=updated_session,
                            output=chunk.output,
                        )
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = self._resequence_event(
                        chunk.event, sequence=emitted_sequence
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )
                if chunk.kind == "output":
                    output = chunk.output
                    yield chunk
                session = chunk.session
        except Exception:
            if session.status == "failed":
                response = RuntimeResponse(
                    session=session,
                    events=stored.events + tuple(loop_events),
                    output=output,
                )
                request = RuntimeRequest(
                    prompt=prompt,
                    session_id=stored.session.session.id,
                    parent_session_id=stored.session.session.parent_id,
                )
                self._persist_response(request=request, response=response)
                return
            raise

        if deferred_startup_acp_events:
            startup_chunks, session, _ = self._emit_acp_events(
                session=session,
                start_sequence=emitted_sequence + 1,
                acp_events=deferred_startup_acp_events,
            )
            deferred_startup_acp_events = ()
            for startup_chunk in startup_chunks:
                startup_event = cast(EventEnvelope, startup_chunk.event)
                emitted_sequence = startup_event.sequence
                loop_events.append(startup_event)
                yield startup_chunk

        last_sequence = emitted_sequence
        if session.status == "waiting":
            session = self._disconnect_acp_for_session_state(session)
        else:
            final_chunks, session, _ = self._finalize_run_acp(
                session=session,
                sequence=last_sequence,
            )
            for chunk in final_chunks:
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = self._resequence_event(
                        chunk.event, sequence=emitted_sequence
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )

        response = RuntimeResponse(
            session=session,
            events=stored.events + tuple(loop_events),
            output=output,
        )

        request = RuntimeRequest(
            prompt=prompt,
            session_id=stored.session.session.id,
            parent_session_id=stored.session.session.parent_id,
        )
        self._persist_response(request=request, response=response)
        return

    def _approval_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingApproval,
        stored_metadata: dict[str, object],
    ) -> _ApprovalResumeCheckpointState | None:
        if checkpoint is None:
            return None
        if checkpoint.get("kind") != "approval_wait":
            return None
        if checkpoint.get("version") != 1:
            return None
        if checkpoint.get("pending_approval_request_id") != pending.request_id:
            return None
        prompt = checkpoint.get("prompt")
        session_metadata = checkpoint.get("session_metadata")
        raw_tool_results = checkpoint.get("tool_results")
        if not isinstance(prompt, str) or not isinstance(session_metadata, dict):
            return None
        if cast(dict[str, object], session_metadata) != stored_metadata:
            return None
        if not isinstance(raw_tool_results, list):
            return None
        checkpoint_tool_results = cast(list[object], raw_tool_results)
        try:
            tool_results = self._tool_results_from_checkpoint(checkpoint_tool_results)
        except (TypeError, ValueError):
            return None
        return _ApprovalResumeCheckpointState(
            prompt=prompt,
            session_metadata=cast(dict[str, object], session_metadata),
            tool_results=tool_results,
        )

    def _load_resume_checkpoint(self, *, session_id: str) -> dict[str, object] | None:
        load_checkpoint = getattr(self._session_store, "load_resume_checkpoint", None)
        if load_checkpoint is None:
            return None
        return cast(
            dict[str, object] | None,
            load_checkpoint(workspace=self._workspace, session_id=session_id),
        )

    @staticmethod
    def _tool_results_from_checkpoint(raw_tool_results: list[object]) -> tuple[ToolResult, ...]:
        parsed: list[ToolResult] = []
        for raw_tool_result in raw_tool_results:
            if not isinstance(raw_tool_result, dict):
                raise ValueError("persisted resume checkpoint tool_results must contain objects")
            payload = cast(dict[str, object], raw_tool_result)
            tool_name = payload.get("tool_name")
            status = payload.get("status")
            data = payload.get("data")
            content = payload.get("content")
            error = payload.get("error")
            if (
                not isinstance(tool_name, str)
                or status not in ("ok", "error")
                or not isinstance(data, dict)
            ):
                raise ValueError("persisted resume checkpoint tool_results are malformed")
            if content is not None and not isinstance(content, str):
                raise ValueError(
                    "persisted resume checkpoint tool result content must be a string or null"
                )
            if error is not None and not isinstance(error, str):
                raise ValueError(
                    "persisted resume checkpoint tool result error must be a string or null"
                )
            tool_status: ToolResultStatus = status
            parsed.append(
                ToolResult(
                    tool_name=tool_name,
                    content=content,
                    status=tool_status,
                    data=cast(dict[str, object], data),
                    error=error,
                )
            )
        return tuple(parsed)

    @staticmethod
    def _replay_response(response: RuntimeResponse) -> Iterator[RuntimeStreamChunk]:
        for event in response.events:
            yield RuntimeStreamChunk(
                kind="event",
                session=VoidCodeRuntime._replayed_chunk_session(
                    response_session=response.session,
                    event=event,
                ),
                event=event,
            )
        if response.output is not None:
            yield RuntimeStreamChunk(
                kind="output",
                session=VoidCodeRuntime._session_with_status(
                    response.session,
                    "completed"
                    if response.session.status == "completed"
                    else response.session.status,
                ),
                output=response.output,
            )

    @staticmethod
    def _session_with_status(session: SessionState, status: SessionStatus) -> SessionState:
        return SessionState(
            session=session.session,
            status=status,
            turn=session.turn,
            metadata=session.metadata,
        )

    @staticmethod
    def _replayed_chunk_session(
        *, response_session: SessionState, event: EventEnvelope
    ) -> SessionState:
        status: SessionStatus = "running"
        if event.event_type == "runtime.approval_requested":
            status = "waiting"
        elif event.event_type == "runtime.failed":
            status = "failed"
        elif event.event_type == "runtime.acp_disconnected" and response_session.status in {
            "failed",
            "completed",
        }:
            status = response_session.status
        elif response_session.status == "completed" and (
            event.event_type == "graph.response_ready"
            or (event.event_type == "graph.loop_step" and event.payload.get("phase") == "finalize")
        ):
            status = "completed"
        return VoidCodeRuntime._session_with_status(response_session, status)

    def _validated_request(self, request: RuntimeRequest) -> RuntimeRequest:
        session_id = request.session_id
        if session_id is not None:
            session_id = validate_session_id(session_id)

        parent_session_id = request.parent_session_id
        if parent_session_id is not None:
            parent_session_id = validate_session_reference_id(
                parent_session_id,
                field_name="parent_session_id",
            )
        if session_id is not None and parent_session_id == session_id:
            raise RuntimeRequestError("parent_session_id must not match session_id")

        existing_session = (
            self._load_existing_session_if_present(session_id=session_id)
            if session_id is not None
            else None
        )
        if parent_session_id is not None:
            parent_session = self._load_existing_session_if_present(session_id=parent_session_id)
            if parent_session is None and not self._is_active_session_id(parent_session_id):
                raise RuntimeRequestError(f"parent session does not exist: {parent_session_id}")

        resolved_parent_session_id = parent_session_id
        if existing_session is not None:
            existing_parent_session_id = existing_session.session.session.parent_id
            if parent_session_id is None:
                resolved_parent_session_id = existing_parent_session_id
            elif existing_parent_session_id != parent_session_id:
                existing_parent_label = (
                    existing_parent_session_id
                    if existing_parent_session_id is not None
                    else "<top-level>"
                )
                raise RuntimeRequestError(
                    f"session {session_id} already belongs to {existing_parent_label} "
                    f"and cannot be rebound to parent session {parent_session_id}"
                )

        return RuntimeRequest(
            prompt=request.prompt,
            session_id=session_id,
            parent_session_id=resolved_parent_session_id,
            metadata=request.metadata,
            allocate_session_id=request.allocate_session_id,
        )

    @staticmethod
    def _resolve_session_id(request: RuntimeRequest) -> str:
        if request.session_id is not None:
            return request.session_id
        if request.allocate_session_id or request.parent_session_id is not None:
            return f"session-{uuid4().hex}"
        return "local-cli-session"

    @staticmethod
    def _prompt_from_events(events: tuple[EventEnvelope, ...]) -> str:
        if not events:
            return ""
        prompt = events[0].payload.get("prompt")
        if isinstance(prompt, str):
            return prompt
        return ""

    def _prepare_single_agent_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
    ) -> RuntimeContextWindow:
        return prepare_single_agent_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy or self._default_context_window_policy,
        )

    @staticmethod
    def _continuity_state_from_session_metadata(
        session_metadata: dict[str, object],
    ) -> RuntimeContinuityState | None:
        runtime_state = session_metadata.get("runtime_state")
        if not isinstance(runtime_state, dict):
            return None
        runtime_state_payload = cast(dict[str, object], runtime_state)
        continuity = runtime_state_payload.get("continuity")
        if not isinstance(continuity, dict):
            return None
        continuity_payload = cast(dict[str, object], continuity)
        summary_text = continuity_payload.get("summary_text")
        dropped = continuity_payload.get("dropped_tool_result_count")
        retained = continuity_payload.get("retained_tool_result_count")
        source = continuity_payload.get("source")
        if summary_text is not None and not isinstance(summary_text, str):
            return None
        if not isinstance(dropped, int) or isinstance(dropped, bool):
            return None
        if not isinstance(retained, int) or isinstance(retained, bool):
            return None
        if not isinstance(source, str):
            return None
        return RuntimeContinuityState(
            summary_text=summary_text,
            dropped_tool_result_count=dropped,
            retained_tool_result_count=retained,
            source=source,
        )

    @staticmethod
    def _session_with_context_window_metadata(
        session: SessionState, context_window: RuntimeContextWindow
    ) -> SessionState:
        raw_runtime_state = session.metadata.get("runtime_state")
        runtime_state = (
            dict(cast(dict[str, object], raw_runtime_state))
            if isinstance(raw_runtime_state, dict)
            else {}
        )
        continuity_payload = (
            context_window.continuity_state.metadata_payload()
            if context_window.continuity_state is not None
            else None
        )
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "context_window": context_window.metadata_payload(),
                "runtime_state": {
                    **runtime_state,
                    **(
                        {"continuity": continuity_payload} if continuity_payload is not None else {}
                    ),
                },
            },
        )

    @staticmethod
    def _renumber_events(
        events: tuple[GraphEvent, ...], *, session_id: str, start_sequence: int
    ) -> tuple[EventEnvelope, ...]:
        return tuple(
            EventEnvelope(
                session_id=session_id,
                sequence=start_sequence + index,
                event_type=event.event_type,
                source=event.source,
                payload=event.payload,
            )
            for index, event in enumerate(events)
        )

    def _loaded_skill_names(self) -> list[str]:
        return sorted(self._skill_registry.skills)

    def _applied_skill_contexts(
        self, metadata: dict[str, object] | None = None
    ) -> tuple[SkillRuntimeContext, ...]:
        selected_skill_names: tuple[str, ...] | None = None
        if metadata is not None and "skills" in metadata:
            raw_skills = metadata["skills"]
            if not isinstance(raw_skills, list):
                raise ValueError("request metadata 'skills' must be a list of skill names")
            parsed_names: list[str] = []
            for index, raw_name in enumerate(cast(list[object], raw_skills)):
                if not isinstance(raw_name, str) or not raw_name:
                    raise ValueError(
                        f"request metadata 'skills[{index}]' must be a non-empty string"
                    )
                parsed_names.append(raw_name)
            selected_skill_names = tuple(parsed_names)
        return build_runtime_contexts(self._skill_registry, skill_names=selected_skill_names)

    @staticmethod
    def _fresh_request_metadata(metadata: dict[str, object]) -> dict[str, object]:
        sanitized = dict(metadata)
        sanitized.pop("applied_skills", None)
        sanitized.pop("applied_skill_payloads", None)
        return sanitized

    @staticmethod
    def _frozen_applied_skill_payloads(
        contexts: Iterable[SkillRuntimeContext],
    ) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "name": context.name,
                "description": context.description,
                "content": context.content,
                "prompt_context": context.prompt_context,
                "execution_notes": context.execution_notes,
                "source_path": context.source_path,
            }
            for context in contexts
        )

    @staticmethod
    def _persisted_applied_skill_payloads(
        metadata: dict[str, object],
    ) -> tuple[dict[str, str], ...] | None:
        if "applied_skill_payloads" not in metadata:
            return None
        raw_payloads = metadata["applied_skill_payloads"]
        if not isinstance(raw_payloads, list):
            raise ValueError("persisted applied skill payloads must be a list")
        persisted_payloads = cast(list[object], raw_payloads)

        payloads: list[dict[str, str]] = []
        for raw_payload in persisted_payloads:
            if not isinstance(raw_payload, dict):
                raise ValueError("persisted applied skill payloads must contain objects")
            payload = cast(dict[str, object], raw_payload)
            name = payload.get("name")
            description = payload.get("description")
            content = payload.get("content")
            prompt_context = payload.get("prompt_context")
            execution_notes = payload.get("execution_notes")
            source_path = payload.get("source_path")
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not isinstance(content, str)
            ):
                raise ValueError("persisted applied skill payloads must include string fields")
            normalized_payload = {"name": name, "description": description, "content": content}
            if prompt_context is not None:
                if not isinstance(prompt_context, str):
                    raise ValueError(
                        "persisted applied skill payload prompt_context must be a string"
                    )
                normalized_payload["prompt_context"] = prompt_context
            if execution_notes is not None:
                if not isinstance(execution_notes, str):
                    raise ValueError(
                        "persisted applied skill payload execution_notes must be a string"
                    )
                normalized_payload["execution_notes"] = execution_notes
            if source_path is not None:
                if not isinstance(source_path, str):
                    raise ValueError("persisted applied skill payload source_path must be a string")
                normalized_payload["source_path"] = source_path
            payloads.append(normalized_payload)
        return tuple(payloads)

    def _available_runtime_contexts(
        self, skill_names: Iterable[str]
    ) -> tuple[SkillRuntimeContext, ...]:
        contexts: list[SkillRuntimeContext] = []
        for skill_name in skill_names:
            skill = self._skill_registry.skills.get(skill_name)
            if skill is None:
                continue
            contexts.append(build_runtime_context(skill))
        return tuple(contexts)

    def _graph_applied_skills(
        self, metadata: dict[str, object] | None = None
    ) -> tuple[dict[str, str], ...]:
        if metadata is not None:
            persisted_payloads = self._persisted_applied_skill_payloads(metadata)
            if persisted_payloads is not None:
                return persisted_payloads
            persisted = metadata.get("applied_skills")
            if isinstance(persisted, list):
                persisted_values = cast(list[object], persisted)
                persisted_names = [item for item in persisted_values if isinstance(item, str)]
                if not persisted_names:
                    return ()
                if persisted_names:
                    return self._frozen_applied_skill_payloads(
                        self._available_runtime_contexts(persisted_names)
                    )
        return self._frozen_applied_skill_payloads(self._applied_skill_contexts(metadata))

    def _runtime_config_metadata(
        self, config: EffectiveRuntimeConfig | None = None
    ) -> dict[str, object]:
        effective_config = config or self._effective_runtime_config_from_metadata(None)
        runtime_config_metadata: dict[str, object] = {
            "approval_mode": effective_config.approval_mode,
            "execution_engine": effective_config.execution_engine,
            "max_steps": effective_config.max_steps,
            "provider_fallback": serialize_provider_fallback_config(
                effective_config.provider_fallback
            ),
            "resolved_provider": resolved_provider_snapshot(effective_config.resolved_provider),
            "plan": serialize_runtime_plan_config(effective_config.plan),
        }
        if effective_config.model is not None:
            runtime_config_metadata["model"] = effective_config.model
        serialized_agent = serialize_runtime_agent_config(effective_config.agent)
        if serialized_agent is not None:
            runtime_config_metadata["agent"] = serialized_agent
        lsp_state = self._lsp_manager.current_state()
        runtime_config_metadata["lsp"] = {
            "mode": lsp_state.mode,
            "configured_enabled": lsp_state.configuration.configured_enabled,
            "servers": list(lsp_state.configuration.servers),
        }
        mcp_state = self._mcp_manager.current_state()
        runtime_config_metadata["mcp"] = {
            "mode": mcp_state.mode,
            "configured_enabled": mcp_state.configuration.configured_enabled,
            "servers": list(mcp_state.configuration.servers),
        }
        return runtime_config_metadata

    def _config_with_request_agent_override(
        self,
        resolved: EffectiveRuntimeConfig,
        raw_agent: object,
    ) -> EffectiveRuntimeConfig:
        agent = parse_runtime_agent_payload(raw_agent, source="request metadata 'agent'")
        if agent is None:
            raise ValueError("request metadata 'agent' must be an object when provided")
        assert agent is not None
        self._validate_runtime_agent_for_execution(agent, source="request metadata 'agent'")
        model = agent.model if agent.model is not None else resolved.model
        execution_engine = (
            agent.execution_engine
            if agent.execution_engine is not None
            else resolved.execution_engine
        )
        provider_fallback = (
            agent.provider_fallback
            if agent.provider_fallback is not None
            else resolved.provider_fallback
        )
        merged_agent = RuntimeAgentConfig(
            preset=agent.preset,
            prompt_profile=(
                agent.prompt_profile
                if agent.prompt_profile is not None
                else resolved.agent.prompt_profile
                if resolved.agent is not None
                else None
            ),
            model=model,
            execution_engine=execution_engine,
            tools=(
                agent.tools
                if agent.tools is not None
                else resolved.agent.tools
                if resolved.agent is not None
                else None
            ),
            skills=(
                agent.skills
                if agent.skills is not None
                else resolved.agent.skills
                if resolved.agent is not None
                else None
            ),
            provider_fallback=provider_fallback,
        )
        resolved_provider = resolve_provider_config(
            model,
            provider_fallback,
            registry=self._model_provider_registry,
        )
        return EffectiveRuntimeConfig(
            approval_mode=resolved.approval_mode,
            model=model,
            execution_engine=execution_engine,
            max_steps=resolved.max_steps,
            provider_fallback=provider_fallback,
            plan=resolved.plan,
            resolved_provider=resolved_provider,
            agent=merged_agent,
        )

    @staticmethod
    def _validate_runtime_agent_for_execution(
        agent: RuntimeAgentConfig,
        *,
        source: str,
    ) -> None:
        if agent.preset in _EXECUTABLE_AGENT_PRESETS:
            return
        valid = ", ".join(sorted(_EXECUTABLE_AGENT_PRESETS))
        raise ValueError(
            f"{source}: agent preset '{agent.preset}' is declaration-only in the current "
            f"runtime; executable agent presets are: {valid}"
        )

    def _runtime_state_metadata(self) -> dict[str, object]:
        acp_state = self._acp_adapter.current_state()
        return {
            "acp": {
                "mode": acp_state.mode,
                "configured_enabled": acp_state.configuration.configured_enabled,
                "status": acp_state.status,
                "available": acp_state.available,
                "last_error": acp_state.last_error,
            }
        }

    @staticmethod
    def _envelopes_for_lsp_events(
        *,
        session_id: str,
        start_sequence: int,
        lsp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        known_event_types = {
            RUNTIME_LSP_SERVER_STARTED,
            RUNTIME_LSP_SERVER_REUSED,
            RUNTIME_LSP_SERVER_STARTUP_REJECTED,
            RUNTIME_LSP_SERVER_STOPPED,
            RUNTIME_LSP_SERVER_FAILED,
        }
        envelopes: list[EventEnvelope] = []
        sequence = start_sequence
        for raw_event in lsp_events:
            if isinstance(raw_event, dict):
                raw_event_dict = cast(dict[str, object], raw_event)
                event_type = raw_event_dict.get("event_type")
                payload = raw_event_dict.get("payload")
            else:
                event_type = getattr(raw_event, "event_type", None)
                payload = getattr(raw_event, "payload", None)
            if event_type not in known_event_types or not isinstance(payload, dict):
                continue
            envelopes.append(
                EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=cast(str, event_type),
                    source="runtime",
                    payload=cast(dict[str, object], payload),
                )
            )
            sequence += 1
        return tuple(envelopes)

    @staticmethod
    def _envelopes_for_acp_events(
        *,
        session_id: str,
        start_sequence: int,
        acp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        known_event_types = {
            RUNTIME_ACP_CONNECTED,
            RUNTIME_ACP_DISCONNECTED,
            RUNTIME_ACP_FAILED,
        }
        envelopes: list[EventEnvelope] = []
        sequence = start_sequence
        for raw_event in acp_events:
            if isinstance(raw_event, dict):
                raw_event_dict = cast(dict[str, object], raw_event)
                event_type = raw_event_dict.get("event_type")
                payload = raw_event_dict.get("payload")
            else:
                event_type = getattr(raw_event, "event_type", None)
                payload = getattr(raw_event, "payload", None)
            if event_type not in known_event_types or not isinstance(payload, dict):
                continue
            envelopes.append(
                EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=cast(str, event_type),
                    source="runtime",
                    payload=cast(dict[str, object], payload),
                )
            )
            sequence += 1
        return tuple(envelopes)

    @staticmethod
    def _envelopes_for_mcp_events(
        *,
        session_id: str,
        start_sequence: int,
        mcp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        known_event_types = {
            RUNTIME_MCP_SERVER_STARTED,
            RUNTIME_MCP_SERVER_STOPPED,
        }
        envelopes: list[EventEnvelope] = []
        sequence = start_sequence
        for raw_event in mcp_events:
            if isinstance(raw_event, dict):
                raw_event_dict = cast(dict[str, object], raw_event)
                event_type = raw_event_dict.get("event_type")
                payload = raw_event_dict.get("payload")
            else:
                event_type = getattr(raw_event, "event_type", None)
                payload = getattr(raw_event, "payload", None)
            if event_type not in known_event_types or not isinstance(payload, dict):
                continue
            envelopes.append(
                EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=cast(str, event_type),
                    source="runtime",
                    payload=cast(dict[str, object], payload),
                )
            )
            sequence += 1
        return tuple(envelopes)

    def _permission_policy_for_session(
        self, metadata: dict[str, object] | None
    ) -> PermissionPolicy:
        approval_mode: PermissionDecision = self._permission_policy.mode
        if metadata is not None:
            persisted_runtime_config = metadata.get("runtime_config")
            if isinstance(persisted_runtime_config, dict):
                runtime_config = cast(dict[str, object], persisted_runtime_config)
                persisted_approval_mode = runtime_config.get("approval_mode")
                if persisted_approval_mode in ("allow", "deny", "ask"):
                    approval_mode = persisted_approval_mode
        return PermissionPolicy(mode=approval_mode)

    def _reconcile_background_tasks_if_needed(self) -> None:
        if self._background_tasks_reconciled:
            return
        fail_incomplete = getattr(self._session_store, "fail_incomplete_background_tasks", None)
        if callable(fail_incomplete):
            fail_incomplete(
                workspace=self._workspace,
                message="background task interrupted before completion",
            )
        self._background_tasks_reconciled = True

    def _run_background_task_worker(self, task_id: str) -> None:
        try:
            task = self.load_background_task(task_id)
            if task.status == "cancelled":
                return
            request = RuntimeRequest(
                prompt=task.request.prompt,
                session_id=task.request.session_id,
                parent_session_id=task.request.parent_session_id,
                metadata=task.request.metadata,
                allocate_session_id=task.request.allocate_session_id,
            )
            session_id = self._resolve_session_id(request)
            running_task = self._session_store.mark_background_task_running(
                workspace=self._workspace,
                task_id=task_id,
                session_id=session_id,
            )
            if running_task.status != "running":
                return
            dispatch_task = self.load_background_task(task_id)
            if dispatch_task.status != "running":
                return
            if dispatch_task.cancel_requested_at is not None:
                self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_id,
                    status="cancelled",
                    error="cancelled before dispatch",
                )
                return
            response = self.run(
                RuntimeRequest(
                    prompt=dispatch_task.request.prompt,
                    session_id=session_id,
                    parent_session_id=dispatch_task.request.parent_session_id,
                    metadata={
                        **dispatch_task.request.metadata,
                        "background_task_id": task_id,
                        "background_run": True,
                    },
                    allocate_session_id=False,
                )
            )
            terminal_status: BackgroundTaskStatus = (
                "completed" if response.session.status == "completed" else "failed"
            )
            error: str | None = None
            if terminal_status == "failed":
                for event in reversed(response.events):
                    if event.event_type == RUNTIME_FAILED:
                        event_error = event.payload.get("error")
                        error = str(event_error) if event_error is not None else None
                        break
            self._session_store.mark_background_task_terminal(
                workspace=self._workspace,
                task_id=task_id,
                status=terminal_status,
                error=error,
            )
        except Exception as exc:
            logger.exception("background task failed: %s", task_id)
            self._session_store.mark_background_task_terminal(
                workspace=self._workspace,
                task_id=task_id,
                status="failed",
                error=str(exc),
            )
        finally:
            self._background_task_threads.pop(task_id, None)

    def _effective_runtime_config_from_metadata(
        self, metadata: dict[str, object] | None
    ) -> EffectiveRuntimeConfig:
        approval_mode: PermissionDecision = self._config.approval_mode
        model = self._config.model
        execution_engine = self._config.execution_engine
        max_steps = self._config.max_steps
        provider_fallback = self._config.provider_fallback
        plan = self._config.plan
        agent = self._config.agent
        if agent is not None:
            agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(agent),
                source="runtime config agent",
            )
            assert agent is not None
            self._validate_runtime_agent_for_execution(
                agent,
                source="runtime config agent",
            )
        execution_engine_override = (
            agent.execution_engine
            if agent is not None and agent.execution_engine is not None
            else None
        )
        model_override = agent.model if agent is not None and agent.model is not None else None
        provider_fallback_override = (
            agent.provider_fallback
            if agent is not None and agent.provider_fallback is not None
            else None
        )
        if execution_engine_override is not None:
            execution_engine = execution_engine_override
        if model_override is not None:
            model = model_override
        if provider_fallback_override is not None:
            provider_fallback = provider_fallback_override
        resolved_provider = resolve_provider_config(
            model,
            provider_fallback,
            registry=self._model_provider_registry,
        )
        if metadata is None:
            return EffectiveRuntimeConfig(
                approval_mode=approval_mode,
                model=model,
                execution_engine=execution_engine,
                max_steps=max_steps,
                provider_fallback=provider_fallback,
                plan=plan,
                resolved_provider=resolved_provider,
                agent=agent,
            )

        persisted_runtime_config = metadata.get("runtime_config")
        if not isinstance(persisted_runtime_config, dict):
            return EffectiveRuntimeConfig(
                approval_mode=approval_mode,
                model=model,
                execution_engine=execution_engine,
                max_steps=max_steps,
                provider_fallback=provider_fallback,
                plan=plan,
                resolved_provider=resolved_provider,
                agent=agent,
            )

        runtime_config = cast(dict[str, object], persisted_runtime_config)
        persisted_approval_mode = runtime_config.get("approval_mode")
        if persisted_approval_mode in ("allow", "deny", "ask"):
            approval_mode = persisted_approval_mode
        persisted_model = runtime_config.get("model")
        if persisted_model is None or isinstance(persisted_model, str):
            model = persisted_model
        persisted_max_steps = runtime_config.get("max_steps")
        if isinstance(persisted_max_steps, int) and not isinstance(persisted_max_steps, bool):
            if persisted_max_steps < 1:
                raise ValueError("persisted runtime_config max_steps must be at least 1")
            max_steps = persisted_max_steps
        provider_fallback = None
        if "provider_fallback" in runtime_config:
            try:
                provider_fallback = parse_provider_fallback_payload(
                    runtime_config.get("provider_fallback"),
                    source="persisted runtime_config.provider_fallback",
                )
            except ValueError as exc:
                raise ValueError(
                    format_invalid_provider_config_error(
                        "persisted runtime_config.provider_fallback",
                        str(exc),
                    )
                ) from exc
        if "plan" in runtime_config:
            try:
                plan = parse_runtime_plan_payload(
                    runtime_config.get("plan"),
                    source="persisted runtime_config.plan",
                )
            except ValueError as exc:
                raise ValueError(
                    format_invalid_provider_config_error(
                        "persisted runtime_config.plan",
                        str(exc),
                    )
                ) from exc
        if "agent" in runtime_config:
            agent = parse_runtime_agent_payload(
                runtime_config.get("agent"),
                source="persisted runtime_config.agent",
            )
            if agent is not None:
                self._validate_runtime_agent_for_execution(
                    agent,
                    source="persisted runtime_config.agent",
                )
        else:
            agent = None
        persisted_execution_engine = runtime_config.get("execution_engine")
        if persisted_execution_engine in ("deterministic", "single_agent"):
            execution_engine = persisted_execution_engine
        raw_resolved_provider = runtime_config.get("resolved_provider")
        if raw_resolved_provider is not None:
            resolved_provider = parse_resolved_provider_snapshot(
                raw_resolved_provider,
                source="persisted runtime_config.resolved_provider",
                registry=self._model_provider_registry,
            )
            model = resolved_provider.model
            provider_fallback = resolved_provider.provider_fallback
        else:
            resolved_provider = resolve_provider_config(
                model,
                provider_fallback,
                registry=self._model_provider_registry,
            )
        return EffectiveRuntimeConfig(
            approval_mode=approval_mode,
            model=model,
            execution_engine=execution_engine,
            max_steps=max_steps,
            provider_fallback=provider_fallback,
            plan=plan,
            resolved_provider=resolved_provider,
            agent=agent,
        )

    def _provider_chain_for_session_metadata(
        self, metadata: dict[str, object] | None
    ) -> ResolvedProviderChain:
        effective_config = self._effective_runtime_config_from_metadata(metadata)
        return effective_config.resolved_provider.target_chain

    def _graph_for_session_metadata(self, metadata: dict[str, object] | None) -> RuntimeGraph:
        if self._graph_override is not None:
            return self._graph_override

        effective_config = self._effective_runtime_config_from_metadata(metadata)

        # Reuse self._graph if the session's config matches the runtime's config
        if (
            effective_config.execution_engine == self._initial_effective_config.execution_engine
            and effective_config.model == self._initial_effective_config.model
            and effective_config.max_steps == self._initial_effective_config.max_steps
            and effective_config.provider_fallback
            == self._initial_effective_config.provider_fallback
            and effective_config.agent == self._initial_effective_config.agent
        ):
            return self._graph

        # Otherwise use cached graph or build new one
        return self._build_graph_for_engine_from_config(effective_config)

    def _validate_session_workspace(self, session: SessionState, *, session_id: str) -> None:
        session_workspace = session.metadata.get("workspace")
        if session_workspace is None:
            return
        if session_workspace != str(self._workspace):
            raise ValueError(f"session {session_id} does not belong to workspace {self._workspace}")

    def _load_existing_session_if_present(self, *, session_id: str) -> RuntimeResponse | None:
        if not self._session_store.has_session(workspace=self._workspace, session_id=session_id):
            return None
        response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(response.session, session_id=session_id)
        return response

    def _is_active_session_id(self, session_id: str) -> bool:
        return _ACTIVE_SESSION_REGISTRY.contains(workspace=self._workspace, session_id=session_id)

    def _register_active_session_id(self, session_id: str) -> None:
        _ACTIVE_SESSION_REGISTRY.register(workspace=self._workspace, session_id=session_id)

    def _unregister_active_session_id(self, session_id: str) -> None:
        _ACTIVE_SESSION_REGISTRY.unregister(workspace=self._workspace, session_id=session_id)

    def _session_belongs_to_workspace(self, session_id: str) -> bool:
        try:
            response = self._load_existing_session_if_present(session_id=session_id)
        except ValueError:
            return False
        if response is None:
            return False
        return True


@dataclass(frozen=True, slots=True)
class EffectiveRuntimeConfig:
    approval_mode: PermissionDecision
    model: str | None
    execution_engine: ExecutionEngineName
    max_steps: int
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    plan: RuntimePlanConfig | None = None
    resolved_provider: ResolvedProviderConfig = field(default_factory=ResolvedProviderConfig)
    agent: RuntimeAgentConfig | None = None


@dataclass(frozen=True, slots=True)
class _ApprovalResumeCheckpointState:
    prompt: str
    session_metadata: dict[str, object]
    tool_results: tuple[ToolResult, ...]


@dataclass(frozen=True, slots=True)
class _PermissionOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    pending_approval: PendingApproval | None = None
    denied: bool = False


@dataclass(frozen=True, slots=True)
class _HookOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    failed_error: str | None = None
