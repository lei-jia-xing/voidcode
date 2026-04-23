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


from ..tools.contracts import ToolCall, ToolDefinition
from .config import LiteLLMProviderConfig
from .errors import provider_execution_error_from_api_payload
from .protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    SingleAgentTurnRequest,
    SingleAgentTurnResult,
)


@dataclass(frozen=True, slots=True)
class LiteLLMBackendSingleAgentProvider:
    name: str
    config: LiteLLMProviderConfig | None
    completion_kwargs: dict[str, object] | None = None
    use_raw_model_name: bool = False

    _LEADER_PROFILE_PROMPT = (
        "You are the VoidCode leader agent, the primary user-facing runtime agent. "
        "Understand the user's intent, choose the smallest safe tool-backed step, "
        "respect the runtime-provided tool boundary, and report concise progress and "
        "results. This is still a single-agent backend path; do not assume multi-agent "
        "delegation exists unless the runtime exposes it explicitly."
    )

    @staticmethod
    def _to_tool_schema(tool: ToolDefinition) -> dict[str, object]:
        parameters: dict[str, object] = tool.input_schema or {
            "type": "object",
            "properties": {},
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

    def _model_identifier(self, request: SingleAgentTurnRequest) -> str:
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

    def _completion_kwargs_for_request(self, request: SingleAgentTurnRequest) -> dict[str, object]:
        _ = request
        return dict(self.completion_kwargs or {})

    @staticmethod
    def _skill_system_message(request: SingleAgentTurnRequest) -> str | None:
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
    def _agent_profile_system_message(cls, request: SingleAgentTurnRequest) -> str | None:
        agent_preset = request.agent_preset
        if agent_preset is None:
            return None
        prompt_profile = agent_preset.get("prompt_profile")
        if not isinstance(prompt_profile, str) or not prompt_profile.strip():
            return None
        normalized_prompt_profile = prompt_profile.strip()
        if normalized_prompt_profile == "leader":
            return cls._LEADER_PROFILE_PROMPT
        return (
            "Runtime-selected VoidCode agent prompt profile: "
            f"{normalized_prompt_profile}. Treat this as the active agent role profile "
            "for this single-agent turn while still following the runtime-provided tool "
            "and skill boundaries."
        )

    @staticmethod
    def _continuity_system_message(request: SingleAgentTurnRequest) -> str | None:
        continuity_state = request.context_window.continuity_state
        if continuity_state is None:
            return None

        summary_text = getattr(continuity_state, "summary_text", None)
        if not isinstance(summary_text, str) or not summary_text.strip():
            return None

        return f"Runtime continuity summary:\n{summary_text.strip()}"

    def _build_messages(self, request: SingleAgentTurnRequest) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
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
        function_obj = cast(dict[str, object], first_tool_call_obj).get("function")
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
        return ToolCall(tool_name=tool_name_obj, arguments=parsed_arguments)

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

    def propose_turn(self, request: SingleAgentTurnRequest) -> SingleAgentTurnResult:
        model_identifier = self._model_identifier(request)
        timeout_seconds = (
            30.0
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
            raw_choices = response_payload.get("choices")
            if not isinstance(raw_choices, list) or not raw_choices:
                return SingleAgentTurnResult(output="")
            choices = cast(list[object], raw_choices)
            first_choice_obj = choices[0]
            if not isinstance(first_choice_obj, dict):
                return SingleAgentTurnResult(output="")
            message_obj = cast(dict[str, object], first_choice_obj).get("message")
            if not isinstance(message_obj, dict):
                return SingleAgentTurnResult(output="")
            message = cast(dict[str, object], message_obj)

            tool_call = self._extract_first_tool_call(message)
            if tool_call is not None:
                return SingleAgentTurnResult(tool_call=tool_call)

            content_obj = message.get("content")
            if isinstance(content_obj, str):
                return SingleAgentTurnResult(output=content_obj)
            return SingleAgentTurnResult(output="")
        except Exception as exc:
            raise self._map_exception(
                exc,
                provider_name=self.name,
                model_name=request.model_name or "unknown",
            ) from exc

    def stream_turn(self, request: SingleAgentTurnRequest) -> Iterator[ProviderStreamEvent]:
        model_identifier = self._model_identifier(request)
        timeout_seconds = (
            30.0
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
            streamed_tool_name: str | None = None
            streamed_tool_arguments = ""
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
                            function_obj = tool_call.get("function")
                            if not isinstance(function_obj, dict):
                                continue
                            function = cast(dict[str, object], function_obj)
                            name_obj = function.get("name")
                            if isinstance(name_obj, str) and name_obj:
                                streamed_tool_name = name_obj
                            arguments_obj = function.get("arguments")
                            if isinstance(arguments_obj, str) and arguments_obj:
                                streamed_tool_arguments += arguments_obj
                        if streamed_tool_name is not None:
                            tool_payload = self._extract_first_tool_call(
                                {
                                    "tool_calls": [
                                        {
                                            "function": {
                                                "name": streamed_tool_name,
                                                "arguments": streamed_tool_arguments,
                                            }
                                        }
                                    ]
                                }
                            )
                            if tool_payload is not None:
                                yield ProviderStreamEvent(
                                    kind="content",
                                    channel="tool",
                                    text=json.dumps(
                                        {
                                            "tool_name": tool_payload.tool_name,
                                            "arguments": tool_payload.arguments,
                                        }
                                    ),
                                )
                finish_reason = first_choice.get("finish_reason")
                if isinstance(finish_reason, str) and finish_reason:
                    yield ProviderStreamEvent(kind="done", done_reason="completed")
                    return
        except Exception as exc:
            raise self._map_exception(
                exc,
                provider_name=self.name,
                model_name=request.model_name or "unknown",
            ) from exc

        yield ProviderStreamEvent(kind="done", done_reason="completed")
