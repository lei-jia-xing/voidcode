from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .protocol import (
    ProviderStreamEvent,
    SingleAgentProvider,
    SingleAgentTurnRequest,
    StubSingleAgentProvider,
    wrap_provider_stream,
)


@dataclass(frozen=True, slots=True)
class OpenAIModelProvider:
    name: str = "openai"

    def single_agent_provider(self) -> SingleAgentProvider:
        return _OpenAIStubSingleAgentProvider(name=self.name)


@dataclass(frozen=True, slots=True)
class _OpenAIStubSingleAgentProvider(StubSingleAgentProvider):
    def stream_turn(self, request: SingleAgentTurnRequest) -> Iterator[ProviderStreamEvent]:
        result = self.propose_turn(request)
        events: list[ProviderStreamEvent] = []
        if result.output is not None:
            events.extend(
                (
                    ProviderStreamEvent(kind="delta", channel="text", text=result.output),
                    ProviderStreamEvent(kind="content", channel="text", text=result.output),
                )
            )
        if result.tool_call is not None:
            events.append(
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text=f"tool:{result.tool_call.tool_name}",
                )
            )
        events.append(ProviderStreamEvent(kind="done", done_reason="completed"))
        model_name = request.model_name or "unknown"
        return wrap_provider_stream(
            iter(events),
            provider_name=self.name,
            model_name=model_name,
            abort_signal=request.abort_signal,
            chunk_timeout_seconds=3.0,
        )
