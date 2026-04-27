from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

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


from ..agent import render_agent_prompt
from ..tools.contracts import ToolCall, ToolDefinition
from ..tools.output import sanitize_tool_arguments, sanitize_tool_result_data
from .config import LiteLLMProviderConfig
from .errors import provider_execution_error_from_api_payload
from .protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
)

_DEFAULT_COMPLETION_TIMEOUT_SECONDS = 300.0


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
            model_name = request.model_name
            if self.config is not None and model_name in self.config.model_map:
                return self.config.model_map[model_name]
            return model_name
        if request.model_name is None:
            raise ProviderExecutionError(
                kind="invalid_model",
                provider_name=self.name,
                model_name="unknown",
                message="provider requires model name",
            )
        if self.config is not None and request.model_name in self.config.model_map:
            model_name = self.config.model_map[request.model_name]
        else:
            model_name = request.model_name
        if self.use_raw_model_name:
            return model_name
        if "/" in model_name:
            return model_name
        return f"{self.name}/{model_name}"

    def _api_base(self) -> str:
        base_url = None if self.config is None else self.config.base_url
        if base_url is None or not base_url.strip():
            return "http://127.0.0.1:4000/v1"
        stripped = base_url.rstrip("/")
        if re.search(r"/v[0-9]+(?:beta|alpha)?$", stripped, re.IGNORECASE):
            return stripped
        return f"{stripped}/v1"

    def _completion_kwargs_for_request(self, request: ProviderTurnRequest) -> dict[str, object]:
        _ = request
        return dict(self.completion_kwargs or {})

    @staticmethod
    def _skill_system_message(request: ProviderTurnRequest) -> str | None:
        if request.skill_prompt_context.strip():
            return request.skill_prompt_context.strip()
        if not request.applied_skills:
            return None

        rendered_skills: list[str] = []
        for skill in request.applied_skills:
            name = skill.get("name", "").strip() or "unnamed-skill"
            description = skill.get("description", "").strip()
            content = skill.get("prompt_context", "").strip() or skill.get("content", "").strip()

            lines = [f"## {name}"]
            if description:
                lines.append(f"Description: {description}")
            if content:
                lines.append(content)
            rendered_skills.append("\n".join(lines))

        if not rendered_skills:
            return None

        return (
            "You must apply the following runtime-managed skills for this turn. "
            "Treat them as active task instructions in addition to the user's request.\n\n"
            + "\n\n".join(rendered_skills)
        )

    @classmethod
    def _agent_profile_system_message(cls, request: ProviderTurnRequest) -> str | None:
        return render_agent_prompt(
            request.agent_preset,
            model_family=cls._model_family_hint(request),
        )

    @staticmethod
    def _model_family_hint(request: ProviderTurnRequest) -> str | None:
        if request.provider_name is not None and request.provider_name.strip():
            return request.provider_name.strip()
        if request.raw_model is None or "/" not in request.raw_model:
            return None
        provider_name, _model_name = request.raw_model.split("/", 1)
        return provider_name.strip() or None

    @staticmethod
    def _continuity_system_message(request: ProviderTurnRequest) -> str | None:
        continuity_state = request.context_window.continuity_state
        if continuity_state is None:
            return None

        summary_text = getattr(continuity_state, "summary_text", None)
        if not isinstance(summary_text, str) or not summary_text.strip():
            return None

        return f"Runtime continuity summary:\n{summary_text.strip()}"

    @staticmethod
    def _tool_result_messages(request: ProviderTurnRequest) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        for index, result in enumerate(request.context_window.tool_results, start=1):
            sanitized_data = sanitize_tool_result_data(result.data)
            raw_tool_call_id = result.data.get("tool_call_id")
            tool_call_id = _normalize_tool_call_id(
                raw_tool_call_id if isinstance(raw_tool_call_id, str) else None,
                fallback=f"voidcode_tool_{index}",
            )
            raw_arguments = result.data.get("arguments")
            arguments_payload = (
                sanitize_tool_arguments(cast(dict[str, object], raw_arguments))
                if isinstance(raw_arguments, dict)
                else {}
            )
            arguments = json.dumps(
                arguments_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            content = result.content or ""
            result_payload = {
                "tool_name": result.tool_name,
                "status": result.status,
                "content": content,
                "error": result.error,
                "data": {
                    key: value
                    for key, value in sanitized_data.items()
                    if key not in {"tool_call_id", "arguments"}
                },
                "truncated": result.truncated,
                "partial": result.partial,
                "reference": result.reference,
            }
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": result.tool_name,
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(
                        result_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            )
        return messages

    @staticmethod
    def _tool_results_user_message(request: ProviderTurnRequest) -> str | None:
        if not request.context_window.tool_results:
            return None

        lines = [
            "The following tool calls have already completed for the current user request.",
            "Use these results as the latest state. Do not repeat a completed tool call "
            "unless a later error explicitly requires retrying it.",
        ]
        for index, result in enumerate(request.context_window.tool_results, start=1):
            sanitized_data = sanitize_tool_result_data(result.data)
            raw_arguments = result.data.get("arguments")
            arguments_payload = (
                sanitize_tool_arguments(cast(dict[str, object], raw_arguments))
                if isinstance(raw_arguments, dict)
                else {}
            )
            content = result.content or ""
            payload = {
                "index": index,
                "tool_name": result.tool_name,
                "arguments": arguments_payload,
                "status": result.status,
                "content": content,
                "error": result.error,
                "data": {
                    key: value
                    for key, value in sanitized_data.items()
                    if key not in {"tool_call_id", "arguments"}
                },
                "truncated": result.truncated,
                "partial": result.partial,
                "reference": result.reference,
            }
            lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)

    def _build_messages(self, request: ProviderTurnRequest) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        agent_profile_message = self._agent_profile_system_message(request)
        if agent_profile_message is not None:
            messages.append({"role": "system", "content": agent_profile_message})
        skill_message = self._skill_system_message(request)
        if skill_message is not None:
            messages.append({"role": "system", "content": skill_message})
        continuity_message = self._continuity_system_message(request)
        if continuity_message is not None:
            messages.append({"role": "system", "content": continuity_message})
        messages.append({"role": "user", "content": request.prompt})
        if request.provider_name == "opencode-go":
            tool_results_message = self._tool_results_user_message(request)
            if tool_results_message is not None:
                messages.append({"role": "user", "content": tool_results_message})
        else:
            messages.extend(self._tool_result_messages(request))
        return messages

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
        payload: dict[str, object] = {
            "model": model_identifier,
            "messages": self._build_messages(request),
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
        payload: dict[str, object] = {
            "model": model_identifier,
            "messages": self._build_messages(request),
            "stream": True,
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
                    reasoning_obj = delta.get("reasoning_content")
                    if isinstance(reasoning_obj, str) and reasoning_obj:
                        yield ProviderStreamEvent(
                            kind="delta", channel="reasoning", text=reasoning_obj
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
                                    kind="delta", channel="reasoning", text=thinking_text
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
