from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError as GoogleAPIError
from google.oauth2 import credentials as oauth_credentials
from google.oauth2 import service_account

from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import (
    ModelContextLimits,
    ProviderDoneReason,
    ProviderEffortLevel,
    ProviderExecutionError,
    ProviderMessage,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from voidcode.sdk.tools import ToolCall

from ._wire_common import (
    OwnedResource,
    ProviderErrorSource,
    abort_signal_cancelled,
    build_usage,
    cancelled_events,
    fallback_execution_error,
    iter_stream_with_timeout,
    map_done_reason,
    normalize_tool_call_id,
    resolve_extra_request_headers,
    usage_int,
)
from .config import GoogleProviderConfig
from .errors import provider_execution_error_from_payload
from .model_catalog import catalog_model_limits, static_catalog_metadata
from .reasoning_effort import (
    clamp_effort_to_supported,
    normalize_reasoning_effort,
)
from .thinking_rules import thinking_rule_for

# Gemini thinking is configured through ``types.ThinkingConfig``. The installed
# SDK types document their own conventions: ``thinking_budget`` accepts ``0``
# (disabled) and ``-1`` (automatic) and states that every other value and
# allowed range is model dependent, while ``thinking_level`` is the Gemini 3
# enum ``MINIMAL``/``LOW``/``MEDIUM``/``HIGH``. Which of the two a model reads is
# a data row (``thinking_rules.json``: ``google-level`` vs ``budget``), and the
# budgets come from that row.
_GOOGLE_THINKING_LEVELS: dict[ProviderEffortLevel, types.ThinkingLevel] = {
    # ``mapEffortToGoogleThinkingLevel`` (``model-thinking.ts:85-110``), values
    # identical to OMP's mapper including the ``xhigh``/``max`` -> HIGH clamp.
    "minimal": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
    "xhigh": types.ThinkingLevel.HIGH,
    "max": types.ThinkingLevel.HIGH,
}
# Service account credentials carry no scope of their own; the Vertex AI /
# Gemini enterprise endpoints authorize with the cloud-platform scope.
_GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# The loader's own exceptions embed the configured file path and sometimes key
# material, so the provider-level message is fixed text that names only the
# config field a user can fix.
_SERVICE_ACCOUNT_LOAD_FAILURE_MESSAGE = (
    "google provider auth service account credentials could not be loaded; check the configured 'service_account_json_path' file"
)


def _service_account_credentials(path: str | None, *, provider_name: str, model_name: str) -> Any:
    """Load the configured service account file or raise a non-retryable auth error."""
    if not path:
        raise ProviderExecutionError(
            kind="missing_auth",
            provider_name=provider_name,
            model_name=model_name,
            message=_SERVICE_ACCOUNT_LOAD_FAILURE_MESSAGE,
            retryable=False,
            details={"reason": "missing_service_account_json_path"},
        )
    try:
        return service_account.Credentials.from_service_account_file(path, scopes=[_GOOGLE_CLOUD_PLATFORM_SCOPE])
    except Exception as exc:
        raise ProviderExecutionError(
            kind="missing_auth",
            provider_name=provider_name,
            model_name=model_name,
            message=_SERVICE_ACCOUNT_LOAD_FAILURE_MESSAGE,
            retryable=False,
            details={"reason": "unreadable_or_invalid_service_account_file", "exception_type": type(exc).__name__},
        ) from exc


def _service_account_project_id(credentials: Any) -> str | None:
    """Return the project named by the loaded credentials, when they expose one."""
    project_id = getattr(credentials, "project_id", None)
    return project_id if isinstance(project_id, str) and project_id else None


class GoogleWireCodec:
    """Google wire knowledge: usage attributes, finish tokens, reported checks."""

    DONE_REASONS: ClassVar[dict[str, ProviderDoneReason]] = {
        "stop": "stop",
        "end_turn": "stop",
        "max_tokens": "length",
        "length": "length",
        "safety": "content_filter",
        "recitation": "content_filter",
        "blocklist": "content_filter",
        "prohibited_content": "content_filter",
        "spii": "content_filter",
        "other": "content_filter",
        "malformed_function_call": "error",
        "unexpected_tool_call": "error",
        "no_image": "error",
    }

    @staticmethod
    def usage(response: object) -> ProviderTokenUsage | None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None
        input_tokens = usage_int(getattr(usage, "prompt_token_count", None))
        candidates = usage_int(getattr(usage, "candidates_token_count", None))
        thoughts = usage_int(getattr(usage, "thoughts_token_count", None))
        # Google reports thinking separately; an incomplete output total stays unknown.
        output_tokens = candidates + thoughts if candidates is not None and thoughts is not None else None
        cache_read = usage_int(getattr(usage, "cached_content_token_count", None))
        # ``prompt_token_count`` is the inclusive prompt total (``cached_content_token_count``
        # is a component of it), so it is stored as-is and the uncached bucket is the
        # remainder: without it every Google turn would price its input at zero (the
        # provider reports no separate uncached count).
        uncached = input_tokens - cache_read if input_tokens is not None and cache_read is not None and cache_read <= input_tokens else None
        return build_usage(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read, uncached_input_tokens=uncached)

    @staticmethod
    def done_reason(value: object) -> ProviderDoneReason:
        return map_done_reason(str(getattr(value, "name", value)).split(".")[-1], GoogleWireCodec.DONE_REASONS)

    @staticmethod
    def finish_reason_reported(value: object) -> bool:
        raw = getattr(value, "name", value)
        token = str(raw).split(".")[-1].strip().upper()
        return value is not None and bool(token) and token not in {"0", "FINISH_REASON_UNSPECIFIED"}


@dataclass(frozen=True, slots=True)
class GoogleGenAIProvider:
    name: str = "google"
    config: GoogleProviderConfig | None = None
    client: Any | None = None
    auth_header: str | None = None
    # Headers this gateway requires on every request it serves. They reach the
    # wire through the request config's ``http_options``: the SDK patches them
    # onto the client's own options per request, so they merge with (never
    # replace) the client's ``Content-Type``/credential headers, and the request
    # body is unaffected because the SDK pops ``config`` from it.
    extra_request_headers: Mapping[str, str] = field(default_factory=dict)
    # One client -- and therefore one HTTP connection pool -- per provider,
    # reused across every turn. A client owns no conversation state: the headers
    # above are patched per request, so reusing it is correct rather than merely
    # cheaper. First-use initialization is serialized.
    _owned_client: OwnedResource[Any] = field(default_factory=OwnedResource, compare=False, repr=False)

    def close(self) -> None:
        self._owned_client.close()

    def model_limits(self, model_name: str) -> ModelContextLimits | None:
        """Advertised limits for one model; gateway aliases resolve before dispatch."""
        if not isinstance(model_name, str) or not model_name.strip():
            return None
        return catalog_model_limits(self.name, model_name)

    def _client(self, *, provider_name: str, model_name: str) -> Any:
        if self.client is not None:
            return self.client
        owned = self._owned_client
        with owned.lock:
            if owned.value is None:
                owned.value = self._create_client(provider_name=provider_name, model_name=model_name)
            return owned.value

    def _create_client(self, *, provider_name: str, model_name: str) -> Any:
        config = self.config
        auth = None if config is None else config.auth
        if auth is None:
            # Credentials are resolved once at admission and recorded in the
            # private snapshot. Reading GOOGLE_API_KEY or letting the SDK use
            # ambient ADC here would authenticate from state the revision never
            # captured, so this fails visibly instead.
            raise ProviderExecutionError(
                kind="missing_auth",
                provider_name=provider_name,
                model_name=model_name,
                message="google provider requires configured auth: an API key, OAuth access token, or service-account file",
                retryable=False,
            )
        credentials: Any = None
        api_key: str | None = None
        if auth.method == "service_account":
            credentials = _service_account_credentials(auth.service_account_json_path, provider_name=provider_name, model_name=model_name)
        elif auth.method == "oauth":
            if config is None or not config.project:
                raise ProviderExecutionError(
                    kind="not_configured",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="Google OAuth requires providers.google.project for Vertex AI credentials",
                    retryable=False,
                )
            credentials = oauth_credentials.Credentials(token=auth.access_token)
        else:
            api_key = auth.api_key
            if not api_key:
                raise ProviderExecutionError(
                    kind="not_configured",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="Configured API-key authentication requires an API key",
                    retryable=False,
                )
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if credentials is not None:
            # ``credentials`` and ``api_key`` are mutually exclusive in the SDK.
            kwargs["credentials"] = credentials
        base_url = None if config is None else config.base_url
        # A configured endpoint replaces every SDK default (the SDK's own
        # ``_base_url`` resolution gives ``http_options.base_url`` precedence
        # over its host defaults, and its client skips the ADC project lookup
        # once one is set). The SDK would then append its own version segment
        # -- ``v1beta`` or ``v1beta1`` -- to a URL that already names one, so
        # the version is cleared: a configured base URL is a complete
        # endpoint root and the SDK appends only the resource path.
        kwargs["http_options"] = types.HttpOptions(
            base_url=base_url or None,
            headers={self.auth_header: f"Bearer {api_key}"} if self.auth_header and api_key else None,
            api_version="" if base_url else None,
            timeout=max(1, int(self._timeout() * 1000)),
        )
        project = None if config is None else config.project
        location = None if config is None else config.region
        if credentials is not None:
            # The SDK resolves an unset project through its own ADC lookup, which
            # fails for users who configured only a service account file.
            if project is None:
                project = _service_account_project_id(credentials)
            # Explicit credentials always select the Vertex AI surface.
            kwargs.update(vertexai=True, project=project, location=location)
        elif project is not None and location is not None:
            # API key configs keep the pre-existing gate: only a
            # complete project+region pair selects Vertex express mode, so a
            # project-only config never moves off the generativelanguage endpoint.
            kwargs.update(vertexai=True, project=project, location=location)
        return genai.Client(**kwargs)

    @staticmethod
    def _tool_response(message: ProviderMessage) -> dict[str, object]:
        # A failed tool reports its error as the response body; the flag travels
        # with it so the model can tell a failure apart from a plain result.
        response: dict[str, object] = {"content": message.content or ""}
        if message.is_error:
            response["is_error"] = True
        return response

    def _contents(self, request: ProviderTurnRequest) -> tuple[str | None, list[dict[str, object]]]:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        if any(bool(m.images) for m in request.messages):
            metadata = static_catalog_metadata(provider_name, request.model_name or "")
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
        system: list[str] = []
        contents: list[dict[str, object]] = []
        wire_ids: dict[str, str | None] = {}
        messages = request.messages
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role == "system":
                if message.content:
                    system.append(message.content)
                index += 1
                continue
            if message.role == "assistant":
                wire_ids.clear()
                replay = json_wire_object(message.metadata).get("google_parts")
                if replay is not None:
                    if not isinstance(replay, list):
                        raise ValueError("Google replay parts must be an array")
                    replayed_parts = [types.Part.model_validate(part) for part in replay]
                    original_calls = [part.function_call for part in replayed_parts if part.function_call is not None]
                    for runtime_call, original_call in zip(message.tool_calls, original_calls, strict=True):
                        if runtime_call.tool_call_id is not None:
                            wire_ids[runtime_call.tool_call_id] = original_call.id
                    contents.append({"role": "model", "parts": replayed_parts})
                    index += 1
                    continue
            if message.role == "assistant" and message.tool_calls:
                parts: list[dict[str, object]] = []
                if message.content:
                    parts.append({"text": message.content})
                for call in message.tool_calls:
                    parts.append(
                        {
                            "function_call": {
                                "id": call.tool_call_id,
                                "name": call.tool_name,
                                "args": json_wire_object(call.arguments or {}),
                            }
                        }
                    )
                contents.append({"role": "model", "parts": parts})
                index += 1
                continue
            if message.role == "tool":
                # One user turn per consecutive batch: Gemini pairs each
                # ``function_response`` with the ``function_call`` it answers,
                # and a batch is exactly the tool messages the model's own
                # assistant turn called. Reasoning is never replayed here.
                parts = []
                while index < len(messages) and messages[index].role == "tool":
                    following = messages[index]
                    parts.append(
                        {
                            "function_response": {
                                "id": wire_ids.get(following.tool_call_id or "", following.tool_call_id),
                                "name": following.tool_name,
                                "response": self._tool_response(following),
                            }
                        }
                    )
                    if following.images:
                        for img in following.images:
                            parts.append({"inline_data": {"mime_type": img.media_type, "data": img.data}})
                    index += 1
                contents.append({"role": "user", "parts": parts})
                continue
            role = "model" if message.role == "assistant" else "user"
            parts: list[dict[str, object]] = [{"text": message.content}] if message.content else []
            if message.images:
                for img in message.images:
                    parts.append({"inline_data": {"mime_type": img.media_type, "data": img.data}})
            if parts:
                contents.append({"role": role, "parts": parts})
            index += 1
        return ("\n\n".join(system) if system else None), contents

    def _config(self, request: ProviderTurnRequest, system: str | None) -> types.GenerateContentConfig:
        metadata = static_catalog_metadata(request.provider_name or self.name, request.model_name or "")
        declarations = (
            [
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=json_wire_object(tool.input_schema) or {"type": "object", "properties": {}},
                )
                for tool in request.available_tools
            ]
            if metadata is None or metadata.supports_tools is not False
            else []
        )
        tools = [types.Tool(function_declarations=declarations)] if declarations else None
        max_output_tokens = metadata.max_output_tokens if metadata is not None else None
        # ``{session_id}`` placeholders resolve per request from the request's own
        # mapping; the client that carries them is reused across conversations.
        session_id = request.metadata.get("session_id")
        extra_headers = resolve_extra_request_headers(
            self.extra_request_headers,
            session_id if isinstance(session_id, str) else None,
        )
        return types.GenerateContentConfig(
            system_instruction=system,
            tools=tools,
            max_output_tokens=max_output_tokens,
            thinking_config=self._thinking_config(request),
            # Request-scoped headers ride on the config's ``http_options``: the
            # SDK patches them over the client's own options for this request
            # only, so one client can serve every conversation and the gateway's
            # own ``Content-Type``/credential headers stay in place.
            http_options=types.HttpOptions(headers=extra_headers) if extra_headers else None,
        )

    def _thinking_config(self, request: ProviderTurnRequest) -> types.ThinkingConfig | None:
        """Translate the canonical reasoning effort into Gemini thinking settings.

        The row's ``mode`` picks the knob: ``google-level`` models take the level
        enum (``mapEffortToGoogleThinkingLevel``), every other Google model takes a
        token budget (``getGoogleBudget``: the row's own table, else ``-1`` for the
        server's dynamic budget). No hint means "leave the request untouched"; an
        explicit ``off`` disables thinking instead of silently dropping the field.
        """
        if request.reasoning_effort is None:
            return None
        metadata = static_catalog_metadata(request.provider_name or self.name, request.model_name or "")
        if metadata is not None and metadata.supports_reasoning is False:
            return None
        effort = normalize_reasoning_effort(request.reasoning_effort)
        if isinstance(effort, int):
            return types.ThinkingConfig(thinking_budget=effort, include_thoughts=effort > 0)
        if effort is None:
            return None
        rule = thinking_rule_for(request.provider_name or self.name, request.model_name or "")
        supported = metadata.supported_effort_levels if metadata is not None else None
        effort = clamp_effort_to_supported(effort, supported)
        if rule.mode == "google-level":
            if effort == "off":
                # Gemini 3 has no fully disabled level below MINIMAL, so "off"
                # asks for the lowest level and suppresses thought summaries.
                return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL, include_thoughts=False)
            return types.ThinkingConfig(thinking_level=_GOOGLE_THINKING_LEVELS[effort], include_thoughts=True)
        if effort == "off":
            return types.ThinkingConfig(thinking_budget=0, include_thoughts=False)
        budget = rule.budget_for(effort)
        # ``-1`` is the SDK's own "no explicit budget" sentinel: the model picks its
        # own dynamic budget instead of a fixed one. It is never a user-visible number.
        return types.ThinkingConfig(thinking_budget=budget if budget is not None else -1, include_thoughts=True)

    @staticmethod
    def _raise_if_prompt_blocked(response: object, *, provider_name: str, model_name: str) -> None:
        feedback = getattr(response, "prompt_feedback", None)
        raw_reason = getattr(feedback, "block_reason", None)
        token = str(getattr(raw_reason, "name", raw_reason)).split(".")[-1].strip().upper()
        if raw_reason is not None and token and token not in {"0", "NONE", "BLOCK_REASON_UNSPECIFIED"}:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider blocked the prompt",
                retryable=False,
                details={"source": "api", "reason": "prompt_blocked", "block_reason": token},
            )

    @staticmethod
    def _map_exception(exc: Exception, *, provider_name: str, model_name: str, source: ProviderErrorSource) -> ProviderExecutionError:
        if isinstance(exc, GoogleAPIError):
            payload = dict(exc.details) if isinstance(exc.details, Mapping) else {}
            payload["status_code"] = exc.code
            if isinstance(exc.message, str):
                payload["message"] = exc.message
            return provider_execution_error_from_payload(
                provider_name=provider_name,
                model_name=model_name,
                payload=payload,
                source=source,
            )
        return fallback_execution_error(
            exc,
            provider_name=provider_name,
            model_name=model_name,
            source=source,
            network_error=isinstance(exc, httpx.TransportError),
        )

    @staticmethod
    def _parts(response: object) -> Iterator[object]:
        candidates = getattr(response, "candidates", None)
        if not isinstance(candidates, list) or not candidates:
            return
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None)
        if isinstance(parts, list):
            yield from parts

    @staticmethod
    def _signed_parts_metadata(parts: list[object]) -> dict[str, object]:
        if not any(getattr(part, "thought_signature", None) for part in parts):
            return {}
        return {"google_parts": [types.Part.model_validate(part).model_dump(mode="json", exclude_none=True) for part in parts]}

    def _events_from_response(self, response: object, request: ProviderTurnRequest, *, ordinal_offset: int = 0) -> Iterator[ProviderStreamEvent]:
        ordinal = ordinal_offset
        for part in self._parts(response):
            if getattr(part, "function_call", None) is not None:
                call = self._tool_call_from_part(part, request, ordinal=ordinal)
                if call is None:
                    continue
                yield ProviderStreamEvent(
                    kind="tool_call_start",
                    channel="tool",
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    tool_call_ordinal=ordinal,
                )
                yield ProviderStreamEvent(
                    kind="tool_call_end",
                    channel="tool",
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    tool_call_ordinal=ordinal,
                    parsed_arguments=dict(call.arguments),
                )
                ordinal += 1
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                channel = "reasoning" if bool(getattr(part, "thought", False)) else "text"
                yield ProviderStreamEvent(kind="delta", channel=channel, text=text)

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        try:
            system, contents = self._contents(request)
            response = self._client(provider_name=provider_name, model_name=model_name).models.generate_content(
                model=model_name,
                contents=contents,
                config=self._config(request, system),
            )
            candidates = getattr(response, "candidates", None)
            if not isinstance(candidates, list) or not candidates:
                self._raise_if_prompt_blocked(response, provider_name=provider_name, model_name=model_name)
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider response contained no candidates",
                    retryable=False,
                    details={"source": "api", "reason": "malformed_response"},
                )
            raw_finish = getattr(candidates[0], "finish_reason", None)
            finish = GoogleWireCodec.done_reason(raw_finish)
            # Missing and empty SDK values are not provider-reported reasons;
            # unknown enum values remain reported and are retained for diagnostics.
            finish_reason_reported = GoogleWireCodec.finish_reason_reported(raw_finish)
            tool_calls: list[ToolCall] = []
            reasoning_parts: list[str] = []
            text_parts: list[str] = []
            ordinal = 0
            parts = list(self._parts(response))
            for part in parts:
                text = getattr(part, "text", None)
                if bool(getattr(part, "thought", False)):
                    if isinstance(text, str) and text:
                        reasoning_parts.append(text)
                    continue
                if isinstance(text, str) and text:
                    text_parts.append(text)
                tool_call = self._tool_call_from_part(part, request, ordinal=ordinal)
                if tool_call is not None:
                    tool_calls.append(tool_call)
                    ordinal += 1
            metadata = self._signed_parts_metadata(parts)
            if finish == "unknown" and finish_reason_reported:
                metadata["finish_reason_raw"] = str(getattr(raw_finish, "name", raw_finish))
            return ProviderTurnResult(
                tool_calls=tuple(tool_calls),
                output="".join(text_parts),
                reasoning="".join(reasoning_parts) or None,
                usage=GoogleWireCodec.usage(response),
                done_reason=finish,
                finish_reason_reported=finish_reason_reported,
                metadata=metadata or None,
            )
        except ProviderExecutionError:
            raise
        except Exception as exc:
            raise self._map_exception(exc, provider_name=provider_name, model_name=model_name, source="api") from exc

    def _tool_call_from_part(self, part: object, request: ProviderTurnRequest, *, ordinal: int) -> ToolCall | None:
        function_call = getattr(part, "function_call", None)
        if function_call is None:
            return None
        provider_tool_name = getattr(function_call, "name", None)
        args = getattr(function_call, "args", None)
        if not isinstance(provider_tool_name, str) or not provider_tool_name:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=request.provider_name or self.name,
                model_name=request.model_name or "unknown",
                message="provider returned a malformed function call",
                retryable=False,
                details={"source": "api", "reason": "malformed_tool_call"},
            )
        if args is None:
            parsed_args: dict[str, object] = {}
        elif isinstance(args, Mapping):
            parsed_args = dict(args)
        else:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=request.provider_name or self.name,
                model_name=request.model_name or "unknown",
                message="provider returned malformed function-call arguments",
                retryable=False,
                details={"source": "api", "reason": "malformed_tool_call_arguments"},
            )
        return ToolCall(
            tool_name=provider_tool_name,
            arguments=parsed_args,
            tool_call_id=normalize_tool_call_id(getattr(function_call, "id", None), fallback=f"{provider_tool_name}_{ordinal + 1}"),
        )

    def _timeout(self) -> float:
        return self.config.timeout_seconds if self.config is not None and self.config.timeout_seconds is not None else 300.0

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        if abort_signal_cancelled(request):
            yield from cancelled_events()
            return
        try:
            system, contents = self._contents(request)
            latest_usage: ProviderTokenUsage | None = None
            raw_finish: object | None = None
            saw_candidates = False
            tool_call_ordinal = 0
            parts: list[object] = []
            raw_stream = self._client(provider_name=provider_name, model_name=model_name).models.generate_content_stream(
                model=model_name, contents=contents, config=self._config(request, system)
            )
            with closing(
                iter_stream_with_timeout(
                    raw_stream,
                    timeout_seconds=self._timeout(),
                    provider_name=provider_name,
                    model_name=model_name,
                    aborted=lambda: abort_signal_cancelled(request),
                )
            ) as stream:
                for chunk in stream:
                    if abort_signal_cancelled(request):
                        break
                    observed_usage = GoogleWireCodec.usage(chunk)
                    if observed_usage is not None:
                        latest_usage = observed_usage
                    parts.extend(self._parts(chunk))
                    self._raise_if_prompt_blocked(chunk, provider_name=provider_name, model_name=model_name)
                    candidates = getattr(chunk, "candidates", None)
                    if isinstance(candidates, list) and candidates:
                        saw_candidates = True
                    candidate_finish = getattr(candidates[0], "finish_reason", None) if isinstance(candidates, list) and candidates else None
                    if GoogleWireCodec.finish_reason_reported(candidate_finish):
                        raw_finish = candidate_finish
                    for stream_event in self._events_from_response(chunk, request, ordinal_offset=tool_call_ordinal):
                        yield stream_event
                        if stream_event.kind == "tool_call_end":
                            tool_call_ordinal += 1
            if abort_signal_cancelled(request):
                yield from cancelled_events()
                return
            if raw_finish is None:
                if not saw_candidates:
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_name,
                        model_name=model_name,
                        message="provider stream contained no candidates",
                        retryable=False,
                        details={"source": "stream", "reason": "malformed_response"},
                    )
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider_name,
                    model_name=model_name,
                    message="provider stream ended without a terminal finish_reason",
                    retryable=False,
                    details={"source": "stream", "reason": "missing_terminal_event"},
                )
            done_reason = GoogleWireCodec.done_reason(raw_finish)
            raw_token = str(getattr(raw_finish, "name", raw_finish))
            stream_metadata = self._signed_parts_metadata(parts)
            if done_reason == "unknown":
                stream_metadata["finish_reason_raw"] = raw_token
            yield ProviderStreamEvent(kind="done", done_reason=done_reason, metadata=stream_metadata or None, usage=latest_usage)
        except ProviderExecutionError as exc:
            if exc.kind != "cancelled":
                raise
            yield ProviderStreamEvent(kind="error", channel="error", error=exc.message, error_kind="cancelled")
            yield ProviderStreamEvent(kind="done", done_reason="cancelled")
        except Exception as exc:
            raise self._map_exception(exc, provider_name=provider_name, model_name=model_name, source="stream") from exc


__all__ = ["GoogleGenAIProvider"]
