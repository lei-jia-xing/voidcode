from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import re
from dataclasses import dataclass
from typing import Protocol, cast

from langgraph.graph import END, START, StateGraph

from ..runtime.events import (
    GRAPH_LOOP_STEP,
    GRAPH_MODEL_TURN,
    GRAPH_RESPONSE_READY,
)
from ..runtime.session import SessionState
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult
from .contracts import GraphEvent, GraphLoopState, GraphRunRequest

READ_REQUEST_PATTERN = re.compile(r"^(read|show)\s+(?P<path>.+)$", re.IGNORECASE)
GREP_REQUEST_PATTERN = re.compile(r"^grep\s+(?P<pattern>.+?)\s+(?P<path>\S+)$", re.IGNORECASE)
RUN_REQUEST_PATTERN = re.compile(r"^run\s+(?P<command>.+)$", re.IGNORECASE)
WRITE_REQUEST_PATTERN = re.compile(r"^write\s+(?P<path>\S+)\s+(?P<content>.+)$", re.IGNORECASE)


class _CompiledGraphApp(Protocol):
    def invoke(self, state: GraphLoopState) -> object: ...


class _StateGraphBuilder(Protocol):
    def add_node(self, node: str, action: object) -> object: ...

    def add_edge(self, start_key: str, end_key: str) -> object: ...

    def add_conditional_edges(
        self,
        source: str,
        path: object,
        path_map: dict[str, str],
    ) -> object: ...

    def compile(self) -> _CompiledGraphApp: ...


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
        workflow = cast(_StateGraphBuilder, StateGraph(GraphLoopState))
        workflow.add_node("plan_turn", self._plan_turn_node)
        workflow.add_node("finalize_turn", self._finalize_turn_node)
        workflow.add_edge(START, "plan_turn")
        workflow.add_conditional_edges(
            "plan_turn",
            self._route_after_plan,
            {"tool": END, "finalize": "finalize_turn", "error": END},
        )
        workflow.add_edge("finalize_turn", END)
        self._app: _CompiledGraphApp = workflow.compile()

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> DeterministicReadOnlyStep:
        graph_state = self._invoke(
            self._initial_state(
                request=request,
                tool_results=tool_results,
                session=session,
            )
        )
        if graph_state["error"] is not None:
            raise ValueError(graph_state["error"])

        tool_calls = graph_state["tool_calls"]
        events = tuple(graph_state["events"])

        is_finished = False
        step_tool_call = None

        if graph_state["output"] is not None:
            is_finished = True
        elif tool_calls:
            step_tool_call = tool_calls[-1]

        return DeterministicReadOnlyStep(
            events=events,
            tool_call=step_tool_call,
            output=graph_state["output"],
            is_finished=is_finished,
        )

    def _invoke(self, state: GraphLoopState) -> GraphLoopState:
        result = self._app.invoke(state)
        return cast(GraphLoopState, result)

    def _initial_state(
        self,
        *,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        session: SessionState,
    ) -> GraphLoopState:
        state: GraphLoopState = {
            "prompt": request.prompt,
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

        try:
            tool_call = self._select_tool_call(
                state["prompt"], state["available_tools"], state["tool_results"]
            )
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

    def _route_after_plan(self, state: GraphLoopState) -> str:
        if state["error"] is not None:
            return "error"
        if state["tool_calls"]:
            return "tool"
        return "finalize"

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
                    {"output_preview": last_result.content or ""},
                ),
            ],
            "output": last_result.content if last_result.content is not None else "",
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

        trimmed_prompt = commands[step_index]

        read_match = READ_REQUEST_PATTERN.match(trimmed_prompt)
        if read_match is not None:
            path_text = read_match.group("path").strip()
            if not path_text:
                raise ValueError("request path must not be empty")

            self._ensure_read_tool_available(available_tools)
            return ToolCall(tool_name="read_file", arguments={"path": path_text})

        grep_match = GREP_REQUEST_PATTERN.match(trimmed_prompt)
        if grep_match is not None:
            pattern_text = grep_match.group("pattern").strip()
            path_text = grep_match.group("path").strip()
            if not pattern_text:
                raise ValueError("request pattern must not be empty")
            if not path_text:
                raise ValueError("request path must not be empty")

            self._ensure_grep_tool_available(available_tools)
            return ToolCall(
                tool_name="grep",
                arguments={"pattern": pattern_text, "path": path_text},
            )

        run_match = RUN_REQUEST_PATTERN.match(trimmed_prompt)
        if run_match is not None:
            command_text = run_match.group("command").strip()
            if not command_text:
                raise ValueError("request command must not be empty")

            self._ensure_shell_exec_tool_available(available_tools)
            return ToolCall(tool_name="shell_exec", arguments={"command": command_text})

        write_match = WRITE_REQUEST_PATTERN.match(trimmed_prompt)
        if write_match is not None:
            path_text = write_match.group("path").strip()
            content_text = write_match.group("content")
            if not path_text:
                raise ValueError("request path must not be empty")
            if not content_text:
                raise ValueError("request content must not be empty")

            self._ensure_write_tool_available(available_tools)
            return ToolCall(
                tool_name="write_file",
                arguments={"path": path_text, "content": content_text},
            )

        msg = (
            "unsupported request: use 'read <relative-path>', 'show <relative-path>', "
            "'grep <pattern> <relative-path>', 'run <command>', or "
            "'write <relative-path> <content>'"
        )
        raise ValueError(msg)

    @staticmethod
    def _graph_event(event_type: str, payload: dict[str, object]) -> GraphEvent:
        return GraphEvent(
            event_type=event_type,
            source="graph",
            payload=payload,
        )

    def _ensure_read_tool_available(self, tools: tuple[ToolDefinition, ...]) -> None:
        if any(tool.name == "read_file" and tool.read_only for tool in tools):
            return
        raise ValueError("read_file tool is not registered for graph execution")

    def _ensure_grep_tool_available(self, tools: tuple[ToolDefinition, ...]) -> None:
        if any(tool.name == "grep" and tool.read_only for tool in tools):
            return
        raise ValueError("grep tool is not registered for graph execution")

    def _ensure_write_tool_available(self, tools: tuple[ToolDefinition, ...]) -> None:
        if any(tool.name == "write_file" and not tool.read_only for tool in tools):
            return
        raise ValueError("write_file tool is not registered for graph execution")

    def _ensure_shell_exec_tool_available(self, tools: tuple[ToolDefinition, ...]) -> None:
        if any(tool.name == "shell_exec" and not tool.read_only for tool in tools):
            return
        raise ValueError("shell_exec tool is not registered for graph execution")
