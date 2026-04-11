from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..runtime.context_window import RuntimeContextWindow
from ..runtime.events import GRAPH_LOOP_STEP, GRAPH_MODEL_TURN, GRAPH_RESPONSE_READY
from ..runtime.model_provider import ResolvedProviderModel
from ..runtime.session import SessionState
from ..runtime.single_agent_provider import SingleAgentProvider, SingleAgentTurnRequest
from ..tools.contracts import ToolCall, ToolResult
from .contracts import GraphEvent, GraphRunRequest


@dataclass(frozen=True, slots=True)
class SingleAgentStep:
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False


class ProviderSingleAgentGraph:
    def __init__(
        self,
        *,
        provider: SingleAgentProvider,
        provider_model: ResolvedProviderModel,
        max_steps: int = 4,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._provider = provider
        self._provider_model = provider_model
        self._max_steps = max_steps

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> SingleAgentStep:
        _ = session
        current_turn = len(tool_results) + 1
        if current_turn > self._max_steps:
            raise ValueError(f"graph exceeded max steps: {self._max_steps}")

        planning_events = (
            self._graph_event(
                GRAPH_LOOP_STEP,
                {"step": current_turn, "phase": "plan", "max_steps": self._max_steps},
            ),
            self._graph_event(
                GRAPH_MODEL_TURN,
                {
                    "turn": current_turn,
                    "mode": "single_agent",
                    "provider": self._provider.name,
                    "model": self._provider_model.selection.model,
                    "attempt": request.metadata.get("provider_attempt", 0),
                    "prompt": request.prompt,
                },
            ),
        )

        turn_result = self._provider.propose_turn(
            SingleAgentTurnRequest(
                prompt=request.prompt,
                available_tools=request.available_tools,
                tool_results=tool_results,
                context_window=request.context_window
                or RuntimeContextWindow(prompt=request.prompt),
                applied_skills=request.applied_skills,
                raw_model=self._provider_model.selection.raw_model,
                provider_name=self._provider_model.selection.provider,
                model_name=self._provider_model.selection.model,
                attempt=cast(int, request.metadata.get("provider_attempt", 0)),
            )
        )

        if turn_result.output is not None:
            finalize_events = planning_events + (
                self._graph_event(
                    GRAPH_LOOP_STEP,
                    {"step": current_turn + 1, "phase": "finalize", "max_steps": self._max_steps},
                ),
                self._graph_event(GRAPH_RESPONSE_READY, {"output_preview": turn_result.output}),
            )
            return SingleAgentStep(
                events=finalize_events,
                output=turn_result.output,
                is_finished=True,
            )

        return SingleAgentStep(events=planning_events, tool_call=turn_result.tool_call)

    @staticmethod
    def _graph_event(event_type: str, payload: dict[str, object]) -> GraphEvent:
        return GraphEvent(event_type=event_type, source="graph", payload=payload)
