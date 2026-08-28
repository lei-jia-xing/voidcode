from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..command.resolver import resolve_tool_instruction
from ..runtime.context_window import normalize_read_output
from ..runtime.events import (
    GRAPH_LOOP_STEP,
    GRAPH_MODEL_TURN,
    GRAPH_RESPONSE_READY,
)
from ..runtime.session import SessionState
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult
from .contracts import GraphEvent, GraphLoopState, GraphRunRequest


@dataclass(frozen=True, slots=True)
class DeterministicReadOnlyStep:
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False

    def __post_init__(self) -> None:
        if self.is_finished:
            if self.tool_call is not None:
                raise ValueError("finished graph steps must not include a tool call")
            if self.output is None:
                raise ValueError("finished graph steps must include output")
            return
        if self.tool_call is None:
            raise ValueError("non-finished graph steps must include a tool call")
        if self.output is not None:
            raise ValueError("non-finished graph steps must not include output")


class DeterministicGraph:
    def __init__(self, *, max_steps: int = 4) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._max_steps = max_steps

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> DeterministicReadOnlyStep:
        state = self._initial_state(
            request=request,
            tool_results=tool_results,
            session=session,
        )
        planned = self._plan_turn_node(state)
        state["events"].extend(cast(list[GraphEvent], planned.get("events", [])))
        state["tool_calls"].extend(cast(list[ToolCall], planned.get("tool_calls", [])))
        if "current_turn" in planned:
            state["current_turn"] = cast(int, planned["current_turn"])
        if "output" in planned:
            state["output"] = cast(str | None, planned["output"])
        if "error" in planned:
            state["error"] = cast(str | None, planned["error"])

        if state["error"] is not None:
            raise ValueError(state["error"])

        tool_calls = state["tool_calls"]
        if state["output"] is not None:
            return DeterministicReadOnlyStep(
                events=tuple(state["events"]),
                output=state["output"],
                is_finished=True,
            )
        if tool_calls:
            return DeterministicReadOnlyStep(
                events=tuple(state["events"]),
                tool_call=tool_calls[-1],
            )

        finalized = self._finalize_turn_node(state)
        state["events"].extend(cast(list[GraphEvent], finalized["events"]))
        output = cast(str, finalized["output"])
        return DeterministicReadOnlyStep(
            events=tuple(state["events"]),
            output=output,
            is_finished=True,
        )

    def _initial_state(
        self,
        *,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        session: SessionState,
    ) -> GraphLoopState:
        _ = session
        state: GraphLoopState = {
            "prompt": request.prompt,
            "metadata": request.metadata,
            "current_turn": len(tool_results) + 1,
            "tool_calls": [],
            "tool_results": list(tool_results),
            "available_tools": request.available_tools,
            "events": [],
            "output": None,
            "error": None,
            "approval_request_id": None,
        }
        return state

    def _plan_turn_node(self, state: GraphLoopState) -> dict[str, object]:
        current_turn = state["current_turn"]
        if current_turn > self._max_steps:
            return {"error": f"graph exceeded max steps: {self._max_steps}"}

        planning_events = [
            self._graph_event(
                GRAPH_LOOP_STEP,
                {
                    "step": current_turn,
                    "phase": "plan",
                    "max_steps": self._max_steps,
                },
            ),
            self._graph_event(
                GRAPH_MODEL_TURN,
                {
                    "turn": current_turn,
                    "mode": "deterministic",
                    "prompt": state["prompt"],
                },
            ),
        ]

        if current_turn == 1 and isinstance(state["metadata"].get("command"), dict):
            return {
                "events": [
                    *planning_events,
                    self._graph_event(
                        GRAPH_RESPONSE_READY,
                        {"output_preview": state["prompt"][:200]},
                    ),
                ],
                "output": state["prompt"],
                "current_turn": current_turn + 1,
            }

        try:
            tool_call = self._select_tool_call(state["prompt"], state["available_tools"], state["tool_results"])
        except ValueError as exc:
            return {
                "events": planning_events,
                "error": str(exc),
                "current_turn": current_turn + 1,
            }

        if tool_call is None:
            return {"current_turn": current_turn + 1}

        return {
            "events": planning_events,
            "tool_calls": [tool_call],
            "current_turn": current_turn + 1,
        }

    def _finalize_turn_node(self, state: GraphLoopState) -> dict[str, object]:
        current_turn = state["current_turn"]

        last_result = state["tool_results"][-1]
        return {
            "events": [
                self._graph_event(
                    GRAPH_LOOP_STEP,
                    {
                        "step": current_turn,
                        "phase": "finalize",
                        "max_steps": self._max_steps,
                    },
                ),
                self._graph_event(
                    GRAPH_RESPONSE_READY,
                    {"output_preview": normalize_read_output(last_result.content) or ""},
                ),
            ],
            "output": normalize_read_output(last_result.content) or "",
        }

    def _select_tool_call(
        self,
        prompt: str,
        available_tools: tuple[ToolDefinition, ...],
        tool_results: list[ToolResult],
    ) -> ToolCall | None:
        commands = [line.strip() for line in prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")

        step_index = len(tool_results)
        if step_index >= len(commands):
            return None

        resolution = resolve_tool_instruction(
            commands[step_index],
            available_tools,
            unavailable_message_suffix="graph execution",
        )
        return resolution.tool_call

    @staticmethod
    def _graph_event(event_type: str, payload: dict[str, object]) -> GraphEvent:
        return GraphEvent(
            event_type=event_type,
            source="graph",
            payload=payload,
        )
