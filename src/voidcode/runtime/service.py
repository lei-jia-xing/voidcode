from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast, final, runtime_checkable
from uuid import uuid4

from ..graph.contracts import GraphEvent, GraphRunRequest
from ..graph.read_only_slice import DeterministicReadOnlyGraph
from ..graph.single_agent_slice import ProviderSingleAgentGraph
from ..tools.contracts import Tool, ToolCall, ToolDefinition, ToolResult
from .acp import AcpAdapter, AcpRequestEnvelope, AcpResponseEnvelope, build_acp_adapter
from .config import (
    ExecutionEngineName,
    RuntimeConfig,
    RuntimeProviderFallbackConfig,
    load_runtime_config,
)
from .context_window import ContextWindowPolicy, RuntimeContextWindow, prepare_single_agent_context
from .contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk, validate_session_id
from .events import (
    RUNTIME_ACP_CONNECTED,
    RUNTIME_ACP_DISCONNECTED,
    RUNTIME_ACP_FAILED,
    RUNTIME_LSP_SERVER_FAILED,
    RUNTIME_LSP_SERVER_STARTED,
    RUNTIME_LSP_SERVER_STOPPED,
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_SKILLS_LOADED,
    RUNTIME_TOOL_HOOK_POST,
    RUNTIME_TOOL_HOOK_PRE,
    EventEnvelope,
)
from .lsp import LspManager, LspManagerState, LspRequest, LspRequestResult, build_lsp_manager
from .model_provider import (
    ModelProviderRegistry,
    ResolvedProviderChain,
    ResolvedProviderModel,
    resolve_provider_chain,
    resolve_provider_model,
)
from .permission import (
    PendingApproval,
    PermissionDecision,
    PermissionPolicy,
    PermissionResolution,
    resolve_permission,
)
from .provider_errors import SingleAgentContextLimitError, classify_provider_error
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .single_agent_provider import ProviderExecutionError
from .skills import SkillRegistry, SkillRuntimeContext
from .storage import SessionStore, SqliteSessionStore
from .tool_provider import BuiltinToolProvider


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
    def with_defaults(cls, *, lsp_tool: Tool | None = None) -> ToolRegistry:
        return cls.from_tools(BuiltinToolProvider(lsp_tool=lsp_tool).provide_tools())

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self.tools.values())

    def resolve(self, tool_name: str) -> Tool:
        try:
            return self.tools[tool_name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_name}") from exc


@final
class VoidCodeRuntime:
    """Headless runtime entrypoint for one local deterministic request."""

    _workspace: Path
    _tool_registry: ToolRegistry
    _graph: RuntimeGraph
    _graph_override: RuntimeGraph | None
    _config: RuntimeConfig
    _permission_policy: PermissionPolicy
    _session_store: SessionStore
    _model_provider_registry: ModelProviderRegistry
    _provider_model: ResolvedProviderModel
    _provider_chain: ResolvedProviderChain
    _skill_registry: SkillRegistry
    _lsp_manager: LspManager
    _acp_adapter: AcpAdapter
    _graph_cache: dict[tuple[ExecutionEngineName, str], RuntimeGraph]
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
        acp_adapter: AcpAdapter | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._config = config or load_runtime_config(self._workspace)
        self._model_provider_registry = (
            model_provider_registry or ModelProviderRegistry.with_defaults()
        )
        self._provider_model = resolve_provider_model(
            self._config.model,
            registry=self._model_provider_registry,
        )
        self._provider_chain = resolve_provider_chain(
            self._config.provider_fallback,
            registry=self._model_provider_registry,
        )
        self._lsp_manager = lsp_manager or build_lsp_manager(self._config.lsp)
        self._tool_registry = tool_registry or ToolRegistry.with_defaults(
            lsp_tool=self._build_lsp_tool()
        )
        self._graph_override = graph
        self._graph_cache = {}
        self._graph = graph or self._build_graph_for_engine_from_config(
            EffectiveRuntimeConfig(
                approval_mode=self._config.approval_mode,
                model=self._config.model,
                execution_engine=self._config.execution_engine,
                max_steps=self._config.max_steps,
                provider_fallback=self._config.provider_fallback,
            )
        )
        self._permission_policy = permission_policy or PermissionPolicy(
            mode=self._config.approval_mode
        )
        self._session_store = session_store or SqliteSessionStore()
        self._skill_registry = skill_registry or self._build_skill_registry()
        self._acp_adapter = acp_adapter or build_acp_adapter(self._config.acp)

    def __enter__(self) -> VoidCodeRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type, exc, tb
        _ = self.disconnect_acp()
        _ = self.shutdown_lsp()

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
        raise ValueError(f"unknown execution engine: {engine_name}")

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
        if config.provider_fallback is not None:
            provider_chain = resolve_provider_chain(
                config.provider_fallback,
                registry=self._model_provider_registry,
            )
            provider_model = provider_chain.preferred
            self._provider_chain = provider_chain
        else:
            provider_model = resolve_provider_model(
                config.model,
                registry=self._model_provider_registry,
            )
            self._provider_chain = ResolvedProviderChain(
                preferred=provider_model,
                all_targets=((provider_model,) if provider_model.provider is not None else ()),
            )
        self._provider_model = provider_model
        graph = self._build_graph_for_engine(
            config.execution_engine,
            provider_model,
            config.max_steps,
        )
        self._graph_cache[cache_key] = graph
        return graph

    def _runtime_config_for_request(self, request: RuntimeRequest) -> EffectiveRuntimeConfig:
        resolved = self._effective_runtime_config_from_metadata(None)
        request_max_steps = request.metadata.get("max_steps")
        if isinstance(request_max_steps, int) and not isinstance(request_max_steps, bool):
            if request_max_steps < 1:
                raise ValueError("request metadata 'max_steps' must be at least 1")
            return EffectiveRuntimeConfig(
                approval_mode=resolved.approval_mode,
                model=resolved.model,
                execution_engine=resolved.execution_engine,
                max_steps=request_max_steps,
                provider_fallback=resolved.provider_fallback,
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

    def current_lsp_state(self) -> LspManagerState:
        return self._lsp_manager.current_state()

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
        events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        try:
            for chunk in self._stream_chunks(request):
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
                response = RuntimeResponse(
                    session=final_session, events=tuple(events), output=output
                )
                self._persist_response(request=request, response=response)
            raise

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
        self._persist_response(request=request, response=response)

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

        return RuntimeResponse(session=final_session, events=tuple(events), output=output)

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        return self._run_with_persistence(request)

    def _stream_chunks(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        session_id = self._resolve_session_id(request)
        effective_config = self._runtime_config_for_request(request)
        session = SessionState(
            session=SessionRef(id=session_id),
            status="running",
            turn=1,
            metadata={
                **request.metadata,
                "workspace": str(self._workspace),
                "runtime_config": self._runtime_config_metadata(effective_config),
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

        applied_skill_contexts = self._applied_skill_contexts(session.metadata)
        frozen_applied_skills = self._frozen_applied_skill_payloads(applied_skill_contexts)
        if self._config.skills is not None and self._config.skills.enabled is True:
            session = SessionState(
                session=session.session,
                status=session.status,
                turn=session.turn,
                metadata={
                    **session.metadata,
                    "applied_skills": [skill.name for skill in applied_skill_contexts],
                    "applied_skill_payloads": [dict(skill) for skill in frozen_applied_skills],
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
                    },
                ),
            )

        graph_request = GraphRunRequest(
            session=session,
            prompt=request.prompt,
            available_tools=self._tool_registry.definitions(),
            applied_skills=self._graph_applied_skills(session.metadata),
            context_window=self._prepare_single_agent_context_window(
                prompt=request.prompt,
                tool_results=(),
                session_metadata=session.metadata,
            ),
            metadata={**request.metadata, "provider_attempt": 0},
        )
        tool_results: list[ToolResult] = []
        graph = self._graph_for_session_metadata(session.metadata)

        yield from self._execute_graph_loop(
            graph=graph,
            session=session,
            sequence=sequence,
            graph_request=graph_request,
            tool_results=tool_results,
            permission_policy=self._permission_policy,
        )

    def _execute_graph_loop(
        self,
        *,
        graph: RuntimeGraph,
        session: SessionState,
        sequence: int,
        graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        active_permission_policy = permission_policy or self._permission_policy
        provider_attempt = cast(int, graph_request.metadata.get("provider_attempt", 0))
        while True:
            context_window = self._prepare_single_agent_context_window(
                prompt=graph_request.prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            )
            session = self._session_with_context_window_metadata(session, context_window)
            graph_request = GraphRunRequest(
                session=session,
                prompt=graph_request.prompt,
                available_tools=graph_request.available_tools,
                applied_skills=graph_request.applied_skills,
                context_window=context_window,
                metadata=graph_request.metadata,
            )
            if context_window.compacted:
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
                        graph = self._build_graph_for_engine(
                            self._effective_runtime_config_from_metadata(
                                session.metadata
                            ).execution_engine,
                            next_target,
                            self._effective_runtime_config_from_metadata(
                                session.metadata
                            ).max_steps,
                        )
                        graph_request = GraphRunRequest(
                            session=session,
                            prompt=graph_request.prompt,
                            available_tools=graph_request.available_tools,
                            applied_skills=graph_request.applied_skills,
                            metadata={
                                **graph_request.metadata,
                                "provider_attempt": provider_attempt,
                            },
                        )
                        continue
                if isinstance(exc, ProviderExecutionError):
                    yield self._failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=str(exc),
                        payload={
                            "provider_error_kind": exc.kind,
                            "provider": exc.provider_name,
                            "model": exc.model_name,
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
                tool = self._tool_registry.resolve(plan_tool_call.tool_name)
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
                for lsp_event in self._envelopes_for_lsp_events(
                    session_id=session.session.id,
                    start_sequence=sequence + 1,
                    lsp_events=self._lsp_manager.drain_events(),
                ):
                    sequence = lsp_event.sequence
                    yield RuntimeStreamChunk(kind="event", session=session, event=lsp_event)
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

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
                    payload=tool_result.data,
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

    def _run_tool_hooks(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_name: str,
        phase: str,
    ) -> _HookOutcome:
        hooks_config = self._config.hooks
        if hooks_config is None or hooks_config.enabled is not True:
            return _HookOutcome(chunks=(), last_sequence=sequence)
        if os.environ.get(self._hook_recursion_env_var) == "1":
            return _HookOutcome(chunks=(), last_sequence=sequence)

        commands = hooks_config.pre_tool if phase == "pre" else hooks_config.post_tool
        last_sequence = sequence
        emitted_chunks: list[RuntimeStreamChunk] = []
        for command in commands:
            last_sequence += 1
            try:
                subprocess.run(
                    list(command),
                    cwd=self._workspace,
                    capture_output=True,
                    text=True,
                    check=True,
                    env={**os.environ, self._hook_recursion_env_var: "1"},
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                hook_event = EventEnvelope(
                    session_id=session.session.id,
                    sequence=last_sequence,
                    event_type=RUNTIME_TOOL_HOOK_PRE if phase == "pre" else RUNTIME_TOOL_HOOK_POST,
                    source="runtime",
                    payload={
                        "phase": phase,
                        "tool_name": tool_name,
                        "session_id": session.session.id,
                        "status": "error",
                        "error": f"tool {phase}-hook failed for {tool_name}: {exc}",
                    },
                )
                return _HookOutcome(
                    chunks=(RuntimeStreamChunk(kind="event", session=session, event=hook_event),),
                    last_sequence=last_sequence,
                    failed_error=f"tool {phase}-hook failed for {tool_name}: {exc}",
                )

            yield_event = EventEnvelope(
                session_id=session.session.id,
                sequence=last_sequence,
                event_type=RUNTIME_TOOL_HOOK_PRE if phase == "pre" else RUNTIME_TOOL_HOOK_POST,
                source="runtime",
                payload={
                    "phase": phase,
                    "tool_name": tool_name,
                    "session_id": session.session.id,
                    "status": "ok",
                },
            )
            emitted_chunks.append(
                RuntimeStreamChunk(kind="event", session=session, event=yield_event)
            )

        return _HookOutcome(chunks=tuple(emitted_chunks), last_sequence=last_sequence)

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

    def effective_runtime_config(self, *, session_id: str | None = None) -> EffectiveRuntimeConfig:
        if session_id is None:
            return self._effective_runtime_config_from_metadata(None)
        validate_session_id(session_id)
        response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        self._validate_session_workspace(response.session, session_id=session_id)
        return self._effective_runtime_config_from_metadata(response.session.metadata)

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
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")
        yield from self._resume_pending_approval_impl(
            stored=stored_response,
            pending=pending,
            approval_decision=approval_decision,
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
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

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
    ) -> Iterator[RuntimeStreamChunk]:
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=stored.session.metadata,
        )

        sequence_before_turn = 1
        for event in reversed(stored.events):
            if event.event_type in (
                "runtime.tool_completed",
                "runtime.skills_applied",
                "runtime.skills_loaded",
            ):
                sequence_before_turn = event.sequence
                break

        max_stored_sequence = stored.events[-1].sequence if stored.events else 0

        tool_results: list[ToolResult] = []
        for event in stored.events:
            if event.event_type == "runtime.tool_completed":
                is_err = "error" in event.payload
                tool_results.append(
                    ToolResult(
                        tool_name=str(event.payload.get("tool", "unknown")),
                        content=str(event.payload.get("content", "")) if not is_err else None,
                        status="error" if is_err else "ok",
                        data=event.payload,
                        error=str(event.payload["error"]) if is_err else None,
                    )
                )

        graph_request = GraphRunRequest(
            session=session,
            prompt=self._prompt_from_events(stored.events),
            available_tools=self._tool_registry.definitions(),
            applied_skills=self._graph_applied_skills(session.metadata),
            context_window=self._prepare_single_agent_context_window(
                prompt=self._prompt_from_events(stored.events),
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            metadata={
                **session.metadata,
                "provider_attempt": cast(int, session.metadata.get("provider_attempt", 0)),
            },
        )
        provider_attempt = cast(int, graph_request.metadata.get("provider_attempt", 0))
        graph = self._graph_for_session_metadata(session.metadata)
        if provider_attempt > 0:
            resume_target = self._provider_chain_for_session_metadata(session.metadata).target_at(
                provider_attempt
            )
            if resume_target is not None:
                graph = self._build_graph_for_engine(
                    self._effective_runtime_config_from_metadata(session.metadata).execution_engine,
                    resume_target,
                    self._effective_runtime_config_from_metadata(session.metadata).max_steps,
                )

        loop_events: list[EventEnvelope] = []
        output: str | None = None
        try:
            for chunk in self._execute_graph_loop(
                graph=graph,
                session=session,
                sequence=sequence_before_turn,
                graph_request=graph_request,
                tool_results=tool_results,
                approval_resolution=(pending, approval_decision),
                permission_policy=self._permission_policy_for_session(session.metadata),
            ):
                if chunk.event is not None:
                    if chunk.event.sequence > max_stored_sequence:
                        loop_events.append(chunk.event)
                        yield chunk
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
                    prompt=self._prompt_from_events(stored.events),
                    session_id=stored.session.session.id,
                )
                self._persist_response(request=request, response=response)
                return
            raise

        response = RuntimeResponse(
            session=session,
            events=stored.events + tuple(loop_events),
            output=output,
        )

        request = RuntimeRequest(
            prompt=self._prompt_from_events(stored.events), session_id=stored.session.session.id
        )
        self._persist_response(request=request, response=response)
        return

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
        elif response_session.status == "completed" and (
            event.event_type == "graph.response_ready"
            or (event.event_type == "graph.loop_step" and event.payload.get("phase") == "finalize")
        ):
            status = "completed"
        return VoidCodeRuntime._session_with_status(response_session, status)

    @staticmethod
    def _validated_request(request: RuntimeRequest) -> RuntimeRequest:
        if request.session_id is None:
            return request
        return RuntimeRequest(
            prompt=request.prompt,
            session_id=validate_session_id(request.session_id),
            metadata=request.metadata,
            allocate_session_id=request.allocate_session_id,
        )

    @staticmethod
    def _resolve_session_id(request: RuntimeRequest) -> str:
        if request.session_id is not None:
            return request.session_id
        if request.allocate_session_id:
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
    def _session_with_context_window_metadata(
        session: SessionState, context_window: RuntimeContextWindow
    ) -> SessionState:
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "context_window": context_window.metadata_payload(),
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
        _ = metadata
        return self._skill_registry.runtime_contexts()

    @staticmethod
    def _frozen_applied_skill_payloads(
        contexts: Iterable[SkillRuntimeContext],
    ) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "name": context.name,
                "description": context.description,
                "content": context.content,
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
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not isinstance(content, str)
            ):
                raise ValueError("persisted applied skill payloads must include string fields")
            payloads.append({"name": name, "description": description, "content": content})
        return tuple(payloads)

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
                names = set(persisted_names)
                if names:
                    return tuple(
                        {
                            "name": skill.name,
                            "description": skill.description,
                            "content": skill.content,
                        }
                        for skill in self._skill_registry.all()
                        if skill.name in names
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
        }
        if effective_config.model is not None:
            runtime_config_metadata["model"] = effective_config.model
        if effective_config.provider_fallback is not None:
            runtime_config_metadata["provider_fallback"] = {
                "preferred_model": effective_config.provider_fallback.preferred_model,
                "fallback_models": list(effective_config.provider_fallback.fallback_models),
            }
        lsp_state = self._lsp_manager.current_state()
        runtime_config_metadata["lsp"] = {
            "mode": lsp_state.mode,
            "configured_enabled": lsp_state.configuration.configured_enabled,
            "servers": list(lsp_state.configuration.servers),
        }
        acp_state = self._acp_adapter.current_state()
        runtime_config_metadata["acp"] = {
            "mode": acp_state.mode,
            "configured_enabled": acp_state.configuration.configured_enabled,
            "status": acp_state.status,
            "available": acp_state.available,
            "last_error": acp_state.last_error,
        }
        return runtime_config_metadata

    @staticmethod
    def _envelopes_for_lsp_events(
        *,
        session_id: str,
        start_sequence: int,
        lsp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        known_event_types = {
            RUNTIME_LSP_SERVER_STARTED,
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

    def _effective_runtime_config_from_metadata(
        self, metadata: dict[str, object] | None
    ) -> EffectiveRuntimeConfig:
        approval_mode: PermissionDecision = self._config.approval_mode
        model = self._config.model
        execution_engine = self._config.execution_engine
        max_steps = self._config.max_steps
        provider_fallback = self._config.provider_fallback
        if metadata is None:
            return EffectiveRuntimeConfig(
                approval_mode=approval_mode,
                model=model,
                execution_engine=execution_engine,
                max_steps=max_steps,
                provider_fallback=provider_fallback,
            )

        persisted_runtime_config = metadata.get("runtime_config")
        if not isinstance(persisted_runtime_config, dict):
            return EffectiveRuntimeConfig(
                approval_mode=approval_mode,
                model=model,
                execution_engine=execution_engine,
                max_steps=max_steps,
                provider_fallback=provider_fallback,
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
            max_steps = persisted_max_steps
        provider_fallback = None
        persisted_provider_fallback = runtime_config.get("provider_fallback")
        if isinstance(persisted_provider_fallback, dict):
            payload = cast(dict[str, object], persisted_provider_fallback)
            preferred_model = payload.get("preferred_model")
            fallback_models = payload.get("fallback_models")
            if isinstance(preferred_model, str) and isinstance(fallback_models, list):
                raw_fallback_models = cast(list[object], fallback_models)
                parsed_fallback_models = [
                    item for item in raw_fallback_models if isinstance(item, str)
                ]
                if len(parsed_fallback_models) == len(raw_fallback_models):
                    provider_fallback = RuntimeProviderFallbackConfig(
                        preferred_model=preferred_model,
                        fallback_models=tuple(parsed_fallback_models),
                    )
        persisted_execution_engine = runtime_config.get("execution_engine")
        if persisted_execution_engine in ("deterministic", "single_agent"):
            execution_engine = persisted_execution_engine
        return EffectiveRuntimeConfig(
            approval_mode=approval_mode,
            model=model,
            execution_engine=execution_engine,
            max_steps=max_steps,
            provider_fallback=provider_fallback,
        )

    def _provider_chain_for_session_metadata(
        self, metadata: dict[str, object] | None
    ) -> ResolvedProviderChain:
        effective_config = self._effective_runtime_config_from_metadata(metadata)
        if effective_config.provider_fallback is None:
            return ResolvedProviderChain()
        return resolve_provider_chain(
            effective_config.provider_fallback,
            registry=self._model_provider_registry,
        )

    def _graph_for_session_metadata(self, metadata: dict[str, object] | None) -> RuntimeGraph:
        if self._graph_override is not None:
            return self._graph_override

        effective_config = self._effective_runtime_config_from_metadata(metadata)

        # Reuse self._graph if the session's config matches the runtime's config
        if (
            effective_config.execution_engine == self._config.execution_engine
            and effective_config.model == self._config.model
            and effective_config.max_steps == self._config.max_steps
            and effective_config.provider_fallback == self._config.provider_fallback
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


@dataclass(frozen=True, slots=True)
class EffectiveRuntimeConfig:
    approval_mode: PermissionDecision
    model: str | None
    execution_engine: ExecutionEngineName
    max_steps: int
    provider_fallback: RuntimeProviderFallbackConfig | None = None


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
