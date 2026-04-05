from __future__ import annotations

import re
from dataclasses import dataclass

from ..runtime.events import EventEnvelope
from ..runtime.session import SessionState
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult
from .contracts import GraphRunRequest, GraphRunResult

READ_REQUEST_PATTERN = re.compile(r"^(read|show)\s+(?P<path>.+)$", re.IGNORECASE)
WRITE_REQUEST_PATTERN = re.compile(r"^write\s+(?P<path>\S+)\s+(?P<content>.+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DeterministicReadOnlyPlan:
    tool_call: ToolCall


class DeterministicReadOnlyGraph:
    def plan(self, request: GraphRunRequest) -> DeterministicReadOnlyPlan:
        prompt = request.prompt.strip()
        read_match = READ_REQUEST_PATTERN.match(prompt)
        if read_match is not None:
            path_text = read_match.group("path").strip()
            if not path_text:
                raise ValueError("request path must not be empty")

            self._ensure_read_tool_available(request.available_tools)
            return DeterministicReadOnlyPlan(
                tool_call=ToolCall(tool_name="read_file", arguments={"path": path_text})
            )

        write_match = WRITE_REQUEST_PATTERN.match(prompt)
        if write_match is not None:
            path_text = write_match.group("path").strip()
            content_text = write_match.group("content")
            if not path_text:
                raise ValueError("request path must not be empty")
            if not content_text:
                raise ValueError("request content must not be empty")

            self._ensure_write_tool_available(request.available_tools)
            return DeterministicReadOnlyPlan(
                tool_call=ToolCall(
                    tool_name="write_file",
                    arguments={"path": path_text, "content": content_text},
                )
            )

        msg = (
            "unsupported request: use 'read <relative-path>', 'show <relative-path>', "
            "or 'write <relative-path> <content>'"
        )
        raise ValueError(msg)

    def finalize(
        self,
        request: GraphRunRequest,
        tool_result: ToolResult,
        *,
        session: SessionState,
    ) -> GraphRunResult:
        return GraphRunResult(
            session=session,
            events=(
                EventEnvelope(
                    session_id=request.session.session.id,
                    sequence=6,
                    event_type="graph.response_ready",
                    source="graph",
                    payload={"output_preview": tool_result.content or ""},
                ),
            ),
            tool_results=(tool_result,),
            output=tool_result.content,
        )

    def _ensure_read_tool_available(self, tools: tuple[ToolDefinition, ...]) -> None:
        if any(tool.name == "read_file" and tool.read_only for tool in tools):
            return
        raise ValueError("read_file tool is not registered for graph execution")

    def _ensure_write_tool_available(self, tools: tuple[ToolDefinition, ...]) -> None:
        if any(tool.name == "write_file" and not tool.read_only for tool in tools):
            return
        raise ValueError("write_file tool is not registered for graph execution")
