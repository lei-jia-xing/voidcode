from __future__ import annotations

import pytest

from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.runtime.context_window import (
    RuntimeAssembledContext,
    RuntimeContextSegment,
    RuntimeContextWindow,
)
from voidcode.runtime.provider_protocol import (
    ProviderExecutionError,
    ProviderTurnRequest,
    StubTurnProvider,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult


def _tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(name="read_file", description="read", input_schema={}, read_only=True),
        ToolDefinition(name="grep", description="grep", input_schema={}, read_only=True),
        ToolDefinition(name="write_file", description="write", input_schema={}, read_only=False),
        ToolDefinition(name="shell_exec", description="run", input_schema={}, read_only=False),
    )


def _assembled_from_context_window(context_window: RuntimeContextWindow) -> RuntimeAssembledContext:
    segments: list[RuntimeContextSegment] = [
        RuntimeContextSegment(role="user", content=context_window.prompt),
    ]
    for index, result in enumerate(context_window.tool_results, start=1):
        raw_tool_call_id = result.data.get("tool_call_id")
        tool_call_id = (
            raw_tool_call_id
            if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip()
            else f"voidcode_tool_{index}"
        )
        tool_arguments = result.data.get("arguments")
        arguments_payload = tool_arguments if isinstance(tool_arguments, dict) else {}
        segments.append(
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                tool_arguments=arguments_payload,
            )
        )
        segments.append(
            RuntimeContextSegment(
                role="tool",
                content=result.content,
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
            )
        )
    return RuntimeAssembledContext(
        prompt=context_window.prompt,
        tool_results=context_window.tool_results,
        continuity_state=context_window.continuity_state,
        segments=tuple(segments),
        metadata={},
    )


def test_stub_provider_protocol_proposes_tool_call_for_first_turn() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    result = StubTurnProvider(name="opencode").propose_turn(
        ProviderTurnRequest(
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
            available_tools=_tool_definitions(),
            raw_model=provider_model.selection.raw_model,
            provider_name=provider_model.selection.provider,
            model_name=provider_model.selection.model,
        )
    )

    assert result.tool_call is not None
    assert result.tool_call.tool_name == "read_file"
    assert result.tool_call.arguments == {"filePath": "sample.txt"}
    assert result.output is None


def test_stub_provider_protocol_finalizes_from_last_tool_result() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    result = StubTurnProvider(name="opencode").propose_turn(
        ProviderTurnRequest(
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(
                    prompt="read sample.txt",
                    tool_results=(
                        ToolResult(
                            tool_name="read_file",
                            content="alpha\nbeta\n",
                            status="ok",
                            data={"path": "sample.txt", "type": "file", "content": "alpha\nbeta\n"},
                        ),
                    ),
                )
            ),
            available_tools=_tool_definitions(),
            raw_model=provider_model.selection.raw_model,
            provider_name=provider_model.selection.provider,
            model_name=provider_model.selection.model,
        )
    )

    assert result.tool_call is None
    assert result.output == "alpha\nbeta\n"


def test_stub_provider_protocol_rejects_unsupported_requests() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    with pytest.raises(ValueError, match="unsupported request"):
        _ = StubTurnProvider(name="opencode").propose_turn(
            ProviderTurnRequest(
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="summarize sample.txt")
                ),
                available_tools=_tool_definitions(),
                raw_model=provider_model.selection.raw_model,
                provider_name=provider_model.selection.provider,
                model_name=provider_model.selection.model,
            )
        )


def test_provider_execution_error_requires_supported_kind() -> None:
    error = ProviderExecutionError(
        kind="rate_limit",
        provider_name="opencode",
        model_name="gpt-5.4",
        message="too many requests",
    )

    assert error.kind == "rate_limit"
    assert str(error) == "too many requests"


def test_stub_provider_protocol_uses_bounded_context_window_results_for_finalize() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    result = StubTurnProvider(name="opencode").propose_turn(
        ProviderTurnRequest(
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(
                    prompt="read sample.txt",
                    tool_results=(
                        ToolResult(
                            tool_name="read_file",
                            content="new\n",
                            status="ok",
                            data={"path": "sample.txt", "type": "file", "content": "new\n"},
                        ),
                    ),
                    compacted=True,
                    compaction_reason="tool_result_window",
                    original_tool_result_count=2,
                    retained_tool_result_count=1,
                )
            ),
            available_tools=_tool_definitions(),
            raw_model=provider_model.selection.raw_model,
            provider_name=provider_model.selection.provider,
            model_name=provider_model.selection.model,
        )
    )

    assert result.output == "new\n"
