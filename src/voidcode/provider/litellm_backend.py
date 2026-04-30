from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import litellm as litellm_module
    from litellm.exceptions import APIError
else:
    try:
        import litellm as litellm_module
        from litellm.exceptions import APIError
    except ModuleNotFoundError:
        litellm_module = None

        class APIError(Exception):
            pass


from ..tools.contracts import ToolCall, ToolDefinition
from ..tools.output import (
    redacted_argument_keys_for_tool,
    sanitize_tool_arguments,
    sanitize_tool_result_data,
    strip_redaction_sentinels,
)
from .config import LiteLLMProviderConfig
from .errors import provider_execution_error_from_api_payload
from .model_catalog import ToolFeedbackMode, infer_model_metadata
from .protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
)

_DEFAULT_COMPLETION_TIMEOUT_SECONDS = 300.0
type ReasoningEffortMode = Literal["auto", "direct", "glm_thinking", "disabled"]
_DIRECT_REASONING_EFFORT_PROVIDERS = frozenset(
    {"openai", "anthropic", "google", "gemini", "vertex_ai", "litellm", "grok"}
)
_THINKING_DISABLED_EFFORTS = frozenset({"none", "off", "disable", "disabled"})
_SYNTHETIC_TOOL_FEEDBACK_PREFIX = "Completed tool calls for current request:"
_CONTINUITY_SUMMARY_PREFIX = "Runtime continuity summary:"

logger = logging.getLogger(__name__)


def _usage_int(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 0
    value = int(raw)
    return value if value > 0 else 0


def _extract_token_usage(payload: dict[str, object]) -> ProviderTokenUsage | None:
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    usage = cast(dict[str, object], raw_usage)
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details_payload = (
        cast(dict[str, object], prompt_details) if isinstance(prompt_details, dict) else {}
    )
    completion_details = usage.get("completion_tokens_details")
    completion_details_payload = (
        cast(dict[str, object], completion_details) if isinstance(completion_details, dict) else {}
    )
    parsed = ProviderTokenUsage(
        input_tokens=_usage_int(usage.get("prompt_tokens"))
        or _usage_int(usage.get("input_tokens")),
        output_tokens=_usage_int(usage.get("completion_tokens"))
        or _usage_int(usage.get("output_tokens")),
        cache_creation_tokens=_usage_int(usage.get("cache_creation_input_tokens"))
        or _usage_int(prompt_details_payload.get("cache_creation_tokens")),
        cache_read_tokens=_usage_int(usage.get("cache_read_input_tokens"))
        or _usage_int(prompt_details_payload.get("cached_tokens"))
        or _usage_int(completion_details_payload.get("cached_tokens")),
    )
    return parsed if parsed.total_tokens > 0 else None


def _merge_extra_body(kwargs: dict[str, object], extra_body: dict[str, object]) -> None:
    existing = kwargs.get("extra_body")
    merged = dict(cast(dict[str, object], existing)) if isinstance(existing, dict) else {}
    merged.update(extra_body)
    kwargs["extra_body"] = merged


def _allow_openai_param(kwargs: dict[str, object], param: str) -> None:
    existing = kwargs.get("allowed_openai_params")
    params = list(cast(list[object], existing)) if isinstance(existing, list) else []
    if param not in params:
        params.append(param)
    kwargs["allowed_openai_params"] = params


def _message_size_chars(message: dict[str, object]) -> int:
    return len(json.dumps(message, ensure_ascii=False, sort_keys=True, default=str))


def _empty_tool_feedback_model_overrides() -> dict[str, ToolFeedbackMode]:
    return {}


def _is_object_json_schema(schema: dict[str, object]) -> bool:
    schema_type = schema.get("type")
    if schema_type == "object":
        return True
    if schema_type is not None:
        return False
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return True
    return any(
        key in schema
        for key in (
            "$defs",
            "$schema",
            "additionalProperties",
            "allOf",
            "anyOf",
            "dependentRequired",
            "dependentSchemas",
            "maxProperties",
            "minProperties",
            "oneOf",
            "patternProperties",
            "propertyNames",
            "required",
            "unevaluatedProperties",
        )
    )


def _normalize_tool_call_id(value: str | None, *, fallback: str) -> str:
    raw = value if value is not None and value.strip() else fallback
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())
    return normalized or fallback


@dataclass(frozen=True, slots=True)
class _StreamedToolCallAccumulator:
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class LiteLLMBackendSingleAgentProvider:
    name: str
    config: LiteLLMProviderConfig | None
    completion_kwargs: dict[str, object] | None = None
    use_raw_model_name: bool = False
    reasoning_effort_mode: ReasoningEffortMode = "auto"
    tool_feedback_model_overrides: Mapping[str, ToolFeedbackMode] = field(
        default_factory=_empty_tool_feedback_model_overrides
    )

    @staticmethod
    def _to_tool_schema(tool: ToolDefinition) -> dict[str, object]:
        input_schema = tool.input_schema or {}
        parameters: dict[str, object]
        if _is_object_json_schema(input_schema):
            parameters = dict(input_schema)
            parameters.setdefault("type", "object")
        else:
            parameters = {
                "type": "object",
                "properties": input_schema,
                "additionalProperties": True,
            }
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": parameters,
            },
        }

    def _model_identifier(self, request: ProviderTurnRequest) -> str:
        if request.provider_name == "litellm":
            if request.model_name is None:
                raise ProviderExecutionError(
                    kind="invalid_model",
                    provider_name=self.name,
                    model_name="unknown",
                    message="litellm provider requires model name",
                )
            return self._mapped_model_name_for_request(request)
        if request.model_name is None:
            raise ProviderExecutionError(
                kind="invalid_model",
                provider_name=self.name,
                model_name="unknown",
                message="provider requires model name",
            )
        model_name = self._mapped_model_name_for_request(request)
        if self.use_raw_model_name:
            return model_name
        if "/" in model_name:
            return model_name
        return f"{self.name}/{model_name}"

    def _mapped_model_name_for_request(self, request: ProviderTurnRequest) -> str:
        if request.model_name is None:
            return ""
        if self.config is not None and request.model_name in self.config.model_map:
            return self.config.model_map[request.model_name]
        return request.model_name

    def _api_base(self) -> str:
        base_url = None if self.config is None else self.config.base_url
        if base_url is None or not base_url.strip():
            return "http://127.0.0.1:4000/v1"
        stripped = base_url.rstrip("/")
        if re.search(r"/v[0-9]+(?:beta|alpha)?$", stripped, re.IGNORECASE):
            return stripped
        return f"{stripped}/v1"

    def _completion_kwargs_for_request(self, request: ProviderTurnRequest) -> dict[str, object]:
        kwargs = dict(self.completion_kwargs or {})
        if not request.reasoning_effort:
            return kwargs

        effort = request.reasoning_effort.strip()
        if not effort:
            return kwargs

        mode = self.reasoning_effort_mode
        provider_name = (request.provider_name or self.name).lower()
        model_name = (request.model_name or "").lower()
        if mode == "disabled":
            return kwargs
        if mode == "glm_thinking" or (
            mode == "auto"
            and (provider_name == "glm" or model_name.startswith(("glm-5", "glm-z1")))
        ):
            thinking_type = (
                "disabled" if effort.lower() in _THINKING_DISABLED_EFFORTS else "enabled"
            )
            _merge_extra_body(kwargs, {"thinking": {"type": thinking_type}})
            _allow_openai_param(kwargs, "extra_body")
            return kwargs
        if mode == "direct" or (
            mode == "auto" and provider_name in _DIRECT_REASONING_EFFORT_PROVIDERS
        ):
            kwargs["reasoning_effort"] = request.reasoning_effort
        return kwargs

    def _stream_completion_kwargs_for_request(
        self, request: ProviderTurnRequest
    ) -> dict[str, object]:
        return self._completion_kwargs_for_request(request)

    def _tool_feedback_mode_for_request(self, request: ProviderTurnRequest) -> ToolFeedbackMode:
        request_model_name = request.model_name
        mapped_model_name = self._mapped_model_name_for_request(request)
        mode = self.tool_feedback_model_overrides.get(mapped_model_name)
        if mode is None and request_model_name is not None:
            mode = self.tool_feedback_model_overrides.get(request_model_name)
        if mode is not None:
            return mode
        metadata_mode = (
            None if request.model_metadata is None else request.model_metadata.tool_feedback_mode
        )
        if metadata_mode is not None:
            return metadata_mode
        provider_name = request.provider_name or self.name
        if mapped_model_name:
            inferred = infer_model_metadata(provider_name, mapped_model_name)
            if inferred is not None and inferred.tool_feedback_mode is not None:
                return inferred.tool_feedback_mode
        return "standard"

    @staticmethod
    def _provider_visible_arguments(
        tool_name: str | None,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        sanitized = sanitize_tool_arguments(arguments)
        stripped = strip_redaction_sentinels(
            sanitized,
            redacted_keys=redacted_argument_keys_for_tool(tool_name),
        )
        return cast(dict[str, object], stripped) if isinstance(stripped, dict) else {}

    def _build_messages(self, request: ProviderTurnRequest) -> list[dict[str, object]]:
        assembled_context = request.assembled_context
        messages: list[dict[str, object]] = []
        if self._tool_feedback_mode_for_request(request) == "synthetic_user_message":
            tool_feedback_lines: list[str] = []
            for segment in assembled_context.segments:
                if segment.role == "tool":
                    metadata = segment.metadata or {}
                    raw_data = metadata.get("data")
                    sanitized_data = (
                        sanitize_tool_result_data(cast(dict[str, object], raw_data))
                        if isinstance(raw_data, dict)
                        else {}
                    )
                    raw_arguments = sanitized_data.get("arguments")
                    sanitized_arguments = (
                        self._provider_visible_arguments(
                            segment.tool_name,
                            cast(dict[str, object], raw_arguments),
                        )
                        if isinstance(raw_arguments, dict)
                        else {}
                    )
                    payload = {
                        "tool_name": segment.tool_name,
                        "arguments": sanitized_arguments,
                        "status": metadata.get("status"),
                        "content": segment.content or "",
                        "error": metadata.get("error"),
                        "data": {
                            key: value
                            for key, value in sanitized_data.items()
                            if key not in {"tool_call_id", "arguments"}
                        },
                        "truncated": metadata.get("truncated"),
                        "partial": metadata.get("partial"),
                        "reference": metadata.get("reference"),
                    }
                    tool_feedback_lines.append(
                        json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    )
                elif segment.role != "assistant":
                    messages.append({"role": segment.role, "content": segment.content})
            if tool_feedback_lines:
                intro_line_1 = "Completed tool calls for current request:"
                intro_line_2 = (
                    "Use these results as latest state. "
                    "Do not repeat completed calls unless retry is required."
                )
                messages.append(
                    {
                        "role": "user",
                        "content": "\n".join(
                            (
                                intro_line_1,
                                intro_line_2,
                                *tool_feedback_lines,
                            )
                        ),
                    }
                )
            return messages

        for segment in assembled_context.segments:
            if segment.role == "assistant" and segment.tool_name is not None:
                tool_call_id = _normalize_tool_call_id(
                    segment.tool_call_id,
                    fallback=segment.tool_name,
                )
                sanitized_arguments = self._provider_visible_arguments(
                    segment.tool_name,
                    segment.tool_arguments or {},
                )
                arguments = json.dumps(
                    sanitized_arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": segment.content,
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": segment.tool_name,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    }
                )
                continue
            if segment.role == "tool":
                metadata = segment.metadata or {}
                raw_data = metadata.get("data")
                sanitized_data = (
                    sanitize_tool_result_data(cast(dict[str, object], raw_data))
                    if isinstance(raw_data, dict)
                    else {}
                )
                payload = {
                    "tool_name": segment.tool_name,
                    "content": segment.content or "",
                    "status": metadata.get("status"),
                    "error": metadata.get("error"),
                    "data": {
                        key: value
                        for key, value in sanitized_data.items()
                        if key not in {"tool_call_id", "arguments"}
                    },
                    "truncated": metadata.get("truncated"),
                    "partial": metadata.get("partial"),
                    "reference": metadata.get("reference"),
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": _normalize_tool_call_id(
                            segment.tool_call_id,
                            fallback=segment.tool_name or "voidcode_tool",
                        ),
                        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    }
                )
                continue
            messages.append({"role": segment.role, "content": segment.content})
        return messages

    @staticmethod
    def _provider_request_diagnostics(
        *,
        messages: list[dict[str, object]],
        request: ProviderTurnRequest,
    ) -> dict[str, object]:
        message_sizes = [_message_size_chars(message) for message in messages]
        largest_index = (
            max(range(len(message_sizes)), key=message_sizes.__getitem__) if messages else None
        )
        largest_message: dict[str, object] | None = None
        if largest_index is not None:
            largest = messages[largest_index]
            largest_message = {
                "index": largest_index,
                "role": largest.get("role"),
                "size_chars": message_sizes[largest_index],
            }
            content = largest.get("content")
            if isinstance(content, str):
                if content.startswith(_SYNTHETIC_TOOL_FEEDBACK_PREFIX):
                    largest_message["source"] = "synthetic_tool_feedback"
                elif content.startswith(_CONTINUITY_SUMMARY_PREFIX):
                    largest_message["source"] = "continuity_summary"

        synthetic_tool_feedback_size = 0
        continuity_summary_size = 0
        for message in messages:
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if content.startswith(_SYNTHETIC_TOOL_FEEDBACK_PREFIX):
                synthetic_tool_feedback_size += len(content)
            if content.startswith(_CONTINUITY_SUMMARY_PREFIX):
                continuity_summary_size += len(content)

        context_window = request.context_window
        return {
            "message_count": len(messages),
            "estimated_chars": sum(message_sizes),
            "largest_message": largest_message,
            "retained_tool_result_count": context_window.retained_tool_result_count,
            "synthetic_tool_feedback_size_chars": synthetic_tool_feedback_size,
            "continuity_summary_size_chars": continuity_summary_size,
            "compacted": context_window.compacted,
        }

    def _log_provider_request_diagnostics(
        self,
        *,
        messages: list[dict[str, object]],
        request: ProviderTurnRequest,
    ) -> None:
        diagnostics = self._provider_request_diagnostics(messages=messages, request=request)
        logger.debug(
            "provider request diagnostics: provider=%s model=%s messages=%d "
            "estimated_chars=%d retained_tool_results=%d synthetic_tool_feedback_size=%d "
            "continuity_summary_size=%d largest_message=%s compacted=%s",
            request.provider_name or self.name,
            request.model_name or "unknown",
            diagnostics["message_count"],
            diagnostics["estimated_chars"],
            diagnostics["retained_tool_result_count"],
            diagnostics["synthetic_tool_feedback_size_chars"],
            diagnostics["continuity_summary_size_chars"],
            diagnostics["largest_message"],
            diagnostics["compacted"],
        )

    def _auth_kwargs(self) -> dict[str, object]:
        if self.config is None:
            return {}
        if self.config.auth_scheme == "none" or self.config.api_key is None:
            return {}
        if self.config.auth_scheme == "token":
            header_name = self.config.auth_header or "Authorization"
            return {"extra_headers": {header_name: self.config.api_key}}
        if self.config.auth_header is not None and self.config.auth_header != "Authorization":
            return {"extra_headers": {self.config.auth_header: f"Bearer {self.config.api_key}"}}
        return {"api_key": self.config.api_key}

    @staticmethod
    def _extract_first_tool_call(message: dict[str, object]) -> ToolCall | None:
        raw_tool_calls = message.get("tool_calls")
        if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
            return None
        tool_calls = cast(list[object], raw_tool_calls)
        first_tool_call_obj = tool_calls[0]
        if not isinstance(first_tool_call_obj, dict):
            return None
        first_tool_call = cast(dict[str, object], first_tool_call_obj)
        function_obj = first_tool_call.get("function")
        if not isinstance(function_obj, dict):
            return None
        function = cast(dict[str, object], function_obj)
        tool_name_obj = function.get("name")
        if not isinstance(tool_name_obj, str) or not tool_name_obj:
            return None
        parsed_arguments: dict[str, object] = {}
        arguments_obj = function.get("arguments")
        if isinstance(arguments_obj, str) and arguments_obj.strip():
            try:
                decoded = json.loads(arguments_obj)
            except json.JSONDecodeError:
                parsed_arguments = {}
            else:
                if isinstance(decoded, dict):
                    parsed_arguments = cast(dict[str, object], decoded)
        tool_call_id_obj = first_tool_call.get("id")
        tool_call_id = _normalize_tool_call_id(
            tool_call_id_obj if isinstance(tool_call_id_obj, str) else None,
            fallback=tool_name_obj,
        )
        return ToolCall(
            tool_name=tool_name_obj,
            arguments=parsed_arguments,
            tool_call_id=tool_call_id,
        )

    @staticmethod
    def _map_exception(
        exc: Exception, *, provider_name: str, model_name: str
    ) -> ProviderExecutionError:
        if isinstance(exc, ProviderExecutionError):
            return exc
        if isinstance(exc, APIError):
            payload: dict[str, object] = {
                "message": str(exc),
                "status_code": getattr(exc, "status_code", None),
                "code": getattr(exc, "code", None),
            }
            return provider_execution_error_from_api_payload(
                provider_name=provider_name,
                model_name=model_name,
                payload=payload,
            )
        return ProviderExecutionError(
            kind="transient_failure",
            provider_name=provider_name,
            model_name=model_name,
            message=str(exc),
            retryable=True,
            details={
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        )

    @staticmethod
    def _call_litellm_completion(payload: dict[str, Any]) -> Any:
        if litellm_module is None:
            raise ProviderExecutionError(
                kind="transient_failure",
                provider_name="litellm",
                model_name="unknown",
                message="litellm dependency is not installed",
            )
        module_any = cast(Any, litellm_module)
        return module_any.completion(**payload)

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        model_identifier = self._model_identifier(request)
        timeout_seconds = (
            _DEFAULT_COMPLETION_TIMEOUT_SECONDS
            if self.config is None or self.config.timeout_seconds is None
            else self.config.timeout_seconds
        )
        messages = self._build_messages(request)
        self._log_provider_request_diagnostics(messages=messages, request=request)
        payload: dict[str, object] = {
            "model": model_identifier,
            "messages": messages,
            "stream": False,
            "api_base": self._api_base(),
            "timeout": timeout_seconds,
            "num_retries": 0,
            **self._auth_kwargs(),
        }
        payload.update(self._completion_kwargs_for_request(request))
        if request.available_tools:
            payload["tools"] = [self._to_tool_schema(tool) for tool in request.available_tools]
            payload["tool_choice"] = "auto"

        try:
            response = self._call_litellm_completion(cast(dict[str, Any], payload))
            response_payload = cast(dict[str, object], response.model_dump())
            usage = _extract_token_usage(response_payload)
            raw_choices = response_payload.get("choices")
            if not isinstance(raw_choices, list) or not raw_choices:
                return ProviderTurnResult(output="", usage=usage)
            choices = cast(list[object], raw_choices)
            first_choice_obj = choices[0]
            if not isinstance(first_choice_obj, dict):
                return ProviderTurnResult(output="", usage=usage)
            message_obj = cast(dict[str, object], first_choice_obj).get("message")
            if not isinstance(message_obj, dict):
                return ProviderTurnResult(output="", usage=usage)
            message = cast(dict[str, object], message_obj)

            tool_call = self._extract_first_tool_call(message)
            if tool_call is not None:
                return ProviderTurnResult(tool_call=tool_call, usage=usage)

            content_obj = message.get("content")
            if isinstance(content_obj, str):
                return ProviderTurnResult(output=content_obj, usage=usage)
            return ProviderTurnResult(output="", usage=usage)
        except Exception as exc:
            raise self._map_exception(
                exc,
                provider_name=self.name,
                model_name=request.model_name or "unknown",
            ) from exc

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        model_identifier = self._model_identifier(request)
        timeout_seconds = (
            _DEFAULT_COMPLETION_TIMEOUT_SECONDS
            if self.config is None or self.config.timeout_seconds is None
            else self.config.timeout_seconds
        )
        messages = self._build_messages(request)
        self._log_provider_request_diagnostics(messages=messages, request=request)
        payload: dict[str, object] = {
            "model": model_identifier,
            "messages": messages,
            "stream": True,
            "api_base": self._api_base(),
            "timeout": timeout_seconds,
            "num_retries": 0,
            **self._auth_kwargs(),
        }
        payload.update(self._stream_completion_kwargs_for_request(request))
        if request.available_tools:
            payload["tools"] = [self._to_tool_schema(tool) for tool in request.available_tools]
            payload["tool_choice"] = "auto"

        try:
            stream = cast(
                Iterator[Any], self._call_litellm_completion(cast(dict[str, Any], payload))
            )
            streamed_tool_calls: dict[int, _StreamedToolCallAccumulator] = {}
            latest_usage: ProviderTokenUsage | None = None
            for chunk in stream:
                if request.abort_signal is not None and request.abort_signal.cancelled:
                    yield ProviderStreamEvent(
                        kind="error",
                        channel="error",
                        error="provider stream cancelled",
                        error_kind="cancelled",
                    )
                    yield ProviderStreamEvent(kind="done", done_reason="cancelled")
                    return
                chunk_payload = cast(dict[str, object], chunk.model_dump())
                latest_usage = _extract_token_usage(chunk_payload) or latest_usage
                raw_choices = chunk_payload.get("choices")
                if not isinstance(raw_choices, list) or not raw_choices:
                    continue
                choices = cast(list[object], raw_choices)
                first_choice_obj = choices[0]
                if not isinstance(first_choice_obj, dict):
                    continue
                first_choice = cast(dict[str, object], first_choice_obj)
                delta_obj = first_choice.get("delta")
                if isinstance(delta_obj, dict):
                    delta = cast(dict[str, object], delta_obj)
                    reasoning_obj = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(reasoning_obj, str) and reasoning_obj:
                        yield ProviderStreamEvent(
                            kind="delta",
                            channel="reasoning",
                            text=reasoning_obj,
                            metadata={"source": "delta.reasoning"},
                        )
                    raw_thinking_blocks = delta.get("thinking_blocks")
                    if isinstance(raw_thinking_blocks, list):
                        thinking_blocks = cast(list[object], raw_thinking_blocks)
                        for block_obj in thinking_blocks:
                            if not isinstance(block_obj, dict):
                                continue
                            block = cast(dict[str, object], block_obj)
                            if block.get("type") != "thinking":
                                continue
                            thinking_text = block.get("thinking")
                            if isinstance(thinking_text, str) and thinking_text:
                                yield ProviderStreamEvent(
                                    kind="delta",
                                    channel="reasoning",
                                    text=thinking_text,
                                    metadata={"source": "delta.thinking_blocks"},
                                )
                    text_obj = delta.get("content")
                    if isinstance(text_obj, str) and text_obj:
                        yield ProviderStreamEvent(kind="delta", channel="text", text=text_obj)
                    raw_tool_calls = delta.get("tool_calls")
                    if isinstance(raw_tool_calls, list):
                        tool_calls = cast(list[object], raw_tool_calls)
                        for tool_call_obj in tool_calls:
                            if not isinstance(tool_call_obj, dict):
                                continue
                            tool_call = cast(dict[str, object], tool_call_obj)
                            index_obj = tool_call.get("index")
                            index = index_obj if isinstance(index_obj, int) else 0
                            accumulator = streamed_tool_calls.get(
                                index,
                                _StreamedToolCallAccumulator(),
                            )
                            tool_call_id_obj = tool_call.get("id")
                            tool_call_id = accumulator.tool_call_id
                            if isinstance(tool_call_id_obj, str) and tool_call_id_obj:
                                tool_call_id = tool_call_id_obj
                            function_obj = tool_call.get("function")
                            tool_name = accumulator.tool_name
                            arguments = accumulator.arguments
                            if isinstance(function_obj, dict):
                                function = cast(dict[str, object], function_obj)
                                name_obj = function.get("name")
                                if isinstance(name_obj, str) and name_obj:
                                    tool_name = name_obj
                                arguments_obj = function.get("arguments")
                                if isinstance(arguments_obj, str) and arguments_obj:
                                    arguments += arguments_obj
                            streamed_tool_calls[index] = _StreamedToolCallAccumulator(
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                arguments=arguments,
                            )
                finish_reason = first_choice.get("finish_reason")
                if isinstance(finish_reason, str) and finish_reason:
                    continue
        except Exception as exc:
            raise self._map_exception(
                exc,
                provider_name=self.name,
                model_name=request.model_name or "unknown",
            ) from exc

        completed_tool_calls = [
            (index, accumulator)
            for index, accumulator in sorted(streamed_tool_calls.items())
            if accumulator.tool_name is not None
        ]
        if completed_tool_calls:
            _index, selected_tool = completed_tool_calls[0]
            tool_payload = self._extract_first_tool_call(
                {
                    "tool_calls": [
                        {
                            "id": selected_tool.tool_call_id,
                            "function": {
                                "name": selected_tool.tool_name,
                                "arguments": selected_tool.arguments,
                            },
                        }
                    ]
                }
            )
            if tool_payload is not None:
                event_payload: dict[str, object] = {
                    "tool_name": tool_payload.tool_name,
                    "arguments": tool_payload.arguments,
                }
                if tool_payload.tool_call_id is not None:
                    event_payload["tool_call_id"] = tool_payload.tool_call_id
                yield ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text=json.dumps(event_payload),
                )

        yield ProviderStreamEvent(
            kind="done",
            done_reason="completed",
            usage=latest_usage,
        )
