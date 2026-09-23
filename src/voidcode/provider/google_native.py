from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from google import genai
from google.genai import types
from google.oauth2 import service_account

from ..tools.contracts import ToolCall
from ..tools.output import redacted_argument_keys_for_tool, sanitize_tool_arguments, strip_redaction_sentinels
from .config import GoogleProviderConfig
from .errors import redact_provider_error_details, redact_provider_error_message
from .protocol import ProviderExecutionError, ProviderStreamEvent, ProviderTokenUsage, ProviderTurnRequest, ProviderTurnResult
from .reasoning_effort import (
    REASONING_EFFORT_HIGH,
    REASONING_EFFORT_LOW,
    REASONING_EFFORT_MAX,
    REASONING_EFFORT_MEDIUM,
    REASONING_EFFORT_MINIMAL,
    REASONING_EFFORT_OFF,
    REASONING_EFFORT_XHIGH,
    clamp_effort_to_supported,
    normalize_reasoning_effort,
)

# Gemini thinking is configured through ``types.ThinkingConfig``. The installed
# SDK types document their own conventions: ``thinking_budget`` accepts ``0``
# (disabled) and ``-1`` (automatic) and states that every other value and
# allowed range is model dependent, while ``thinking_level`` is the Gemini 3
# enum ``MINIMAL``/``LOW``/``MEDIUM``/``HIGH``. Graded budgets therefore use the
# same canonical ladder as the other adapters and ``xhigh``/``max`` clamp to
# ``high`` (mirroring ``reasoning_effort.map_effort_for_provider`` for Google).
_GOOGLE_THINKING_BUDGETS: dict[str, int] = {
    REASONING_EFFORT_MINIMAL: 1024,
    REASONING_EFFORT_LOW: 2048,
    REASONING_EFFORT_MEDIUM: 4096,
    REASONING_EFFORT_HIGH: 8192,
    REASONING_EFFORT_XHIGH: 8192,
    REASONING_EFFORT_MAX: 8192,
}
_GOOGLE_THINKING_LEVELS: dict[str, types.ThinkingLevel] = {
    REASONING_EFFORT_MINIMAL: types.ThinkingLevel.MINIMAL,
    REASONING_EFFORT_LOW: types.ThinkingLevel.LOW,
    REASONING_EFFORT_MEDIUM: types.ThinkingLevel.MEDIUM,
    REASONING_EFFORT_HIGH: types.ThinkingLevel.HIGH,
    REASONING_EFFORT_XHIGH: types.ThinkingLevel.HIGH,
    REASONING_EFFORT_MAX: types.ThinkingLevel.HIGH,
}


def _uses_thinking_level(model_name: str | None) -> bool:
    """Gemini 3 selects thinking with the level enum; older families use a token budget."""
    return (model_name or "").strip().lower().startswith("gemini-3")


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
            fallback_allowed=True,
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
            fallback_allowed=True,
            details={"reason": "unreadable_or_invalid_service_account_file", "exception_type": type(exc).__name__},
        ) from exc


def _service_account_project_id(credentials: Any) -> str | None:
    """Return the project named by the loaded credentials, when they expose one."""
    project_id = getattr(credentials, "project_id", None)
    return project_id if isinstance(project_id, str) and project_id else None


# A declared request header may name the conversation with ``{session_id}``. The
# SDK client is reused across turns, so a value resolved at construction time
# would freeze the first conversation's id; it is resolved per request instead.
_SESSION_ID_PLACEHOLDER = "{session_id}"


def _resolve_extra_request_headers(declared: Mapping[str, str], session_id: str | None) -> dict[str, str]:
    """Resolve declared request headers for one turn, dropping ones with no value.

    A declaration whose value names ``{session_id}`` is omitted when the request
    carries no session id: an empty header is not a routable conversation.
    """
    resolved: dict[str, str] = {}
    for name, value in declared.items():
        if _SESSION_ID_PLACEHOLDER in value:
            if not session_id:
                continue
            value = value.replace(_SESSION_ID_PLACEHOLDER, session_id)
        resolved[name] = value
    return resolved


@dataclass(slots=True)
class _OwnedClient:
    """One-slot holder letting a frozen provider own a single SDK client."""

    value: Any | None = None


@dataclass(frozen=True, slots=True)
class GoogleGenAIProvider:
    name: str = "google"
    config: GoogleProviderConfig | None = None
    client: Any | None = None
    # Headers this gateway requires on every request it serves. They reach the
    # wire through the request config's ``http_options``: the SDK patches them
    # onto the client's own options per request, so they merge with (never
    # replace) the client's ``Content-Type``/credential headers, and the request
    # body is unaffected because the SDK pops ``config`` from it.
    extra_request_headers: Mapping[str, str] = field(default_factory=dict)
    # One client -- and therefore one HTTP connection pool -- per provider,
    # reused across every turn. A client owns no conversation state: the headers
    # above are patched per request, so reusing it is correct rather than merely
    # cheaper. Only the first-use race can drop one losing client.
    _owned_client: _OwnedClient = field(default_factory=_OwnedClient, compare=False, repr=False)

    def _client(self, *, provider_name: str, model_name: str) -> Any:
        if self.client is not None:
            return self.client
        owned = self._owned_client
        if owned.value is None:
            owned.value = self._create_client(provider_name=provider_name, model_name=model_name)
        return owned.value

    def _create_client(self, *, provider_name: str, model_name: str) -> Any:
        config = self.config
        auth = None if config is None else config.auth
        credentials: Any = None
        api_key: str | None = None
        if auth is not None and auth.method == "service_account":
            # A configured service account file is authoritative for this method:
            # falling back to ``GOOGLE_API_KEY`` would silently ignore it.
            credentials = _service_account_credentials(auth.service_account_json_path, provider_name=provider_name, model_name=model_name)
        elif auth is not None:
            api_key = auth.api_key if auth.method == "api_key" else auth.access_token
        if credentials is None:
            api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if credentials is not None:
            # ``credentials`` and ``api_key`` are mutually exclusive in the SDK.
            kwargs["credentials"] = credentials
        base_url = None if config is None else config.base_url
        if base_url:
            # A configured endpoint replaces every SDK default (the SDK's own
            # ``_base_url`` resolution gives ``http_options.base_url`` precedence
            # over its host defaults, and its client skips the ADC project lookup
            # once one is set). The SDK would then append its own version segment
            # -- ``v1beta`` or ``v1beta1`` -- to a URL that already names one, so
            # the version is cleared: a configured base URL is a complete
            # endpoint root and the SDK appends only the resource path.
            kwargs["http_options"] = types.HttpOptions(base_url=base_url, api_version="")
        project = None if config is None else config.project
        location = None if config is None else config.region
        if credentials is not None:
            # The SDK resolves an unset project through its own ADC lookup, which
            # fails for users who configured only a service account file.
            if project is None:
                project = _service_account_project_id(credentials)
            # Explicit credentials always select the Vertex AI surface.
            kwargs.update(vertexai=True, project=project, location=location)
        elif api_key is None:
            # No credential material at all, so the SDK falls back to ambient ADC.
            # Either half of project/location selects Vertex AI; the previous
            # ``project and region`` gate silently skipped ADC-only users.
            if project is not None or location is not None:
                kwargs.update(vertexai=True, project=project, location=location)
        elif project is not None and location is not None:
            # API key / access token configs keep the pre-existing gate: only a
            # complete project+region pair selects Vertex express mode, so a
            # project-only config never moves off the generativelanguage endpoint.
            kwargs.update(vertexai=True, project=project, location=location)
        return genai.Client(**kwargs)

    @staticmethod
    def _tool_name_maps(request: ProviderTurnRequest) -> tuple[dict[str, str], dict[str, str]]:
        forward: dict[str, str] = {}
        reverse: dict[str, str] = {}
        for name in dict.fromkeys(tool.name for tool in request.available_tools if tool.name):
            forward[name] = name
            reverse[name] = name
        return forward, reverse

    @staticmethod
    def _visible_arguments(tool_name: str | None, arguments: Mapping[str, object]) -> dict[str, object]:
        sanitized = sanitize_tool_arguments(dict(arguments))
        stripped = strip_redaction_sentinels(sanitized, redacted_keys=redacted_argument_keys_for_tool(tool_name))
        return stripped if isinstance(stripped, dict) else {}

    def _contents(self, request: ProviderTurnRequest) -> tuple[str | None, list[dict[str, object]]]:
        system: list[str] = []
        contents: list[dict[str, object]] = []
        for segment in request.assembled_context.segments:
            if segment.role == "system":
                if segment.content:
                    system.append(segment.content)
                continue
            role = "model" if segment.role == "assistant" else "user"
            parts: list[dict[str, object]] = []
            if segment.role == "assistant" and segment.tool_name:
                parts.append(
                    {
                        "function_call": {
                            "name": segment.tool_name,
                            "args": self._visible_arguments(segment.tool_name, segment.tool_arguments or {}),
                        }
                    }
                )
            elif segment.role == "tool":
                parts.append(
                    {
                        "function_response": {
                            "name": segment.tool_name or "voidcode_tool",
                            "response": {"content": segment.content or ""},
                        }
                    }
                )
            elif segment.content:
                parts.append({"text": segment.content})
            if parts:
                contents.append({"role": role, "parts": parts})
        return ("\n\n".join(system) if system else None), contents

    def _config(self, request: ProviderTurnRequest, system: str | None) -> types.GenerateContentConfig:
        declarations: list[types.FunctionDeclaration] = []
        for tool in request.available_tools:
            declarations.append(
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.input_schema or {"type": "object", "properties": {}},
                )
            )
        tools = [types.Tool(function_declarations=declarations)] if declarations else None
        max_output_tokens = request.model_metadata.max_output_tokens if request.model_metadata is not None else None
        extra_headers = _resolve_extra_request_headers(self.extra_request_headers, request.session_id)
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

        No hint means "leave the request untouched" (the server default applies).
        An explicit ``off`` disables thinking instead of silently dropping the
        field. ``xhigh``/``max`` clamp to the highest level this mapping knows.
        """
        if not request.reasoning_effort:
            return None
        effort = normalize_reasoning_effort(request.reasoning_effort)
        supported = request.model_metadata.supported_effort_levels if request.model_metadata is not None else None
        effort = clamp_effort_to_supported(effort, supported)
        if _uses_thinking_level(request.model_name):
            if effort == REASONING_EFFORT_OFF:
                # Gemini 3 has no fully disabled level below MINIMAL, so "off"
                # asks for the lowest level and suppresses thought summaries.
                return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL, include_thoughts=False)
            return types.ThinkingConfig(
                thinking_level=_GOOGLE_THINKING_LEVELS.get(effort, types.ThinkingLevel.HIGH),
                include_thoughts=True,
            )
        if effort == REASONING_EFFORT_OFF:
            return types.ThinkingConfig(thinking_budget=0, include_thoughts=False)
        return types.ThinkingConfig(thinking_budget=_GOOGLE_THINKING_BUDGETS.get(effort, -1), include_thoughts=True)

    @staticmethod
    def _finish_reason(value: object) -> str:
        raw = getattr(value, "name", value)
        normalized = str(raw).split(".")[-1].lower()
        if normalized in {"stop", "end_turn"}:
            return "stop"
        if normalized in {"max_tokens", "length"}:
            return "length"
        if normalized in {"safety", "recitation", "blocklist", "prohibited_content", "spii", "other"}:
            return "content_filter"
        if normalized in {"malformed_function_call", "unexpected_tool_call", "no_image"}:
            return "error"
        return "unknown"

    @staticmethod
    def _usage(response: object) -> ProviderTokenUsage | None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None
        input_tokens = getattr(usage, "prompt_token_count", None)
        output_tokens = getattr(usage, "candidates_token_count", None)
        cache_read = getattr(usage, "cached_content_token_count", None)
        if not any(isinstance(value, int) for value in (input_tokens, output_tokens, cache_read)):
            return None
        return ProviderTokenUsage(
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            cache_read_tokens=cache_read if isinstance(cache_read, int) else None,
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

    def _events_from_response(self, response: object, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        reverse = self._tool_name_maps(request)[1]
        ordinal = 0
        for part in self._parts(response):
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                name = getattr(function_call, "name", None)
                if isinstance(name, str) and name:
                    args = getattr(function_call, "args", {})
                    parsed = dict(args) if isinstance(args, Mapping) else {}
                    tool_id = f"{name}_{ordinal + 1}"
                    yield ProviderStreamEvent(
                        kind="tool_call_start",
                        channel="tool",
                        tool_call_id=tool_id,
                        tool_name=reverse.get(name, name),
                        tool_call_ordinal=ordinal,
                    )
                    yield ProviderStreamEvent(
                        kind="tool_call_end",
                        channel="tool",
                        tool_call_id=tool_id,
                        tool_name=reverse.get(name, name),
                        tool_call_ordinal=ordinal,
                        parsed_arguments=parsed,
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
            text = getattr(response, "text", "") or ""
            candidates = getattr(response, "candidates", None)
            raw_finish = getattr(candidates[0], "finish_reason", None) if isinstance(candidates, list) and candidates else None
            finish = self._finish_reason(raw_finish)
            # Reported only when the candidate carried a finish reason: an omitted
            # one is the silent-truncation case, while an unrecognized enum value is
            # still a reported token.
            finish_reason_reported = raw_finish is not None
            tool_calls: list[ToolCall] = []
            reasoning_parts: list[str] = []
            ordinal = 0
            for part in self._parts(response):
                if bool(getattr(part, "thought", False)):
                    # The SDK's ``response.text`` skips thought parts, so the
                    # non-streaming path must collect them here to persist the
                    # same reasoning the streaming path emits as deltas.
                    thought = getattr(part, "text", None)
                    if isinstance(thought, str) and thought:
                        reasoning_parts.append(thought)
                    continue
                tool_call = self._tool_call_from_part(part, request, ordinal=ordinal)
                if tool_call is not None:
                    tool_calls.append(tool_call)
                    ordinal += 1
            return ProviderTurnResult(
                tool_calls=tuple(tool_calls),
                output=text,
                reasoning="".join(reasoning_parts) or None,
                usage=self._usage(response),
                done_reason=cast(Any, finish),
                finish_reason_reported=finish_reason_reported,
                metadata=(
                    {"finish_reason_raw": str(getattr(raw_finish, "name", raw_finish))} if finish == "unknown" and finish_reason_reported else None
                ),
            )
        except ProviderExecutionError:
            raise
        except Exception as exc:
            message = redact_provider_error_message(str(exc)) or "provider request failed"
            raise ProviderExecutionError(
                kind="transient_failure",
                provider_name=provider_name,
                model_name=model_name,
                message=message,
                retryable=True,
                fallback_allowed=True,
                details=cast(dict[str, object], redact_provider_error_details({"exception_type": type(exc).__name__, "exception_message": message})),
            ) from exc

    def _tool_call_from_part(self, part: object, request: ProviderTurnRequest, *, ordinal: int) -> ToolCall | None:
        function_call = getattr(part, "function_call", None)
        name = getattr(function_call, "name", None) if function_call is not None else None
        if not isinstance(name, str) or not name:
            return None
        args = getattr(function_call, "args", {})
        return ToolCall(
            tool_name=self._tool_name_maps(request)[1].get(name, name),
            arguments=dict(args) if isinstance(args, Mapping) else {},
            # Mirrors the streaming ordinal so the same response yields the same
            # ids on both paths, and repeated calls to one function stay distinct.
            tool_call_id=f"{name}_{ordinal + 1}",
        )

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider_name = request.provider_name or self.name
        model_name = request.model_name or "unknown"
        try:
            system, contents = self._contents(request)
            stream = self._client(provider_name=provider_name, model_name=model_name).models.generate_content_stream(
                model=model_name,
                contents=contents,
                config=self._config(request, system),
            )
            last: object | None = None
            for chunk in stream:
                last = chunk
                yield from self._events_from_response(chunk, request)
            candidates = getattr(last, "candidates", None)
            raw_finish = getattr(candidates[0], "finish_reason", None) if isinstance(candidates, list) and candidates else None
            # An unrecognized/omitted finish reason is still a terminal response: it
            # resolves to the canonical ``unknown`` reason, which the graph treats as
            # a completed, stop-equivalent state (never a user-visible failure).
            done_reason = self._finish_reason(raw_finish)
            stream_metadata: dict[str, object] | None = None
            if done_reason == "unknown" and raw_finish is not None:
                # Reported but unrecognized: carry the token so the graph's
                # finish_reason_reported diagnostics read it as reported.
                stream_metadata = {"finish_reason_raw": str(getattr(raw_finish, "name", raw_finish))}
            yield ProviderStreamEvent(
                kind="done", done_reason=cast(Any, done_reason), metadata=stream_metadata, usage=self._usage(last) if last is not None else None
            )
        except ProviderExecutionError:
            raise
        except Exception as exc:
            message = redact_provider_error_message(str(exc)) or "provider request failed"
            raise ProviderExecutionError(
                kind="transient_failure",
                provider_name=provider_name,
                model_name=model_name,
                message=message,
                retryable=True,
                fallback_allowed=True,
                details=cast(dict[str, object], redact_provider_error_details({"exception_type": type(exc).__name__, "exception_message": message})),
            ) from exc


__all__ = ["GoogleGenAIProvider"]
