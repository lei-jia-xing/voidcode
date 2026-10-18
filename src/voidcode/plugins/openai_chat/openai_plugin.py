"""OpenAI Chat provider plugin implementing TurnProvider using the official OpenAI SDK."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import openai
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import (
    ProviderDoneReason,
    ProviderErrorKind,
    ProviderExecutionError,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from voidcode.sdk.tools import ToolCall


def _format_error_message(exc: Exception, model_name: str) -> str:
    """Controlled error message with no raw bodies or credentials leaked."""
    if isinstance(exc, AuthenticationError):
        return "Upstream authentication failed (HTTP 401)"
    if isinstance(exc, RateLimitError):
        return "Upstream rate limit exceeded (HTTP 429)"
    if isinstance(exc, NotFoundError):
        return f"Upstream model or endpoint not found (HTTP 404): {model_name}"
    if isinstance(exc, BadRequestError):
        return "Upstream bad request (HTTP 400)"
    if isinstance(exc, APITimeoutError):
        return "Upstream request timed out"
    if isinstance(exc, APIConnectionError):
        return "Upstream network connection failed"
    if isinstance(exc, InternalServerError):
        return "Upstream internal server error (HTTP 500)"
    if isinstance(exc, APIStatusError):
        return f"Upstream returned HTTP status {exc.status_code}"
    if isinstance(exc, APIError):
        return f"Upstream API error: {type(exc).__name__}"
    return f"Unexpected error: {type(exc).__name__}"


# ponytail: non-streaming propose_turn in P1; SSE streaming with token events migrates in P4
# ponytail: single cached OpenAI client per provider; pooling and proxy config migrate in P4
class OpenAIChatProvider:
    name: str = "openai-chat"

    def __init__(self, config: Mapping[str, object] | None = None) -> None:
        self._config = dict(config or {})
        raw_providers = self._config.get("providers")
        self._provider_configs: dict[str, dict[str, Any]] = {}
        if isinstance(raw_providers, Mapping):
            for pid, pconf in raw_providers.items():
                if isinstance(pconf, Mapping):
                    self._provider_configs[str(pid)] = dict(pconf)
        self._clients: dict[str, openai.OpenAI] = {}

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()

    def _resolve_client(self, provider_name: str | None, model_name: str) -> tuple[openai.OpenAI, str]:
        if not provider_name or not isinstance(provider_name, str):
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name="unknown",
                model_name=model_name,
                message="request.provider_name is required; no silent fallback allowed",
            )

        entry = self._provider_configs.get(provider_name)
        if entry is None:
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name=provider_name,
                model_name=model_name,
                message=f"Provider '{provider_name}' is not configured in plugin config",
            )

        api_key = entry.get("api_key")
        if not api_key or not isinstance(api_key, str) or not api_key.strip():
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name=provider_name,
                model_name=model_name,
                message=f"Provider '{provider_name}' has no API key configured (missing credential)",
            )

        base_url = entry.get("base_url")
        if not isinstance(base_url, str):
            base_url = ""
        try:
            endpoint = urlsplit(base_url)
            valid_endpoint = endpoint.scheme in {"http", "https"} and endpoint.hostname is not None and endpoint.port != 0
        except ValueError:
            valid_endpoint = False
        if not valid_endpoint:
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name=provider_name,
                model_name=model_name,
                message=f"Provider '{provider_name}' requires an explicit absolute HTTP(S) base_url; no silent fallback allowed",
            )

        if provider_name not in self._clients:
            self._clients[provider_name] = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                timeout=120.0,
            )

        return self._clients[provider_name], provider_name

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        model_name = request.model_name
        if not model_name or not isinstance(model_name, str):
            raise ProviderExecutionError(
                kind="invalid_model",
                provider_name=request.provider_name or "openai-chat",
                model_name=str(model_name or ""),
                message="Request model_name must be a non-empty string",
            )

        client, provider_id = self._resolve_client(request.provider_name, model_name)

        if request.abort_signal is not None and request.abort_signal.cancelled:
            raise ProviderExecutionError(
                kind="cancelled",
                provider_name=provider_id,
                model_name=model_name,
                message="Turn cancelled before provider request",
            )

        # Build messages payload
        messages_payload: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role == "system":
                messages_payload.append({"role": "system", "content": msg.content or ""})
            elif msg.role == "user":
                messages_payload.append({"role": "user", "content": msg.content or ""})
            elif msg.role == "tool":
                messages_payload.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id or "",
                        "content": msg.content or "",
                    }
                )
            elif msg.role == "assistant":
                asst_payload: dict[str, Any] = {"role": "assistant"}
                if msg.content is not None:
                    asst_payload["content"] = msg.content
                # True reasoning replay: replay exact reasoning without synthesis
                if msg.reasoning is not None:
                    asst_payload["reasoning_content"] = msg.reasoning
                if msg.tool_calls:
                    tcs: list[dict[str, Any]] = []
                    for tc in msg.tool_calls:
                        tcs.append(
                            {
                                "id": tc.tool_call_id or "",
                                "type": "function",
                                "function": {
                                    "name": tc.tool_name,
                                    "arguments": json.dumps(json_wire_object(tc.arguments), separators=(",", ":")),
                                },
                            }
                        )
                    asst_payload["tool_calls"] = tcs
                    if "content" not in asst_payload:
                        asst_payload["content"] = None
                messages_payload.append(asst_payload)

        # Build tools payload
        tools_payload: list[dict[str, Any]] | None = None
        if request.available_tools:
            tools_payload = []
            for tool_def in request.available_tools:
                tools_payload.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool_def.name,
                            "description": tool_def.description,
                            "parameters": json_wire_object(tool_def.input_schema),
                        },
                    }
                )

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages_payload,
            "timeout": 120.0,
        }
        if tools_payload:
            kwargs["tools"] = tools_payload
        if request.reasoning_effort and request.reasoning_effort != "none":
            kwargs["reasoning_effort"] = request.reasoning_effort

        try:
            response = client.chat.completions.create(**kwargs)
        except AuthenticationError as exc:
            raise ProviderExecutionError(
                kind="missing_auth",
                provider_name=provider_id,
                model_name=model_name,
                message=_format_error_message(exc, model_name),
                details={"status_code": exc.status_code},
            ) from None
        except RateLimitError as exc:
            raise ProviderExecutionError(
                kind="rate_limit",
                provider_name=provider_id,
                model_name=model_name,
                message=_format_error_message(exc, model_name),
                retryable=True,
                details={"status_code": exc.status_code},
            ) from None
        except NotFoundError as exc:
            raise ProviderExecutionError(
                kind="invalid_model",
                provider_name=provider_id,
                model_name=model_name,
                message=_format_error_message(exc, model_name),
                details={"status_code": exc.status_code},
            ) from None
        except BadRequestError as exc:
            code = getattr(exc, "code", None)
            kind: ProviderErrorKind = "context_limit" if code == "context_length_exceeded" else "provider_error"
            raise ProviderExecutionError(
                kind=kind,
                provider_name=provider_id,
                model_name=model_name,
                message=_format_error_message(exc, model_name),
                details={"status_code": exc.status_code},
            ) from None
        except (APITimeoutError, APIConnectionError, InternalServerError) as exc:
            status_code = getattr(exc, "status_code", None)
            raise ProviderExecutionError(
                kind="transient_failure",
                provider_name=provider_id,
                model_name=model_name,
                message=_format_error_message(exc, model_name),
                retryable=True,
                details={"status_code": status_code} if status_code else None,
            ) from None
        except (APIStatusError, APIError) as exc:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_id,
                model_name=model_name,
                message=_format_error_message(exc, model_name),
                details={"status_code": getattr(exc, "status_code", None)},
            ) from None
        except Exception as exc:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_id,
                model_name=model_name,
                message=f"Unexpected error: {type(exc).__name__}",
            ) from None

        if not response.choices:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_id,
                model_name=model_name,
                message="Response contained no choices",
            )

        choice = response.choices[0]
        finish_reason_raw = getattr(choice, "finish_reason", None)
        finish_reason = str(finish_reason_raw) if finish_reason_raw is not None else None
        choice_message = choice.message

        refusal = getattr(choice_message, "refusal", None)

        # Strict finish_reason / done_reason mapping: no false completed
        if refusal:
            done_reason: ProviderDoneReason = "content_filter"
        elif finish_reason == "length":
            done_reason = "length"
        elif finish_reason in ("content_filter", "refusal"):
            done_reason = "content_filter"
        elif finish_reason == "tool_calls":
            done_reason = "tool_calls"
        elif finish_reason == "stop":
            done_reason = "stop"
        elif finish_reason == "error":
            done_reason = "error"
        else:
            done_reason = "unknown"

        # Reasoning extraction
        reasoning_raw = getattr(choice_message, "reasoning_content", None)
        if reasoning_raw is None:
            reasoning_raw = getattr(choice_message, "reasoning", None)
        if reasoning_raw is None and isinstance(choice_message, dict):
            reasoning_raw = choice_message.get("reasoning_content") or choice_message.get("reasoning")

        reasoning: str | None = None
        if isinstance(reasoning_raw, str) and reasoning_raw:
            reasoning = reasoning_raw

        # Tool calls extraction and strict validation
        tool_calls_list: list[ToolCall] = []
        raw_tool_calls = getattr(choice_message, "tool_calls", None)
        if raw_tool_calls:
            seen_ids: set[str] = set()
            for tc in raw_tool_calls:
                tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
                if not tc_id or not isinstance(tc_id, str):
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_id,
                        model_name=model_name,
                        message="Tool call missing valid string id",
                    )
                if tc_id in seen_ids:
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_id,
                        model_name=model_name,
                        message=f"Duplicate tool call id: {tc_id}",
                    )
                seen_ids.add(tc_id)

                function_obj = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
                if not function_obj:
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_id,
                        model_name=model_name,
                        message=f"Tool call '{tc_id}' missing function object",
                    )

                fn_name = getattr(function_obj, "name", None) or (function_obj.get("name") if isinstance(function_obj, dict) else None)
                if not fn_name or not isinstance(fn_name, str):
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_id,
                        model_name=model_name,
                        message=f"Tool call '{tc_id}' missing function name",
                    )

                raw_args = getattr(function_obj, "arguments", None)
                try:
                    if not isinstance(raw_args, str):
                        raise ValueError("arguments must be a JSON string")
                    parsed_args = json.loads(raw_args)
                    if not isinstance(parsed_args, dict):
                        raise ValueError("arguments must decode to an object")
                    tool_call = ToolCall(tool_name=fn_name, arguments=parsed_args, tool_call_id=tc_id)
                except ValueError, TypeError:
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_id,
                        model_name=model_name,
                        message=f"Malformed tool call arguments JSON for '{fn_name}'",
                    ) from None
                tool_calls_list.append(tool_call)

        raw_content = getattr(choice_message, "content", None)
        output_text: str | None = None
        if isinstance(raw_content, str):
            output_text = raw_content
        elif raw_content is not None:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_id,
                model_name=model_name,
                message="Response content must be text or null",
            )

        # Token usage preservation: preserve None vs 0
        token_usage: ProviderTokenUsage | None = None
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            input_tokens = getattr(raw_usage, "prompt_tokens", None)
            output_tokens = getattr(raw_usage, "completion_tokens", None)
            prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
            cache_read_tokens = getattr(prompt_details, "cached_tokens", None)
            if cache_read_tokens is None:
                cache_read_tokens = getattr(raw_usage, "prompt_cache_hit_tokens", None)
            uncached_input_tokens = getattr(raw_usage, "prompt_cache_miss_tokens", None)
            for count in (input_tokens, output_tokens, cache_read_tokens, uncached_input_tokens):
                if count is not None and (type(count) is not int or count < 0):
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_id,
                        model_name=model_name,
                        message="Reported token counts must be nonnegative integers or null",
                    )
            token_usage = ProviderTokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                uncached_input_tokens=uncached_input_tokens,
            )

        meta: dict[str, object] = {}
        if finish_reason is not None:
            meta["finish_reason"] = finish_reason
        if refusal:
            meta["refusal"] = refusal

        return ProviderTurnResult(
            tool_calls=tuple(tool_calls_list),
            output=output_text,
            usage=token_usage,
            reasoning=reasoning,
            done_reason=done_reason,
            finish_reason_reported=finish_reason is not None,
            metadata=meta if meta else None,
        )


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    provider = OpenAIChatProvider(config)
    return Plugin(
        tools=(),
        providers=(provider,),
        close=provider.close,
    )
