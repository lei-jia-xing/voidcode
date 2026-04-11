from __future__ import annotations

import pytest

from voidcode.runtime.context_window import RuntimeContextWindow
from voidcode.runtime.model_provider import ModelProviderRegistry, resolve_provider_model
from voidcode.runtime.single_agent_provider import (
    SingleAgentTurnRequest,
    StubSingleAgentProvider,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult


def _tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(name="read_file", description="read", input_schema={}, read_only=True),
        ToolDefinition(name="grep", description="grep", input_schema={}, read_only=True),
        ToolDefinition(name="write_file", description="write", input_schema={}, read_only=False),
        ToolDefinition(name="shell_exec", description="run", input_schema={}, read_only=False),
    )


def test_stub_single_agent_provider_proposes_tool_call_for_first_turn() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    result = StubSingleAgentProvider(name="opencode").propose_turn(
        SingleAgentTurnRequest(
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            tool_results=(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            applied_skills=(),
            raw_model=provider_model.selection.raw_model,
            provider_name=provider_model.selection.provider,
            model_name=provider_model.selection.model,
        )
    )

    assert result.tool_call is not None
    assert result.tool_call.tool_name == "read_file"
    assert result.tool_call.arguments == {"path": "sample.txt"}
    assert result.output is None


def test_stub_single_agent_provider_finalizes_from_last_tool_result() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    result = StubSingleAgentProvider(name="opencode").propose_turn(
        SingleAgentTurnRequest(
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            tool_results=(
                ToolResult(
                    tool_name="read_file",
                    content="alpha\nbeta\n",
                    status="ok",
                    data={"path": "sample.txt", "content": "alpha\nbeta\n"},
                ),
            ),
            context_window=RuntimeContextWindow(
                prompt="read sample.txt",
                tool_results=(
                    ToolResult(
                        tool_name="read_file",
                        content="alpha\nbeta\n",
                        status="ok",
                        data={"path": "sample.txt", "content": "alpha\nbeta\n"},
                    ),
                ),
            ),
            applied_skills=(),
            raw_model=provider_model.selection.raw_model,
            provider_name=provider_model.selection.provider,
            model_name=provider_model.selection.model,
        )
    )

    assert result.tool_call is None
    assert result.output == "alpha\nbeta\n"


def test_stub_single_agent_provider_rejects_unsupported_requests() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    with pytest.raises(ValueError, match="unsupported request"):
        _ = StubSingleAgentProvider(name="opencode").propose_turn(
            SingleAgentTurnRequest(
                prompt="summarize sample.txt",
                available_tools=_tool_definitions(),
                tool_results=(),
                context_window=RuntimeContextWindow(prompt="summarize sample.txt"),
                applied_skills=(),
                raw_model=provider_model.selection.raw_model,
                provider_name=provider_model.selection.provider,
                model_name=provider_model.selection.model,
            )
        )


def test_stub_single_agent_provider_uses_bounded_context_window_results_for_finalize() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    result = StubSingleAgentProvider(name="opencode").propose_turn(
        SingleAgentTurnRequest(
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            tool_results=(
                ToolResult(
                    tool_name="read_file",
                    content="old\n",
                    status="ok",
                    data={"path": "sample.txt", "content": "old\n"},
                ),
                ToolResult(
                    tool_name="read_file",
                    content="new\n",
                    status="ok",
                    data={"path": "sample.txt", "content": "new\n"},
                ),
            ),
            context_window=RuntimeContextWindow(
                prompt="read sample.txt",
                tool_results=(
                    ToolResult(
                        tool_name="read_file",
                        content="new\n",
                        status="ok",
                        data={"path": "sample.txt", "content": "new\n"},
                    ),
                ),
                compacted=True,
                compaction_reason="tool_result_window",
                original_tool_result_count=2,
                retained_tool_result_count=1,
            ),
            applied_skills=(),
            raw_model=provider_model.selection.raw_model,
            provider_name=provider_model.selection.provider,
            model_name=provider_model.selection.model,
        )
    )

    assert result.output == "new\n"
