from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..tools.contracts import ToolCall, ToolDefinition, ToolResult

type AppliedSkill = dict[str, str]

READ_REQUEST_PATTERN = re.compile(r"^(read|show)\s+(?P<path>.+)$", re.IGNORECASE)
GREP_REQUEST_PATTERN = re.compile(r"^grep\s+(?P<pattern>.+?)\s+(?P<path>\S+)$", re.IGNORECASE)
RUN_REQUEST_PATTERN = re.compile(r"^run\s+(?P<command>.+)$", re.IGNORECASE)
WRITE_REQUEST_PATTERN = re.compile(r"^write\s+(?P<path>\S+)\s+(?P<content>.+)$", re.IGNORECASE)


@runtime_checkable
class SingleAgentContextWindow(Protocol):
    @property
    def prompt(self) -> str: ...

    @property
    def tool_results(self) -> tuple[ToolResult, ...]: ...

    @property
    def compacted(self) -> bool: ...

    @property
    def retained_tool_result_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SingleAgentTurnRequest:
    prompt: str
    available_tools: tuple[ToolDefinition, ...]
    tool_results: tuple[ToolResult, ...]
    context_window: SingleAgentContextWindow
    applied_skills: tuple[AppliedSkill, ...]
    raw_model: str | None
    provider_name: str | None
    model_name: str | None
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class SingleAgentTurnResult:
    tool_call: ToolCall | None = None
    output: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderExecutionError(ValueError):
    kind: Literal["rate_limit", "context_limit", "invalid_model", "transient_failure"]
    provider_name: str
    model_name: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


@runtime_checkable
class SingleAgentProvider(Protocol):
    @property
    def name(self) -> str: ...

    def propose_turn(self, request: SingleAgentTurnRequest) -> SingleAgentTurnResult: ...


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def single_agent_provider(self) -> SingleAgentProvider: ...


@dataclass(frozen=True, slots=True)
class StubSingleAgentProvider:
    name: str

    def propose_turn(self, request: SingleAgentTurnRequest) -> SingleAgentTurnResult:
        commands = [line.strip() for line in request.prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")

        step_index = len(request.tool_results)
        if step_index >= len(commands):
            if not request.context_window.tool_results:
                raise ValueError("request must contain at least one actionable command")
            last_result = request.context_window.tool_results[-1]
            output = last_result.content if last_result.content else ""
            return SingleAgentTurnResult(
                output=self._apply_skill_context_to_output(output, request.applied_skills)
            )

        trimmed_prompt = commands[step_index]

        read_match = READ_REQUEST_PATTERN.match(trimmed_prompt)
        if read_match is not None:
            path_text = read_match.group("path").strip()
            if not path_text:
                raise ValueError("request path must not be empty")
            self._ensure_tool(request.available_tools, "read_file", read_only=True)
            return SingleAgentTurnResult(tool_call=ToolCall("read_file", {"path": path_text}))

        grep_match = GREP_REQUEST_PATTERN.match(trimmed_prompt)
        if grep_match is not None:
            pattern_text = grep_match.group("pattern").strip()
            path_text = grep_match.group("path").strip()
            if not pattern_text:
                raise ValueError("request pattern must not be empty")
            if not path_text:
                raise ValueError("request path must not be empty")
            self._ensure_tool(request.available_tools, "grep", read_only=True)
            return SingleAgentTurnResult(
                tool_call=ToolCall("grep", {"pattern": pattern_text, "path": path_text})
            )

        run_match = RUN_REQUEST_PATTERN.match(trimmed_prompt)
        if run_match is not None:
            command_text = run_match.group("command").strip()
            if not command_text:
                raise ValueError("request command must not be empty")
            self._ensure_tool(request.available_tools, "shell_exec", read_only=False)
            return SingleAgentTurnResult(
                tool_call=ToolCall("shell_exec", {"command": command_text})
            )

        write_match = WRITE_REQUEST_PATTERN.match(trimmed_prompt)
        if write_match is not None:
            path_text = write_match.group("path").strip()
            content_text = write_match.group("content")
            if not path_text:
                raise ValueError("request path must not be empty")
            if not content_text:
                raise ValueError("request content must not be empty")
            self._ensure_tool(request.available_tools, "write_file", read_only=False)
            return SingleAgentTurnResult(
                tool_call=ToolCall("write_file", {"path": path_text, "content": content_text})
            )

        msg = (
            "unsupported request: use 'read <relative-path>', 'show <relative-path>', "
            "'grep <pattern> <relative-path>', 'run <command>', or "
            "'write <relative-path> <content>'"
        )
        raise ValueError(msg)

    @staticmethod
    def _ensure_tool(tools: tuple[ToolDefinition, ...], tool_name: str, *, read_only: bool) -> None:
        if any(tool.name == tool_name and tool.read_only is read_only for tool in tools):
            return
        raise ValueError(f"{tool_name} tool is not registered for single-agent execution")

    @staticmethod
    def _apply_skill_context_to_output(
        output: str,
        applied_skills: tuple[AppliedSkill, ...],
    ) -> str:
        if not applied_skills:
            return output

        skill_lines = ["[applied skills]"]
        skill_lines.extend(f"- {skill['name']}: {skill['description']}" for skill in applied_skills)
        skill_lines.append("")
        skill_lines.append(output)
        return "\n".join(skill_lines)
