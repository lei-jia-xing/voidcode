"""Request serialization for the OpenAI Chat Completions wire.

This module turns neutral provider messages and tool definitions into the JSON
body Chat Completions accepts. It owns message/tool/image/reasoning serialization
only; transport, response parsing, and streaming live in their own modules.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ProviderExecutionError, ProviderTurnRequest

from ._wire_common import resolve_extra_request_headers, tool_name_maps
from .model_catalog import static_catalog_metadata
from .openai_wire import output_cap, requires_reasoning_content_with_tool_calls, session_id
from .reasoning_effort import clamp_effort_to_supported, normalize_reasoning_effort, reasoning_kwargs
from .thinking_rules import thinking_rule_for


def build_messages(request: ProviderTurnRequest, *, provider_name: str, model_name: str) -> list[dict[str, object]]:
    original_to_provider, _ = tool_name_maps(request)
    tool_images_present = any(message.role == "tool" and message.images for message in request.messages)
    if tool_images_present:
        return build_synthetic_feedback_messages(request, original_to_provider)
    requires_reasoning_content = requires_reasoning_content_with_tool_calls(provider_name=provider_name, model_name=model_name)
    messages: list[dict[str, object]] = []
    for message in request.messages:
        if message.role == "assistant" and message.tool_calls:
            # A reasoning-locked provider still returns tool-call turns with
            # zero reasoning tokens when its own effort ladder decides not
            # to think (observed on the real DeepSeek wire); replaying that
            # authentic shape must not fail. Carry the provider's own
            # reasoning verbatim when it exists and never synthesize it when
            # the vendor produced none.
            tool_calls: list[dict[str, object]] = []
            for call in message.tool_calls:
                arguments = json.dumps(
                    json_wire_object(call.arguments),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                tool_calls.append(
                    {
                        # Ingest normalised this id once; render it verbatim
                        # rather than substituting the tool name, which two
                        # calls to the same tool would share.
                        "id": call.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": original_to_provider.get(call.tool_name, call.tool_name),
                            "arguments": arguments,
                        },
                    }
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    # DeepSeek requires prior reasoning_content on every
                    # assistant turn of a tools request, including turns
                    # without tool calls; the core persists the provider's
                    # own text, so replay it verbatim when it exists.
                    **({"reasoning_content": message.reasoning} if requires_reasoning_content and message.reasoning else {}),
                    "tool_calls": tool_calls,
                }
            )
            continue
        if message.role == "tool":
            if message.images:
                raise ProviderExecutionError(
                    kind="unsupported_feature",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="OpenAI chat completions wire does not support image attachments in tool messages",
                    retryable=False,
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content or "",
                }
            )
        else:
            if message.images:
                content: object = [
                    {"type": "text", "text": message.content or ""},
                    *[{"type": "image_url", "image_url": {"url": f"data:{img.media_type};base64,{img.data}"}} for img in message.images],
                ]
            else:
                content = message.content
            replayed: dict[str, object] = {"role": message.role, "content": content}
            if message.role == "assistant" and requires_reasoning_content and message.reasoning:
                replayed["reasoning_content"] = message.reasoning
            messages.append(replayed)
    return messages


def build_synthetic_feedback_messages(request: ProviderTurnRequest, original_to_provider: Mapping[str, str]) -> list[dict[str, object]]:
    """Keep conversation order while converting tool feedback for selected gateways."""
    call_arguments: dict[str, Mapping[str, object]] = {}
    messages: list[dict[str, object]] = []
    for message in request.messages:
        for call in message.tool_calls:
            if call.tool_call_id is not None:
                call_arguments[call.tool_call_id] = call.arguments
        if message.role == "tool":
            arguments = call_arguments.pop(message.tool_call_id or "", None)
            payload = {
                "tool_name": original_to_provider.get(message.tool_name or "", message.tool_name),
                "tool_call_id": message.tool_call_id,
                "arguments": json_wire_object(arguments) if arguments is not None else None,
                "status": "error" if message.is_error else "ok",
                "content": message.content or "",
            }
            if message.images:
                synthetic_content: list[dict[str, object]] = [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
                ]
                for img in message.images:
                    synthetic_content.append({"type": "image_url", "image_url": {"url": f"data:{img.media_type};base64,{img.data}"}})
                messages.append({"role": "user", "content": synthetic_content})
            else:
                messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)})
        elif message.role != "assistant" or not message.tool_calls or message.content or message.images:
            if message.images:
                content = [
                    {"type": "text", "text": message.content or ""},
                    *[{"type": "image_url", "image_url": {"url": f"data:{img.media_type};base64,{img.data}"}} for img in message.images],
                ]
            else:
                content = message.content
            messages.append({"role": message.role, "content": content})
    return messages


def build_tools(request: ProviderTurnRequest) -> list[dict[str, object]]:
    original_to_provider, _ = tool_name_maps(request)
    tools: list[dict[str, object]] = []
    for tool in request.available_tools:
        schema = json_wire_object(tool.input_schema)
        schema.setdefault("type", "object")
        tools.append(
            {
                "type": "function",
                "function": {"name": original_to_provider.get(tool.name, tool.name), "description": tool.description, "parameters": schema},
            }
        )
    return tools


def build_payload(
    request: ProviderTurnRequest,
    *,
    provider_name: str,
    model_name: str,
    extra_request_headers: Mapping[str, str],
    stream: bool,
) -> dict[str, object]:
    # The request carries no catalog object, so the shipped catalog entry for
    # the provider and the mapped model is the authority the legacy
    # ``request.model_metadata`` used to be.
    metadata = static_catalog_metadata(provider_name, model_name)
    if any(bool(m.images) for m in request.messages):
        if metadata is not None and (
            metadata.supports_vision is False or (metadata.modalities_input is not None and "image" not in metadata.modalities_input)
        ):
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=provider_name,
                model_name=model_name,
                message=f"model '{model_name}' does not support image input",
                retryable=False,
            )
    rule = thinking_rule_for(provider_name, model_name)
    payload: dict[str, object] = {
        "model": model_name,
        "messages": build_messages(request, provider_name=provider_name, model_name=model_name),
        "stream": stream,
    }
    # The catalog is the authority on what this model accepts: a model that
    # cannot call tools is not handed a tool schema, and one that does not
    # reason is not handed a reasoning knob.
    if request.available_tools and (metadata is None or metadata.supports_tools is not False):
        payload["tools"] = build_tools(request)
        if not rule.requires_reasoning_content_for_tool_calls:
            payload["tool_choice"] = "auto"
    if rule.sends_output_cap_by_default:
        # OMP sends no output cap unless the caller asks for one; the kimi
        # family is the one exception, and it sends the model's own maximum
        # (``resolveOpenAIOutputTokenParam``, openai-shared.ts:630-647).
        payload[rule.max_tokens_field_or_default()] = output_cap(metadata)
    if request.reasoning_effort is not None and (metadata is None or metadata.supports_reasoning is not False):
        effort = normalize_reasoning_effort(request.reasoning_effort)
        if isinstance(effort, int):
            if effort != 0:
                raise ProviderExecutionError(
                    kind="unsupported_feature",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="This Chat Completions adapter accepts reasoning effort levels, not numeric token budgets",
                    retryable=False,
                    details={"source": "payload", "reason": "unsupported_reasoning_budget"},
                )
            effort = "off"
        if effort is not None:
            supported = metadata.supported_effort_levels if metadata is not None else None
            payload.update(
                reasoning_kwargs(
                    rule=rule,
                    effort=clamp_effort_to_supported(effort, supported),
                    supported_levels=supported,
                )
            )
    extra_headers = resolve_extra_request_headers(extra_request_headers, session_id(request))
    if extra_headers:
        # A request option, not a body field: the SDK merges it into the HTTP
        # request and never serializes it into the JSON payload.
        payload["extra_headers"] = extra_headers
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


__all__ = ["build_messages", "build_payload", "build_synthetic_feedback_messages", "build_tools"]
